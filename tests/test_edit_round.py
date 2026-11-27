"""Tests for the 2026-11-27 edit round (admin dashboard, FS template,
checker template, blackout/whiteout logic, % threshold, checker modes,
Bengali Rules footer, MTG 1 STEP, DD.MM.YYYY dates).

Run: python tests/test_edit_round.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from synthetic_data import force_candle, make_day, to_raw  # noqa: E402

from bot import candles_db as candles_db_mod  # noqa: E402
from bot import config, database, handlers  # noqa: E402
from bot.candles_db import CandleDB, DHAKA  # noqa: E402
from bot.checker import (build_lookup, check_signals, dot_date,  # noqa: E402
                         format_check_results, parse_signal_line)
from bot.collector import rows_from_raw  # noqa: E402
from bot.signal_engine import (cross_asset_filter,  # noqa: E402
                               generate_futures_signals,
                               generate_prev_minute_signals)
from bot.premium_emoji import pe  # noqa: E402


def _mk_candle(date, hhmm, direction):
    # Strong candle: body 0.10, range 0.12, ratio 0.83 -> passes strength.
    if direction == "CALL":
        o, c = 1.00, 1.10
    else:
        o, c = 1.10, 1.00
    return {"datetime": f"{date} {hhmm}:00", "O": o, "H": 1.11, "L": 0.99, "C": c}


def _mk_doji(date, hhmm):
    return {"datetime": f"{date} {hhmm}:00", "O": 1.05, "H": 1.11,
            "L": 0.99, "C": 1.05}


# ---------------------------------------------------------------------------
# 1) Blackout / whiteout engine (T-1 logic)
# ---------------------------------------------------------------------------
def test_prev_minute_engine():
    # 3 days, each with ONLY a 09:01 candle: CALL, CALL, PUT.
    # BLACKOUT flips each -> PUT, PUT, CALL -> PUT at 66%.
    # WHITEOUT keeps each -> CALL, CALL, PUT -> CALL at 66%.
    days = [[_mk_candle("2026-11-20", "09:01", "CALL")],
            [_mk_candle("2026-11-21", "09:01", "CALL")],
            [_mk_candle("2026-11-22", "09:01", "PUT")]]
    # 66% < 70% default -> both modes drop the 09:02 signal.
    assert generate_prev_minute_signals(days, "TEST_otc", "09:00", "09:59",
                                        mode="BLACKOUT") == []
    assert generate_prev_minute_signals(days, "TEST_otc", "09:00", "09:59",
                                        mode="WHITEOUT") == []
    # With a 60% threshold both keep it, with the right direction.
    bw = generate_prev_minute_signals(days, "TEST_otc", "09:00", "09:59",
                                      mode="BLACKOUT", min_win_rate=0.60)
    assert [(s["hhmm"], s["direction"]) for s in bw] == [("09:02", "PUT")], bw
    wo = generate_prev_minute_signals(days, "TEST_otc", "09:00", "09:59",
                                      mode="WHITEOUT", min_win_rate=0.60)
    assert [(s["hhmm"], s["direction"]) for s in wo] == [("09:02", "CALL")], wo
    # 3-0 (100%) passes the default 70% in both modes.
    days3 = [[_mk_candle("2026-11-20", "09:01", "CALL")],
             [_mk_candle("2026-11-21", "09:01", "CALL")],
             [_mk_candle("2026-11-22", "09:01", "CALL")]]
    bw3 = generate_prev_minute_signals(days3, "TEST_otc", "09:00", "09:59",
                                       mode="BLACKOUT")
    assert [(s["hhmm"], s["direction"]) for s in bw3] == [("09:02", "PUT")], bw3
    assert bw3[0]["win_rate"] == 1.0
    # T=00:00 never produces a signal (its T-1 belongs to the
    # previous day, which is not in the selected days) — even with a
    # strong 00:00 candle. The 00:00 candle IS the T-1 for 00:01.
    days0 = [[_mk_candle("2026-11-20", "00:00", "CALL")]]
    got0 = generate_prev_minute_signals(days0, "TEST_otc", "00:00", "00:59",
                                        mode="WHITEOUT")
    assert [(s["hhmm"], s["direction"]) for s in got0] == [("00:01", "CALL")], got0
    # A DOJI T-1 skips that day's vote.
    days_d = [[_mk_doji("2026-11-20", "09:01")],
              [_mk_candle("2026-11-21", "09:01", "CALL")]]
    got_d = generate_prev_minute_signals(days_d, "TEST_otc", "09:00", "09:59",
                                         mode="WHITEOUT")
    assert [(s["hhmm"], s["direction"]) for s in got_d] == [("09:02", "CALL")], got_d
    print("PASS 1) blackout/whiteout engine — flip/keep, majority vote, "
          "% filter, 00:00 skipped, DOJI T-1 skipped")


# ---------------------------------------------------------------------------
# 2) Gap removal (FS generators only — Auto Live Signal keeps the gap)
# ---------------------------------------------------------------------------
def test_gap_removed_in_fs_generators():
    day = make_day("USDMXN_otc", "2026-11-21", seed=8)
    with_gap = generate_futures_signals([day], "USDMXN_otc", "09:00", "10:59")
    assert len(with_gap) == 16, len(with_gap)  # the verified sample
    without_gap = generate_futures_signals([day], "USDMXN_otc", "09:00", "10:59",
                                           apply_gap=False)
    assert len(without_gap) > 16, len(without_gap)
    mins = [int(s["hhmm"][:2]) * 60 + int(s["hhmm"][3:]) for s in without_gap]
    assert any(b - a < 7 for a, b in zip(mins, mins[1:])), mins
    # Cross-asset: two signals 2 minutes apart.
    pool = [{"asset": "A_otc", "hhmm": "09:02", "direction": "CALL",
             "win_rate": 1.0, "strength": 0.9},
            {"asset": "B_otc", "hhmm": "09:04", "direction": "PUT",
             "win_rate": 1.0, "strength": 0.9}]
    assert len(cross_asset_filter(pool)) == 1, cross_asset_filter(pool)
    assert len(cross_asset_filter(pool, apply_gap=False)) == 2
    print("PASS 2) 7-min gap removed in FS generators (default keeps it "
          "for Auto Live Signal + the verified sample)")


# ---------------------------------------------------------------------------
# 3) % threshold parsing (user types the number)
# ---------------------------------------------------------------------------
def test_pct_parsing():
    assert handlers._parse_fs_pct("80") == 80
    assert handlers._parse_fs_pct("70") == 70
    assert handlers._parse_fs_pct("100") == 100
    assert handlers._parse_fs_pct("80%") == 80
    assert handlers._parse_fs_pct(" 90 ") == 90
    assert handlers._parse_fs_pct("69") is None
    assert handlers._parse_fs_pct("101") is None
    assert handlers._parse_fs_pct("abc") is None
    assert handlers._parse_fs_pct("80.5") is None
    assert handlers._parse_fs_pct("") is None
    print("PASS 3) % parsing — 70-100 accepted, anything else rejected")


# ---------------------------------------------------------------------------
# 4) FS template + Rules footer + date format
# ---------------------------------------------------------------------------
def test_fs_template_and_footer():
    head = handlers._futures_template_head("OTC FS", "27.11.2026", 80)
    assert "FUTURE SIGNALS — OTC FS" in head, head
    assert "27.11.2026" in head, head
    assert "MTG 1 STEP" in head, head
    assert "MARTINGALE" not in head, head
    assert "Avoid Below 80%" in head, head
    assert "TIMEZONE: UTC/GMT +6:00 [DHAKA, BANGLADESH]" in head, head
    assert "1 MINUTES:" in head, head
    assert "BLUE FISH PRIME" in head, head
    # No extra lines from the old format.
    assert "Assets:" not in head, head
    assert "Source:" not in head, head
    assert "Signals:" not in head, head
    assert "No signal guarantees profit" not in head, head
    assert "Generated:" not in head, head
    # Real market title.
    assert "FUTURE SIGNALS — REAL MARKET FS" in handlers._futures_template_head(
        "REAL", "27.11.2026", 70)
    assert "Real Market Pairs" in handlers._futures_template_head(
        "REAL", "27.11.2026", 70)
    # Rules footer: blackout = opposite, whiteout = same; OTC none.
    bw = handlers._rules_footer("BLACKOUT FS")
    assert "Opposite Direction" in bw and "বিপরীত" in bw, bw
    assert "14:50" in bw and "14:51" in bw, bw
    wo = handlers._rules_footer("WHITEOUT FS")
    assert "Same Direction" in wo and "একই" in wo, wo
    assert handlers._rules_footer("OTC FS") == ""
    assert handlers._rules_footer("REAL") == ""
    # Date format.
    assert dot_date("2026-11-27") == "27.11.2026"
    # Line fitting: a huge list is cut by whole lines, never mid-tag.
    lines = [f"<code>USDARS-OTC,09:{i:02d} M1 PUT</code>" for i in range(200)]
    text = handlers._fit_futures_text(head, lines, bw)
    assert text.count("<code>") == text.count("</code>"), text[-200:]
    assert "+1" in text or "more" in text, text[-200:]
    print("PASS 4) FS template — title, DD.MM.YYYY date, MTG 1 STEP, "
          "% line, no old extra lines; Rules footer blackout/whiteout only")


# ---------------------------------------------------------------------------
# 5) Checker template
# ---------------------------------------------------------------------------
def test_checker_template():
    results = [{"asset": "USDPKR_otc", "hhmm": "02:39", "direction": "PUT",
                "result": "LOSS", "actual": "CALL"},
               {"asset": "USDPKR_otc", "hhmm": "02:40", "direction": "CALL",
                "result": "WIN", "actual": "CALL"}]
    summary = {"total": 2, "win": 1, "loss": 1, "doji": 0,
               "no_data": 0, "win_pct": 50.0}
    text = format_check_results(results, summary, date_str="2026-11-27")
    assert "Check Results" in text, text
    assert "DATE: 27.11.2026" in text, text
    assert "<code>USDPKR-OTC,02:39 M1 PUT</code>" in text, text
    assert "Total: 2" in text, text
    assert "Accuracy: 50.0%" in text, text
    # No header for Backtest Lab.
    plain = format_check_results(results, summary, header=False)
    assert "Check Results" not in plain, plain
    assert "<code>USDPKR-OTC,02:39 M1 PUT</code>" in plain, plain
    assert "Accuracy: 50.0%" in plain, plain
    # Directionless lines print without CALL/PUT.
    bw_results = [{"asset": "USDARS_otc", "hhmm": "09:07", "direction": None,
                   "result": "WIN", "actual": "PUT"}]
    bw_text = format_check_results(bw_results, {"total": 1, "win": 1, "loss": 0,
                                                "doji": 0, "no_data": 0,
                                                "win_pct": 100.0},
                                   date_str="2026-11-27")
    assert "<code>USDARS-OTC,09:07 M1</code>" in bw_text, bw_text
    assert "M1 CALL" not in bw_text and "M1 PUT" not in bw_text, bw_text
    print("PASS 5) checker template — header, DATE DD.MM.YYYY, monospace "
          "lines, Total + Accuracy footer, header=False for backtest")


# ---------------------------------------------------------------------------
# 6) Checker: blackout/whiteout derived check
# ---------------------------------------------------------------------------
def test_checker_blackout_derived():
    d = "2026-11-25"
    candles = [
        _mk_candle(d, "09:01", "CALL"),  # T-1 green -> blackout PUT
        _mk_candle(d, "09:02", "PUT"),   # T red -> blackout WIN
        _mk_candle(d, "09:06", "CALL"),  # T-1 green -> blackout PUT
        _mk_candle(d, "09:07", "CALL"),  # T green -> blackout LOSS
        _mk_candle(d, "09:08", "PUT"),   # T+1 red -> MTG1 WIN
        _mk_doji(d, "09:11"),            # T-1 DOJI
        _mk_candle(d, "09:12", "PUT"),
    ]
    lookup = build_lookup(candles, "USDARS_otc")
    # Directionless lines are parsed (direction None).
    assert parse_signal_line("USDARS-OTC,09:07 M1") == ("USDARS-OTC", "09:07", None)
    # Strict format stays: no space between time and M1 is skipped.
    assert parse_signal_line("USDARS-OTC,09:07M1") is None
    # Blackout: 09:02 WIN, 09:07 LOSS, 09:12 DOJI (T-1 was DOJI).
    results, summary = check_signals(
        ["USDARS-OTC,09:02 M1", "USDARS-OTC,09:07 M1",
         "USDARS-OTC,09:12 M1"], lookup, d, mode="BLACKOUT")
    assert [r["result"] for r in results] == ["WIN", "LOSS", "DOJI"], results
    assert summary == {"total": 3, "win": 1, "loss": 1, "doji": 1,
                       "no_data": 0, "win_pct": 50.0}, summary
    # Whiteout: same T-1 -> expected CALL; 09:02 PUT is a LOSS.
    results_w, summary_w = check_signals(
        ["USDARS-OTC,09:02 M1"], lookup, d, mode="WHITEOUT")
    assert results_w[0]["result"] == "LOSS", results_w
    assert summary_w["win_pct"] == 0.0
    # T-1 missing (09:20 has no 09:19 candle) -> NO DATA.
    results_n, _ = check_signals(["USDARS-OTC,09:20 M1"], lookup, d,
                                 mode="BLACKOUT")
    assert results_n[0]["result"] == "NO DATA", results_n
    # T=00:00 has no T-1 in the day -> NO DATA.
    results_0, _ = check_signals(["USDARS-OTC,00:00 M1"], lookup, d,
                                 mode="BLACKOUT")
    assert results_0[0]["result"] == "NO DATA", results_0
    # A pasted line WITH a direction is checked vs the T candle
    # directly, in blackout mode too.
    results_d, _ = check_signals(["USDARS-OTC,09:02 M1 PUT"], lookup, d,
                                 mode="BLACKOUT")
    assert results_d[0]["result"] == "WIN", results_d
    # Directionless lines are skipped in OTC mode (nothing to check).
    results_o, summary_o = check_signals(["USDARS-OTC,09:02 M1"], lookup, d,
                                         mode="OTC")
    assert results_o == [] and summary_o["total"] == 0, summary_o
    # MTG1 on a derived check: the 09:07 loss recovers on 09:08,
    # the dot shows, and the summary counts it as a win.
    results_m, summary_m = check_signals(
        ["USDARS-OTC,09:07 M1"], lookup, d, mode="BLACKOUT", with_mtg1=True)
    assert results_m[0]["mtg1"] == "WIN", results_m
    assert summary_m["win"] == 1 and summary_m["loss"] == 0, summary_m
    assert summary_m["win_pct"] == 100.0
    text = format_check_results(results_m, summary_m, date_str=d)
    dotted = [ln for ln in text.splitlines()
              if "USDARS-OTC,09:07" in ln][0]
    assert dotted.endswith(pe("check") + "."), dotted
    print("PASS 6) checker blackout/whiteout — derived from T-1, judged "
          "by T, DOJI/NO DATA edges, direction lines checked directly, "
          "MTG1 recovery with dot")


# ---------------------------------------------------------------------------
# 7) Admin Dashboard: stats + keyboard + menu visibility
# ---------------------------------------------------------------------------
def test_admin_stats_and_keyboard():
    old_url, old_path = config.DATABASE_URL, config.DB_PATH
    try:
        config.DATABASE_URL = ""
        config.DB_PATH = Path(tempfile.mkdtemp()) / "users.db"
        database._CONN = None
        # User 1: active now + premium. User 2: last seen 20 min ago.
        database.get_or_create_user(111, "one")
        database.grant_premium(111, days=30)
        database.get_or_create_user(222, "two")
        old_seen = (datetime.now() - timedelta(minutes=20)).isoformat(
            timespec="seconds")
        database._execute("UPDATE users SET last_seen=? WHERE telegram_id=?",
                          (old_seen, 222))
        stats = database.get_user_stats()
        assert stats == {"total": 2, "premium": 1, "free": 1,
                         "online": 1}, stats
        # The dashboard keyboard shows the counts as display-only
        # buttons; tapping one does nothing.
        kb = handlers._admin_dashboard_keyboard(stats)
        btns = [b for row in kb.inline_keyboard for b in row]
        assert any(b.text == "Total Users: 2" for b in btns), btns
        assert any(b.text == "Online Now: 1" for b in btns), btns
        assert any(b.text == "Premium: 1" for b in btns), btns
        assert any(b.text == "Free: 1" for b in btns), btns
        stat_btns = [b for b in btns if b.callback_data == "admin_stat_noop"]
        assert len(stat_btns) == 4, btns
        assert any(b.callback_data == "menu_main" for b in btns), btns
        # The menu shows Admin Dashboard ONLY to the owner.
        admin_kb = handlers.main_keyboard(admin=True)
        admin_btns = [b for row in admin_kb.inline_keyboard for b in row]
        assert any(b.callback_data == "menu_admin" for b in admin_btns)
        plain_kb = handlers.main_keyboard(admin=False)
        plain_btns = [b for row in plain_kb.inline_keyboard for b in row]
        assert not any(b.callback_data == "menu_admin" for b in plain_btns)
        old_admins = config.ADMIN_USER_IDS
        try:
            config.ADMIN_USER_IDS = {5618029321}
            assert handlers._is_admin(5618029321) is True
            assert handlers._is_admin(999) is False
        finally:
            config.ADMIN_USER_IDS = old_admins
    finally:
        config.DATABASE_URL, config.DB_PATH = old_url, old_path
        database._CONN = None
    print("PASS 7) admin stats (total/premium/free/online) + dashboard "
          "keyboard + menu visibility (admin only)")


# ---------------------------------------------------------------------------
# 8) Checker 4-mode flow end-to-end (mode -> MTG -> date -> list)
# ---------------------------------------------------------------------------
def _tmp_db():
    tmp = tempfile.TemporaryDirectory()
    db = CandleDB(database_url="", sqlite_path=Path(tmp.name) / "candles.db").init()
    db._tmpdir = tmp
    return db, tmp


class FakeMessage:
    def __init__(self, message_id=123):
        self.message_id = message_id
        self.chat_id = 999


class FakeQuery:
    def __init__(self, data, message_id=123):
        self.data = data
        self.message = FakeMessage(message_id)
        self.texts = []
        self.markups = []

    async def answer(self):
        return None

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.texts.append(text)
        self.markups.append(reply_markup)
        return None


class FakeUser:
    id = 999
    username = "tester"


class FakeUpdate:
    def __init__(self, query):
        self.callback_query = query
        self.effective_user = FakeUser()


class FakeMember:
    status = "member"


class FakeBot:
    async def get_chat_member(self, chat_id, user_id):
        return FakeMember()


class FakeContext:
    def __init__(self):
        self.user_data = {}
        self.bot = FakeBot()


class FakeWorkingMessage:
    def __init__(self):
        self.texts = []
        self.markups = []

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        self.texts.append(text)
        self.markups.append(reply_markup)
        return None


class FakeTextMessage:
    def __init__(self, text):
        self.text = text
        self.texts = []
        self.markups = []
        self.working = FakeWorkingMessage()

    async def reply_text(self, text, reply_markup=None, parse_mode=None):
        self.texts.append(text)
        self.markups.append(reply_markup)
        return self.working


class FakeTextUpdate:
    def __init__(self, text):
        self.message = FakeTextMessage(text)
        self.effective_user = FakeUser()


def _drive(ctx, query, data):
    query.data = data
    asyncio.run(handlers.menu_callback(FakeUpdate(query), ctx))
    return query


def _send_text(ctx, text):
    upd = FakeTextUpdate(text)
    asyncio.run(handlers.text_message(upd, ctx))
    return upd


def _last_buttons(query):
    kb = query.markups[-1]
    return [b for row in kb.inline_keyboard for b in row]


def _button(query, callback_data):
    for b in _last_buttons(query):
        if b.callback_data == callback_data:
            return b
    return None


def _setup(db):
    real_get_db = candles_db_mod.get_candle_db
    real_mode = config.DATA_MODE
    real_user = handlers.database.get_or_create_user
    candles_db_mod.get_candle_db = lambda: db
    config.DATA_MODE = "db"
    handlers.database.get_or_create_user = lambda *a, **k: None

    def restore():
        candles_db_mod.get_candle_db = real_get_db
        config.DATA_MODE = real_mode
        handlers.database.get_or_create_user = real_user

    return restore


def test_checker_four_mode_flow():
    db, tmp = _tmp_db()
    # One synthetic day (2026-11-25) for USDARS_otc with the
    # blackout minutes from test 6.
    day = make_day("USDARS_otc", "2026-11-25", seed=11)
    force_candle(day, "09:01", "CALL")
    force_candle(day, "09:02", "PUT")
    force_candle(day, "09:06", "CALL")
    force_candle(day, "09:07", "CALL")
    force_candle(day, "09:08", "PUT")
    db.insert_candles("USDARS_otc", rows_from_raw(to_raw(day)))
    restore = _setup(db)
    try:
        ctx = FakeContext()
        q = FakeQuery("menu_checker")
        _drive(ctx, q, "menu_checker")
        # Four checker modes, all buttons.
        for cb in ("checker_mode_OTC", "checker_mode_REAL",
                   "checker_mode_BLACKOUT", "checker_mode_WHITEOUT"):
            assert _button(q, cb) is not None, q.texts[-1]
        _drive(ctx, q, "checker_mode_BLACKOUT")
        assert "MTG1" in q.texts[-1], q.texts[-1]
        assert _button(q, "checker_mtg_1") is not None, q.texts[-1]
        assert _button(q, "checker_mtg_0") is not None, q.texts[-1]
        _drive(ctx, q, "checker_mtg_1")
        assert "Which date" in q.texts[-1], q.texts[-1]
        upd = _send_text(ctx, "2026-11-25")
        assert "Blackout" in upd.message.texts[-1], upd.message.texts
        assert "USDARS-OTC,09:07 M1" in upd.message.texts[-1], upd.message.texts
        upd2 = _send_text(ctx, "USDARS-OTC,09:02 M1\nUSDARS-OTC,09:07 M1")
        result = upd2.message.working.texts[-1]
        assert "Check Results" in result, result
        assert "DATE: 25.11.2026" in result, result
        # 09:02 is a blackout WIN (T-1 green -> PUT, T red);
        # 09:07 lost M1 but recovered on MTG1 (09:08 red) -> dot.
        win_line = [ln for ln in result.splitlines()
                    if "USDARS-OTC,09:02" in ln][0]
        assert win_line.endswith(pe("check")), win_line
        mtg_line = [ln for ln in result.splitlines()
                    if "USDARS-OTC,09:07" in ln][0]
        assert mtg_line.endswith(pe("check") + "."), mtg_line
        assert "Accuracy: 100.0%" in result, result
    finally:
        restore()
    print("PASS 8) checker 4-mode flow end-to-end — mode picker, MTG, "
          "date, directionless blackout list, MTG1 dot, DD.MM.YYYY date")


def main():
    test_prev_minute_engine()
    test_gap_removed_in_fs_generators()
    test_pct_parsing()
    test_fs_template_and_footer()
    test_checker_template()
    test_checker_blackout_derived()
    test_admin_stats_and_keyboard()
    test_checker_four_mode_flow()
    print("ALL EDIT-ROUND TESTS PASSED (8/8)")


if __name__ == "__main__":
    main()
