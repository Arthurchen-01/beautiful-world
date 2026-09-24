# -*- coding: utf-8 -*-
"""latbreak.py —— 延迟拆解：这 5 秒到底是谁造成的"""
import socket, ssl, struct, sys, time, io

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HOST = "passport.wanmei.com"
PORT = 443
PATH = "/sso/servlet/ajax?op=mCaptchaInit&isAICap=1"


def tcp_only(host, port, n=3):
    print("=== A) 直连目标站（不经代理）===")
    for i in range(n):
        try:
            t0 = time.time()
            s = socket.create_connection((host, port), timeout=20)
            t_tcp = (time.time() - t0) * 1000
            t1 = time.time()
            ctx = ssl._create_unverified_context()
            ss = ctx.wrap_socket(s, server_hostname=host)
            t_tls = (time.time() - t1) * 1000
            t2 = time.time()
            ss.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: Mozilla/5.0\r\nConnection: close\r\n\r\n"
                        % (PATH, host)).encode())
            buf = b""
            ss.settimeout(20)
            while len(buf) < 1024:
                d = ss.recv(1024)
                if not d:
                    break
                buf += d
            t_http = (time.time() - t2) * 1000
            ss.close()
            ok = b"200" in buf[:20]
            print("   #%d TCP=%5.0fms  TLS=%5.0fms  HTTP=%5.0fms  合计=%6.0fms  200=%s"
                  % (i + 1, t_tcp, t_tls, t_http, t_tcp + t_tls + t_http, ok))
        except Exception as e:
            print("   #%d 失败 %s: %s" % (i + 1, type(e).__name__, str(e)[:60]))


def via_proxy(px, n=3):
    print()
    print("=== B) 经 arxlabs 代理（拆开每一步）===")
    print("   sid: ...%s" % px[2][-14:])
    for i in range(n):
        s = None
        try:
            t0 = time.time()
            s = socket.create_connection((px[0], px[1]), timeout=25)
            t_conn = (time.time() - t0) * 1000

            t1 = time.time()
            s.settimeout(25)
            s.sendall(b"\x05\x01\x02")
            r = s.recv(2)
            u, p = px[2].encode(), px[3].encode()
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            r = s.recv(2)
            t_auth = (time.time() - t1) * 1000

            t2 = time.time()
            h = HOST.encode()
            s.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack(">H", PORT))
            r = s.recv(4)
            if len(r) < 2 or r[1] != 0:
                print("   #%d CONNECT 失败 code=%r" % (i + 1, r[1] if len(r) > 1 else None))
                continue
            atyp = r[3]
            if atyp == 1:
                s.recv(6)
            elif atyp == 3:
                nn = s.recv(1)[0]
                s.recv(nn + 2)
            t_connect = (time.time() - t2) * 1000

            t3 = time.time()
            ctx = ssl._create_unverified_context()
            ss = ctx.wrap_socket(s, server_hostname=HOST)
            t_tls = (time.time() - t3) * 1000

            t4 = time.time()
            ss.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: Mozilla/5.0\r\nConnection: close\r\n\r\n"
                        % (PATH, HOST)).encode())
            buf = b""
            ss.settimeout(25)
            while len(buf) < 1024:
                d = ss.recv(1024)
                if not d:
                    break
                buf += d
            t_http = (time.time() - t4) * 1000
            ss.close()
            ok = b"200" in buf[:20]
            print("   #%d TCP=%5.0fms  认证=%5.0fms  CONNECT=%5.0fms  TLS=%5.0fms  HTTP=%5.0fms  合计=%6.0fms  200=%s"
                  % (i + 1, t_conn, t_auth, t_connect, t_tls, t_http,
                     t_conn + t_auth + t_connect + t_tls + t_http, ok))
        except Exception as e:
            print("   #%d 失败 %s: %s" % (i + 1, type(e).__name__, str(e)[:60]))
        finally:
            try:
                if s:
                    s.close()
            except Exception:
                pass


def main():
    proxies = []
    with io.open(r"C:\WMRoleScan\proxies.txt", encoding="utf-8-sig", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            p = ln.split(":")
            if len(p) >= 4:
                proxies.append((p[0], int(p[1]), ":".join(p[2:-1]), p[-1]))
    print("代理条目: %d" % len(proxies))
    print()
    tcp_only(HOST, PORT)
    via_proxy(proxies[0])
    print()
    via_proxy(proxies[50])


if __name__ == "__main__":
    main()
