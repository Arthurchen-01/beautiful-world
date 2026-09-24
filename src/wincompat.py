# -*- coding: utf-8 -*-
"""
wincompat.py —— Windows 版本兼容层
================================================================
目标：同一份 exe 在 Win7 / Win8.1 / Win10 / Win11 上都能起来，
      能用的能力就用，不能用的自动降级，并且**把实际用到的能力记下来**。

覆盖的差异：
  1. DPI 感知：Win10 1703+ 有 PerMonitorV2；Win8.1 只有 shcore；
     Win7 只有 SetProcessDPIAware（甚至可能没有）
  2. GetDpiForWindow / GetDpiForSystem：Win10 1607+ 才有 → 降级到
     GetDeviceCaps(LOGPIXELSX)
  3. SetProcessDpiAwarenessContext：Win10 1703+ → 降级 shcore → user32
  4. GetSystemMetricsForDpi：Win10 1607+ → 降级 GetSystemMetrics
  5. PowerShell 可能不存在或被策略禁 → 只用它做可选加速，不作为必需
  6. 中文字体：Win7 有 Microsoft YaHei，但没有 "Microsoft YaHei UI"
  7. WebView2 不依赖（我们用 Tkinter）

用法：
    import wincompat
    wincompat.init()                 # 尽早调用（创建窗口之前）
    print(wincompat.describe())
"""
import ctypes
import os
import platform
import sys

# ---------------------------------------------------------------- 结果缓存
_INFO = {
    "os_name": "",
    "os_version": "",
    "build": 0,
    "arch": "",
    "is_wow64": False,
    "dpi_mode": None,          # 实际用上的 DPI 感知方式
    "dpi_api": None,           # 实际能用的取 DPI 接口
    "ui_font": None,           # 实际用上的界面字体
    "mono_font": None,
    "powershell": None,
    "notes": [],
}


# ---------------------------------------------------------------- 版本探测
def _os_info():
    v = sys.getwindowsversion() if hasattr(sys, "getwindowsversion") else None
    major, minor, build = (v.major, v.minor, v.build) if v else (0, 0, 0)
    if major == 10 and build >= 22000:
        name = "Windows 11"
    elif major == 10:
        name = "Windows 10"
    elif major == 6 and minor == 3:
        name = "Windows 8.1"
    elif major == 6 and minor == 2:
        name = "Windows 8"
    elif major == 6 and minor == 1:
        name = "Windows 7"
    elif major == 6 and minor == 0:
        name = "Windows Vista"
    else:
        name = f"Windows {major}.{minor}"
    return name, f"{major}.{minor}.{build}", build


def _arch():
    bits = 64 if sys.maxsize > 2 ** 32 else 32
    try:
        is_wow = bool(ctypes.windll.kernel32.IsWow64Process(
            ctypes.windll.kernel32.GetCurrentProcess(),
            ctypes.byref(ctypes.c_int(0))))
    except Exception:
        is_wow = False
    return f"x{bits}", is_wow


# ---------------------------------------------------------------- DPI
def _set_dpi_awareness():
    """按能力从高到低降级，返回实际用上的方式名"""
    u = ctypes.windll.user32

    # 1) Per-Monitor v2（Win10 1703+）
    #    ★ 必须设 argtypes：不设的话 ctypes 按 32 位 int 传参，
    #      而这个函数要 64 位 HANDLE，x64 上会调用失败（返回 0）。
    try:
        u.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        u.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        if u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "PerMonitorV2"
    except Exception:
        pass

    # 2) Per-Monitor（Win10 1607+）
    try:
        if u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-3)):
            return "PerMonitor"
    except Exception:
        pass

    # 3) System DPI aware（Win8.1+，shcore）
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(1) == 0:
            return "System(shcore)"
    except Exception:
        pass

    # 4) System DPI aware（Win7 也有）
    try:
        if u.SetProcessDPIAware():
            return "System(user32)"
    except Exception:
        pass

    return None


def get_dpi(root=None):
    """取当前 DPI。按 Win10 1607+ / 更老 逐级降级。"""
    u = ctypes.windll.user32
    # 1) 窗口级（Win10 1607+）
    if root is not None:
        try:
            u.GetDpiForWindow.argtypes = [ctypes.c_void_p]
            u.GetDpiForWindow.restype = ctypes.c_uint
            d = u.GetDpiForWindow(ctypes.c_void_p(root.winfo_id()))
            if d:
                _INFO["dpi_api"] = "GetDpiForWindow"
                return float(d)
        except Exception:
            pass
    # 2) 系统级（Win10 1607+）
    try:
        u.GetDpiForSystem.restype = ctypes.c_uint
        d = u.GetDpiForSystem()
        if d:
            _INFO["dpi_api"] = "GetDpiForSystem"
            return float(d)
    except Exception:
        pass
    # 3) 老办法（所有版本）
    try:
        hdc = u.GetDC(0)
        d = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)   # LOGPIXELSX
        u.ReleaseDC(0, hdc)
        if d:
            _INFO["dpi_api"] = "GetDeviceCaps"
            return float(d)
    except Exception:
        pass
    # 4) Tk 自己的
    if root is not None:
        try:
            d = float(root.winfo_fpixels("1i"))
            if d:
                _INFO["dpi_api"] = "winfo_fpixels"
                return d
        except Exception:
            pass
    _INFO["dpi_api"] = "default96"
    return 96.0


# ---------------------------------------------------------------- 字体
def pick_fonts():
    """挑一套本机存在的字体（Win7 没有 Microsoft YaHei UI）"""
    import tkinter.font as tkfont
    try:
        have = set(tkfont.families())
    except Exception:
        have = set()

    def first(*names):
        for n in names:
            if n in have:
                return n
        return names[-1]

    ui = first("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
               "Segoe UI", "Tahoma", "Arial")
    mono = first("Consolas", "Cascadia Mono", "Courier New", "Courier")
    _INFO["ui_font"] = ui
    _INFO["mono_font"] = mono
    return ui, mono


# ---------------------------------------------------------------- PowerShell
def has_powershell():
    """PowerShell 只是可选加速，不是必需"""
    import shutil
    for exe in ("powershell.exe", "pwsh.exe"):
        p = shutil.which(exe)
        if p:
            _INFO["powershell"] = p
            return True
    _INFO["powershell"] = None
    return False


# ---------------------------------------------------------------- 入口
def init():
    name, ver, build = _os_info()
    _INFO["os_name"] = name
    _INFO["os_version"] = ver
    _INFO["build"] = build
    _INFO["arch"], _INFO["is_wow64"] = _arch()

    _INFO["dpi_mode"] = _set_dpi_awareness()

    if not _INFO["dpi_mode"]:
        _INFO["notes"].append("无法设置 DPI 感知 —— 高 DPI 屏上界面可能发虚（不影响功能）")
    if build and build < 10240:
        _INFO["notes"].append("系统较老（Win10 之前）—— 已使用兼容路径")
    if not has_powershell():
        _INFO["notes"].append("未找到 PowerShell —— 已改用不依赖它的实现")
    return _INFO


def describe():
    i = _INFO
    lines = [
        f"系统      : {i['os_name']}  ({i['os_version']})",
        f"架构      : {i['arch']}" + ("  (WOW64)" if i["is_wow64"] else ""),
        f"DPI 感知  : {i['dpi_mode'] or '未启用'}",
        f"取 DPI 用 : {i['dpi_api'] or '未探测'}",
        f"界面字体  : {i['ui_font'] or '未探测'} / {i['mono_font'] or '未探测'}",
        f"PowerShell: {i['powershell'] or '无'}",
    ]
    for n in i["notes"]:
        lines.append(f"注意      : {n}")
    return "\n".join(lines)


def info():
    return dict(_INFO)


if __name__ == "__main__":
    init()
    print(describe())
