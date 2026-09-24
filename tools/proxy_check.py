# -*- coding: utf-8 -*-
"""
proxy_check.py —— 代理池体检：通不通 / 出口 IP 是否真不同 / 延迟 / 归属地

代理行格式： host:port:username:password
用法： python proxy_check.py [proxies.txt] [并发数]
"""
import io, json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "proxies.txt")
CONC = int(sys.argv[2]) if len(sys.argv) > 2 else 10

ECHO = "http://ip-api.com/json/?fields=status,country,countryCode,regionName,city,isp,query"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def parse(line):
    """host:port:user:pass  ->  (name, proxy_url)"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(":")
    if len(parts) < 4:
        return None
    host, port, user, pwd = parts[0], parts[1], ":".join(parts[2:-1]), parts[-1]
    m = re.search(r"sid-([A-Za-z0-9]+)", user)
    name = m.group(1) if m else user[-8:]
    url = f"socks5h://{user}:{pwd}@{host}:{port}"   # ★ 这批是 SOCKS5
    return name, url


def check(item):
    name, url = item
    t0 = time.time()
    try:
        r = requests.get(ECHO, proxies={"http": url, "https": url},
                         headers={"User-Agent": UA}, timeout=25)
        dt = time.time() - t0
        d = r.json()
        return {"name": name, "url": url, "ok": True, "ms": round(dt * 1000),
                "ip": d.get("query"), "cc": d.get("countryCode"),
                "region": d.get("regionName"), "city": d.get("city"),
                "isp": d.get("isp")}
    except Exception as e:
        return {"name": name, "url": url, "ok": False, "ms": round((time.time() - t0) * 1000),
                "err": f"{type(e).__name__}: {str(e)[:80]}"}


items = [x for x in (parse(l) for l in io.open(FILE, encoding="utf-8")) if x]
print(f"解析到 {len(items)} 条代理，并发 {CONC} 体检中 ...\n")

with ThreadPoolExecutor(max_workers=CONC) as ex:
    results = list(ex.map(check, items))

ok = [r for r in results if r["ok"]]
bad = [r for r in results if not r["ok"]]
ips = {}
for r in ok:
    ips.setdefault(r["ip"], []).append(r["name"])

print(f"=== 结果 ===")
print(f"  可用 {len(ok)}/{len(results)}   不可用 {len(bad)}")
if ok:
    ms = sorted(r["ms"] for r in ok)
    print(f"  延迟  最快 {ms[0]}ms  中位 {ms[len(ms)//2]}ms  最慢 {ms[-1]}ms")
    ccs = {}
    for r in ok:
        ccs[r.get("cc")] = ccs.get(r.get("cc"), 0) + 1
    print(f"  出口国家: {ccs}")
    print(f"  唯一出口 IP: {len(ips)} 个（{len(ok)} 条代理）")
    dup = {k: v for k, v in ips.items() if len(v) > 1}
    if dup:
        print(f"  ⚠ 有 {len(dup)} 个 IP 被多条代理共用：")
        for k, v in list(dup.items())[:5]:
            print(f"      {k} <- {v}")
    else:
        print(f"  ✅ 每条代理一个独立 IP，无重复")
    print(f"\n  样例（前 8 条）：")
    for r in ok[:8]:
        print(f"    {r['name']:<12} {r['ip']:<16} {r.get('cc')}/{r.get('region')}/"
              f"{r.get('city')}  {r['ms']}ms  {r.get('isp')}")
if bad:
    print(f"\n  失败样例（前 5 条）：")
    for r in bad[:5]:
        print(f"    {r['name']:<12} {r['err']}")

json.dump(results, io.open(os.path.join(HERE, "proxy_check_out.json"), "w",
                           encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"\n-> proxy_check_out.json")
