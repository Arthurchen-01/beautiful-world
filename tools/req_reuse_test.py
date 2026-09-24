# -*- coding: utf-8 -*-
"""
req_reuse_test.py —— requests.Session 走 SOCKS5 到底复不复用连接？

三种写法对比（同一个 sid，同一目标，各发 8 个请求）：
  A) 每次请求都传 proxies=...        ← 现在扫描工具就是这么写的
  B) proxies 设在 Session 上
  C) 显式配大连接池 + Session 级 proxies

如果 A 明显慢于 B/C，说明**按请求传 proxies 会破坏连接复用**，这就是要改的地方。
"""
import io
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import requests
from requests.adapters import HTTPAdapter

HOST = "passport.wanmei.com"
URL = "https://passport.wanmei.com/sso/servlet/ajax?op=mCaptchaInit&isAICap=1"
H = {"User-Agent": "Mozilla/5.0"}


def load(path):
    out = []
    with io.open(path, encoding="utf-8-sig", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            p = ln.split(":")
            if len(p) >= 4:
                out.append("socks5h://%s:%s@%s:%s" % (":".join(p[2:-1]), p[-1], p[0], p[1]))
    return out


def run(label, proxies, mode, n=8):
    sess = requests.Session()
    sess.trust_env = False
    if mode in ("session", "pooled"):
        sess.proxies = proxies
    if mode == "pooled":
        ad = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
        sess.mount("https://", ad)
        sess.mount("http://", ad)

    ts = []
    okn = 0
    for i in range(n):
        t0 = time.time()
        try:
            if mode == "percall":
                r = sess.get(URL, headers=H, proxies=proxies, timeout=30)
            else:
                r = sess.get(URL, headers=H, timeout=30)
            _ = r.text          # ★ 必须读完 body，连接才会被放回池子
            okn += 1
        except Exception as e:
            ts.append((time.time() - t0) * 1000)
            print("     #%d 失败 %s: %s" % (i + 1, type(e).__name__, str(e)[:60]))
            continue
        ts.append((time.time() - t0) * 1000)
    sess.close()

    if not ts:
        print("  %-34s 全失败" % label)
        return
    total = sum(ts) / 1000.0
    print("  %-34s 成功 %d/%d  合计 %6.1fs  首请求 %6.0fms  后续中位 %6.0fms  平均 %6.0fms"
          % (label, okn, n, total, ts[0], sorted(ts[1:])[len(ts[1:]) // 2] if len(ts) > 1 else ts[0],
             total / len(ts) * 1000))


def main():
    plist = load(r"C:\WMRoleScan\proxies.txt")
    print("代理条目: %d" % len(plist))
    px = {"http": plist[0], "https": plist[0]}
    print("用 sid: ...%s" % plist[0].split("sid-")[1][:12])
    print()
    print("=== 同一个 sid，各发 8 个请求 ===")
    print("  如果「后续中位」都很小（几十~几百 ms）→ 已经在复用")
    print("  如果「后续中位」还是 4~5 秒       → 每次都在重做握手，就是要改的地方")
    print()
    run("A) 每次请求传 proxies=（现状）", px, "percall")
    run("B) proxies 设在 Session 上", px, "session")
    run("C) Session 级 + 大连接池", px, "pooled")
    print()
    print("=== 换一个 sid 再确认一遍 ===")
    px2 = {"http": plist[50], "https": plist[50]}
    print("用 sid: ...%s" % plist[50].split("sid-")[1][:12])
    run("A) 每次请求传 proxies=（现状）", px2, "percall")
    run("C) Session 级 + 大连接池", px2, "pooled")


if __name__ == "__main__":
    main()
