"""Deterministic synthetic candles for tests.

Replaces the deleted USDMXN sample file
(data/sample_candles_USDMXN_otc_2026-11-21.json). Everything here is
generated from a fixed seed, so the numbers are stable across runs.
"""
from __future__ import annotations

import random


def make_day(asset, date_str, seed=7, base_price=19.90):
    """One full Dhaka day of 1-min candles (1440) as normalized dicts
    (datetime/open/high/low/close/hhmm). Seeded random walk with
    occasional DOJI candles (C == O)."""
    rng = random.Random(seed)
    price = base_price
    out = []
    for minute in range(1440):
        hh, mm = divmod(minute, 60)
        o = price
        drift = rng.uniform(-0.008, 0.008)
        c = o + drift
        if rng.random() < 0.02:  # DOJI
            c = o
        h = max(o, c) + rng.uniform(0, 0.002)
        low = min(o, c) - rng.uniform(0, 0.002)
        out.append({"datetime": f"{date_str} {hh:02d}:{mm:02d}:00",
                    "hhmm": f"{hh:02d}:{mm:02d}",
                    "open": round(o, 5), "high": round(h, 5),
                    "low": round(low, 5), "close": round(c, 5)})
        price = c
    return out


def force_candle(day, hhmm, direction):
    """Force one minute of a generated day to a known strong candle:
    direction CALL/PUT, or DOJI (C == O)."""
    for c in day:
        if c["hhmm"] == hhmm:
            if direction == "DOJI":
                c["close"] = c["open"]
                c["high"] = max(c["open"], c["close"]) + 0.001
                c["low"] = min(c["open"], c["close"]) - 0.001
            elif direction == "CALL":
                c["open"] = 1.00
                c["close"] = 1.10
                c["high"] = 1.11
                c["low"] = 0.99
            else:  # PUT
                c["open"] = 1.10
                c["close"] = 1.00
                c["high"] = 1.11
                c["low"] = 0.99
            return c
    raise KeyError(hhmm)


def to_raw(candles):
    """Normalized dicts -> raw-like dicts (H/L/O/C + datetime), the
    shape the DB insert path (rows_from_raw) reads."""
    return [{"datetime": c["datetime"], "O": c["open"], "H": c["high"],
             "L": c["low"], "C": c["close"]} for c in candles]
