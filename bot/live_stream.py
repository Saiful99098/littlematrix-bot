"""Live candle stream (hybrid): one persistent Quotex WebSocket that
writes each closed 1-minute candle for every open asset into the DB
as it arrives.

The 2-hourly batch collector stays the backbone (gap-filling source
of truth); the stream only makes data fresher — AI Signal, Market
Live, Trend Scan and Candle Scan then read candles at most ~1 minute
old instead of up to 2 hours old, and the CHECKER finds the recent
candles it needs right after a signal window ends.

Safety, in order:
- The batch collector still runs every 2 hours and fills any gap,
  so a dead stream can never lose data permanently.
- Inserts are idempotent (UNIQUE(asset, ts) — ON CONFLICT DO NOTHING),
  so a candle arriving via both stream and batch is stored once.
- One Quotex login at a time across the process (the collector's
  _CONNECT_LOCK): stream reconnects and collector runs never log in
  simultaneously.
- Reconnects back off (30s -> 300s cap); a 429 rests 10 minutes.
- On (re)connect the stream tail-fills each asset from its last
  stored candle, so a disconnect gap is closed immediately.
- The loop never raises out of live_stream_loop — a dead stream is
  logged loudly and retried, like the collector loop.

Set LIVE_STREAM_ENABLED=false to turn the stream off; the bot then
works exactly as before, from the 2-hourly batch alone.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from . import candles_db, config, quotex_client
from .candles_db import DHAKA, utc_iso
from .collector import (
    SharedQuotexSession,
    _fetch_assets_3_at_a_time,
    _safe_set_state,
    _tail_seconds_back,
    backfill_dates,
    day_window_utc,
    dhaka_now,
    fetch_seconds_back,
    rows_from_raw,
)

logger = logging.getLogger("littlematrix.live_stream")


def _row_from_event(asset, ev, prev_ts, prev_close):
    """One closed-candle server event -> DB row, or None to skip.

    The 'candle-generated' event carries index (unix ts), open, high,
    low, close. If open is missing, it is the previous candle's close
    — but only when the previous candle ends exactly where this one
    starts (continuous 1-min candles); otherwise the row is skipped
    and the 2-hourly batch fills it with the true values. A guessed
    open must never overwrite the batch's true candle (inserts are
    ON CONFLICT DO NOTHING).
    """
    try:
        idx = int(ev.get("index") or ev.get("time") or 0)
        high, low, close = ev.get("high"), ev.get("low"), ev.get("close")
        if not idx or None in (high, low, close):
            return None
        open_ = ev.get("open")
        if open_ is None:
            if (prev_ts is not None and prev_close is not None
                    and idx - int(prev_ts.timestamp()) == 60):
                open_ = prev_close
            else:
                return None
        ts = datetime.fromtimestamp(idx, tz=timezone.utc)
        return {"ts": ts, "open": float(open_), "high": float(high),
                "low": float(low), "close": float(close)}
    except (TypeError, ValueError):
        return None


async def _stream_assets(db, session):
    """Assets to stream: live open assets, falling back to every
    asset already in the DB when the live list fails."""
    try:
        assets = await session.get_open_assets()
    except Exception as exc:
        logger.warning("live stream: open assets failed: %s", exc)
        assets = None
    if assets:
        return sorted(set(assets))
    try:
        return sorted(db.get_assets())
    except Exception:
        return []


async def _tail_fill(db, session, assets, now=None):
    """Fetch only each asset's missing tail (last stored candle ->
    now) and store it — closes the gap a disconnect left behind.
    Reuses the collector's tail machinery; never raises."""
    now = now or dhaka_now()
    try:
        dates = backfill_dates(config.BACKFILL_DAYS, now)
        oldest_start, _ = day_window_utc(dates[-1])
        spans = {a: _tail_seconds_back(db, a, oldest_start, now)
                 for a in assets}
        results = await _fetch_assets_3_at_a_time(session, assets, spans)
        total = 0
        for asset, raw in results.items():
            if not raw:
                continue
            total += db.insert_candles(asset, rows_from_raw(raw))
        if total:
            logger.info("live stream: tail-fill stored %d candles", total)
    except quotex_client.QuotexRateLimited:
        raise
    except Exception as exc:
        logger.warning("live stream: tail-fill failed: %s", exc)


async def _poll_loop(db, session, sleep):
    """Watch for newly closed candles and store them.

    Returns only when the connection is lost or goes stale — the
    caller reconnects. Raises nothing except the injected sleep's
    stop signal (tests) and QuotexRateLimited.
    """
    period = config.LIVE_STREAM_PERIOD
    poll = config.LIVE_STREAM_POLL_SECONDS
    stale_after = config.LIVE_STREAM_STALE_MINUTES * 60
    last_ts = {}     # asset -> datetime (UTC) of last stored stream candle
    last_close = {}  # asset -> close of that candle
    last_any = time.monotonic()
    while True:
        await sleep(poll)
        if not await session.is_connected():
            logger.warning("live stream: connection lost")
            return
        try:
            generated = session.generated_candles() or {}
        except Exception:
            generated = {}
        inserted = 0
        for asset, by_period in generated.items():
            ev = (by_period or {}).get(period)
            if not isinstance(ev, dict):
                continue
            row = _row_from_event(asset, ev, last_ts.get(asset),
                                  last_close.get(asset))
            if row is None:
                continue
            if asset in last_ts and row["ts"] <= last_ts[asset]:
                continue
            try:
                n = db.insert_candles(asset, [row])
            except Exception as exc:
                logger.warning("live stream: insert failed for %s: %s",
                               asset, exc)
                continue
            if n:
                inserted += 1
            last_ts[asset] = row["ts"]
            last_close[asset] = row["close"]
        if inserted:
            last_any = time.monotonic()
            _safe_set_state(db, "live_stream_last_seen", utc_iso(dhaka_now()))
            logger.info("live stream: %d new candles", inserted)
        if time.monotonic() - last_any > stale_after:
            logger.warning("live stream: stale — no new candle for %d "
                           "minutes, reconnecting",
                           config.LIVE_STREAM_STALE_MINUTES)
            return


async def live_stream_loop(db=None, session_factory=None, sleep=None):
    """Keep the live candle stream running forever. Never raises out
    of the loop — disconnects back off and reconnect, a 429 rests
    10 minutes, and every other error is logged and retried. The
    2-hourly batch collector keeps filling any gap meanwhile.
    """
    db = db or candles_db.get_candle_db()
    session_factory = session_factory or SharedQuotexSession
    sleep = sleep or asyncio.sleep
    backoff = 30
    logger.info("live stream: starting (all open assets, %d-second candles)",
                config.LIVE_STREAM_PERIOD)
    while True:
        session = None
        try:
            session = session_factory()
            await session.connect()
            assets = await _stream_assets(db, session)
            if not assets:
                raise RuntimeError("no assets to stream")
            logger.info("live stream: subscribed to %d assets", len(assets))
            await session.start_asset_streams(assets, config.LIVE_STREAM_PERIOD)
            await _tail_fill(db, session, assets)
            _safe_set_state(db, "live_stream_status", "connected")
            run_started = time.monotonic()
            await _poll_loop(db, session, sleep)
            # A long clean run resets the backoff; a flapping
            # connection keeps doubling it (up to 5 minutes).
            if time.monotonic() - run_started > 300:
                backoff = 30
            logger.warning("live stream: disconnected, reconnecting")
        except quotex_client.QuotexRateLimited:
            logger.warning("live stream: 429 — resting 10 minutes")
            await sleep(600)
            backoff = 30
            continue
        except Exception as exc:
            logger.warning("live stream: %s", exc)
        finally:
            _safe_set_state(db, "live_stream_status", "reconnecting")
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    pass
        await sleep(backoff)
        backoff = min(backoff * 2, 300)
