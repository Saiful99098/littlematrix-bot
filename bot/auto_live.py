"""Live Auto Signal loop (the owner, 2026-11-28).

The user picks ONE market and turns it ON; the bot watches that
market live and sends a signal only when the analysis is strong
(multi-day analysis + win-% threshold — the same four checks as AI
Signal: EMA trend, RSI zone, last 3 candles, 5-day same-time
history), NOT a signal every minute. Each signal carries a chart
screenshot; after the minute closes, a result screenshot follows
with a tick on the winning candle (profit) or a cross (loss).

The loop wakes just after each minute turns, so the just-closed
candle is already stored by the stream. Pending signals (sent, result
not yet delivered) live in the candle DB state, so a restart neither
loses them nor double-sends them.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

from . import candles_db
from .chart import draw_candles_png
from .collector import _safe_set_state, dhaka_now
from .signal_engine import (
    ai_next_signal,
    candle_direction,
    display_asset,
    normalize_candle,
)

logger = logging.getLogger("littlematrix.auto_live")

_AUTO_PENDING_KEY = "auto_live_pending"
_CHART_CANDLES = 30
_RESULT_WAIT_MINUTES = 3  # drop a pending signal this old (candle never came)


def _load_pending(db):
    try:
        raw = db.get_state(_AUTO_PENDING_KEY)
    except Exception:
        return []
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except Exception:
        return []
    return [s for s in items if isinstance(s, dict)
            and s.get("uid") and s.get("asset") and s.get("minute")]


def _save_pending(db, pending):
    try:
        _safe_set_state(db, _AUTO_PENDING_KEY, json.dumps(pending))
    except Exception as exc:
        logger.warning("auto live: could not save pending signals: %s", exc)


def _tick_delay(now):
    """Seconds until 5s past the next minute boundary — the
    just-closed candle is stored by the stream by then."""
    nxt = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
    return max(1.0, (nxt - now).total_seconds() + 5)


def _signal_result(db, sig, now):
    """(outcome, candle) for a pending signal, or None when the
    minute has not closed yet / its candle is not stored. outcome is
    'WIN', 'LOSS' or 'DOJI'."""
    from datetime import datetime as _dt
    try:
        minute_dt = _dt.strptime(sig["minute"], "%Y-%m-%d %H:%M")
    except (KeyError, ValueError):
        return ("LOSS", None)  # a broken record — drop it, do not retry
    # The minute closes one minute after it starts; give the stream
    # a minute of grace before calling it missing.
    if now.replace(tzinfo=None) < minute_dt + timedelta(minutes=2):
        return None
    date_str, hhmm = sig["minute"][:10], sig["minute"][11:16]
    try:
        candle = db.get_candle(sig["asset"], date_str, hhmm)
    except Exception:
        candle = None
    if candle is None:
        return None
    direction = candle_direction(candle)
    if direction == "DOJI":
        return ("DOJI", candle)
    return ("WIN" if direction == sig.get("direction") else "LOSS", candle)


def _recent_chart_candles(db, asset, now, n=_CHART_CANDLES):
    """Last `n` normalized candles of today for one asset."""
    try:
        rows = db.get_candles(asset, now.strftime("%Y-%m-%d"))
    except Exception:
        return []
    return [normalize_candle(c) for c in rows[-n:]]


async def _send_signal(bot, db, uid, asset, minute_key, direction, now):
    candles = _recent_chart_candles(db, asset, now)
    hhmm = minute_key[11:16]
    caption = (f"{display_asset(asset)} — {hhmm} M1 {direction}\n"
               "Auto Live Signal — all four checks agree.")
    try:
        png = draw_candles_png(
            candles, title=f"{display_asset(asset)} {hhmm} M1 {direction}")
        await bot.send_photo(chat_id=uid, photo=png, caption=caption)
    except Exception:
        # A chart must never block the signal itself.
        await bot.send_message(chat_id=uid, text=caption)
    logger.info("auto live: signal %s %s %s to %s",
                asset, minute_key, direction, uid)


async def _send_result(bot, db, sig, outcome, candle, now):
    uid, asset = sig["uid"], sig["asset"]
    hhmm = sig["minute"][11:16]
    candles = _recent_chart_candles(db, asset, now)
    idx = None
    for i, c in enumerate(candles):
        if c.get("hhmm") == hhmm:
            idx = i
            break
    mark = "win" if outcome == "WIN" else ("loss" if outcome == "LOSS" else None)
    caption = (f"{display_asset(asset)} — {hhmm} M1 {sig.get('direction')} — "
               f"{outcome}")
    try:
        png = draw_candles_png(
            candles, highlight_idx=idx, mark=mark,
            title=f"{display_asset(asset)} {hhmm} M1 {sig.get('direction')} — {outcome}")
        await bot.send_photo(chat_id=uid, photo=png, caption=caption)
    except Exception:
        await bot.send_message(chat_id=uid, text=caption)
    logger.info("auto live: result %s %s %s to %s",
                asset, sig["minute"], outcome, uid)


async def _auto_live_tick(bot, db, now_fn=None):
    """One minute of work: results first, then new signals."""
    now = now_fn() if now_fn else dhaka_now()
    pending = _load_pending(db)
    still_pending = []
    for sig in pending:
        got = _signal_result(db, sig, now)
        if got is None:
            # Give up on a signal whose candle never arrives.
            from datetime import datetime as _dt
            try:
                age = (now.replace(tzinfo=None)
                       - _dt.strptime(sig["minute"], "%Y-%m-%d %H:%M"))
            except (KeyError, ValueError):
                age = timedelta(minutes=99)
            if age > timedelta(minutes=_RESULT_WAIT_MINUTES):
                logger.info("auto live: dropping stale pending signal %s %s",
                            sig["asset"], sig["minute"])
                continue
            still_pending.append(sig)
            continue
        outcome, candle = got
        try:
            await _send_result(bot, db, sig, outcome, candle, now)
        except Exception as exc:
            logger.warning("auto live: result to %s failed: %s",
                           sig["uid"], exc)
            still_pending.append(sig)
    _save_pending(db, still_pending)
    try:
        from . import database as users_db
        subs = users_db.get_auto_subscribers()
    except Exception:
        subs = []
    for uid, asset in subs or []:
        if not asset:
            continue
        next_min = (now.replace(second=0, microsecond=0)
                    + timedelta(minutes=1))
        minute_key = next_min.strftime("%Y-%m-%d %H:%M")
        if any(s["uid"] == uid and s["asset"] == asset
               for s in still_pending):
            # One live signal at a time per market: the previous one
            # is still awaiting its result — do not stack signals.
            # (This also dedupes the same minute after a restart.)
            continue
        try:
            direction, checks, n = ai_next_signal(db, asset, now=now)
        except Exception as exc:
            logger.warning("auto live: analysis for %s failed: %s", asset, exc)
            continue
        if not direction:
            continue  # no signal this minute — the point of the filter
        try:
            await _send_signal(bot, db, uid, asset, minute_key, direction, now)
        except Exception as exc:
            logger.warning("auto live: signal to %s failed: %s", uid, exc)
            continue
        still_pending.append({"uid": uid, "asset": asset,
                              "minute": minute_key, "direction": direction})
    _save_pending(db, still_pending)


async def auto_live_loop(bot, db, sleep=asyncio.sleep, now_fn=None,
                         max_ticks=None):
    """Background task, started from main.py post_init. Wakes just
    after each minute turns. `max_ticks` bounds the loop for tests
    (None = run forever)."""
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        ticks += 1
        try:
            await _auto_live_tick(bot, db, now_fn=now_fn)
        except Exception as exc:
            logger.warning("auto live: tick failed: %s", exc)
        now = now_fn() if now_fn else dhaka_now()
        await sleep(_tick_delay(now))
