# -*- coding: utf-8 -*-
"""
proxy_pool.py —— 多 IP 代理池（SOCKS5 粘性会话）
================================================================
输入文件格式（每行一条）：
    host:port:username:password
例：
    us.proxy.example:3010:user-region-JP-sid-XXXX-t-5:password

★ 实测结论（2026-09-23）：
  · 这批代理是 **SOCKS5**，不是 HTTP。用 http:// 连会收到 407/断连，用 socks5h:// 才通。
  · 每个 `sid-XXXX` = 一个独立出口 IP（日本住宅：Sony / NTT / KDDI / SoftBank）
  · 50 条里 44 条可用，42 个唯一 IP，延迟 2.5~9.5s

设计：
  · 一个账号 = 一个 IP（会话粘性），账号之间绝不共用 IP
  · 线程安全：用 thread-local 保存"当前账号在用的代理"
  · 自动向 netguard 登记，避免被强制改回默认代理
"""
import os
import re
import socket
import threading
import time

import netguard  # noqa: F401
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.join(HERE, "proxies.txt")
DEFAULT_PROXY = os.environ.get("WM_PROXY", "http://127.0.0.1:7890")

def _safe_print(*a, **k):
    try:
        print(*a, **k)
    except Exception:
        pass


_tls = threading.local()
_pool = None


# ---------------------------------------------------------------- 解析
def parse_line(line):
    """host:port:user:pass  ->  dict"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(":")
    if len(parts) < 4:
        return None
    host, port = parts[0], parts[1]
    user = ":".join(parts[2:-1])
    pwd = parts[-1]
    m = re.search(r"sid-([A-Za-z0-9]+)", user)
    name = m.group(1) if m else user[-8:]
    url = f"socks5h://{user}:{pwd}@{host}:{port}"      # ★ SOCKS5
    return {"name": name, "url": url, "host": host, "port": port,
            "user": user, "pwd": pwd, "ok": None, "ms": None, "ip": None,
            "in_use": False}


def load(path=None):
    path = path or DEFAULT_FILE
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for ln in f:
            p = parse_line(ln)
            if p:
                out.append(p)
    return out


# ---------------------------------------------------------------- 线程本地
def set_thread_proxy(url):
    _tls.proxy = url


def clear_thread_proxy():
    _tls.proxy = None


def current_proxy():
    return getattr(_tls, "proxy", None) or DEFAULT_PROXY


def proxies_for(url=None):
    """给 requests 用的 proxies dict（当前线程绑定的代理）"""
    p = current_proxy()
    return {"http": p, "https": p}


# ---------------------------------------------------------------- 池
class ProxyPool:
    def __init__(self, entries, verbose=True):
        self.entries = entries
        self.verbose = verbose
        self._lock = threading.Lock()
        for e in entries:
            try:
                netguard.allow_proxy(e["url"])     # ★ 向 netguard 登记，别被改回去
            except Exception:
                pass

    # ---------- 体检 ----------
    def check_one(self, e, echo=None, timeout=25):
        echo = echo or ("http://ip-api.com/json/?fields=status,query,countryCode,"
                        "regionName,city,isp")
        t0 = time.time()
        try:
            r = requests.get(echo, proxies={"http": e["url"], "https": e["url"]},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
            d = r.json()
            e.update(ok=True, ms=round((time.time() - t0) * 1000),
                     ip=d.get("query"), cc=d.get("countryCode"),
                     region=d.get("regionName"), city=d.get("city"), isp=d.get("isp"))
        except Exception as ex:
            e.update(ok=False, ms=round((time.time() - t0) * 1000),
                     err=f"{type(ex).__name__}: {str(ex)[:70]}")
        return e

    def check_all(self, workers=10, echo=None, timeout=25):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(lambda e: self.check_one(e, echo, timeout), self.entries))
        good = [e for e in self.entries if e.get("ok")]
        ips = {e["ip"] for e in good}
        if self.verbose:
            _safe_print(f"[pool] 体检完成：可用 {len(good)}/{len(self.entries)}，"
                        f"唯一出口 IP {len(ips)}")
        return good

    # ---------- 取用 ----------
    def acquire(self, prefer_ip_unique=True):
        """取一条空闲代理并标记占用；没有空闲返回 None"""
        with self._lock:
            cands = [e for e in self.entries if e.get("ok") and not e["in_use"]]
            if not cands:
                return None
            if prefer_ip_unique:
                used_ips = {e["ip"] for e in self.entries if e["in_use"] and e.get("ip")}
                uniq = [e for e in cands if e.get("ip") not in used_ips]
                if uniq:
                    cands = uniq
            e = sorted(cands, key=lambda x: x.get("ms") or 99999)[0]
            e["in_use"] = True
            return e

    def release(self, e):
        with self._lock:
            if e:
                e["in_use"] = False

    @property
    def good(self):
        return [e for e in self.entries if e.get("ok")]

    def stats(self):
        g = self.good
        return {"total": len(self.entries), "good": len(g),
                "unique_ips": len({e["ip"] for e in g}),
                "busy": sum(1 for e in self.entries if e["in_use"]),
                "median_ms": (sorted(e["ms"] for e in g)[len(g) // 2] if g else None)}


# ---------------------------------------------------------------- 便捷入口
def build(path=None, workers=10, verbose=True, quick_sample=0):
    """加载 + 体检，返回 ProxyPool"""
    global _pool
    entries = load(path)
    if not entries:
        raise RuntimeError(f"没有解析到任何代理，检查文件: {path or DEFAULT_FILE}")
    if verbose:
        _safe_print(f"[pool] 载入 {len(entries)} 条代理")
    _pool = ProxyPool(entries, verbose=verbose)
    if quick_sample:
        sample = entries[:quick_sample]
        for e in sample:
            _pool.check_one(e)
        if verbose:
            ok = sum(1 for e in sample if e.get("ok"))
            _safe_print(f"[pool] 抽样体检 {ok}/{len(sample)} 可用")
    else:
        _pool.check_all(workers=workers)
    return _pool


def get_pool():
    return _pool


if __name__ == "__main__":
    import sys
    pool = build(verbose=True)
    s = pool.stats()
    print(f"\n池状态: {s}")
    print("\n可用代理（前 15 条）:")
    for e in pool.good[:15]:
        print(f"  {e['name']:<12} {e.get('ip'):<16} {e.get('cc')}/{e.get('region')}/"
              f"{e.get('city')}  {e.get('ms')}ms  {e.get('isp')}")


# ================================================================ 代理 API 提取
def _detect_protocol(host, port, timeout=10):
    """探测代理协议：先试 SOCKS5，再试 HTTP。返回 'socks5' / 'http' / None"""
    import socket
    # --- SOCKS5 ---
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(b"\x05\x01\x00")
        r = s.recv(16)
        s.close()
        if len(r) >= 2 and r[0] == 5 and r[1] in (0, 2):
            return "socks5"
    except Exception:
        pass
    # --- HTTP ---
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(b"GET http://ip-api.com/json/ HTTP/1.1\r\nHost: ip-api.com\r\n"
                  b"Connection: close\r\n\r\n")
        buf = b""
        while True:
            try:
                d = s.recv(2048)
            except Exception:
                break
            if not d:
                break
            buf += d
            if len(buf) > 4000:
                break
        s.close()
        if b"HTTP/" in buf[:40]:
            return "http"
    except Exception:
        pass
    return None


def fetch_from_api(api_url, timeout=40, verbose=True, direct=False):
    """从 API 拉代理列表，返回 [(host, port), ...]（去重）

    direct=False（默认）：经当前代理/隧道请求 —— 本机 IP 不暴露，
                          但多数国内代理商 token 绑 IP，会返回 403。
    direct=True         ：直连 —— 本机 IP 会露给代理商（供应商，正常），
                          但能通过 IP 白名单校验。
    """
    import requests
    def say(s):
        if verbose:
            _safe_print(s)
    say(f"[api] 拉取 {api_url[:90]} ...  ({'直连' if direct else '经隧道'})")
    s = requests.Session()
    s.trust_env = False
    kw = {} if direct else {"proxies": proxies_for()}
    r = s.get(api_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout, **kw)
    if r.status_code == 403:
        body = r.text[:200]
        low = body.lower()
        if "expired" in low:
            raise RuntimeError(
                f"API 返回 403 —— token 已失效或过期。\n"
                f"          响应: {body}\n"
                f"          去代理商后台拿一个新的提取网址，重新粘进来即可。\n"
                f"          （国内代理商的 token 通常还绑来源 IP，换机器要重新登记）")
        if "required" in low:
            raise RuntimeError(
                f"API 返回 403 —— 网址里缺 token。\n"
                f"          响应: {body}\n"
                f"          请把完整的提取网址（含 token=...）粘进来。")
        raise RuntimeError(
            f"API 返回 403 —— token 与来源 IP 绑定。\n"
            f"          响应: {body}\n"
            f"          当前{'已用直连' if direct else '经隧道（外国 IP）'}，"
            f"{'请确认本机公网 IP 已在代理商后台登记' if direct else '可试试勾上「API 直连」'}")
    if r.status_code != 200:
        raise RuntimeError(f"API 返回 HTTP {r.status_code}  {r.text[:120]}")
    text = r.text
    out, seen = [], set()
    for line in text.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 支持 ip:port 与 user:pass@ip:port 两种
        if "@" in line:
            line = line.split("@", 1)[1]
        if "://" in line:
            line = line.split("://", 1)[1]
        if ":" not in line:
            continue
        host, _, port = line.rpartition(":")
        host, port = host.strip(), port.strip()
        if not host or not port.isdigit():
            continue
        k = f"{host}:{port}"
        if k in seen:
            continue
        seen.add(k)
        out.append((host, port))
    say(f"[api] 解析出 {len(out)} 条 ip:port")
    return out


def build_from_api(api_url, want_alive=True, workers=20, timeout=12,
                   reject_cn=True, verbose=True, progress_cb=None, direct=False):
    """完整流程：拉取 → 探协议 → 测存活 → 过滤 → 返回可直接用的代理条目"""
    def say(s):
        if verbose:
            _safe_print(s)
    raw = fetch_from_api(api_url, verbose=verbose, direct=direct)
    if not raw:
        raise RuntimeError("API 没返回任何代理")

    total = len(raw)
    good, dead, cn = [], [], []
    done = [0]
    import threading as _th
    lock = _th.Lock()

    def probe(item):
        host, port = item
        proto = _detect_protocol(host, port, timeout)
        rec = None
        if proto:
            url = (f"socks5h://{host}:{port}" if proto == "socks5"
                   else f"http://{host}:{port}")
            try:
                import requests as _rq
                rr = _rq.get("http://ip-api.com/json/?fields=status,query,"
                             "countryCode,city,isp", proxies={"http": url, "https": url},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout + 5)
                d = rr.json()
                if d.get("status") == "success":
                    rec = {"name": f"{host[-8:]}:{port}", "url": url, "host": host,
                           "port": str(port), "user": "", "pwd": "", "proto": proto,
                           "ok": True, "ip": d.get("query"),
                           "cc": d.get("countryCode"), "city": d.get("city"),
                           "isp": d.get("isp"), "in_use": False}
            except Exception:
                rec = None
        with lock:
            done[0] += 1
            if rec:
                (cn if (rec.get("cc") or "").upper() == "CN" else good).append(rec)
            else:
                dead.append(item)
            if progress_cb:
                try:
                    progress_cb(done[0], total)
                except Exception:
                    pass

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(probe, raw))

    say(f"[api] 结果：可用 {len(good)}  国内 {len(cn)}  失效 {len(dead)}  (共 {total})")
    if reject_cn and cn:
        say(f"[api] ★ 已按红线拒收 {len(cn)} 条国内出口")
    return {"good": good, "cn": cn, "dead": dead, "total": total}
