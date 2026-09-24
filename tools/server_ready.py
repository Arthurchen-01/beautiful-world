# -*- coding: utf-8 -*-
"""
server_ready.py —— 服务器就绪监视器
================================================================
回答一个问题：**154.219.105.203 到底起来了没有？**

★ 为什么不能简单 ping / telnet
----------------------------------------------------------------
两条路都会骗你：

1. 本机默认路由走 iKuuuVPN 的 TUN（fake-ip）。它**对任何 TCP 都本地假装
   握手成功**，然后黑洞非 80/443 端口。拿测试保留地址 203.0.113.7 去连，
   一样"全端口开放"。

2. 就算绕开 TUN 走 HTTP 代理，代理对 CONNECT **乐观应答 200**，
   不管目标死活。

所以本工具**只认「真协议回复」**：
    3389 → 发 RDP TPKT，等 X.224 确认
    445  → 发 SMB1 negotiate
    5985 → 发 HTTP 请求
    443  → 真 TLS 握手
    22   → 等服务端自己报 banner

用法
----------------------------------------------------------------
    # 查一次
    python tools\\server_ready.py

    # 每 30 秒查一次，起来了就响铃并退出
    python tools\\server_ready.py --watch 30

    # 换目标
    python tools\\server_ready.py --host 1.2.3.4 --ports 3389,445,22

    # 直连模式（先关 VPN）
    python tools\\server_ready.py --first-hop none
"""
import argparse
import socket
import ssl
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# 真协议载荷 —— 只有真服务才会回话
PROBES = {
    3389: bytes.fromhex("030000132ee000000000000100080003000000"),  # RDP TPKT
    445: bytes.fromhex("00000054ff534d42720000000018012800000000000000"
                       "0000000000000000000000000000000000000000"
                       "0000000000000000"),                          # SMB1 negotiate
    80: b"GET / HTTP/1.0\r\nHost: x\r\n\r\n",
    5985: b"GET /wsman HTTP/1.1\r\nHost: x\r\n\r\n",
}
BANNER_PORTS = {22, 25, 110, 143, 21, 3306, 6379}
TLS_PORTS = {443, 8443, 5986}


def say(*a):
    try:
        print(*a, flush=True)
    except Exception:
        pass


def open_sock(first_hop, host, port, timeout=12):
    if not first_hop:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.settimeout(timeout)
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


def probe(first_hop, host, port, timeout=12):
    """返回 (是否真活, 说明)"""
    port = int(port)
    t0 = time.time()
    try:
        s = open_sock(first_hop, host, port, timeout)
    except Exception as ex:
        return False, "连不上: %s" % ex
    ms = round((time.time() - t0) * 1000)
    try:
        if port in TLS_PORTS:
            try:
                s.settimeout(8)
                ss = ssl._create_unverified_context().wrap_socket(
                    s, server_hostname=host)
                v = ss.version()
                ss.close()
                return True, "%dms TLS %s" % (ms, v)
            except Exception as ex:
                return False, "%dms TLS 失败 (%s)" % (ms, type(ex).__name__)
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
            return True, "%dms 回应 %d 字节 (%s)" % (ms, len(d), d[:8].hex())
        return False, "%dms 无回应（代理的 200 不算数）" % ms
    finally:
        try:
            s.close()
        except Exception:
            pass


def one_round(host, ports, first_hop, quiet=False):
    alive = []
    lines = []
    for p in ports:
        ok, detail = probe(first_hop, host, p)
        lines.append("  %-6s %s:%-5d %s" % ("[通]" if ok else "[不通]", host, p, detail))
        if ok:
            alive.append(p)
    if not quiet:
        say("  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        for l in lines:
            say(l)
    return alive


def main():
    ap = argparse.ArgumentParser(description="服务器就绪监视器（只认真协议回复）")
    ap.add_argument("--host", default="154.219.105.203")
    ap.add_argument("--ports", default="3389,445,22,5985")
    ap.add_argument("--first-hop", default="127.0.0.1:7890")
    ap.add_argument("--watch", type=int, default=0, help="轮询间隔秒数；0=只查一次")
    ap.add_argument("--max-rounds", type=int, default=0, help="最多轮询次数；0=不限")
    args = ap.parse_args()

    ports = [int(x) for x in args.ports.split(",") if x.strip()]
    first_hop = None
    if args.first_hop not in ("none", "-", ""):
        h, p = args.first_hop.rsplit(":", 1)
        first_hop = (h, int(p))

    say("=" * 66)
    say("服务器就绪监视器")
    say("  目标     : %s   端口 %s" % (args.host, ports))
    say("  第一跳   : %s" % (args.first_hop if first_hop else "无（直连）"))
    say("  判活方式 : 真协议握手（不信 CONNECT 的 200）")
    say("=" * 66)

    if not args.watch:
        alive = one_round(args.host, ports, first_hop)
        say("")
        if alive:
            say("★ 服务器已就绪，开放端口：%s" % alive)
            say("  下一步：")
            say("    python tools\\tunnel.py --map 13389:%s:3389 --first-hop %s"
                % (args.host, args.first_hop))
            say("    mstsc /v:127.0.0.1:13389")
            return 0
        say("✗ 服务器仍不可达（真协议无任何回应）。")
        say("  需在商家控制台的 Web VNC 里确认实例是否已开机 / 系统是否装完。")
        return 1

    rnd = 0
    while True:
        rnd += 1
        alive = one_round(args.host, ports, first_hop, quiet=True)
        stamp = time.strftime("%H:%M:%S")
        if alive:
            say("[%s] ★★ 服务器起来了！开放端口 %s" % (stamp, alive))
            say("")
            say("  下一步：")
            say("    python tools\\tunnel.py --map 13389:%s:3389 --first-hop %s"
                % (args.host, args.first_hop))
            say("    mstsc /v:127.0.0.1:13389")
            say("\a")
            return 0
        say("[%s] 第 %d 轮：仍不可达" % (stamp, rnd))
        if args.max_rounds and rnd >= args.max_rounds:
            say("已达最大轮询次数，退出。")
            return 1
        time.sleep(args.watch)


if __name__ == "__main__":
    sys.exit(main())
