"""LittleMatrix — the main client.

A thin, fast wrapper around Quotex (the fixed pyquotex core):
one account by default, or a MultiLoginManager for several.
Adds the premium helpers — sentiment analyser, fast trader and
deep-history candles — in one place, under the LittleMatrix name.
"""

from __future__ import annotations

import time
from typing import Any

from pyquotex.stable_api import Quotex

from .fast import FastTrader
from .multilogin import AccountConfig, MultiLoginManager
from .sentiment import SentimentAnalyzer


class LittleMatrix:
    """One LittleMatrix client = one Quotex account + premium helpers."""

    brand = "LittleMatrix"

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        host: str = "market-qx.info",
        lang: str = "pt",
        proxies: dict[str, str] | str | None = None,
    ) -> None:
        self.client = Quotex(
            email=email, password=password, host=host, lang=lang, proxies=proxies
        )
        self.fast = FastTrader(self.client)
        self._sentiment: dict[str, SentimentAnalyzer] = {}

    async def connect(self) -> tuple[bool, str]:
        return await self.client.connect()

    async def close(self) -> None:
        await self.client.close()

    async def get_balance(self) -> float | None:
        return await self.client.get_balance()

    async def get_candles_1d(self, asset: str, period: int = 60) -> list[dict[str, Any]]:
        """Full past day of candles (~1440 at 1m) via deep history."""
        return await self.client.get_historical_candles(asset, 86400, period)

    async def get_candles_timed(self, asset: str, seconds: int = 86400, period: int = 60):
        """Deep history with elapsed time, so speed is measurable."""
        start = time.monotonic()
        candles = await self.client.get_historical_candles(asset, seconds, period)
        return candles, (time.monotonic() - start) * 1000

    def sentiment_analyzer(self, asset: str) -> SentimentAnalyzer:
        if asset not in self._sentiment:
            self._sentiment[asset] = SentimentAnalyzer(asset)
        return self._sentiment[asset]

    async def buy_fast(self, amount: float, asset: str, direction: str, duration: int):
        return await self.fast.buy_fast(amount, asset, direction, duration)

    # -- multilogin convenience -------------------------------------------
    @staticmethod
    def multilogin() -> MultiLoginManager:
        return MultiLoginManager()

    @staticmethod
    def account(email: str, password: str, proxy: str | None = None) -> AccountConfig:
        return AccountConfig(email=email, password=password, proxy=proxy)
