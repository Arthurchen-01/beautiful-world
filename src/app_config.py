# -*- coding: utf-8 -*-
"""
app_config.py —— 统一配置（GUI 和 CLI 共用）
存放位置：%LOCALAPPDATA%\\WMRoleScan\\config.json
"""
import json
import os
import sys

APP_NAME = "WMRoleScan"


def app_dir():
    """程序数据目录（打包成 exe 后也能正常写）"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


CONFIG_FILE = os.path.join(app_dir(), "config.json")
WORK_DIR = app_dir()          # 结果/日志/进度都写这里

DEFAULTS = {
    "ocr_dir": "",            # 识别端目录（空 = 自动找）
    "accounts_file": "",
    "proxy_file": "",
    "out_file": os.path.join(WORK_DIR, "result.txt"),
    "game": "auto",
    "parallel": 8,
    "workers": 3,
    "rate": 0.8,
    "acct_delay": 3.0,
    "use_proxy_pool": True,
}


def bundled_dir():
    """PyInstaller 打包后的临时解包目录（没打包时返回脚本目录）"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def ensure_ocr_dir():
    """★ 首次运行时，把打包进 exe 的「识别端」释放到本地目录（只做一次）。

    为什么不在 _MEIPASS 里直接用：
      · _MEIPASS 是临时目录，每次启动都重建（80MB 白解一遍）
      · 识别服务会在自己旁边写「错误图片」，临时目录退出就没了
    """
    import shutil
    dst = os.path.join(app_dir(), "识别端")
    exe = os.path.join(dst, "图像识别POST服务.exe")
    if os.path.exists(exe):
        return dst
    for src in (os.path.join(bundled_dir(), "识别端"),
                os.path.join(bundled_dir(), "assets", "识别端"),
                os.path.join(bundled_dir(), "_ocr", "识别端")):
        if os.path.isdir(src):
            try:
                os.makedirs(dst, exist_ok=True)
                for fn in os.listdir(src):
                    s, d = os.path.join(src, fn), os.path.join(dst, fn)
                    if os.path.isfile(s) and not os.path.exists(d):
                        shutil.copy2(s, d)
                with open(os.path.join(dst, ".extracted"), "w") as f:
                    f.write("ok")
            except Exception:
                pass
            break
    return dst


# ★ 防递归：load() 会调 default_ocr_dir()，而 default_ocr_dir() 又要读配置。
#   没有这个哨兵就会无限递归 → RecursionError（实测踩过）。
_LOADING = False


def default_ocr_dir():
    """按优先级找一个可用的「识别端」目录

    ★ 不写死任何绝对路径 —— 在别人机器上也能找到。
    顺序：配置里的 -> 环境变量 -> 自动解包的 -> exe 同目录 -> exe 内嵌资源
    """
    cands = [
        (load() or {}).get("ocr_dir"),
        os.environ.get("WM_OCR_DIR"),
        ensure_ocr_dir(),
        os.path.join(exe_dir(), "识别端"),
        os.path.join(app_dir(), "识别端"),
        os.path.join(bundled_dir(), "识别端"),
        os.path.join(bundled_dir(), "assets", "识别端"),
    ]
    for c in cands:
        if c and os.path.isdir(c) and os.path.exists(
                os.path.join(c, "图像识别POST服务.exe")):
            return c
    return cands[2]


def load():
    global _LOADING
    if _LOADING:
        # 递归进来了（default_ocr_dir 反过来调 load）—— 直接给默认值，别转圈
        return dict(DEFAULTS)
    _LOADING = True
    try:
        cfg = dict(DEFAULTS)
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg.update(json.load(f) or {})
        except Exception:
            pass
        if not cfg.get("ocr_dir"):
            cfg["ocr_dir"] = default_ocr_dir()
        return cfg
    finally:
        _LOADING = False


def save(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    print("配置目录:", app_dir())
    print("配置文件:", CONFIG_FILE)
    print("默认识别端:", default_ocr_dir())
    print(json.dumps(load(), ensure_ascii=False, indent=2))


def exe_dir():
    """程序所在目录 —— 结果文件默认放这里，用户一眼能找到。

    打包成 exe 时是 exe 所在目录；源码运行时是脚本所在目录。
    """
    import sys as _sys
    if getattr(_sys, "frozen", False):
        return os.path.dirname(_sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def default_out():
    """默认结果文件路径：程序目录下的「结果.txt」"""
    return os.path.join(exe_dir(), "结果.txt")
