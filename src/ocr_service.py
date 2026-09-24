# -*- coding: utf-8 -*-
"""
ocr_service.py —— 本地点选验证码识别服务的控制与调用封装
================================================================
组件：  <诛仙>\识别端\图像识别POST服务.exe   监听 127.0.0.1:506
        <诛仙>\识别端\OCR.dll  (54MB 模型) + XYLib.dll + HPSocket4C.dll

关键前提（2026-09-23 确认）：
  · XYLib.dll 需要 32 位 VCOMP140.DLL，系统只装了 x64 版 → 必须把
    32 位 vcomp140.dll 放进 识别端\ 目录，否则服务启动即崩 0xC000041D
  · 启动后必须点窗口上的「启动」按钮（易语言 WTWindow），服务才监听 506
  · 实测识别可用率 ~89%（9 张真实挑战中 8 张出点）

用法：
    from ocr_service import OcrService
    with OcrService() as s:                 # 自动拉起 + 点启动
        print(s.ocr(open('cap.jpg','rb').read()))
        # -> '113,108|249,46|240,119|48,50|'
"""
import base64, ctypes, os, socket, subprocess, time
from ctypes import wintypes

def _resolve_service_dir():
    """按优先级定位「识别端」目录：
       1) 环境变量 WM_OCR_DIR
       2) 配置文件里的 ocr_dir
       3) 内置默认路径
    """
    import os
    p = os.environ.get("WM_OCR_DIR")
    if p and os.path.isdir(p):
        return p
    try:
        import app_config
        p = app_config.load().get("ocr_dir")
        if p and os.path.isdir(p):
            return p
        # ★ 不写死绝对路径 —— 交给 app_config 按「配置 -> 自动解包 -> exe 同目录」
        #   的顺序去找，别人机器上也能找到
        return app_config.default_ocr_dir()
    except Exception:
        pass
    # 最后的兜底：exe / 脚本所在目录下的「识别端」
    import sys as _sys
    base = (os.path.dirname(_sys.executable) if getattr(_sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "识别端")


SERVICE_DIR = _resolve_service_dir()
EXE = os.path.join(SERVICE_DIR, "图像识别POST服务.exe")
HOST, PORT = "127.0.0.1", 506

def _safe_print(*a, **k):
    try:
        print(*a, **k)
    except Exception:
        pass


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _text(h):
    b = ctypes.create_unicode_buffer(1024); _user32.GetWindowTextW(h, b, 1024); return b.value


def _cls(h):
    b = ctypes.create_unicode_buffer(256); _user32.GetClassNameW(h, b, 256); return b.value


def _children(h):
    out = []
    def cb(w, l):
        out.append(w); return True
    _user32.EnumChildWindows(h, _ENUMPROC(cb), 0)
    return out


def _top():
    out = []
    def cb(w, l):
        out.append(w); return True
    _user32.EnumWindows(_ENUMPROC(cb), 0)
    return out


def _pid_of(h):
    p = wintypes.DWORD(); _user32.GetWindowThreadProcessId(h, ctypes.byref(p)); return p.value


def port_open(timeout=2):
    try:
        socket.create_connection((HOST, PORT), timeout=timeout).close()
        return True
    except Exception:
        return False


def check_vcomp():
    """确认 32 位 vcomp140.dll 在位（否则服务必崩）"""
    p = os.path.join(SERVICE_DIR, "vcomp140.dll")
    if not os.path.exists(p):
        return False, "识别端\\ 缺 32 位 vcomp140.dll —— 服务会以 0xC000041D 崩溃"
    return True, p


def kill():
    subprocess.run(["taskkill", "/F", "/IM", "图像识别POST服务.exe"],
                   capture_output=True, creationflags=0x08000000)


def _running_count():
    """当前有几个服务进程（不靠中文名匹配，用 WMI 取进程列表更稳）"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process -Name '图像识别POST服务' -ErrorAction SilentlyContinue).Count"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=0x08000000)
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return 0


def _kill_all(verbose=False):
    """彻底杀干净，并等到真的没了"""
    for i in range(5):
        subprocess.run(["taskkill", "/F", "/IM", "图像识别POST服务.exe"],
                       capture_output=True, creationflags=0x08000000)
        time.sleep(1.0)
        n = _running_count()
        if n == 0:
            if verbose:
                print("[ocr] 旧实例已清空")
            return True
    if verbose:
        print(f"[ocr] 警告：仍有 {_running_count()} 个旧实例没清掉")
    return False


def _find_button(main_win, timeout=20):
    """★ 窗口出现后，子控件可能还没建完 —— 要重试着找按钮"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        for h in _children(main_win):
            tt = _text(h).strip().replace("&", "")
            if tt == "启动" and _cls(h).lower().startswith(("button", "_el")):
                return h
        time.sleep(0.5)
    return None


def _try_start_once(verbose=True, window_wait=40, port_wait=45):
    """启动一次；成功返回 True"""
    p = subprocess.Popen([EXE], cwd=SERVICE_DIR)
    if verbose:
        print(f"[ocr] 已拉起 PID={p.pid}")

    # 1) 等主窗口（只认 WTWindow，别被 GDI+ Hook Window 抢走）
    main_win = None
    t0 = time.time()
    while time.time() - t0 < window_wait:
        time.sleep(0.6)
        if p.poll() is not None:
            raise RuntimeError(f"服务启动即退出，码=0x{p.returncode & 0xFFFFFFFF:08X}")
        cands = [w for w in _top() if _pid_of(w) == p.pid and _user32.IsWindowVisible(w)]
        wts = [w for w in cands if _cls(w) == "WTWindow"]
        if wts:
            main_win = wts[0]
            break
    if main_win is None:
        if verbose:
            print("[ocr] 没等到 WTWindow 主窗口")
        return False

    # 2) 找「启动」按钮（重试，等控件建完）
    btn = _find_button(main_win, timeout=20)
    if btn is None:
        if verbose:
            print("[ocr] 没找到「启动」按钮（等 20 秒仍无）")
        return False

    # 3) 点它
    _user32.PostMessageW(btn, 0x00F5, 0, 0)
    if verbose:
        print("[ocr] 已点「启动」")

    # 4) 等端口
    t0 = time.time()
    while time.time() - t0 < port_wait:
        if port_open():
            if verbose:
                print(f"[ocr] 506 已监听（{time.time()-t0:.1f}s）")
            hide_window(verbose=verbose)
            return True
        time.sleep(0.5)
    if verbose:
        print("[ocr] 点了启动但端口没起来")
    return False


def start(wait=45, verbose=True):
    """拉起服务并点「启动」按钮。返回 True 表示 506 已监听。

    会先彻底清掉旧实例，再整体重试一次 —— 残留实例是实测最常见的失败原因。
    """
    ok, msg = check_vcomp()
    if not ok:
        raise RuntimeError(msg)
    if port_open():
        if verbose:
            print("[ocr] 服务已在监听 506")
        hide_window(verbose=verbose)
        return True

    last = ""
    for attempt in range(2):
        if verbose and attempt:
            print("[ocr] 第一次没起来，重试 …")
        _kill_all(verbose)
        time.sleep(2)
        try:
            if _try_start_once(verbose=verbose, port_wait=wait):
                return True
            last = "点了启动但端口没起来"
        except Exception as e:
            last = str(e)
    raise RuntimeError(f"识别服务起不来（试了 2 次）：{last}")


def ocr(img_bytes, timeout=60):
    """把图片交给本地服务，返回坐标串原文（失败时返回 '-1'）"""
    body = base64.b64encode(img_bytes)
    s = socket.create_connection((HOST, PORT), timeout=10)
    s.sendall(("POST /ocr HTTP/1.1\r\nHost: %s\r\n"
               "Content-Type: application/x-www-form-urlencoded\r\n"
               "Content-Length: %d\r\nConnection: close\r\n\r\n"
               % (HOST, len(body))).encode() + body)
    s.settimeout(timeout)
    buf = b""
    while True:
        try:
            d = s.recv(8192)
        except socket.timeout:
            break
        if not d:
            break
        buf += d
        if len(buf) > 200000:
            break
    s.close()
    t = buf.decode("utf-8", "replace")
    return t.split("\r\n\r\n", 1)[1].strip() if "\r\n\r\n" in t else t.strip()


class OcrService:
    """上下文管理器：进入时确保服务在跑，退出时默认不关（常驻更省事）。"""

    def __init__(self, autostart=True, keep_alive=True):
        self.autostart = autostart
        self.keep_alive = keep_alive

    def __enter__(self):
        if self.autostart:
            start()
        return self

    def __exit__(self, *a):
        if not self.keep_alive:
            kill()
        return False

    ocr = staticmethod(ocr)
    start = staticmethod(start)
    kill = staticmethod(kill)
    port_open = staticmethod(port_open)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "stop":
        kill(); print("已停止"); sys.exit(0)
    start()
    if len(sys.argv) > 1:
        raw = open(sys.argv[1], "rb").read()
        print("识别结果:", ocr(raw))
    else:
        print("服务就绪。用法: python ocr_service.py <图片> | stop")


def hide_window(verbose=False):
    """把识别服务的窗口藏到屏幕外。

    它是个 GUI 程序，启动后会弹一个「图像识别POST服务」窗口 ——
    对使用者来说莫名其妙。藏到屏幕外即可（**不能销毁**，
    因为「启动」按钮要靠它，销毁了服务会退）。

    ★ 按窗口标题找，不依赖 PID（PID 查询在有些机器上不可靠）。
    """
    import ctypes
    import ctypes.wintypes as wt
    u = ctypes.windll.user32
    u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    u.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    u.IsWindowVisible.argtypes = [wt.HWND]

    found = []
    ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(h, l):
        n = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(h, n, 256)
        if "图像识别" in n.value:
            found.append((h, n.value))
        return True

    try:
        u.EnumWindows(ENUMPROC(cb), 0)
    except Exception:
        pass

    ok = False
    for h, title in found:
        # SWP_NOSIZE(0x1) | SWP_NOZORDER(0x4) | SWP_NOACTIVATE(0x10)
        if u.SetWindowPos(h, 0, -32000, -32000, 0, 0, 0x1 | 0x4 | 0x10):
            ok = True
            if verbose:
                print(f"[ocr] 已把「{title}」移到屏幕外")
    if verbose and not found:
        print("[ocr] 没找到服务窗口（可能已最小化或在托盘）")
    return ok


def _find_pid():
    """找当前在跑的识别服务进程 PID"""
    import subprocess
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process -Name '图像识别POST服务' -ErrorAction SilentlyContinue |"
             " Select-Object -First 1).Id"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=0x08000000)
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return 0
