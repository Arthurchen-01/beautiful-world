# -*- coding: utf-8 -*-
<#
================================================================
deploy_server.ps1 —— 完美世界扫号工具 · Windows 服务器部署
================================================================

在**服务器本机**（或经隧道映射后的本机）上运行，一次配完：

  1. 组件体检（Media Foundation / Direct3D 11 / VC++ 运行库 / 32 位 vcomp140）
  2. 系统体检（是不是 Server Core、有没有桌面）
  3. 建工作目录、铺文件
  4. 写运行包装脚本（带日志轮转）
  5. 注册计划任务（开机自启 + 定时跑）
  6. 出一份体检报告

★ 为什么要先体检
----------------------------------------------------------------
这套 OCR 引擎（`图像识别POST服务.exe` + `OCR.dll` + `XYLib.dll`）依赖两样东西：

  · **Media Foundation**（MF.dll / MFPlat.DLL / MFReadWrite.dll）
  · **Direct3D 11**（d3d11.dll）

Windows 原生自带，但 **Server Core** 和商家做的**「定制版」**会把它们删掉。
Linux/Wine 就是死在这两个上（Wine 实现不完整）。

另外 `XYLib.dll` 是 **32 位** 的，需要 **32 位** `vcomp140.dll`
（`C:\Windows\SysWOW64\vcomp140.dll`）。喂 64 位的会在加载阶段直接崩
`0xC000041D` —— 这个坑本地踩过一次。

好消息：**服务器不需要显卡**。Windows 自带 WARP 软件 D3D11 渲染器。

用法
----------------------------------------------------------------
    # 先干跑（只体检、只打印打算做什么，不改任何东西）
    .\deploy_server.ps1 -DryRun

    # 真部署
    .\deploy_server.ps1 `
        -Exe       "C:\Users\Administrator\Desktop\完美世界扫号工具.exe" `
        -Accounts  "C:\Users\Administrator\Desktop\账号.txt" `
        -Proxies   "C:\Users\Administrator\Desktop\代理.txt" `
        -WorkDir   "C:\WMRoleScan" `
        -IntervalMinutes 30

    # 取消计划任务
    .\deploy_server.ps1 -Uninstall

参数
----------------------------------------------------------------
    -Exe             扫号工具 exe 路径
    -Accounts        账号文件（host:port 或 账号----密码 每行一条）
    -Proxies         代理文件
    -WorkDir         工作目录，默认 C:\WMRoleScan
    -TaskName        计划任务名，默认 WMRoleScan
    -IntervalMinutes 定时跑间隔（分钟），0 = 只在开机时跑一次
    -DryRun          只体检、只打印，不做任何修改
    -Uninstall       删除计划任务
================================================================
#>

param(
    [string]$Exe       = "",
    [string]$Accounts  = "",
    [string]$Proxies   = "",
    [string]$WorkDir   = "C:\WMRoleScan",
    [string]$TaskName  = "WMRoleScan",
    [int]$IntervalMinutes = 30,
    [switch]$DryRun,
    [switch]$SkipSmoke,
    [switch]$Uninstall
)

$ErrorActionPreference = "Continue"
$script:issues  = @()
$script:warns   = @()
$script:oks     = @()

function Say([string]$m, [string]$c = "Gray") { Write-Host $m -ForegroundColor $c }
function OK([string]$m)   { $script:oks    += $m; Say "  [OK]   $m" "Green" }
function Warn([string]$m) { $script:warns  += $m; Say "  [警告] $m" "Yellow" }
function Bad([string]$m)  { $script:issues += $m; Say "  [失败] $m" "Red" }
function Hdr([string]$m)  { Say ""; Say ("=" * 62) "DarkCyan"; Say "  $m" "Cyan"; Say ("=" * 62) "DarkCyan" }

# ---------------------------------------------------------------- 1. 组件体检
function Test-Components {
    Hdr "1 / 6  组件体检（决定 OCR 能不能跑）"

    $files = @(
        @{ p = "$env:SystemRoot\System32\MF.dll";         what = "Media Foundation 主库";       need = $true  },
        @{ p = "$env:SystemRoot\System32\MFPlat.DLL";     what = "Media Foundation 平台层";     need = $true  },
        @{ p = "$env:SystemRoot\System32\MFReadWrite.dll";what = "Media Foundation 读写";       need = $true  },
        @{ p = "$env:SystemRoot\System32\d3d11.dll";      what = "Direct3D 11";                 need = $true  },
        @{ p = "$env:SystemRoot\System32\msvcp140.dll";   what = "VC++ 2015+ 运行库 (x64)";     need = $false },
        @{ p = "$env:SystemRoot\SysWOW64\vcomp140.dll";   what = "VC++ OpenMP 运行库 (x86) ★";  need = $false },
        @{ p = "$env:SystemRoot\SysWOW64\msvcp140.dll";   what = "VC++ 2015+ 运行库 (x86)";     need = $false }
    )
    foreach ($f in $files) {
        if (Test-Path $f.p) {
            $sz = (Get-Item $f.p).Length
            OK ("{0,-34} {1,10:N0} 字节   {2}" -f $f.what, $sz, $f.p)
        } elseif ($f.need) {
            Bad ("{0,-34} 缺失！   {1}" -f $f.what, $f.p)
        } else {
            Warn ("{0,-34} 缺失     {1}" -f $f.what, $f.p)
        }
    }

    # 32 位 vcomp140 缺失的补救：exe 自带一份，但要确认
    if (-not (Test-Path "$env:SystemRoot\SysWOW64\vcomp140.dll")) {
        Warn "32 位 vcomp140.dll 不在系统目录 —— exe 内部自带一份，正常不用管；"
        Warn "  若 OCR 起不来（0xC000041D），把 exe 同目录的 识别端\vcomp140.dll 补到 SysWOW64"
    }

    # 显卡 / 渲染器
    #
    # ★ 别用 dxdiag 探测 —— 实测它会挂住（`dxdiag /t` 起的是 GUI 程序，
    #   在无桌面/受限环境下不返回，整个部署脚本就卡死了）。
    #   WARP 是 Windows 自带的软件 D3D11 渲染器，**不需要探测**：
    #   只要 d3d11.dll 在（上面已验），没显卡也能跑，只是慢。
    try {
        $gpus = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -and $_.Name -notmatch "Basic|Remote|Mirror" })
        if ($gpus.Count -gt 0) {
            OK ("显卡: {0}" -f (($gpus | ForEach-Object { $_.Name }) -join " / "))
        } else {
            OK "没检测到独立显卡 —— 没关系，Windows 自带 WARP 软件渲染器，D3D11 照跑"
        }
    } catch {
        OK "显卡信息没查到 —— 没关系，WARP 是系统自带的"
    }
}

# ---------------------------------------------------------------- 2. 系统体检
function Test-OS {
    Hdr "2 / 6  系统体检"

    $os = Get-CimInstance Win32_OperatingSystem
    Say ("  系统      : {0}" -f $os.Caption)
    Say ("  版本      : {0}  (Build {1})" -f $os.Version, $os.BuildNumber)
    Say ("  架构      : {0}" -f $os.OSArchitecture)

    if ($os.OSArchitecture -notmatch "64") {
        Bad "这是 32 位系统 —— 扫号工具是 x64 的，跑不了"
    } else {
        OK "64 位系统"
    }

    # ---- ★ 会话 / 桌面 体检（Server 上最容易踩的坑）----
    #
    # 这套 OCR 引擎（图像识别POST服务.exe）是**图形界面程序**：
    # 它必须先把窗口建出来，我们再往窗口上的「启动」按钮发消息，它才监听 506。
    #
    # 计划任务如果用 SYSTEM 或「不管用户是否登录」，就跑在 **Session 0** ——
    # 那里没有交互桌面。窗口消息（PostMessage / SetWindowPos）本身能用，
    # 但「GUI 程序能不能在 Session 0 把窗口建起来」**没验证过**。
    #
    # 所以这里先把会话情况明确报出来，别等失败了才发现。
    $sid  = (Get-Process -Id $PID).SessionId
    $who  = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $isSys = ($who -match "SYSTEM$")
    Say ("  当前会话  : Session {0}   身份 {1}" -f $sid, $who)

    if ($isSys -or $sid -eq 0) {
        Warn "正在 Session 0 / SYSTEM 下运行（计划任务通常就是这样）"
        Warn "  OCR 引擎是 GUI 程序，要靠窗口消息点「启动」按钮。"
        Warn "  窗口消息在 Session 0 能用，但 GUI 能不能建出窗口**没验证过**。"
        Warn "  第 6 步冒烟测试会实际验证 —— 过不了看下面的兜底方案。"
    } else {
        OK ("有交互桌面（Session {0}）—— OCR 引擎能正常起" -f $sid)
    }

    # Server Core 判定：没有 explorer.exe / 没有桌面组件
    $explorer = Get-Process explorer -ErrorAction SilentlyContinue
    if ($explorer) {
        OK "有桌面（explorer.exe 在跑）—— 不是 Server Core"
    } else {
        $sess = (Get-CimInstance Win32_ComputerSystem).UserName
        if ($sess) {
            Warn "没看到 explorer.exe。若你正用远程桌面看着桌面，那是我判断错了，忽略这条"
        } else {
            Bad "没有桌面进程 —— 可能是 Server Core。图形界面跑不了，需要重装成「桌面体验」版"
        }
    }

    # 图形界面依赖
    if (Test-Path "$env:SystemRoot\System32\user32.dll") { OK "user32.dll 在" } else { Bad "user32.dll 缺失" }

    # 内存 / 磁盘
    $memGB = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
    Say ("  物理内存  : {0} GB" -f $memGB)
    if ($memGB -lt 3) { Warn "内存小于 3GB —— OCR 引擎加载 80MB 模型，可能吃紧" } else { OK "内存够（$memGB GB）" }

    $sys = Get-PSDrive -Name ($env:SystemDrive.TrimEnd(':')) -ErrorAction SilentlyContinue
    if ($sys) {
        $freeGB = [math]::Round($sys.Free / 1GB, 1)
        Say ("  系统盘可用: {0} GB" -f $freeGB)
        if ($freeGB -lt 3) { Warn "系统盘可用空间不足 3GB" } else { OK "磁盘够（$freeGB GB 可用）" }
    }

    # 端口 506 有没有被占（OCR 服务要用）
    $busy = Get-NetTCPConnection -LocalPort 506 -State Listen -ErrorAction SilentlyContinue
    if ($busy) {
        Warn "端口 506 已被占用（可能是 OCR 服务已经在跑）—— 正常，只要它是我们自己的"
    } else {
        OK "端口 506 空闲（OCR 服务会占它）"
    }
}

# ---------------------------------------------------------------- 2b. Defender 排除
#
# ★★ 这是 2026-09-24 实测踩到的最硬的坑，必须最先做。
#
# Windows Defender 把**易语言**编译的程序判为广告软件：
#     Adware:Win32/Flystudio        ← Flystudio 就是易语言的代号
#     Program:Win32/Contebrew.A!ml
#
# 于是 PyInstaller 把「识别端」解包到临时目录的那一刻，
# Defender 就把 `HPSocket4C.dll`（网络库）和 `OCR.dll`（54MB 模型）删掉了。
# 后果：OCR 服务窗口能出来、按钮能找到、BM_CLICK 也返回 True，
#      但**一点「启动」进程就崩**（内存 9MB，只加载 7 个系统 DLL，OCR/XYLib 都没加载）。
# 现象和「缺 vcomp140.dll」几乎一样，极容易误判 —— 我在这上面绕了很久。
#
# 实测记录（加排除项之前，服务器上 Defender 的隔离日志）：
#   09/24 15:07:27  _MEI...\识别端\HPSocket4C.dll
#   09/24 15:26:27  _MEI...\识别端\OCR.dll
#   09/24 15:30:07  _MEI...\识别端\OCR.dll
#
# 加完排除项后：506 立刻监听，OCR 实拍识别正常
#   cap_00.jpg -> 113,108|249,46|240,119|48,252|187,38|
function Add-DefenderExclusions {
    Hdr "2b / 6  Windows Defender 排除项（★ 不做这步 OCR 必崩）"

    $defender = Get-MpComputerStatus -ErrorAction SilentlyContinue
    if (-not $defender) {
        Warn "没装 Windows Defender（或查不到）—— 跳过"
        Warn "  ⚠️ 但如果客户装了火绒/360/卡巴，一样要加白名单"
        return
    }

    Say ("  实时保护: {0}" -f $(if ($defender.RealTimeProtectionEnabled) { "开启（必须加排除项）" } else { "关闭" }))

    if ($DryRun) {
        Say "  [DryRun] 会加这些排除项：" "DarkYellow"
        foreach ($p in @($WorkDir, "$env:LOCALAPPDATA\Temp",
                         "$env:LOCALAPPDATA\WMRoleScan", "$env:SystemRoot\Temp")) {
            Say ("    [路径] {0}" -f $p) "DarkYellow"
        }
        Say  "    [进程] 图像识别POST服务.exe / 完美世界扫号工具.exe" "DarkYellow"
        return
    }

    # 要排除的路径：exe 所在目录 + 所有可能被 PyInstaller 解包到的临时目录
    $paths = @(
        $WorkDir,
        "$env:LOCALAPPDATA\Temp",
        "$env:LOCALAPPDATA\WMRoleScan",
        "$env:SystemRoot\Temp",
        "$env:SystemRoot\System32\config\systemprofile\AppData\Local\Temp",
        "$env:SystemRoot\System32\config\systemprofile\AppData\Local\WMRoleScan"
    ) | Where-Object { $_ } | Select-Object -Unique

    foreach ($p in $paths) {
        try {
            Add-MpPreference -ExclusionPath $p -ErrorAction Stop
            OK ("[路径] {0}" -f $p)
        } catch {
            Warn ("[路径] {0} 加不上: {1}" -f $p, $_.Exception.Message)
        }
    }

    foreach ($pr in @("图像识别POST服务.exe", "完美世界扫号工具.exe")) {
        try {
            Add-MpPreference -ExclusionProcess $pr -ErrorAction Stop
            OK ("[进程] {0}" -f $pr)
        } catch {
            Warn ("[进程] {0} 加不上" -f $pr)
        }
    }

    Say ""
    Say "  当前排除项：" "Gray"
    Say ("    路径: {0}" -f (((Get-MpPreference).ExclusionPath) -join " | ")) "Gray"
    Say ("    进程: {0}" -f (((Get-MpPreference).ExclusionProcess) -join " | ")) "Gray"
    Say ""
    Warn "  ⚠️ 客户若有集中式杀软（火绒/360/卡巴），也要在那上面加白名单，"
    Warn "     否则一样会在解包时删掉 HPSocket4C.dll / OCR.dll"
}

# ---------------------------------------------------------------- 3. 文件检查
function Test-Inputs {
    Hdr "3 / 6  输入文件检查"

    foreach ($pair in @(@{n="exe";p=$Exe}, @{n="账号文件";p=$Accounts}, @{n="代理文件";p=$Proxies})) {
        if (-not $pair.p) { Warn ("{0} 没指定" -f $pair.n); continue }
        if (Test-Path $pair.p) {
            $sz = (Get-Item $pair.p).Length
            OK ("{0,-8} {1,12:N0} 字节   {2}" -f $pair.n, $sz, $pair.p)
        } else {
            Bad ("{0,-8} 找不到: {1}" -f $pair.n, $pair.p)
        }
    }

    if ($Accounts -and (Test-Path $Accounts)) {
        $lines = @(Get-Content $Accounts -Encoding UTF8 | Where-Object { $_.Trim() -and -not $_.StartsWith("#") })
        Say ("  账号文件行数: {0}" -f $lines.Count)
        $bad = @($lines | Where-Object { $_ -notmatch "----" })
        if ($bad.Count -gt 0) {
            Warn ("{0} 行格式不是 账号----密码，例如: {1}" -f $bad.Count, $bad[0])
        } else {
            OK "账号格式看起来正常（账号----密码）"
        }
    }

    if ($Proxies -and (Test-Path $Proxies)) {
        $plines = @(Get-Content $Proxies -Encoding UTF8 | Where-Object { $_.Trim() -and -not $_.StartsWith("#") })
        Say ("  代理文件行数: {0}" -f $plines.Count)
        # 本地桥接端口 vs 真代理
        $local = @($plines | Where-Object { $_ -match "^127\.0\.0\.1:" }).Count
        if ($local -eq $plines.Count -and $plines.Count -gt 0) {
            OK "看起来是 chain_proxy 桥接后的本地端口清单"
            Warn "  注意：桥接器必须在本机跑着，这些端口才有用"
        } elseif ($plines.Count -gt 0) {
            Say "  看起来是直连代理（非本地桥接）"
        }
    }
}

# ---------------------------------------------------------------- 4. 铺文件
function Install-Files {
    Hdr "4 / 6  铺文件到工作目录"

    if ($DryRun) { Say "  [DryRun] 会创建目录: $WorkDir" "DarkYellow"; return }

    foreach ($d in @($WorkDir, "$WorkDir\logs", "$WorkDir\out")) {
        if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    }
    OK "工作目录就绪: $WorkDir"

    # ★ 源和目标可能是同一个文件（比如 exe 已经放在工作目录里了）——
    #   直接 Copy-Item 会报 "无法使用项 X 其自身覆盖该项"。先比一下再拷。
    function Copy-IfDifferent([string]$src, [string]$dst, [string]$label) {
        if (-not $src -or -not (Test-Path $src)) { return }
        try {
            if (Test-Path $dst) {
                $a = (Resolve-Path $src).Path
                $b = (Resolve-Path $dst).Path
                if ($a -eq $b) { OK ("{0} 已在位（无需拷贝）" -f $label); return }
            }
            Copy-Item $src $dst -Force -ErrorAction Stop
            OK ("{0} 已就位" -f $label)
        } catch {
            Warn ("{0} 拷贝失败: {1}" -f $label, $_.Exception.Message)
        }
    }

    Copy-IfDifferent $Exe      (Join-Path $WorkDir "完美世界扫号工具.exe") "exe"
    Copy-IfDifferent $Accounts (Join-Path $WorkDir "账号.txt")            "账号.txt"
    Copy-IfDifferent $Proxies  (Join-Path $WorkDir "代理.txt")            "代理.txt"
    $dstExe = Join-Path $WorkDir "完美世界扫号工具.exe"
    if (Test-Path $dstExe) { Say ("  exe 大小: {0:N1} MB" -f ((Get-Item $dstExe).Length / 1MB)) }

    # 运行包装脚本
    $runner = Join-Path $WorkDir "run_scan.cmd"
    $body = @"
@echo off
rem ================================================================
rem  完美世界扫号工具 · 定时运行包装
rem  由 deploy_server.ps1 生成，改这个文件会被下次部署覆盖
rem ================================================================
setlocal
set WD=%~dp0
set LOGDIR=%WD%logs
set STAMP=%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%_%TIME:~0,2%%TIME:~3,2%
set STAMP=%STAMP: =0%

echo [%DATE% %TIME%] 开始 ========================>> "%LOGDIR%\runner.log"

rem --- 没有可用代理就直接退出，别拿本机 IP 去撞（红线）---
if not exist "%WD%代理.txt" (
  echo [%DATE% %TIME%] 代理.txt 不存在，拒绝运行>> "%LOGDIR%\runner.log"
  exit /b 2
)

rem --- 先跑一次代理体检 ---
"%WD%完美世界扫号工具.exe" --cli "%WD%账号.txt" ^
    --out "%WD%out\结果_%STAMP%.txt" ^
    --proxy-file "%WD%代理.txt" ^
    --parallel 4 --workers 3 --rate 0.8 --retries 3 ^
    >> "%LOGDIR%\scan_%STAMP%.log" 2>&1

echo [%DATE% %TIME%] 结束 rc=%ERRORLEVEL% ==========>> "%LOGDIR%\runner.log"

rem --- 日志只留最近 30 份 ---
for /f "skip=30 delims=" %%f in ('dir /b /o-d "%LOGDIR%\scan_*.log" 2^>nul') do del "%LOGDIR%\%%f" 2>nul
endlocal
"@
    Set-Content -Path $runner -Value $body -Encoding OEM
    OK "运行包装脚本: $runner"

    # 手动跑一次的快捷方式
    $manual = Join-Path $WorkDir "手动跑一次.cmd"
    Set-Content -Path $manual -Value "@echo off`r`ncall `"%~dp0run_scan.cmd`"`r`necho.`r`necho 跑完了，结果在 out\ 目录，日志在 logs\ 目录`r`npause" -Encoding OEM
    OK "手动运行脚本: $manual"
}

# ---------------------------------------------------------------- 5. 计划任务
function Install-Task {
    Hdr "5 / 6  计划任务"

    $runner = Join-Path $WorkDir "run_scan.cmd"

    if ($DryRun) {
        Say ("  [DryRun] 会注册计划任务 '{0}'" -f $TaskName) "DarkYellow"
        Say ("           执行: {0}" -f $runner) "DarkYellow"
        if ($IntervalMinutes -gt 0) {
            Say ("           每 {0} 分钟跑一次，开机也跑一次" -f $IntervalMinutes) "DarkYellow"
        } else {
            Say  "           只在开机时跑一次" "DarkYellow"
        }
        return
    }

    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Warn "已删除同名旧任务，准备重建"
    }

    # ★ 先判管理员 —— 非管理员下 Register-ScheduledTask 会抛异常甚至卡住，
    #   直接跳过比让它挂在那儿强。
    $isAdmin = ([Security.Principal.WindowsPrincipal] `
                [Security.Principal.WindowsIdentity]::GetCurrent()
               ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        Bad "当前不是管理员，跳过计划任务注册"
        Warn "  用管理员身份重开 PowerShell，再跑一次同一个命令即可补上"
        Warn ("  或者手工执行: schtasks /create /tn `"{0}`" /tr `"{1}`" /sc onstart /ru SYSTEM" -f $TaskName, $runner)
        return
    }

    $action = New-ScheduledTaskAction -Execute $runner -WorkingDirectory $WorkDir
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
        -MultipleInstances IgnoreNew -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)

    if ($IntervalMinutes -gt 0) {
        $triggers = @(
            (New-ScheduledTaskTrigger -AtStartup),
            (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
                -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes))
        )
    } else {
        $triggers = @( (New-ScheduledTaskTrigger -AtStartup) )
    }

    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
            -Settings $settings -Principal $principal `
            -Description "完美世界查区查等级 · 批量扫号（由 deploy_server.ps1 注册）" | Out-Null
        OK ("计划任务已注册: {0}" -f $TaskName)
        if ($IntervalMinutes -gt 0) { OK ("  触发: 开机 + 每 {0} 分钟" -f $IntervalMinutes) }
        else                       { OK  "  触发: 仅开机" }
    } catch {
        Bad ("注册计划任务失败: {0}" -f $_.Exception.Message)
        Warn "  试试用管理员身份重开 PowerShell 再跑"
        return
    }

    # ---- ★ 配套任务：在【用户会话】里把 OCR 服务常驻起来 ----
    #
    # 为什么需要它：主任务是 SYSTEM / Session 0，而 OCR 引擎是 GUI 程序，
    # 要靠窗口消息点「启动」按钮。GUI 能不能在 Session 0 建出窗口**没验证过**。
    #
    # 这个配套任务在**用户登录时**跑（有桌面），用 --ocr-start 把服务拉起来。
    # 之后主任务一连 506 就发现已经在监听，ocr_service.start() 直接返回，
    # **根本不碰窗口** —— Session 0 的坑就绕过去了。
    #
    # 代价：需要有人登录过至少一次。没人登录它就只是不跑，不影响主任务。
    $ocrTask = "$TaskName-OCR"
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    try {
        $existingOcr = Get-ScheduledTask -TaskName $ocrTask -ErrorAction SilentlyContinue
        if ($existingOcr) { Unregister-ScheduledTask -TaskName $ocrTask -Confirm:$false }

        $oExe = Join-Path $WorkDir "完美世界扫号工具.exe"
        $oAct = New-ScheduledTaskAction -Execute $oExe -Argument "--ocr-start" `
                    -WorkingDirectory $WorkDir
        $oTrg = @( (New-ScheduledTaskTrigger -AtLogOn -User $me) )
        $oSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                        -DontStopIfGoingOnBatteries -StartWhenAvailable `
                        -ExecutionTimeLimit (New-TimeSpan -Hours 0) -MultipleInstances IgnoreNew
        # 用当前用户 + Interactive：这样它跑在**有桌面的会话**里
        $oPrincipal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Highest

        Register-ScheduledTask -TaskName $ocrTask -Action $oAct -Trigger $oTrg `
            -Settings $oSettings -Principal $oPrincipal `
            -Description "完美世界扫号 · OCR 服务常驻（登录时拉起，供 Session 0 的主任务使用）" | Out-Null
        OK ("OCR 常驻任务已注册: {0}  （用户 {1} 登录时拉起）" -f $ocrTask, $me)
        Warn "  代价：需要有人登录过一次；没人登录它就只是不跑，不影响主任务"
        Warn ("  手工先拉一次：{0}\完美世界扫号工具.exe --ocr-start" -f $WorkDir)
    } catch {
        Warn ("OCR 常驻任务没注册上（{0}）—— 不影响主任务，但 Session 0 兜底就没了" -f $_.Exception.Message)
    }
}

# ---------------------------------------------------------------- 6. 冒烟测试
function Test-Smoke {
    Hdr "6 / 6  冒烟测试（★ 这一步专门验 OCR 能不能起来）"

    if ($DryRun)   { Say "  [DryRun] 会跑一次 --selftest" "DarkYellow"; return }
    if ($SkipSmoke) { Warn "按 -SkipSmoke 跳过冒烟测试 —— 但这一步是验 OCR 的关键，建议别跳"; return }

    $dstExe = Join-Path $WorkDir "完美世界扫号工具.exe"
    if (-not (Test-Path $dstExe)) { Bad "exe 不在工作目录，跳过"; return }

    $sid = (Get-Process -Id $PID).SessionId
    Say ("  跑 --selftest（约 30 秒）...  当前 Session {0}" -f $sid)
    $out = & $dstExe --selftest 2>&1 | Out-String
    $tail = ($out -split "`n" | Select-Object -Last 14) -join "`n"
    Say $tail

    $ocrOk = ($out -match "服务已在监听 506") -or ($out -match "506 已监听")
    $allOk = ($out -match "全部通过")

    if ($allOk) {
        OK "自检全部通过"
        if ($ocrOk) { OK "OCR 服务在本次会话里起来了 —— Session 0 这条路走得通" }
        return
    }

    # ---- 失败：区分「OCR 起不来」和「别的问题」----
    if (-not $ocrOk) {
        Bad "OCR 服务没起来（506 没监听）"
        Say ""
        if ($sid -eq 0) {
            Say "  ★ 这就是 Session 0 的问题：OCR 引擎是 GUI 程序，" -ForegroundColor Yellow
            Say "    它要靠窗口消息点「启动」按钮，而 Session 0 没有交互桌面。" -ForegroundColor Yellow
            Say ""
            Say "  ★ 兜底方案（三选一，从省事到麻烦）：" -ForegroundColor Cyan
            Say ""
            Say "  方案 1（最省事）：先用远程桌面登进去，手工把 OCR 服务跑起来，" -ForegroundColor Gray
            Say "      让它常驻。之后计划任务里 ocr_service 会发现 506 已经在监听，" -ForegroundColor Gray
            Say "      **直接跳过「点按钮」这一步**，就不会踩 Session 0 的坑了。" -ForegroundColor Gray
            Say ("        手工起：  {0}\完美世界扫号工具.exe --selftest" -f $WorkDir) -ForegroundColor Gray
            Say ""
            Say "  方案 2：把计划任务改成「只在用户登录时运行」，并保持一个远程桌面会话开着。" -ForegroundColor Gray
            Say "      （这样任务跑在交互会话里，有桌面，OCR 能正常起）" -ForegroundColor Gray
            Say ("        schtasks /change /tn `"{0}`" /ru 你的用户名" -f $TaskName) -ForegroundColor Gray
            Say ""
            Say "  方案 3：联系我，把 OCR 引擎换成不依赖窗口的启动方式。" -ForegroundColor Gray
        } else {
            Say "  当前有交互桌面，OCR 还起不来，先查这几样：" -ForegroundColor Yellow
            Say "    · 识别端\vcomp140.dll 在不在（必须是 32 位，缺了会崩 0xC000041D）"
            Say "    · 杀毒软件有没有拦「图像识别POST服务.exe」"
            Say "    · 手工双击 识别端\图像识别POST服务.exe 看看能不能出窗口"
        }
        Say ""
    } else {
        Bad "自检没全过，看上面的输出"
    }
}

# ---------------------------------------------------------------- 报告
function Write-Report {
    Hdr "部署报告"

    Say ("  通过 {0} 项 / 警告 {1} 项 / 失败 {2} 项" -f $script:oks.Count, $script:warns.Count, $script:issues.Count) `
        $(if ($script:issues.Count) { "Red" } elseif ($script:warns.Count) { "Yellow" } else { "Green" })

    if ($script:issues.Count) {
        Say ""
        Say "  ★ 必须解决的问题：" "Red"
        $script:issues | ForEach-Object { Say ("    · {0}" -f $_) "Red" }
    }
    if ($script:warns.Count) {
        Say ""
        Say "  提醒（不一定影响运行）：" "Yellow"
        $script:warns | ForEach-Object { Say ("    · {0}" -f $_) "Yellow" }
    }

    Say ""
    Say "  部署后怎么用："
    Say ("    · 手动跑一次: {0}\手动跑一次.cmd" -f $WorkDir) "Gray"
    Say ("    · 看结果    : {0}\out\" -f $WorkDir) "Gray"
    Say ("    · 看日志    : {0}\logs\" -f $WorkDir) "Gray"
    Say ("    · 立即触发  : Start-ScheduledTask -TaskName {0}" -f $TaskName) "Gray"
    Say ("    · 看任务状态: Get-ScheduledTaskInfo -TaskName {0}" -f $TaskName) "Gray"

    $reportPath = Join-Path $WorkDir "部署报告.txt"
    if (-not $DryRun) {
        $rep = @()
        $rep += "完美世界扫号工具 · 服务器部署报告"
        $rep += "生成时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        $rep += "工作目录: $WorkDir"
        $rep += "计划任务: $TaskName"
        $rep += ""
        $rep += "[通过] " + ($script:oks    -join "`r`n[通过] ")
        $rep += ""
        $rep += "[警告] " + ($script:warns  -join "`r`n[警告] ")
        $rep += ""
        $rep += "[失败] " + ($script:issues -join "`r`n[失败] ")
        Set-Content -Path $reportPath -Value ($rep -join "`r`n") -Encoding UTF8
        Say ""
        Say ("  报告已写入: {0}" -f $reportPath) "Gray"
    }
}

# ---------------------------------------------------------------- 主流程
Say ""
Say "==================================================================" "DarkCyan"
Say "  完美世界扫号工具 · Windows 服务器部署" "Cyan"
Say ("  模式: {0}" -f $(if ($DryRun) { "DryRun（只体检，不改任何东西）" } else { "真部署" })) "Cyan"
Say "==================================================================" "DarkCyan"

if ($Uninstall) {
    Hdr "卸载"
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($t) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        OK ("计划任务已删除: {0}" -f $TaskName)
    } else {
        Warn ("没找到计划任务: {0}" -f $TaskName)
    }
    Say ""
    Say "  （工作目录 $WorkDir 保留没删，要删自己删）" "Gray"
    exit 0
}

Test-Components
Test-OS
Add-DefenderExclusions
Test-Inputs
Install-Files
Install-Task
Test-Smoke
Write-Report

Say ""
if ($script:issues.Count) { exit 1 } else { exit 0 }
