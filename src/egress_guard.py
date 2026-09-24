# -*- coding: utf-8 -*-
"""
egress_guard.py —— 出口红线守卫 v2（可选：与本机「零泄漏闸门」协同）
================================================================
**任何一次出站都绝不能落到真实国内 IP 上。** 这是硬约束。

如果本机装了「EgressGuard 零泄漏闸门」，它的核心判据是：

    GetExtendedTcpTable 的 dwLocalAddr
      · 198.18.x.x     → 进了隧道，安全
      · 192.168.x.x    → 绑定物理网卡 = 裸奔泄漏 = 掐断

**本模块用完全一样的判据自查我们自己的连接**，不依赖闸门是否开着。
闸门路径通过环境变量 EGRESSGUARD_DIR 或配置项指定，不写死。

三道检查：
  1. preflight()             开工前体检：闸门状态 / 代理端口 / 隧道绑定 / 出口归属
  2. check_tunnel_binding()  ★ 最硬：实测我们自己的连接绑的是隧道还是物理网卡
  3. egressguard_status()    查询本机 EgressGuard 闸门状态（装了才查）

设计原则：
  · **宁可停工，不可裸连**。检查不过就 raise，绝不"降级直连"。
  · 只经代理去问出口 IP —— 绝不为了"看看自己 IP"而发一个直连请求（那本身就是泄露）。
  · **绝不把自己加进闸门白名单** —— 那等于把保护关掉；我们要的是被它保护。
"""
import ipaddress
import json
import os
import socket
import sys
import time
from urllib.parse import urlparse

import netguard  # noqa: F401  —— 强制代理守卫，必须早于 requests
import requests

PROXY = os.environ.get("WM_PROXY", "http://127.0.0.1:7890")
PROXIES = {"http": PROXY, "https": PROXY}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

FORBIDDEN_COUNTRIES = {"CN", "CHINA"}

# iKuuu 隧道的 fake-ip 网段（与 EgressGuard 的 tunnel_cidrs 一致）
TUNNEL_CIDRS = ["198.18.0.0/15"]

# EgressGuard 闸门 API
EG_API = "http://127.0.0.1:47821"

ECHO_ENDPOINTS = [
    ("http://ip-api.com/json/?fields=status,country,countryCode,regionName,city,isp,query",
     "ip-api"),
    ("https://api.ipify.org?format=json", "ipify"),
]


class EgressUnsafe(RuntimeError):
    """出口不安全 —— 必须停工"""


def _safe_print(*a, **k):
    try:
        print(*a, **k)
    except Exception:
        pass


# ================================================================ 基础
def proxy_listening(host="127.0.0.1", port=7890, timeout=3):
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except Exception:
        return False


# ================================================================ 1. 隧道绑定自查
def _in_tunnel(ip):
    try:
        a = ipaddress.ip_address(ip)
    except Exception:
        return False
    for c in TUNNEL_CIDRS:
        try:
            if a in ipaddress.ip_network(c):
                return True
        except Exception:
            pass
    return False


def local_bind_ip(host, port, timeout=10):
    """连一下目标，返回**本地绑定地址** —— 这是 EgressGuard 用的同一判据"""
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        return s.getsockname()[0]
    finally:
        s.close()


def physical_ips():
    """本机物理网卡 IP 集合（UDP 探测法，不依赖 PowerShell）"""
    ips = set()
    for target in (("8.8.8.8", 80), ("1.1.1.1", 80), ("223.5.5.5", 80)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(target)
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return ips


def _hosts_from_proxy_file():
    """从代理文件里取几个真实目标，给隧道绑定自查用。

    ★ 原来 hosts 的默认值是 `us.proxy.example` —— 那是**脱敏占位符**，
      根本解析不了。结果是自查每次都打印一行 `→ None !! 裸奔`，
      把"没连上"误报成"裸奔"（恰好是反的），自检看起来像出事了。
    """
    import os as _os
    cand = []
    for p in (_os.environ.get("WM_PROXY_FILE", ""),
              _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "proxies.txt"),
              _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                            "..", "proxies.txt")):
        if p and _os.path.exists(p):
            cand.append(p)
    for p in cand:
        out = []
        try:
            with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    parts = ln.split(":")
                    if len(parts) < 2:
                        continue
                    h, pt = parts[0], parts[1]
                    if h in ("127.0.0.1", "localhost"):
                        continue          # 本地桥接端口，查不出隧道归属
                    if pt.isdigit():
                        hp = (h, int(pt))
                        if hp not in out:          # 去重：代理池里同一个域名会重复上百次
                            out.append(hp)
                    if len(out) >= 3:
                        break
        except Exception:
            continue
        if out:
            return out
    return None


def check_tunnel_binding(hosts=None, verbose=True):
    """★ 最硬的自查：我们自己的出站连接到底绑在哪个地址上？

    返回 {"ok": bool, "results": [...], "physical_ips": [...], "tunnel_cidrs": [...]}
    """
    if hosts is None:
        hosts = _hosts_from_proxy_file()
        if not hosts:
            # 没有真实代理文件 —— 明说"跳过"，别拿占位符假装查过了
            if verbose:
                _safe_print("[tunnel] 没有代理文件，隧道绑定自查跳过"
                            "（设 WM_PROXY_FILE 或放一个 proxies.txt 就能查）")
            return {"ok": True, "results": [], "skipped": True,
                    "physical_ips": sorted(physical_ips()),
                    "tunnel_cidrs": TUNNEL_CIDRS}
    res, bad = [], []
    phys = physical_ips()
    for h, p in hosts:
        if h in ("127.0.0.1", "localhost"):
            continue
        try:
            ip = local_bind_ip(h, p)
        except Exception as e:
            res.append({"target": f"{h}:{p}", "local_ip": None,
                        "err": f"{type(e).__name__}: {str(e)[:60]}", "in_tunnel": None})
            continue
        ok = _in_tunnel(ip)
        rec = {"target": f"{h}:{p}", "local_ip": ip, "in_tunnel": ok,
               "is_physical": ip in phys}
        res.append(rec)
        if not ok:
            bad.append(rec)

    out = {"ok": not bad, "results": res, "physical_ips": sorted(phys),
           "tunnel_cidrs": TUNNEL_CIDRS}
    if verbose:
        for r in res:
            if r.get("local_ip") is None:
                _safe_print(f"[tunnel] {r['target']:<24} 连不上 ({r.get('err')})")
            else:
                mark = "OK 隧道内" if r["in_tunnel"] else "!! 裸奔(物理网卡)"
                _safe_print(f"[tunnel] {r['target']:<24} 本地地址={r['local_ip']:<16} {mark}")
    return out


# ================================================================ 2. EgressGuard 状态
def egressguard_status(timeout=5):
    """查本机 EgressGuard 闸门状态。没装 / 没跑 → 返回 None。"""
    try:
        r = requests.get(f"{EG_API}/api/status", timeout=timeout)
        d = r.json()
        return {"running": True, "mode": d.get("mode"), "enabled": d.get("enabled"),
                "dry_run": d.get("dry_run"), "action": d.get("effective_action"),
                "tunnel_ok": d.get("tunnel_ok"), "egress_ip": d.get("egress_ip"),
                "egress_geo": d.get("egress_geo")}
    except Exception:
        return None


def why_blocked(pid=None, timeout=5):
    """问闸门：我这个进程为什么被掐了？（EgressGuard 的 /api/why）"""
    try:
        return requests.get(f"{EG_API}/api/why", params={"pid": pid or os.getpid()},
                            timeout=timeout).json()
    except Exception:
        return None


# ================================================================ 3. 出口 IP
def egress_info(timeout=15):
    s = requests.Session()
    s.trust_env = False
    ip = None
    for url, tag in ECHO_ENDPOINTS:
        try:
            r = s.get(url, proxies=PROXIES, headers={"User-Agent": UA}, timeout=timeout)
            if tag == "ip-api":
                d = r.json()
                if d.get("status") == "success":
                    return {"ip": d.get("query"), "country": d.get("countryCode"),
                            "country_name": d.get("country"), "region": d.get("regionName"),
                            "city": d.get("city"), "isp": d.get("isp"), "via": "ip-api"}
            else:
                ip = r.json().get("ip")
                break
        except Exception:
            continue
    if ip:
        try:
            r = s.get(f"http://ip-api.com/json/{ip}"
                      "?fields=status,country,countryCode,regionName,city,isp,query",
                      proxies=PROXIES, headers={"User-Agent": UA}, timeout=timeout)
            d = r.json()
            if d.get("status") == "success":
                return {"ip": d.get("query"), "country": d.get("countryCode"),
                        "country_name": d.get("country"), "region": d.get("regionName"),
                        "city": d.get("city"), "isp": d.get("isp"), "via": "ipify+ip-api"}
        except Exception:
            pass
        return {"ip": ip, "country": None, "country_name": None, "region": None,
                "city": None, "isp": None, "via": "ipify"}
    return None


# ================================================================ 总入口
def preflight(verbose=True, allow_cn=False, retries=3, check_tunnel=True):
    """开工前体检。不安全就 raise EgressUnsafe。返回出口信息 dict。"""
    def say(s):
        if verbose:
            _safe_print(s)

    # --- 0) EgressGuard 闸门状态（装了才看）---
    eg = egressguard_status()
    if eg:
        say(f"[egress] 本机 EgressGuard 闸门: 模式={eg['mode']} 启用={eg['enabled']} "
            f"演练={eg['dry_run']} 隧道={eg['tunnel_ok']}")
        if eg["enabled"] and not eg["dry_run"] and not eg.get("tunnel_ok"):
            raise EgressUnsafe(
                "EgressGuard 闸门在实弹档，但**隧道是断的** —— 拒绝开工。\n"
                "  隧道断时出站会被闸门掐断，且可能裸奔。先把 VPN 连上。")
    else:
        say("[egress] 未检测到 EgressGuard 闸门（没装或没在跑）")

    # --- 1) 代理端口 ---
    if not proxy_listening():
        raise EgressUnsafe(
            f"代理端口 {PROXY} 没在监听 —— 拒绝开工。\n"
            f"  （宁可停工也不裸连：裸连 = 用你的真实 IP 直连目标站）")

    # --- 2) ★ 隧道绑定自查（最硬，不依赖闸门）---
    if check_tunnel:
        tb = check_tunnel_binding(verbose=verbose)
        if not tb["ok"]:
            bad = [r for r in tb["results"] if r.get("in_tunnel") is False]
            raise EgressUnsafe(
                "★ 出口连接绑在**物理网卡**上，没走隧道 —— 拒绝开工！\n"
                f"  裸奔的连接: {[(r['target'], r['local_ip']) for r in bad]}\n"
                f"  物理网卡 IP: {tb['physical_ips']}\n"
                f"  隧道网段   : {tb['tunnel_cidrs']}\n"
                "  这会把你真实的昆明 IP 暴露给目标。先确认 iKuuu/VPN 在正常工作。")

    # --- 3) 出口 IP 归属 ---
    info = None
    for i in range(retries):
        info = egress_info()
        if info and info.get("ip"):
            break
        say(f"[egress] 第 {i+1}/{retries} 次取出口失败，重试 ...")
        time.sleep(2 + i * 2)

    if not info or not info.get("ip"):
        raise EgressUnsafe(
            "取不到出口 IP（代理可能在抖）—— 拒绝开工。\n"
            "  取不到就无法证明出口安全，按最坏情况处理。")

    ip, cc = info["ip"], (info.get("country") or "").upper()
    say(f"[egress] 出口 IP = {ip}")
    say(f"[egress] 归属     = {info.get('country_name')} / {info.get('region')} / "
        f"{info.get('city')} / {info.get('isp')}  ({cc})")

    if not cc:
        raise EgressUnsafe(f"出口 {ip} 的归属地查不到 —— 拒绝开工（无法排除是国内 IP）")

    if cc in FORBIDDEN_COUNTRIES and not allow_cn:
        raise EgressUnsafe(
            f"★ 出口是国内 IP（{cc} {info.get('region')} {info.get('city')}）—— 已阻断！\n"
            f"  这会把你的真实位置暴露给目标站。")

    say("[egress] ✅ 三项全过：代理在跑 / 连接走隧道 / 出口不在国内")
    return info


def assert_safe(url, verbose=False):
    host = (urlparse(url).hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1") or host.startswith("127."):
        return True
    if not proxy_listening():
        raise EgressUnsafe(f"代理挂了，拒绝访问 {host}")
    return True


if __name__ == "__main__":
    _safe_print("=" * 70)
    _safe_print("出口红线体检")
    _safe_print("=" * 70)
    try:
        info = preflight()
        _safe_print("\n结果: " + json.dumps(info, ensure_ascii=False))
    except EgressUnsafe as e:
        _safe_print(f"\n❌ {e}")
        sys.exit(2)
