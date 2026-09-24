# -*- coding: utf-8 -*-
"""
tunnel.py —— 本地端口转发（经第一跳），给 RDP / WinRM / SMB 用
================================================================
为什么需要它
----------------------------------------------------------------
本机默认路由走 iKuuuVPN 的 TUN（fake-ip 模式），它**对所有 TCP 都本地假装
握手成功**，然后把非 80/443 端口直接黑洞。所以：

    · mstsc 直连 3389        → 假连上，然后卡死
    · WinRM 连 5985          → 同上
    · 任何端口扫描           → 全部"开放"，全是假的

（铁证：拿测试保留地址 203.0.113.7 去连，也是"全端口开放"。）

本工具用本地 HTTP 代理（iKuuu `127.0.0.1:7890`，香港出口）做第一跳，
把远端端口映射到本地，于是：

    mstsc /v:127.0.0.1:13389      →  经香港出口  →  服务器 3389

**服务器看到的是香港 IP，不是昆明 IP。**

用法
----------------------------------------------------------------
    # 一条映射
    python tunnel.py --map 13389:154.219.105.203:3389

    # 多条（RDP + SMB + WinRM + SSH）
    python tunnel.py ^
        --map 13389:154.219.105.203:3389 ^
        --map 1445:154.219.105.203:445 ^
        --map 15985:154.219.105.203:5985 ^
        --map 10022:154.219.105.203:22

    # 直连（关掉 VPN 时用，源 IP 是本机公网 IP）
    python tunnel.py --map 13389:154.219.105.203:3389 --first-hop none

参数
----------------------------------------------------------------
    --map        本地端口:目标主机:目标端口   可重复
    --first-hop  第一跳 HTTP 代理，默认 127.0.0.1:7890；写 none 表示直连
    --probe      启动后先探一次目标端口，报告真实连通性
"""
import argparse
import select
import socket
import sys
import threading
import time

# ★ 控制台是 GBK 时，任何非 GBK 字符（★ ✗ → 等）都会让 print 抛
#   UnicodeEncodeError。而 say() 为了不让日志打断主流程会吞掉异常 ——
#   结果是"整行日志凭空消失"，极难排查。这里统一把 stdout/stderr 改成
#   UTF-8 + replace，从根上避免。
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


def open_via_first_hop(first_hop, host, port, timeout=25):
    if not first_hop:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.settimeout(timeout)
        return s
    fh_host, fh_port = first_hop
    s = socket.create_connection((fh_host, int(fh_port)), timeout=timeout)
    s.settimeout(timeout)
    s.sendall(("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n"
               % (host, int(port), host, int(port))).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = s.recv(4096)
        if not d:
            raise ConnectionError("第一跳在 CONNECT 阶段断开")
        buf += d
    line = buf.split(b"\r\n", 1)[0].decode("latin1", "replace")
    if " 200" not in line:
        raise ConnectionError("第一跳拒绝: %s" % line.strip())
    return s


def pump(a, b):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 180)
            if not r:
                break
            for s in r:
                d = s.recv(65536)
                if not d:
                    return
                (b if s is a else a).sendall(d)
    except Exception:
        pass


def handle(conn, target, first_hop):
    try:
        up = open_via_first_hop(first_hop, target[0], target[1])
    except Exception as ex:
        say("  [!] -> %s:%d 失败: %s" % (target[0], target[1], ex))
        try:
            conn.close()
        except Exception:
            pass
        return
    pump(conn, up)
    for s in (conn, up):
        try:
            s.close()
        except Exception:
            pass


def serve(listen_port, target, first_hop, stop):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", listen_port))
    srv.listen(32)
    srv.settimeout(1.0)
    say("  [OK] 127.0.0.1:%-6d -> %s:%d" % (listen_port, target[0], target[1]))
    while not stop.is_set():
        try:
            c, _ = srv.accept()
        except socket.timeout:
            continue
        except Exception:
            break
        threading.Thread(target=handle, args=(c, target, first_hop), daemon=True).start()
    try:
        srv.close()
    except Exception:
        pass


# 各端口的"真握手"载荷 —— 只有真服务才会回话
#
# ★ 为什么必须这样：iKuuu 这类 HTTP 代理对 CONNECT **乐观应答 200**，
#   不管目标死活都返回 200。所以"CONNECT 成功"完全不能证明目标在线。
#   唯一可靠的办法是发一个真协议请求，看有没有真回复。
PROBES = {
    3389: bytes.fromhex("030000132ee000000000000100080003000000"),   # RDP TPKT
    80: b"GET / HTTP/1.0\r\nHost: ",
    5985: b"GET /wsman HTTP/1.1\r\nHost: x\r\n\r\n",
    445: bytes.fromhex("00000054ff534d42720000000018012800000000000000"
                       "0000000000000000000000000000000000000000"
                       "0000000000000000"),                           # SMB1 negotiate
}
# 这些端口：服务端会先开口（不用我们发东西）
BANNER_PORTS = {22, 25, 110, 143, 21, 3306, 6379}
# 这些端口：用真 TLS 握手来判活
TLS_PORTS = {443, 8443, 5986}


def probe_target(first_hop, host, port, timeout=15):
    """★ 权威探活：CONNECT 成功不算数，必须拿到真协议回复。"""
    import ssl as _ssl
    port = int(port)
    t0 = time.time()
    try:
        s = open_via_first_hop(first_hop, host, port, timeout)
    except Exception as ex:
        return False, "连不上: %s" % ex
    ms = round((time.time() - t0) * 1000)

    # ---- TLS 端口：做一次真握手 ----
    if port in TLS_PORTS:
        try:
            s.settimeout(8)
            ctx = _ssl._create_unverified_context()
            ss = ctx.wrap_socket(s, server_hostname=host)
            ver = ss.version()
            ss.close()
            return True, "%dms, [TLS 握手成功] %s" % (ms, ver)
        except Exception as ex:
            try:
                s.close()
            except Exception:
                pass
            return False, "%dms, [TLS 握手失败] %s" % (ms, type(ex).__name__)

    try:
        s.settimeout(6)
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
            # 未知端口：先等 banner，再试 SOCKS5 问候，最后推一个换行
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
            return True, "%dms, [有真回应] %d 字节 (%s)" % (ms, len(d), d[:8].hex())
        return False, ("%dms, [无任何回应] 目标没起来或被防火墙挡了"
                       "（注意：CONNECT 的 200 是代理乐观应答，不算数）" % ms)
    finally:
        try:
            s.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="本地端口转发（可经第一跳）")
    ap.add_argument("--map", action="append", default=[], metavar="LPORT:HOST:PORT")
    ap.add_argument("--first-hop", default="127.0.0.1:7890")
    ap.add_argument("--probe", action="store_true", default=True)
    ap.add_argument("--no-probe", dest="probe", action="store_false")
    args = ap.parse_args()

    if not args.map:
        say("至少给一条 --map 本地端口:目标主机:目标端口")
        return 1

    first_hop = None
    if args.first_hop not in ("none", "-", ""):
        h, p = args.first_hop.rsplit(":", 1)
        first_hop = (h, int(p))

    maps = []
    for m in args.map:
        parts = m.split(":")
        if len(parts) != 3:
            say("映射格式不对: %s" % m)
            return 1
        maps.append((int(parts[0]), (parts[1], int(parts[2]))))

    say("=" * 66)
    say("本地端口转发")
    say("  第一跳 : %s" % (args.first_hop if first_hop else "无（直连，会暴露本机公网 IP）"))
    say("=" * 66)

    if args.probe:
        say("")
        say("目标端口探测：")
        for _, tgt in maps:
            ok, detail = probe_target(first_hop, tgt[0], tgt[1])
            say("  %-6s %s:%d  -> %s"
                % ("[通]" if ok else "[不通]", tgt[0], tgt[1], detail))
        say("")

    stop = threading.Event()
    say("已建立映射：")
    for lp, tgt in maps:
        threading.Thread(target=serve, args=(lp, tgt, first_hop, stop),
                         daemon=True).start()
    time.sleep(0.8)

    say("")
    say("用法示例：")
    for lp, tgt in maps:
        if tgt[1] == 3389:
            say("  远程桌面   mstsc /v:127.0.0.1:%d" % lp)
        elif tgt[1] == 445:
            say("  文件共享   \\\\127.0.0.1@%d\\c$" % lp)
        elif tgt[1] == 22:
            say("  SSH        ssh -p %d user@127.0.0.1" % lp)
        elif tgt[1] == 5985:
            say("  WinRM      winrs -r:http://127.0.0.1:%d -u:USER -p:PASS cmd" % lp)
    say("")
    say("运行中（Ctrl+C 退出）...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
        say("\n已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
