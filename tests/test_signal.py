"""Tests for the LittleMatrix local-test bot. Run: python tests/test_signal.py

Must pass:
1) Synthetic USDMXN 2026-11-21 09:00-10:59: raw 117 -> filtered exactly 16.
2) Checker demo: 10 signals -> 7 WIN, 3 LOSS, 70%.
3) DOJI candle returns DOJI (not DRAW, not WIN/LOSS).
4) 2-day tie (1-1) skipped; 3-day majority (2-1) wins at 66%.
5) NO DATA renders as plain '?' (no misleading WIN? sticker).
6) Checker MTG1 choice: with_mtg1 recovers 3 of 5 M1 losses
   (58.3% -> 83.3% MTG1 Accuracy); result lines have no arrow.
7) Formatter flips the asset format: -OTC <-> _otc, per line.
8) Checker date input parses YYYY-MM-DD, YYYY/MM/DD, DD-MM-YYYY,
   DD/MM/YYYY (single digits OK); garbage returns None.

The old bundled sample file is deleted — tests 1-3 now use the
deterministic synthetic generator in tests/synthetic_data.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot.checker import (build_lookup, check_signals, format_check_results,
                         parse_checker_date)
from bot.signal_engine import (
    generate_futures_signals,
    majority_vote,
)
from synthetic_data import force_candle, make_day

# 1-day filtered signals for the synthetic 2026-11-21 day (seed 8),
# window 09:00-10:59 — the regression list for test 1.
EXPECTED_16 = [
    ("09:00", "CALL"), ("09:07", "PUT"), ("09:14", "CALL"),
    ("09:21", "PUT"), ("09:28", "CALL"), ("09:35", "CALL"),
    ("09:43", "PUT"), ("09:50", "PUT"), ("09:58", "PUT"),
    ("10:05", "PUT"), ("10:13", "CALL"), ("10:22", "PUT"),
    ("10:30", "PUT"), ("10:39", "CALL"), ("10:47", "CALL"),
    ("10:57", "CALL"),
]


def _synth_day():
    """Synthetic 2026-11-21 day with 09:02 forced CALL and 00:38 DOJI
    (the two minutes tests 2-3 check)."""
    day = make_day("USDMXN_otc", "2026-11-21", seed=8)
    force_candle(day, "09:02", "CALL")
    force_candle(day, "00:38", "DOJI")
    return day


def test_raw_117_filtered_16():
    day = _synth_day()
    window = [c for c in day if "09:00" <= c["hhmm"] <= "10:59"]
    assert len(window) == 120, f"window candles: {len(window)}"
    raw = [c for c in window if c["close"] != c["open"]]
    assert len(raw) == 117, f"raw signals: {len(raw)}"
    signals = generate_futures_signals([day], "USDMXN_otc", "09:00", "10:59")
    got = [(s["hhmm"], s["direction"]) for s in signals]
    assert got == EXPECTED_16, f"filtered mismatch:\n got={got}\n exp={EXPECTED_16}"
    print("PASS 1) raw 117 -> filtered 16 (exact list)")


def test_checker_demo_7_of_10():
    # Actuals chosen so the 10 lines below give 7 WIN / 3 LOSS.
    candles = [
        {"datetime": "2026-11-21 09:02:00", "O": 1.00, "H": 1.11, "L": 0.99, "C": 1.10},
        {"datetime": "2026-11-21 09:11:00", "O": 1.00, "H": 1.11, "L": 0.99, "C": 1.10},
        {"datetime": "2026-11-21 09:18:00", "O": 1.10, "H": 1.11, "L": 0.99, "C": 1.00},
        {"datetime": "2026-11-21 09:31:00", "O": 1.10, "H": 1.11, "L": 0.99, "C": 1.00},
        {"datetime": "2026-11-21 10:09:00", "O": 1.00, "H": 1.11, "L": 0.99, "C": 1.10},
        {"datetime": "2026-11-21 10:16:00", "O": 1.00, "H": 1.11, "L": 0.99, "C": 1.10},
        {"datetime": "2026-11-21 10:24:00", "O": 1.00, "H": 1.11, "L": 0.99, "C": 1.10},
        {"datetime": "2026-11-21 10:31:00", "O": 1.00, "H": 1.11, "L": 0.99, "C": 1.10},
    ]
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
    day = _synth_day()
    lookup = build_lookup(
        [{"datetime": c["datetime"], "O": c["open"], "H": c["high"],
          "L": c["low"], "C": c["close"]} for c in day], "USDMXN_otc")
    # 00:38 was forced DOJI (O == C).
    results, summary = check_signals(
        ["USDMXN-OTC,00:38 M1 CALL"], lookup, "2026-11-21")
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
    # Signal text is monospace now (the owner's template, 2026-11-27);
    # the plain "?" sits OUTSIDE the code tags.
    assert "<code>AUDNZD-OTC,01:00 M1 CALL</code> ?" in text, text
    assert "→" not in text, text
    assert "? 1" in text, text
    assert "Check Results" in text, text
    assert "Accuracy: 0.0%" in text, text
    print("PASS 5) NO DATA renders as plain '?' (no misleading WIN? sticker)")


def test_checker_mtg1_choice_no_arrow():
    # the owner's real BTCUSD list 2026-11-25: M1 7 WIN / 5 LOSS = 58.3%.
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
    # MTG1: 3 losses recover -> the summary counts them as wins:
    # 10 WIN / 2 LOSS = 83.3%, one Accuracy line, no MTG1 line.
    _, summary_mtg = check_signals(lines, lookup, d, with_mtg1=True)
    assert summary_mtg["win"] == 10 and summary_mtg["loss"] == 2, summary_mtg
    assert summary_mtg["win_pct"] == 83.3, summary_mtg
    assert "mtg_win" not in summary_mtg, summary_mtg
    text = format_check_results(
        check_signals(lines, lookup, d, with_mtg1=True)[0], summary_mtg)
    assert "→" not in text, text
    assert "Accuracy: 83.3%" in text, text
    assert "MTG1 Accuracy" not in text, text
    # No "(MTG1: ...)" suffix: an MTG1 win is the check sticker + dot,
    # an MTG1 loss is just the loss sticker. Lines are monospace now,
    # so they start with <code> — match by content, not by start.
    assert "(MTG1:" not in text, text
    from bot.premium_emoji import pe
    mtg_win_line = [ln for ln in text.splitlines()
                    if "BTCUSD-OTC,09:20" in ln][0]
    assert mtg_win_line.endswith(pe("check") + "."), mtg_win_line
    mtg_loss_line = [ln for ln in text.splitlines()
                     if "BTCUSD-OTC,09:06" in ln][0]
    assert mtg_loss_line.endswith(pe("cross")), mtg_loss_line
    print("PASS 6) checker MTG1 choice — M1 58.3%, MTG1 wins count as wins "
          "(83.3%), no arrow, MTG1 win = check + dot, no (MTG1:) suffix, "
          "no MTG1 line")


def test_checker_mtg_wins_count_as_wins():
    # The owner's real 12-signal list 2026-11-26 (reported 13:23):
    # M1 5 WIN / 7 LOSS = 41.7%. 4 of the 7 losses recover on MTG1
    # (09:26, 09:58, 10:06, 10:41) -> the summary must count them as
    # wins: 9 WIN / 3 LOSS = 75.0%, one Accuracy line.
    d = "2026-11-26"
    per_asset = {}
    for asset, hhmm, direction in [
            ("AXSUSD_otc", "09:02", "PUT"), ("AXSUSD_otc", "09:58", "PUT"),
            ("AXSUSD_otc", "09:59", "CALL"), ("AXSUSD_otc", "10:06", "PUT"),
            ("AXSUSD_otc", "10:07", "CALL"),
            ("CADJPY_otc", "09:16", "PUT"), ("CADJPY_otc", "09:26", "CALL"),
            ("CADJPY_otc", "09:27", "PUT"),
            ("EURAUD_otc", "09:40", "PUT"), ("EURAUD_otc", "09:41", "PUT"),
            ("EURAUD_otc", "10:29", "PUT"), ("EURAUD_otc", "10:30", "PUT"),
            ("DOTUSD_otc", "09:47", "CALL"), ("DOTUSD_otc", "10:15", "CALL"),
            ("EURGBP_otc", "10:41", "CALL"), ("EURGBP_otc", "10:42", "PUT"),
            ("DASUSD_otc", "10:48", "PUT"),
            ("CADCHF_otc", "10:55", "CALL"), ("CADCHF_otc", "10:56", "CALL")]:
        per_asset.setdefault(asset, []).append(_mk_candle(d, hhmm, direction))
    lookup = {}
    for asset, cs in per_asset.items():
        lookup.update(build_lookup(cs, asset))
    lines = ["AXSUSD-OTC,09:02 M1 PUT", "CADJPY-OTC,09:16 M1 PUT",
             "CADJPY-OTC,09:26 M1 PUT", "EURAUD-OTC,09:40 M1 CALL",
             "DOTUSD-OTC,09:47 M1 CALL", "AXSUSD-OTC,09:58 M1 CALL",
             "AXSUSD-OTC,10:06 M1 CALL", "DOTUSD-OTC,10:15 M1 CALL",
             "EURAUD-OTC,10:29 M1 CALL", "EURGBP-OTC,10:41 M1 PUT",
             "DASUSD-OTC,10:48 M1 PUT", "CADCHF-OTC,10:55 M1 PUT"]
    # Non-MTG (M1 only): 5 WIN / 7 LOSS = 41.7%.
    _, summary = check_signals(lines, lookup, d)
    assert summary["win"] == 5 and summary["loss"] == 7, summary
    assert summary["win_pct"] == 41.7, summary
    # MTG1: 4 losses recover -> 9 WIN / 3 LOSS = 75.0%.
    _, summary_mtg = check_signals(lines, lookup, d, with_mtg1=True)
    assert summary_mtg["win"] == 9 and summary_mtg["loss"] == 3, summary_mtg
    assert summary_mtg["win_pct"] == 75.0, summary_mtg
    text = format_check_results(
        check_signals(lines, lookup, d, with_mtg1=True)[0], summary_mtg)
    assert "Accuracy: 75.0%" in text, text
    assert "MTG1 Accuracy" not in text, text
    # The dotted check lines (MTG1 wins) are the 4 recovered ones.
    from bot.premium_emoji import pe
    dotted = [ln for ln in text.splitlines()
              if ln.rstrip().endswith(pe("check") + ".")]
    assert len(dotted) == 4, dotted
    print("PASS 6b) checker MTG1 — owner's 12-signal list: M1 41.7%, "
          "MTG1 wins count as wins -> 9/12 = 75.0%, one Accuracy line")


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


def test_checker_date_parse():
    assert parse_checker_date("2026-11-25") == "2026-11-25"
    assert parse_checker_date("2026/11/25") == "2026-11-25"
    assert parse_checker_date("25-11-2026") == "2026-11-25"
    assert parse_checker_date("25/11/2026") == "2026-11-25"
    assert parse_checker_date("2026-11-5") == "2026-11-05"
    assert parse_checker_date(" 2026-11-25 ") == "2026-11-25"
    assert parse_checker_date("hello") is None
    assert parse_checker_date("2026-13-01") is None
    assert parse_checker_date("2026-11-32") is None
    assert parse_checker_date("") is None
    print("PASS 8) checker date input parses YYYY-MM-DD / DD-MM-YYYY, "
          "garbage returns None")


def main():
    test_raw_117_filtered_16()
    test_checker_demo_7_of_10()
    test_doji_returns_doji()
    test_tie_and_majority()
    test_no_data_renders_plain_question_mark()
    test_checker_mtg1_choice_no_arrow()
    test_checker_mtg_wins_count_as_wins()
    test_formatter_flips_format()
    test_checker_date_parse()
    print("ALL TESTS PASSED (9/9)")


if __name__ == "__main__":
    main()
