# -*- coding: utf-8 -*-
"""
solve_pick_text.py —— 点选验证码端到端求解（本地 OCR 服务 + 已逆向协议）

单次 attempt 的完整链路：
  mCaptchaInit -> firstTest -> getCaptcha -> 下载图
    -> POST 127.0.0.1:506/ocr 拿坐标（失败则换一张重来）
    -> validData = AES({validate,time}, encKey(capKey))
    -> secondTest/pick_text_captcha -> secCode

所有对外请求经 netguard 强制走代理，绝不裸连。
用法：  python solve_pick_text.py [次数]
"""
import base64, json, os, random, re, socket, sys, time

import netguard  # noqa: F401
import requests

from wmcaptcha import (PROXIES, UA, PASSPORT, CAPTCHAS, enc_key, aes_encrypt_b64,
                       build_fp, gen_human_events, FINGERPRINT_PROFILES, strip_jsonp)

HERE = os.path.dirname(os.path.abspath(__file__))
OCR_HOST, OCR_PORT = "127.0.0.1", 506
LOG = []

# HTTP 超时（秒）—— 从环境变量读，默认 30。
# ★ 走住宅代理链时 30 秒不够：验证码图片 CDN 实测要 5~14 秒才下完，
#   并发一高就 ReadTimeout，账号被判 INCOMPLETE 重跑。
HTTP_TIMEOUT = float(os.environ.get("WM_HTTP_TIMEOUT", "30"))


def log(s=""):
    print(s, flush=True)
    LOG.append(str(s))


def local_ocr(img_bytes, timeout=60):
    body = base64.b64encode(img_bytes)
    s = socket.create_connection((OCR_HOST, OCR_PORT), timeout=10)
    s.sendall(("POST /ocr HTTP/1.1\r\nHost: %s\r\n"
               "Content-Type: application/x-www-form-urlencoded\r\n"
               "Content-Length: %d\r\nConnection: close\r\n\r\n"
               % (OCR_HOST, len(body))).encode() + body)
    s.settimeout(timeout)
    buf = b""
    while True:
        try:
            d = s.recv(8192)
        except socket.timeout:
            break
        if not d:
            break
        buf += d
        if len(buf) > 200000:
            break
    s.close()
    t = buf.decode("utf-8", "replace")
    return t.split("\r\n\r\n", 1)[1].strip() if "\r\n\r\n" in t else t.strip()


def coords_to_validate(resp):
    pts = re.findall(r"(-?\d+)\s*,\s*(-?\d+)", resp)
    return ",".join("%s,%s" % (x, y) for x, y in pts), pts


def cb():
    return "jQuery%d%d_%d" % (random.randint(100000, 999999),
                              random.randint(100000, 999999), int(time.time() * 1000))


def get(sess, url, **kw):
    """带 3 次重试的 GET（代理会抖）

    超时从 WM_HTTP_TIMEOUT 读（默认 30）。走住宅代理链时要调大 ——
    验证码图片 CDN（腾讯 COS）实测要 5~14 秒才下完一张 26KB 的图。
    """
    last = None
    for a in range(3):
        try:
            return sess.get(url, proxies=PROXIES, timeout=HTTP_TIMEOUT, **kw)
        except Exception as e:
            last = e
            log(f"    (重试 {a+1}/3: {type(e).__name__})")
            time.sleep(1.5 + a)
    raise last


def challenge(sess, app_id, cap_ticket, fp):
    """走完 firstTest + getCaptcha，返回 (kind, payload)"""
    kt = enc_key(cap_ticket)
    ev = gen_human_events(seed=random.randint(1, 10 ** 9))
    op = aes_encrypt_b64(json.dumps(ev, separators=(",", ":")), kt)

    r = get(sess, f"{CAPTCHAS}/aicaptcha/firstTest",
            params={"callback": cb(), "appId": app_id, "capTicket": cap_ticket,
                    "mobile": 0, "op": op, "fp": fp, "isInIframe": "false",
                    "_": int(time.time() * 1000)},
            headers={"User-Agent": UA, "Referer": f"{PASSPORT}/", "Accept": "*/*"})
    ft = json.loads(strip_jsonp(r.text))
    log(f"    firstTest -> next={ft.get('next')} type={ft.get('type')}")
    if not ft.get("next"):
        return "PASS", ft.get("secCode")

    r = get(sess, f"{CAPTCHAS}/aicaptcha/getCaptcha",
            params={"callback": cb(), "appId": app_id, "capTicket": cap_ticket,
                    "_": int(time.time() * 1000)},
            headers={"User-Agent": UA, "Referer": f"{PASSPORT}/", "Accept": "*/*"})
    gc = json.loads(strip_jsonp(r.text))
    if gc.get("code") != 0:
        return "ERR", gc
    img = get(sess, gc["imgUrl"], headers={"User-Agent": UA}).content
    return "CHALLENGE", {"gc": gc, "img": img, "ev": ev}


def submit(sess, app_id, cap_ticket, fp, gc, validate, ev, spend_ms):
    kk = enc_key(gc["capKey"])
    valid_data = aes_encrypt_b64(
        json.dumps({"validate": validate, "time": spend_ms}, separators=(",", ":")), kk)
    op2 = aes_encrypt_b64(json.dumps(ev, separators=(",", ":")), kk)
    r = get(sess, gc["validateUrl"],
            params={"callback": cb(), "appId": app_id, "capTicket": cap_ticket,
                    "capKey": gc["capKey"], "validData": valid_data, "op": op2,
                    "fp": fp, "isInIframe": "false", "_": int(time.time() * 1000)},
            headers={"User-Agent": UA, "Referer": f"{PASSPORT}/", "Accept": "*/*"})
    return json.loads(strip_jsonp(r.text))


def attempt(sess, fp, max_challenges=4):
    """一轮登录级尝试：最多换 max_challenges 张图（本地识别失败就换）"""
    detail = {"challenges": []}
    for c in range(max_challenges):
        init = get(sess, f"{PASSPORT}/sso/servlet/ajax",
                   params={"op": "mCaptchaInit", "isAICap": "1"},
                   headers={"User-Agent": UA, "Referer": f"{PASSPORT}/"}).json()
        if init.get("code") != 0:
            return False, "init_failed", detail
        app_id, ticket = init["data"]["appId"], init["data"]["capTicket"]

        kind, payload = challenge(sess, app_id, ticket, fp)
        if kind == "PASS":
            log("    ★ 无感通过（直接拿到 secCode）")
            return True, payload, detail
        if kind == "ERR":
            log(f"    getCaptcha 失败 {payload}"); continue

        gc, img = payload["gc"], payload["img"]
        t0 = time.time()
        resp = local_ocr(img)
        ocr_ms = int((time.time() - t0) * 1000)
        validate, pts = coords_to_validate(resp)
        log(f"    图 {len(img)}B  本地OCR({ocr_ms}ms) -> {resp[:70]!r}  {len(pts)} 点")
        detail["challenges"].append({"ocr": resp, "npts": len(pts)})
        if not pts:
            log("    本地识别失败，换一张")
            time.sleep(random.uniform(1.2, 2.6))
            continue

        spend = max(1800, ocr_ms + random.randint(900, 2800))
        st = submit(sess, app_id, ticket, fp, gc, validate, payload["ev"], spend)
        log(f"    secondTest -> {json.dumps(st, ensure_ascii=False)[:260]}")
        detail["validate"] = validate
        detail["spend_ms"] = spend
        detail["secondTest"] = st
        if st.get("code") == 0 and st.get("result"):
            return True, st["result"], detail
        return False, st.get("msg") or st, detail
    return False, "no_challenge_solved", detail


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    sess = requests.Session()
    sess.trust_env = False
    fp = build_fp(FINGERPRINT_PROFILES[0])

    res = []
    okn = 0
    for i in range(n):
        log(f"\n===== 第 {i+1}/{n} 次 =====")
        try:
            ok, r, d = attempt(sess, fp)
        except Exception as e:
            ok, r, d = False, f"exc:{type(e).__name__}:{str(e)[:120]}", {}
        log(f"  >>> {'✅ 通过' if ok else '❌ 未通过'}   result={str(r)[:140]}")
        res.append({"ok": ok, "result": str(r)[:200], "detail": d})
        okn += ok
        if i < n - 1:
            time.sleep(random.uniform(3.0, 7.0))

    log(f"\n===== 汇总：{okn}/{n} 通过 =====")
    json.dump({"ok": okn, "total": n, "results": res},
              open(os.path.join(HERE, "solve_result.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    open(os.path.join(HERE, "solve_run.log"), "w", encoding="utf-8").write("\n".join(LOG))
    netguard.report()
    return 0 if okn else 2


if __name__ == "__main__":
    sys.exit(main())
