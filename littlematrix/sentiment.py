"""Advanced sentiment — more than raw buy/sell percentages.

pyquotex already streams realtime sentiment (call/put percentages
per asset). This analyser keeps a short history of those readings
and derives what a trader actually wants:

* pressure  — call% minus put%  (-100 .. +100)
* trend     — pressure rising, falling or flat over the last readings
* spike     — a sudden pressure jump (>= spike_threshold in one step)
* signal    — STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL

Thresholds are configurable; nothing here is a trading recommendation,
it is a measurement of what other traders are doing right now.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SentimentSignal:
    asset: str
    call_pct: float
    put_pct: float
    pressure: float  # call - put
    trend: str  # "rising" | "falling" | "flat"
    spike: bool
    signal: str  # STRONG_BUY | BUY | NEUTRAL | SELL | STRONG_SELL
    samples: int
    timestamp: float = field(default_factory=time.time)


class SentimentAnalyzer:
    def __init__(
        self,
        asset: str,
        strong_threshold: float = 30.0,
        signal_threshold: float = 10.0,
        spike_threshold: float = 20.0,
        history_size: int = 20,
    ) -> None:
        self.asset = asset
        self.strong_threshold = strong_threshold
        self.signal_threshold = signal_threshold
        self.spike_threshold = spike_threshold
        self._history: deque[SentimentSignal] = deque(maxlen=history_size)
        self._pressures: deque[float] = deque(maxlen=history_size)

    @staticmethod
    def _parse(data: dict[str, Any]) -> tuple[float, float]:
        """Accept the shapes pyquotex returns: call/put, bulls/bears, buy/sell."""
        call = data.get("call", data.get("bulls", data.get("buy", 50)))
        put = data.get("put", data.get("bears", data.get("sell", 50)))
        return float(call), float(put)

    def update(self, data: dict[str, Any]) -> SentimentSignal:
        call, put = self._parse(data)
        pressure = call - put
        spike = False
        if self._pressures:
            spike = abs(pressure - self._pressures[-1]) >= self.spike_threshold
        self._pressures.append(pressure)

        trend = "flat"
        if len(self._pressures) >= 3:
            last3 = list(self._pressures)[-3:]
            if last3[2] > last3[1] > last3[0]:
                trend = "rising"
            elif last3[2] < last3[1] < last3[0]:
                trend = "falling"

        if pressure >= self.strong_threshold:
            signal = "STRONG_BUY"
        elif pressure >= self.signal_threshold:
            signal = "BUY"
        elif pressure <= -self.strong_threshold:
            signal = "STRONG_SELL"
        elif pressure <= -self.signal_threshold:
            signal = "SELL"
        else:
            signal = "NEUTRAL"

        result = SentimentSignal(
            asset=self.asset,
            call_pct=call,
            put_pct=put,
            pressure=pressure,
            trend=trend,
            spike=spike,
            signal=signal,
            samples=len(self._pressures),
        )
        self._history.append(result)
        return result

    @property
    def latest(self) -> SentimentSignal | None:
        return self._history[-1] if self._history else None

    @property
    def history(self) -> list[SentimentSignal]:
        return list(self._history)

    def average_pressure(self) -> float | None:
        if not self._pressures:
            return None
        return sum(self._pressures) / len(self._pressures)
