"""Resolve system proxy settings once per destination without mutating env.

HTTPX 0.28 requires ``httpx[socks]`` and accepts socks5/socks5h, not the
``socks://`` spelling emitted by some desktop proxy tools. Websockets 15 has
separate proxy selection and requires python-socks[asyncio]. Keep both clients
consistent on selection, bypass rules and that legacy scheme alias.

DNS: HTTPX/httpcore sends SOCKS hostnames to the proxy. Websockets uses local
DNS for socks5 and proxy DNS for socks5h; explicitly use socks5h for remote DNS.
The ambiguous legacy socks scheme is treated as SOCKS5, never SOCKS4.

Official references, checked against the installed source on 2026-11-17:
https://www.python-httpx.org/advanced/proxies/
https://www.python-httpx.org/environment_variables/
https://websockets.readthedocs.io/en/15.0.1/topics/proxies.html
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit
from urllib.request import getproxies, proxy_bypass

import httpx


class ProxyConfigurationError(ValueError):
    """Safe user-visible error; never include a proxy URL or credentials."""


def normalize_proxy(value: str, *, system_socks: bool = False) -> str:
    try:
        value = value.strip()
        if "://" not in value:
            value = "http://" + value
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if system_socks and scheme == "http":
            scheme = "socks5h"
        elif scheme == "socks":
            scheme = "socks5"
        if (scheme not in {"http", "https", "socks5", "socks5h"}
                or not parsed.hostname or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}
                or parsed.port is not None and not 1 <= parsed.port <= 65535
                or any(character.isspace() for character in parsed.netloc)):
            raise ValueError
        # Accessing .port above validates malformed / non-numeric values too.
        return urlunsplit((scheme, parsed.netloc, "", "", ""))
    except (AttributeError, TypeError, ValueError):
        raise ProxyConfigurationError(
            "系统代理地址格式不受支持。请使用 HTTP、HTTPS、SOCKS5 或 SOCKS5H 代理；修改后重新启动应用。"
        ) from None


def _bypass(url: str, proxies: dict[str, str]) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
        if address.is_loopback:
            return True
    except ValueError:
        address = None
    default_port = 443 if parsed.scheme in {"https", "wss"} else 80
    port = parsed.port or default_port
    bypass_list = proxies.get("no")
    if bypass_list is None:
        # Preserve operating-system bypass lists on Windows and macOS.
        return proxy_bypass(f"{host}:{port}")
    for item in bypass_list.split(","):
        item = item.strip().lower()
        if item == "*":
            return True
        if not item:
            continue
        # CIDR is commonly emitted by desktop proxy clients.
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            pass
        else:
            if address is not None and address in network:
                return True
            continue
        try:
            if "://" in item:
                exception = urlsplit(item)
                if exception.scheme != parsed.scheme:
                    continue
            else:
                if item.count(":") > 1 and not item.startswith("["):
                    item = f"[{item}]"
                exception = urlsplit("//" + item)
            exception_host = (exception.hostname or "").lstrip("*.").rstrip(".")
            exception_port = exception.port
        except ValueError:
            continue
        if exception_host and (exception_port is None or exception_port == port):
            if host == exception_host or host.endswith("." + exception_host):
                return True
    return False


def proxy_for_url(url: str) -> str | None:
    """Honor uppercase/lowercase environment and OS proxies, plus ALL_PROXY.

    WebSocket-specific settings take priority, then OS SOCKS and HTTPS/HTTP.
    ALL_PROXY is a final fallback for both HTTP and WebSocket. An invalid proxy
    for an unrelated protocol or a bypassed destination cannot break a request.
    """
    proxies = getproxies()
    if _bypass(url, proxies):
        return None
    scheme = urlsplit(url).scheme
    keys = {
        "http": ("http", "all"),
        "https": ("https", "all"),
        "ws": ("ws", "socks", "https", "http", "all"),
        "wss": ("wss", "socks", "https", "all"),
    }.get(scheme, ())
    for key in keys:
        value = proxies.get(key)
        if value:
            return normalize_proxy(value, system_socks=key == "socks")
    return None


def http_client(url: str) -> httpx.AsyncClient:
    proxy = proxy_for_url(url)
    try:
        # Keep custom CA environment settings, while preventing HTTPX from
        # parsing the unnormalized proxy environment a second time.
        context = httpx.create_ssl_context(verify=True, trust_env=True)
        return httpx.AsyncClient(proxy=proxy, trust_env=False, verify=context,
                                 timeout=httpx.Timeout(180, connect=15), follow_redirects=False)
    except ImportError:
        raise ProxyConfigurationError("代理运行组件缺失，请更新或重新安装完整应用。") from None
    except (ValueError, OSError):
        raise ProxyConfigurationError("网络代理或证书配置无效，请检查系统网络设置后重试。") from None
