# beautiful-world

> 完美世界 · 查区查等级 —— 批量扫号工具

输入一批「账号 + 密码」，自动登录完美世界，查出每个账号在哪些区服有角色、
角色多少级，输出成一张表。

**版本：v1.0.1** ｜ Windows 单文件 exe ｜ 内置 OCR 识别引擎

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
| `--fresh` | 忽略进度全部重跑 |

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
| 并行数上限 | 单 IP 最多 2；有代理池可到 8，**超过 8 会被风控** |
| 单账号耗时 | 约 3~5 分钟（53 个区服 + Vanguard 反复拦截） |
| 代理质量 | 住宅代理会偶发断连，已加「换代理自动重试」 |
| Linux | 目前不支持。OCR 引擎依赖 D3D11 + Media Foundation，Wine 跑不动 |

---

## 文档

- [使用说明](docs/使用说明.txt) —— 给新人的三步上手
- [交付说明](docs/交付说明.md) —— 完整交付文档（含验收记录、坑位表）
- [网络链路修复](docs/网络链路修复_20260924.md) —— ★ 代理池拒大陆 IP / TUN 假握手 / 服务器状态
- [服务器部署报告](docs/服务器部署报告.md) —— Linux 部署尝试与结论
- [架构](docs/TOOL_ARCH.md) · [链路](docs/CHAIN_ARCH.md) · [代理池](docs/PROXY_POOL.md)

---

## 许可

内部工具，仅供授权使用。
