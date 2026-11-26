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
  same direction. Reported as an extra line only, never overwriting
  the M1 result.
"""
from __future__ import annotations

import re

from .premium_emoji import pe
from .signal_engine import candle_direction, normalize_candle

LINE_RE = re.compile(
    r"^\s*([A-Za-z0-9]+(?:[-_][A-Za-z]+)?)\s*,\s*(\d{1,2}:\d{2})\s+M1\s+(CALL|PUT)\s*$",
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
    """Parse one line. Returns (asset, hhmm, direction) or None."""
    m = LINE_RE.match(line.strip())
    if not m:
        return None
    asset, hhmm, direction = m.group(1).upper(), m.group(2), m.group(3).upper()
    h, mi = hhmm.split(":")
    hhmm = f"{int(h):02d}:{int(mi):02d}"
    return asset, hhmm, direction


def _norm_asset(asset):
    return asset.upper().replace("_OTC", "-OTC")


def check_signals(lines, candles_by_key, date_str, with_mtg1=False):
    """Check signal lines against candles.

    candles_by_key: dict {(asset_norm, 'YYYY-MM-DD HH:MM:00'): candle dict}
    date_str: 'YYYY-MM-DD' the user chose for the list.
    Returns (results, summary).
    results: list of {asset, hhmm, direction, result, actual}
    result in WIN / LOSS / DOJI / NO DATA.
    summary: {total, win, loss, doji, no_data, win_pct}
    """
    results = []
    for line in lines:
        parsed = parse_signal_line(line)
        if parsed is None:
            continue
        asset, hhmm, direction = parsed
        key = (_norm_asset(asset), f"{date_str} {hhmm}:00")
        candle = candles_by_key.get(key)
        if candle is None:
            # Try without -OTC / with _otc variants.
            for k, c in candles_by_key.items():
                if k[1] == f"{date_str} {hhmm}:00":
                    candle = c
                    break
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
    decided = win + loss
    win_pct = round(win / decided * 100, 1) if decided else 0.0
    summary = {"total": total, "win": win, "loss": loss, "doji": doji,
               "no_data": no_data, "win_pct": win_pct}
    if with_mtg1:
        # MTG1 accuracy: M1 WINs plus M1 LOSSes recovered by MTG1.
        mtg_wins = sum(1 for r in results
                       if r["result"] == "WIN"
                       or (r["result"] == "LOSS" and r.get("mtg1") == "WIN"))
        summary["mtg_win"] = mtg_wins
        summary["mtg_win_pct"] = (round(mtg_wins / decided * 100, 1)
                                  if decided else 0.0)
    return results, summary


def _check_mtg1(asset, hhmm, direction, candles_by_key, date_str):
    """Check the next minute with the same direction. Returns WIN/LOSS/DOJI/NO DATA."""
    h, m = map(int, hhmm.split(":"))
    m += 1
    if m >= 60:
        h += 1
        m -= 60
    next_hhmm = f"{h:02d}:{m:02d}"
    key = (_norm_asset(asset), f"{date_str} {next_hhmm}:00")
    candle = candles_by_key.get(key)
    if candle is None:
        for k, c in candles_by_key.items():
            if k[1] == f"{date_str} {next_hhmm}:00":
                candle = c
                break
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


def format_check_results(results, summary):
    """Format checker output for Telegram — symbols only, no outcome words."""
    # Premium symbols (the owner: checker symbols must be premium too).
    # NO DATA is plain "?" on purpose: the ZEROX premium ❓
    # (ID 6282525354242348869) renders as a gold "WIN?" sticker,
    # so users read NO DATA as WIN. Plain "?" cannot be misread.
    _sym = {"WIN": pe("check"), "LOSS": pe("cross"),
            "DOJI": pe("minus"), "NO DATA": "?"}
    lines = []
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
        line = f"{asset},{r['hhmm']} M1 {r['direction']} {emoji}"
        lines.append(line)
    lines.append("")
    lines.append(f"{pe('chart')} Total: {summary['total']} | {pe('check')} {summary['win']} | "
                 f"{pe('cross')} {summary['loss']} | {pe('minus')} {summary['doji']} | "
                 f"? {summary['no_data']}")
    lines.append(f"Accuracy: {summary['win_pct']}%")
    if "mtg_win_pct" in summary:
        lines.append(f"MTG1 Accuracy: {summary['mtg_win_pct']}%")
    return "\n".join(lines)
