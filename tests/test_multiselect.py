"""Multi-select asset pickers + the time mechanism (Generated + Refresh).

Run: python tests/test_multiselect.py

a) Analysis pickers (AI Signal / Candle Scan / Trend Scan / Market Live):
   tap an asset to toggle it (premium check appears on selected
   buttons), SELECT ALL toggles everything, Next runs the tool for
   the selection — 1 asset, 10 assets, or all of them.
b) Futures picker: the same toggle + SELECT ALL + Next flow; a list
   of assets goes through the cross-asset filter like SELECT ALL.
c) Time mechanism: every result carries "Generated: HH:MM:SS
   (Dhaka)" and a Refresh button that re-runs exactly the result
   shown in that message (stored under its message id).
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests.synthetic_data import force_candle, make_day, to_raw  # noqa: E402

from bot import config  # noqa: E402
from bot.candles_db import CandleDB, DHAKA  # noqa: E402
from bot.collector import rows_from_raw  # noqa: E402

CHECK_ICON = "6267008582294705964"  # premium check (selected button)
ASSET_ICON = "5231200819986047254"  # chart (unselected button)


def _tmp_db():
    tmp = tempfile.TemporaryDirectory()
    db = CandleDB(database_url="", sqlite_path=Path(tmp.name) / "candles.db").init()
    db._tmpdir = tmp  # keep the temp dir alive with the db
    return db, tmp


def _load_today(db, asset, n=60):
    """n 1-min candles today (09:00-09:59 Dhaka) for one asset."""
    import bot.handlers as handlers
    today = handlers._today_date_str()
    d = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=DHAKA)
    rows = []
    price = 100.0
    for i in range(n):
        price += 0.1 if i % 2 == 0 else -0.05
        rows.append({"ts": d + timedelta(hours=9, minutes=i),
                     "open": price - 0.05, "high": price + 0.1,
                     "low": price - 0.1, "close": price})
    return db.insert_candles(asset, rows)


def _load_two_assets_two_days(db):
    """Two synthetic full days (2026-11-20/21) for three OTC assets,
    so futures has completed days to vote from — and selecting 2 of
    3 is a real subset (not SELECT ALL)."""
    for asset, seed in (("BTCUSD_otc", 7), ("ETHUSD_otc", 17),
                        ("USDMXN_otc", 27)):
        d20 = make_day(asset, "2026-11-20", seed=seed)
        d21 = make_day(asset, "2026-11-21", seed=seed + 1)
        force_candle(d21, "09:02", "CALL")
        rows = rows_from_raw(to_raw(d20) + to_raw(d21))
        db.insert_candles(asset, rows)


class FakeMessage:
    def __init__(self, message_id=123):
        self.message_id = message_id


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


def _drive(ctx, query, data):
    """Send one callback tap through menu_callback on the same
    message (same message_id) and return the query."""
    import bot.handlers as handlers
    query.data = data
    asyncio.run(handlers.menu_callback(FakeUpdate(query), ctx))
    return query


def _last_buttons(query):
    kb = query.markups[-1]
    return [b for row in kb.inline_keyboard for b in row]


def _button(query, callback_data):
    for b in _last_buttons(query):
        if b.callback_data == callback_data:
            return b
    return None


def _setup(db):
    """Patch handlers to use the temp DB in db mode; returns a
    restore function. Also neuters the real user DB write."""
    import bot.candles_db as candles_db_mod
    import bot.handlers as handlers
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


def test_analysis_multiselect_toggle_and_next():
    db, tmp = _tmp_db()
    _load_today(db, "BTCUSD_otc")
    _load_today(db, "ETHUSD_otc")
    restore = _setup(db)
    try:
        ctx = FakeContext()
        q = FakeQuery("menu_ai")
        _drive(ctx, q, "menu_ai")
        assert "Selected: 0" in q.texts[-1], q.texts[-1]
        assert _button(q, "an_next_ai") is not None, q.texts[-1]
        assert _button(q, "an_all_ai") is not None, q.texts[-1]
        # Toggle ETHUSD on: check appears, count goes to 1.
        _drive(ctx, q, "an_asset_ai_ETHUSD_otc")
        assert "Selected: 1" in q.texts[-1], q.texts[-1]
        assert _button(q, "an_asset_ai_ETHUSD_otc").icon_custom_emoji_id == CHECK_ICON
        assert _button(q, "an_asset_ai_BTCUSD_otc").icon_custom_emoji_id == ASSET_ICON
        # Toggle BTCUSD on: 2 selected.
        _drive(ctx, q, "an_asset_ai_BTCUSD_otc")
        assert "Selected: 2" in q.texts[-1], q.texts[-1]
        assert _button(q, "an_asset_ai_BTCUSD_otc").icon_custom_emoji_id == CHECK_ICON
        # Toggle ETHUSD off again: back to 1, icon back to chart.
        _drive(ctx, q, "an_asset_ai_ETHUSD_otc")
        assert "Selected: 1" in q.texts[-1], q.texts[-1]
        assert _button(q, "an_asset_ai_ETHUSD_otc").icon_custom_emoji_id == ASSET_ICON
        # Back on, then Next: one AI Signal message with BOTH markets,
        # a build time, and a Refresh button.
        _drive(ctx, q, "an_asset_ai_ETHUSD_otc")
        _drive(ctx, q, "an_next_ai")
        result = q.texts[-1]
        assert "AI SIGNAL" in result, result
        assert "BTCUSD-OTC" in result, result
        assert "ETHUSD-OTC" in result, result
        assert "Generated:" in result, result
        assert _button(q, "refresh") is not None, result
    finally:
        restore()
    print("PASS a) analysis picker: toggle + check icons + Next runs both markets")


def test_analysis_next_empty_warns():
    db, tmp = _tmp_db()
    _load_today(db, "BTCUSD_otc")
    restore = _setup(db)
    try:
        ctx = FakeContext()
        q = FakeQuery("menu_ai")
        _drive(ctx, q, "menu_ai")
        _drive(ctx, q, "an_next_ai")
        assert "Select at least 1 market first" in q.texts[-1], q.texts[-1]
        # The picker is still there — the user can pick and try again.
        assert _button(q, "an_asset_ai_BTCUSD_otc") is not None, q.texts[-1]
    finally:
        restore()
    print("PASS b) Next with nothing selected warns and keeps the picker")


def test_futures_multiselect():
    db, tmp = _tmp_db()
    _load_two_assets_two_days(db)
    restore = _setup(db)
    try:
        ctx = FakeContext()
        q = FakeQuery("menu_futures")
        _drive(ctx, q, "menu_futures")
        _drive(ctx, q, "fs_mode_OTC FS")
        assert "Selected: 0" in q.texts[-1], q.texts[-1]
        _drive(ctx, q, "fs_asset_BTCUSD_otc")
        assert "Selected: 1" in q.texts[-1], q.texts[-1]
        assert _button(q, "fs_asset_BTCUSD_otc").icon_custom_emoji_id == CHECK_ICON
        _drive(ctx, q, "fs_asset_ETHUSD_otc")
        assert "Selected: 2" in q.texts[-1], q.texts[-1]
        # Next -> days -> time -> result for the 2 selected assets.
        _drive(ctx, q, "fs_next")
        assert "How many days of history" in q.texts[-1], q.texts[-1]
        _drive(ctx, q, "fs_days_1")
        assert "Choose a time window" in q.texts[-1], q.texts[-1]
        _drive(ctx, q, "fs_time_09:00-10:59")
        result = q.texts[-1]
        assert "FUTURE SIGNALS" in result, result
        assert "2 selected" in result, result
        assert "Generated:" in result, result
        assert _button(q, "refresh") is not None, result
        sig_lines = [ln for ln in result.splitlines()
                     if ln.startswith(("BTCUSD-OTC,", "ETHUSD-OTC,"))]
        assert sig_lines, result
    finally:
        restore()
    print("PASS c) futures picker: toggle + Next -> 2 selected, signals + Generated")


def test_futures_list_matches_single_asset():
    """A 1-element list goes through the same cross-asset path as
    SELECT ALL and gives the same signal lines as the single asset."""
    import bot.handlers as handlers

    db, tmp = _tmp_db()
    _load_two_assets_two_days(db)
    restore = _setup(db)
    try:
        class Ctx:
            user_data = {"fs_asset": "BTCUSD_otc", "fs_days": 1,
                         "fs_mode": "OTC FS"}

        single = asyncio.run(handlers._compute_futures_text(Ctx(), "09:00", "10:59"))
        single_lines = sorted(ln for ln in single.splitlines()
                              if ln.startswith("BTCUSD-OTC,"))
        assert single_lines, single
        Ctx.user_data = {"fs_asset": ["BTCUSD_otc"], "fs_days": 1,
                         "fs_mode": "OTC FS"}
        listed = asyncio.run(handlers._compute_futures_text(Ctx(), "09:00", "10:59"))
        # A 1-element list behaves like the single pick: same header,
        # same (unfiltered) signal lines.
        assert "Asset: BTCUSD-OTC" in listed, listed
        listed_lines = sorted(ln for ln in listed.splitlines()
                              if ln.startswith("BTCUSD-OTC,"))
        assert listed_lines == single_lines, (listed_lines, single_lines)
    finally:
        restore()
    print("PASS d) futures list of 1 == single asset signals")


def test_futures_select_all_toggle():
    db, tmp = _tmp_db()
    _load_two_assets_two_days(db)
    restore = _setup(db)
    try:
        ctx = FakeContext()
        q = FakeQuery("menu_futures")
        _drive(ctx, q, "menu_futures")
        _drive(ctx, q, "fs_mode_OTC FS")
        _drive(ctx, q, "fs_asset_ALL")
        assert "Selected: 3" in q.texts[-1], q.texts[-1]
        # Tap SELECT ALL again: everything deselected.
        _drive(ctx, q, "fs_asset_ALL")
        assert "Selected: 0" in q.texts[-1], q.texts[-1]
        # Select all, then Next: the flow stores "ALL".
        _drive(ctx, q, "fs_asset_ALL")
        _drive(ctx, q, "fs_next")
        assert ctx.user_data["fs_asset"] == "ALL", ctx.user_data
        assert "How many days of history" in q.texts[-1], q.texts[-1]
    finally:
        restore()
    print("PASS e) futures SELECT ALL toggles on/off, Next stores ALL")


def test_refresh_reruns_same_result():
    db, tmp = _tmp_db()
    _load_today(db, "BTCUSD_otc")
    _load_today(db, "ETHUSD_otc")
    restore = _setup(db)
    try:
        ctx = FakeContext()
        q = FakeQuery("menu_ai")
        _drive(ctx, q, "menu_ai")
        _drive(ctx, q, "an_asset_ai_ETHUSD_otc")
        _drive(ctx, q, "an_next_ai")
        n_before = len(q.texts)
        assert "AI SIGNAL" in q.texts[-1], q.texts[-1]
        # Refresh on the SAME message re-runs the same result.
        _drive(ctx, q, "refresh")
        assert len(q.texts) > n_before, q.texts
        assert "AI SIGNAL" in q.texts[-1], q.texts[-1]
        assert "ETHUSD-OTC" in q.texts[-1], q.texts[-1]
        assert "Generated:" in q.texts[-1], q.texts[-1]
        # Refresh on an unknown message id: honest "too old" message.
        q2 = FakeQuery("refresh", message_id=999)
        _drive(ctx, q2, "refresh")
        assert "too old to refresh" in q2.texts[-1], q2.texts[-1]
    finally:
        restore()
    print("PASS f) Refresh re-runs the same result; unknown message says so")


def test_botstatus_refresh():
    db, tmp = _tmp_db()
    _load_today(db, "BTCUSD_otc")
    restore = _setup(db)
    try:
        ctx = FakeContext()
        q = FakeQuery("menu_botstatus")
        _drive(ctx, q, "menu_botstatus")
        assert "BOT STATUS" in q.texts[-1], q.texts[-1]
        assert _button(q, "refresh") is not None, q.texts[-1]
        n_before = len(q.texts)
        _drive(ctx, q, "refresh")
        assert len(q.texts) > n_before, q.texts
        assert "BOT STATUS" in q.texts[-1], q.texts[-1]
    finally:
        restore()
    print("PASS g) Bot Status has Refresh and re-reads the database")


if __name__ == "__main__":
    test_analysis_multiselect_toggle_and_next()
    test_analysis_next_empty_warns()
    test_futures_multiselect()
    test_futures_list_matches_single_asset()
    test_futures_select_all_toggle()
    test_refresh_reruns_same_result()
    test_botstatus_refresh()
    print("ALL MULTI-SELECT TESTS PASSED")
