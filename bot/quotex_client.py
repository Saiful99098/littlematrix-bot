"""Quotex client wrapper — shared session, 429-safe, sample fallback.

Local-test behaviour (live mode):
- If DATA_MODE=live AND QUOTEX_EMAIL + QUOTEX_PASSWORD are set in .env,
  fetch_yesterday_candles() really logs in ONCE per call with the bundled
  pyquotex lib and calls get_historical_candles() for 2 days (172800s,
  period 60, max_workers 5), then filters to the requested date.
  2 days is required: get_historical_candles() fetches back from NOW,
  so 1 day (86400s) only reaches back to the same time yesterday.
  Yesterday 00:00-01:59 is ~36h ago at noon today, so 1-day fetch gave
  Candles: 0 for that window. 2-day fetch covers the full yesterday.
  One call per asset, 3 assets at a time for SELECT ALL.
- IMPORTANT FIX (2026-11-25): fetch_yesterday_candles() is ASYNC and
  must be awaited from the Telegram handler. The old sync version used
  asyncio.run() inside the bot's running event loop, which raises
  'asyncio.run() cannot be called from a running event loop', was
  swallowed as None, and showed as 'no-data' with no signal.
  Never call asyncio.run() from inside a running loop.
- Fetched candles are cached to data/candles_<asset>_<date>.json
  (H/L/O/C + time + Dhaka datetime, same format as the sample file),
  so a second request for the same asset/date does NOT log in again.
- If DATA_MODE=sample or no login is set, or the import/login fails,
  return None and the caller falls back to the bundled sample file
  (USDMXN_otc only). Login failures now raise RuntimeError with the
  reason (shown to the user) instead of silently returning None.
- 429 handling: raise QuotexRateLimited with a clear English message.
  Never retry in a loop. The caller shows the message and stops.
- get_historical_candles fetches back from NOW, so 'yesterday' means
  the last 24h. For an old date like 2026-11-21 the live path cannot
  go back that far — handlers fall back to the sample file for that
  date, or to a cached candles_*.json file in data/ if you collected
  it earlier with collect_yesterday.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

logger = logging.getLogger("littlematrix.quotex")


class QuotexRateLimited(Exception):
    """Quotex temporarily blocked us (HTTP 429). Wait, do not retry."""


class QuotexNotConfigured(Exception):
    pass


_cache = {"payout": (0.0, None), "market": (0.0, None)}
_CACHE_TTL = 60  # seconds
# In-memory cache: (asset, date_str) -> list of raw candle dicts
_mem_cache = {}


def _live_available():
    if config.DATA_MODE != "live":
        return False
    return config.has_quotex_login()


_last_import_error = ""


def _load_quotex():
    """Try to import pyquotex from the bundled vendor copy."""
    global _last_import_error
    try:
        from pyquotex.stable_api import Quotex  # noqa: F401
        return True
    except Exception as exc:
        _last_import_error = str(exc)
        logger.warning("pyquotex import failed: %s", exc)
        return False


def _dhaka_datetime(ts):
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Dhaka")
    except Exception:
        tz = timezone(timedelta(hours=6))
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(tz)


def _to_raw_candles(candles):
    """pyquotex candle dicts -> sample-file format.

    Input: {time: unix, open, high, low, close}
    Output: {time, datetime (Dhaka 'YYYY-MM-DD HH:MM:SS'), H, L, O, C}
    Sorted, de-duplicated by time.
    """
    seen = {}
    for c in candles or []:
        if not isinstance(c, dict) or "time" not in c:
            continue
        ts = int(c["time"])
        if ts in seen:
            continue
        dt = _dhaka_datetime(ts).strftime("%Y-%m-%d %H:%M:%S")
        seen[ts] = {
            "time": ts,
            "datetime": dt,
            "H": c.get("high"),
            "L": c.get("low"),
            "O": c.get("open"),
            "C": c.get("close"),
        }
    return [seen[ts] for ts in sorted(seen)]


def _cache_path(asset, date_str):
    safe = asset.replace("/", "_").replace("-", "_")
    return Path(config.BASE_DIR) / "data" / f"candles_{safe}_{date_str}.json"


def _save_cache(asset, date_str, raw_candles):
    try:
        path = _cache_path(asset, date_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "asset": asset,
            "period": 60,
            "count": len(raw_candles),
            "candles": raw_candles,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("save cache failed for %s: %s", asset, exc)


def _load_cache(asset, date_str):
    path = _cache_path(asset, date_str)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        candles = data.get("candles", []) if isinstance(data, dict) else data
        if candles:
            return candles
    except Exception:
        return None
    return None


# Open-assets cache: {"ts": float, "assets": list | None}
_open_assets_cache = {"ts": 0.0, "assets": None}
_OPEN_ASSETS_TTL = 300  # seconds


async def get_open_assets():
    """Return a sorted list of currently OPEN Quotex asset names.

    Logs in ONCE with pyquotex, calls client.get_instruments(),
    keeps rows where i[14] is True (open), returns i[1] names
    (e.g. "EURUSD_otc"). Result is cached in memory for 5 minutes.
    Returns None when not in live mode, when no login is set, or on
    any failure — the caller then falls back to config.SAMPLE_ASSETS.
    Raises QuotexRateLimited on 429, never retries in a loop.
    The client is closed in a finally block.
    """
    if not _live_available():
        return None
    now = time.time()
    if (_open_assets_cache["assets"] is not None
            and now - _open_assets_cache["ts"] < _OPEN_ASSETS_TTL):
        return list(_open_assets_cache["assets"])
    if not _load_quotex():
        return None
    from pyquotex.stable_api import Quotex

    client = Quotex(email=config.QUOTEX_EMAIL, password=config.QUOTEX_PASSWORD)
    try:
        check, message = await client.connect()
        if not check:
            msg = str(message or "")
            if "429" in msg or "temporarily" in msg.lower() or "blocked" in msg.lower():
                raise QuotexRateLimited(
                    "Quotex temporarily blocked us (429). Please wait 15-30 minutes, "
                    "do not retry.")
            raise RuntimeError(f"Quotex login failed: {message}")
        instruments = await client.get_instruments()
        open_assets = []
        for row in instruments or []:
            try:
                if len(row) > 14 and row[14] is True and row[1]:
                    open_assets.append(str(row[1]))
            except Exception:
                continue
        open_assets = sorted(set(open_assets))
        _open_assets_cache["ts"] = now
        _open_assets_cache["assets"] = open_assets
        return list(open_assets)
    except QuotexRateLimited:
        raise
    except RuntimeError:
        raise
    except Exception as exc:
        logger.warning("get_open_assets failed: %s", exc)
        if "429" in str(exc):
            raise QuotexRateLimited(
                "Quotex temporarily blocked us (429). Please wait 15-30 minutes, "
                "do not retry.")
        return None
    finally:
        try:
            await client.close()
        except Exception:
            pass


async def _fetch_one_day_async(asset, date_str):
    """Real live fetch: 2 days back, filtered to the requested date.

    get_historical_candles() fetches back from NOW, so to cover a full
    yesterday (00:00-23:59) we must fetch 2 days (172800s), not 1 day.
    At noon today, yesterday 00:00 is ~36h ago, outside a 24h fetch.
    """
    from pyquotex.stable_api import Quotex

    client = Quotex(email=config.QUOTEX_EMAIL, password=config.QUOTEX_PASSWORD)
    try:
        check, message = await client.connect()
        if not check:
            msg = str(message or "")
            if "429" in msg or "temporarily" in msg.lower() or "blocked" in msg.lower():
                raise QuotexRateLimited(
                    "Quotex temporarily blocked us (429). Please wait 15-30 minutes, "
                    "do not retry.")
            raise RuntimeError(f"Quotex login failed: {message}")
        candles = await client.get_historical_candles(
            asset=asset,
            amount_of_seconds=172800,
            period=60,
            max_workers=5,
        )
        raw_all = _to_raw_candles(candles)
        # Filter to the requested date only.
        raw = [c for c in raw_all if c.get("datetime", "").startswith(date_str)]
        # If the requested date has no candles (e.g. market closed),
        # return the full 2-day list so the caller can see the range;
        # handlers filter by date again, so this is safe.
        if not raw:
            return raw_all
        return raw
    finally:
        try:
            await client.close()
        except Exception:
            pass


async def fetch_yesterday_candles(asset, date_str):
    """ASYNC helper used by handlers. Returns list of raw candle dicts or None.

    Order: memory cache -> data/candles_<asset>_<date>.json -> live Quotex
    (only if DATA_MODE=live and login is set) -> None.
    Raises QuotexRateLimited on 429 (no retry), RuntimeError on login failure.
    Must be awaited — do NOT wrap it in asyncio.run() from a running loop.
    """
    key = (asset, date_str)
    if key in _mem_cache:
        return _mem_cache[key]

    cached = _load_cache(asset, date_str)
    if cached:
        _mem_cache[key] = cached
        return cached

    if not _live_available():
        return None
    if not _load_quotex():
        raise RuntimeError(
            f"pyquotex import failed ({_last_import_error}). "
            "Run: pip install -r requirements.txt")

    # Live fetch gives the last 24h from now. If the requested date is
    # old (not today/yesterday), do not waste a login — return None so
    # the caller can fall back to sample / cached file.
    try:
        today = _dhaka_datetime(time.time()).date()
        yesterday = today - timedelta(days=1)
        req = datetime.strptime(date_str, "%Y-%m-%d").date()
        if req not in (today, yesterday):
            return None
    except Exception:
        pass

    try:
        raw = await _fetch_one_day_async(asset, date_str)
    except QuotexRateLimited:
        raise
    except RuntimeError:
        raise
    except Exception as exc:
        logger.warning("live fetch failed for %s: %s", asset, exc)
        if "429" in str(exc):
            raise QuotexRateLimited(
                "Quotex temporarily blocked us (429). Please wait 15-30 minutes, "
                "do not retry.")
        raise RuntimeError(f"Live fetch failed for {asset}: {exc}")

    if not raw:
        logger.warning("live fetch returned 0 candles for %s", asset)
        return None
    # raw is already filtered to date_str (or full 2-day list if that
    # date had no candles). Only cache under date_str when it matches.
    try:
        last_date = raw[-1]["datetime"][:10]
    except Exception:
        last_date = date_str
    _mem_cache[key] = raw
    if all(c.get("datetime", "").startswith(date_str) for c in raw):
        _mem_cache[(asset, last_date)] = raw
        _save_cache(asset, date_str, raw)
    else:
        _mem_cache[(asset, last_date)] = raw
        _save_cache(asset, last_date, raw)
    return raw


def get_payouts():
    """Live payouts with 60s cache. Returns None in sample mode."""
    if not _live_available():
        return None
    now = time.time()
    ts = _cache["payout"][0]
    if _cache["payout"][1] is not None and now - ts < _CACHE_TTL:
        return _cache["payout"][1]
    return None


def get_market_status():
    """Live market open/closed with 60s cache. Returns None in sample mode."""
    if not _live_available():
        return None
    now = time.time()
    ts = _cache["market"][0]
    if _cache["market"][1] is not None and now - ts < _CACHE_TTL:
        return _cache["market"][1]
    return None


async def fetch_batch_3_at_a_time_async(assets, fetch_fn):
    """Async 3-at-a-time batch. fetch_fn(asset) is an async function.

    Returns {asset: result}. This is the ONLY place the 3-at-a-time
    rule lives for live Quotex fetching.
    """
    results = {}
    for i in range(0, len(assets), 3):
        batch = assets[i:i + 3]
        for asset in batch:
            try:
                results[asset] = await fetch_fn(asset)
            except QuotexRateLimited:
                raise
            except Exception as exc:
                logger.warning("batch fetch failed for %s: %s", asset, exc)
                results[asset] = None
        await asyncio.sleep(1)  # small pause between batches, anti-429
    return results


def fetch_batch_3_at_a_time(assets, fetch_fn):
    """Sync 3-at-a-time batch (for non-async callers only).

    fetch_fn(asset) -> result. Returns {asset: result}.
    Do NOT use this with an async fetch_fn from inside a running loop.
    """
    results = {}
    for i in range(0, len(assets), 3):
        batch = assets[i:i + 3]
        for asset in batch:
            results[asset] = fetch_fn(asset)
        time.sleep(1)  # small pause between batches, anti-429
    return results
