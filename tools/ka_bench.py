# -*- coding: utf-8 -*-
"""
ka_bench.py —— 连接复用（keep-alive）能带来多少提升

对比：
  A) 每次请求都新建连接（现状）—— 每请求要重做 TCP+SOCKS5认证+CONNECT+TLS ≈ 4 秒
  B) 一条连接发多个请求（keep-alive）—— 每请求只花 HTTP 往返 ≈ 1 秒

真实扫号一个账号要发 ~53 个请求（扫 53 个区服）。如果 B 成立，
单账号耗时能从 ~265 秒降到 ~53 秒。
"""
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

HOST = "passport.wanmei.com"
PORT = 443
PATH = "/sso/servlet/ajax?op=mCaptchaInit&isAICap=1"


def load(path):
    out = []
    with io.open(path, encoding="utf-8-sig", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            p = ln.split(":")
            if len(p) >= 4:
                out.append((p[0], int(p[1]), ":".join(p[2:-1]), p[-1]))
    return out


def socks5(s, px, timeout=25):
    s.settimeout(timeout)
    s.sendall(b"\x05\x01\x02")
    if s.recv(2) != b"\x05\x02":
        raise IOError("greet")
    u, p = px[2].encode(), px[3].encode()
    s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
    if s.recv(2) != b"\x01\x00":
        raise IOError("auth")
    h = HOST.encode()
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack(">H", PORT))
    r = s.recv(4)
    if len(r) < 2 or r[1] != 0:
        raise IOError("connect")
    atyp = r[3]
    if atyp == 1:
        s.recv(6)
    elif atyp == 3:
        n = s.recv(1)[0]
        s.recv(n + 2)
    return s


def fresh_conn(px):
    s = socket.create_connection((px[0], px[1]), timeout=25)
    socks5(s, px)
    return ssl._create_unverified_context().wrap_socket(s, server_hostname=HOST)


def one_req(ss, timeout=25):
    ss.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: Mozilla/5.0\r\n"
                "Accept: */*\r\nConnection: keep-alive\r\n\r\n" % (PATH, HOST)).encode())
    ss.settimeout(timeout)
    buf = b""
    # 读到 header 结束 + 一点 body
    while b"\r\n\r\n" not in buf:
        d = ss.recv(4096)
        if not d:
            raise IOError("closed")
        buf += d
    head, _, rest = buf.partition(b"\r\n\r\n")
    # 解析 Content-Length 把 body 读干净
    clen = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            clen = int(line.split(b":")[1].strip())
    body = rest
    while len(body) < clen:
        d = ss.recv(4096)
        if not d:
            break
        body += d
    return b"200" in head[:20], body


def bench_fresh(proxies, n_conc, per_conc):
    """每次请求都新建连接"""
    res = []
    lock = threading.Lock()
    idx = [0]

    def w():
        while True:
            with lock:
                if idx[0] >= n_conc:
                    return
                my = idx[0]
                idx[0] += 1
            px = proxies[my % len(proxies)]
            t0 = time.time()
            try:
                ss = fresh_conn(px)
                one_req(ss)
                ss.close()
                with lock:
                    res.append(time.time() - t0)
            except Exception:
                with lock:
                    res.append(None)

    ths = [threading.Thread(target=w) for _ in range(n_conc)]
    t0 = time.time()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    dt = time.time() - t0
    ok = sum(1 for r in res if r is not None)
    return ok, n_conc, dt


def bench_ka(proxies, n_conc, per_conn):
    """每条连接发 per_conn 个请求（keep-alive）"""
    res = []
    lock = threading.Lock()
    idx = [0]

    def w():
        while True:
            with lock:
                if idx[0] >= n_conc:
                    return
                my = idx[0]
                idx[0] += 1
            px = proxies[my % len(proxies)]
            t0 = time.time()
            okn = 0
            ss = None
            try:
                ss = fresh_conn(px)
                for _ in range(per_conn):
                    ok, _b = one_req(ss)
                    if ok:
                        okn += 1
                ss.close()
            except Exception:
                try:
                    if ss:
                        ss.close()
                except Exception:
                    pass
            with lock:
                res.append((okn, time.time() - t0))

    ths = [threading.Thread(target=w) for _ in range(n_conc)]
    t0 = time.time()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    dt = time.time() - t0
    total_ok = sum(r[0] for r in res)
    return total_ok, n_conc * per_conn, dt


def main():
    proxies = load(r"C:\WMRoleScan\proxies.txt")
    print("代理条目: %d" % len(proxies))
    print()

    print("=== A) 现状：每次请求新建连接 ===")
    print("  并发   成功/总数   墙钟(秒)   吞吐(req/s)   单请求(ms)")
    print("  " + "-" * 60)
    for n in (32, 128):
        ok, tot, dt = bench_fresh(proxies, n, 1)
        print("  %-6d %-11s %-10.1f %-14.2f %-12.0f"
              % (n, "%d/%d" % (ok, tot), dt, tot / dt, dt / max(tot, 1) * 1000))

    print()
    print("=== B) keep-alive：一条连接发多个请求 ===")
    print("  并发   每连接请求数   成功/总数   墙钟(秒)   吞吐(req/s)   单请求(ms)")
    print("  " + "-" * 78)
    for n, per in ((32, 8), (128, 8), (128, 16), (256, 8)):
        ok, tot, dt = bench_ka(proxies, n, per)
        print("  %-6d %-14d %-11s %-10.1f %-14.2f %-12.0f"
              % (n, per, "%d/%d" % (ok, tot), dt, ok / dt, dt / max(ok, 1) * 1000))


if __name__ == "__main__":
    main()
