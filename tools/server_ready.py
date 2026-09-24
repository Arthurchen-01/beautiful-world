# -*- coding: utf-8 -*-
"""
server_ready.py —— 服务器就绪诊断器
================================================================
回答一个问题：**这台服务器到底卡在哪一层？**

★ 为什么不能简单 ping / telnet
----------------------------------------------------------------
三条路都会骗你：

1. **本机默认路由走 iKuuuVPN 的 TUN（fake-ip）**，它**对任何 TCP 都本地假装
   握手成功**，然后黑洞非 80/443 端口。拿测试保留地址 203.0.113.7 去连，
   一样"全端口开放"。→ 所以探测要么绑物理网卡，要么走第一跳代理。

2. **HTTP 代理对 CONNECT 乐观应答 200**，不管目标死活。
   → 所以判活只认「真协议回复」。

3. **"ping 通"不等于"能用"**。ICMP 通了但 TCP 全超时，是**防火墙**的特征，
   不是"机器起来了"。这两个必须分开看。

判读表（本工具会直接给结论）
----------------------------------------------------------------
| ICMP | TCP            | 结论                                    |
|:-----|:---------------|:----------------------------------------|
| 不通 | 全超时         | 主机不在 / 网络不通 / 关机              |
| 通   | 全超时         | ★ **防火墙挡了**（或机器没有任何服务）  |
| 通   | 部分通         | 机器起来了，看开放的是哪些端口推断系统  |
| 通   | 全 RST(拒绝)   | 机器在、没防火墙、但那些端口没服务      |

TTL 还能提示系统类型：**64 ≈ Linux/BSD，128 ≈ Windows**。

用法
----------------------------------------------------------------
    # 查一次（默认目标）
    python tools\\server_ready.py

    # 换目标 + 扫更多端口
    python tools\\server_ready.py --host 1.2.3.4 --ports 22,3389,445,80,443,5985

    # 轮询，起来了就响铃退出
    python tools\\server_ready.py --watch 30

    # 直连模式（先关 VPN；会用本机公网 IP 去连）
    python tools\\server_ready.py --first-hop none --direct

    # 用第一跳代理探测（默认，源 IP 不暴露）
    python tools\\server_ready.py --first-hop 127.0.0.1:7890
"""
import argparse
import os
import re
import socket
import ssl
import struct
import subprocess
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# 真协议载荷 —— 只有真服务才会回话
PROBES = {
    3389: bytes.fromhex("030000132ee000000000000100080003000000"),   # RDP TPKT
    445: bytes.fromhex("00000054ff534d42720000000018012800000000000000"
                       "0000000000000000000000000000000000000000"
                       "0000000000000000"),                          # SMB1 negotiate
    80: b"GET / HTTP/1.0\r\nHost: x\r\n\r\n",
    5985: b"GET /wsman HTTP/1.1\r\nHost: x\r\n\r\n",
}
BANNER_PORTS = {22, 25, 110, 143, 21, 3306, 6379}
TLS_PORTS = {443, 8443, 5986}
DEFAULT_PORTS = [3389, 445, 22, 5985, 80, 443, 135, 139]


def say(*a):
    try:
        print(*a, flush=True)
    except Exception:
        pass


# ------------------------------------------------------------------ 本机信息
def _is_fake_ip(ip):
    """198.18.0.0/15 是 iKuuu / Clash 这类 TUN 的 fake-ip 网段。

    ★ 必须排掉它 —— 绑到 fake-ip 上发出去的包会被 TUN 本地假接受，
      于是又变成"全端口开放"的假象。**这个坑我自己踩过一次**：
      `--direct` 模式绑到了 198.18.0.1，结果所有端口都"通"。
    """
    try:
        a, b = (int(x) for x in ip.split(".")[:2])
        return a == 198 and 18 <= b <= 19
    except Exception:
        return False


def local_phys_ip():
    """本机**物理网卡** IP —— 明确排除 TUN 的 fake-ip 网段。

    用 UDP 探测法拿到的是「默认路由走的那个地址」，VPN 开着时那是 TUN 地址，
    不能用来做直连探测。所以这里枚举本机所有地址，挑一个真实局域网地址。
    """
    cands = []
    # 1) 主机名解析出来的所有 IPv4
    try:
        cands += socket.gethostbyname_ex(socket.gethostname())[2]
    except Exception:
        pass
    # 2) UDP 探测（作为补充）
    for tgt in (("223.5.5.5", 80), ("114.114.114.114", 80), ("192.168.0.1", 80)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(tgt)
            cands.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass

    seen = []
    for ip in cands:
        if ip in seen:
            continue
        seen.append(ip)
        if ip.startswith("127.") or _is_fake_ip(ip):
            continue
        return ip
    return None


def vpn_tun_detected():
    """本机是不是挂着 fake-ip 模式的 TUN（Clash / iKuuu 那类）"""
    try:
        return any(_is_fake_ip(ip)
                   for ip in socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:
        return False


# ------------------------------------------------------------------ ICMP
def icmp_ping(host, src=None, count=3, timeout_ms=2000):
    """用系统 ping 命令（要 TTL，Python 原生做 ICMP 要管理员权限）

    返回 (是否通, 平均延迟ms, TTL, 原始输出)

    ★ 判定「通不通」用**退出码**，不用文本正则。
      Windows 的 ping 输出是 GBK/OEM 编码，按 UTF-8 解码会变成乱码，
      中文正则（"来自 ... 的回复"）永远匹配不上 —— 实测踩过：
      TTL 能读到（`TTL=` 是 ASCII）但判成"不通"，自相矛盾。
      退出码 0 = 至少有一个回复，这个最可靠。
    """
    cmd = ["ping", "-n", str(count), "-w", str(timeout_ms)]
    if src:
        cmd += ["-S", src]
    cmd.append(host)
    try:
        r = subprocess.run(cmd, capture_output=True,
                           creationflags=0x08000000)
    except Exception as e:
        return False, None, None, "ping 执行失败: %s" % e

    # 退出码 0 = 有回复。同时把输出按多种编码试着解，只为了拿 TTL。
    ok = (r.returncode == 0)
    raw = b""
    for stream in (r.stdout, r.stderr):
        if stream:
            raw += stream
    out = ""
    for enc in ("gbk", "utf-8", "latin1"):
        try:
            out = raw.decode(enc, "replace")
            break
        except Exception:
            continue

    # 兜底：退出码不可信时（某些系统 ping 不返回码）再看文本
    if not ok:
        ok = bool(re.search(r"(Reply from|的回复|bytes=\d+)", out))

    ms = None
    m = re.search(r"(?:time|时间)[=<]\s*(\d+)\s*ms", out)
    if m:
        ms = int(m.group(1))
    ttl = None
    m = re.search(r"TTL=(\d+)", out, re.I)
    if m:
        ttl = int(m.group(1))
    return ok, ms, ttl, out


# ------------------------------------------------------------------ TCP
def open_sock(first_hop, host, port, timeout=12, src=None):
    if not first_hop:
        s = socket.socket()
        s.settimeout(timeout)
        if src:
            try:
                s.bind((src, 0))
            except Exception:
                pass
        s.connect((host, int(port)))
        return s
    s = socket.create_connection(first_hop, timeout=timeout)
    s.settimeout(timeout)
    s.sendall(("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n"
               % (host, int(port), host, int(port))).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = s.recv(4096)
        if not d:
            raise ConnectionError("第一跳断开")
        buf += d
    if " 200" not in buf.split(b"\r\n", 1)[0].decode("latin1", "replace"):
        raise ConnectionError("第一跳拒绝 CONNECT")
    return s


def probe_tcp(first_hop, host, port, timeout=12, src=None):
    """返回 (状态, 说明)

    状态: 'open' 有真回应 / 'open-nodata' 连上但没回话 / 'refused' 端口关 /
          'drop' 被防火墙丢包 / 'err' 其它
    """
    port = int(port)
    t0 = time.time()
    try:
        s = open_sock(first_hop, host, port, timeout, src)
    except ConnectionRefusedError:
        return "refused", "端口关着（收到 RST —— 说明**没防火墙挡，只是没服务**）"
    except socket.timeout:
        return "drop", "超时（包被丢了 —— **典型的防火墙 DROP**）"
    except Exception as ex:
        msg = str(ex)
        if "timed out" in msg.lower():
            return "drop", "超时（包被丢了 —— **典型的防火墙 DROP**）"
        return "err", "%s: %s" % (type(ex).__name__, msg[:60])
    ms = round((time.time() - t0) * 1000)

    try:
        if port in TLS_PORTS:
            try:
                s.settimeout(8)
                ss = ssl._create_unverified_context().wrap_socket(
                    s, server_hostname=host)
                v = ss.version()
                ss.close()
                return "open", "%dms TLS %s" % (ms, v)
            except Exception as ex:
                return "open-nodata", "%dms TLS 失败 (%s)" % (ms, type(ex).__name__)
        d = b""
        if port in BANNER_PORTS:
            try:
                d = s.recv(64)
            except Exception:
                d = b""
        elif port in PROBES:
            s.sendall(PROBES[port])
            try:
                d = s.recv(64)
            except Exception:
                d = b""
        else:
            for payload in (None, b"\x05\x01\x00", b"\r\n"):
                if payload:
                    s.sendall(payload)
                try:
                    d = s.recv(64)
                except Exception:
                    d = b""
                if d:
                    break
        if d:
            return "open", "%dms 真回应 %d 字节 (%s)" % (ms, len(d), d[:8].hex())
        return "open-nodata", "%dms 连上了但没回话" % ms
    finally:
        try:
            s.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 判定
def looks_like_tun_fake(results):
    """★ 假开放的特征：**所有**端口都在 1~30ms 内"连上但没回话"。

    真机器不可能所有端口都开着还都不回话；这是 TUN 在本地假握手。
    命中就说明探测根本没出去，结论一个字都不能信。
    """
    if not results:
        return False
    fast_nodata = 0
    for st, msg in results.values():
        if st == "open-nodata" and re.search(r"\b(\d{1,2})ms", msg):
            ms = int(re.search(r"\b(\d{1,2})ms", msg).group(1))
            if ms <= 30:
                fast_nodata += 1
    return fast_nodata == len(results)


def suspicious_slow_open(results, threshold_ms=1500):
    """★ 「慢半通」：连上了但**耗时异常长**、且发数据就被重置。

    实测过这个坑：机房的**防护网关**会先替后端把 TCP 三次握手接掉（SYN 代理），
    所以端口看起来"开着"；但后端其实没有服务，
    于是——连接耗时 7~15 秒（正常 RDP 是几十毫秒），
    而且一发真协议数据就收到 RST。

    **这种"开放"是假的**，不能据此说"服务器起来了"。
    """
    out = []
    for p, (st, msg) in results.items():
        if st != "open-nodata":
            continue
        m = re.search(r"(\d+)ms", msg)
        if m and int(m.group(1)) >= threshold_ms:
            out.append(p)
    return out


def verdict(icmp_ok, results, ttl):
    """把观测翻译成人话"""
    # ★ 先排除「假开放」—— 这个结论一旦漏判，后面全错
    if looks_like_tun_fake(results):
        return ("⛔ 探测没出去！结果全部无效（TUN 假握手）",
                ["所有端口都在几十毫秒内『连上但不回话』—— 真机器不可能这样。",
                 "这是 **VPN 的 TUN（fake-ip 模式）在本地假接受连接**，",
                 "包根本没发出去。**下面的端口结论一个字都不能信。**",
                 "",
                 "怎么办：",
                 "  · 关掉 VPN 再用 --direct，或者",
                 "  · 别用 --direct，改走第一跳代理（默认方式）"],
                "")

    states = [st for st, _ in results.values()]
    n_open = sum(1 for st in states if st in ("open", "open-nodata"))
    n_ref = sum(1 for st in states if st == "refused")
    n_drop = sum(1 for st in states if st == "drop")
    slow = suspicious_slow_open(results)

    os_hint = ""
    if ttl is not None:
        if ttl <= 70:
            os_hint = "TTL=%d → 像 **Linux/BSD**（Windows 默认是 128）" % ttl
        elif ttl <= 140:
            os_hint = "TTL=%d → 像 **Windows**" % ttl
        else:
            os_hint = "TTL=%d（少见的值，可能是网络设备）" % ttl

    # ★ 只有「慢半通」而没有真回应 → 是中间设备，不是服务
    if n_open > 0 and n_open == len(slow) and not any(
            st == "open" for st in states):
        return ("🔴 那些『开放』的端口是**中间设备接的，不是真服务**",
                ["特征：TCP 连接要 1.5 秒以上才『成功』，发真协议数据就被重置。",
                 "真服务不可能这样（正常 RDP/SSH 握手是几十毫秒）。",
                 "这是机房的**防护网关在做 SYN 代理** —— 它替后端接了握手，",
                 "但后端根本没有服务在跑。",
                 "",
                 "**结论：机器本身还是没起来 / 没装好。**",
                 "别被『端口开着』骗了 —— 那个端口连不进去。",
                 "拿这条去问服务商：为什么 3389 是网关接的、后端没起来。"],
                os_hint)

    if not icmp_ok and n_open == 0:
        return ("⛔ 主机不在 / 网络不通",
                ["ping 不通，TCP 也全无响应 —— 机器没起来、关机，或链路断。",
                 "去服务商控制台看实例状态（运行中？），用 Web VNC 看画面。"],
                os_hint)

    if icmp_ok and n_open == 0 and n_drop > 0:
        return ("🔴 ping 通但 TCP 全被挡 —— **防火墙问题，不是机器问题**",
                ["ICMP 能过、TCP 全被丢包，这是**防火墙 DROP** 的特征，",
                 "跟『机器没起来』是两回事 —— 机器很可能已经起来了。",
                 "两个地方要查：",
                 "  ① **服务商的安全组 / 防护策略** —— 端口没放行（最常见）",
                 "  ② 机器自己的防火墙 —— Windows 防火墙 / Linux iptables",
                 "先让服务商放行 **3389（远程桌面）** 或 **22（SSH）**。"],
                os_hint)

    if icmp_ok and n_ref > 0 and n_open == 0:
        return ("🟡 机器在、没防火墙挡，但那些端口没服务",
                ["收到 RST（端口关），说明**包能到机器、也没被防火墙丢**，",
                 "只是那些端口上没有程序在监听。",
                 "→ 系统装好了但对应服务没开，或者装的是别的系统。"],
                os_hint)

    if n_open > 0:
        opened = [p for p, (st, _) in results.items() if st in ("open", "open-nodata")]
        win = [p for p in opened if p in (3389, 445, 135, 139, 5985, 5986)]
        nix = [p for p in opened if p in (22,)]
        extra = []
        if win and not nix:
            extra.append("开放的端口像 **Windows**（3389/445/135/139）")
        elif nix and not win:
            extra.append("开放的端口像 **Linux**（只有 22）")
        elif win and nix:
            extra.append("Windows 和 Linux 的特征端口都有（可能都开了，或中间有转发）")
        if slow:
            extra.append("⚠️ 其中 %s 是「慢半通」（连接超过 1.5 秒），"
                         "可能是中间设备接的，**建议真握手确认**" % slow)
        return ("✅ 服务器已就绪，开放端口 %s" % opened, extra, os_hint)

    return ("❓ 结果不明确", ["看下面的逐端口明细。"], os_hint)


# ------------------------------------------------------------------ 主流程
def one_round(host, ports, first_hop, src, quiet=False):
    icmp_ok, ms, ttl, _raw = icmp_ping(host, src=src)

    results = {}
    for p in ports:
        st, msg = probe_tcp(first_hop, host, p, src=src)
        results[p] = (st, msg)

    v, notes, os_hint = verdict(icmp_ok, results, ttl)

    if not quiet:
        say("  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        say("  ── ICMP ──")
        say("    %s  %s%s" % ("通" if icmp_ok else "不通",
                              ("%dms  " % ms) if ms is not None else "",
                              ("TTL=%d" % ttl) if ttl is not None else ""))
        if os_hint:
            say("    %s" % os_hint)
        say("  ── TCP ──")
        for p in ports:
            st, msg = results[p]
            mark = {"open": "[通]  ", "open-nodata": "[半通]", "refused": "[拒绝]",
                    "drop": "[丢包]", "err": "[出错]"}.get(st, "[?]   ")
            say("    %s :%-5d %s" % (mark, p, msg))
        say("  ── 结论 ──")
        say("    %s" % v)
        for n in notes:
            say("    %s" % n)

    return icmp_ok, results, v


def main():
    ap = argparse.ArgumentParser(description="服务器就绪诊断器")
    ap.add_argument("--host", default="154.219.105.203")
    ap.add_argument("--ports", default=",".join(str(p) for p in DEFAULT_PORTS))
    ap.add_argument("--first-hop", default="127.0.0.1:7890",
                    help="第一跳 HTTP 代理；none = 直连")
    ap.add_argument("--direct", action="store_true",
                    help="直连模式：绑定本机物理网卡（需先关 VPN，会暴露本机公网 IP）")
    ap.add_argument("--watch", type=int, default=0, help="轮询间隔秒；0=只查一次")
    ap.add_argument("--max-rounds", type=int, default=0)
    args = ap.parse_args()

    ports = [int(x) for x in args.ports.split(",") if x.strip()]

    first_hop = None
    if not args.direct and args.first_hop not in ("none", "-", ""):
        h, p = args.first_hop.rsplit(":", 1)
        first_hop = (h, int(p))

    src = None
    if args.direct:
        src = local_phys_ip()
        first_hop = None
        if not src or _is_fake_ip(src):
            say("=" * 68)
            say("⛔ 直连模式不可用：找不到物理网卡地址")
            say("   本机挂着 fake-ip 模式的 VPN（TUN），默认路由不走物理网卡。")
            say("   绑到 fake-ip 上探测会得到『全端口开放』的**假结果**。")
            say("")
            say("   两个办法：")
            say("     · 关掉 VPN 再跑一次")
            say("     · 去掉 --direct，改走第一跳代理（默认方式，推荐）")
            say("=" * 68)
            return 2

    say("=" * 68)
    say("服务器就绪诊断器")
    say("  目标     : %s   端口 %s" % (args.host, ports))
    if args.direct:
        say("  探测方式 : 直连（绑定物理网卡 %s）" % src)
        say("             ⚠️ 会暴露本机公网 IP，且需先关 VPN")
    elif first_hop:
        say("  探测方式 : 经第一跳 %s:%d" % first_hop)
    else:
        say("  探测方式 : 直连（不绑网卡）")
    say("  判活方式 : 真协议握手 + RST/超时 区分（不信代理的 200）")
    if vpn_tun_detected() and not args.direct:
        say("  ⚠️ 检测到本机挂着 fake-ip 模式的 TUN —— 直连类探测会被它骗，")
        say("     所以本工具默认走第一跳代理。")
    say("=" * 68)
    say("")

    if not args.watch:
        one_round(args.host, ports, first_hop, src)
        return 0

    rnd = 0
    while True:
        rnd += 1
        icmp_ok, results, v = one_round(args.host, ports, first_hop, src, quiet=True)
        stamp = time.strftime("%H:%M:%S")
        if v.startswith("✅"):
            say("[%s] %s" % (stamp, v))
            say("")
            say("  下一步：")
            say("    python tools\\tunnel.py --map 13389:%s:3389 --first-hop %s"
                % (args.host, args.first_hop))
            say("    mstsc /v:127.0.0.1:13389")
            say("\a")
            return 0
        say("[%s] 第 %d 轮：%s" % (stamp, rnd, v))
        if args.max_rounds and rnd >= args.max_rounds:
            say("已达最大轮询次数，退出。")
            return 1
        time.sleep(args.watch)


if __name__ == "__main__":
    sys.exit(main())
