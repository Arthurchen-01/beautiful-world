# -*- coding: utf-8 -*-
"""
netguard —— 出口强制守卫
================================================================
作用：把 requests 的 Session.request 打上补丁，**强制所有出站 HTTP(S) 走代理**。
      任何脚本只要在 import requests 之前 `import netguard`，
      就不可能因为忘记传 proxies 而裸连出去。

设计原则：
  1. 默认拒绝 —— 非本地地址一律强制走代理，覆盖调用方传入的任何 proxies
  2. 本地放行 —— 127.0.0.1 / localhost / ::1 的控制接口不走代理
  3. 可审计 —— 每次被强制改写都记一笔，便于事后核查

用法：
    import netguard          # 必须放在 import requests 之前或紧随其后
    import requests
"""
import os
import sys
from urllib.parse import urlparse

import requests

_orig_request = requests.Session.request

PROXY = os.environ.get("WM_PROXY", "http://127.0.0.1:7890")
PROXIES = {"http": PROXY, "https": PROXY}

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

# 审计日志
FORCED = []          # 被强制改写的请求
ALLOWED_LOCAL = []   # 放行的本地请求
ALLOWED_EXTRA = []   # 放行的显式链式出口

# 显式登记、允许使用的「替代出口」（例如链式代理的第二跳）
# 只有被登记的地址才能绕过默认代理强制，避免出现静默裸连
EXTRA_ALLOWED = set()


def allow_proxy(url: str):
    """登记一个允许使用的替代出口。链式代理的第二跳必须显式登记。"""
    EXTRA_ALLOWED.add(url)


def _is_local(host: str) -> bool:
    if not host:
        return False
    if host in _LOCAL_HOSTS:
        return True
    return host.startswith("127.") or host.endswith(".local")


def _guarded_request(self, method, url, **kwargs):
    host = (urlparse(url).hostname or "").lower()

    if _is_local(host):
        ALLOWED_LOCAL.append(url)
        return _orig_request(self, method, url, **kwargs)

    given = kwargs.get("proxies")

    # 显式登记的替代出口（链式第二跳）才放行
    if isinstance(given, dict) and given:
        vals = {v for v in given.values() if v}
        if vals and vals <= EXTRA_ALLOWED:
            ALLOWED_EXTRA.append({"url": url, "via": sorted(vals)})
            return _orig_request(self, method, url, **kwargs)

    # ---- 其余一律强制走默认代理，覆盖调用方传进来的任何值 ----
    if given != PROXIES:
        FORCED.append({"url": url, "caller_proxies": given})
        if given is None:
            _safe_print(f"[netguard] 拦截裸连并强制代理: {url}", file=sys.stderr)
        elif isinstance(given, dict) and given:
            _safe_print(f"[netguard] 未登记的替代出口被拒，回退默认代理: {url}  {given}",
                        file=sys.stderr)
    kwargs["proxies"] = PROXIES
    kwargs.pop("trust_env", None)
    return _orig_request(self, method, url, **kwargs)


def _safe_print(*a, **k):
    """无控制台模式下 stdout/stderr 可能是 None，别让日志把程序搞崩"""
    try:
        print(*a, **k)
    except Exception:
        pass


def install():
    if getattr(requests.Session, "_netguard_installed", False):
        return
    requests.Session.request = _guarded_request
    requests.Session._netguard_installed = True


def report():
    _safe_print(f"[netguard] 强制代理改写 {len(FORCED)} 次，"
          f"放行本地 {len(ALLOWED_LOCAL)} 次，"
          f"放行链式出口 {len(ALLOWED_EXTRA)} 次")
    for f in FORCED:
        _safe_print(f"   [强制] {f['url']}  (调用方 proxies={f['caller_proxies']})")
    for a in ALLOWED_EXTRA:
        _safe_print(f"   [链式] {a['url']}  via {a['via']}")


install()
