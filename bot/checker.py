"""CHECKER — check a pasted signal list against candle data.

Rules agreed with the owner:
- Input line: ASSET-OTC,HH:MM M1 CALL/PUT
  (asset may be given with or without -OTC; case-insensitive).
- Lookup the candle for that asset + date + HH:MM in the DB/sample data.
- C > O = CALL, C < O = PUT, C == O = DOJI.
- Signal direction matches actual -> WIN.
- Signal direction opposite actual -> LOSS.
- C == O -> write DOJI. Do NOT change it to DRAW, do NOT count it
  as WIN or LOSS.
- No candle for that time -> NO DATA (not counted as WIN/LOSS).
- Summary: Total / WIN / LOSS / DOJI / NO DATA / WIN%.
  WIN% = WIN / (WIN + LOSS) — DOJI and NO DATA excluded.
- Optional MTG1: if LOSS, check the next minute's candle with the
  same direction. An MTG1 WIN counts as a WIN in the summary (the
  owner, 2026-11-26): in MTG mode the MTG1 result IS the result, so
  there is one Accuracy line, never a separate "MTG1 Accuracy".
  An MTG1 LOSS / DOJI / NO DATA leaves the M1 loss standing.
"""
from __future__ import annotations

import re

from .premium_emoji import pe
from .signal_engine import candle_direction, normalize_candle

LINE_RE = re.compile(
    r"^\s*([A-Za-z0-9]+(?:[-_][A-Za-z]+)?)\s*,\s*(\d{1,2}:\d{2})\s+M1\s+(CALL|PUT)\s*$",
    re.IGNORECASE,
)
# Blackout/whiteout lines carry NO direction (the owner, 2026-11-27):
# "USDARS-OTC,09:07 M1". The direction group is optional here; the
# space between the time and M1 is still required ("01:33M1" is
# skipped, as before — the strict format stays).
LINE_RE_OPT = re.compile(
    r"^\s*([A-Za-z0-9]+(?:[-_][A-Za-z]+)?)\s*,\s*(\d{1,2}:\d{2})\s+M1(?:\s+(CALL|PUT))?\s*$",
    re.IGNORECASE,
)


def parse_checker_date(text):
    """Parse a date the user typed for the CHECKER.

    Accepts YYYY-MM-DD, YYYY/MM/DD, DD-MM-YYYY, DD/MM/YYYY
    (single digits OK, e.g. 2026-11-5 or 5-11-2026).
    Returns "YYYY-MM-DD" or None when it does not parse.
    """
    from datetime import datetime as _dt

    s = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return _dt.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_signal_line(line):
    """Parse one line. Returns (asset, hhmm, direction) or None.
    direction is None for blackout/whiteout lines without CALL/PUT
    ("USDARS-OTC,09:07 M1")."""
    m = LINE_RE_OPT.match(line.strip())
    if not m:
        return None
    asset, hhmm = m.group(1).upper(), m.group(2)
    direction = m.group(3).upper() if m.group(3) else None
    h, mi = hhmm.split(":")
    hhmm = f"{int(h):02d}:{int(mi):02d}"
    return asset, hhmm, direction


def _norm_asset(asset):
    return asset.upper().replace("_OTC", "-OTC")


def _prev_hhmm(hhmm):
    """One minute earlier, or None for 00:00."""
    h, m = map(int, hhmm.split(":"))
    if h == 0 and m == 0:
        return None
    m -= 1
    if m < 0:
        h -= 1
        m = 59
    return f"{h:02d}:{m:02d}"


def _lookup_candle(candles_by_key, asset, date_str, hhmm):
    """One candle from the lookup, with the -OTC/_otc fallback."""
    key = (_norm_asset(asset), f"{date_str} {hhmm}:00")
    candle = candles_by_key.get(key)
    if candle is None:
        for k, c in candles_by_key.items():
            if k[1] == f"{date_str} {hhmm}:00":
                candle = c
                break
    return candle


def _expected_from_prev(asset, hhmm, candles_by_key, date_str, mode):
    """Expected trade direction for a directionless blackout/whiteout
    line, derived from the T-1 candle of the CHECKED date (the owner,
    2026-11-27): BLACKOUT = opposite of T-1, WHITEOUT = same as T-1.
    Returns 'CALL' / 'PUT' / 'DOJI' (T-1 was DOJI: no trade) /
    'NO DATA' (no T-1 candle — including T=00:00, whose T-1 belongs
    to the previous day)."""
    prev = _prev_hhmm(hhmm)
    if prev is None:
        return "NO DATA"
    candle = _lookup_candle(candles_by_key, asset, date_str, prev)
    if candle is None:
        return "NO DATA"
    d = candle_direction(normalize_candle(candle))
    if d == "DOJI":
        return "DOJI"
    if mode == "BLACKOUT":
        return "PUT" if d == "CALL" else "CALL"
    return d  # WHITEOUT keeps the T-1 direction


def check_signals(lines, candles_by_key, date_str, with_mtg1=False,
                  mode="OTC"):
    """Check signal lines against candles.

    candles_by_key: dict {(asset_norm, 'YYYY-MM-DD HH:MM:00'): candle dict}
    date_str: 'YYYY-MM-DD' the user chose for the list.
    mode: 'OTC' / 'REAL' / 'BLACKOUT' / 'WHITEOUT' (the owner,
    2026-11-27). OTC/REAL check the pasted direction against the T
    candle, as before. BLACKOUT/WHITEOUT lines usually carry NO
    direction — the expected direction is derived from the T-1
    candle of the checked date (BLACKOUT opposite, WHITEOUT same)
    and the WIN/LOSS is judged by the T candle. A pasted line that
    DOES carry a direction is checked against the T candle directly,
    in every mode. A directionless line in OTC/REAL mode is skipped
    (it carries nothing to check).
    Returns (results, summary).
    results: list of {asset, hhmm, direction, result, actual}
    result in WIN / LOSS / DOJI / NO DATA. direction is None for
    directionless lines (the formatter prints them without CALL/PUT).
    summary: {total, win, loss, doji, no_data, win_pct}
    """
    results = []
    for line in lines:
        parsed = parse_signal_line(line)
        if parsed is None:
            continue
        asset, hhmm, direction = parsed
        if direction is None and mode not in ("BLACKOUT", "WHITEOUT"):
            continue  # nothing to check in OTC/REAL mode
        if direction is None:
            # Directionless blackout/whiteout line: derive the trade
            # from the T-1 candle, judge it with the T candle.
            expected = _expected_from_prev(asset, hhmm, candles_by_key,
                                           date_str, mode)
            if expected in ("NO DATA", "DOJI"):
                results.append({"asset": asset, "hhmm": hhmm,
                                "direction": None, "result": expected,
                                "actual": None})
                continue
            candle = _lookup_candle(candles_by_key, asset, date_str, hhmm)
            if candle is None:
                results.append({"asset": asset, "hhmm": hhmm,
                                "direction": None, "result": "NO DATA",
                                "actual": None})
                continue
            actual = candle_direction(normalize_candle(candle))
            if actual == "DOJI":
                result = "DOJI"
            elif actual == expected:
                result = "WIN"
            else:
                result = "LOSS"
            entry = {"asset": asset, "hhmm": hhmm, "direction": None,
                     "result": result, "actual": actual,
                     "_expected": expected}
            if with_mtg1 and result == "LOSS":
                entry["mtg1"] = _check_mtg1(asset, hhmm, expected,
                                            candles_by_key, date_str)
            results.append(entry)
            continue
        candle = _lookup_candle(candles_by_key, asset, date_str, hhmm)
        if candle is None:
            results.append({"asset": asset, "hhmm": hhmm, "direction": direction,
                            "result": "NO DATA", "actual": None})
            continue
        n = normalize_candle(candle)
        actual = candle_direction(n)
        if actual == "DOJI":
            result = "DOJI"
        elif actual == direction:
            result = "WIN"
        else:
            result = "LOSS"
        entry = {"asset": asset, "hhmm": hhmm, "direction": direction,
                 "result": result, "actual": actual}
        if with_mtg1 and result == "LOSS":
            entry["mtg1"] = _check_mtg1(asset, hhmm, direction,
                                        candles_by_key, date_str)
        results.append(entry)

    win = sum(1 for r in results if r["result"] == "WIN")
    loss = sum(1 for r in results if r["result"] == "LOSS")
    doji = sum(1 for r in results if r["result"] == "DOJI")
    no_data = sum(1 for r in results if r["result"] == "NO DATA")
    total = len(results)
    if with_mtg1:
        # MTG mode: a LOSS recovered by MTG1 counts as a WIN in the
        # summary (the owner, 2026-11-26) — the MTG1 result IS the
        # result. An MTG1 DOJI / NO DATA leaves the M1 loss standing.
        recovered = sum(1 for r in results
                        if r["result"] == "LOSS" and r.get("mtg1") == "WIN")
        win += recovered
        loss -= recovered
    decided = win + loss
    win_pct = round(win / decided * 100, 1) if decided else 0.0
    summary = {"total": total, "win": win, "loss": loss, "doji": doji,
               "no_data": no_data, "win_pct": win_pct}
    return results, summary


def _check_mtg1(asset, hhmm, direction, candles_by_key, date_str):
    """Check the next minute with the same direction. Returns WIN/LOSS/DOJI/NO DATA."""
    h, m = map(int, hhmm.split(":"))
    m += 1
    if m >= 60:
        h += 1
        m -= 60
    next_hhmm = f"{h:02d}:{m:02d}"
    candle = _lookup_candle(candles_by_key, asset, date_str, next_hhmm)
    if candle is None:
        return "NO DATA"
    actual = candle_direction(normalize_candle(candle))
    if actual == "DOJI":
        return "DOJI"
    return "WIN" if actual == direction else "LOSS"


def build_lookup(candles, asset):
    """Build {(asset_norm, datetime_str): raw candle} from a candle list."""
    out = {}
    for c in candles:
        dt = c.get("datetime", "")
        if dt:
            out[(_norm_asset(asset), dt)] = c
    return out


DIVIDER = "━━━━━━━━━━━━━━━"


def dot_date(date_str):
    """'YYYY-MM-DD' -> 'DD.MM.YYYY' (the owner's list format)."""
    if not date_str:
        return ""
    y, m, d = date_str.split("-")
    return f"{d}.{m}.{y}"


_MODE_LABEL = {"OTC": "OTC SIGNAL", "REAL": "REAL MARKET SIGNAL",
               "BLACKOUT": "BLACKOUT SIGNAL",
               "WHITEOUT": "WHITEOUT SIGNAL"}


def format_check_results(results, summary, date_str=None, header=True,
                         mode="OTC"):
    """Format checker output for Telegram — the owner's template
    (2026-11-27), premium emojis, signal text in monospace.

    The signal TEXT is wrapped in <code>...</code> and the premium
    result symbol sits OUTSIDE the code tags, so the line stays
    copy-pasteable and the symbol still renders. Blackout/whiteout
    lines carry no direction, so they print without CALL/PUT.
    NO DATA is plain "?" on purpose: the ZEROX premium ❓
    (ID 6282525354242348869) renders as a gold "WIN?" sticker,
    so users read NO DATA as WIN. Plain "?" cannot be misread.
    date_str: the checked date ('YYYY-MM-DD') for the DATE line.
    header=False (Backtest Lab): only the lines + the Total /
    Accuracy footer, no template header — the caller has its own.
    One Accuracy line only: in MTG mode the win/loss counts above
    already count MTG1 wins as wins, so there is no separate
    "MTG1 Accuracy" line (the owner, 2026-11-26).
    mode (the owner, 2026-11-27): the header names which signal was
    checked — "OTC SIGNAL Check Results", "WHITEOUT SIGNAL Check
    Results", and so on — so a pasted result is never ambiguous.
    The DATE line carries premium emojis around the date (same pair
    as the FS date line). Signal lines are separated by a blank line
    (the owner, 2026-11-27: packed lines looked cramped), and a
    long list is cut to whole lines under Telegram's 4096-char cap
    with a "… +N more lines" note — the Total / Accuracy footer
    still counts the whole list.
    """
    _sym = {"WIN": pe("check"), "LOSS": pe("cross"),
            "DOJI": pe("minus"), "NO DATA": "?"}
    head = []
    if header:
        label = _MODE_LABEL.get(mode, "OTC SIGNAL")
        head.append(f"{pe('search')}{pe('diamond')} {label} Check Results"
                    f"{pe('search')}{pe('diamond')}")
        head.append(DIVIDER)
        if date_str:
            head.append(f"DATE: {pe('sparkle')}{dot_date(date_str)}{pe('star')}")
        head.append(f"{pe('next')}1 MINUTES:")
        head.append(f"    {pe('fire')} BLUE FISH PRIME {pe('fire')}")
    sig_lines = []
    for r in results:
        asset = r['asset'].upper().replace("_OTC", "-OTC")
        if r.get("mtg1"):
            # MTG1 mode: the MTG1 outcome IS the line's symbol — no
            # "(MTG1: ...)" suffix written beside it (the owner, 2026-11-25).
            # MTG1 WIN  -> check sticker + dot (won on MTG1, not M1).
            # MTG1 LOSS -> loss sticker. MTG1 DOJI -> minus + dot.
            # MTG1 NO DATA -> the M1 loss stands (nothing to show).
            if r["mtg1"] == "WIN":
                emoji = _sym["WIN"] + "."
            elif r["mtg1"] == "DOJI":
                emoji = _sym["DOJI"] + "."
            else:
                emoji = _sym["LOSS"]
        else:
            emoji = _sym.get(r["result"], "")
        # No arrow between the direction and the result symbol —
        # just the symbol (the owner, 2026-11-25).
        sig = f"{asset},{r['hhmm']} M1"
        if r.get("direction"):
            sig += f" {r['direction']}"
        sig_lines.append(f"<code>{sig}</code> {emoji}")
    foot = (f"{pe('chart')} Total: {summary['total']} | {pe('check')} {summary['win']} | "
            f"{pe('cross')} {summary['loss']} | {pe('minus')} {summary['doji']} | "
            f"? {summary['no_data']}\n"
            f"{pe('crown')} Accuracy: {summary['win_pct']}% {pe('fire')}")
    text = "\n".join(head)
    kept = 0
    for ln in sig_lines:
        add = ("\n\n" + ln) if text else ln
        # Reserve room for the footer (plus its leading blank line).
        if len(text) + len(add) + 2 + len(foot) > 4000:
            break
        text += add
        kept += 1
    dropped = len(sig_lines) - kept
    if dropped:
        text += f"\n\n… +{dropped} more lines"
    text += "\n\n" + foot
    return text
