# -*- coding: utf-8 -*-
"""
chain_proxy.py —— 代理池「第一跳」桥接器
================================================================
解决的问题
----------------------------------------------------------------
arxlabs 代理池（SOCKS5）**拒绝中国大陆来源 IP**：

    直连 198.44.166.3:3010
    → HTTP/1.1 403 Forbidden
      msg: forbidden ip=<本机公网IP> not supported

但经一个境外第一跳转发过去就正常。实测可用链路：

    本机 → 127.0.0.1:7890 (iKuuu VPN，香港出口) → arxlabs SOCKS5 → 目标
    出口 = 日本住宅 IP（KDDI / NTT / SoftBank），昆明 IP 全程不出现

本工具把这条链**封装成普通的本地 SOCKS5/HTTP 代理**，每条 sid 一个本地端口：

    127.0.0.1:20001  →  sid-XXXX  →  日本住宅 IP #1
    127.0.0.1:20002  →  sid-YYYY  →  日本住宅 IP #2
    ...

于是 proxy_pool.py 完全不用改，只要把 proxies.txt 换成：

    127.0.0.1:20001::
    127.0.0.1:20002::
    ...

用法
----------------------------------------------------------------
    # 1) 启动桥接（前台，看体检结果）
    python chain_proxy.py --proxies proxies.txt --first-hop 127.0.0.1:7890

    # 2) 它会写出 proxies_local.txt，直接喂给主程序
    python wm_scan.py accounts.txt --proxy-file proxies_local.txt

    # 3) 不想用第一跳（本机 IP 已被代理商放行）时
    python chain_proxy.py --proxies proxies.txt --first-hop none

参数
----------------------------------------------------------------
    --proxies    代理列表文件，格式 host:port:user:pass（每行一条）
    --first-hop  第一跳 HTTP 代理，默认 127.0.0.1:7890（iKuuu）
                 写 none / - 表示不走第一跳，直连目标代理
    --base-port  本地端口起始值，默认 20001
    --out        输出的本地代理文件，默认 proxies_local.txt
    --check      启动后逐条体检（拿出口 IP / 归属地），默认开启
    --no-check   跳过体检，秒起
    --only N     只桥接前 N 条（调试用）

★ 红线
----------------------------------------------------------------
    · 第一跳必须是境外出口（iKuuu 香港 / 其他），否则代理商会拒
    · 体检里只要出现 CN 出口，本工具会把它标红并**不写进输出文件**
    · 本机公网 IP 永远不直接接触目标站
"""
import argparse
import json
import os
import socket
import struct
import sys
import threading
import time

# ★ 控制台是 GBK 时，非 GBK 字符会让 print 抛 UnicodeEncodeError；
#   而 say() 会吞掉异常 —— 于是"整行日志凭空消失"。统一改成 UTF-8 + replace。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROXIES = os.path.join(os.path.dirname(HERE), "proxies.txt")
DEFAULT_OUT = os.path.join(os.path.dirname(HERE), "proxies_local.txt")

BUFSIZE = 65536


def say(*a):
    try:
        print(*a, flush=True)
    except Exception:
        pass


# ------------------------------------------------------------------ 基础 IO
def recv_exact(sock, n, timeout=30):
    sock.settimeout(timeout)
    buf = b""
    while len(buf) < n:
        d = sock.recv(n - len(buf))
        if not d:
            raise ConnectionError("peer closed")
        buf += d
    return buf


def pump(a, b):
    """双向转发直到任一端关闭"""
    try:
        while True:
            r, _, _ = select_select([a, b], [], [], 120)
            if not r:
                break
            for s in r:
                d = s.recv(BUFSIZE)
                if not d:
                    return
                (b if s is a else a).sendall(d)
    except Exception:
        pass


def select_select(rlist, wlist, xlist, timeout):
    import select
    return select.select(rlist, wlist, xlist, timeout)


# ------------------------------------------------------------------ 第一跳
def open_via_first_hop(first_hop, host, port, timeout=25):
    """经第一跳 HTTP 代理 CONNECT 到 host:port，返回已连通的 socket"""
    if not first_hop:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.settimeout(timeout)
        return s
    fh_host, fh_port = first_hop
    s = socket.create_connection((fh_host, int(fh_port)), timeout=timeout)
    s.settimeout(timeout)
    req = ("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n"
           % (host, int(port), host, int(port)))
    s.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = s.recv(4096)
        if not d:
            raise ConnectionError("first hop closed during CONNECT")
        buf += d
    line = buf.split(b"\r\n", 1)[0].decode("latin1", "replace")
    if " 200" not in line:
        raise ConnectionError("first hop refused: %s" % line.strip())
    return s


# ------------------------------------------------------------------ SOCKS5 客户端
def socks5_connect(sock, user, pwd, dst, dport, timeout=25):
    """在已建立的 socket 上完成 SOCKS5 + 用户名密码认证 + CONNECT"""
    sock.settimeout(timeout)
    sock.sendall(b"\x05\x01\x02")                    # 版本5，1种方法：用户名密码
    r = recv_exact(sock, 2, timeout)
    if r[0] != 5:
        raise ConnectionError("不是 SOCKS5 服务（首字节 0x%02x）" % r[0])
    if r[1] == 0xFF:
        raise ConnectionError("SOCKS5 拒绝所有认证方式")
    if r[1] == 2:
        u = user.encode()
        p = pwd.encode()
        sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
        r = recv_exact(sock, 2, timeout)
        if r[1] != 0:
            raise ConnectionError("SOCKS5 认证失败")
    # CONNECT，用域名方式（远端解析）
    h = dst.encode() if isinstance(dst, str) else dst
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack(">H", int(dport)))
    r = recv_exact(sock, 4, timeout)
    if r[1] != 0:
        names = {1: "general failure", 2: "not allowed", 3: "network unreachable",
                 4: "host unreachable", 5: "connection refused", 6: "TTL expired",
                 7: "command not supported", 8: "address type not supported"}
        raise ConnectionError("SOCKS5 CONNECT 失败: %s" % names.get(r[1], r[1]))
    atyp = r[3]
    if atyp == 1:
        recv_exact(sock, 4 + 2, timeout)
    elif atyp == 3:
        n = recv_exact(sock, 1, timeout)[0]
        recv_exact(sock, n + 2, timeout)
    elif atyp == 4:
        recv_exact(sock, 16 + 2, timeout)
    return sock


# ------------------------------------------------------------------ 本地 SOCKS5 服务端
def handle_client(conn, entry, first_hop):
    """本地客户端 → 第一跳 → 上游 SOCKS5 → 目标"""
    try:
        conn.settimeout(30)
        head = recv_exact(conn, 2, 30)
        if head[0] != 5:
            conn.close()
            return
        nmethods = head[1]
        recv_exact(conn, nmethods, 30)
        conn.sendall(b"\x05\x00")                     # 本地不做认证
        req = recv_exact(conn, 4, 30)
        if req[1] != 1:                               # 只支持 CONNECT
            conn.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            conn.close()
            return
        atyp = req[3]
        if atyp == 1:
            dst = socket.inet_ntoa(recv_exact(conn, 4, 30))
        elif atyp == 3:
            n = recv_exact(conn, 1, 30)[0]
            dst = recv_exact(conn, n, 30).decode("latin1")
        elif atyp == 4:
            dst = socket.inet_ntop(socket.AF_INET6, recv_exact(conn, 16, 30))
        else:
            conn.close()
            return
        dport = struct.unpack(">H", recv_exact(conn, 2, 30))[0]

        try:
            up = open_via_first_hop(first_hop, entry["host"], entry["port"])
            socks5_connect(up, entry["user"], entry["pwd"], dst, dport)
        except Exception as ex:
            say("  [!] %s -> %s:%d 失败: %s" % (entry["name"], dst, dport, ex))
            conn.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
            conn.close()
            return

        conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        pump(conn, up)
        try:
            up.close()
        except Exception:
            pass
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def serve_local(port, entry, first_hop, stop_evt):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(64)
    srv.settimeout(1.0)
    while not stop_evt.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        except Exception:
            break
        threading.Thread(target=handle_client, args=(conn, entry, first_hop),
                         daemon=True).start()
    try:
        srv.close()
    except Exception:
        pass


# ------------------------------------------------------------------ 解析 / 体检
def parse_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(":")
    if len(parts) < 4:
        return None
    host, port = parts[0], parts[1]
    user = ":".join(parts[2:-1])
    pwd = parts[-1]
    import re
    m = re.search(r"sid-([A-Za-z0-9]+)", user)
    name = m.group(1) if m else user[-8:]
    return {"name": name, "host": host, "port": port, "user": user, "pwd": pwd,
            "local_port": None, "ip": None, "geo": None, "ok": None, "ms": None}


def probe(local_port, timeout=25):
    """经本地端口查出口 IP + 归属地"""
    t0 = time.time()
    s = socket.create_connection(("127.0.0.1", local_port), timeout=timeout)
    socks5_connect(s, "", "", "ipinfo.io", 80, timeout)
    s.sendall(b"GET /json HTTP/1.1\r\nHost: ipinfo.io\r\n"
              b"User-Agent: Mozilla/5.0\r\nConnection: close\r\n\r\n")
    buf = b""
    s.settimeout(timeout)
    try:
        while True:
            d = s.recv(4096)
            if not d:
                break
            buf += d
    except Exception:
        pass
    s.close()
    body = buf.decode("latin1", "replace").split("\r\n\r\n")[-1]
    d = json.loads(body)
    return {"ip": d.get("ip"), "cc": d.get("country"),
            "geo": "%s/%s" % (d.get("city"), d.get("org", "")[:28]),
            "ms": round((time.time() - t0) * 1000)}


# ------------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser(description="代理池第一跳桥接器")
    ap.add_argument("--proxies", default=DEFAULT_PROXIES)
    ap.add_argument("--first-hop", default="127.0.0.1:7890")
    ap.add_argument("--base-port", type=int, default=20001)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--check", dest="check", action="store_true", default=True)
    ap.add_argument("--no-check", dest="check", action="store_false")
    ap.add_argument("--only", type=int, default=0)
    args = ap.parse_args()

    if args.first_hop in ("none", "-", ""):
        first_hop = None
    else:
        fh = args.first_hop.rsplit(":", 1)
        first_hop = (fh[0], int(fh[1]))

    entries = []
    with open(args.proxies, "r", encoding="utf-8-sig", errors="replace") as f:
        for ln in f:
            e = parse_line(ln)
            if e:
                entries.append(e)
    if args.only:
        entries = entries[:args.only]
    if not entries:
        say("没有解析到任何代理，检查文件: %s" % args.proxies)
        return 1

    say("=" * 66)
    say("代理池第一跳桥接器")
    say("  代理文件   : %s  (%d 条)" % (args.proxies, len(entries)))
    say("  第一跳     : %s" % (args.first_hop if first_hop else "无（直连上游代理）"))
    say("  本地端口   : 127.0.0.1:%d ..." % args.base_port)
    say("=" * 66)

    stop = threading.Event()
    for i, e in enumerate(entries):
        e["local_port"] = args.base_port + i
        threading.Thread(target=serve_local,
                         args=(e["local_port"], e, first_hop, stop),
                         daemon=True).start()
    time.sleep(0.6)

    good = []
    if args.check:
        from concurrent.futures import ThreadPoolExecutor

        def one(e):
            try:
                r = probe(e["local_port"])
                e.update(ok=True, ip=r["ip"], geo=r["geo"], ms=r["ms"], cc=r["cc"])
            except Exception as ex:
                e.update(ok=False, geo=type(ex).__name__)
            return e

        with ThreadPoolExecutor(max_workers=12) as ex:
            list(ex.map(one, entries))

        ips = {e["ip"] for e in entries if e.get("ok")}
        cn = [e for e in entries if e.get("ok") and (e.get("cc") or "").upper() == "CN"]
        good = [e for e in entries if e.get("ok") and (e.get("cc") or "").upper() != "CN"]
        say("")
        say("体检结果：可用 %d/%d   唯一出口 IP %d   国内出口 %d"
            % (len([e for e in entries if e.get("ok")]), len(entries), len(ips), len(cn)))
        say("")
        say("  %-6s %-10s %-16s %-26s %s" % ("端口", "sid", "出口IP", "归属", "延迟"))
        say("  " + "-" * 74)
        for e in entries:
            if e.get("ok"):
                say("  %-6d %-10s %-16s %-26s %dms"
                    % (e["local_port"], e["name"], e["ip"], e["geo"] or "", e["ms"]))
            else:
                say("  %-6d %-10s %-16s %-26s %s"
                    % (e["local_port"], e["name"], "-", "-", "不可用 " + str(e.get("geo"))))
        if cn:
            say("")
            say("  ★ 红线：以下 %d 条出口在国内，已剔除，不写入输出文件：" % len(cn))
            for e in cn:
                say("     %s  %s" % (e["name"], e["ip"]))
    else:
        good = entries
        say("")
        say("已跳过体检。全部 %d 条端口已就绪。" % len(entries))

    if good:
        with open(args.out, "w", encoding="utf-8") as f:
            for e in good:
                f.write("127.0.0.1:%d::\n" % e["local_port"])
        say("")
        say("★ 已写出 %d 条本地代理 -> %s" % (len(good), args.out))
        say("  用法: python wm_scan.py accounts.txt --proxy-file %s" % args.out)
    else:
        say("")
        say("★ 没有可用代理，未写出输出文件。")

    say("")
    say("桥接器运行中（Ctrl+C 退出）...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
        say("\n已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
