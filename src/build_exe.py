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
ICON = os.path.join(HERE, "app.ico")


def ensure_icon():
    """★ app.ico 是生成物、不入库。缺了就现场生成 ——
    否则 PyInstaller 会抛 FileNotFoundError，然后脚本还报"OK"（见下）。"""
    if os.path.exists(ICON):
        return True
    mk = os.path.join(HERE, "make_icon.py")
    if not os.path.exists(mk):
        print("[build] !! 缺 app.ico，也找不到 make_icon.py")
        return False
    print("[build] app.ico 不存在，用 make_icon.py 生成 ...")
    r = subprocess.run([sys.executable, mk], cwd=HERE)
    if r.returncode != 0 or not os.path.exists(ICON):
        print("[build] !! 生成 app.ico 失败（退出码 %d）" % r.returncode)
        return False
    print("[build] app.ico 已生成")
    return True


# ---------------------------------------------------------------- 识别端位置
#
# ★ 这里原来写的是占位符 `<PROJECT_DIR>\识别端`（为了入库脱敏），
#   直接跑会打出一个**没有 OCR 引擎的残废包**（只在日志里打一行"跳过"就过去了，
#   很容易漏看）。改成：环境变量优先 → 常见位置兜底 → 都没有就明确报错。
OCR_SRC = os.environ.get("WM_OCR_SRC", "").strip()

_OCR_CANDIDATES = [
    os.path.join(os.path.dirname(HERE), "识别端"),        # 仓库旁边
    r"D:\验证码\识别端",
    r"D:\Desktop\桌面迁移\AI brain storming\tg\诛仙\识别端",
]
if not OCR_SRC:
    for c in _OCR_CANDIDATES:
        if os.path.isdir(c) and os.path.exists(os.path.join(c, "OCR.dll")):
            OCR_SRC = c
            break

# 识别端必须齐这些文件，缺一个 OCR 就跑不起来
OCR_REQUIRED = ["OCR.dll", "XYLib.dll", "HPSocket4C.dll", "vcomp140.dll",
                "图像识别POST服务.exe", "Config.ini"]

HIDDEN = [
    "wm_scan", "wmcaptcha", "netguard", "proxy_pool", "ocr_service",
    "servers", "egress_guard", "solve_pick_text", "app_config", "wincompat",
    "webui",                      # ★ 网页控制台（--web 模式）
    "http.server", "http.cookies", "socketserver", "email.utils",
    "socks", "sockshandler",
    "Crypto", "Crypto.Cipher", "Crypto.Cipher.AES", "Crypto.Cipher.PKCS1_v1_5",
    "Crypto.Util", "Crypto.Util.Padding", "Crypto.PublicKey", "Crypto.PublicKey.RSA",
]

onedir = "--dir" in sys.argv
lean = "--lean" in sys.argv
sep = ";"

# ★ 先确保图标在 —— 缺了 PyInstaller 会直接抛 FileNotFoundError，
#   而旧版脚本照样打印 "OK 产物"（只检查文件存在、不检查大小），
#   结果就是一个 0.3MB 的空壳被当成成功产物。这里两道都堵上。
if not ensure_icon():
    print("[build] !! 没有图标，中止。")
    sys.exit(2)

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
# ★ 别只找 src/samples —— 仓库里样本图放在**仓库根目录**的 samples/，
#   只找 src/ 下会导致样本没打进包，自检的「本地 OCR 实拍」那一步
#   静默跳过（打印"(没有样本图，跳过)"），看起来是绿的，其实没测。
samp = os.path.join(HERE, "samples")
if not os.path.isdir(samp):
    _alt = os.path.join(os.path.dirname(HERE), "samples")
    if os.path.isdir(_alt):
        samp = _alt
if os.path.isdir(samp):
    _n = len([f for f in os.listdir(samp) if f.lower().endswith((".jpg", ".jpeg", ".png"))])
    if _n == 0:
        print("[build] !! samples 目录存在但没有图片: " + samp)
    else:
        cmd += ["--add-data", samp + sep + "samples"]
        print("[build] 打包样本图: %s  (%d 张)" % (samp, _n))
else:
    print("[build] !! 没找到 samples 目录 —— 自检的「本地 OCR 实拍」会跳过")
    print("[build] !! 找过: %s" % os.path.join(HERE, "samples"))
    print("[build] !!       %s" % os.path.join(os.path.dirname(HERE), "samples"))

# 识别端（首次运行会解到 %LOCALAPPDATA%）
if lean:
    print("[build] 瘦身模式：不打包识别端")
elif OCR_SRC and os.path.isdir(OCR_SRC):
    missing = [f for f in OCR_REQUIRED if not os.path.exists(os.path.join(OCR_SRC, f))]
    if missing:
        print("[build] !! 识别端缺文件，打出来 OCR 会跑不起来: %s" % ", ".join(missing))
        print("[build] !! 位置: %s" % OCR_SRC)
        sys.exit(2)
    sizes = sum(os.path.getsize(os.path.join(OCR_SRC, f)) for f in OCR_REQUIRED
                if os.path.exists(os.path.join(OCR_SRC, f)))
    print("[build] 打包识别端: %s  (%.1f MB)" % (OCR_SRC, sizes / 1024 / 1024))
    cmd += ["--add-data", OCR_SRC + sep + "识别端"]
else:
    print("[build] !! 找不到识别端 —— 这样打出来的包没有 OCR，点选验证码会失败。")
    print("[build] !! 解决办法：设环境变量 WM_OCR_SRC 指向识别端目录，例如")
    print('[build] !!   $env:WM_OCR_SRC = "D:\\验证码\\识别端"')
    print("[build] !! 要找的位置（都试过了）：")
    for c in _OCR_CANDIDATES:
        print("[build] !!   %s" % c)
    print("[build] !! 想明确打瘦身版请加 --lean")
    sys.exit(2)

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

# ★ 别只看"文件存在" —— PyInstaller 报错时也可能留下一个几十 KB 的空壳，
#   旧版脚本就因此把失败当成功报。这里按大小和退出码双重判定。
MIN_MB = 10.0 if not lean else 3.0
if p.returncode != 0:
    print("[build] FAIL PyInstaller 退出码 %d，看上面的报错" % p.returncode)
    sys.exit(1)
if not os.path.exists(target):
    print("[build] FAIL 没找到产物，看上面的报错")
    sys.exit(1)

size_mb = os.path.getsize(target) / 1024 / 1024
if size_mb < MIN_MB:
    print("[build] FAIL 产物只有 %.1f MB（预期 > %.0f MB）—— 多半是空壳，不是真产物"
          % (size_mb, MIN_MB))
    print("[build]      产物: %s" % target)
    sys.exit(1)

print("[build] OK 产物: " + target)
print("[build]    大小: %.1f MB" % size_mb)
if not lean:
    print("[build]    校验: 含识别端" if OCR_SRC else "[build]    警告: 没含识别端")

