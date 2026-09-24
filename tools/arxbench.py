# -*- coding: utf-8 -*-
"""
arxbench.py —— 从服务器出发测 arxlabs 代理池的真实并发吞吐

方法：N 个并发 worker，每个做一次完整往返：
    SOCKS5 握手 → CONNECT 目标 → TLS → HTTP GET → 读响应 → 关闭
按不同并发档位跑，量出 请求/秒。

用法：
    python arxbench.py --proxies 代理.txt --levels 1,4,16,64,128 --per 32
"""
import argparse
import io
import socket
import ssl
import struct
import sys
import threading
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def say(*a):
    print(*a, flush=True)


def load_sids(path):
    """从 host:port:user:pass 每行一条的文件里抠出 (host, port, user, pwd)"""
    out = []
    with io.open(path, encoding="utf-8-sig", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            p = ln.split(":")
            if len(p) < 4:
                continue
            out.append((p[0], int(p[1]), ":".join(p[2:-1]), p[-1]))
    return out


def socks5_connect(sock, user, pwd, dst, dport, timeout):
    sock.settimeout(timeout)
    sock.sendall(b"\x05\x01\x02")
    r = sock.recv(2)
    if r != b"\x05\x02":
        raise IOError("socks greet %r" % r)
    u, p = user.encode(), pwd.encode()
    sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
    r = sock.recv(2)
    if r != b"\x01\x00":
        raise IOError("socks auth %r" % r)
    h = dst.encode()
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack(">H", dport))
    r = sock.recv(4)
    if len(r) < 2 or r[1] != 0:
        raise IOError("socks connect code=%r" % (r[1] if len(r) > 1 else None))
    atyp = r[3] if len(r) > 3 else 1
    if atyp == 1:
        sock.recv(6)
    elif atyp == 3:
        n = sock.recv(1)[0]
        sock.recv(n + 2)
    elif atyp == 4:
        sock.recv(18)
    return sock


def one(proxy, target, tls_host, path, timeout=25):
    """一次完整往返，返回 (成功?, 毫秒, 说明)"""
    t0 = time.time()
    s = None
    try:
        s = socket.create_connection((proxy[0], proxy[1]), timeout=timeout)
        socks5_connect(s, proxy[2], proxy[3], target[0], target[1], timeout)
        ctx = ssl._create_unverified_context()
        ss = ctx.wrap_socket(s, server_hostname=tls_host)
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: Mozilla/5.0\r\n"
               "Connection: close\r\n\r\n" % (path, tls_host))
        ss.sendall(req.encode())
        buf = b""
        ss.settimeout(timeout)
        while len(buf) < 2048:
            d = ss.recv(2048)
            if not d:
                break
            buf += d
        ss.close()
        ok = b"200" in buf[:20] or b"HTTP/1." in buf[:20]
        return ok, (time.time() - t0) * 1000, ""
    except Exception as e:
        return False, (time.time() - t0) * 1000, type(e).__name__
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxies", required=True)
    ap.add_argument("--levels", default="1,4,16,64,128")
    ap.add_argument("--per", type=int, default=32, help="每档总请求数")
    ap.add_argument("--host", default="passport.wanmei.com")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--path", default="/sso/servlet/ajax?op=mCaptchaInit&isAICap=1")
    args = ap.parse_args()

    proxies = load_sids(args.proxies)
    say("代理条目: %d" % len(proxies))
    if not proxies:
        return 1

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    say("")
    say("  并发   总数   成功   失败   墙钟(秒)   吞吐(req/s)   平均延迟(ms)   主要错误")
    say("  " + "-" * 84)

    for n in levels:
        per = max(args.per, n)
        results = []
        lock = threading.Lock()
        idx = [0]

        def worker():
            while True:
                with lock:
                    if idx[0] >= per:
                        return
                    my = idx[0]
                    idx[0] += 1
                px = proxies[my % len(proxies)]
                r = one(px, (args.host, args.port), args.host, args.path)
                with lock:
                    results.append(r)

        ths = [threading.Thread(target=worker) for _ in range(n)]
        t0 = time.time()
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        dt = time.time() - t0

        ok = sum(1 for r in results if r[0])
        bad = len(results) - ok
        lat = sorted(r[1] for r in results)
        med = lat[len(lat) // 2] if lat else 0
        errs = {}
        for r in results:
            if not r[0] and r[2]:
                errs[r[2]] = errs.get(r[2], 0) + 1
        top = max(errs.items(), key=lambda kv: kv[1])[0] if errs else ""
        say("  %-6d %-6d %-6d %-6d %-10.1f %-14.2f %-15.0f %s"
            % (n, per, ok, bad, dt, per / dt, med, top))

    say("")
    say("说明：这是「一次完整往返（SOCKS5+TLS+GET）」的吞吐。")
    say("      真实扫号一个账号要发 ~8.4 个请求（93% 登录失败 + 7% 扫 53 区服）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
