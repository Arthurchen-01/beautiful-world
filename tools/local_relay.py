# -*- coding: utf-8 -*-
"""
local_relay.py —— 绕过本机 TUN 的本地端口中转
================================================================
解决什么问题
----------------------------------------------------------------
本机挂着 fake-ip 模式的 VPN（iKuuu / Clash 那类），**默认路由走 TUN**。
于是：

  · PowerShell 的 WinRM 客户端、mstsc 这些程序，**只会走默认路由**
  · 而 TUN 对非 80/443 端口是"本地假握手 + 黑洞"
  · 结果就是：明明用 Python 绑物理网卡能连上目标，但工具就是连不上

加路由需要管理员权限。这个中转不用 —— 它自己**把源地址绑到物理网卡**，
在本机开一个 127.0.0.1 的端口，任何程序连这个本地端口就等于从物理网卡出去。

    mstsc /v:127.0.0.1:13389        →  物理网卡  →  目标:3389
    WinRM http://127.0.0.1:15985    →  物理网卡  →  目标:5985

⚠️ 注意：这条路的**源 IP 是本机公网 IP**（会暴露）。只用于连你自己的机器，
   不要用来连目标站。

用法
----------------------------------------------------------------
    # 自动挑物理网卡，映射 RDP + WinRM
    python tools\\local_relay.py --target 1.2.3.4 --map 13389:3389 --map 15985:5985

    # 指定绑哪个网卡地址
    python tools\\local_relay.py --target 1.2.3.4 --bind 192.168.0.101 --map 13389:3389

    # 看看会绑哪个地址（不启动）
    python tools\\local_relay.py --target 1.2.3.4 --dry-run
"""
import argparse
import select
import socket
import sys
import threading
import time

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


def _is_fake_ip(ip):
    """198.18.0.0/15 = TUN 的 fake-ip 网段，绝对不能绑它"""
    try:
        a, b = (int(x) for x in ip.split(".")[:2])
        return a == 198 and 18 <= b <= 19
    except Exception:
        return False


def pick_phys_ip():
    """挑一个真实物理网卡地址（排除 TUN 的 fake-ip 和回环）"""
    cands = []
    try:
        cands += socket.gethostbyname_ex(socket.gethostname())[2]
    except Exception:
        pass
    for tgt in (("192.168.0.1", 80), ("223.5.5.5", 80), ("114.114.114.114", 80)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(tgt)
            cands.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    for ip in cands:
        if not ip.startswith("127.") and not _is_fake_ip(ip):
            return ip
    return None


def pump(a, b):
    """双向转发。

    ★ 两个 socket 必须都设成**阻塞、无超时**，只靠 select 控制。
      早期版本 connect 后 socket 还留着 15 秒超时 —— 大文件传输时只要
      有 15 秒没数据就抛 socket.timeout，pump 直接退出把连接掐了。
      实测后果：8 路并行传 84MB 时全部报 HTTP 12152（服务器返回无效响应）。
    """
    for s in (a, b):
        try:
            s.settimeout(None)
        except Exception:
            pass
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 600)
            if not r:
                continue
            for s in r:
                d = s.recv(262144)
                if not d:
                    return
                (b if s is a else a).sendall(d)
    except Exception:
        pass


def handle(conn, target, bind_ip):
    up = None
    try:
        up = socket.socket()
        up.settimeout(15)
        if bind_ip:
            up.bind((bind_ip, 0))
        up.connect(target)
        up.settimeout(None)          # ★ 连上后取消超时，交给 pump 用 select 管
        conn.settimeout(None)
        pump(conn, up)
    except Exception:
        pass
    finally:
        for s in (conn, up):
            try:
                if s:
                    s.close()
            except Exception:
                pass


def serve(lport, target, bind_ip, stop):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", lport))
    srv.listen(64)
    srv.settimeout(1.0)
    say("  [OK] 127.0.0.1:%-6d -> %s:%d   (源地址 %s)"
        % (lport, target[0], target[1], bind_ip))
    while not stop.is_set():
        try:
            c, _ = srv.accept()
        except socket.timeout:
            continue
        except Exception:
            break
        threading.Thread(target=handle, args=(c, target, bind_ip), daemon=True).start()
    try:
        srv.close()
    except Exception:
        pass


def verify(target, bind_ip, timeout=8):
    """确认这条路真的通（不是 TUN 假握手）"""
    try:
        s = socket.socket()
        s.settimeout(timeout)
        s.bind((bind_ip, 0))
        t0 = time.time()
        s.connect(target)
        ms = round((time.time() - t0) * 1000)
        s.close()
        return True, ms
    except Exception as e:
        return False, type(e).__name__


def main():
    ap = argparse.ArgumentParser(description="绕过本机 TUN 的本地端口中转")
    ap.add_argument("--target", required=True, help="目标主机")
    ap.add_argument("--map", action="append", default=[], metavar="LPORT:RPORT")
    ap.add_argument("--bind", default="", help="绑定哪个本机地址（默认自动挑物理网卡）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.map:
        ap.error("至少给一条 --map 本地端口:目标端口")

    bind_ip = args.bind.strip() or pick_phys_ip()
    if not bind_ip or _is_fake_ip(bind_ip):
        say("⛔ 找不到物理网卡地址（本机默认路由走 TUN）")
        say("   用 --bind 手工指定，例如 --bind 192.168.0.101")
        return 2

    maps = []
    for m in args.map:
        lp, _, rp = m.partition(":")
        maps.append((int(lp), int(rp)))

    say("=" * 64)
    say("本地端口中转（绕过 TUN）")
    say("  目标      : %s" % args.target)
    say("  绑定源地址: %s   （= 本机公网 IP 会暴露，只连自己的机器）" % bind_ip)
    say("=" * 64)
    say("")
    say("目标端口可达性（绑物理网卡实测）：")
    ok_any = False
    for _, rp in maps:
        ok, info = verify((args.target, rp), bind_ip)
        say("  %s :%-6d %s" % ("[通]" if ok else "[不通]", rp,
                               ("%dms" % info) if ok else info))
        ok_any = ok_any or ok

    if args.dry_run:
        say("")
        say("--dry-run：不启动中转。")
        return 0 if ok_any else 1

    say("")
    say("已建立映射：")
    stop = threading.Event()
    for lp, rp in maps:
        threading.Thread(target=serve, args=(lp, (args.target, rp), bind_ip, stop),
                         daemon=True).start()
    time.sleep(0.8)
    say("")
    say("用法：")
    for lp, rp in maps:
        if rp == 3389:
            say("  远程桌面   mstsc /v:127.0.0.1:%d" % lp)
        elif rp == 5985:
            say("  WinRM      New-PSSession -ConnectionURI http://127.0.0.1:%d/wsman ..." % lp)
        elif rp == 445:
            say("  文件共享   \\\\127.0.0.1@%d\\c$" % lp)
        elif rp == 22:
            say("  SSH        ssh -p %d user@127.0.0.1" % lp)
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
