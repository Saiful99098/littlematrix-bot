"""CHECKER — check a pasted signal list against candle data.

Rules agreed with Saiful:
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
    # Premium symbols (Saiful: checker symbols must be premium too).
    _sym = {"WIN": pe("check"), "LOSS": pe("cross"),
            "DOJI": pe("minus"), "NO DATA": pe("question")}
    lines = []
    for r in results:
        emoji = _sym.get(r["result"], "")
        asset = r['asset'].upper().replace("_OTC", "-OTC")
        line = f"{asset},{r['hhmm']} M1 {r['direction']} → {emoji}"
        if r.get("mtg1"):
            line += f" (MTG1: {_sym.get(r['mtg1'], '')})"
        lines.append(line)
    lines.append("")
    lines.append(f"{pe('chart')} Total: {summary['total']} | {pe('check')} {summary['win']} | "
                 f"{pe('cross')} {summary['loss']} | {pe('minus')} {summary['doji']} | "
                 f"{pe('question')} {summary['no_data']}")
    lines.append(f"Accuracy: {summary['win_pct']}%")
    return "\n".join(lines)
