"""Multilogin — run several Quotex accounts at once.

The open-source pyquotex client handles one account per Quotex
instance. This manager keeps one instance per account, connects
them in parallel, and picks the best connected account for a trade
(highest balance, or first connected on a tie). If one account
disconnects, the next one takes over — no single point of failure.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from pyquotex.stable_api import Quotex


@dataclass
class AccountConfig:
    email: str
    password: str
    proxy: str | dict[str, str] | None = None
    host: str = "market-qx.info"
    lang: str = "pt"
    label: str = ""


class MultiLoginManager:
    """Manage several Quotex accounts concurrently."""

    def __init__(self, client_factory: Callable[..., Any] | None = None) -> None:
        self._factory = client_factory or Quotex
        self._accounts: list[AccountConfig] = []
        self._clients: dict[str, Any] = {}
        self._balances: dict[str, float | None] = {}

    def add_account(self, config: AccountConfig) -> None:
        if any(a.email == config.email for a in self._accounts):
            raise ValueError(f"Account already added: {config.email}")
        self._accounts.append(config)

    def add(self, email: str, password: str, proxy: str | None = None, **kwargs: Any) -> None:
        self.add_account(AccountConfig(email=email, password=password, proxy=proxy, **kwargs))

    @property
    def accounts(self) -> list[AccountConfig]:
        return list(self._accounts)

    def _make_client(self, config: AccountConfig) -> Any:
        return self._factory(
            email=config.email,
            password=config.password,
            host=config.host,
            lang=config.lang,
            proxies=config.proxy,
        )

    async def connect_all(self) -> dict[str, tuple[bool, str]]:
        """Connect every account in parallel. Returns email -> (ok, reason)."""
        async def _connect_one(config: AccountConfig) -> tuple[str, tuple[bool, str]]:
            client = self._make_client(config)
            try:
                ok, reason = await client.connect()
            except Exception as e:  # never let one account kill the rest
                ok, reason = False, str(e)
            if ok:
                self._clients[config.email] = client
            return config.email, (ok, reason)

        results = await asyncio.gather(*[_connect_one(a) for a in self._accounts])
        return dict(results)

    def connected_clients(self) -> dict[str, Any]:
        return dict(self._clients)

    def connected_emails(self) -> list[str]:
        return list(self._clients)

    async def get_balances(self) -> dict[str, float | None]:
        """Balance per connected account (None if the call fails)."""
        out: dict[str, float | None] = {}
        for email, client in self._clients.items():
            try:
                balance = await client.get_balance()
                out[email] = float(balance) if balance is not None else None
            except Exception:
                out[email] = None
        self._balances = out
        return dict(out)

    def best_account(self) -> str | None:
        """Email of the connected account with the highest known balance."""
        if not self._clients:
            return None
        if self._balances:
            ranked = sorted(
                self._clients,
                key=lambda e: (self._balances.get(e) is not None, self._balances.get(e) or 0),
                reverse=True,
            )
            return ranked[0]
        return next(iter(self._clients))

    async def buy_on_best(
        self, amount: float, asset: str, direction: str, duration: int
    ) -> tuple[str | None, bool, Any]:
        """Buy on the best connected account. Returns (email, ok, result)."""
        email = self.best_account()
        if email is None:
            return None, False, "No account connected"
        client = self._clients[email]
        ok, result = await client.buy(amount, asset, direction, duration)
        return email, ok, result

    async def close_all(self) -> None:
        for client in list(self._clients.values()):
            try:
                await client.close()
            except Exception:
                pass
        self._clients.clear()
