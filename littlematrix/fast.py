"""Fast execution — pre-warmed trades with measured latency.

Why a normal Quotex.buy() is slow: every call re-starts the
realtime price stream and re-syncs the server time before it
even sends the order. FastTrader warms those up once per asset
(``warmup()``), then each buy skips the warmup work:

* If the low-level ``client.api.buy`` channel is available, the
  order is sent through it directly and we wait only for the
  buy confirmation.
* Otherwise it falls back to the normal ``client.buy()``.

Every buy returns a FastTradeResult with ``latency_ms`` — the
real time from sending to confirmation — so "ultra fast" is a
measurement, not a slogan. Network + Quotex server time dominate
that number; no library can make it zero.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from pyquotex import expiration


@dataclass
class FastTradeResult:
    ok: bool
    result: Any
    latency_ms: float
    warmed: bool


class FastTrader:
    def __init__(self, client: Any) -> None:
        self.client = client
        self._warmed: set[str] = set()

    async def warmup(self, asset: str, duration: int = 60) -> None:
        """Pre-load the price stream + server time once for this asset."""
        try:
            await self.client.start_realtime_price(asset, duration)
        except Exception:
            pass
        try:
            await self.client.get_server_time()
        except Exception:
            pass
        self._warmed.add(asset)

    @property
    def warmed_assets(self) -> set[str]:
        return set(self._warmed)

    async def buy_fast(
        self,
        amount: float,
        asset: str,
        direction: str,
        duration: int,
        timeout: float = 35.0,
    ) -> FastTradeResult:
        """Buy, measuring latency. Warms up first if this asset is new."""
        if asset not in self._warmed:
            await self.warmup(asset, duration)

        start = time.monotonic()
        api = getattr(self.client, "api", None)
        # Low-level fast path: send the order directly, wait for confirm.
        if api is not None and getattr(api, "buy", None) is not None and hasattr(api, "slots"):
            try:
                request_id = expiration.get_timestamp()
                api.buy_id = None
                api.slots.buy_confirm.clear()
                await api.buy(amount, asset, direction, duration, request_id, True, "TIME")
                event_data = await api.slots.buy_confirm.wait(timeout=timeout)
                latency = (time.monotonic() - start) * 1000
                if isinstance(event_data, dict) and "error" in event_data:
                    return FastTradeResult(False, event_data["error"], latency, True)
                return FastTradeResult(True, event_data, latency, True)
            except asyncio.TimeoutError:
                return FastTradeResult(False, "Timeout", (time.monotonic() - start) * 1000, True)
            except Exception as e:
                # Fall through to the normal path on any low-level error.
                pass

        ok, result = await self.client.buy(amount, asset, direction, duration)
        latency = (time.monotonic() - start) * 1000
        return FastTradeResult(ok, result, latency, asset in self._warmed)
