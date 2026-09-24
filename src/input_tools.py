# -*- coding: utf-8 -*-
"""
input_tools.py —— 输入体检 + 本地查重 + 代理存活检测
================================================================
用户诉求：
  · 账号/代理加进来之前先体检一次，告诉你「一共多少条、多少条能用」
  · 跟**本地历史**查重 —— 跑过的账号不要再跑
  · 代理要能测存活
  · 一键处理

本地落盘（第一次运行就建好，之后一直累积）：
    %LOCALAPPDATA%\\WMRoleScan\\已跑过账号.txt      跑过的账号（小写归一化）
    %LOCALAPPDATA%\\WMRoleScan\\代理历史.txt        用过的代理
    %LOCALAPPDATA%\\WMRoleScan\\处理报告.txt        每次体检的摘要
"""
import os
import re
import threading
import time
from collections import OrderedDict

import app_config

WORK = app_config.WORK_DIR
SEEN_ACCOUNTS = os.path.join(WORK, "已跑过账号.txt")
SEEN_PROXIES = os.path.join(WORK, "代理历史.txt")
REPORT = os.path.join(WORK, "处理报告.txt")

_lock = threading.Lock()


# ================================================================ 归一化
def norm_account(a):
    return (a or "").strip().lower()


def norm_proxy(host, port, user):
    """同 host:port:用户名 = 同一条代理（sid 不同也算不同，因为出口 IP 不同）"""
    return f"{(host or '').strip().lower()}:{(port or '').strip()}:{(user or '').strip()}"


def parse_account_line(line):
    """返回 (账号, 密码, 备注) 或 None"""
    line = line.lstrip("\ufeff").strip()
    if not line or line.startswith("#"):
        return None
    if "----" in line:
        seg = line.split("----")
        a = seg[0].strip()
        p = seg[1].strip() if len(seg) > 1 else ""
        note = "----".join(seg[2:]).strip() if len(seg) > 2 else ""
        return (a, p, note) if a else None
    if "," in line:
        a, _, p = line.partition(",")
        return (a.strip(), p.strip(), "") if a.strip() else None
    return None


def parse_proxy_line(line):
    """host:port:user:pass -> (host, port, user, pwd) 或 None"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(":")
    if len(parts) < 4:
        return None
    host, port = parts[0].strip(), parts[1].strip()
    user = ":".join(parts[2:-1]).strip()
    pwd = parts[-1].strip()
    if not host or not port.isdigit():
        return None
    return (host, port, user, pwd)


def split_lines(text):
    return [l for l in (text or "").splitlines()
            if l.strip() and not l.strip().startswith("#")]


# ================================================================ 本地历史
def load_seen(path):
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            return {l.strip() for l in f if l.strip()}
    except Exception:
        return set()


def append_seen(path, keys):
    """幂等追加（不重复写）"""
    if not keys:
        return 0
    with _lock:
        have = load_seen(path)
        new = [k for k in keys if k and k not in have]
        if not new:
            return 0
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for k in new:
                f.write(k + "\n")
        return len(new)


def seen_stats():
    return {"accounts": len(load_seen(SEEN_ACCOUNTS)),
            "proxies": len(load_seen(SEEN_PROXIES)),
            "account_file": SEEN_ACCOUNTS,
            "proxy_file": SEEN_PROXIES}


def write_report(title, lines):
    try:
        with open(REPORT, "a", encoding="utf-8") as f:
            f.write(f"\n===== {title}  {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            f.write("\n".join(str(x) for x in lines) + "\n")
    except Exception:
        pass


# ================================================================ 账号体检
def analyze_accounts(text, use_local_dedupe=True):
    """返回体检报告 dict"""
    raw = split_lines(text)
    valid, invalid = [], []
    for l in raw:
        p = parse_account_line(l)
        if p and p[0] and p[1]:
            valid.append(p)
        else:
            invalid.append(l)

    # 输入内去重（按账号归一化，保留第一次出现）
    uniq = OrderedDict()
    dup_in_input = 0
    for a, pw, note in valid:
        k = norm_account(a)
        if k in uniq:
            dup_in_input += 1
            # 密码不一致要提示（可能是两个不同密码的同名账号）
            if uniq[k][1] != pw:
                uniq[k] = (a, pw, note, True)
            continue
        uniq[k] = (a, pw, note, False)

    local = load_seen(SEEN_ACCOUNTS) if use_local_dedupe else set()
    already = [k for k in uniq if k in local]
    fresh = [k for k in uniq if k not in local]

    pwd_conflict = [v[0] for v in uniq.values() if v[3]]

    return {
        "raw_lines": len(raw),
        "valid": len(valid),
        "invalid": len(invalid),
        "invalid_samples": invalid[:8],
        "dup_in_input": dup_in_input,
        "unique": len(uniq),
        "already_run": len(already),
        "already_samples": already[:8],
        "fresh": len(fresh),
        "pwd_conflict": pwd_conflict[:8],
        "local_total": len(local),
        "items": list(uniq.values()),          # (账号, 密码, 备注, 密码冲突)
        "fresh_keys": fresh,
    }


def build_accounts_text(items, skip_seen=True):
    """按体检结果生成要保存的文本；同时把跑过的记进本地历史"""
    local = load_seen(SEEN_ACCOUNTS) if skip_seen else set()
    out, keys = [], []
    for a, pw, note, _conf in items:
        k = norm_account(a)
        if skip_seen and k in local:
            continue
        line = f"{a}----{pw}"
        if note:
            line += "----" + note
        out.append(line)
        keys.append(k)
    return "\n".join(out) + ("\n" if out else ""), keys


# ================================================================ 代理体检
def analyze_proxies(text, check_alive=False, workers=10, timeout=20,
                    progress_cb=None, stop_flag=None):
    """返回体检报告 dict；check_alive=True 时并发测存活"""
    raw = split_lines(text)
    valid, invalid = [], []
    for l in raw:
        p = parse_proxy_line(l)
        (valid if p else invalid).append(p or l)

    uniq = OrderedDict()
    dup_in_input = 0
    for host, port, user, pwd in valid:
        k = norm_proxy(host, port, user)
        if k in uniq:
            dup_in_input += 1
            continue
        uniq[k] = (host, port, user, pwd)

    local = load_seen(SEEN_PROXIES)
    already = [k for k in uniq if k in local]

    rep = {
        "raw_lines": len(raw),
        "valid": len(valid),
        "invalid": len(invalid),
        "invalid_samples": [str(x)[:60] for x in invalid[:8]],
        "dup_in_input": dup_in_input,
        "unique": len(uniq),
        "already_used": len(already),
        "local_total": len(local),
        "items": list(uniq.values()),          # (host, port, user, pwd)
        "keys": list(uniq.keys()),
        "alive": None, "dead": None, "alive_items": None,
    }
    if not check_alive:
        return rep

    # ---- 并发存活检测 ----
    import socket
    alive, dead = [], []
    total = len(uniq)
    done = [0]
    dlock = threading.Lock()

    def probe(item):
        if stop_flag is not None and stop_flag.is_set():
            return
        host, port, user, pwd = item
        t0 = time.time()
        ok = False
        try:
            s = socket.create_connection((host, int(port)), timeout=timeout)
            # 真做一次 SOCKS5 握手 + 认证，不能只看 TCP 通
            s.settimeout(timeout)
            s.sendall(b"\x05\x02\x00\x02")
            r = s.recv(16)
            if len(r) >= 2 and r[0] == 5 and r[1] == 2:
                u, p = user.encode(), pwd.encode()
                s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
                r2 = s.recv(16)
                ok = len(r2) >= 2 and r2[1] == 0
            elif len(r) >= 2 and r[0] == 5 and r[1] == 0:
                ok = True                      # 不需要认证
            s.close()
        except Exception:
            ok = False
        ms = int((time.time() - t0) * 1000)
        with dlock:
            (alive if ok else dead).append((item, ms))
            done[0] += 1
            if progress_cb:
                try:
                    progress_cb(done[0], total)
                except Exception:
                    pass

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(probe, list(uniq.values())))

    rep["alive"] = len(alive)
    rep["dead"] = len(dead)
    rep["alive_items"] = [a[0] for a in sorted(alive, key=lambda x: x[1])]
    rep["alive_detail"] = [(a[0][0], a[0][1], a[0][2][-8:] if len(a[0][2]) > 8
                            else a[0][2], a[1]) for a in sorted(alive, key=lambda x: x[1])]
    rep["dead_detail"] = [(a[0][0], a[0][1]) for a in dead]
    return rep


def build_proxies_text(items, alive_items=None, mark_used=True):
    """生成要保存的代理文本；同时记进本地历史"""
    if alive_items is not None:
        items = alive_items
    lines, keys = [], []
    for host, port, user, pwd in items:
        lines.append(f"{host}:{port}:{user}:{pwd}")
        keys.append(norm_proxy(host, port, user))
    if mark_used:
        append_seen(SEEN_PROXIES, keys)
    return "\n".join(lines) + ("\n" if lines else ""), keys


if __name__ == "__main__":
    print("本地历史:")
    print("  已跑过账号文件:", SEEN_ACCOUNTS)
    print("  代理历史文件  :", SEEN_PROXIES)
    s = seen_stats()
    print(f"  已记录账号 {s['accounts']} 个，代理 {s['proxies']} 条")
    demo = "<ACCOUNT>----<PASSWORD>\<ACCOUNT>----<PASSWORD>\<ACCOUNT>----<PASSWORD>\n坏的没有分隔符\n"
    r = analyze_accounts(demo)
    print("\n账号体检 demo:")
    for k, v in r.items():
        if k not in ("items", "fresh_keys"):
            print(f"  {k} = {v}")
