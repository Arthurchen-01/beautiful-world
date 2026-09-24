# -*- coding: utf-8 -*-
"""
build_exe.py —— 一键打包成单文件 exe
用法:  python build_exe.py [--dir] [--lean]
  --dir   打包成文件夹版（启动更快）
  --lean  不打包识别端（体积小，但需要旁边放一个「识别端」目录）
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OCR_SRC = r"<PROJECT_DIR>\识别端"
ICON = os.path.join(HERE, "app.ico")

HIDDEN = [
    "wm_scan", "wmcaptcha", "netguard", "proxy_pool", "ocr_service",
    "servers", "egress_guard", "solve_pick_text", "app_config", "wincompat",
    "socks", "sockshandler",
    "Crypto", "Crypto.Cipher", "Crypto.Cipher.AES", "Crypto.Cipher.PKCS1_v1_5",
    "Crypto.Util", "Crypto.Util.Padding", "Crypto.PublicKey", "Crypto.PublicKey.RSA",
]

onedir = "--dir" in sys.argv
lean = "--lean" in sys.argv
sep = ";"

APP_NAME = "完美世界扫号工具"
if lean:
    APP_NAME += "-瘦身版"

cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
       "--windowed",
       "--name", APP_NAME,
       "--icon", ICON,
       "--distpath", os.path.join(HERE, "dist"),
       "--workpath", os.path.join(HERE, "build"),
       "--specpath", HERE,
       "--onedir" if onedir else "--onefile"]

# 版本资源（exe 属性里能看到版本号/产品名）
_ver = os.path.join(HERE, "version_info.txt")
if os.path.exists(_ver):
    cmd += ["--version-file", _ver]

for h in HIDDEN:
    cmd += ["--hidden-import", h]

# 样本图（自检用）
samp = os.path.join(HERE, "samples")
if os.path.isdir(samp):
    cmd += ["--add-data", samp + sep + "samples"]
    print("[build] 打包样本图: " + samp)

# 识别端（首次运行会解到 %LOCALAPPDATA%）
if not lean and os.path.isdir(OCR_SRC):
    cmd += ["--add-data", OCR_SRC + sep + "识别端"]
    print("[build] 打包识别端: " + OCR_SRC)
elif lean:
    print("[build] 瘦身模式：不打包识别端")
else:
    print("[build] !! 识别端不存在，跳过: " + OCR_SRC)

cmd.append(os.path.join(HERE, "wm_gui.py"))

print("[build] 开始打包（首次可能要几分钟）...\n")
t0 = time.time()
p = subprocess.run(cmd, cwd=HERE)
dt = time.time() - t0

out_dir = os.path.join(HERE, "dist")
target = os.path.join(out_dir, APP_NAME + ".exe")
if onedir:
    target = os.path.join(out_dir, APP_NAME, APP_NAME + ".exe")

print("\n[build] 退出码 %d  用时 %.1f 分" % (p.returncode, dt / 60))
if os.path.exists(target):
    size = os.path.getsize(target)
    print("[build] OK 产物: " + target)
    print("[build]    大小: %.1f MB" % (size / 1024 / 1024))
else:
    print("[build] FAIL 没找到产物，看上面的报错")
