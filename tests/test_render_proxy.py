"""Tests for the Render 403 fix: QUOTEX_PROXY / QUOTEX_HOST wiring.

Run: python tests/test_render_proxy.py

Background (2026-11-25): the Render collector gets
"Quotex login failed: Error on access page: 403" because the access-page
GET (https://market-qx.info/pt) is blocked from Render's datacenter IP.
The lib already accepted Quotex(proxies=...), but the proxy never
reached the HTTP login: QuotexAPI.login built Login(self) with no
proxies kwarg, and Browser only uses its own `proxies` kwarg.

Must pass:
a) config.quotex_proxies_dict() — env string -> {"http","https"} dict,
   empty -> None, unsupported scheme -> None.
b) The plumbing gap is fixed: QuotexAPI(proxies={...}).login.proxies
   is that same dict (before the fix it was None).
c) SharedQuotexSession.connect() passes proxies + host to Quotex()
   (mocked constructor, no real network, no real login).
d) With no QUOTEX_PROXY set, connect() passes proxies=None and
   host=None (default market-qx.info, unchanged behaviour).

No real Quotex login is attempted anywhere here.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import collector, config


def test_quotex_proxies_dict():
    real = config.QUOTEX_PROXY
    try:
        config.QUOTEX_PROXY = ""
        assert config.quotex_proxies_dict() is None
        config.QUOTEX_PROXY = "http://user:pass@proxy.example:8080"
        assert config.quotex_proxies_dict() == {
            "http": "http://user:pass@proxy.example:8080",
            "https": "http://user:pass@proxy.example:8080",
        }
        config.QUOTEX_PROXY = "proxy.example:8080"
        assert config.quotex_proxies_dict() == {
            "http": "http://proxy.example:8080",
            "https": "http://proxy.example:8080",
        }
        config.QUOTEX_PROXY = "socks5://127.0.0.1:1080"
        assert config.quotex_proxies_dict() == {
            "http": "socks5://127.0.0.1:1080",
            "https": "socks5://127.0.0.1:1080",
        }
        config.QUOTEX_PROXY = "ftp://proxy.example:21"
        assert config.quotex_proxies_dict() is None
    finally:
        config.QUOTEX_PROXY = real
    print("PASS a) quotex_proxies_dict — env string -> http/https dict, "
          "empty/unsupported -> None")


def test_login_property_passes_proxies():
    from pyquotex.api import QuotexAPI

    proxies = {"http": "http://user:pass@proxy.example:8080",
               "https": "http://user:pass@proxy.example:8080"}
    api = QuotexAPI("market-qx.info", "u@example.com", "dummy",
                    "pt", proxies=proxies)
    # The gap fix: the Login handler must carry the same proxy dict,
    # otherwise the access-page GET goes direct and 403s on Render.
    assert api.login.proxies == proxies, api.login.proxies
    api2 = QuotexAPI("market-qx.info", "u@example.com", "dummy", "pt")
    assert api2.login.proxies is None
    print("PASS b) QuotexAPI.login carries proxies (gap fixed) — "
          "None when no proxy is set")


class FakeQuotex:
    """Stand-in for pyquotex.stable_api.Quotex — records constructor
    kwargs, never touches the network."""

    seen_kwargs = []

    def __init__(self, **kwargs):
        FakeQuotex.seen_kwargs.append(kwargs)
        self.kwargs = kwargs

    async def connect(self):
        return True, "ok"

    async def close(self):
        pass


def _run_connect_with(proxy_env, host_env):
    import pyquotex.stable_api as stable_api_mod

    real_quotex = stable_api_mod.Quotex
    real_email = config.QUOTEX_EMAIL
    real_password = config.QUOTEX_PASSWORD
    real_proxy = config.QUOTEX_PROXY
    real_host = config.QUOTEX_HOST
    FakeQuotex.seen_kwargs = []
    try:
        stable_api_mod.Quotex = FakeQuotex
        config.QUOTEX_EMAIL = "pc@test.example"
        config.QUOTEX_PASSWORD = "dummy"
        config.QUOTEX_PROXY = proxy_env
        config.QUOTEX_HOST = host_env
        session = collector.SharedQuotexSession()
        asyncio.run(session.connect())
        assert session.connected is True
        asyncio.run(session.close())
    finally:
        stable_api_mod.Quotex = real_quotex
        config.QUOTEX_EMAIL = real_email
        config.QUOTEX_PASSWORD = real_password
        config.QUOTEX_PROXY = real_proxy
        config.QUOTEX_HOST = real_host
    assert len(FakeQuotex.seen_kwargs) == 1, FakeQuotex.seen_kwargs
    return FakeQuotex.seen_kwargs[0]


def test_session_connect_passes_proxy_and_host():
    kwargs = _run_connect_with("http://user:pass@proxy.example:8080",
                               "qxbroker.com")
    assert kwargs["email"] == "pc@test.example"
    assert kwargs["proxies"] == {
        "http": "http://user:pass@proxy.example:8080",
        "https": "http://user:pass@proxy.example:8080",
    }, kwargs
    assert kwargs["host"] == "qxbroker.com", kwargs
    print("PASS c) SharedQuotexSession.connect() passes proxies + host "
          "to Quotex()")


def test_session_connect_no_proxy_unchanged():
    kwargs = _run_connect_with("", None)
    assert kwargs["proxies"] is None, kwargs
    assert kwargs["host"] is None, kwargs
    print("PASS d) no proxy/host set — proxies=None, host=None "
          "(default behaviour unchanged)")


def main():
    test_quotex_proxies_dict()
    test_login_property_passes_proxies()
    test_session_connect_passes_proxy_and_host()
    test_session_connect_no_proxy_unchanged()
    print("ALL TESTS PASSED (4/4)")


if __name__ == "__main__":
    main()
