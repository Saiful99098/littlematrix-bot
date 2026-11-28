"""Live Auto Signal loop (the owner, 2026-11-28).

The user picks one or more markets and turns it ON; the bot watches
those markets live and sends a signal only when the analysis is
strong (multi-day analysis + win-% threshold — the same four checks
as AI Signal: EMA trend, RSI zone, last 3 candles, 5-day same-time
history), NOT a signal every minute. Each signal carries a chart
screenshot; the result follows within 5 seconds of the deciding
candle's close (the loop wakes 5s past each minute), with a tick on
the winning candle (profit) or a cross (loss). In MTG1 mode a
direct M1 win is sent at once; only a lost M1 waits for the next
minute, and a MTG1 win carries the dot (check + dot), exactly like
the CHECKER. Every 5 completed signals the bot sends the user a
5-signal summary. When the user presses Back from the dashboard to
the menu, pending results are cancelled (they are no longer sent).
Turning Auto Live OFF cancels pending results too. The owner can
also post his own signals to his channel from the dashboard.

Pending signals (sent, result not yet delivered) live in the candle
DB state, so a restart neither loses them nor double-sends them.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

from . import candles_db, config
from .chart import draw_candles_png
from .collector import _safe_set_state, dhaka_now
from .premium_emoji import pe
from .signal_engine import (
    ai_next_signal,
    candle_direction,
    display_asset,
    normalize_candle,
)

logger = logging.getLogger("littlematrix.auto_live")

_AUTO_PENDING_KEY = "auto_live_pending"
_AUTO_HISTORY_KEY = "auto_live_history"
_AUTO_CHANNEL_KEY = "auto_live_channel"
_CHART_CANDLES = 30
_RESULT_WAIT_MINUTES = 3  # non-MTG: drop a pending signal this old
_RESULT_WAIT_MTG1_MINUTES = 4  # MTG1 decides a minute later
_SUMMARY_SIGNALS = 5  # send a summary after this many completed signals


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


def cancel_pending_for_user(db, uid):
    """Drop a user's pending Auto Live signals (the owner,
    2026-11-28): when he presses Back from the dashboard to the
    menu, the bot stops sending those signals' results. Returns the
    number dropped."""
    pending = _load_pending(db)
    kept = [s for s in pending if s.get("uid") != uid]
    dropped = len(pending) - len(kept)
    if dropped:
        _save_pending(db, kept)
        logger.info("auto live: cancelled %d pending signal(s) for %s",
                    dropped, uid)
    return dropped


def _load_history(db):
    """Completed-signal history as {str(uid): [entries]}; each entry
    is {asset, hhmm, direction, outcome}."""
    try:
        raw = db.get_state(_AUTO_HISTORY_KEY)
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_history(db, history):
    try:
        _safe_set_state(db, _AUTO_HISTORY_KEY, json.dumps(history))
    except Exception as exc:
        logger.warning("auto live: could not save signal history: %s", exc)


def history_count(db, uid):
    """Completed signals a user has banked toward their next
    5-signal summary (0-4)."""
    entries = _load_history(db).get(str(uid)) or []
    return len(entries) if isinstance(entries, list) else 0


def channel_send_on(db):
    """True when the owner turned ON posting his Auto Live signals
    to his channel (the owner, 2026-11-28)."""
    try:
        return db.get_state(_AUTO_CHANNEL_KEY) == "1"
    except Exception:
        return False


def set_channel_send(db, on):
    try:
        _safe_set_state(db, _AUTO_CHANNEL_KEY, "1" if on else "0")
    except Exception as exc:
        logger.warning("auto live: could not save channel setting: %s", exc)


def _tick_delay(now):
    """Seconds until 5s past the next minute boundary — the
    just-closed candle is stored by the stream by then."""
    nxt = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
    return max(1.0, (nxt - now).total_seconds() + 5)


def _signal_result(db, sig, now):
    """(outcome, candle) for a pending signal, or None when the
    deciding candle has not closed yet / is not stored. outcome is
    'WIN', 'WIN_MTG1' (MTG1 win — carries the dot), 'LOSS' or
    'DOJI'. Non-MTG decides on the signal minute itself. MTG1: a
    direct M1 win is returned at once, 5s after the signal candle
    closes (the owner, 2026-11-28: waiting for the MTG1 candle
    made every direct win 1 minute late); only when the signal
    minute lost does the next minute (MTG1) decide. The result is
    ready 5s after the deciding candle closes — the loop wakes 5s
    past each minute."""
    from datetime import datetime as _dt
    try:
        minute_dt = _dt.strptime(sig["minute"], "%Y-%m-%d %H:%M")
    except (KeyError, ValueError):
        return ("LOSS", None)  # a broken record — drop it, do not retry
    mtg1 = bool(sig.get("mtg1"))
    # The signal-minute candle closes one minute after its minute
    # starts; the MTG1 candle closes a minute after that.
    m1_close = minute_dt + timedelta(minutes=1)
    if now.replace(tzinfo=None) < m1_close:
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
    if direction == sig.get("direction"):
        # M1 win — return at once, even in MTG1 mode. Do NOT wait
        # for the MTG1 candle (Fix 4, 2026-11-28).
        return ("WIN", candle)
    if not mtg1:
        return ("LOSS", candle)
    # MTG1: the signal minute lost — the next minute decides, so
    # wait for its candle to close.
    mtg1_dt = minute_dt + timedelta(minutes=1)
    if now.replace(tzinfo=None) < mtg1_dt + timedelta(minutes=1):
        return None
    mtg1_date, mtg1_hhmm = (mtg1_dt.strftime("%Y-%m-%d"),
                            mtg1_dt.strftime("%H:%M"))
    try:
        mtg1_candle = db.get_candle(sig["asset"], mtg1_date, mtg1_hhmm)
    except Exception:
        mtg1_candle = None
    if mtg1_candle is None:
        return None
    mtg1_dir = candle_direction(mtg1_candle)
    if mtg1_dir == "DOJI":
        return ("DOJI", mtg1_candle)
    if mtg1_dir == sig.get("direction"):
        return ("WIN_MTG1", mtg1_candle)
    return ("LOSS", mtg1_candle)


def _recent_chart_candles(db, asset, now, n=_CHART_CANDLES):
    """Last `n` normalized candles of today for one asset."""
    try:
        rows = db.get_candles(asset, now.strftime("%Y-%m-%d"))
    except Exception:
        return []
    return [normalize_candle(c) for c in rows[-n:]]


async def _send_photo_or_text(bot, chat_id, png, caption, what):
    """Send a chart photo with a premium caption to one chat; fall
    back to the caption as a plain message when the photo fails.
    Returns True when something was delivered."""
    if png is not None:
        try:
            await bot.send_photo(chat_id=chat_id, photo=png,
                                 caption=caption, parse_mode="HTML")
            return True
        except Exception as exc:
            logger.warning("auto live: %s photo to %s failed: %s",
                           what, chat_id, exc)
    try:
        await bot.send_message(chat_id=chat_id, text=caption,
                               parse_mode="HTML")
        return True
    except Exception as exc:
        logger.warning("auto live: %s to %s failed: %s", what, chat_id, exc)
        return False


async def _send_signal(bot, db, uid, asset, minute_key, direction, now,
                       mtg1=False, extra_chats=()):
    candles = _recent_chart_candles(db, asset, now)
    hhmm = minute_key[11:16]
    mode = " (MTG1)" if mtg1 else ""
    caption = (f"{pe('bell')} {display_asset(asset)} — {hhmm} M1 {direction}\n"
               f"{pe('diamond')} Auto Live Signal{mode}")
    try:
        png = draw_candles_png(
            candles, title=f"{display_asset(asset)} {hhmm} M1 {direction}")
    except Exception:
        png = None
    await _send_photo_or_text(bot, uid, png, caption, "signal")
    for chat_id in extra_chats:
        await _send_photo_or_text(bot, chat_id, png, caption, "signal")
    logger.info("auto live: signal %s %s %s to %s",
                asset, minute_key, direction, uid)


async def _send_result(bot, db, sig, outcome, candle, now, extra_chats=()):
    uid, asset = sig["uid"], sig["asset"]
    hhmm = sig["minute"][11:16]
    candles = _recent_chart_candles(db, asset, now)
    # Highlight the deciding candle (the MTG1 candle in MTG1 mode).
    decide_hhmm = hhmm
    if candle is not None:
        try:
            decide_hhmm = normalize_candle(candle).get("hhmm") or hhmm
        except Exception:
            pass
    idx = None
    for i, c in enumerate(candles):
        if c.get("hhmm") == decide_hhmm:
            idx = i
            break
    # The result caption carries NO "WIN"/"LOSS" text — only the
    # premium sticker (the owner, 2026-11-28): a win shows the
    # check sticker, a loss the cross, a DOJI the minus; a MTG1
    # win carries the dot, exactly like the CHECKER. The sticker
    # sits BESIDE the direction (after CALL/PUT), NOT before the
    # asset — the owner, 2026-11-28 16:10.
    if outcome == "WIN":
        mark, sticker = "win", pe("check")
    elif outcome == "WIN_MTG1":
        mark, sticker = "win", pe("check") + "."
    elif outcome == "LOSS":
        mark, sticker = "loss", pe("cross")
    else:
        mark, sticker = None, pe("minus")
    caption = (f"{display_asset(asset)} — {hhmm} M1 "
               f"{sig.get('direction')} {sticker}")
    try:
        png = draw_candles_png(
            candles, highlight_idx=idx, mark=mark,
            title=f"{display_asset(asset)} {hhmm} M1 {sig.get('direction')}")
    except Exception:
        png = None
    await _send_photo_or_text(bot, uid, png, caption, "result")
    for chat_id in extra_chats:
        await _send_photo_or_text(bot, chat_id, png, caption, "result")
    logger.info("auto live: result %s %s %s to %s",
                asset, sig["minute"], outcome, uid)


def _summary_text(entries):
    """The 5-signal summary in the CHECKER format (the owner,
    2026-11-28): the signal date at the top, a gap below the
    header, the signals packed in monospace with the stickers in
    one straight column (padded like the checker), a gap above the
    Accuracy line. Accuracy excludes DOJI, like the CHECKER."""
    date_str = ((entries[0].get("date") if entries else None)
                or dhaka_now().strftime("%Y-%m-%d"))
    y, m, d = date_str.split("-")
    sigs = [f"{i}. {display_asset(e['asset'])}, {e['hhmm']} M1 "
            f"{e['direction']}"
            for i, e in enumerate(entries, 1)]
    width = max((len(s) for s in sigs), default=0)
    lines = []
    wins = losses = 0
    for i, e in enumerate(entries, 1):
        out = e.get("outcome")
        if out in ("WIN", "WIN_MTG1"):
            wins += 1
            mark = pe("check") + ("." if out == "WIN_MTG1" else "")
        elif out == "LOSS":
            losses += 1
            mark = pe("cross")
        else:
            mark = pe("minus")
        lines.append(f"<code>{sigs[i - 1].ljust(width)}</code> {mark}")
    total = wins + losses
    acc = f"{wins}/{total} = {wins * 100 // total}%" if total else "—"
    return (
        f"DATE: {pe('sparkle')}{d}.{m}.{y}{pe('star')}\n"
        f"\n"
        f"{pe('bell')} Auto Live Signal {pe('diamond')}\n"
        f"\n"
        + "\n".join(lines)
        + f"\n\n{pe('crown')} Accuracy: {acc}")


async def _send_summary(bot, uid, entries):
    try:
        await bot.send_message(chat_id=uid, text=_summary_text(entries),
                               parse_mode="HTML")
    except Exception as exc:
        logger.warning("auto live: summary to %s failed: %s", uid, exc)
    logger.info("auto live: %d-signal summary to %s", len(entries), uid)


async def _auto_live_tick(bot, db, now_fn=None):
    """One minute of work: results first, then new signals."""
    now = now_fn() if now_fn else dhaka_now()
    pending = _load_pending(db)
    still_pending = []
    history = _load_history(db)
    try:
        from . import database as users_db
    except Exception:
        users_db = None
    for sig in pending:
        got = _signal_result(db, sig, now)
        if got is None:
            # Give up on a signal whose candle never arrives. MTG1
            # decides a minute later, so it gets a minute more.
            from datetime import datetime as _dt
            try:
                age = (now.replace(tzinfo=None)
                       - _dt.strptime(sig["minute"], "%Y-%m-%d %H:%M"))
            except (KeyError, ValueError):
                age = timedelta(minutes=99)
            limit = timedelta(
                minutes=_RESULT_WAIT_MTG1_MINUTES if sig.get("mtg1")
                else _RESULT_WAIT_MINUTES)
            if age > limit:
                logger.info("auto live: dropping stale pending signal %s %s",
                            sig["asset"], sig["minute"])
                continue
            still_pending.append(sig)
            continue
        outcome, candle = got
        # OFF means no more results: if the user turned Auto Live
        # OFF after the signal was sent, drop the result instead of
        # sending it (Fix 2, 2026-11-28). The toggle already cancels
        # pending signals; this covers the race where OFF lands
        # mid-tick.
        try:
            subscribed = (users_db.is_auto_subscriber(sig["uid"])
                          if users_db else True)
        except Exception:
            subscribed = True
        if not subscribed:
            logger.info("auto live: dropping result for %s — user turned OFF",
                        sig["uid"])
            continue
        extra = _extra_chats(db, sig["uid"])
        try:
            await _send_result(bot, db, sig, outcome, candle, now,
                               extra_chats=extra)
        except Exception as exc:
            logger.warning("auto live: result to %s failed: %s",
                           sig["uid"], exc)
            still_pending.append(sig)
            continue
        # Bank the completed signal; every 5th one sends a summary.
        key = str(sig["uid"])
        entries = history.get(key) or []
        if not isinstance(entries, list):
            entries = []
        entries.append({"asset": sig["asset"],
                        "hhmm": sig["minute"][11:16],
                        "date": sig["minute"][:10],
                        "direction": sig.get("direction"),
                        "outcome": outcome})
        if len(entries) >= _SUMMARY_SIGNALS:
            await _send_summary(bot, sig["uid"], entries)
            history[key] = []
        else:
            history[key] = entries
    _save_pending(db, still_pending)
    _save_history(db, history)
    try:
        subs = users_db.get_auto_subscribers() if users_db else []
    except Exception:
        subs = []
    # Free Auto Live is 30 minutes a day (the owner, 2026-11-28):
    # one tick = one minute of being ON. A free user who hits the
    # limit is turned OFF (pending results cancelled) and told to
    # get Premium. Premium users are unlimited. A DB error never
    # turns anyone off — on error the user is treated as premium.
    try:
        if users_db is not None:
            today_str = now.strftime("%Y-%m-%d")
            for _uid in sorted({uid for uid, _a in (subs or [])}):
                try:
                    _is_prem = users_db.is_premium(_uid)
                except Exception:
                    _is_prem = True
                if _is_prem:
                    continue
                try:
                    _count = users_db.increment_usage(
                        _uid, "auto_live", today_str)
                except Exception:
                    continue
                if _count >= config.AUTO_LIVE_FREE_MINUTES:
                    try:
                        users_db.set_auto_signals(_uid, False)
                    except Exception:
                        pass
                    cancel_pending_for_user(db, _uid)
                    try:
                        from .handlers import _limit_text as _handlers_limit_text
                        _limit_msg = _handlers_limit_text()
                    except Exception:
                        _limit_msg = ("Your limit is reached for today. "
                                      "Get Premium for unlimited use.")
                    try:
                        await bot.send_message(chat_id=_uid, text=_limit_msg,
                                               parse_mode="HTML")
                    except Exception:
                        pass
                    logger.info("auto live: free user %s hit the %d-minute "
                                "daily limit", _uid,
                                config.AUTO_LIVE_FREE_MINUTES)
    except Exception as exc:
        logger.warning("auto live: daily-minutes check failed: %s", exc)
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
            mtg1 = users_db.get_auto_mtg1(uid)
        except Exception:
            mtg1 = False
        try:
            await _send_signal(bot, db, uid, asset, minute_key, direction,
                               now, mtg1=mtg1,
                               extra_chats=_extra_chats(db, uid))
        except Exception as exc:
            logger.warning("auto live: signal to %s failed: %s", uid, exc)
            continue
        still_pending.append({"uid": uid, "asset": asset,
                              "minute": minute_key, "direction": direction,
                              "mtg1": bool(mtg1)})
    _save_pending(db, still_pending)


def _extra_chats(db, uid):
    """Extra chats for one subscriber's signals: the owner's channel,
    but only for the owner's own signals and only when he turned the
    channel send ON in the dashboard (the owner, 2026-11-28)."""
    try:
        if uid not in config.ADMIN_USER_IDS:
            return []
        if not channel_send_on(db):
            return []
        ch = (config.REQUIRED_CHANNEL or "").strip()
        return [ch] if ch else []
    except Exception:
        return []


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
