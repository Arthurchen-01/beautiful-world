# -*- coding: utf-8 -*-
"""
完美世界 · 查区查等级 —— 批量扫号工具（图形界面版 v1.2）

v1.2 改动：
  · ★★ 修掉 DPI 感知的真实 bug —— SetProcessDpiAwarenessContext 少了 argtypes，
       64 位下参数传递错误导致调用失败，界面一直被 Windows 放大 2 倍（"720p 感"的根因）
  · 窗口尺寸与所有像素间距按 DPI 缩放，200% 屏上不再是巨窗
  · 浅色主题，收紧布局，窗口逻辑尺寸 660x520

打包：  python build_exe.py
"""
import io
import os
import queue
import re
import sys
import threading
import time

# ================================================================ ★ OCR 专用入口
#
# 必须放在**所有 tkinter 导入之前**：这两条路径是给服务器上的常驻任务用的，
# 而服务器上可能压根没有图形界面库（Server Core / 精简环境）。
# 早期版本把它们放在 main() 里，结果 import tkinter 先失败了，
# 功能根本没走到 —— 实测报 `ModuleNotFoundError: No module named 'tkinter'`。
#
# 为什么要这个入口：OCR 引擎是 GUI 程序，要靠窗口消息点「启动」按钮。
# 计划任务用 SYSTEM 跑在 Session 0（没有交互桌面）时，能不能把窗口建出来
# 是**没验证过**的。兜底做法：在**用户会话**里用 --ocr-start 把服务常驻起来，
# 之后 Session 0 里的扫描任务一连 506 就发现已经在监听，
# ocr_service.start() 会直接返回、根本不碰窗口，坑就绕过去了。
if len(sys.argv) > 1 and sys.argv[1] in ("--ocr-start", "--ocr-stop"):
    _here0 = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
              else os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, _here0)
    try:
        sys.path.insert(0, getattr(sys, "_MEIPASS", _here0))
    except Exception:
        pass

    def _ocr_task_main():
        import app_config
        import ocr_service
        logp = os.path.join(app_config.app_dir(), "ocr_task.log")

        def _ol(m):
            try:
                with io.open(logp, "a", encoding="utf-8") as f:
                    f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), m))
            except Exception:
                pass

        if sys.argv[1] == "--ocr-stop":
            ocr_service.kill()
            _ol("已停止 OCR 服务")
            return 0

        _ol("---- 开始拉起 OCR 服务 ----  argv=%r" % (sys.argv,))
        if ocr_service.port_open():
            _ol("506 已在监听，无需操作")
            return 0
        try:
            ocr_service.start(verbose=False)
            ok = ocr_service.port_open()
            _ol("OK 506 已监听" if ok else "FAIL 506 没起来")
            return 0 if ok else 1
        except Exception as e:
            _ol("EXC %s: %s" % (type(e).__name__, e))
            return 1

    sys.exit(_ocr_task_main())


# ================================================================ 兼容层
# 系统版本 / DPI / 字体 / PowerShell 的探测与降级都在 wincompat 里，
# 同一份 exe 在 Win7~Win11 上都能起来。
import wincompat                                        # noqa: E402

_OSINFO = wincompat.init()
_DPI_MODE = _OSINFO.get("dpi_mode")


import tkinter as tk                                    # noqa: E402
from tkinter import filedialog, messagebox, ttk        # noqa: E402

if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
    sys.path.insert(0, getattr(sys, "_MEIPASS", HERE))
    sys.path.insert(0, HERE)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import app_config                                       # noqa: E402

APP_TITLE = "完美世界扫号工具"
VERSION = "v1.2"

# ---------------------------------------------------------------- 浅色配色
BG = "#f2f4f7"
CARD = "#ffffff"
FG = "#111827"
DIM = "#6b7280"
LINE = "#e3e6ea"
ACC = "#2563eb"
OKC = "#15803d"
ERRC = "#b91c1c"
WARNC = "#b45309"
LOGBG = "#fbfcfd"

UI = "Microsoft YaHei UI"      # 创建窗口后会被 wincompat 挑出的真实字体替换
MONO = "Consolas"

# 逻辑尺寸（会按 DPI 放大成物理像素）
# ★ 实测事实：Tk 的**字体**按 `tk scaling` 自动缩放，
#   但**窗口 geometry 与 padx/pady 是裸物理像素**。
#   在 200% 缩放的屏幕上必须自己乘 dpi/96，否则窗口和间距都只有一半。
WIN_W, WIN_H = 660, 520
_SCALE = 1.0


def S(n):
    """逻辑像素 -> 物理像素（窗口尺寸、间距都要过这一层）"""
    return max(1, int(round(n * _SCALE)))


def _real_dpi(root=None):
    """★ 直接问 Windows 要 DPI。

    不能用 Tk 的 winfo_fpixels('1i') —— 实测在打包成 exe 后它返回 96，
    而窗口真实 DPI 是 192，导致界面尺寸算错一半。
    """
    import ctypes
    u = ctypes.windll.user32
    try:
        u.GetDpiForWindow.restype = ctypes.c_uint
        if root is not None:
            d = u.GetDpiForWindow(root.winfo_id())
            if d:
                return float(d)
    except Exception:
        pass
    try:
        u.GetDpiForSystem.restype = ctypes.c_uint
        d = u.GetDpiForSystem()
        if d:
            return float(d)
    except Exception:
        pass
    try:
        return float(root.winfo_fpixels("1i")) if root is not None else 96.0
    except Exception:
        return 96.0


class App:
    def __init__(self, root):
        global _SCALE
        self.root = root
        self.cfg = app_config.load()
        self.q = queue.Queue()
        self.running = False
        self.scan_finished = False      # 线程安全的完成标志（E2E 靠它判断）
        self.stat = {"ok": 0, "fail": 0, "done": 0, "total": 0, "t0": None,
                     "skip": 0, "input": 0, "rate": 0, "eta_min": 0,
                     "elapsed_min": 0, "idx": 0, "reasons": {}}

        # ---- DPI 缩放（走兼容层，老系统自动降级）----
        global UI, MONO
        try:
            root.update_idletasks()
        except Exception:
            pass
        dpi = wincompat.get_dpi(root)
        _SCALE = max(1.0, dpi / 96.0)
        self.dpi = dpi
        try:
            UI, MONO = wincompat.pick_fonts()
        except Exception:
            pass

        root.title(f"{APP_TITLE} {VERSION}")
        # geometry / 间距用物理像素（乘过 _SCALE）；字体交给 Tk 的 tk scaling。
        root.geometry(f"{S(WIN_W)}x{S(WIN_H)}")
        root.minsize(S(520), S(420))
        root.configure(bg=BG)
        try:
            with open(os.path.join(app_config.WORK_DIR, "ui_debug.txt"), "w",
                      encoding="utf-8") as _f:
                _f.write(f"dpi={dpi} scale={_SCALE}\n"
                         f"tk_scaling={root.tk.call('tk', 'scaling')}\n"
                         f"geometry={S(WIN_W)}x{S(WIN_H)}\n")
        except Exception:
            pass

        self._style()
        self._build()

        self.root.after(120, self._drain)
        self.root.after(500, self._tick_status)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._log(f"DPI 感知 {_DPI_MODE or '未启用'}   屏幕缩放 {dpi/96*100:.0f}%   "
                  f"窗口 {WIN_W}x{WIN_H} 逻辑 = {S(WIN_W)}x{S(WIN_H)} 物理", "dim")
        self._log("先点「环境自检」，三项都 ✅ 再开始。", "dim")

    # ------------------------------------------------------------ 窗口尺寸
    def _top_hwnd(self):
        """★ 找出**真正可见的那个顶层窗口**。

        坑（踩了很久）：`root.winfo_id()` 返回的 HWND 不是可见窗口 ——
        实测应用自报 22088548，屏幕上真正可见的 TkTopLevel 是 7800866。
        GetParent(winfo_id()) 也不行（那是 Tk 的隐藏 owner）。
        唯一可靠的办法：枚举本进程的窗口，按类名 `TkTopLevel` 找。
        """
        import ctypes
        import ctypes.wintypes as wt
        u = ctypes.windll.user32
        u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        u.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        u.IsWindowVisible.argtypes = [wt.HWND]
        mypid = os.getpid()
        found = []
        ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

        def cb(h, l):
            pid = wt.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            if pid.value == mypid:
                c = ctypes.create_unicode_buffer(200)
                u.GetClassNameW(h, c, 200)
                if c.value == "TkTopLevel" and u.IsWindowVisible(h):
                    found.append(h)
            return True

        try:
            u.EnumWindows(ENUMPROC(cb), 0)
        except Exception:
            pass
        if found:
            return wt.HWND(found[0])
        return wt.HWND(self.root.winfo_id())

    def _calibrate_size(self, tag=""):
        """★ 运行时实测校准窗口尺寸。

        实测：源码形态下 Tk 的 geometry 是"逻辑单位、Tk 自己乘 DPI"，一切正常；
        但打包成 exe 后（PyInstaller 自带的 Tcl/Tk），同样的 geometry(660x520)
        只给出 330x260 物理 —— 单位差一倍。
        所以这里**量一次真实物理尺寸，算出比例，再重设**，不靠猜。
        """
        try:
            import ctypes
            import ctypes.wintypes as wt
            u = ctypes.windll.user32
            hwnd = self._top_hwnd()
            r = wt.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(r))
            cur_w, cur_h = r.right - r.left, r.bottom - r.top
            tk_w = max(1, self.root.winfo_width())
            tk_h = max(1, self.root.winfo_height())
            if cur_w <= 0 or cur_h <= 0:
                return
            # 想要的物理尺寸
            want_w = int(WIN_W * self.dpi / 96.0)
            want_h = int(WIN_H * self.dpi / 96.0)
            # 先按 Tk 单位重设一次（源码形态靠这条就够）
            self.root.geometry(f"{want_w * tk_w // max(1, cur_w)}x"
                               f"{want_h * tk_h // max(1, cur_h)}")
            self.root.update_idletasks()
            # 再用 Win32 直接定死（冻结形态靠这条）
            u.GetWindowRect(hwnd, ctypes.byref(r))
            cw, ch = r.right - r.left, r.bottom - r.top
            if abs(cw - want_w) > 12 or abs(ch - want_h) > 12:
                u.MoveWindow(hwnd, r.left, r.top, want_w, want_h, True)
            u.GetWindowRect(hwnd, ctypes.byref(r))
            with open(os.path.join(app_config.WORK_DIR, "ui_debug.txt"), "a",
                      encoding="utf-8") as f:
                f.write(f"calib[{tag}]: 起点 {cur_w}x{cur_h} (tk {tk_w}x{tk_h}) "
                        f"-> 目标 {want_w}x{want_h} -> 实际 "
                        f"{r.right-r.left}x{r.bottom-r.top}\n")
        except Exception as e:
            try:
                with open(os.path.join(app_config.WORK_DIR, "ui_debug.txt"), "a",
                          encoding="utf-8") as f:
                    f.write(f"calib[{tag}] 失败: {type(e).__name__}: {e}\n")
            except Exception:
                pass

    def _size_dialog(self, dlg, w, h):
        """对话框尺寸 + 居中。

        ★ 实测事实：Tk 的窗口 geometry 是**裸物理像素**（字体才走 tk scaling），
        所以这里必须过 S() 乘 dpi/96，否则在 200% 屏上对话框只有一半大 ——
        按钮会被挤出可见区域，用户就只能点右上角的叉。
        """
        try:
            dlg.geometry(f"{S(w)}x{S(h)}")
            dlg.update_idletasks()
            sw = dlg.winfo_screenwidth()
            sh = dlg.winfo_screenheight()
            x = max(0, (sw - S(w)) // 2)
            y = max(0, (sh - S(h)) // 3)
            dlg.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _dump_geom(self):
        """运行后再回读一次窗口几何，排查尺寸被改小的问题"""
        try:
            import ctypes
            import ctypes.wintypes as wt
            u = ctypes.windll.user32
            r = wt.RECT()
            u.GetWindowRect(self.root.winfo_id(), ctypes.byref(r))
            with open(os.path.join(app_config.WORK_DIR, "ui_debug.txt"), "a",
                      encoding="utf-8") as f:
                f.write(f"after900 tk_geometry={self.root.winfo_geometry()}\n")
                f.write(f"after900 winfo_wxh={self.root.winfo_width()}x"
                        f"{self.root.winfo_height()}\n")
                f.write(f"after900 物理={r.right-r.left}x{r.bottom-r.top}\n")
                f.write(f"after900 reqw={self.root.winfo_reqwidth()}x"
                        f"{self.root.winfo_reqheight()}\n")
        except Exception:
            pass

    def _style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("TFrame", background=BG)
        s.configure("TLabel", background=BG, foreground=FG, font=(UI, 9))
        s.configure("Dim.TLabel", background=BG, foreground=DIM, font=(UI, 8))
        s.configure("TButton", font=(UI, 9), padding=(S(9), S(4)))
        s.configure("Go.TButton", font=(UI, 9, "bold"), padding=(S(13), S(5)))
        s.configure("TEntry", fieldbackground="#ffffff", foreground=FG,
                    bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
                    padding=S(4))
        s.configure("TCombobox", fieldbackground="#ffffff", foreground=FG,
                    padding=S(3))
        s.configure("TCheckbutton", background=CARD, foreground=FG, font=(UI, 9))
        s.map("TCheckbutton", background=[("active", CARD)])
        s.configure("TProgressbar", troughcolor="#e8ebef", background=ACC,
                    bordercolor=BG, lightcolor=ACC, darkcolor=ACC, thickness=S(7))

    # ------------------------------------------------------------ 布局
    def _build(self):
        pad = S(14)

        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=pad, pady=(S(10), S(5)))
        tk.Label(head, text=APP_TITLE, bg=BG, fg=FG,
                 font=(UI, 11, "bold")).pack(side="left")
        tk.Label(head, text=VERSION, bg=BG, fg=DIM,
                 font=(UI, 8)).pack(side="left", padx=(S(6), 0))

        # ---- 卡片：输入 ----
        c1 = tk.Frame(self.root, bg=CARD, highlightbackground=LINE,
                      highlightthickness=S(1))
        c1.pack(fill="x", padx=pad)
        inner = tk.Frame(c1, bg=CARD)
        inner.pack(fill="x", padx=S(10), pady=S(8))

        self.v_acc = tk.StringVar(value=self.cfg.get("accounts_file", ""))
        self.v_pxy = tk.StringVar(value=self.cfg.get("proxy_file", ""))
        # ★ 结果路径默认放「程序所在目录」，用户一眼能找到。
        #   历史配置里如果存的是内部隐藏目录（AppData），自动纠正过来。
        _saved = self.cfg.get("out_file", "") or ""
        _internal = os.path.normcase(app_config.WORK_DIR)
        if (not _saved) or os.path.normcase(os.path.dirname(_saved)).startswith(_internal):
            _saved = app_config.default_out()
        self.v_out = tk.StringVar(value=_saved)

        self.acc_hint = self._row(inner, 0, "账号", self.v_acc,
                                  self._pick_accounts, self._manual_accounts,
                                  "每行 账号----密码", self._process_accounts)
        self.pxy_hint = self._row(inner, 1, "代理", self.v_pxy,
                                  self._pick_proxy, self._manual_proxy,
                                  "每行 host:port:用户:密码", self._process_proxies)
        self._row(inner, 2, "结果", self.v_out, self._pick_out, None,
                  "跑完的表存这里")

        # ---- 代理 API 行（原软件的「Http代理地址」）----
        self.v_api = tk.StringVar(value=self.cfg.get("proxy_api", ""))
        tk.Label(inner, text="代理API", bg=CARD, fg=FG,
                 font=(UI, 9)).grid(row=3, column=0, sticky="w", pady=S(2))
        ttk.Entry(inner, textvariable=self.v_api, font=(UI, 9)).grid(
            row=3, column=1, sticky="we", padx=(S(9), S(6)), pady=S(2))
        _bf = tk.Frame(inner, bg=CARD)
        _bf.grid(row=3, column=2, pady=S(2))
        ttk.Button(_bf, text="拉取", command=self._fetch_api).pack(side="left")
        self.api_hint = tk.Label(inner, text="粘贴代理提取网址，一键拉取", bg=CARD,
                                 fg=DIM, font=(UI, 8))
        self.api_hint.grid(row=3, column=3, sticky="w", padx=(S(9), 0))

        self.v_api_direct = tk.BooleanVar(
            value=bool(self.cfg.get("api_direct", False)))
        ttk.Checkbutton(inner, text="API 直连", variable=self.v_api_direct).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=S(2))
        tk.Label(inner, text="多数国内代理商的 token 绑 IP —— 走隧道会 403，"
                             "勾这个才能拉到；勾上后本机 IP 会露给代理商",
                 bg=CARD, fg=WARNC, font=(UI, 8)).grid(
            row=4, column=2, columnspan=2, sticky="w", padx=(S(6), 0))

        # ---- 卡片：参数 ----
        c2 = tk.Frame(self.root, bg=CARD, highlightbackground=LINE,
                      highlightthickness=S(1))
        c2.pack(fill="x", padx=pad, pady=(S(7), 0))
        i2 = tk.Frame(c2, bg=CARD)
        i2.pack(fill="x", padx=S(10), pady=S(8))

        tk.Label(i2, text="游戏", bg=CARD, fg=DIM, font=(UI, 8)).pack(side="left")
        self.v_game = tk.StringVar(value=self.cfg.get("game", "auto"))
        cb = ttk.Combobox(i2, textvariable=self.v_game, width=6, state="readonly",
                          font=(UI, 9), values=["auto", "1", "11", "10", "15", "all"])
        cb.pack(side="left", padx=(S(5), S(14)))

        def num(label, key, default, width=4, tip=""):
            tk.Label(i2, text=label, bg=CARD, fg=DIM,
                     font=(UI, 8)).pack(side="left")
            v = tk.StringVar(value=str(self.cfg.get(key, default)))
            e = ttk.Entry(i2, textvariable=v, width=width, font=(UI, 9))
            e.pack(side="left", padx=(S(5), S(14)))
            if tip:
                e.bind("<FocusIn>", lambda ev: self._log("  " + tip, "dim"))
            return v

        self.v_par = num("并行", "parallel", 8, 4,
                         "同时跑几个账号。上限 = 你的独立代理条数\n"
                         "（一个账号配一条 IP，账号之间不共用）\n"
                         "同一个账号不能同时跑两次 —— 对方按账号锁会话")
        self.v_rate = num("限速", "rate", 0.8, 5, "两次请求最小间隔秒（0.8 最优）")
        self.v_retry = num("重试", "retries", 3, 3, "网络类失败换代理重跑的次数")
        self.v_usepool = tk.BooleanVar(value=bool(self.cfg.get("use_proxy_pool", True)))
        ttk.Checkbutton(i2, text="多 IP 代理池",
                        variable=self.v_usepool).pack(side="left")
        self.v_verbose = tk.BooleanVar(value=bool(self.cfg.get("verbose_log", False)))
        ttk.Checkbutton(i2, text="详细日志", variable=self.v_verbose,
                        command=self._on_verbose).pack(side="left", padx=(S(14), 0))

        # ---- 按钮 ----
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=pad, pady=(S(9), 0))
        self.b_check = ttk.Button(bar, text="环境自检", command=self.on_check)
        self.b_check.pack(side="left")
        self.b_start = ttk.Button(bar, text="▶ 开始扫描", style="Go.TButton",
                                  command=self.on_start)
        self.b_start.pack(side="left", padx=(S(7), 0))
        self.b_stop = ttk.Button(bar, text="■ 停止", command=self.on_stop,
                                 state="disabled")
        self.b_stop.pack(side="left", padx=(S(5), 0))
        ttk.Button(bar, text="打开结果", command=self.on_open_out).pack(side="right")
        ttk.Button(bar, text="数据目录", command=self.on_open_dir).pack(
            side="right", padx=(0, S(5)))

        # ---- 进度 ----
        pf = tk.Frame(self.root, bg=BG)
        pf.pack(fill="x", padx=pad, pady=(S(9), 0))
        self.pb = ttk.Progressbar(pf, mode="determinate", maximum=100)
        self.pb.pack(fill="x")
        self.lb_stat = tk.Label(pf, text="等待开始", bg=BG, fg=DIM, font=(UI, 8),
                                anchor="w")
        self.lb_stat.pack(fill="x", pady=(S(2), 0))

        # ---- 日志 ----
        lf = tk.Frame(self.root, bg=CARD, highlightbackground=LINE,
                      highlightthickness=S(1))
        lf.pack(fill="both", expand=True, padx=pad, pady=(S(7), S(12)))
        box = tk.Frame(lf, bg=LOGBG)
        box.pack(fill="both", expand=True, padx=S(1), pady=S(1))
        self.txt = tk.Text(box, bg=LOGBG, fg=FG, insertbackground=FG,
                           font=(MONO, 9), wrap="none", relief="flat",
                           padx=S(7), pady=S(5), height=14,
                           highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(box, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        for tag, color in (("ok", OKC), ("err", ERRC), ("warn", WARNC),
                           ("dim", DIM), ("acc", ACC)):
            self.txt.tag_configure(tag, foreground=color)

    def _row(self, parent, row, label, var, pick_cb, manual_cb, hint,
             proc_cb=None):
        tk.Label(parent, text=label, bg=CARD, fg=FG,
                 font=(UI, 9)).grid(row=row, column=0, sticky="w", pady=S(2))
        e = ttk.Entry(parent, textvariable=var, font=(UI, 9))
        e.grid(row=row, column=1, sticky="we", padx=(S(9), S(6)), pady=S(2))
        bf = tk.Frame(parent, bg=CARD)
        bf.grid(row=row, column=2, pady=S(2))
        ttk.Button(bf, text=("另存为" if proc_cb is None else "选文件"),
                   command=pick_cb).pack(side="left")
        if manual_cb:
            ttk.Button(bf, text="✎ 手动", command=manual_cb).pack(side="left",
                                                              padx=(S(4), 0))
        if proc_cb:
            ttk.Button(bf, text="⚙ 处理", command=proc_cb).pack(side="left",
                                                            padx=(S(4), 0))
        lb = tk.Label(parent, text=hint, bg=CARD, fg=DIM, font=(UI, 8))
        lb.grid(row=row, column=3, sticky="w", padx=(S(9), 0))
        parent.grid_columnconfigure(1, weight=1)
        return lb

    # ------------------------------------------------------------ 手动输入
    def _manual_dialog(self, title, hint, initial, on_ok):
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.configure(bg=BG)
        self._size_dialog(dlg, 560, 380)
        dlg.transient(self.root)
        dlg.grab_set()

        tk.Label(dlg, text=title, bg=BG, fg=FG,
                 font=(UI, 11, "bold")).pack(anchor="w", padx=S(14), pady=(S(12), S(2)))
        tk.Label(dlg, text=hint, bg=BG, fg=DIM, justify="left",
                 font=(UI, 9)).pack(anchor="w", padx=S(14))

        box = tk.Frame(dlg, bg="#ffffff", highlightbackground=LINE,
                       highlightthickness=S(1))
        box.pack(fill="both", expand=True, padx=S(14), pady=S(7))
        txt = tk.Text(box, bg="#ffffff", fg=FG, insertbackground=FG,
                      font=(MONO, 10), wrap="none", relief="flat",
                      padx=S(7), pady=S(5), undo=True,
                      highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(box, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", initial or "")
        txt.focus_set()

        cnt = tk.Label(dlg, text="", bg=BG, fg=DIM, font=(UI, 9))
        cnt.pack(anchor="w", padx=S(14))

        def refresh(*_a):
            n = len([l for l in txt.get("1.0", "end").splitlines()
                     if l.strip() and not l.strip().startswith("#")])
            cnt.config(text=f"已识别 {n} 行")

        txt.bind("<KeyRelease>", refresh)
        txt.bind("<<Paste>>", lambda e: dlg.after(60, refresh))
        refresh()

        def ok():
            on_ok(txt.get("1.0", "end"))
            dlg.destroy()

        bar = tk.Frame(dlg, bg=BG)
        bar.pack(fill="x", padx=S(14), pady=(0, S(12)))
        ttk.Button(bar, text="确定并保存", style="Go.TButton",
                   command=ok).pack(side="right")
        ttk.Button(bar, text="取消", command=dlg.destroy).pack(side="right",
                                                            padx=(0, S(8)))
        tk.Label(bar, text="可直接从 Excel/记事本粘贴", bg=BG, fg=DIM,
                 font=(UI, 8)).pack(side="left")
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.bind("<Control-Return>", lambda e: ok())

    def _save_manual(self, fname, text, hint_widget, unit):
        lines = [l.rstrip() for l in text.splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        if not lines:
            messagebox.showwarning("没内容", "至少填一行。")
            return None
        p = os.path.join(app_config.WORK_DIR, fname)
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self._log(f"手动输入已保存（{len(lines)} {unit}）", "ok")
        if hint_widget:
            hint_widget.config(text=f"✅ 手动 {len(lines)} {unit}", fg=OKC)
        return p

    def _manual_accounts(self):
        cur = self._read_current(self.v_acc.get())
        self._manual_dialog(
            "手动输入账号",
            "一行一个：账号----密码\n"
            "例：<ACCOUNT>----<PASSWORD>\n"
            "空行和 # 开头的行会被忽略。",
            cur,
            lambda t: (lambda s: s and self.v_acc.set(s))(
                self._save_manual("手动账号.txt", t, self.acc_hint, "个账号")))

    def _manual_proxy(self):
        cur = self._read_current(self.v_pxy.get())
        self._manual_dialog(
            "手动输入代理 IP",
            "一行一条：host:port:用户名:密码\n"
            "例：us.proxy.example:3010:user-region-JP-sid-XXXX-t-5:password\n"
            "留空则不用代理池。",
            cur,
            lambda t: (lambda s: s and self.v_pxy.set(s))(
                self._save_manual("手动代理.txt", t, self.pxy_hint, "条代理")))

    @staticmethod
    def _read_current(p):
        if p and os.path.exists(p):
            try:
                return open(p, encoding="utf-8-sig", errors="replace").read()
            except Exception:
                return ""
        return ""

    # ------------------------------------------------------------ 体检 / 查重 / 存活
    def _process_accounts(self):
        import input_tools as IT
        text = self._read_current(self.v_acc.get())
        if not text.strip():
            messagebox.showinfo("没有内容", "先「✎ 手动」或「选文件」给账号。")
            return
        self._log("账号体检 …", "acc")
        rep = IT.analyze_accounts(text)
        self._log(f"  共 {rep['raw_lines']} 行 → 有效 {rep['valid']} / "
                  f"格式错 {rep['invalid']} / 输入内重复 {rep['dup_in_input']}")
        self._log(f"  去重后唯一 {rep['unique']}，本地已跑过 {rep['already_run']}，"
                  f"待跑 {rep['fresh']}", "ok" if rep["fresh"] else "warn")
        self._account_dialog(rep, IT)

    def _account_dialog(self, rep, IT):
        dlg = tk.Toplevel(self.root)
        dlg.title("账号体检 / 查重")
        dlg.configure(bg=BG)
        self._size_dialog(dlg, 560, 430)
        dlg.transient(self.root)
        dlg.grab_set()

        tk.Label(dlg, text="账号体检", bg=BG, fg=FG,
                 font=(UI, 11, "bold")).pack(anchor="w", padx=S(14), pady=(S(12), S(6)))

        body = tk.Frame(dlg, bg=BG)
        body.pack(fill="x", padx=S(14))

        def kv(k, v, color=FG, bold=False):
            r = tk.Frame(body, bg=BG)
            r.pack(fill="x", pady=S(1))
            tk.Label(r, text=k, bg=BG, fg=DIM, font=(UI, 9), width=16,
                     anchor="w").pack(side="left")
            tk.Label(r, text=str(v), bg=BG, fg=color,
                     font=(UI, 9, "bold") if bold else (UI, 9),
                     anchor="w").pack(side="left")

        kv("总行数", rep["raw_lines"])
        kv("格式正确", rep["valid"], OKC)
        kv("格式错误", rep["invalid"], ERRC if rep["invalid"] else DIM)
        kv("输入内重复", rep["dup_in_input"], WARNC if rep["dup_in_input"] else DIM)
        tk.Frame(body, bg=LINE, height=S(1)).pack(fill="x", pady=S(5))
        kv("去重后唯一", rep["unique"])
        kv("本地已跑过", rep["already_run"], DIM)
        kv("★ 待跑（新）", rep["fresh"], OKC, True)
        if rep["pwd_conflict"]:
            kv("同名不同密码", len(rep["pwd_conflict"]), WARNC)

        if rep["invalid_samples"]:
            tk.Label(dlg, text="格式错误的行（前几条）:", bg=BG, fg=DIM,
                     font=(UI, 8)).pack(anchor="w", padx=S(14), pady=(S(8), S(2)))
            box = tk.Frame(dlg, bg="#fff", highlightbackground=LINE,
                           highlightthickness=S(1))
            box.pack(fill="x", padx=S(14))
            tk.Label(box, text="\n".join(rep["invalid_samples"])[:300], bg="#fff",
                     fg=ERRC, font=(MONO, 8), justify="left",
                     anchor="w").pack(fill="x", padx=S(6), pady=S(4))

        opt = tk.Frame(dlg, bg=BG)
        opt.pack(fill="x", padx=S(14), pady=(S(10), 0))
        v_skip = tk.BooleanVar(value=True)
        v_dedup = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="去掉本地已跑过的账号",
                        variable=v_skip).pack(anchor="w")
        ttk.Checkbutton(opt, text="输入内去重（同名只留第一条）",
                        variable=v_dedup).pack(anchor="w")
        tk.Label(dlg, text=f"本地历史：{IT.SEEN_ACCOUNTS}", bg=BG, fg=DIM,
                 font=(UI, 8)).pack(anchor="w", padx=S(14), pady=(S(8), 0))

        def do():
            items = rep["items"]
            if v_dedup.get():
                pass                       # analyze 已去重
            txt, keys = IT.build_accounts_text(items, skip_seen=v_skip.get())
            n = len([l for l in txt.splitlines() if l.strip()])
            if not n:
                messagebox.showwarning("没有可跑的",
                                       "去重后没有新账号了（都跑过）。\n"
                                       "可以取消勾选「去掉已跑过的」重跑一遍。")
                return
            p = os.path.join(app_config.WORK_DIR, "账号_已处理.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write(txt)
            IT.append_seen(IT.SEEN_ACCOUNTS, keys)
            IT.write_report("账号体检", [
                f"总行 {rep['raw_lines']}  有效 {rep['valid']}  "
                f"格式错 {rep['invalid']}  输入内重复 {rep['dup_in_input']}",
                f"唯一 {rep['unique']}  已跑过 {rep['already_run']}  待跑 {rep['fresh']}",
                f"本次写入 {n} 条 -> {p}"])
            self.v_acc.set(p)
            self.acc_hint.config(text=f"✅ 待跑 {n} 个", fg=OKC)
            self._log(f"  一键处理完成：写入 {n} 条到 {p}", "ok")
            self._log(f"  已记入本地历史（累计 "
                      f"{IT.seen_stats()['accounts']} 个账号）", "dim")
            dlg.destroy()

        bar = tk.Frame(dlg, bg=BG)
        bar.pack(fill="x", padx=S(14), pady=(S(12), S(12)), side="bottom")
        ttk.Button(bar, text="一键处理并保存", style="Go.TButton",
                   command=do).pack(side="right")
        ttk.Button(bar, text="取消", command=dlg.destroy).pack(side="right",
                                                            padx=(0, S(8)))

    def _process_proxies(self):
        import input_tools as IT
        text = self._read_current(self.v_pxy.get())
        if not text.strip():
            messagebox.showinfo("没有内容", "先「✎ 手动」或「选文件」给代理。")
            return
        self._log("代理体检 …", "acc")
        rep = IT.analyze_proxies(text)
        self._log(f"  共 {rep['raw_lines']} 条 → 格式正确 {rep['valid']} / "
                  f"格式错 {rep['invalid']} / 输入内重复 {rep['dup_in_input']}")
        self._log(f"  去重后唯一 {rep['unique']}，历史用过 {rep['already_used']}")
        self._proxy_dialog(rep, IT)

    def _proxy_dialog(self, rep, IT):
        dlg = tk.Toplevel(self.root)
        dlg.title("代理体检 / 查重 / 存活检测")
        dlg.configure(bg=BG)
        self._size_dialog(dlg, 580, 470)
        dlg.transient(self.root)
        dlg.grab_set()

        tk.Label(dlg, text="代理体检", bg=BG, fg=FG,
                 font=(UI, 11, "bold")).pack(anchor="w", padx=S(14), pady=(S(12), S(6)))

        body = tk.Frame(dlg, bg=BG)
        body.pack(fill="x", padx=S(14))

        def kv(k, v, color=FG, bold=False):
            r = tk.Frame(body, bg=BG)
            r.pack(fill="x", pady=S(1))
            tk.Label(r, text=k, bg=BG, fg=DIM, font=(UI, 9), width=16,
                     anchor="w").pack(side="left")
            lbl = tk.Label(r, text=str(v), bg=BG, fg=color,
                           font=(UI, 9, "bold") if bold else (UI, 9), anchor="w")
            lbl.pack(side="left")
            return lbl

        kv("总条数", rep["raw_lines"])
        kv("格式正确", rep["valid"], OKC)
        kv("格式错误", rep["invalid"], ERRC if rep["invalid"] else DIM)
        kv("输入内重复", rep["dup_in_input"], WARNC if rep["dup_in_input"] else DIM)
        tk.Frame(body, bg=LINE, height=S(1)).pack(fill="x", pady=S(5))
        kv("去重后唯一", rep["unique"], FG, True)
        kv("历史用过", rep["already_used"], DIM)
        lbl_alive = kv("存活", "— 还没测", DIM)

        pb = ttk.Progressbar(dlg, mode="determinate", maximum=100)
        pb.pack(fill="x", padx=S(14), pady=(S(8), 0))
        lbl_p = tk.Label(dlg, text="", bg=BG, fg=DIM, font=(UI, 8), anchor="w")
        lbl_p.pack(fill="x", padx=S(14))

        state = {"alive_items": None, "testing": False}

        def start_test():
            if state["testing"]:
                return
            state["testing"] = True
            b_test.config(state="disabled", text="检测中 …")
            lbl_alive.config(text="检测中 …", fg=WARNC)
            pb["value"] = 0

            def prog(d, t):
                try:
                    self.root.after(0, lambda: (
                        pb.config(value=d * 100 / max(1, t)),
                        lbl_p.config(text=f"{d}/{t}")))
                except Exception:
                    pass

            # ★ 主线程先读出来（工作线程读 Tk 变量会抛 main thread is not in main loop）
            _pxy_text = self._read_current(self.v_pxy.get())

            def work():
                r = IT.analyze_proxies(
                    _pxy_text, check_alive=True,
                    workers=12, timeout=18, progress_cb=prog)
                state["alive_items"] = r.get("alive_items")
                n_a, n_d = r.get("alive") or 0, r.get("dead") or 0
                try:
                    self.root.after(0, lambda: (
                        lbl_alive.config(text=f"✓ {n_a}    ✗ 失效 {n_d}",
                                         fg=OKC if n_a else ERRC),
                        b_test.config(state="normal", text="重新检测"),
                        lbl_p.config(text=f"检测完成（{n_a} 条可用）")))
                except Exception:
                    pass
                self._log(f"  存活检测完成：可用 {n_a} / 失效 {n_d}", "ok")
                IT.write_report("代理体检", [
                    f"总 {rep['raw_lines']}  正确 {rep['valid']}  错 {rep['invalid']}  "
                    f"重复 {rep['dup_in_input']}  唯一 {rep['unique']}",
                    f"存活 {n_a}  失效 {n_d}"])

            threading.Thread(target=work, daemon=True).start()

        opt = tk.Frame(dlg, bg=BG)
        opt.pack(fill="x", padx=S(14), pady=(S(8), 0))
        v_dedup = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="输入内去重（同 host:port:用户 只留一条）",
                        variable=v_dedup).pack(anchor="w")
        v_only_alive = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="只保留存活的（要先点存活检测）",
                        variable=v_only_alive).pack(anchor="w")

        btns = tk.Frame(dlg, bg=BG)
        btns.pack(fill="x", padx=S(14), pady=(S(6), 0))
        b_test = ttk.Button(btns, text="① 存活检测", command=start_test)
        b_test.pack(side="left")

        def do():
            items = rep["items"]
            if v_only_alive.get():
                if state["alive_items"] is None:
                    messagebox.showinfo("还没测", "先点「① 存活检测」。")
                    return
                items = state["alive_items"]
                if not items:
                    messagebox.showwarning("全挂了", "存活检测结果为空。")
                    return
            txt, keys = IT.build_proxies_text(items, mark_used=True)
            p = os.path.join(app_config.WORK_DIR, "代理_已处理.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write(txt)
            n = len([l for l in txt.splitlines() if l.strip()])
            self.v_pxy.set(p)
            self.pxy_hint.config(text=f"✅ {n} 条可用", fg=OKC)
            self._log(f"  一键处理完成：写入 {n} 条到 {p}", "ok")
            dlg.destroy()

        bar = tk.Frame(dlg, bg=BG)
        bar.pack(fill="x", padx=S(14), pady=(S(12), S(12)), side="bottom")
        ttk.Button(bar, text="② 一键处理并保存", style="Go.TButton",
                   command=do).pack(side="right")
        ttk.Button(bar, text="取消", command=dlg.destroy).pack(side="right",
                                                            padx=(0, S(8)))
        tk.Label(bar, text=f"历史：{IT.SEEN_PROXIES}", bg=BG, fg=DIM,
                 font=(UI, 8)).pack(side="left")

    # ------------------------------------------------------------ 文件选择
    def _pick_accounts(self):
        p = filedialog.askopenfilename(title="选择账号文件",
                                       filetypes=[("文本文件", "*.txt"),
                                                  ("所有文件", "*.*")])
        if p:
            self.v_acc.set(p)
            n = sum(1 for ln in open(p, encoding="utf-8-sig", errors="replace")
                    if ln.strip() and not ln.strip().startswith("#"))
            self.acc_hint.config(text=f"✅ {n} 个账号", fg=OKC)

    def _pick_proxy(self):
        p = filedialog.askopenfilename(title="选择代理文件",
                                       filetypes=[("文本文件", "*.txt"),
                                                  ("所有文件", "*.*")])
        if p:
            self.v_pxy.set(p)
            n = sum(1 for ln in open(p, encoding="utf-8-sig", errors="replace")
                    if ln.strip() and not ln.strip().startswith("#"))
            self.pxy_hint.config(text=f"✅ {n} 条代理", fg=OKC)

    def _pick_out(self):
        p = filedialog.asksaveasfilename(title="结果保存到", defaultextension=".txt",
                                         initialfile="result.txt",
                                         filetypes=[("文本文件", "*.txt")])
        if p:
            self.v_out.set(p)

    # ------------------------------------------------------------ 代理 API 提取
    def _fetch_api(self):
        """粘贴提取网址 -> 拉取 -> 探协议 -> 测存活 -> 按红线过滤 -> 落盘

        国内代理常见「IP 白名单」：只有授权过的来源 IP 才能用。
        我们的请求如果走隧道（外国 IP），代理商那边会静默丢弃 ——
        实测表现是「TCP 连得上，但协议层一个字都不回」。
        所以拉取失败时要把这个原因说清楚。
        """
        url = self.v_api.get().strip()
        if not url:
            messagebox.showinfo("还没填", "把代理提取网址粘到「代理API」框里。")
            return
        if not url.lower().startswith(("http://", "https://")):
            messagebox.showwarning("网址不对", "要以 http:// 或 https:// 开头。")
            return

        self._log("─" * 44, "dim")
        self._log("从 API 拉取代理 …", "acc")
        self.api_hint.config(text="拉取中 …", fg=WARNC)
        _reject_cn = True      # ★ 红线：国内出口一律拒收

        def work():
            try:
                import proxy_pool as PP
                r = PP.build_from_api(url, workers=20, timeout=12,
                                      reject_cn=_reject_cn, verbose=False,
                                      direct=bool(self.v_api_direct.get()))
                good, cn, dead = r["good"], r["cn"], r["dead"]
                self.q.put(f"   拉到 {r['total']} 条：可用 {len(good)}  "
                           f"国内(拒收) {len(cn)}  失效 {len(dead)}")
                if cn:
                    self.q.put(f"   ★ 已按红线拒收 {len(cn)} 条国内出口")
                if good:
                    lines = [f"{e['host']}:{e['port']}" for e in good]
                    p = os.path.join(app_config.WORK_DIR, "API代理_已处理.txt")
                    with io.open(p, "w", encoding="utf-8") as f:
                        f.write("\n".join(lines) + "\n")
                    self.q.put(f"   ✅ 可用 {len(good)} 条已存到 {p}")
                    self.root.after(0, lambda: self.v_pxy.set(p))
                    self.root.after(0, lambda: self.pxy_hint.config(
                        text=f"✅ API 拉到 {len(good)} 条", fg=OKC))
                elif dead and not cn:
                    self.q.put("   ❌ 一条都不可用。最可能的原因：")
                    self.q.put("      · 代理商开了 IP 白名单，而我们的请求走隧道"
                               "（外国 IP）→ 被静默丢弃")
                    self.q.put("      · 表现：TCP 能连上，但协议层不应答")
                    self.q.put("      · 处理：在代理商后台把「本机公网 IP」加进白名单，"
                               "或让本工具直连（不走隧道）")
                    self.q.put("      · 也可先在浏览器直接打开那个网址，"
                               "看能不能返回 ip:port 列表")
                self.q.put("   拉取结束")
            except Exception as e:
                self.q.put(f"   ❌ 拉取失败: {type(e).__name__}: {str(e)[:120]}")
            finally:
                try:
                    self.root.after(0, lambda: self.api_hint.config(
                        text="粘贴代理提取网址，一键拉取", fg=DIM))
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ 日志
    def _log(self, s, tag=None):
        self.txt.insert("end", f"[{time.strftime('%H:%M:%S')}] {s}\n", tag or "")
        self.txt.see("end")
        if int(self.txt.index("end-1c").split(".")[0]) > 4000:
            self.txt.delete("1.0", "800.0")

    # 这些是内部过程日志，默认不显示（勾「详细日志」才看）
    NOISE = (
        re.compile(r"\[并发\] 第\d+轮"),
        re.compile(r"\[vanguard\] (命中机器人校验|第 \d+/\d+ 次|getCaptcha)"),
        re.compile(r"^\s*\[链式\]"),
        re.compile(r"\[ocr\]"),
        re.compile(r"^\s*\[pool\]"),
    )

    def _on_verbose(self):
        self._log("已切换为「详细日志」—— 会显示内部过程" if self.v_verbose.get()
                  else "已切换为「简洁日志」—— 只看账号级进度", "dim")

    def _is_noise(self, s):
        if self.v_verbose.get():
            return False
        return any(p.search(s) for p in self.NOISE)

    def _drain(self):
        """把日志队列里的东西搬进界面。

        ★ 必须是铁打的：任何一条日志处理失败都不能让这个循环死掉，
          否则日志和进度条会永久停住（实测踩过）。
        """
        try:
            while True:
                try:
                    s = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    if self._is_noise(s):
                        self._parse(s)          # 噪声也统计，只是不显示
                        continue
                    tag = None
                    if ("✗" in s or "❌" in s or "!!" in s or "失败" in s
                            or "裸奔" in s):
                        tag = "err"
                    elif "✓" in s or "✅" in s:
                        tag = "ok"
                    elif "校验" in s or "警告" in s or "暂停" in s or "重试" in s:
                        tag = "warn"
                    elif s.strip().startswith(("—", "[")):
                        tag = "dim"
                    self._log(s, tag)
                    self._parse(s)
                except Exception as e:
                    # 单条失败只记一笔，绝不中断循环
                    try:
                        self._log(f"[日志处理异常] {type(e).__name__}: "
                                  f"{str(e)[:80]}", "err")
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try:
                self.root.after(120, self._drain)
            except Exception:
                pass

    def _parse(self, s):
        """从日志里解析进度与统计。

        认这几类行：
          [3/12] ✓ 账号...         -> 成功 +1
          [3/12] ✗ 账号... 原因    -> 失败 +1
          —— 进度 5/12  已用 x 分 预计剩余 y 分（成功 a 失败 b）
          可用 44/50                -> 代理池
          失败原因分布              -> 分诊
        """
        dirty = False
        m = re.search(r"\[(\d+)/(\d+)\]\s+([✓✗])", s)
        if m:
            self.stat["idx"] = int(m.group(1))
            self.stat["total"] = int(m.group(2))
            if m.group(3) == "✓":
                self.stat["ok"] += 1
            else:
                self.stat["fail"] += 1
                r = re.search(r"✗\s+\S+\s+(\S+)", s)
                if r:
                    k = r.group(1)[:24]
                    self.stat["reasons"][k] = self.stat["reasons"].get(k, 0) + 1
            dirty = True

        m = re.search(r"—— 进度 (\d+)/(\d+)\s+已用 ([\d.]+) 分\s+预计剩余 ([\d.]+) 分", s)
        if m:
            self.stat["done"] = int(m.group(1))
            self.stat["total"] = int(m.group(2))
            self.stat["elapsed_min"] = float(m.group(3))
            self.stat["eta_min"] = float(m.group(4))
            dirty = True

        m = re.search(r"\[输入\] (\d+) 个账号", s)
        if m:
            self.stat["input"] = int(m.group(1))
        m = re.search(r"\[待跑\] (\d+) 个（跳过已完成 (\d+) 个）", s)
        if m:
            self.stat["total"] = int(m.group(1))
            self.stat["skip"] = int(m.group(2))
            self.stat["t0"] = time.time()
            dirty = True

        m = re.search(r"可用 (\d+)/(\d+)", s)
        if m:
            self.pxy_hint.config(text=f"✅ {m.group(1)}/{m.group(2)} 条可用", fg=OKC)

        if dirty:
            self._update_status()

    def _tick_status(self):
        """扫描期间定时刷新状态栏/进度条 —— 不依赖日志是否及时到达"""
        try:
            if self.running:
                self._update_status()
        except Exception:
            pass
        finally:
            try:
                self.root.after(500, self._tick_status)
            except Exception:
                pass

    def _update_status(self):
        st = self.stat
        total = st.get("total") or 0
        done = st.get("ok", 0) + st.get("fail", 0)
        st["done"] = done
        pct = (done * 100 / total) if total else 0
        self.pb["value"] = min(100, pct)

        # 时速
        el = time.time() - st["t0"] if st.get("t0") else 0
        rate = (done / el * 3600) if el > 5 and done else 0
        st["rate"] = rate

        parts = [f"{done}/{total}  ({pct:.0f}%)"]
        parts.append(f"成功 {st['ok']}")
        parts.append(f"失败 {st['fail']}")
        if st.get("skip"):
            parts.append(f"跳过 {st['skip']}")
        if rate:
            parts.append(f"时速 {rate:.0f}/时")
        if el:
            parts.append(f"已用 {el/60:.1f} 分")
        eta = st.get("eta_min")
        if eta and eta > 0 and done < total:
            parts.append(f"剩余约 {eta:.1f} 分")

        txt = "   ".join(parts)
        if st["reasons"]:
            top = sorted(st["reasons"].items(), key=lambda x: -x[1])[:3]
            txt += "   |   " + "  ".join(f"{k}×{v}" for k, v in top)
        self.lb_stat.config(text=txt)

    # ------------------------------------------------------------ 配置
    def _save_cfg(self):
        self.cfg.update({
            "accounts_file": self.v_acc.get().strip(),
            "proxy_file": self.v_pxy.get().strip(),
            "out_file": self.v_out.get().strip(),
            "game": self.v_game.get(),
            "parallel": int(self.v_par.get() or 8),
            "rate": float(self.v_rate.get() or 0.8),
            "retries": int(self.v_retry.get() or 3),
            "verbose_log": bool(self.v_verbose.get()),
            "proxy_api": self.v_api.get().strip(),
            "api_direct": bool(self.v_api_direct.get()),
            "use_proxy_pool": bool(self.v_usepool.get()),
        })
        app_config.save(self.cfg)

    def _validate(self):
        a = self.v_acc.get().strip()
        if not a or not os.path.exists(a):
            messagebox.showwarning("缺少账号", "请先「手动」或「选文件」给账号。")
            return False
        p = self.v_pxy.get().strip()
        if self.v_usepool.get() and p and not os.path.exists(p):
            messagebox.showwarning("代理文件不存在", "代理文件路径无效。")
            return False
        if not self.v_out.get().strip():
            messagebox.showwarning("缺少结果文件", "请指定结果保存位置。")
            return False
        return True

    # ------------------------------------------------------------ 动作
    def on_check(self):
        self.b_check.config(state="disabled")
        self._log("─" * 44, "dim")
        self._log("环境自检 …", "acc")

        # ★ 在主线程先把 Tk 变量读出来。
        #   工作线程里读 Tk 变量会抛
        #   RuntimeError: main thread is not in main loop
        #   —— 这就是「每次都报 自检异常」的根因。
        _pf = self.v_pxy.get().strip()
        _usepool = bool(self.v_usepool.get())

        def work():
            try:
                import ocr_service, egress_guard, proxy_pool
                self.q.put("① 识别端")
                ok, msg = ocr_service.check_vcomp()
                if not ok:
                    self.q.put(f"   ❌ {msg}")
                elif ocr_service.port_open():
                    self.q.put("   ✅ 识别服务已在监听")
                else:
                    self.q.put("   正在拉起识别服务 …")
                    try:
                        ocr_service.start(verbose=False)
                        self.q.put("   ✅ 识别服务已启动")
                    except Exception as e:
                        self.q.put(f"   ❌ 起不来: {e}")

                self.q.put("② 出口红线")
                try:
                    eg = egress_guard.egressguard_status()
                    self.q.put(f"   闸门 {eg['mode']} 启用={eg['enabled']} "
                               f"隧道={eg['tunnel_ok']}" if eg
                               else "   未检测到 EgressGuard 闸门")
                except Exception as e:
                    self.q.put(f"   闸门查询失败: {e}")
                try:
                    tb = egress_guard.check_tunnel_binding(verbose=False)
                    for r in tb["results"]:
                        if r.get("local_ip"):
                            self.q.put(f"   {r['target']} → {r['local_ip']} "
                                       + ("✅ 隧道内" if r["in_tunnel"]
                                          else "❌ 裸奔(物理网卡)"))
                    self.q.put("   ✅ 出站全部走隧道" if tb["ok"]
                               else "   ❌ 有连接裸奔 —— 会暴露昆明 IP！")
                except Exception as e:
                    self.q.put(f"   隧道自查失败: {e}")
                try:
                    info = egress_guard.preflight(verbose=False, check_tunnel=False)
                    self.q.put(f"   ✅ 出口 {info['ip']} ({info.get('country_name')} "
                               f"{info.get('city')})")
                except Exception as e:
                    self.q.put(f"   ❌ {e}")

                self.q.put("③ 代理池")
                # ★ 用主线程提前抓下来的值 —— 工作线程里读 Tk 变量会抛
                #   RuntimeError: main thread is not in main loop
                if _usepool and _pf:
                    pool = proxy_pool.build(_pf, workers=10, verbose=False)
                    st = pool.stats()
                    self.q.put(f"   可用 {st['good']}/{st['total']} 条，"
                               f"唯一 IP {st['unique_ips']} 个，"
                               f"中位延迟 {st['median_ms']}ms")
                    cn = [e for e in pool.good if (e.get("cc") or "").upper() == "CN"]
                    self.q.put("   ❌ 池里有国内出口，拒绝使用" if cn
                               else "   ✅ 全部境外出口")
                else:
                    self.q.put("   未启用代理池，走默认出口")

                self.q.put("自检结束。都 ✅ 就可以开始了。")
            except Exception as e:
                self.q.put(f"❌ 自检异常: {type(e).__name__}: {e}")
            finally:
                try:
                    self.root.after(0,
                                    lambda: self.b_check.config(state="normal"))
                except Exception:
                    pass   # 窗口已销毁时会抛 main thread is not in main loop

        threading.Thread(target=work, daemon=True).start()

    def on_start(self):
        if self.running or not self._validate():
            return
        self._save_cfg()
        self.running = True
        self.scan_finished = False
        self.stat = {"ok": 0, "fail": 0, "done": 0, "total": 0, "t0": time.time(),
                     "skip": 0, "input": 0, "rate": 0, "eta_min": 0,
                     "elapsed_min": 0, "idx": 0, "reasons": {}}
        self.pb["value"] = 0
        self.b_start.config(state="disabled")
        self.b_stop.config(state="normal")
        self._log("─" * 44, "dim")
        self._log("开始扫描 …", "acc")

        args = ["--out", self.v_out.get().strip(), "--game", self.v_game.get(),
                "--parallel", self.v_par.get().strip() or "8",
                "--workers", "3", "--rate", self.v_rate.get().strip() or "0.8",
                "--retries", self.v_retry.get().strip() or "3"]
        pf = self.v_pxy.get().strip()
        if self.v_usepool.get() and pf:
            args += ["--proxy-file", pf]
        args.append(self.v_acc.get().strip())

        def work():
            try:
                import wm_scan
                wm_scan.LOG_SINK = lambda s: self.q.put(s)
                ns = wm_scan.build_parser().parse_args(args)
                rc = wm_scan.run_scan(ns)
                self.q.put(f"扫描结束（退出码 {rc}）")
            except SystemExit:
                pass
            except Exception as e:
                self.q.put(f"❌ 运行异常: {type(e).__name__}: {e}")
            finally:
                # 直接赋值：跨线程调 root.after 不可靠，E2E 会一直等下去
                self.running = False
                self.scan_finished = True
                try:
                    self.root.after(0, self._reset_buttons)
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True).start()

    def _reset_buttons(self):
        self.b_start.config(state="normal")
        self.b_stop.config(state="disabled")
        st = self.stat
        el = time.time() - (st["t0"] or time.time())
        txt = (f"已结束   成功 {st['ok']}   失败 {st['fail']}   "
               f"跳过 {st.get('skip', 0)}   用时 {el/60:.1f} 分")
        if st.get("rate"):
            txt += f"   平均时速 {st['rate']:.0f}/时"
        if st["reasons"]:
            txt += "   |   " + "  ".join(
                f"{k}×{v}" for k, v in
                sorted(st["reasons"].items(), key=lambda x: -x[1])[:4])
        self.lb_stat.config(text=txt)
        self._log("─" * 44, "dim")
        self._log(f"本轮汇总：成功 {st['ok']}  失败 {st['fail']}  "
                  f"跳过 {st.get('skip', 0)}  用时 {el/60:.1f} 分",
                  "ok" if st["ok"] else "warn")
        if st["reasons"]:
            for k, v in sorted(st["reasons"].items(), key=lambda x: -x[1]):
                self._log(f"    失败原因 {k} × {v}", "err")

    def on_stop(self):
        if not self.running:
            return
        if not messagebox.askyesno("确认停止",
                                   "未完成的账号会留在进度里，下次可续跑。确定停止吗？"):
            return
        try:
            import wm_scan
            wm_scan.STOP_EVENT.set()
            self._log("已发出停止指令 …", "warn")
        except Exception as e:
            self._log(f"停止失败: {e}", "err")

    def on_open_out(self):
        p = self.v_out.get().strip()
        if p and os.path.exists(p):
            os.startfile(p)
        else:
            messagebox.showinfo("还没结果", "结果文件还不存在，先跑一次吧。")

    def on_open_dir(self):
        os.startfile(app_config.app_dir())

    def _on_close(self):
        if self.running:
            if not messagebox.askyesno("正在运行", "扫描还在进行，确定退出吗？"):
                return
            try:
                import wm_scan
                wm_scan.STOP_EVENT.set()
            except Exception:
                pass
        self.root.destroy()


# ================================================================ 命令行模式
def _install_excepthook():
    """★ 兜住所有没预料到的异常。

    打包成 --windowed 的 exe 后，任何未捕获异常都会**静默退出**，
    用户只看到窗口没了、什么提示都没有。这里统一记盘 + 弹窗。
    """
    def handle(exc_type, exc_value, exc_tb):
        import traceback
        txt = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        p = _crash_log(txt)
        try:
            print(txt, file=sys.stderr, flush=True)
        except Exception:
            pass
        try:
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(
                "程序遇到未预料的错误",
                f"{txt[-1200:]}\n\n完整日志已写入：\n{p}\n\n"
                f"把这个文件发给我就能定位。")
            r.destroy()
        except Exception:
            pass

    sys.excepthook = handle

    # Tk 回调里抛的异常走的是另一条路，单独接管
    def tk_hook(self, exc, val, tb):
        handle(exc, val, tb)

    try:
        tk.Tk.report_callback_exception = tk_hook
    except Exception:
        pass

    # 工作线程里的异常：★ 只记盘，绝不弹窗。
    # 弹模态框在无人值守（比如端到端自测）时会卡死整个进程。
    def thread_handle(a):
        import traceback
        txt = "".join(traceback.format_exception(a.exc_type, a.exc_value,
                                                 a.exc_traceback))
        _crash_log("[线程异常]\n" + txt)

    try:
        threading.excepthook = thread_handle
    except Exception:
        pass


def _crash_log(exc_text):
    try:
        p = os.path.join(app_config.app_dir(), "crash.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n{exc_text}\n")
        return p
    except Exception:
        return None


def run_selftest():
    lines = []

    def out(s):
        lines.append(str(s))
        try:
            print(s, flush=True)
        except Exception:
            pass

    out(f"=== 自检 {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    out(f"打包模式: {getattr(sys, 'frozen', False)}")
    out(wincompat.describe())
    # ---- DPI 诊断（打包版曾经算错，这里留证据）----
    try:
        import ctypes
        u = ctypes.windll.user32
        u.GetDpiForSystem.restype = ctypes.c_uint
        out(f"GetDpiForSystem : {u.GetDpiForSystem()}")
    except Exception as e:
        out(f"GetDpiForSystem : 失败 {e}")
    try:
        u.GetDpiForWindow.restype = ctypes.c_uint
        out(f"GetDpiForWindow(桌面): {u.GetDpiForWindow(u.GetDesktopWindow())}")
    except Exception as e:
        out(f"GetDpiForWindow : 失败 {e}")
    try:
        _r = tk.Tk(); _r.withdraw(); _r.update_idletasks()
        out(f"winfo_fpixels('1i'): {_r.winfo_fpixels('1i')}")
        out(f"GetDpiForWindow(本窗口): {u.GetDpiForWindow(_r.winfo_id())}")
        out(f"_real_dpi()     : {_real_dpi(_r)}")
        _r.destroy()
    except Exception as e:
        out(f"Tk DPI 探测失败: {e}")
    out(f"数据目录: {app_config.app_dir()}")
    out(f"识别端  : {app_config.default_ocr_dir()}")
    rc = 0
    try:
        import ocr_service
        out("\n[1] 识别端")
        ok, msg = ocr_service.check_vcomp()
        out(f"    vcomp140: {ok}  {msg}")
        if not ok:
            rc = 1
        if ocr_service.port_open():
            out("    服务已在监听 506")
        else:
            out("    正在拉起服务 ...")
            try:
                ocr_service.start(verbose=True)
                out("    OK 服务已启动")
            except Exception as e:
                out(f"    FAIL 起不来: {e}")
                rc = 1

        out("\n[2] 出口红线")
        import egress_guard
        try:
            tb = egress_guard.check_tunnel_binding(verbose=False)
            if tb.get("skipped"):
                out("    （没有代理文件，隧道绑定自查跳过 —— 不是出错，"
                    "配了代理文件就会查）")
            for r in tb["results"]:
                # ★ 三态，别用二态：local_ip=None 表示"根本没连上"（比如域名解析不了），
                #   这时候报"裸奔"是**反的** —— 什么都没连，哪来的泄漏。
                #   之前这里就因此把 `us.proxy.example`（脱敏占位符）报成"!! 裸奔"，
                #   自检看起来像出事了，其实是噪声。
                if r.get("local_ip") is None:
                    mark = "跳过(连不上，非裸奔)"
                elif r.get("in_tunnel"):
                    mark = "隧道内"
                else:
                    mark = "!! 裸奔(物理网卡)"
                out(f"    {r['target']} → {r.get('local_ip')} {mark}")
            if not tb["ok"]:
                rc = 1
            info = egress_guard.preflight(verbose=False, check_tunnel=False)
            out(f"    OK 出口 {info['ip']} ({info.get('country_name')})")
        except Exception as e:
            out(f"    FAIL {e}")
            rc = 1

        out("\n[3] 依赖")
        for m in ("requests", "Crypto", "socks", "tkinter"):
            try:
                __import__(m)
                out(f"    OK   {m}")
            except Exception as e:
                out(f"    FAIL {m}: {e}")
                rc = 1

        out("\n[4] 本地 OCR 实拍")
        try:
            import glob
            d = os.path.join(getattr(sys, "_MEIPASS", HERE), "samples")
            imgs = sorted(glob.glob(os.path.join(d, "*.jpg")))
            if imgs:
                r = ocr_service.ocr(open(imgs[0], "rb").read())
                out(f"    {os.path.basename(imgs[0])} -> {r[:60]}")
                if r.strip() in ("-1", ""):
                    out("    FAIL 没识别出坐标")
                    rc = 1
            else:
                out("    (没有样本图，跳过)")
        except Exception as e:
            out(f"    FAIL {type(e).__name__}: {e}")
            rc = 1
    except Exception:
        import traceback
        out(traceback.format_exc())
        rc = 1

    out("")
    out(f"=== 结论: {'全部通过' if rc == 0 else '有问题，见上'} ===")
    try:
        p = os.path.join(app_config.app_dir(), "selftest.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        out(f"日志: {p}")
    except Exception:
        pass
    return rc


def run_cli(argv):
    try:
        import wm_scan
        ns = wm_scan.build_parser().parse_args(argv)
        rc = wm_scan.run_scan(ns)
    except SystemExit as e:
        rc = e.code or 0
    except Exception:
        import traceback
        _crash_log(traceback.format_exc())
        rc = 1
    return rc


def run_e2e(argv):
    """★ 端到端测试：真实驱动界面走完「填表 → 环境自检 → 开始扫描 → 出结果」。

    不是绕开界面调底层 —— 走的就是 on_check() / on_start() 这两个按钮的处理函数，
    所以它验证的是**用户实际点击的那条路径**。
    """
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", required=True)
    ap.add_argument("--proxies", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=2400,
                    help="扫描等待上限（秒）—— 要覆盖自检+代理体检+真扫描")
    a = ap.parse_args(argv)

    lines = []

    def out(s):
        lines.append(str(s))
        try:
            print(s, flush=True)
        except Exception:
            pass

    rc = 0
    out("=" * 62)
    out("  端到端测试（真实驱动界面）")
    out("=" * 62)
    out(wincompat.describe())
    out("")

    def _write_report(final=True):
        """边跑边写：中途挂了也能看到已经走到哪一步"""
        try:
            p = os.path.join(app_config.WORK_DIR, "e2e_report.txt")
            with io.open(p, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            if final:
                out(f"报告: {p}")
        except Exception:
            pass

    def out_live(s):
        """★ 每次输出都落盘。

        之前做了 1 秒节流，结果长跑时关键步骤被吞掉，报告停在半路 ——
        报告只有几 KB，每次都写没有任何代价。
        """
        out(s)
        _write_report(final=False)

    root = None
    try:
        return _e2e_body(a, out_live, lines, _write_report)
    except Exception:
        import traceback
        tb = traceback.format_exc()
        out("")
        out("❌ 端到端测试自身抛异常：")
        out(tb)
        _crash_log(tb)
        _write_report()
        return 9


def _e2e_body(a, out, lines, _write_report):
    rc = 0
    root = tk.Tk()
    app = App(root)
    root.update()

    out(f"[1] 界面已建立  字体={UI}/{MONO}  DPI={app.dpi}  scale={_SCALE}")

    # --- 填表 ---
    app.v_acc.set(a.accounts)
    if a.proxies:
        app.v_pxy.set(a.proxies)
        app.v_usepool.set(True)
    out_path = a.out or os.path.join(app_config.WORK_DIR, "e2e_result.txt")
    app.v_out.set(out_path)
    app.v_par.set(str(a.parallel))
    root.update()
    out(f"[2] 已填表  账号={os.path.basename(a.accounts)}  "
        f"代理={os.path.basename(a.proxies) if a.proxies else '无'}  "
        f"结果={os.path.basename(out_path)}")

    def pump(seconds, until=None):
        """跑 Tk 事件循环，直到条件满足或超时"""
        t0 = time.time()
        while time.time() - t0 < seconds:
            root.update()
            if until and until():
                return True
            time.sleep(0.05)
        return bool(until and until())

    # --- 环境自检 ---
    # ★ 不依赖控件状态（跨线程改 UI 状态不可靠）：点了之后固定等一段时间，
    #   期间把 Tk 事件循环跑起来，让自检线程的日志能进界面。
    out("[3] 点「环境自检」…")
    app.on_check()
    pump(150)
    out("    自检已跑 150 秒（不依赖控件状态）")
    out("")

    # --- 开始扫描 ---
    # ★ 端到端测试必须忽略历史进度，否则账号全被「续跑」跳过，什么都测不到
    if os.path.exists(out_path):
        os.remove(out_path)
    _prog = os.path.join(app_config.WORK_DIR, "progress.jsonl")
    if os.path.exists(_prog):
        try:
            os.remove(_prog)
            out("    已清空历史进度（保证这次真的跑）")
        except Exception as e:
            out(f"    ⚠ 清进度失败: {e}")
    out("[4] 点「开始扫描」…")
    app.on_start()
    if not app.running:
        out("    ❌ 没能启动 —— 校验没过。逐项检查：")
        for _lbl, _v in (("账号文件", a.accounts),
                         ("代理文件", a.proxies or "(未用)"),
                         ("结果文件", out_path)):
            _ok = (not _v) or _v == "(未用)" or os.path.exists(_v)
            out(f"       {_lbl}: {_v}  {'✅ 存在' if _ok else '❌ 不存在'}")
        out(f"       账号文件内容可读: "
            f"{'✅' if os.path.exists(a.accounts) else '❌ 路径不存在'}")
        try:
            root.destroy()
        except Exception:
            pass
        rc = 2
    else:
        t0 = time.time()
        _tick = 0
        while not app.scan_finished and time.time() - t0 < a.timeout:
            root.update()
            time.sleep(0.1)
            _tick += 1
            if _tick % 300 == 0:          # 每 30 秒报一次心跳
                _el = time.time() - t0
                out(f"    …扫描中  已等 {_el/60:.1f} 分  "
                    f"（界面状态：成功 {app.stat.get('ok',0)} "
                    f"失败 {app.stat.get('fail',0)}）")
        el = time.time() - t0
        out(f"    扫描{'完成' if app.scan_finished else '超时'}  用时 {el/60:.1f} 分")

        # --- 断言 ---
        out("")
        out("[5] 断言结果")
        if not os.path.exists(out_path):
            out("    ❌ 结果文件不存在")
            rc = 3
        else:
            rows = [l.strip() for l in io.open(out_path, encoding="utf-8",
                                               errors="replace") if l.strip()]
            out(f"    结果行数: {len(rows)}")
            bad = 0
            for r in rows:
                if "----" not in r:
                    out(f"    ❌ 格式异常: {r[:60]}")
                    bad += 1
                elif not (r.count("[") >= 1 and r.rstrip().endswith("]")):
                    out(f"    ❌ 结构异常（缺 [ 或结尾 ]）: {r[:60]}")
                    bad += 1
                else:
                    out(f"    ✓ {r[:110]}")
            if bad or not rows:
                rc = 4
            else:
                out(f"    ✅ 全部 {len(rows)} 行格式正确")
    try:
        root.destroy()
    except Exception:
        pass

    out("")
    out("=" * 62)
    out(f"  端到端测试结论: {'通过 ✅' if rc == 0 else f'失败 ❌ (rc={rc})'}")
    out("=" * 62)
    _write_report()
    return rc


def main():
    _install_excepthook()
    argv = sys.argv[1:]
    if argv and argv[0] == "--e2e":
        return run_e2e(argv[1:])
    if argv and argv[0] == "--selftest":
        rc = run_selftest()
        if getattr(sys, "frozen", False):
            try:
                r = tk.Tk(); r.withdraw()
                p = os.path.join(app_config.app_dir(), "selftest.log")
                messagebox.showinfo("自检完成",
                                    ("全部通过 ✅" if rc == 0 else "有问题 ❌")
                                    + f"\n\n日志：\n{p}")
                r.destroy()
            except Exception:
                pass
        return rc
    if argv and argv[0] == "--cli":
        return run_cli(argv[1:])
    # 注：--ocr-start / --ocr-stop 在文件最顶部就处理了（那里还没 import tkinter），
    #     不在这里 —— 见文件头的「OCR 专用入口」注释。

    try:
        root = tk.Tk()
        App(root)
        root.mainloop()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        p = _crash_log(tb)
        try:
            r = tk.Tk(); r.withdraw()
            messagebox.showerror("程序出错", f"{tb[-1200:]}\n\n日志：\n{p}")
            r.destroy()
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(main() or 0)
