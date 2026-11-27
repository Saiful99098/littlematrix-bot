"""Live candle stream (hybrid): one persistent Quotex WebSocket that
writes each closed 1-minute candle for every open asset into the DB
as it arrives.

The batch collector stays the backbone (gap-filling source
of truth); the stream only makes data fresher — AI Signal, Market
Live, Trend Scan and Candle Scan then read candles at most ~1 minute
old instead of up to 30 minutes old, and the CHECKER finds the recent
candles it needs right after a signal window ends.

Safety, in order:
- The batch collector still runs every 30 minutes and fills any gap,
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
works exactly as before, from the batch collector alone.
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
    recent_otc_assets_missing,
    rows_from_raw,
)

logger = logging.getLogger("littlematrix.live_stream")

# A subscribed asset counts as "was trading recently" when its last
# stored candle is within this long (24h). Used by the never-ticked
# check: a subscribed asset that was trading recently but delivers
# no tick at all in this run has a broken subscription.
_NEVER_TICKED_RECENT_SECONDS = 86400
# After a never-ticked reconnect, don't re-trigger for the same
# asset within this long (2h) — bounds reconnect flapping when a
# subscription stays broken.
_NEVER_TICKED_COOLDOWN_SECONDS = 7200
# Tail-fill skip window (the owner, 2026-11-27): an asset whose last
# stored candle is this fresh has no gap worth fetching — the fetch
# floor is 10 min, so fetching it would only re-store minutes the DB
# already has. Skipping current assets is what keeps the tail-fill
# cheap in the normal case.
_TAIL_FILL_SKIP_SECONDS = 120


def _tail_is_stale(db, asset, now):
    """True when the asset's last DB candle is older than the skip
    window (or missing/unreadable) — it has a real gap worth fetching."""
    try:
        last = db.last_candle_time(asset)
    except Exception:
        return True
    if not last:
        return True
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return True
    return ((now.astimezone(timezone.utc) - last_dt).total_seconds()
            > _TAIL_FILL_SKIP_SECONDS)


def _row_from_event(asset, ev, prev_ts, prev_close):
    """One closed-candle server event -> DB row, or None to skip.

    The 'candle-generated' event carries index (unix ts), open, high,
    low, close. If open is missing, it is the previous candle's close
    — but only when the previous candle ends exactly where this one
    starts (continuous 1-min candles); otherwise the row is skipped
    and the batch collector fills it with the true values. A guessed
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
        # The open list can silently drop a pair while its market
        # stays open (the owner, 2026-11-27: 20 major forex OTC pairs
        # vanished at 10:44) — add recently-trading OTC assets from
        # the DB so they keep streaming and tail-filling anyway.
        missing = recent_otc_assets_missing(db, assets)
        if missing:
            logger.warning("live stream: %d recently-trading OTC asset(s) "
                           "missing from the Quotex open list — adding "
                           "from DB: %s%s",
                           len(missing), ", ".join(sorted(missing)[:10]),
                           f" +{len(missing) - 10} more" if len(missing) > 10 else "")
            assets = sorted(set(assets) | set(missing))
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
    Reuses the collector's tail machinery; never raises.

    Assets already current (last candle within _TAIL_FILL_SKIP_SECONDS)
    are skipped: fetching them would only re-store minutes the DB
    already has (the fetch floor is 10 min), and skipping them is
    what keeps the tail-fill cheap in the normal case — a restart a
    minute after the old instance stopped has almost nothing to fetch.
    """
    now = now or dhaka_now()
    try:
        dates = backfill_dates(config.BACKFILL_DAYS, now)
        oldest_start, _ = day_window_utc(dates[-1])
        todo = [a for a in assets if _tail_is_stale(db, a, now)]
        if not todo:
            return
        spans = {a: _tail_seconds_back(db, a, oldest_start, now)
                 for a in todo}
        results = await _fetch_assets_3_at_a_time(session, todo, spans)
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
    never overwrites a batch/event row — the batch collector
    (on_conflict="update") corrects it with the server's true candle
    within 30 minutes. Returns (n_inserted, n_assets_with_ticks,
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


async def _poll_loop(db, session, sleep, assets=(), never_cool=None):
    """Watch for newly closed candles and store them.

    Returns only when the connection is lost, goes stale, one asset
    goes silent while others tick, or a subscribed asset never ticks
    at all in this run — the caller reconnects. Raises nothing
    except the injected sleep's stop signal (tests) and
    QuotexRateLimited.

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
    batch collector keep the DB fed meanwhile, so silence is a
    warning, never a crash.
    """
    period = config.LIVE_STREAM_PERIOD
    poll = config.LIVE_STREAM_POLL_SECONDS
    stale_after = config.LIVE_STREAM_STALE_MINUTES * 60
    n_assets = len(assets)
    never_cool = never_cool if never_cool is not None else {}
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
    last_tick = {}  # asset -> newest tick unix-seconds seen in THIS run
    loop_started = time.monotonic()
    # Assets that were trading recently (last DB candle within 24h)
    # but have not ticked at all in THIS run — see the never-ticked
    # check at the end of the poll. Closed assets are never in the
    # open-assets list, so a subscribed asset in this set is one
    # whose subscription is broken, not one whose market is shut.
    expected = {}
    if assets:
        now_ts = time.time()
        for asset in assets:
            try:
                prev = db.last_candle(asset)
            except Exception:
                prev = None
            if not prev or prev.get("ts") is None:
                continue
            try:
                age = now_ts - prev["ts"].timestamp()
            except Exception:
                continue
            if age < _NEVER_TICKED_RECENT_SECONDS:
                expected[asset] = prev["ts"]
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
        now_s = int(time.time())
        try:
            ticks = session.generated_ticks() or {}
        except Exception:
            ticks = {}
        # Per-asset liveness (the owner, 2026-11-27): remember the
        # newest tick seen for each asset in THIS run — the
        # per-asset silence check at the end of the poll uses it.
        for asset, tick_list in (ticks or {}).items():
            for t in tick_list or []:
                try:
                    ts = float(t["time"])
                except (KeyError, TypeError, ValueError):
                    continue
                if ts > 1e11:  # milliseconds — normalize to seconds
                    ts /= 1000.0
                if asset not in last_tick or ts > last_tick[asset]:
                    last_tick[asset] = ts
        try:
            tick_inserted, tick_assets, newest_ts = _store_tick_candles(
                db, ticks, period, tick_buckets, built_until, now_s)
        except Exception as exc:
            logger.warning("live stream: tick build failed: %s", exc)
            tick_inserted, tick_assets, newest_ts = 0, 0, None
        if newest_ts is not None:
            tick_age = max(0, now_s - int(newest_ts))
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
        # Per-asset silence (the owner, 2026-11-27): EURUSD-OTC and
        # USDJPY-OTC stopped at 10:44 and BTCUSD-OTC at 12:13 while
        # USDPKR-OTC kept ticking — the GLOBAL check above never
        # fired, because last_any resets on ANY asset's candle, so
        # the loop sat "connected" for hours with most assets dead.
        # An asset that ticked during THIS run and then goes quiet
        # for the whole stale window while others still tick has a
        # broken subscription — reconnect (the tail-fill on
        # reconnect closes its gap). Assets that never ticked in
        # this run (closed market) never trigger this.
        fresh = [a for a, ts in last_tick.items()
                 if now_s - ts <= stale_after]
        silent = [a for a, ts in last_tick.items()
                  if now_s - ts > stale_after]
        if silent and fresh:
            shown = ", ".join(sorted(silent)[:10])
            extra = (f" +{len(silent) - 10} more"
                     if len(silent) > 10 else "")
            logger.warning(
                "live stream: %d asset(s) silent for %d minutes while "
                "others still tick (%s%s) — reconnecting",
                len(silent), config.LIVE_STREAM_STALE_MINUTES, shown, extra)
            return
        # Never ticked at all in this run (the owner, 2026-11-27):
        # 20 major forex OTC pairs sat at 10:44 while exotics kept
        # ticking — they never ticked in the new run, so neither the
        # global check nor the per-asset silence check above could
        # fire for them. A subscribed asset that was trading
        # recently but stays completely silent for the whole stale
        # window — the run must be older than the window first, so
        # subscriptions have time to deliver first ticks — while
        # others tick has a broken subscription. Reconnect so the
        # tail-fill can heal it. A per-asset cooldown (2h) bounds
        # reconnect flapping when the subscription stays broken.
        if expected and fresh and time.monotonic() - loop_started > stale_after:
            now_mono = time.monotonic()
            never = [a for a in expected
                     if a not in last_tick
                     and now_mono - never_cool.get(a, 0)
                     > _NEVER_TICKED_COOLDOWN_SECONDS]
            if never:
                for a in never:
                    never_cool[a] = now_mono
                shown = ", ".join(sorted(never)[:10])
                extra = (f" +{len(never) - 10} more"
                         if len(never) > 10 else "")
                logger.warning(
                    "live stream: %d subscribed asset(s) never ticked "
                    "in this run while others tick (%s%s) — reconnecting",
                    len(never), shown, extra)
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
    # Per-asset cooldown for the never-ticked reconnect trigger —
    # shared across reconnects so one broken subscription cannot
    # flap the stream every few minutes (the stale window is 3 min
    # since 2026-11-27).
    never_cool = {}
    logger.info("live stream: starting (all open assets, %d-second candles)",
                config.LIVE_STREAM_PERIOD)
    while True:
        session = None
        tail_task = None
        try:
            session = session_factory()
            await session.connect()
            assets = await _stream_assets(db, session)
            if not assets:
                raise RuntimeError("no assets to stream")
            assets = _pick_stream_assets(assets, config.LIVE_STREAM_TEST_ASSET)
            logger.info("live stream: subscribed to %d assets", len(assets))
            await session.start_asset_streams(assets, config.LIVE_STREAM_PERIOD)
            # PERMANENT delay fix (the owner, 2026-11-27): the tail-fill
            # used to run BEFORE the poll loop, so a slow tail-fill
            # (timed-out history fetches) froze the stream for ~10 min
            # — no live tick was stored and the staleness trigger could
            # not fire either, which is exactly the 20:41 freeze on
            # 2026-11-27. Now the poll loop starts at once and the
            # tail-fill runs in the background: live ticks flow from
            # the first second, the gaps fill behind them. The
            # tail-fill shares this session's history-fetch channel
            # while the poll loop only reads local tick buffers, so
            # they do not interfere; and the tail-fill never raises.
            tail_task = asyncio.create_task(_tail_fill(db, session, assets))
            _safe_set_state(db, "live_stream_status", "connected")
            run_started = time.monotonic()
            await _poll_loop(db, session, sleep, assets, never_cool)
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
            # Stop the background tail-fill before closing the session
            # it fetches through — otherwise it would keep fetching
            # against a closed session and log failures.
            if tail_task is not None and not tail_task.done():
                tail_task.cancel()
                try:
                    await asyncio.wait_for(tail_task, timeout=5)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                except Exception:
                    pass
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    pass
        await sleep(backoff)
        backoff = min(backoff * 2, 300)
