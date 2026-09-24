# -*- coding: utf-8 -*-
"""
webui.py —— 扫号工具的网页端控制面板
================================================================
给现有 exe 加一个 `--web` 模式，不另起程序、不引入任何第三方依赖
（只用 Python 标准库），直接打进现有的 PyInstaller 包。

设计要点
----------------------------------------------------------------
· **只监听 127.0.0.1** —— 公网暴露交给 Cloudflare Tunnel / 反向代理。
  服务器本身**不开任何入站端口**，真实 IP 不暴露。
· **登录认证**：团队多人账号，PBKDF2 存哈希，会话用随机 token + HttpOnly Cookie。
· **扫描在后台线程跑** —— 面板本身不被长任务卡死。
· **实时日志靠轮询文件偏移** —— 比 SSE/WebSocket 简单得多，且断线重连天然正确。
· **零依赖** —— 只用 http.server / hashlib / hmac / secrets 这些标准库。

用法
----------------------------------------------------------------
    wmscan.exe --web --port 8080
    wmscan.exe --web --port 8080 --bind 127.0.0.1

首次启动会生成一个管理员账号，密码打印在控制台和 `webui/初始密码.txt`。
"""
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def say(*a):
    try:
        print(*a, flush=True)
    except Exception:
        pass


# ================================================================ 目录与文件
def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE = app_dir()
WEB_DIR = os.path.join(BASE, "webui")
USERS_F = os.path.join(WEB_DIR, "users.json")
SESS_F = os.path.join(WEB_DIR, "sessions.json")
HIST_F = os.path.join(WEB_DIR, "history.jsonl")
OUT_DIR = os.path.join(BASE, "out")
LOG_DIR = os.path.join(BASE, "logs")
SCAN_LOG = os.path.join(BASE, "scan.log")


def _ensure_dirs():
    for d in (WEB_DIR, OUT_DIR, LOG_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


def _find_file(*names):
    """在 BASE 下按候选名字找文件（中文名 / ASCII 名都试）"""
    for n in names:
        p = os.path.join(BASE, n)
        if os.path.exists(p):
            return p
    return os.path.join(BASE, names[0])      # 不存在就返回第一个，供写入


ACCOUNTS_F = _find_file("账号.txt", "accounts.txt")
PROXIES_F = _find_file("代理.txt", "proxies.txt", "proxies_arx.txt")


# ================================================================ 账号与会话
def _hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000)
    return salt, dk.hex()


def _load_json(path, default):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, obj):
    try:
        _ensure_dirs()
        tmp = path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        say("[webui] 写 %s 失败: %s" % (path, e))


USERS = {}
SESSIONS = {}
_LOCK = threading.RLock()


def _init_users():
    global USERS
    _ensure_dirs()
    USERS = _load_json(USERS_F, {})
    if not USERS:
        pw = secrets.token_urlsafe(12)
        salt, h = _hash_pw(pw)
        USERS = {"admin": {"salt": salt, "hash": h, "role": "admin",
                           "created": time.strftime("%Y-%m-%d %H:%M:%S")}}
        _save_json(USERS_F, USERS)
        p = os.path.join(WEB_DIR, "初始密码.txt")
        try:
            with io.open(p, "w", encoding="utf-8") as f:
                f.write("用户名: admin\n密码:   %s\n"
                        "（登录后请到「设置」里改掉，并删掉这个文件）\n" % pw)
        except Exception:
            pass
        say("=" * 62)
        say("  ★ 首次启动，已创建管理员账号")
        say("     用户名: admin")
        say("     密码  : %s" % pw)
        say("     也写到了: %s" % p)
        say("=" * 62)
    SESSIONS.clear()


def verify_login(user, pw):
    u = USERS.get(user)
    if not u:
        return False
    _, h = _hash_pw(pw, u["salt"])
    return hmac.compare_digest(h, u["hash"])


def new_session(user):
    tok = secrets.token_urlsafe(32)
    with _LOCK:
        SESSIONS[tok] = {"user": user, "exp": time.time() + 7 * 86400}
    return tok


def session_user(tok):
    if not tok:
        return None
    with _LOCK:
        s = SESSIONS.get(tok)
        if not s:
            return None
        if s["exp"] < time.time():
            SESSIONS.pop(tok, None)
            return None
        s["exp"] = time.time() + 7 * 86400          # 滑动续期
        return s["user"]


def drop_session(tok):
    with _LOCK:
        SESSIONS.pop(tok, None)


# ================================================================ 扫描任务
class ScanJob:
    """一次扫描批次。跑在后台线程，面板只读它的状态。"""

    def __init__(self):
        self.proc = None
        self.thread = None
        self.start_at = None          # 给人看的时间字符串
        self.start_ts = None          # ★ 给算术用的时间戳（float）
        self.end_at = None
        self.exit_code = None
        self.params = {}
        self.log_offset = 0
        self.out_path = ""
        self.tag = ""

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def to_dict(self):
        return {
            "running": self.running,
            "start_at": self.start_at,
            "end_at": self.end_at,
            "exit_code": self.exit_code,
            "params": self.params,
            "out_path": self.out_path,
            "tag": self.tag,
            # ★ 用数值时间戳算耗时 —— 早先拿字符串减 float，直接 TypeError，
            #   表现成「面板接口 500 / 连接被掐断」。
            "elapsed": round(time.time() - self.start_ts, 1) if self.start_ts else 0,
        }


JOB = ScanJob()


def _run_scan_thread(params):
    """在后台线程里跑扫描。

    ★ 直接调 wm_scan.run_scan（同一个进程内），而不是起子进程 ——
      这样 exe 不用再复制一份自己，日志也能直接读文件。
    """
    try:
        import argparse
        import wm_scan
        ns = argparse.Namespace(
            accounts=params["accounts"],
            out=params["out"],
            game=params.get("game", "auto"),
            limit=int(params.get("limit", 0) or 0),
            workers=int(params.get("workers", 3) or 3),
            rate=float(params.get("rate", 0.8) or 0.8),
            parallel=int(params.get("parallel", 2) or 2),
            acct_delay=float(params.get("acct_delay", 3.0) or 3.0),
            retries=int(params.get("retries", 3) or 3),
            fresh=bool(params.get("fresh", True)),
            no_egress_check=bool(params.get("no_egress_check", False)),
            proxy_file=params.get("proxy_file") or None,
            proxy_check=int(params.get("proxy_check", 10) or 10),
            timeout=float(params.get("timeout", 60) or 60),
        )
        rc = wm_scan.run_scan(ns)
        JOB.exit_code = rc
    except Exception as e:
        import traceback
        say("[webui] 扫描异常: %s\n%s" % (e, traceback.format_exc()))
        JOB.exit_code = -1
    finally:
        JOB.end_at = time.strftime("%Y-%m-%d %H:%M:%S")
        _append_history()


def _append_history():
    try:
        rec = {
            "tag": JOB.tag,
            "start": JOB.start_at,
            "end": JOB.end_at,
            "exit_code": JOB.exit_code,
            "params": JOB.params,
            "out": JOB.out_path,
            "out_size": os.path.getsize(JOB.out_path) if os.path.exists(JOB.out_path) else 0,
            "result_lines": _count_lines(JOB.out_path),
        }
        _ensure_dirs()
        with io.open(HIST_F, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _count_lines(p):
    try:
        with io.open(p, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def start_scan(params):
    if JOB.running:
        raise RuntimeError("已有扫描在跑，先停掉它")
    _ensure_dirs()
    tag = time.strftime("%Y%m%d_%H%M%S")
    out = params.get("out") or os.path.join(OUT_DIR, "结果_%s.txt" % tag)
    params["out"] = out

    # 记录日志起点 —— 前端按偏移量拉增量
    JOB.log_offset = os.path.getsize(SCAN_LOG) if os.path.exists(SCAN_LOG) else 0

    JOB.start_at = time.strftime("%Y-%m-%d %H:%M:%S")
    JOB.start_ts = time.time()
    JOB.end_at = None
    JOB.exit_code = None
    JOB.params = dict(params)
    JOB.out_path = out
    JOB.tag = tag
    JOB.thread = threading.Thread(target=_run_scan_thread, args=(params,), daemon=True)
    JOB.thread.start()
    return tag


def stop_scan():
    """请求停止。wm_scan 用全局 STOP_EVENT，置位即可。"""
    try:
        import wm_scan
        wm_scan.STOP_EVENT.set()
        return True
    except Exception:
        return False


# ================================================================ 代理池体检
PROXYCHECK = {"running": False, "done": 0, "total": 0, "result": None, "start": None}


def start_proxy_check(proxy_file, workers=20):
    if PROXYCHECK["running"]:
        raise RuntimeError("体检已经在跑了")

    def work():
        PROXYCHECK.update(running=True, done=0, total=0, result=None,
                          start=time.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            import proxy_pool
            entries = proxy_pool.load(proxy_file)
            PROXYCHECK["total"] = len(entries)
            pool = proxy_pool.ProxyPool(entries, verbose=False)
            pool.check_all(workers=workers)
            good = [e for e in entries if e.get("ok")]
            ips = {e["ip"] for e in good if e.get("ip")}
            cc = {}
            for e in good:
                k = (e.get("cc") or "?")
                cc[k] = cc.get(k, 0) + 1
            PROXYCHECK["result"] = {
                "total": len(entries),
                "good": len(good),
                "unique_ips": len(ips),
                "countries": cc,
                "cn": cc.get("CN", 0),
                "rows": [{"name": e.get("name"), "ip": e.get("ip"), "cc": e.get("cc"),
                          "city": e.get("city"), "isp": e.get("isp"), "ms": e.get("ms"),
                          "ok": e.get("ok")} for e in entries[:300]],
            }
        except Exception as e:
            PROXYCHECK["result"] = {"error": "%s: %s" % (type(e).__name__, e)}
        finally:
            PROXYCHECK["running"] = False

    threading.Thread(target=work, daemon=True).start()


# ================================================================ 系统状态
def ocr_listening():
    try:
        socket.create_connection(("127.0.0.1", 506), timeout=2).close()
        return True
    except Exception:
        return False


def ocr_process_count():
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process -Name '图像识别POST服务' -ErrorAction SilentlyContinue).Count"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=0x08000000, timeout=15)
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return 0


def tasks_list():
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-ScheduledTask -TaskName 'WMRoleScan*' -ErrorAction SilentlyContinue | "
             "ForEach-Object { \"$($_.TaskName)|$($_.State)\" }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=0x08000000, timeout=20)
        out = []
        for ln in (r.stdout or "").splitlines():
            ln = ln.strip()
            if "|" in ln:
                n, s = ln.split("|", 1)
                out.append({"name": n, "state": s})
        return out
    except Exception:
        return []


def disk_info():
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$d=Get-CimInstance Win32_LogicalDisk -Filter \"DeviceID='C:'\"; "
             "\"{0:N1}|{1:N1}\" -f ($d.FreeSpace/1GB),($d.Size/1GB)"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=0x08000000, timeout=20)
        free, total = (r.stdout or "0|0").strip().split("|")[:2]
        return {"free_gb": float(free), "total_gb": float(total)}
    except Exception:
        return {}


def read_tail(path, n=200):
    try:
        with io.open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception:
        return ""


def read_since(path, offset, limit=200_000):
    """从 offset 读增量 —— 前端轮询实时日志用"""
    try:
        size = os.path.getsize(path)
        if offset > size:                 # 文件被截断/轮转了
            offset = 0
        with io.open(path, "rb") as f:
            f.seek(offset)
            data = f.read(limit)
        return data.decode("utf-8", "replace"), offset + len(data)
    except Exception:
        return "", offset


# ================================================================ HTTP 服务
class Handler(BaseHTTPRequestHandler):
    server_version = "WMRoleScanWeb/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 工具 ----------
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False), extra=extra)

    def _err(self, code, msg):
        self._json({"ok": False, "error": msg}, code)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""
        except Exception:
            return b""

    def _json_body(self):
        try:
            return json.loads(self._body().decode("utf-8") or "{}")
        except Exception:
            return {}

    def _cookie(self, name):
        try:
            c = SimpleCookie(self.headers.get("Cookie") or "")
            return c[name].value if name in c else None
        except Exception:
            return None

    def _user(self):
        return session_user(self._cookie("wmsess"))

    def log_message(self, *a):
        pass                                    # 静音，别刷屏

    # ---------- 路由 ----------
    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)

        if p in ("/", "/index.html"):
            return self._send(200, HTML, "text/html; charset=utf-8")
        if p == "/api/ping":
            return self._json({"ok": True, "need_login": self._user() is None})

        # ---- 以下都要登录 ----
        if p.startswith("/api/") and p != "/api/login":
            if not self._user():
                return self._err(401, "未登录")
        if p == "/api/status":
            return self._json(self.status())
        if p == "/api/scan/log":
            off = int((q.get("since") or ["0"])[0])
            txt, new = read_since(SCAN_LOG, off)
            return self._json({"ok": True, "text": txt, "offset": new})
        if p == "/api/scan/result":
            if not os.path.exists(JOB.out_path):
                return self._json({"ok": True, "text": ""})
            return self._json({"ok": True, "text": read_tail(JOB.out_path, 500)})
        if p == "/api/scan/download":
            if not os.path.exists(JOB.out_path):
                return self._err(404, "还没有结果文件")
            with io.open(JOB.out_path, "rb") as f:
                data = f.read()
            fn = os.path.basename(JOB.out_path)
            return self._send(200, data, "text/plain; charset=utf-8",
                              {"Content-Disposition": "attachment; filename*=UTF-8''%s"
                               % _urlquote(fn)})
        if p == "/api/accounts":
            return self._json({"ok": True, "path": ACCOUNTS_F,
                               "text": _read_text(ACCOUNTS_F)})
        if p == "/api/proxies":
            return self._json({"ok": True, "path": PROXIES_F,
                               "text": _read_text(PROXIES_F)})
        if p == "/api/history":
            return self._json({"ok": True, "items": list_history()})
        if p == "/api/history/file":
            fn = (q.get("name") or [""])[0]
            fn = os.path.basename(fn)
            fp = os.path.join(OUT_DIR, fn)
            if not os.path.exists(fp):
                return self._err(404, "找不到该批次结果")
            return self._json({"ok": True, "text": read_tail(fp, 500)})
        if p == "/api/history/download":
            fn = os.path.basename((q.get("name") or [""])[0])
            fp = os.path.join(OUT_DIR, fn)
            if not os.path.exists(fp):
                return self._err(404, "找不到该批次结果")
            with io.open(fp, "rb") as f:
                data = f.read()
            return self._send(200, data, "text/plain; charset=utf-8",
                              {"Content-Disposition": "attachment; filename*=UTF-8''%s"
                               % _urlquote(fn)})
        if p == "/api/proxycheck":
            return self._json({"ok": True, "state": PROXYCHECK})
        if p == "/api/tasks":
            return self._json({"ok": True, "items": tasks_list()})
        if p == "/api/users":
            return self._json({"ok": True,
                               "items": [{"name": k, "role": v.get("role"),
                                          "created": v.get("created")}
                                         for k, v in USERS.items()]})
        return self._err(404, "没有这个接口")

    def do_POST(self):
        p = urlparse(self.path).path

        if p == "/api/login":
            b = self._json_body()
            if verify_login(b.get("user", ""), b.get("password", "")):
                tok = new_session(b["user"])
                return self._json({"ok": True, "user": b["user"]}, extra={
                    "Set-Cookie": "wmsess=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800"
                                  % tok})
            time.sleep(0.6)                      # 慢一点，挡暴力猜
            return self._err(401, "用户名或密码不对")

        if not self._user():
            return self._err(401, "未登录")

        if p == "/api/logout":
            drop_session(self._cookie("wmsess"))
            return self._json({"ok": True}, extra={
                "Set-Cookie": "wmsess=; Path=/; Max-Age=0"})
        if p == "/api/accounts":
            b = self._json_body()
            return self._json(_write_text(ACCOUNTS_F, b.get("text", "")))
        if p == "/api/proxies":
            b = self._json_body()
            return self._json(_write_text(PROXIES_F, b.get("text", "")))
        if p == "/api/scan/start":
            b = self._json_body()
            try:
                b.setdefault("accounts", ACCOUNTS_F)
                b.setdefault("proxy_file", PROXIES_F)
                tag = start_scan(b)
                return self._json({"ok": True, "tag": tag})
            except Exception as e:
                return self._err(400, str(e))
        if p == "/api/scan/stop":
            return self._json({"ok": stop_scan()})
        if p == "/api/proxycheck":
            b = self._json_body()
            try:
                start_proxy_check(b.get("proxy_file") or PROXIES_F,
                                  int(b.get("workers", 20) or 20))
                return self._json({"ok": True})
            except Exception as e:
                return self._err(400, str(e))
        if p == "/api/task/run":
            b = self._json_body()
            name = re.sub(r"[^A-Za-z0-9_\-]", "", b.get("name", ""))
            if not name:
                return self._err(400, "任务名不合法")
            try:
                r = subprocess.run(["schtasks", "/run", "/tn", name],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", creationflags=0x08000000, timeout=30)
                return self._json({"ok": r.returncode == 0, "out": (r.stdout or "") + (r.stderr or "")})
            except Exception as e:
                return self._err(400, str(e))
        if p == "/api/ocr/start":
            try:
                import ocr_service
                threading.Thread(target=lambda: _ocr_bg("start"), daemon=True).start()
                return self._json({"ok": True, "msg": "已在后台拉起 OCR"})
            except Exception as e:
                return self._err(400, str(e))
        if p == "/api/ocr/stop":
            try:
                import ocr_service
                ocr_service.kill()
                return self._json({"ok": True})
            except Exception as e:
                return self._err(400, str(e))
        if p == "/api/user/add":
            b = self._json_body()
            name = re.sub(r"[^A-Za-z0-9_.\-]", "", b.get("user", ""))[:32]
            pw = b.get("password", "")
            if not name or len(pw) < 6:
                return self._err(400, "用户名不合法或密码少于 6 位")
            salt, h = _hash_pw(pw)
            USERS[name] = {"salt": salt, "hash": h, "role": "user",
                           "created": time.strftime("%Y-%m-%d %H:%M:%S")}
            _save_json(USERS_F, USERS)
            return self._json({"ok": True})
        if p == "/api/user/del":
            b = self._json_body()
            n = b.get("user", "")
            if n == "admin":
                return self._err(400, "admin 不能删")
            USERS.pop(n, None)
            _save_json(USERS_F, USERS)
            return self._json({"ok": True})
        if p == "/api/passwd":
            b = self._json_body()
            me = self._user()
            if not verify_login(me, b.get("old", "")):
                return self._err(400, "原密码不对")
            if len(b.get("new", "")) < 6:
                return self._err(400, "新密码少于 6 位")
            salt, h = _hash_pw(b["new"])
            USERS[me].update(salt=salt, hash=h)
            _save_json(USERS_F, USERS)
            return self._json({"ok": True})
        return self._err(404, "没有这个接口")

    # ---------- 状态聚合 ----------
    def status(self):
        return {
            "ok": True,
            "server": {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "host": socket.gethostname(),
                "user": self._user(),
                "pid": os.getpid(),
                "disk": disk_info(),
            },
            "ocr": {"listening": ocr_listening(), "processes": ocr_process_count()},
            "job": JOB.to_dict(),
            "proxycheck": {"running": PROXYCHECK["running"],
                           "done": PROXYCHECK["done"], "total": PROXYCHECK["total"]},
            "files": {
                "accounts": {"path": ACCOUNTS_F, "lines": _count_lines(ACCOUNTS_F),
                             "exists": os.path.exists(ACCOUNTS_F)},
                "proxies": {"path": PROXIES_F, "lines": _count_lines(PROXIES_F),
                            "exists": os.path.exists(PROXIES_F)},
            },
            "tasks": tasks_list(),
            "history_count": _count_lines(HIST_F),
        }


def _ocr_bg(what):
    try:
        import ocr_service
        ocr_service.start(verbose=False)
    except Exception as e:
        say("[webui] OCR 启动失败: %s" % e)


def _read_text(p):
    try:
        with io.open(p, encoding="utf-8-sig", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _write_text(p, text):
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return {"ok": True, "lines": sum(1 for _ in text.splitlines() if _.strip())}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def _urlquote(s):
    from urllib.parse import quote
    return quote(s)


def list_history(limit=200):
    items = []
    try:
        with io.open(HIST_F, encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        items.append(json.loads(ln))
                    except Exception:
                        pass
    except Exception:
        pass
    return list(reversed(items[-limit:]))


# ================================================================ 前端
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>扫号控制台</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#0f1115;color:#e6e8ec;font:14px/1.6 "Microsoft YaHei",system-ui,sans-serif}
a{color:#5aa9ff}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
header{display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid #232833}
h1{font-size:17px;margin:0;font-weight:600}
.sp{flex:1}
.tabs{display:flex;gap:6px;margin:14px 0}
.tabs button{background:#171b23;border:1px solid #262c38;color:#aab2c0;padding:7px 14px;border-radius:8px;cursor:pointer;font-size:13px}
.tabs button.on{background:#1d4ed8;border-color:#1d4ed8;color:#fff}
.card{background:#141821;border:1px solid #232833;border-radius:10px;padding:14px;margin-bottom:12px}
.card h2{font-size:14px;margin:0 0 10px;color:#9aa4b2;font-weight:600}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.kv{display:flex;gap:8px;font-size:13px;margin:4px 0}
.kv b{color:#7d8798;font-weight:400;min-width:88px}
.ok{color:#4ade80}.bad{color:#f87171}.warn{color:#fbbf24}.dim{color:#6b7280}
input,textarea,select,button{font-family:inherit;font-size:13px}
input,textarea,select{background:#0f1319;border:1px solid #2a3140;color:#e6e8ec;border-radius:8px;padding:8px 10px;width:100%}
textarea{min-height:150px;resize:vertical;font-family:Consolas,monospace;font-size:12px}
button.p{background:#1d4ed8;border:0;color:#fff;padding:9px 18px;border-radius:8px;cursor:pointer;font-weight:600}
button.d{background:#7f1d1d;border:0;color:#fff;padding:9px 18px;border-radius:8px;cursor:pointer}
button.s{background:#232a36;border:1px solid #333c4c;color:#cfd6e2;padding:7px 14px;border-radius:8px;cursor:pointer}
button:disabled{opacity:.45;cursor:not-allowed}
pre{background:#0b0e13;border:1px solid #1e2430;border-radius:8px;padding:10px;max-height:420px;overflow:auto;font:12px/1.5 Consolas,monospace;white-space:pre-wrap;word-break:break-all;margin:0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:760px){.grid{grid-template-columns:1fr}}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;background:#232a36;color:#cfd6e2}
.badge.g{background:#14532d;color:#86efac}.badge.r{background:#7f1d1d;color:#fca5a5}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #1e2430}
th{color:#7d8798;font-weight:400}
#login{max-width:340px;margin:14vh auto}
.msg{font-size:13px;min-height:20px;margin-top:8px}
</style></head><body>
<div class="wrap">

<div id="login" class="card" style="display:none">
  <h2>扫号控制台 · 登录</h2>
  <div style="margin:8px 0"><input id="lu" placeholder="用户名" autocomplete="username"></div>
  <div style="margin:8px 0"><input id="lp" type="password" placeholder="密码" autocomplete="current-password"></div>
  <button class="p" style="width:100%" onclick="doLogin()">登录</button>
  <div class="msg bad" id="lmsg"></div>
</div>

<div id="app" style="display:none">
  <header>
    <h1>扫号控制台</h1>
    <span class="badge" id="bServer">—</span>
    <span class="badge" id="bOcr">—</span>
    <span class="sp"></span>
    <span class="dim" id="who"></span>
    <button class="s" onclick="doLogout()">退出</button>
  </header>

  <div class="tabs">
    <button data-t="overview" class="on">概览</button>
    <button data-t="scan">扫描</button>
    <button data-t="history">历史</button>
    <button data-t="proxy">代理池</button>
    <button data-t="settings">设置</button>
  </div>

  <!-- 概览 -->
  <div class="tab" id="t-overview">
    <div class="grid">
      <div class="card"><h2>服务器</h2><div id="ovServer"></div></div>
      <div class="card"><h2>OCR 识别服务</h2><div id="ovOcr"></div>
        <div class="row" style="margin-top:10px">
          <button class="s" onclick="ocrStart()">拉起 OCR</button>
          <button class="s" onclick="ocrStop()">停止 OCR</button>
        </div>
      </div>
      <div class="card"><h2>计划任务</h2><div id="ovTasks"></div></div>
      <div class="card"><h2>输入文件</h2><div id="ovFiles"></div></div>
    </div>
  </div>

  <!-- 扫描 -->
  <div class="tab" id="t-scan" style="display:none">
    <div class="card">
      <h2>参数</h2>
      <div class="grid">
        <div>
          <div class="kv"><b>账号并行</b><input id="pParallel" value="8"></div>
          <div class="kv"><b>账号内并发</b><input id="pWorkers" value="3"></div>
          <div class="kv"><b>请求间隔(秒)</b><input id="pRate" value="0.8"></div>
        </div>
        <div>
          <div class="kv"><b>单请求超时</b><input id="pTimeout" value="60"></div>
          <div class="kv"><b>重试次数</b><input id="pRetries" value="3"></div>
          <div class="kv"><b>游戏</b><input id="pGame" value="auto"></div>
        </div>
      </div>
      <div class="row" style="margin-top:10px">
        <label class="dim"><input type="checkbox" id="pFresh" checked style="width:auto"> 忽略进度全部重跑</label>
      </div>
      <div class="row" style="margin-top:12px">
        <button class="p" id="btnStart" onclick="scanStart()">▶ 开始扫描</button>
        <button class="d" id="btnStop" onclick="scanStop()" disabled>■ 停止</button>
        <button class="s" onclick="scanDownload()">下载结果</button>
      </div>
      <div class="msg" id="smsg"></div>
    </div>
    <div class="grid">
      <div class="card"><h2>实时日志</h2><pre id="logBox">—</pre></div>
      <div class="card"><h2>当前结果</h2><pre id="resBox">—</pre></div>
    </div>
  </div>

  <!-- 历史 -->
  <div class="tab" id="t-history" style="display:none">
    <div class="card"><h2>批次历史</h2><div id="histBox">—</div></div>
    <div class="card"><h2>结果预览</h2><pre id="histPrev">（在上面点「看」）</pre></div>
  </div>

  <!-- 代理池 -->
  <div class="tab" id="t-proxy" style="display:none">
    <div class="card">
      <h2>代理池内容（host:port:user:pass 每行一条）</h2>
      <textarea id="pxText" spellcheck="false"></textarea>
      <div class="row" style="margin-top:10px">
        <button class="p" onclick="saveProxies()">保存</button>
        <button class="s" onclick="checkProxies()">体检</button>
        <span class="dim" id="pxMsg"></span>
      </div>
    </div>
    <div class="card"><h2>体检结果</h2><div id="pxResult">—</div></div>
  </div>

  <!-- 设置 -->
  <div class="tab" id="t-settings" style="display:none">
    <div class="card">
      <h2>账号文件</h2>
      <textarea id="acText" spellcheck="false"></textarea>
      <div class="row" style="margin-top:10px">
        <button class="p" onclick="saveAccounts()">保存</button>
        <span class="dim" id="acMsg"></span>
      </div>
    </div>
    <div class="card">
      <h2>面板账号</h2>
      <div id="userBox">—</div>
      <div class="row" style="margin-top:10px">
        <input id="nu" placeholder="新用户名" style="max-width:180px">
        <input id="np" type="password" placeholder="新密码(≥6位)" style="max-width:180px">
        <button class="s" onclick="addUser()">添加</button>
      </div>
      <div class="row" style="margin-top:10px">
        <input id="op" type="password" placeholder="当前密码" style="max-width:180px">
        <input id="cp" type="password" placeholder="新密码(≥6位)" style="max-width:180px">
        <button class="s" onclick="chgPass()">改自己的密码</button>
      </div>
      <div class="msg" id="uMsg"></div>
    </div>
  </div>
</div>
</div>
<script>
let LOGOFF=0, TIMER=null;
function $(id){return document.getElementById(id)}
async function api(path,opt){
  opt=opt||{}; opt.headers=Object.assign({'Content-Type':'application/json'},opt.headers||{});
  const r=await fetch(path,opt);
  let j={}; try{j=await r.json()}catch(e){}
  if(r.status===401){showLogin();throw new Error('未登录')}
  return j;
}
function showLogin(){$('login').style.display='block';$('app').style.display='none';if(TIMER)clearInterval(TIMER)}
function showApp(){$('login').style.display='none';$('app').style.display='block';refresh();if(TIMER)clearInterval(TIMER);TIMER=setInterval(refresh,2500)}
async function doLogin(){
  const j=await api('/api/login',{method:'POST',body:JSON.stringify({user:$('lu').value,password:$('lp').value})});
  if(j.ok)showApp(); else $('lmsg').textContent=j.error||'登录失败';
}
async function doLogout(){await api('/api/logout',{method:'POST'});showLogin()}
function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}

document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');
  document.querySelectorAll('.tab').forEach(t=>t.style.display='none');
  $('t-'+b.dataset.t).style.display='block';
  if(b.dataset.t==='history')loadHistory();
  if(b.dataset.t==='proxy')loadProxies();
  if(b.dataset.t==='settings'){loadAccounts();loadUsers()}
});

async function refresh(){
  let s; try{s=await api('/api/status')}catch(e){return}
  $('who').textContent=s.server.user+'@'+s.server.host;
  $('bServer').textContent='服务器 '+s.server.time.slice(11);
  $('bServer').className='badge g';
  $('bOcr').textContent=s.ocr.listening?'OCR 正常':'OCR 未监听';
  $('bOcr').className='badge '+(s.ocr.listening?'g':'r');
  $('ovServer').innerHTML=
    kv('主机',s.server.host)+kv('时间',s.server.time)+
    kv('磁盘','C: 可用 '+s.server.disk.free_gb+' / '+s.server.disk.total_gb+' GB');
  $('ovOcr').innerHTML=kv('506 端口',s.ocr.listening?'<span class=ok>监听中</span>':'<span class=bad>未监听</span>')+
    kv('进程数',s.ocr.processes);
  $('ovTasks').innerHTML=s.tasks.length?s.tasks.map(t=>kv(t.name,t.state==='Ready'?'<span class=ok>就绪</span>':t.state)).join(''):'<span class=dim>没有</span>';
  $('ovFiles').innerHTML=kv('账号',s.files.accounts.lines+' 行')+kv('代理',s.files.proxies.lines+' 行');
  const r=s.job.running;
  $('btnStart').disabled=r; $('btnStop').disabled=!r;
  if(s.job.start_at)$('smsg').textContent=(r?'运行中':'已结束')+' · 开始 '+s.job.start_at+(s.job.end_at?' · 结束 '+s.job.end_at:'')+(s.job.exit_code!=null?' · 退出码 '+s.job.exit_code:'');
  if(s.job.running||LOGOFF){await pullLog();await pullRes()}
  if(s.proxycheck.running)$('pxMsg').textContent='体检中…';
  else if($('pxMsg').textContent==='体检中…'){$('pxMsg').textContent='';await pullProxy()}
}
function kv(k,v){return '<div class=kv><b>'+k+'</b><span>'+v+'</span></div>'}

async function pullLog(){
  const j=await api('/api/scan/log?since='+LOGOFF); if(!j.ok)return;
  if(j.text){LOGOFF=j.offset;const b=$('logBox');b.textContent+=j.text;b.scrollTop=b.scrollHeight;LOGOFF=j.offset}
  else LOGOFF=j.offset;
}
async function pullRes(){const j=await api('/api/scan/result'); if(j.ok&&j.text)$('resBox').textContent=j.text}

async function scanStart(){
  const body={parallel:+$('pParallel').value,workers:+$('pWorkers').value,rate:+$('pRate').value,
    timeout:+$('pTimeout').value,retries:+$('pRetries').value,game:$('pGame').value,fresh:$('pFresh').checked};
  $('logBox').textContent='';$('resBox').textContent='';LOGOFF=0;
  const j=await api('/api/scan/start',{method:'POST',body:JSON.stringify(body)});
  $('smsg').textContent=j.ok?('已启动批次 '+j.tag):('启动失败: '+j.error);
}
async function scanStop(){const j=await api('/api/scan/stop',{method:'POST'});$('smsg').textContent=j.ok?'已发送停止请求':'停止失败'}
function scanDownload(){location.href='/api/scan/download'}

async function loadHistory(){
  const j=await api('/api/history');
  if(!j.ok||!j.items.length){$('histBox').innerHTML='<span class=dim>还没有批次</span>';return}
  let h='<table><tr><th>批次</th><th>开始</th><th>结束</th><th>退出码</th><th>结果行</th><th></th></tr>';
  j.items.forEach(it=>{const fn=it.out?it.out.split(/[\\/]/).pop():'';
    h+='<tr><td>'+esc(it.tag)+'</td><td>'+esc(it.start||'')+'</td><td>'+esc(it.end||'')+'</td><td>'+it.exit_code+'</td><td>'+it.result_lines+'</td>'+
       '<td><a href="#" onclick="prevHist(\''+esc(fn)+'\');return false">看</a> · <a href="/api/history/download?name='+encodeURIComponent(fn)+'">下载</a></td></tr>'});
  $('histBox').innerHTML=h+'</table>';
}
async function prevHist(fn){const j=await api('/api/history/file?name='+encodeURIComponent(fn));$('histPrev').textContent=j.ok?j.text:j.error}

async function loadProxies(){const j=await api('/api/proxies');if(j.ok)$('pxText').value=j.text}
async function saveProxies(){const j=await api('/api/proxies',{method:'POST',body:JSON.stringify({text:$('pxText').value})});$('pxMsg').textContent=j.ok?('已保存 '+j.lines+' 行'):j.error}
async function checkProxies(){const j=await api('/api/proxycheck',{method:'POST',body:JSON.stringify({workers:20})});$('pxMsg').textContent=j.ok?'体检中…':j.error}
async function pullProxy(){
  const j=await api('/api/proxycheck');const s=j.state;
  if(s.running){$('pxMsg').textContent='体检中… '+s.done+'/'+s.total;return}
  if(!s.result){return}
  const r=s.result;
  if(r.error){$('pxResult').innerHTML='<span class=bad>'+esc(r.error)+'</span>';return}
  let h=kv('总数',r.total)+kv('可用',r.good)+kv('唯一出口 IP',r.unique_ips)+
        kv('国内出口',r.cn>0?'<span class=bad>'+r.cn+'</span>':'<span class=ok>0</span>');
  h+='<table style="margin-top:8px"><tr><th>名称</th><th>出口 IP</th><th>国家</th><th>城市</th><th>延迟</th></tr>';
  (r.rows||[]).slice(0,60).forEach(x=>{h+='<tr><td>'+esc(x.name)+'</td><td>'+esc(x.ip||'-')+'</td><td>'+esc(x.cc||'-')+'</td><td>'+esc(x.city||'-')+'</td><td>'+(x.ms||'-')+'ms</td></tr>'});
  $('pxResult').innerHTML=h+'</table>';
}

async function loadAccounts(){const j=await api('/api/accounts');if(j.ok)$('acText').value=j.text}
async function saveAccounts(){const j=await api('/api/accounts',{method:'POST',body:JSON.stringify({text:$('acText').value})});$('acMsg').textContent=j.ok?('已保存 '+j.lines+' 行'):j.error}
async function loadUsers(){const j=await api('/api/users');if(!j.ok)return;
  let h='<table><tr><th>用户</th><th>角色</th><th>创建</th><th></th></tr>';
  j.items.forEach(u=>{h+='<tr><td>'+esc(u.name)+'</td><td>'+esc(u.role)+'</td><td>'+esc(u.created||'')+'</td><td>'+
    (u.name==='admin'?'':('<a href="#" onclick="delUser(\''+esc(u.name)+'\');return false">删</a>'))+'</td></tr>'});
  $('userBox').innerHTML=h+'</table>';
}
async function addUser(){const j=await api('/api/user/add',{method:'POST',body:JSON.stringify({user:$('nu').value,password:$('np').value})});$('uMsg').textContent=j.ok?'已添加':j.error;if(j.ok){$('nu').value='';$('np').value='';loadUsers()}}
async function delUser(n){const j=await api('/api/user/del',{method:'POST',body:JSON.stringify({user:n})});$('uMsg').textContent=j.ok?'已删除':j.error;loadUsers()}
async function chgPass(){const j=await api('/api/passwd',{method:'POST',body:JSON.stringify({old:$('op').value,new:$('cp').value})});$('uMsg').textContent=j.ok?'已修改':j.error}
async function ocrStart(){const j=await api('/api/ocr/start',{method:'POST'});alert(j.msg||j.error)}
async function ocrStop(){const j=await api('/api/ocr/stop',{method:'POST'});alert(j.ok?'已停止':j.error)}

(async()=>{const j=await api('/api/ping');if(j.need_login)showLogin();else showApp()})();
</script></body></html>
"""


# ================================================================ 入口
def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="wmscan --web")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="监听地址（默认 127.0.0.1 —— 公网暴露交给隧道/反代）")
    ap.add_argument("--port", type=int, default=8080)
    args, _ = ap.parse_known_args(argv or sys.argv[1:])

    _ensure_dirs()
    _init_users()

    if args.bind not in ("127.0.0.1", "localhost", "::1"):
        say("⚠️  注意：--bind %s 会让面板直接对外监听。"
            "推荐保持 127.0.0.1，用 Cloudflare Tunnel / 反向代理暴露。" % args.bind)

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    srv.daemon_threads = True
    say("=" * 62)
    say("  扫号控制台已启动")
    say("    本机访问 : http://%s:%d/" % (args.bind, args.port))
    say("    账号文件 : %s" % ACCOUNTS_F)
    say("    代理文件 : %s" % PROXIES_F)
    say("    结果目录 : %s" % OUT_DIR)
    say("=" * 62)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        say("\n已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
