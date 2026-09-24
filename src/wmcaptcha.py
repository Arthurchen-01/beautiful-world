# -*- coding: utf-8 -*-
"""
完美世界 WanmeiCaptcha 复现 harness
=====================================================
从 wanmeiCaptcha.min.js 逆出的全部算法，纯 Python 复现：

  1. encKey(capTicket)        -> 16 位 AES-128 密钥
  2. Encrypt(plaintext, key)  -> AES-128-CBC(key=iv=key, PKCS7) -> Base64
  3. OpRcd 事件模型            -> [x, y, typeCode, dtMs]
     typeCode: mousedown=1 mouseup=2 mousemove=3 scroll=4 mouseenter=5 mouseleave=6
     节流: 同类事件间隔 >= 100ms；总窗口 20000ms
  4. FingerprintJS v1 + murmurhash3_32_gc(key, 31) -> fp
  5. GET https://captchas.wanmei.com/aicaptcha/firstTest?...

全部请求经代理，绝不裸连。
"""
import base64
import json
import math
import os
import random
import sys
import time
import urllib.parse

import netguard  # noqa: F401  —— 出口强制守卫，必须早于 requests 生效
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# ---------------------------------------------------------------- 网络
PROXY = "http://127.0.0.1:7890"
PROXIES = {"http": PROXY, "https": PROXY}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
PASSPORT = "https://passport.wanmei.com"
CAPTCHAS = "https://captchas.wanmei.com"
HERE = os.path.dirname(os.path.abspath(__file__))

M32 = 0xFFFFFFFF


# ================================================================ 1. 密钥派生
def enc_key(cap_ticket: str) -> str:
    """JS: t.substring(1,3)+t.substring(10,13)+t.substring(20,22)+t.substring(26,31)+t.substring(21,25)"""
    return (cap_ticket[1:3] + cap_ticket[10:13] + cap_ticket[20:22]
            + cap_ticket[26:31] + cap_ticket[21:25])


# ================================================================ 2. AES 加密
def aes_encrypt_b64(plaintext: str, key: str) -> str:
    """AES-128-CBC, key == iv, PKCS7, 输出 Base64"""
    k = key.encode("utf-8")
    ct = AES.new(k, AES.MODE_CBC, iv=k).encrypt(pad(plaintext.encode("utf-8"), 16))
    return base64.b64encode(ct).decode()


# ================================================================ 3. 指纹
def _u16_units(s: str):
    """模拟 JS charCodeAt：UTF-16 码元序列"""
    b = s.encode("utf-16-le")
    return [b[i] | (b[i + 1] << 8) for i in range(0, len(b), 2)]


def _mul32(a: int, b: int) -> int:
    """JS 的 32 位乘法 (a*b) & 0xFFFFFFFF，按 JS 的拆分写法等价实现"""
    return ((a & 0xFFFF) * b + ((((a >> 16) * b) & 0xFFFF) << 16)) & M32


def murmurhash3_32_gc(key: str, seed: int = 31) -> int:
    """逐行移植自 fp.min.js 的 murmurhash3_32_gc"""
    u = _u16_units(key)
    n = len(u)
    remainder = n & 3
    nbytes = n - remainder
    h1 = seed
    c1, c2 = 3432918353, 461845907
    i = 0
    while i < nbytes:
        k1 = ((u[i] & 255) | ((u[i + 1] & 255) << 8)
              | ((u[i + 2] & 255) << 16) | ((u[i + 3] & 255) << 24))
        i += 4
        k1 = _mul32(k1, c1)
        k1 = ((k1 << 15) | (k1 >> 17)) & M32
        k1 = _mul32(k1, c2)
        h1 = (h1 ^ k1) & M32
        h1 = ((h1 << 13) | (h1 >> 19)) & M32
        h1b = _mul32(h1, 5)
        h1 = ((h1b & 0xFFFF) + 27492
              + ((((h1b >> 16) + 58964) & 0xFFFF) << 16)) & M32
    k1 = 0
    if remainder == 3:
        k1 ^= (u[i + 2] & 255) << 16
    if remainder >= 2:
        k1 ^= (u[i + 1] & 255) << 8
    if remainder >= 1:
        k1 ^= (u[i] & 255)
        k1 = _mul32(k1, c1)
        k1 = ((k1 << 15) | (k1 >> 17)) & M32
        k1 = _mul32(k1, c2)
        h1 = (h1 ^ k1) & M32
    h1 = (h1 ^ n) & M32
    h1 = (h1 ^ (h1 >> 16)) & M32
    h1 = _mul32(h1, 2246822507)
    h1 = (h1 ^ (h1 >> 13)) & M32
    h1 = _mul32(h1, 3266489909)
    h1 = (h1 ^ (h1 >> 16)) & M32
    return h1


# 真实 Chrome/Win 的典型指纹画像（可随机化成多套，用于反聚类）
FINGERPRINT_PROFILES = [
    {
        "userAgent": UA,
        "language": "zh-CN",
        "colorDepth": 24,
        "screen": "1080x1920",
        "tzOffset": -480,
        "sessionStorage": "true",
        "localStorage": "true",
        "indexDb": "true",
        "addBehavior": "undefined",
        "openDatabase": "function",
        "cpuClass": "",          # JS 里是 undefined -> join 后变空串
        "platform": "Win32",
        "doNotTrack": "",        # Chrome 默认 null -> 空串
        "plugins": ("PDF Viewer::Portable Document Format::application/pdf~pdf,"
                    "text/pdf~pdf;Chrome PDF Viewer::Portable Document Format::"
                    "application/pdf~pdf,text/pdf~pdf;Chromium PDF Viewer::"
                    "Portable Document Format::application/pdf~pdf,text/pdf~pdf;"
                    "Microsoft Edge PDF Viewer::Portable Document Format::"
                    "application/pdf~pdf,text/pdf~pdf;WebKit built-in PDF::"
                    "Portable Document Format::application/pdf~pdf,text/pdf~pdf"),
        # canvas 在真实浏览器里是 toDataURL 的结果，这里用典型值占位
        "canvas": ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAHgAAAAyCAYAAACGE6YQ"
                   "AAAAAXNSR0IArs4c6QAABFZJREFUeF7tmWlIVFEUx//3vTfjNpqallmW7Rtp0U"
                   "ZF0EJEUARB9CGiD0EfIgiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiC"
                   "IAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiC"),
    },
]


def build_fp(profile: dict) -> int:
    """按 fp.min.js 的 get() 顺序拼 key，再 murmurhash3_32_gc(key, 31)"""
    keys = [
        profile["userAgent"], profile["language"], profile["colorDepth"],
        profile["screen"], profile["tzOffset"],
        profile["sessionStorage"], profile["localStorage"], profile["indexDb"],
        profile["addBehavior"], profile["openDatabase"],
        profile["cpuClass"], profile["platform"], profile["doNotTrack"],
        profile["plugins"], profile["canvas"],
    ]
    # JS 的 join 会把数字隐式转成字符串，这里保持一致
    return murmurhash3_32_gc("###".join(str(k) for k in keys), 31)


# ================================================================ 4. 行为事件
def gen_human_events(width=1920, height=1080, target=(960, 620),
                     seed=None, style="curve"):
    """
    生成拟人鼠标轨迹，格式 [[x, y, typeCode, dtMs], ...]
    对齐 OpRcd：mousemove 节流 >=100ms；总时长 < 20000ms
    """
    rnd = random.Random(seed)
    events = []
    t = 0

    # 起点：视口随机位置
    x0 = rnd.uniform(width * 0.15, width * 0.75)
    y0 = rnd.uniform(height * 0.20, height * 0.70)
    tx, ty = target

    # 贝塞尔控制点 -> 拟人弧线（人手不会走直线）
    cx1 = x0 + (tx - x0) * rnd.uniform(0.2, 0.5) + rnd.uniform(-120, 120)
    cy1 = y0 + (ty - y0) * rnd.uniform(0.1, 0.4) + rnd.uniform(-90, 90)
    cx2 = x0 + (tx - x0) * rnd.uniform(0.6, 0.9) + rnd.uniform(-120, 120)
    cy2 = y0 + (ty - y0) * rnd.uniform(0.6, 0.9) + rnd.uniform(-90, 90)

    steps = rnd.randint(14, 28)
    for s in range(1, steps + 1):
        u = s / steps
        inv = 1 - u
        # 三次贝塞尔
        bx = (inv ** 3 * x0 + 3 * inv ** 2 * u * cx1
              + 3 * inv * u ** 2 * cx2 + u ** 3 * tx)
        by = (inv ** 3 * y0 + 3 * inv ** 2 * u * cy1
              + 3 * inv * u ** 2 * cy2 + u ** 3 * ty)
        # 手抖
        bx += rnd.gauss(0, 2.2)
        by += rnd.gauss(0, 2.2)
        # 速度非匀速：中段快、两端慢（人手特征）
        base = 118 if 0.25 < u < 0.8 else 165
        t += int(base * rnd.uniform(0.75, 1.35))
        events.append([round(bx, 1), round(by, 1), 3, t])

    # 落点微调（人对准目标时会减速）
    for _ in range(rnd.randint(2, 4)):
        t += rnd.randint(90, 160)
        tx += rnd.gauss(0, 1.5)
        ty += rnd.gauss(0, 1.5)
        events.append([round(tx, 1), round(ty, 1), 3, t])

    # 按下 + 抬起
    t += rnd.randint(90, 220)
    events.append([round(tx, 1), round(ty, 1), 1, t])
    t += rnd.randint(70, 150)
    events.append([round(tx, 1), round(ty, 1), 2, t])
    return events


# ================================================================ 5. 协议
def mcaptcha_init(sess: requests.Session):
    r = sess.get(f"{PASSPORT}/sso/servlet/ajax",
                 params={"op": "mCaptchaInit", "isAICap": "1"},
                 proxies=PROXIES, headers={"User-Agent": UA, "Referer": f"{PASSPORT}/"},
                 timeout=25)
    r.raise_for_status()
    return r.json()


def check_need_rand(sess: requests.Session, username: str):
    r = sess.get(f"{PASSPORT}/sso/servlet/ajax",
                 params={"op": "checkNeedRand", "username": username},
                 proxies=PROXIES, headers={"User-Agent": UA, "Referer": f"{PASSPORT}/"},
                 timeout=25)
    return r.json()


def first_test(sess: requests.Session, app_id, cap_ticket, op_b64, fp, label="",
               is_in_iframe=0, jsonp=None):
    params = {
        "appId": app_id,
        "capTicket": cap_ticket,
        "mobile": 0,
        "op": op_b64,
        "fp": fp,
        "label": label,
        "isInIframe": is_in_iframe,
    }
    if jsonp:
        params["callback"] = jsonp
    url = f"{CAPTCHAS}/aicaptcha/firstTest?" + urllib.parse.urlencode(params)
    r = sess.get(url, proxies=PROXIES,
                 headers={"User-Agent": UA, "Referer": f"{PASSPORT}/",
                          "Accept": "*/*"},
                 timeout=30)
    return r, url


def strip_jsonp(text: str):
    """剥掉 jQuery JSONP 包裹：cb(...) 或 /**/cb(...)"""
    t = text.strip()
    if t.startswith("/**/"):
        t = t[4:]
    i = t.find("(")
    j = t.rfind(")")
    if i != -1 and j != -1 and j > i:
        return t[i + 1:j]
    return t


# ================================================================ main
def main():
    out = {"steps": []}
    sess = requests.Session()
    sess.trust_env = False          # 不读环境变量代理，只用显式 PROXIES

    # ---- 1. 取 appId / capTicket ----
    init = mcaptcha_init(sess)
    print("[1] mCaptchaInit:", json.dumps(init, ensure_ascii=False))
    out["mcaptcha_init"] = init
    if init.get("code") != 0:
        print("!! mCaptchaInit 失败"); return 1
    app_id = init["data"]["appId"]
    cap_ticket = init["data"]["capTicket"]

    # ---- 2. 推导密钥 ----
    key = enc_key(cap_ticket)
    print(f"[2] capTicket={cap_ticket}  ->  AES key='{key}'  (len={len(key)})")
    out["cap_ticket"] = cap_ticket
    out["aes_key"] = key

    # ---- 3. 造行为 + 加密 ----
    events = gen_human_events(seed=random.randint(1, 10 ** 9))
    payload = json.dumps(events, separators=(",", ":"))
    op_b64 = aes_encrypt_b64(payload, key)
    print(f"[3] 事件数={len(events)}  明文={len(payload)}B  密文={len(op_b64)}B")
    print(f"    前3事件: {events[:3]}")
    print(f"    末3事件: {events[-3:]}")
    out["events"] = events
    out["op_b64_len"] = len(op_b64)

    # ---- 4. 指纹 ----
    prof = FINGERPRINT_PROFILES[0]
    fp = build_fp(prof)
    print(f"[4] fp = {fp}")
    out["fp"] = fp

    # ---- 5. 打 firstTest ----
    r, url = first_test(sess, app_id, cap_ticket, op_b64, fp, jsonp="jQuery3510")
    print(f"[5] firstTest -> HTTP {r.status_code}  len={len(r.content)}")
    raw = r.text
    out["first_test_raw"] = raw[:4000]
    out["first_test_status"] = r.status_code
    try:
        body = strip_jsonp(raw)
        data = json.loads(body)
        print("[5] 响应:", json.dumps(data, ensure_ascii=False)[:1200])
        out["first_test_json"] = data
    except Exception as e:
        print(f"[5] JSONP 解析失败 ({e})，原文前 600 字:")
        print("    " + raw[:600])

    with open(os.path.join(HERE, "cap_trace.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n已写入 cap_trace.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
