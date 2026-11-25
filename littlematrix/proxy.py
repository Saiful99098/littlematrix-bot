"""Proxy / DNS — per-account proxy, rotation and custom DNS.

What this does:
* Keeps a pool of proxies and hands them out round-robin, so each
  account can run on its own IP.
* Validates proxy URLs (http://, https://, socks5://).
* Resolves a host to an IP, optionally through a custom DNS server
  (needs ``dnspython`` installed; without it, the system resolver
  is used and a warning is logged).

What it does NOT do: pyquotex's WebSocket path goes through
curl_cffi, which does not take a per-connection proxy for WS the
way the HTTP login path does. The proxy is passed to Quotex()
as ``proxies=...`` and is used for the HTTP login/session calls;
do not expect it to change the WS route by itself. If you need
full WS proxying, run the whole process behind a system-level
proxy / VPN.
"""

from __future__ import annotations

import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_VALID_SCHEMES = {"http", "https", "socks5", "socks5h"}


class ProxyManager:
    def __init__(self, proxies: list[str] | None = None, dns_servers: list[str] | None = None) -> None:
        self._proxies: list[str] = []
        for p in proxies or []:
            self.add_proxy(p)
        self.dns_servers = list(dns_servers or [])
        self._index = 0

    @staticmethod
    def validate(proxy: str) -> bool:
        parsed = urlparse(proxy)
        return parsed.scheme in _VALID_SCHEMES and bool(parsed.hostname)

    def add_proxy(self, proxy: str) -> None:
        if not self.validate(proxy):
            raise ValueError(f"Invalid proxy URL (use http://, https:// or socks5://): {proxy}")
        if proxy not in self._proxies:
            self._proxies.append(proxy)

    @property
    def proxies(self) -> list[str]:
        return list(self._proxies)

    def next_proxy(self) -> str | None:
        """Next proxy in round-robin order, or None if the pool is empty."""
        if not self._proxies:
            return None
        proxy = self._proxies[self._index % len(self._proxies)]
        self._index += 1
        return proxy

    def proxy_for_account(self, account_index: int) -> str | None:
        """Stable proxy per account index (same account, same proxy)."""
        if not self._proxies:
            return None
        return self._proxies[account_index % len(self._proxies)]

    def proxies_dict(self, proxy: str | None) -> dict[str, str] | None:
        """Shape Quotex() expects for its ``proxies`` argument."""
        if proxy is None:
            return None
        return {"http": proxy, "https": proxy}

    def resolve(self, host: str) -> str | None:
        """Resolve host -> IP. Uses custom DNS when dnspython is available."""
        if self.dns_servers:
            try:
                import dns.resolver  # type: ignore

                resolver = dns.resolver.Resolver()
                resolver.nameservers = self.dns_servers
                answer = resolver.resolve(host, "A")
                return str(answer[0])
            except ImportError:
                logger.warning("dnspython not installed, custom DNS ignored; using system DNS")
            except Exception as e:
                logger.warning("Custom DNS lookup for %s failed (%s); using system DNS", host, e)
        try:
            return socket.gethostbyname(host)
        except socket.gaierror:
            return None
