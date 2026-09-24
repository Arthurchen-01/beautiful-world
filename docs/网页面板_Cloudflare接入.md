# 网页面板 · 接入 Cloudflare Tunnel

> 目标：让你和团队从 `https://<你的域名>` 打开扫号控制台，
> **服务器不开任何入站端口**，真实 IP 完全不暴露。

---

## 一、为什么用 Tunnel 而不是直接开 443

| | 直接开 443 | **Cloudflare Tunnel** |
| :--- | :--- | :--- |
| 服务器入站端口 | 要开 80/443 | **一个都不用开** |
| 真实 IP | 暴露给全世界 | **完全隐藏** |
| HTTPS 证书 | 要自己签、自己续 | Cloudflare 自动 |
| 抗 DDoS | 无 | 自带 |
| 第二层认证 | 要自己写 | **Cloudflare Access**（邮箱验证码） |
| 被扫描器发现 | 每天几百次 | 扫不到（没有开放端口） |

**这台服务器的 IP 是要去撞完美世界的，不该被摆到扫描器面前。**

---

## 二、在 Cloudflare 控制台创建 Tunnel（你操作，约 3 分钟）

### 第 1 步：确认域名在 Cloudflare

打开 <https://dash.cloudflare.com>，确认你的域名在列表里。
不在的话先「添加站点」，把 NS 记录改成 Cloudflare 给的两个。

### 第 2 步：创建隧道

```
Zero Trust  →  Networks  →  Tunnels  →  Create a tunnel
   ↓
选择 Cloudflared
   ↓
隧道名填：  wmscan
   ↓
保存后会显示「Install connector」页面，选 Windows
   ↓
★ 页面里有一串很长的命令，形如：
     cloudflared.exe service install eyJhIjoiXXXXX...
   ↓
把 eyJ 开头的那个 **token 整串复制给我**
```

> **那串 token 就是安装凭据**，形如 `eyJhIjoi...`，很长（几百字符）。
> 给我之后我在服务器上装好并起服务。

### 第 3 步：配置路由（也可以我来配）

在同一页面的 **Public Hostnames** 标签里加一条：

| 字段 | 填什么 |
| :--- | :--- |
| Subdomain | `scan`（或你喜欢的，比如 `wm`） |
| Domain | 选你的域名 |
| Path | 留空 |
| Service Type | **HTTP** |
| URL | `127.0.0.1:8080` |

保存后，`https://scan.你的域名.com` 就会指向服务器上的面板。

---

## 三、加第二层认证（强烈建议）

面板本身已经有登录（团队多人账号），但再加一层 Cloudflare Access 会更稳：

```
Zero Trust  →  Access  →  Applications  →  Add an application
   ↓
Self-hosted
   ↓
Application name : 扫号控制台
Session Duration : 24 hours
   ↓
Public hostname  : scan.你的域名.com
   ↓
Policy:
   Action  : Allow
   Include : Emails  →  填你和团队成员的邮箱（逗号分隔）
   ↓
登录方式：One-time PIN（邮箱验证码，不用装任何东西）
```

之后打开面板会**先要邮箱验证码**，再进面板登录页 —— 双保险。

---

## 四、服务器侧我要做的事（拿到 token 后）

```powershell
# 1) 下载 cloudflared
Invoke-WebRequest "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
  -OutFile "C:\WMRoleScan\cloudflared.exe" -UseBasicParsing

# 2) 装成 Windows 服务（★ 用你给的 token）
& "C:\WMRoleScan\cloudflared.exe" service install <token>

# 3) 起服务
Start-Service cloudflared

# 4) 验证：隧道状态
& "C:\WMRoleScan\cloudflared.exe" tunnel info wmscan
```

**装成服务之后**：开机自启、挂了自动重启，不需要人盯着。

---

## 五、面板本身怎么跑成可靠服务

面板**不能用 `Start-Process` 起**（WinRM 会话一关就被带走，这是实测踩过的坑）。
用计划任务：

```powershell
$act = New-ScheduledTaskAction -Execute "C:\WMRoleScan\wmscan.exe" `
         -Argument "--web --port 8080 --bind 127.0.0.1" -WorkingDirectory "C:\WMRoleScan"
$pr  = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$st  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable `
         -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Hours 0)
Register-ScheduledTask -TaskName "WMRoleScan-Web" -Action $act -Principal $pr -Settings $st -Force
Start-ScheduledTask -TaskName "WMRoleScan-Web"
```

`-RestartCount 999` + `-ExecutionTimeLimit 0` = **挂了自动拉起、永不超时杀掉**。

---

## 六、安全清单

| 项 | 做法 |
| :--- | :--- |
| 面板只监听 | `127.0.0.1`，**不监听 0.0.0.0** |
| 服务器入站端口 | 只有 3389/5985；上了 Tunnel 之后**连这两个都可以关** |
| 面板登录 | 团队每人一个账号（PBKDF2 存哈希，不是明文） |
| 第二层认证 | Cloudflare Access（邮箱验证码） |
| 会话 | HttpOnly + SameSite Cookie，7 天滑动过期 |
| 首次密码 | 写在 `webui\初始密码.txt`，**登录后改掉并删文件** |
| 传输 | 全程 HTTPS（Cloudflare 终结） |

---

## 七、访问方式（最终形态）

```
你的浏览器
   ↓ HTTPS
Cloudflare（自动证书 + 可选邮箱验证）
   ↓ 隧道（出站连接，服务器不开端口）
服务器 127.0.0.1:8080
   ↓
扫号控制台
```

**面板能做什么**：

| 标签 | 功能 |
| :--- | :--- |
| 概览 | 服务器状态 / OCR 服务（可拉起、可停止）/ 计划任务 / 输入文件 |
| 扫描 | 参数设置 → 启停 → **实时日志** → 结果预览 → 下载 |
| 历史 | 每批次的开始结束时间、退出码、结果行数、下载 |
| 代理池 | 编辑代理文件 → 一键体检（可用数 / 唯一 IP / 国内出口告警 / 逐条延迟） |
| 设置 | 账号文件编辑 / 面板账号管理 / 改密码 |
