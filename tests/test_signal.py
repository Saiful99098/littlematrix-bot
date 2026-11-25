"""Tests for the LittleMatrix local-test bot. Run: python tests/test_signal.py

Must pass:
1) Sample USDMXN 2026-11-21 09:00-10:59: raw 118 -> filtered exactly 13.
2) Checker demo: 10 signals -> 7 WIN, 3 LOSS, 70%.
3) DOJI candle returns DOJI (not DRAW, not WIN/LOSS).
4) 2-day tie (1-1) skipped; 3-day majority (2-1) wins at 66%.
5) NO DATA renders as plain '?' (no misleading WIN? sticker).
6) Checker MTG1 choice: with_mtg1 recovers 3 of 5 M1 losses
   (58.3% -> 83.3% MTG1 Accuracy); result lines have no arrow.
7) Formatter flips the asset format: -OTC <-> _otc, per line.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.checker import build_lookup, check_signals, format_check_results
from bot.signal_engine import (
    generate_futures_signals,
    load_json_candles,
    majority_vote,
)

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_candles_USDMXN_otc_2026-11-21.json"

EXPECTED_13 = [
    ("09:02", "CALL"), ("09:11", "CALL"), ("09:18", "PUT"),
    ("09:31", "PUT"), ("09:42", "PUT"), ("09:49", "PUT"),
    ("10:02", "PUT"), ("10:09", "CALL"), ("10:16", "CALL"),
    ("10:24", "CALL"), ("10:31", "CALL"), ("10:40", "CALL"),
    ("10:56", "CALL"),
]


def test_raw_118_filtered_13():
    asset, candles = load_json_candles(SAMPLE)
    assert asset == "USDMXN_otc", asset
    day = [c for c in candles if c["datetime"].startswith("2026-11-21")]
    window = [c for c in day if "09:00" <= c["hhmm"] <= "10:59"]
    assert len(window) == 120, f"window candles: {len(window)}"
    raw = [c for c in window if c["close"] != c["open"]]
    assert len(raw) == 118, f"raw signals: {len(raw)}"
    signals = generate_futures_signals([day], asset, "09:00", "10:59")
    got = [(s["hhmm"], s["direction"]) for s in signals]
    assert got == EXPECTED_13, f"filtered mismatch:\n got={got}\n exp={EXPECTED_13}"
    print("PASS 1) raw 118 -> filtered 13 (exact list)")


def test_checker_demo_7_of_10():
    _, candles = load_json_candles(SAMPLE)
    lookup = build_lookup(candles, "USDMXN_otc")
    lines = [
        "USDMXN-OTC,09:02 M1 CALL",
        "USDMXN-OTC,09:11 M1 CALL",
        "USDMXN-OTC,09:18 M1 PUT",
        "USDMXN-OTC,09:31 M1 PUT",
        "USDMXN-OTC,09:02 M1 PUT",
        "USDMXN-OTC,09:11 M1 PUT",
        "USDMXN-OTC,10:09 M1 CALL",
        "USDMXN-OTC,10:16 M1 CALL",
        "USDMXN-OTC,10:24 M1 PUT",
        "USDMXN-OTC,10:31 M1 CALL",
    ]
    results, summary = check_signals(lines, lookup, "2026-11-21")
    assert summary == {"total": 10, "win": 7, "loss": 3, "doji": 0,
                       "no_data": 0, "win_pct": 70.0}, summary
    print("PASS 2) checker demo 10 -> 7 WIN, 3 LOSS, 70%")


def test_doji_returns_doji():
    _, candles = load_json_candles(SAMPLE)
    lookup = build_lookup(candles, "USDMXN_otc")
    # 2026-11-21 19:55:00 has O == C == 19.91297 in the sample file.
    results, summary = check_signals(
        ["USDMXN-OTC,19:55 M1 CALL"], lookup, "2026-11-21")
    assert len(results) == 1
    assert results[0]["result"] == "DOJI", results[0]
    assert summary["doji"] == 1 and summary["win"] == 0 and summary["loss"] == 0
    print("PASS 3) DOJI candle -> DOJI (not DRAW, not WIN/LOSS)")


def _mk_candle(date, hhmm, direction):
    # Strong candle: body 0.10, range 0.12, ratio 0.83 -> passes strength.
    if direction == "CALL":
        o, c = 1.00, 1.10
    else:
        o, c = 1.10, 1.00
    return {"datetime": f"{date} {hhmm}:00", "O": o, "H": 1.11, "L": 0.99, "C": c}


def test_tie_and_majority():
    d, wr, calls, puts = majority_vote(["CALL", "PUT"])
    assert d is None and (calls, puts) == (1, 1), (d, calls, puts)
    d, wr, calls, puts = majority_vote(["CALL", "CALL", "PUT"])
    assert d == "CALL" and abs(wr - 2 / 3) < 1e-9, (d, wr)
    d, wr, calls, puts = majority_vote(["PUT", "PUT", "PUT"])
    assert d == "PUT" and wr == 1.0

    # End-to-end through the generator with synthetic days.
    day1 = [_mk_candle("2026-11-20", "09:02", "CALL"),
            _mk_candle("2026-11-20", "09:10", "CALL")]
    day2 = [_mk_candle("2026-11-21", "09:02", "PUT"),
            _mk_candle("2026-11-21", "09:10", "CALL")]
    # 09:02 is 1-1 tie -> skipped; 09:10 is 2-0 CALL -> kept.
    signals = generate_futures_signals([day1, day2], "TEST_otc", "09:00", "09:59")
    got = [(s["hhmm"], s["direction"]) for s in signals]
    assert got == [("09:10", "CALL")], got

    day3 = [_mk_candle("2026-11-22", "09:02", "CALL"),
            _mk_candle("2026-11-22", "09:10", "PUT")]
    # 3 days: 09:02 = CALL,CALL,PUT -> CALL 66% -> dropped by >=70% filter.
    # 09:10 = CALL,CALL,PUT -> CALL 66% -> also dropped.
    signals3 = generate_futures_signals([day1, day2, day3], "TEST_otc", "09:00", "09:59")
    assert signals3 == [], signals3
    print("PASS 4) 2-day 1-1 tie skipped; 3-day 2-1 = 66% (below 70% filter, dropped); majority_vote correct")


def test_no_data_renders_plain_question_mark():
    # 2026-11-25: the ZEROX premium ❓ (ID 6282525354242348869) renders
    # as a gold "WIN?" sticker, so every NO DATA line looked like WIN.
    # NO DATA must render as a plain "?" — never that custom emoji.
    results = [{"asset": "AUDNZD_otc", "hhmm": "01:00", "direction": "CALL",
                "result": "NO DATA", "actual": None}]
    summary = {"total": 1, "win": 0, "loss": 0, "doji": 0,
               "no_data": 1, "win_pct": 0.0}
    text = format_check_results(results, summary)
    assert "6282525354242348869" not in text, text
    assert "AUDNZD-OTC,01:00 M1 CALL ?" in text, text
    assert "→" not in text, text
    assert "? 1" in text, text
    print("PASS 5) NO DATA renders as plain '?' (no misleading WIN? sticker)")


def test_checker_mtg1_choice_no_arrow():
    # Saiful's real BTCUSD list 2026-11-25: M1 7 WIN / 5 LOSS = 58.3%.
    # With MTG1, 3 of the 5 losses recover (09:21, 09:42, 10:58 go the
    # signal direction; 09:07 and 10:22 do not) -> 10/12 = 83.3%.
    d = "2026-11-25"
    candles = [
        _mk_candle(d, "09:06", "CALL"), _mk_candle(d, "09:07", "CALL"),
        _mk_candle(d, "09:20", "PUT"), _mk_candle(d, "09:21", "CALL"),
        _mk_candle(d, "09:29", "PUT"),
        _mk_candle(d, "09:41", "CALL"), _mk_candle(d, "09:42", "PUT"),
        _mk_candle(d, "09:48", "PUT"), _mk_candle(d, "10:01", "PUT"),
        _mk_candle(d, "10:13", "PUT"),
        _mk_candle(d, "10:21", "CALL"), _mk_candle(d, "10:22", "CALL"),
        _mk_candle(d, "10:29", "PUT"), _mk_candle(d, "10:37", "PUT"),
        _mk_candle(d, "10:49", "CALL"),
        _mk_candle(d, "10:57", "CALL"), _mk_candle(d, "10:58", "PUT"),
    ]
    lookup = build_lookup(candles, "BTCUSD_otc")
    lines = ["BTCUSD-OTC,09:06 M1 PUT", "BTCUSD-OTC,09:20 M1 CALL",
             "BTCUSD-OTC,09:29 M1 PUT", "BTCUSD-OTC,09:41 M1 PUT",
             "BTCUSD-OTC,09:48 M1 PUT", "BTCUSD-OTC,10:01 M1 PUT",
             "BTCUSD-OTC,10:13 M1 PUT", "BTCUSD-OTC,10:21 M1 PUT",
             "BTCUSD-OTC,10:29 M1 PUT", "BTCUSD-OTC,10:37 M1 PUT",
             "BTCUSD-OTC,10:49 M1 CALL", "BTCUSD-OTC,10:57 M1 PUT"]
    # Non-MTG (M1 only): 7 WIN / 5 LOSS.
    _, summary = check_signals(lines, lookup, d)
    assert summary["win"] == 7 and summary["loss"] == 5, summary
    assert summary["win_pct"] == 58.3, summary
    assert "mtg_win_pct" not in summary, summary
    # MTG1: 3 losses recover -> 10/12 = 83.3%.
    _, summary_mtg = check_signals(lines, lookup, d, with_mtg1=True)
    assert summary_mtg["mtg_win"] == 10, summary_mtg
    assert summary_mtg["mtg_win_pct"] == 83.3, summary_mtg
    text = format_check_results(
        check_signals(lines, lookup, d, with_mtg1=True)[0], summary_mtg)
    assert "→" not in text, text
    assert "MTG1 Accuracy: 83.3%" in text, text
    print("PASS 6) checker MTG1 choice — M1 58.3%, MTG1 83.3%, no arrow")


def test_formatter_flips_format():
    from bot.text_tools import format_signal_list
    # Display form -> stored form, and back.
    assert (format_signal_list("USDPHP-OTC,23:45 M1 PUT")
            == "USDPHP_otc,23:45 M1 PUT")
    assert (format_signal_list("BTCUSD_otc,10:57 M1 PUT")
            == "BTCUSD-OTC,10:57 M1 PUT")
    # Mixed list flips per line; plain assets are unchanged.
    got = format_signal_list(
        "USDPHP-OTC,23:45 M1 PUT\nBTCUSD_otc,10:57 M1 PUT\n"
        "EURUSD,09:02 M1 CALL")
    assert got == ("USDPHP_otc,23:45 M1 PUT\nBTCUSD-OTC,10:57 M1 PUT\n"
                   "EURUSD,09:02 M1 CALL"), got
    print("PASS 7) formatter flips format — -OTC <-> _otc, per line")


def main():
    test_raw_118_filtered_13()
    test_checker_demo_7_of_10()
    test_doji_returns_doji()
    test_tie_and_majority()
    test_no_data_renders_plain_question_mark()
    test_checker_mtg1_choice_no_arrow()
    test_formatter_flips_format()
    print("ALL TESTS PASSED (7/7)")


if __name__ == "__main__":
    main()
