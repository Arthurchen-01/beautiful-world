# beautiful-world

> 完美世界 · 查区查等级 —— 批量扫号工具

输入一批「账号 + 密码」，自动登录完美世界，查出每个账号在哪些区服有角色、
角色多少级，输出成一张表。

**版本：v1.4.0** ｜ Windows 单文件 exe ｜ 内置 OCR 识别引擎

---

## 它解决什么问题

老板原来那台 `角色查询-诛仙.exe` 已经停掉了。这个工具是替代品：
批量输入账号密码，自动跑完 53 个区服，输出「哪个账号在哪个区、角色多少级」。

---

## 怎么用

```
1. 双击 完美世界扫号工具.exe
2. 点「✎ 手动」粘账号，再点「✎ 手动」粘代理（或「选文件」）
3. 点「环境自检」→ 三项都 ✅ → 点「▶ 开始扫描」
```

结果默认存在 exe 同目录的 `结果.txt`。

详细说明见 [`docs/使用说明.txt`](docs/使用说明.txt)。

---

## 输出格式

```
账号----密码----[区服----等级----等级…----[区服2----…
```

例：

```
zhangsan@example.com----mypassword----[经典区-天上人间----100----[经典区-白鸟----4----100----[经典区-逆鳞----100]
```

---

## 目录结构

```
beautiful-world/
├── src/                        核心源码
│   ├── wm_gui.py               图形界面（Tkinter）
│   ├── wm_scan.py              扫描引擎（登录 / 查角色 / 验证码 / Vanguard）
│   ├── wmcaptcha.py            验证码协议逆向实现（AES + 指纹 + 行为事件）
│   ├── solve_pick_text.py      点选验证码求解
│   ├── ocr_service.py          本地 OCR 服务管理（启动 / 探活 / 隐藏窗口）
│   ├── proxy_pool.py           代理池（体检 / 轮换 / 存活检测 / API 提取）
│   ├── input_tools.py          输入体检 + 本地查重
│   ├── wincompat.py            Windows 版本兼容层（Win7~Win11 降级链）
│   ├── egress_guard.py         出口红线守卫
│   ├── netguard.py             网络守卫（强制走代理）
│   ├── servers.py              区服表（53 个）
│   ├── app_config.py           配置管理
│   ├── build_exe.py            打包脚本
│   ├── make_icon.py            生成图标
│   └── make_version.py         生成版本资源
├── tools/
│   ├── chain_proxy.py          ★ 代理池第一跳桥接（解决代理商拒大陆 IP）
│   ├── tunnel.py               ★ 本地端口转发（RDP/WinRM/SMB）+ 权威探活
│   ├── server_ready.py         ★ 服务器就绪监视（只认真协议回复）
│   ├── deploy_server.ps1       ★ Windows 服务器一键部署（六段体检）
│   ├── pick_text_linux.py      Linux 原生验证码求解器（研发中）
│   └── proxy_check.py          代理批量体检
├── docs/                       文档
├── samples/                    验证码样本
├── 账号.txt.example
├── 代理.txt.example
├── VERSION
└── CHANGELOG.md
```

---

## 命令行用法

```bat
:: 环境自检
完美世界扫号工具.exe --selftest

:: 端到端自测（真驱动界面跑一遍并断言结果）
完美世界扫号工具.exe --e2e --accounts 账号.txt --proxies 代理.txt --out 结果.txt

:: 纯命令行批量扫号
完美世界扫号工具.exe --cli 账号.txt --out 结果.txt --parallel 4 --proxy-file 代理.txt
```

常用参数：

| 参数 | 说明 |
| :--- | :--- |
| `--game auto\|1\|11\|all` | 指定游戏（auto = 自动识别） |
| `--parallel N` | 账号并行数 |
| `--workers N` | 账号内区服并发数（默认 3） |
| `--rate 0.8` | 请求最小间隔秒（实测 0.8 最优） |
| `--retries N` | 网络类失败换代理重试次数 |
| `--timeout N` | HTTP 超时秒数（默认 30）。**★ 走住宅代理链建议 60** —— 验证码图片 CDN 经日本住宅代理实测要 5~14 秒一张 |
| `--fresh` | 忽略进度全部重跑 |

> 不想加参数也行：设环境变量 `WM_HTTP_TIMEOUT=60`（`WM_POST_TIMEOUT` 自动取 `N+10`）。

---

## 部署到 Windows 服务器

```powershell
# 先干跑（只体检，不改任何东西）
.\tools\deploy_server.ps1 -DryRun

# 真部署：建目录 / 铺文件 / 生成定时运行脚本 / 注册计划任务 / 冒烟测试
.\tools\deploy_server.ps1 `
    -Exe      "C:\Users\Administrator\Desktop\完美世界扫号工具.exe" `
    -Accounts "C:\Users\Administrator\Desktop\账号.txt" `
    -Proxies  "C:\Users\Administrator\Desktop\代理.txt" `
    -WorkDir  "C:\WMRoleScan" -IntervalMinutes 30
```

它会先查**这套 OCR 引擎活不活得下来**：Media Foundation、Direct3D 11、
32 位 `vcomp140.dll`（缺了会崩 `0xC000041D`）、**是不是 Server Core**
（Server Core 没有桌面，图形界面跑不了）、以及**当前会话有没有桌面**。

> 服务器**不需要显卡** —— Windows 自带 WARP 软件 D3D11 渲染器。

### ⚠️ 服务器上的三个坑（脚本已处理，但要知道）

**坑 1：Server Core 跑不了。** 装系统时必须选「桌面体验」版。

**坑 2：★★★ Windows Defender 会删掉 OCR 的文件。**

Defender 把**易语言**编译的程序判为广告软件（`Adware:Win32/Flystudio`），
PyInstaller 解包那一刻就删掉 `HPSocket4C.dll`（网络库）和 `OCR.dll`（54MB 模型）。

**后果极具误导性**：OCR 窗口能建出来、按钮能找到、`BM_CLICK` 返回 True，
但**一点「启动」进程就崩**（内存 9MB，只加载 7 个系统 DLL）。
**现象和「缺 vcomp140.dll」几乎一样。**

**判据**：看崩溃前**加载了几个模块**。只加载 7 个系统 DLL = 依赖文件被删；
加载了几十个 = 引擎初始化失败。

部署脚本的 **2b 段**会自动加排除项。客户若有火绒/360/卡巴，也要在那边加白名单。

**坑 3：32 位 VC++ 运行库。**

新装的 Windows Server 默认不带 x86 运行库，而 `XYLib.dll` 是 32 位的：

```powershell
Invoke-WebRequest "https://aka.ms/vs/17/release/vc_redist.x86.exe" -OutFile "$env:TEMP\vc_redist.x86.exe" -UseBasicParsing
Start-Process "$env:TEMP\vc_redist.x86.exe" -ArgumentList "/install","/quiet","/norestart" -Wait
```

> 服务器**不需要显卡** —— Windows 自带 WARP 软件 D3D11 渲染器。

### ★ 服务器上不需要第一跳

实测：**香港机房的服务器可以被 arxlabs 直接接受**（不像大陆 IP 会被 `403 forbidden`）。
所以服务器上的链路就是最简单的：

```
服务器  →  arxlabs SOCKS5  →  完美世界
```

不用 `chain_proxy.py`，也没有那个「1.5 请求/秒」的瓶颈。

**出口红线会自动识别部署形态**（本机 / 服务器），判据不同：

| 形态 | 触发条件 | 判据 |
| :--- | :--- | :--- |
| 本机 | 没有代理文件 | 本地代理在跑 + 连接走隧道 + 出口不在国内 |
| **服务器** | 有代理文件 | 代理文件有内容 + 取到出口 + 出口不在国内 |

> 早期版本只认「本机」形态，导致**服务器上必然拒绝开工**，只能靠 `--no-egress-check` 硬绕。

### ⚠️ 产能现状（实测，别抱幻想）

```
错密码号（登录就被拒）  --parallel 32 → 1,129 账号/小时
真号（扫满 53 区服）    --parallel 6  →    41 账号/小时
按 93% 错密码 + 7% 真号  --parallel 32 → 约 2,160 账号/小时
客户要的                10,000 账号/分钟 = 600,000 账号/小时
差距                    约 278 倍
```

**瓶颈在代理商（arxlabs），不在目标站**：目标站直连只要 280ms，
经 arxlabs 要 4.7~10.9 秒。详见 [`docs/吞吐实测报告.md`](docs/吞吐实测报告.md)。

> **★ 最大的杠杆不是技术优化，是问清楚「要不要扫满 53 个区服」。**
> 如果只要筛出有效账号（每账号 ~5 个请求），产能直接上一个数量级。

**服务器起不来（掉进恢复界面）看** [`docs/服务器恢复手册.md`](docs/服务器恢复手册.md)。
**完整部署复盘看** [`docs/服务器部署实战记录.md`](docs/服务器部署实战记录.md)。
**要搬到新服务器看** [`docs/服务器迁移可行性评估.md`](docs/服务器迁移可行性评估.md)。

---

## 环境要求

| 项目 | 要求 |
| :--- | :--- |
| 系统 | Windows 7 SP1 / 8.1 / 10 / 11（x64） |
| 运行库 | 无需安装，exe 自带 |
| 网络 | 需要能访问完美世界；建议配代理 |

程序启动时会自动探测系统版本并选择能力（DPI、API），不能用的自动降级。

---

## 出口红线

程序**每次启动都会强制校验**，不通过就拒绝运行：

```
[tunnel] us.proxy.example:3010 → 198.18.0.1  ✅ 隧道内
[egress] 出口 IP = 203.0.113.20  (Hong Kong)
[egress] ✅ 三项全过：代理在跑 / 连接走隧道 / 出口不在国内
```

三项：**代理在跑** / **连接走隧道**（本地地址在 `198.18.x.x` 网段）/
**出口不在国内**。

**本机真实 IP 绝不暴露给目标站。**

### ⚠️ 代理池必须先过「第一跳」

**代理商（arxlabs）拒绝中国大陆来源 IP**，直连一律返回：

```
403 Forbidden   msg: forbidden ip=<本机公网IP> not supported
```

所以本机**不能直连代理池**，必须经一个境外第一跳转发。用 `tools/chain_proxy.py`
把整条链封装成本地 SOCKS5，**`proxy_pool.py` 零改动**：

```bat
:: 1) 启动桥接（每条 sid 一个本地端口，自动体检并剔除国内出口）
python tools\chain_proxy.py --proxies 代理.txt --first-hop 127.0.0.1:7890

:: 2) 它写出 proxies_local.txt，直接喂主程序
python src\wm_scan.py 账号.txt --proxy-file proxies_local.txt
```

> **别信 HTTP 代理的 `200 Connection established`** —— 那是乐观应答，
> 目标死活都返回 200。判断连通性要用真协议握手，
> 见 `tools/server_ready.py` / `tools/tunnel.py` 的 `probe_target()`。

排查过程见 [`docs/网络链路修复_20260924.md`](docs/网络链路修复_20260924.md)。

---

## 已知限制

| 限制 | 说明 |
| :--- | :--- |
| **并发数上限** | **= 你的代理条数**。一个账号配一条独立 IP，账号之间不共用。**没有"8 个上限"这回事**（见下） |
| **同一账号不能并发** | 一个账号同一时刻只能跑一个会话 —— 对方的 Vanguard 风控是**按账号**锁的，同账号开多个会话会互相踢掉。这是对方的机制，改不了 |
| 单账号耗时 | 登录失败的号 ~18 秒；登录成功要扫满 53 个区服的号 ~3 分钟 |
| **产能瓶颈在出口线路** | 实测：消费级 VPN 做中转时吞吐饱和在 **1.5 请求/秒**，加并发也没用。详见 [`docs/产能分析_能跑多快卡在哪.md`](docs/产能分析_能跑多快卡在哪.md) |
| 代理质量 | 住宅代理会偶发断连，已加「换代理自动重试」 |
| Linux | 目前不支持。OCR 引擎依赖 D3D11 + Media Foundation，Wine 跑不动 |

> ### ⚠️ 更正：曾经写过的「并行数不要超过 8」是**错的**
>
> 早期 `docs/PROXY_POOL.md` 记过一次「4 账号并行失败」，当时误判成并发上限。
> 后来查清根因是：**那 4 个并发用的是同一个账号** ——
> Vanguard 按账号锁会话，同账号多会话互踢。**那是测试设计错误，不是并发上限。**
>
> **真正的规则只有两条：**
> 1. 一个账号同一时刻只能跑一个会话（对方的风控，改不了）
> 2. **并发数 = 独立 IP 条数**（一个账号一条 IP）
>
> 参考：老板原软件是 **200 线程 + 31,186 条代理**。
> 我们只要代理池够大，一样能开几百并发。

---

## 文档

**给使用者：**

- [**一页说明书**](docs/使用说明_一页版.md) —— ★ **三步上手 + 界面数字怎么看 + 常见问题**，先看这个
- [使用说明（详细）](docs/使用说明.txt)
- [交付说明](docs/交付说明.md) —— 完整交付文档（含验收记录、坑位表）

**给运维/开发：**

- [**产能分析：能跑多快，卡在哪**](docs/产能分析_能跑多快卡在哪.md) —— ★ 实测吞吐、瓶颈定位、扩产方案
- [**服务器恢复手册**](docs/服务器恢复手册.md) —— ★ 服务器掉进恢复界面怎么办（含 virtio 驱动诊断）
- [网络链路修复](docs/网络链路修复_20260924.md) —— 代理池拒大陆 IP / TUN 假握手 / 服务器状态
- [服务器部署报告](docs/服务器部署报告.md) —— Linux 部署尝试与结论
- [架构](docs/TOOL_ARCH.md) · [链路](docs/CHAIN_ARCH.md) · [代理池](docs/PROXY_POOL.md)

---

## 许可

内部工具，仅供授权使用。
