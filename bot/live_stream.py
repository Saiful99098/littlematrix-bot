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


def _pick_stream_assets(assets, test_asset):
    """TEST MODE (2026-11-26): the 61-asset bulk subscribe never
    delivered a single 'candle-generated' event, even with the
    numeric-code follow sent for all 61. When LIVE_STREAM_TEST_ASSET
    names one asset, stream only that asset — if events flow for one,
    the server caps bulk subscribes; if not, the subscribe itself is
    wrong. An empty test asset (default) streams everything, as
    before. The name may use '-' or '_' (BTCUSD-OTC = BTCUSD_otc)."""
    if not test_asset:
        return list(assets)
    wanted = test_asset.replace("-", "_").upper()
    for asset in assets:
        if asset.replace("-", "_").upper() == wanted:
            logger.info("live stream: TEST MODE — streaming only %s", asset)
            return [asset]
    logger.warning("live stream: TEST asset %s is not open — streaming all %d assets",
                   test_asset, len(assets))
    return list(assets)


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
            # Batch truth: overwrite tick-built rows for the same minutes.
            total += db.insert_candles(asset, rows_from_raw(raw),
                                       on_conflict="update")
        if total:
            logger.info("live stream: tail-fill stored %d candles", total)
    except quotex_client.QuotexRateLimited:
        raise
    except Exception as exc:
        logger.warning("live stream: tail-fill failed: %s", exc)


def _store_tick_candles(db, ticks, period, buckets, built_until, now_s):
    """Build 1-minute candles from live price ticks and store the
    minutes that have closed.

    2026-11-26: the server never sends 'candle-generated' — not for
    61 assets, not even for 1 — but the SAME subscribe already
    delivers price ticks (the lib's own realtime-price path waits on
    them, not on the event). A tick is [asset, ts-seconds, price]:
    feed it through the vendored process_tick (open = first tick of
    the minute, close = latest, high/low = extremes). Closed minutes
    are inserted with on_conflict="nothing", so a tick-built row
    never overwrites a batch/event row — the 2-hourly batch
    (on_conflict="update") corrects it with the server's true candle
    within 2 hours. Returns (n_inserted, n_assets_with_ticks,
    newest_tick_ts|None)."""
    from pyquotex.utils.processor import process_tick

    cur_min = now_s - (now_s % period)
    inserted = 0
    newest_ts = None
    for asset, tick_list in (ticks or {}).items():
        if not tick_list:
            continue
        asset_buckets = buckets.setdefault(asset, {})
        for t in tick_list:
            try:
                ts = float(t["time"])
                price = float(t["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if ts > 1e11:  # milliseconds — normalize to seconds
                ts /= 1000.0
            if newest_ts is None or ts > newest_ts:
                newest_ts = ts
            minute = int(ts // period * period)
            if minute <= built_until.get(asset, -1):
                continue
            process_tick([asset, ts, price, 0], period, asset_buckets)
        for minute in sorted(asset_buckets):
            if minute >= cur_min:
                break
            candle = asset_buckets.pop(minute)
            row = {"ts": datetime.fromtimestamp(minute, tz=timezone.utc),
                   "open": candle["open"], "high": candle["high"],
                   "low": candle["low"], "close": candle["close"]}
            try:
                n = db.insert_candles(asset, [row])
            except Exception as exc:
                logger.warning("live stream: tick insert failed for %s: %s",
                               asset, exc)
                continue
            if n:
                inserted += 1
            built_until[asset] = minute
    n_assets = sum(1 for a in (ticks or {}) if ticks[a])
    return inserted, n_assets, newest_ts


async def _poll_loop(db, session, sleep, n_assets=0):
    """Watch for newly closed candles and store them.

    Returns only when the connection is lost or goes stale — the
    caller reconnects. Raises nothing except the injected sleep's
    stop signal (tests) and QuotexRateLimited.

    Diagnostic (the owner, 2026-11-26): the stream can sit
    "connected" while delivering nothing — Bot Status then shows
    last candle "—" and the checker shows "?" for recent minutes.
    So the loop reports, at most once a minute, which of the two
    silent states it is in: no candle-generated events at all, or
    events arriving but no candle stored (with a sample of the
    dropped event's keys, so the log shows whether 'open' is
    missing or the shape is wrong). Since 2026-11-26 the event
    channel never delivers at all — so closed minutes are ALSO
    built from live price ticks (_store_tick_candles), and the
    diagnostic says which channel is alive. The tail-fill and the
    2-hourly batch keep the DB fed meanwhile, so silence is a
    warning, never a crash.
    """
    period = config.LIVE_STREAM_PERIOD
    poll = config.LIVE_STREAM_POLL_SECONDS
    stale_after = config.LIVE_STREAM_STALE_MINUTES * 60
    diag_polls = max(1, int(60 // poll))  # ~60s between diagnostic lines
    last_ts = {}     # asset -> datetime (UTC) of last stored stream candle
    last_close = {}  # asset -> close of that candle
    # Seed for the FIRST event only: the DB's last candle (ts, close),
    # used solely to fill a missing 'open' when the event continues
    # it exactly. Never the dedup watermark — the tail-fill already
    # stored newer candles, so seeding last_ts itself would wrongly
    # suppress the stream's own older events.
    seed_ts = {}
    seed_close = {}
    seeded = set()
    last_any = time.monotonic()
    quiet_polls = 0    # consecutive polls with no events at all
    drop_polls = 0     # consecutive polls with events but 0 stored
    drop_sample = None  # (asset, keys, open_present) of a dropped event
    # Tick-built candles (2026-11-26): 'candle-generated' never
    # arrives, so closed minutes are built from live price ticks
    # instead — see _store_tick_candles. Buckets hold the minutes
    # still being built; built_until remembers the last stored
    # minute per asset so late ticks never rebuild it.
    tick_buckets = {}
    built_until = {}
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
        events_seen = 0
        dropped_this_poll = False
        for asset, by_period in generated.items():
            ev = (by_period or {}).get(period)
            if not isinstance(ev, dict) or not ev:
                continue
            events_seen += 1
            if asset not in last_ts and asset not in seeded:
                seeded.add(asset)
                try:
                    prev = db.last_candle(asset)
                except Exception:
                    prev = None
                if prev:
                    seed_ts[asset] = prev["ts"]
                    seed_close[asset] = prev["close"]
            row = _row_from_event(
                asset, ev,
                last_ts.get(asset, seed_ts.get(asset)),
                last_close.get(asset, seed_close.get(asset)))
            if row is None:
                dropped_this_poll = True
                drop_sample = (asset, sorted(ev.keys()), "open" in ev)
                continue
            if asset in last_ts and row["ts"] <= last_ts[asset]:
                continue
            try:
                # Server truth: a closed-candle event corrects a
                # tick-built row for the same minute at once.
                n = db.insert_candles(asset, [row], on_conflict="update")
            except Exception as exc:
                logger.warning("live stream: insert failed for %s: %s",
                               asset, exc)
                continue
            if n:
                inserted += 1
            last_ts[asset] = row["ts"]
            last_close[asset] = row["close"]
        # Tick path: build closed minutes from live price ticks.
        # Runs every poll; inert when no ticks arrive.
        tick_inserted = 0
        tick_assets = 0
        tick_age = None
        try:
            ticks = session.generated_ticks() or {}
        except Exception:
            ticks = {}
        try:
            tick_inserted, tick_assets, newest_ts = _store_tick_candles(
                db, ticks, period, tick_buckets, built_until,
                int(time.time()))
        except Exception as exc:
            logger.warning("live stream: tick build failed: %s", exc)
            tick_inserted, tick_assets, newest_ts = 0, 0, None
        if newest_ts is not None:
            tick_age = max(0, int(time.time() - newest_ts))
        if inserted or tick_inserted:
            last_any = time.monotonic()
            quiet_polls = 0
            drop_polls = 0
            _safe_set_state(db, "live_stream_last_seen", utc_iso(dhaka_now()))
            if inserted:
                logger.info("live stream: %d new candles", inserted)
            if tick_inserted:
                logger.info("live stream: %d tick-built candles",
                            tick_inserted)
        elif dropped_this_poll:
            # Events arrived but at least one was dropped by the row
            # builder (missing fields / missing 'open' with no
            # continuous previous candle). A healthy stream re-reads
            # the same already-stored event most polls — that is a
            # dedup skip, not a drop, and resets this counter.
            drop_polls += 1
            quiet_polls = 0
            if drop_polls >= diag_polls and drop_sample is not None:
                logger.warning(
                    "live stream: events arrived but 0 candles stored in "
                    "the last 60s (sample %s keys=%s open=%s — the event "
                    "shape is being dropped)", drop_sample[0],
                    drop_sample[1], drop_sample[2])
                drop_polls = 0
        elif events_seen:
            drop_polls = 0
            quiet_polls = 0
        else:
            quiet_polls += 1
            drop_polls = 0
            if quiet_polls >= diag_polls:
                if tick_assets:
                    # The event channel is dead but ticks flow and
                    # candles are being built from them — info, not a
                    # warning. (When tick_inserted > 0 this branch is
                    # never reached; this covers ticks arriving but no
                    # minute closing yet.)
                    logger.info(
                        "live stream: no candle-generated events in the "
                        "last 60s (%d assets subscribed) — building "
                        "candles from ticks (%d assets, newest tick %ds ago)",
                        n_assets, tick_assets,
                        tick_age if tick_age is not None else -1)
                else:
                    logger.warning(
                        "live stream: no candle-generated events in the last "
                        "60s (%d assets subscribed — server silent or "
                        "subscription not delivering)", n_assets)
                quiet_polls = 0
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
            assets = _pick_stream_assets(assets, config.LIVE_STREAM_TEST_ASSET)
            logger.info("live stream: subscribed to %d assets", len(assets))
            await session.start_asset_streams(assets, config.LIVE_STREAM_PERIOD)
            await _tail_fill(db, session, assets)
            _safe_set_state(db, "live_stream_status", "connected")
            run_started = time.monotonic()
            await _poll_loop(db, session, sleep, len(assets))
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
