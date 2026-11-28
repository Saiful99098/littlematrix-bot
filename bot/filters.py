"""Signal FILTER — rebuilt (the owner, 2026-11-28).

The old Aston/Newexe filter is dropped from the bot. The new filter
works on ANY signal list the user pastes: each line's asset is
analysed from the candle database and the line is kept only when
all three agree:

1) WIN % — over the past N days, how often did the candle at that
   HH:MM go in the line's direction (CALL = green, PUT = red)?
   DOJI days are excluded. Keep when win % >= the typed threshold.
2) TREND — the asset's EMA9/21 trend (last 60 closes today) must
   agree with the line's direction. When there are not enough
   candles to compute a trend, this check is skipped, not failed.
3) BLACKOUT / WHITEOUT — the T-1 candle theory: over the past N
   days, was the T candle mostly OPPOSITE to the T-1 candle
   (Blackout) or the SAME as the T-1 candle (Whiteout)? The line is
   kept only when its direction matches the direction the dominant
   pattern predicts for T. A tie (no dominant pattern) is neutral
   and passes.

The output keeps the input's lines exactly as pasted (same format),
only the kept ones, with a summary of how many were kept.
"""
from __future__ import annotations

import re

from .signal_engine import (
    candle_direction,
    normalize_candle,
    trend_direction,
)

LINE_RE = re.compile(
    r"^\s*([A-Za-z0-9]+(?:[-_][A-Za-z]+)?)\s*,\s*(\d{1,2}:\d{2})\s+M1\s+(CALL|PUT)\s*$",
    re.IGNORECASE,
)


def parse_line(line):
    m = LINE_RE.match(line.strip())
    if not m:
        return None
    asset = m.group(1).upper()
    h, mi = m.group(2).split(":")
    hhmm = f"{int(h):02d}:{int(mi):02d}"
    return {"asset": asset, "hhmm": hhmm, "direction": m.group(3).upper()}


def _prev_hhmm(hhmm):
    """The minute before HH:MM (T-1), or None for 00:00."""
    h, m = hhmm.split(":")
    total = int(h) * 60 + int(m) - 1
    if total < 0:
        return None
    return f"{total // 60:02d}:{total % 60:02d}"


def _today_str():
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(hours=6)).strftime("%Y-%m-%d")


def _win_rate_and_pattern(db, asset, hhmm, direction, days, today):
    """(win_rate, votes, pattern_info) for one signal line over
    `days` completed days before `today`.

    win_rate is the fraction of days (with a non-DOJI T candle)
    whose T candle matched `direction`. votes is how many days
    voted. pattern_info is (pattern, t1_calls, t1_puts) where
    pattern is "BLACKOUT" / "WHITEOUT" / None.
    """
    from .candles_db import normalize_asset_name

    asset = normalize_asset_name(asset)
    dates = db.get_dates(asset) if hasattr(db, "get_dates") else []
    dates = [d for d in dates if d < today][-days:]
    if not dates:
        return None, 0, (None, 0, 0)
    t1 = _prev_hhmm(hhmm)
    wins = 0
    votes = 0
    blackout = 0
    whiteout = 0
    t1_calls = 0
    t1_puts = 0
    for date in dates:
        c = db.get_candle(asset, date, hhmm)
        if not c:
            continue
        t_dir = candle_direction(normalize_candle(c))
        if t_dir is None:
            continue
        votes += 1
        if t_dir == direction:
            wins += 1
        if t1:
            c1 = db.get_candle(asset, date, t1)
            if c1:
                t1_dir = candle_direction(normalize_candle(c1))
                if t1_dir == "CALL":
                    t1_calls += 1
                elif t1_dir == "PUT":
                    t1_puts += 1
                if t1_dir is not None:
                    if t_dir != t1_dir:
                        blackout += 1
                    else:
                        whiteout += 1
    if votes == 0:
        return None, 0, (None, 0, 0)
    pattern = None
    if blackout > whiteout:
        pattern = "BLACKOUT"
    elif whiteout > blackout:
        pattern = "WHITEOUT"
    return wins / votes, votes, (pattern, t1_calls, t1_puts)


def _trend_agrees(db, asset, direction, today):
    """True when the EMA9/21 trend agrees with `direction`, False
    when it disagrees, None when there is not enough data."""
    try:
        candles = db.get_candles(asset, today)
    except Exception:
        return None
    closes = [normalize_candle(c)["close"] for c in candles]
    if len(closes) < 21:
        return None
    trend = trend_direction(closes)
    if direction == "CALL":
        return trend == "UP"
    return trend == "DOWN"


def filter_one_line(db, line, days, pct, today):
    """Check ONE parsed signal line against the DB. Returns
    (kept, detail) where detail is (line, win_rate, pattern, kept,
    reason)."""
    win_rate, votes, pat = _win_rate_and_pattern(
        db, line["asset"], line["hhmm"], line["direction"], days, today)
    if win_rate is None:
        return False, (line, None, None, False, "no data")
    pattern, t1_calls, t1_puts = pat
    # Pattern check: the line's direction must match the
    # direction the dominant Blackout/Whiteout pattern predicts.
    pattern_ok = True
    if pattern == "BLACKOUT":
        t1_majority = "CALL" if t1_calls >= t1_puts else "PUT"
        expected = "PUT" if t1_majority == "CALL" else "CALL"
        pattern_ok = line["direction"] == expected
    elif pattern == "WHITEOUT":
        t1_majority = "CALL" if t1_calls >= t1_puts else "PUT"
        pattern_ok = line["direction"] == t1_majority
    trend = _trend_agrees(db, line["asset"], line["direction"], today)
    if win_rate * 100 < pct:
        return False, (line, win_rate, pattern, False,
                       f"win {win_rate * 100:.0f}% < {pct}%")
    if not pattern_ok:
        return False, (line, win_rate, pattern, False,
                       f"{pattern} says the other way")
    if trend is False:
        return False, (line, win_rate, pattern, False, "trend disagrees")
    return True, (line, win_rate, pattern, True, "")


def filter_signals_by_db(db, lines, days, pct, today=None):
    """Filter parsed signal lines against the DB. Returns
    (kept, stats) where kept is the list of kept line dicts (same
    order as the input) and stats is {"total", "kept", "dropped",
    "no_data", "details": [(line, win_rate, pattern, kept, reason)]}.
    """
    today = today or _today_str()
    kept = []
    details = []
    no_data = 0
    for line in lines:
        ok, detail = filter_one_line(db, line, days, pct, today)
        if ok:
            kept.append(line)
        elif detail[4] == "no data":
            no_data += 1
        details.append(detail)
    stats = {
        "total": len(lines),
        "kept": len(kept),
        "dropped": len(lines) - len(kept),
        "no_data": no_data,
        "details": details,
    }
    return kept, stats


def progress_bar(pct):
    """A download-style progress bar: 10 blocks + the %."""
    filled = max(0, min(10, int(pct / 10)))
    return "█" * filled + "░" * (10 - filled) + f" {pct:.0f}%"


def format_filter_result(kept, stats, days, pct):
    """The kept lines in exactly the input's format, plus a summary.
    The kept lines are returned as plain text (the caller wraps them
    in a monospace code block, as the input list is shown)."""
    if kept:
        lines = [f"{s['asset']},{s['hhmm']} M1 {s['direction']}" for s in kept]
        body = "\n".join(lines)
    else:
        body = "No signals passed the filter."
    summary = (f"\n\nFiltered: {stats['kept']}/{stats['total']} kept "
               f"({days} days, win >= {pct}%)")
    return body + summary
