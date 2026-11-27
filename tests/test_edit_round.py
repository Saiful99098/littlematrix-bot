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
                         format_check_results, parse_signal_line,
                         split_message)
from bot.collector import rows_from_raw  # noqa: E402
from bot.signal_engine import (cross_asset_filter,  # noqa: E402
                               generate_futures_signals,
                               generate_prev_minute_signals)
from bot.premium_emoji import pe  # noqa: E402
from bot.quotex_client import QuotexRateLimited  # noqa: E402


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
    # Gaps live between the HEADER lines (the owner, 2026-11-27) —
    # the MTG line is followed by a blank line, then 1 MINUTES:.
    assert "MTG 1 STEP\n\n" in head, head
    # One blank line after BLUE FISH PRIME, then packed signals.
    assert "\n\n<code>USDARS-OTC,09:00 M1 PUT</code>" in text, text
    assert "\n<code>USDARS-OTC,09:01 M1 PUT</code>" in text, text
    assert "\n\n<code>USDARS-OTC,09:01" not in text, text
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
    assert "OTC SIGNAL Check Results" in text, text
    # DATE line carries premium emojis around the date (same pair as
    # the FS date line, the owner, 2026-11-27).
    assert f"DATE: {pe('sparkle')}27.11.2026{pe('star')}" in text, text
    # The PUT line is one char shorter than the CALL line, so it is
    # padded with one space inside the <code> tag — that is what
    # lines the stickers up in one column (the owner, 2026-11-27).
    assert "<code>USDPKR-OTC,02:39 M1 PUT </code>" in text, text
    assert "Total: 2" in text, text
    assert "Accuracy: 50.0%" in text, text
    # A blank line sits between the HEADER lines (the owner,
    # 2026-11-27) — the DATE line follows the divider after one.
    assert "\n\nDATE:" in text, text
    # ...and one blank line after BLUE FISH PRIME, then packed signals.
    assert "\n\n<code>USDPKR-OTC,02:39 M1 PUT </code>" in text, text
    assert "\n<code>USDPKR-OTC,02:40 M1 CALL</code>" in text, text
    assert "\n\n<code>USDPKR-OTC,02:40" not in text, text
    # The header names the mode that was checked.
    w_text = format_check_results(results, summary, date_str="2026-11-27",
                                  mode="WHITEOUT")
    assert "WHITEOUT SIGNAL Check Results" in w_text, w_text
    # A very long list is NEVER cut (the owner, 2026-11-27: the full
    # list must be shown) — the whole 100 lines come back, and the
    # caller splits them into Telegram-sized chunks instead.
    many = [{"asset": "USDPKR_otc", "hhmm": f"{h:02d}:{m:02d}",
             "direction": "CALL", "result": "WIN", "actual": "CALL"}
            for h in range(10) for m in range(10)]
    many_sum = {"total": 100, "win": 100, "loss": 0, "doji": 0,
                "no_data": 0, "win_pct": 100.0}
    long_text = format_check_results(many, many_sum, date_str="2026-11-27")
    assert "more lines" not in long_text, long_text
    assert long_text.count("<code>") == 100, long_text.count("<code>")
    assert "Total: 100" in long_text, long_text
    parts = split_message(long_text)
    assert len(parts) > 1, parts
    assert all(len(p) <= 4000 for p in parts), [len(p) for p in parts]
    assert "\n".join(parts) == long_text
    # No header for Backtest Lab.
    plain = format_check_results(results, summary, header=False)
    assert "Check Results" not in plain, plain
    assert "<code>USDPKR-OTC,02:39 M1 PUT </code>" in plain, plain
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
    text = format_check_results(results_m, summary_m, date_str=d,
                                mode="BLACKOUT")
    assert "BLACKOUT SIGNAL Check Results" in text, text
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
        assert any(b.callback_data == "menu_botstatus" for b in admin_btns)
        plain_kb = handlers.main_keyboard(admin=False)
        plain_btns = [b for row in plain_kb.inline_keyboard for b in row]
        assert not any(b.callback_data == "menu_admin" for b in plain_btns)
        assert not any(b.callback_data == "menu_botstatus" for b in plain_btns)
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


class RecordingBot(FakeBot):
    """A bot that records every edit_message_text call (the admin
    dashboard loop edits through the bot, not through a query)."""

    def __init__(self):
        self.edits = []  # (chat_id, message_id, text)
        self.sends = []  # (chat_id, text)
        # reply_markup per edit/send, in the same order — so tests can
        # assert a message carries the Back button (the owner,
        # 2026-11-27) without changing the tuple shapes above.
        self.edit_markups = []
        self.send_markups = []
        self._next_mid = 1000

    async def send_message(self, chat_id=None, text=None,
                           reply_markup=None, parse_mode=None, **kwargs):
        self.sends.append((chat_id, text))
        self.send_markups.append(reply_markup)
        self._next_mid += 1
        return FakeMessage(self._next_mid)

    async def edit_message_text(self, chat_id=None, message_id=None,
                                text=None, reply_markup=None,
                                parse_mode=None, **kwargs):
        self.edits.append((chat_id, message_id, text))
        self.edit_markups.append(reply_markup)
        return None


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
        assert "BLACKOUT SIGNAL Check Results" in result, result
        assert f"DATE: {pe('sparkle')}25.11.2026{pe('star')}" in result, result
        # Header lines have gaps; one blank line after BLUE FISH
        # PRIME, then packed signals.
        assert "\n\nDATE:" in result, result
        assert "\n\n<code>USDARS-OTC,09:02 M1</code>" in result, result
        assert "\n<code>USDARS-OTC,09:07 M1</code>" in result, result
        assert "\n\n<code>USDARS-OTC,09:07" not in result, result
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


# ---------------------------------------------------------------------------
# 9) Admin dashboard refresh stops the moment the owner leaves it
# ---------------------------------------------------------------------------
def test_admin_dashboard_stops_when_leaving():
    # The owner, 2026-11-27: the dashboard refresh kept editing the
    # message back into the dashboard every 10s even after he went
    # back to the main menu. The refresh must run ONLY while he is
    # looking at the dashboard.
    old_interval = handlers._ADMIN_DASH_INTERVAL_SECONDS
    old_stats = handlers.database.get_user_stats
    old_user = handlers.database.get_or_create_user
    old_admins = config.ADMIN_USER_IDS
    try:
        handlers._ADMIN_DASH_INTERVAL_SECONDS = 0.05
        handlers.database.get_user_stats = lambda: {
            "total": 5, "premium": 1, "free": 4, "online": 2}
        handlers.database.get_or_create_user = lambda *a, **k: None
        config.ADMIN_USER_IDS = {999}  # FakeUser id
        bot = RecordingBot()

        def dash_edits():
            return [e for e in bot.edits if "ADMIN DASHBOARD" in (e[2] or "")]

        async def scenario():
            ctx = FakeContext()
            ctx.bot = bot
            q = FakeQuery("menu_admin")
            await handlers.menu_callback(FakeUpdate(q), ctx)
            assert "ADMIN DASHBOARD" in q.texts[-1], q.texts[-1]
            await asyncio.sleep(0.25)
            assert len(dash_edits()) >= 2, bot.edits
            # A stat tap means he is STILL on the dashboard — the
            # loop must keep refreshing.
            q_stat = FakeQuery("admin_stat_noop")
            await handlers.menu_callback(FakeUpdate(q_stat), ctx)
            n_before = len(dash_edits())
            await asyncio.sleep(0.15)
            assert len(dash_edits()) > n_before, bot.edits
            # Leave via ANOTHER tool (not Back): the loop must stop
            # too. The old code only cancelled on Back, so leaving
            # any other way kept flipping the message back to the
            # dashboard every 10 seconds.
            q_fs = FakeQuery("menu_futures")
            await handlers.menu_callback(FakeUpdate(q_fs), ctx)
            n_dash = len(dash_edits())
            await asyncio.sleep(0.3)
            assert len(dash_edits()) == n_dash, bot.edits
            # Re-open, then leave via Back: also stops.
            q2 = FakeQuery("menu_admin")
            await handlers.menu_callback(FakeUpdate(q2), ctx)
            await asyncio.sleep(0.15)
            assert len(dash_edits()) > n_dash, bot.edits
            q_menu = FakeQuery("menu_main")
            await handlers.menu_callback(FakeUpdate(q_menu), ctx)
            assert "Trading Terminal" in q_menu.texts[-1], q_menu.texts[-1]
            n_dash2 = len(dash_edits())
            await asyncio.sleep(0.3)
            assert len(dash_edits()) == n_dash2, bot.edits

        asyncio.run(scenario())
    finally:
        handlers._ADMIN_DASH_INTERVAL_SECONDS = old_interval
        handlers.database.get_user_stats = old_stats
        handlers.database.get_or_create_user = old_user
        config.ADMIN_USER_IDS = old_admins
        handlers._ADMIN_DASH.clear()
    print("PASS 9) admin dashboard refresh stops when leaving it "
          "(stat taps keep it alive)")


# ---------------------------------------------------------------------------
# 10) Admin Fill Gaps button (the owner, 2026-11-27: the gap-fill only
# ran from his PC — he asked for it on Render/Telegram instead)
# ---------------------------------------------------------------------------
def _seed_day_with_hole(db, asset, date_str, hole_start_min, hole_end_min,
                        base=1.08):
    """One full Dhaka day of candles for `asset` except the minutes
    [hole_start_min, hole_end_min). (Same shape as the collector-db
    gap tests: USDPHP 02:41-08:32 is minutes 161-512.)"""
    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=DHAKA)
    rows = []
    for minute in range(1440):
        if hole_start_min <= minute < hole_end_min:
            continue
        ts = day + timedelta(minutes=minute)
        o = base + (minute % 10) * 0.001
        c = o + 0.001 if minute % 2 == 0 else o - 0.001
        rows.append({"ts": ts, "open": o, "high": max(o, c) + 0.001,
                     "low": min(o, c) - 0.001, "close": c})
    return db.insert_candles(asset, rows, on_conflict="nothing")


class FakeGapSession:
    """Fake Quotex session for the gap-fill job: deterministic
    1-min candles (the same minute always gets the same candle, like
    the real server), so the job can be tested offline."""

    def __init__(self, now=None, raise_429=False):
        self.now = now or datetime.now(DHAKA)
        self.raise_429 = raise_429
        self.connects = 0
        self.connected = False

    async def connect(self):
        self.connects += 1
        self.connected = True
        return True

    async def fetch_asset_span(self, asset, seconds_back):
        if self.raise_429:
            raise QuotexRateLimited("Quotex temporarily blocked us (429).")
        now = self.now
        start = now - timedelta(seconds=int(seconds_back))
        out = []
        t = start.replace(second=0, microsecond=0)
        while t <= now:
            base = 1.08
            i = int(t.timestamp()) // 60
            o = base + (i % 10) * 0.001
            c = o + 0.001 if i % 2 == 0 else o - 0.001
            out.append({"time": int(t.timestamp()), "open": o,
                        "high": max(o, c) + 0.001, "low": min(o, c) - 0.001,
                        "close": c})
            t += timedelta(minutes=1)
        return out

    async def close(self):
        self.connected = False


def test_admin_fill_gaps_job():
    # a) The dashboard keyboard carries the Fill Gaps button.
    stats = {"total": 5, "premium": 1, "free": 4, "online": 2}
    kb = handlers._admin_dashboard_keyboard(stats)
    callbacks = [b.callback_data for row in kb.inline_keyboard
                 for b in row]
    assert "admin_fill_gaps" in callbacks, callbacks
    assert any(b.text == "Fill Gaps" for row in kb.inline_keyboard
               for b in row), callbacks
    # b) The job fills a real hole and reports the counts.
    db, tmp = _tmp_db()
    date = "2026-11-27"
    now = datetime(2026, 11, 27, 19, 30, tzinfo=DHAKA)
    _seed_day_with_hole(db, "USDPHP_otc", date, 161, 513)
    assert db.count_candles("USDPHP_otc", date) == 1088
    session = FakeGapSession(now=now)
    bot = RecordingBot()
    result = asyncio.run(handlers._gap_fill_job(
        bot, 999, db=db, session_factory=lambda: session, now=now))
    assert result == (1, 1, 352), result
    assert db.count_candles("USDPHP_otc", date) == 1440
    assert session.connects == 1, session.connects
    assert any("Gap-fill started" in (t or "") for _, t in bot.sends), bot.sends
    assert any("1 asset(s), 1 gap(s), 352 new candles" in (t or "")
               for _, _, t in bot.edits), bot.edits
    # The result message carries a Back button (the owner, 2026-11-27)
    # — tapping it turns the message back into the main menu.
    assert bot.edit_markups[-1] is not None, bot.edit_markups
    assert [b.callback_data
            for row in bot.edit_markups[-1].inline_keyboard
            for b in row] == ["menu_main"], bot.edit_markups[-1].inline_keyboard
    # c) No gaps -> no Quotex login at all.
    db2, tmp2 = _tmp_db()
    _seed_day_with_hole(db2, "EURUSD_otc", date, 1440, 1440)  # full day
    session2 = FakeGapSession(now=now)
    bot2 = RecordingBot()
    result2 = asyncio.run(handlers._gap_fill_job(
        bot2, 999, db=db2, session_factory=lambda: session2, now=now))
    assert result2 == (0, 0, 0), result2
    assert session2.connects == 0, session2.connects
    assert any("no gaps found" in (t or "") for _, _, t in bot2.edits), bot2.edits
    # The "no gaps" result carries the Back button too.
    assert bot2.edit_markups[-1] is not None, bot2.edit_markups
    assert [b.callback_data
            for row in bot2.edit_markups[-1].inline_keyboard
            for b in row] == ["menu_main"], bot2.edit_markups[-1].inline_keyboard
    # d) A 429 on the first pass waits and retries once.
    db3, tmp3 = _tmp_db()
    _seed_day_with_hole(db3, "USDPHP_otc", date, 161, 513)
    session3 = FakeGapSession(now=now, raise_429=True)
    bot3 = RecordingBot()
    sleeps = []

    async def no_sleep(seconds):
        sleeps.append(seconds)

    async def scenario429():
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            if calls["n"] == 1:
                return session3
            return FakeGapSession(now=now)

        return await handlers._gap_fill_job(
            bot3, 999, db=db3, session_factory=factory, sleep=no_sleep,
            now=now)

    result3 = asyncio.run(scenario429())
    assert result3 == (1, 1, 352), result3
    assert sleeps == [config.COLLECTOR_429_RETRY_MINUTES * 60], sleeps
    assert db3.count_candles("USDPHP_otc", date) == 1440
    print("PASS 10a) admin Fill Gaps job fills the hole, skips login "
          "when complete, retries once on 429")


def test_admin_fill_gaps_button_wiring():
    old_stats = handlers.database.get_user_stats
    old_user = handlers.database.get_or_create_user
    old_admins = config.ADMIN_USER_IDS
    old_job = handlers._gap_fill_job
    try:
        handlers.database.get_user_stats = lambda: {
            "total": 5, "premium": 1, "free": 4, "online": 2}
        handlers.database.get_or_create_user = lambda *a, **k: None
        config.ADMIN_USER_IDS = {999}
        calls = []

        async def fake_job(bot, chat_id, **kwargs):
            calls.append((bot, chat_id))
            return (0, 0, 0)

        handlers._gap_fill_job = fake_job

        async def scenario():
            ctx = FakeContext()
            bot = RecordingBot()
            ctx.bot = bot
            # A non-admin is refused (the button is hidden from
            # everyone else, but a crafted callback must not work).
            config.ADMIN_USER_IDS = {123}
            q_denied = FakeQuery("admin_fill_gaps")
            await handlers.menu_callback(FakeUpdate(q_denied), ctx)
            assert "Only the owner can run a gap-fill." in q_denied.texts[-1]
            config.ADMIN_USER_IDS = {999}
            # First tap starts the job; the guard is popped when the
            # task finishes. (create_task only schedules the job, so
            # the call lands after a loop tick, not inside the tap.)
            q = FakeQuery("admin_fill_gaps")
            await handlers.menu_callback(FakeUpdate(q), ctx)
            assert 999 in handlers._FILL_GAPS_TASK
            await asyncio.sleep(0.05)
            assert len(calls) == 1, calls
            assert calls[0][1] == 999
            assert 999 not in handlers._FILL_GAPS_TASK
            # A second tap while one is running is refused.
            handlers._FILL_GAPS_TASK[999] = {"task": object()}
            q2 = FakeQuery("admin_fill_gaps")
            await handlers.menu_callback(FakeUpdate(q2), ctx)
            assert len(calls) == 1, calls
            assert any("already running" in (t or "")
                       for _, t in bot.sends), bot.sends

        asyncio.run(scenario())
    finally:
        handlers.database.get_user_stats = old_stats
        handlers.database.get_or_create_user = old_user
        config.ADMIN_USER_IDS = old_admins
        handlers._gap_fill_job = old_job
        handlers._FILL_GAPS_TASK.clear()
        handlers._ADMIN_DASH.clear()
    print("PASS 10b) admin Fill Gaps button: owner-only, one run at a time")


# ---------------------------------------------------------------------------
# 11) Checker stickers aligned + channel posts ignored (the owner,
# 2026-11-27: stickers after the monospace text sat ragged, and every
# channel post crashed text_message with AttributeError)
# ---------------------------------------------------------------------------
def test_checker_stickers_aligned():
    # Lines of different lengths put the stickers at different
    # columns. Every sticker must start at the same column — one
    # straight column, one below the other.
    results = [
        {"asset": "USDZD_otc", "hhmm": "00:19", "direction": "PUT",
         "result": "LOSS", "mtg1": None},
        {"asset": "CADCHF_otc", "hhmm": "00:32", "direction": "CALL",
         "result": "WIN", "mtg1": None},
        {"asset": "XRPUSD_otc", "hhmm": "00:48", "direction": "CALL",
         "result": "WIN", "mtg1": None},
        {"asset": "AUDNZD_otc", "hhmm": "01:52", "direction": "CALL",
         "result": "WIN", "mtg1": None},
        {"asset": "BRLUSD_otc", "hhmm": "02:30", "direction": "PUT",
         "result": "WIN", "mtg1": "WIN"},  # MTG1 win -> check + dot
        {"asset": "USDPKR_otc", "hhmm": "02:35", "direction": "CALL",
         "result": "NO DATA", "mtg1": None},
    ]
    summary = {"total": 6, "win": 3, "loss": 1, "doji": 0, "no_data": 1,
               "win_pct": 75.0}
    text = format_check_results(results, summary, date_str="2026-11-27",
                                mode="OTC")
    lines = [ln for ln in text.split("\n") if ln.startswith("<code>")]
    assert len(lines) == 6, lines
    # Every <code>...</code> holds the same number of characters.
    insides = [ln.split("</code>")[0][len("<code>"):] for ln in lines]
    assert len({len(s) for s in insides}) == 1, insides
    # ...so every sticker starts at the same column.
    starts = [ln.index("</code>") for ln in lines]
    assert len(set(starts)) == 1, starts
    # The MTG1 dot is still there, after its sticker.
    assert any(ln.endswith(pe("check") + ".") for ln in lines), lines
    # The shortest line was padded (USDZD-OTC is shorter than the rest).
    assert insides[0].endswith(" "), insides
    print("PASS 11a) checker stickers aligned — one straight column")


class FakeChannelPost:
    def __init__(self, text="signal post"):
        self.text = text


class FakeChannelUpdate:
    """A channel post: no sender user, no update.message — exactly
    what the bot receives for every post in its channel."""

    def __init__(self):
        self.message = None
        self.channel_post = FakeChannelPost()
        self.effective_user = None


def test_channel_post_ignored():
    old_user = handlers.database.get_or_create_user
    try:
        handlers.database.get_or_create_user = lambda *a, **k: None
        # A channel post must be ignored silently — before this fix
        # it crashed with AttributeError ('NoneType' has no 'get')
        # on context.user_data, once per post.
        bot = RecordingBot()
        ctx = FakeContext()
        ctx.bot = bot
        asyncio.run(handlers.text_message(FakeChannelUpdate(), ctx))
        assert bot.sends == [] and bot.edits == [], (bot.sends, bot.edits)
        # A normal user message still works (no flow -> menu hint).
        upd = FakeTextUpdate("hello")
        asyncio.run(handlers.text_message(upd, ctx))
        assert any("Tap a button in the menu" in t
                   for t in upd.message.texts), upd.message.texts
    finally:
        handlers.database.get_or_create_user = old_user
    print("PASS 11b) channel post ignored (no crash), normal text still works")


# ---------------------------------------------------------------------------
# 12) Long checker lists are split, never cut (the owner, 2026-11-27:
# a 42-signal list came back as "… +5 more lines" — the full list
# must be shown, as several Telegram messages when needed)
# ---------------------------------------------------------------------------
def test_split_message():
    # Short text comes back untouched, one part.
    assert split_message("hello") == ["hello"]
    assert split_message("a" * 4000) == ["a" * 4000]
    # Splits at line boundaries; no line is ever broken, and the
    # parts rejoin to the original text exactly.
    text = "\n".join(f"line {i:03d}" for i in range(200))
    parts = split_message(text, limit=100)
    assert len(parts) > 1, parts
    assert all(len(p) <= 100 for p in parts), [len(p) for p in parts]
    assert "\n".join(parts) == text
    for p in parts:
        for ln in p.split("\n"):
            assert ln.startswith("line "), ln
    # A single over-long line is hard-split, never dropped.
    long_line = "x" * 250
    parts2 = split_message(long_line, limit=100)
    assert all(len(p) <= 100 for p in parts2), [len(p) for p in parts2]
    assert "".join(parts2) == long_line
    # A 42-signal checker result (the owner's screenshot): all 42
    # lines survive across the parts, no "more lines" note.
    results = [{"asset": "USDPKR_otc", "hhmm": f"{h:02d}:{m:02d}",
                "direction": "CALL" if (h + m) % 2 == 0 else "PUT",
                "result": "WIN" if (h + m) % 3 else "LOSS", "mtg1": None}
               for h in range(7) for m in range(6)]
    summary = {"total": 42, "win": 24, "loss": 6, "doji": 0, "no_data": 12,
               "win_pct": 80.0}
    full = format_check_results(results, summary, date_str="2026-11-27",
                                mode="OTC")
    assert "more lines" not in full, full
    assert full.count("<code>") == 42, full.count("<code>")
    parts3 = split_message(full)
    assert all(len(p) <= 4000 for p in parts3), [len(p) for p in parts3]
    assert "\n".join(parts3) == full
    assert sum(p.count("<code>") for p in parts3) == 42
    print("PASS 12) long lists split into Telegram-sized parts, full "
          "list kept")


def test_checker_long_list_sent_in_parts():
    # End-to-end: a 42-line pasted list goes through text_message and
    # comes out as several Telegram messages — the working message
    # holds part 1, the rest are replies — with all 42 lines present
    # and the Back button on the last part only.
    db, tmp = _tmp_db()
    day = make_day("USDPKR_otc", "2026-11-25", seed=7)
    db.insert_candles("USDPKR_otc", rows_from_raw(to_raw(day)))
    restore = _setup(db)
    try:
        ctx = FakeContext()
        ctx.user_data.update({"flow": "checker_list",
                              "checker_date": "2026-11-25",
                              "checker_mtg1": False,
                              "checker_mode": "OTC"})
        lines = [f"USDPKR-OTC,{h:02d}:{m:02d} M1 CALL"
                 for h in range(7) for m in range(0, 60, 10)]
        assert len(lines) == 42, len(lines)
        upd = _send_text(ctx, "\n".join(lines))
        sent = upd.message.working.texts + upd.message.texts[1:]
        joined = "\n".join(sent)
        assert "more lines" not in joined, joined
        assert joined.count("<code>") == 42, joined.count("<code>")
        assert len(sent) > 1, sent  # it did not fit in one message
        # Back button on the last part only, not the first.
        assert upd.message.markups[-1] is not None, upd.message.markups
        assert upd.message.working.markups[-1] is None, \
            upd.message.working.markups
    finally:
        restore()
    print("PASS 13) checker long list sent in parts — all 42 lines, "
          "Back on the last part")


# ---------------------------------------------------------------------------
# 14) Gap-fill result Back button (the owner, 2026-11-27: the result
# text sat below the menu forever — it now carries a Back button,
# and tapping it turns that message back into the main menu)
# ---------------------------------------------------------------------------
def test_gap_fill_result_back_button():
    old_admins = config.ADMIN_USER_IDS
    old_user = handlers.database.get_or_create_user
    try:
        config.ADMIN_USER_IDS = {999}
        handlers.database.get_or_create_user = lambda *a, **k: None
        ctx = FakeContext()
        q = FakeQuery("menu_main")
        _drive(ctx, q, "menu_main")
        assert "BLUE FISH PRIME" in q.texts[-1], q.texts[-1]
        assert "Pick a tool below" in q.texts[-1], q.texts[-1]
        cbs = [b.callback_data for row in q.markups[-1].inline_keyboard
               for b in row]
        assert "menu_checker" in cbs, cbs
        assert "menu_admin" in cbs, cbs
    finally:
        config.ADMIN_USER_IDS = old_admins
        handlers.database.get_or_create_user = old_user
    print("PASS 14) gap-fill result Back button -> main menu")


# ---------------------------------------------------------------------------
# 15) Owner menu hides the user-only buttons (the owner, 2026-11-27:
# Contact Owner, Referral and Premium are for the users, not for
# him — his menu must not show them)
# ---------------------------------------------------------------------------
def test_owner_menu_hides_user_only_buttons():
    user_kb = handlers.main_keyboard(admin=False)
    user_cbs = [b.callback_data for row in user_kb.inline_keyboard
                for b in row]
    user_urls = [b.url for row in user_kb.inline_keyboard
                 for b in row if getattr(b, "url", None)]
    assert "menu_referral" in user_cbs, user_cbs
    assert "menu_premium" in user_cbs, user_cbs
    assert "https://t.me/LittleMatrix" in user_urls, user_urls
    owner_kb = handlers.main_keyboard(admin=True)
    owner_cbs = [b.callback_data for row in owner_kb.inline_keyboard
                 for b in row]
    owner_urls = [b.url for row in owner_kb.inline_keyboard
                  for b in row if getattr(b, "url", None)]
    assert "menu_referral" not in owner_cbs, owner_cbs
    assert "menu_premium" not in owner_cbs, owner_cbs
    assert "https://t.me/LittleMatrix" not in owner_urls, owner_urls
    # The owner still gets his own rows, and the rest of the menu.
    assert "menu_admin" in owner_cbs, owner_cbs
    assert "menu_botstatus" in owner_cbs, owner_cbs
    assert "menu_checker" in owner_cbs, owner_cbs
    print("PASS 15) owner menu hides Contact Owner / Referral / Premium")


def main():
    test_prev_minute_engine()
    test_gap_removed_in_fs_generators()
    test_pct_parsing()
    test_fs_template_and_footer()
    test_checker_template()
    test_checker_blackout_derived()
    test_admin_stats_and_keyboard()
    test_checker_four_mode_flow()
    test_admin_dashboard_stops_when_leaving()
    test_admin_fill_gaps_job()
    test_admin_fill_gaps_button_wiring()
    test_checker_stickers_aligned()
    test_channel_post_ignored()
    test_split_message()
    test_checker_long_list_sent_in_parts()
    test_gap_fill_result_back_button()
    test_owner_menu_hides_user_only_buttons()
    print("ALL EDIT-ROUND TESTS PASSED (15/15)")


if __name__ == "__main__":
    main()
