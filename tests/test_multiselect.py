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
   shown in that message (stored under its message id) — except
   FUTURE SIGNALS, whose template (the owner, 2026-11-27) has no
   Generated line; its Refresh button stays.
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


class FakeChat:
    id = 999


class FakeUpdate:
    def __init__(self, query):
        self.callback_query = query
        self.effective_user = FakeUser()
        self.effective_chat = FakeChat()


class FakeMember:
    status = "member"


class FakeBot:
    def __init__(self):
        self.photos = []  # (chat_id, png_bytes, caption)
        self.messages = []  # (chat_id, text)

    async def get_chat_member(self, chat_id, user_id):
        return FakeMember()

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        self.photos.append((chat_id, photo, caption))
        return None

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))
        return None


class FakeContext:
    def __init__(self):
        self.user_data = {}
        self.bot = FakeBot()


class FakeWorkingMessage:
    """The 'Working...' message text_message edits in place."""
    def __init__(self, message_id=456):
        self.message_id = message_id
        self.texts = []
        self.markups = []

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        self.texts.append(text)
        self.markups.append(reply_markup)
        return None


class FakeTextMessage:
    def __init__(self, text):
        self.text = text
        self.texts = []  # reply_text calls on this message
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
    """Send one callback tap through menu_callback on the same
    message (same message_id) and return the query."""
    import bot.handlers as handlers
    query.data = data
    asyncio.run(handlers.menu_callback(FakeUpdate(query), ctx))
    return query


def _send_text(ctx, text):
    """Send one typed message through text_message and return the
    (fake) update, so the reply / edited texts can be asserted."""
    import bot.handlers as handlers
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
        # The multi-select picker still serves Candle Scan / Trend
        # Scan / Market Live (AI Signal takes a typed asset now, the
        # owner, 2026-11-28).
        q = FakeQuery("menu_counter")
        _drive(ctx, q, "menu_counter")
        assert "Selected: 0" in q.texts[-1], q.texts[-1]
        assert _button(q, "an_next_counter") is not None, q.texts[-1]
        assert _button(q, "an_all_counter") is not None, q.texts[-1]
        # Toggle ETHUSD on: check appears, count goes to 1.
        _drive(ctx, q, "an_asset_counter_ETHUSD_otc")
        assert "Selected: 1" in q.texts[-1], q.texts[-1]
        assert _button(q, "an_asset_counter_ETHUSD_otc").icon_custom_emoji_id == CHECK_ICON
        assert _button(q, "an_asset_counter_BTCUSD_otc").icon_custom_emoji_id == ASSET_ICON
        # Toggle BTCUSD on: 2 selected.
        _drive(ctx, q, "an_asset_counter_BTCUSD_otc")
        assert "Selected: 2" in q.texts[-1], q.texts[-1]
        assert _button(q, "an_asset_counter_BTCUSD_otc").icon_custom_emoji_id == CHECK_ICON
        # Toggle ETHUSD off again: back to 1, icon back to chart.
        _drive(ctx, q, "an_asset_counter_ETHUSD_otc")
        assert "Selected: 1" in q.texts[-1], q.texts[-1]
        assert _button(q, "an_asset_counter_ETHUSD_otc").icon_custom_emoji_id == ASSET_ICON
        # Back on, then Next: one Candle Scan message with BOTH
        # markets, a build time, and a Refresh button.
        _drive(ctx, q, "an_asset_counter_ETHUSD_otc")
        _drive(ctx, q, "an_next_counter")
        result = q.texts[-1]
        assert "CANDLE SCAN" in result, result
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
        q = FakeQuery("menu_counter")
        _drive(ctx, q, "menu_counter")
        _drive(ctx, q, "an_next_counter")
        assert "Select at least 1 market first" in q.texts[-1], q.texts[-1]
        # The picker is still there — the user can pick and try again.
        assert _button(q, "an_asset_counter_BTCUSD_otc") is not None, q.texts[-1]
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
        # Next -> days -> % (typed) -> time -> result for the 2 assets.
        _drive(ctx, q, "fs_next")
        assert "How many days of history" in q.texts[-1], q.texts[-1]
        _drive(ctx, q, "fs_days_1")
        assert "Win rate filter" in q.texts[-1], q.texts[-1]
        upd = _send_text(ctx, "80")
        assert "Choose a time window" in upd.message.texts[-1], upd.message.texts
        _drive(ctx, q, "fs_time_09:00-10:59")
        result = q.texts[-1]
        assert "FUTURE SIGNALS" in result, result
        assert "FUTURE SIGNALS — OTC FS" in result, result
        assert "MTG 1 STEP" in result, result
        # The owner's template: no Assets/Dates/Days line, no
        # Time/Source line, no Signals-count line, no disclaimer,
        # no Generated line — and the Refresh button stays.
        assert "Generated:" not in result, result
        assert "2 selected" not in result, result
        assert "Assets:" not in result, result
        assert "No signal guarantees profit" not in result, result
        assert _button(q, "refresh") is not None, result
        sig_lines = [ln for ln in result.splitlines()
                     if "<code>BTCUSD-OTC," in ln or "<code>ETHUSD-OTC," in ln]
        assert sig_lines, result
    finally:
        restore()
    print("PASS c) futures picker: toggle + Next -> days -> typed % -> "
          "time -> template result (no Generated/Assets lines), Refresh stays")


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
                         "fs_mode": "OTC FS", "fs_pct": 70}

        single = asyncio.run(handlers._compute_futures_text(Ctx(), "09:00", "10:59"))
        single_lines = sorted(ln for ln in single.splitlines()
                              if "<code>BTCUSD-OTC," in ln)
        assert single_lines, single
        Ctx.user_data = {"fs_asset": ["BTCUSD_otc"], "fs_days": 1,
                         "fs_mode": "OTC FS", "fs_pct": 70}
        listed = asyncio.run(handlers._compute_futures_text(Ctx(), "09:00", "10:59"))
        # A 1-element list behaves like the single pick: same template
        # header, same (unfiltered) signal lines.
        assert "FUTURE SIGNALS — OTC FS" in listed, listed
        listed_lines = sorted(ln for ln in listed.splitlines()
                              if "<code>BTCUSD-OTC," in ln)
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


def _load_ai_bullish(db, asset):
    """Seed one asset so all four AI Signal checks agree on CALL:
    today gets 60 rising candles (up 0.10 twice, down 0.10 once per
    3 minutes — EMA up, RSI ~67, last 3 majority CALL), and the past
    5 days get CALL candles in a +-60 min window around now, so the
    next minute's HH:MM votes CALL 5-0."""
    import bot.handlers as handlers
    today = handlers._today_date_str()
    d = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=DHAKA)
    rows = []
    price = 100.0
    for i in range(60):
        up = i % 3 != 2
        price += 0.10 if up else -0.10
        open_p = price - 0.05 if up else price + 0.05
        rows.append({"ts": d + timedelta(hours=9, minutes=i),
                     "open": open_p, "high": max(open_p, price) + 0.02,
                     "low": min(open_p, price) - 0.02, "close": price})
    db.insert_candles(asset, rows)
    # Past 5 days: CALL candles around the current time of day.
    import bot.collector as collector_mod
    now = collector_mod.dhaka_now()
    base = now.replace(second=0, microsecond=0)
    for back in range(1, 6):
        day = (base - timedelta(days=back)).strftime("%Y-%m-%d")
        day_rows = []
        for off in range(-60, 61):
            ts = base - timedelta(days=back) + timedelta(minutes=off)
            day_rows.append({"ts": ts, "open": 100.0, "high": 100.3,
                             "low": 99.9, "close": 100.2})
        db.insert_candles(asset, day_rows)


def test_ai_next_signal_agrees_on_call():
    """g) The owner, 2026-11-28: AI Signal gives the NEXT minute's
    direction only when EMA trend, RSI zone, the last 3 candles and
    the 5-day same-time history ALL agree — here they all say CALL."""
    import bot.handlers as handlers
    db, tmp = _tmp_db()
    _load_ai_bullish(db, "BTCUSD_otc")
    restore = _setup(db)
    try:
        direction, checks, n = handlers._ai_next_signal("BTCUSD_otc")
        assert direction == "CALL", (direction, checks)
        assert all(v == "CALL" for v in checks.values()), checks
        assert n == 60, n
        # And the typed flow shows it: no Open/Close, no disclaimer,
        # no Generated line, with the next minute's HH:MM.
        ctx = FakeContext()
        q = FakeQuery("menu_ai")
        _drive(ctx, q, "menu_ai")
        upd = _send_text(ctx, "BTCUSD-OTC")
        result = upd.message.working.texts[-1]
        assert "AI SIGNAL" in result, result
        assert "M1 CALL" in result, result
        assert "Close:" not in result, result
        assert "Open:" not in result, result
        assert "No signal guarantees" not in result, result
        assert "Generated:" not in result, result
    finally:
        restore()
    print("PASS g) AI Signal: all four checks agree -> CALL for the next minute")


def test_refresh_reruns_same_result():
    db, tmp = _tmp_db()
    _load_today(db, "BTCUSD_otc")
    _load_today(db, "ETHUSD_otc")
    restore = _setup(db)
    try:
        ctx = FakeContext()
        # AI Signal now takes a typed asset (the owner, 2026-11-28):
        # menu_ai asks for the name, typing it runs the signal.
        q = FakeQuery("menu_ai")
        _drive(ctx, q, "menu_ai")
        assert "type the asset" in q.texts[-1], q.texts[-1]
        upd = _send_text(ctx, "ETHUSD-OTC")
        result = upd.message.working.texts[-1]
        assert "AI SIGNAL" in result, result
        assert "ETHUSD-OTC" in result, result
        # No Open/Close data, no disclaimer, no Generated line.
        assert "Close:" not in result, result
        assert "Open:" not in result, result
        assert "No signal guarantees" not in result, result
        assert "Generated:" not in result, result
        # Refresh on the SAME message re-runs the same result.
        q2 = FakeQuery("refresh", message_id=456)
        _drive(ctx, q2, "refresh")
        assert "AI SIGNAL" in q2.texts[-1], q2.texts[-1]
        assert "ETHUSD-OTC" in q2.texts[-1], q2.texts[-1]
        # Refresh on an unknown message id: honest "too old" message.
        q3 = FakeQuery("refresh", message_id=999)
        _drive(ctx, q3, "refresh")
        assert "too old to refresh" in q3.texts[-1], q3.texts[-1]
    finally:
        restore()
    print("PASS f) AI Signal typed asset + Refresh re-runs; unknown message says so")


def test_backtest_manual_inputs():
    """h) The owner, 2026-11-28: Backtest Lab takes asset, date, time
    window and days by hand — no more fixed yesterday 09:00-10:59."""
    db, tmp = _tmp_db()
    _load_two_assets_two_days(db)
    restore = _setup(db)
    try:
        ctx = FakeContext()
        q = FakeQuery("menu_backtest")
        _drive(ctx, q, "menu_backtest")
        assert "Send the asset name" in q.texts[-1], q.texts[-1]
        # A bad asset asks again, keeping the flow.
        upd = _send_text(ctx, "NOPE-OTC")
        assert "do not have that asset" in upd.message.texts[-1], upd.message.texts[-1]
        upd = _send_text(ctx, "BTCUSD-OTC")
        assert "Which date" in upd.message.texts[-1], upd.message.texts[-1]
        # A future date asks again.
        upd = _send_text(ctx, "2027-01-01")
        assert "not valid" in upd.message.texts[-1], upd.message.texts[-1]
        upd = _send_text(ctx, "2026-11-21")
        assert "Which time window" in upd.message.texts[-1], upd.message.texts[-1]
        # A bad time asks again.
        upd = _send_text(ctx, "10:59 09:00")
        assert "not valid" in upd.message.texts[-1], upd.message.texts[-1]
        upd = _send_text(ctx, "09:00 10:59")
        assert "how many days" in upd.message.texts[-1], upd.message.texts[-1]
        # Pick 1 training day -> the backtest runs for 2026-11-21,
        # built from 2026-11-20.
        q2 = FakeQuery("backtest_days_1")
        _drive(ctx, q2, "backtest_days_1")
        result = q2.texts[-1]
        assert "BACKTEST LAB" in result, result
        assert "BTCUSD-OTC" in result, result
        assert "2026-11-21" in result, result
        assert "09:00-10:59" in result, result
        assert "2026-11-20" in result, result
        assert "Total:" in result, result
    finally:
        restore()
    print("PASS h) Backtest Lab: asset + date + time + days by hand, with validation")


def test_market_live_improved():
    """i) The owner, 2026-11-28: Market Live shows the live price +
    change, the last 5 candles, support/resistance, the payout %
    and a signal hint."""
    import bot.handlers as handlers
    db, tmp = _tmp_db()
    _load_today(db, "BTCUSD_otc")
    db.set_payouts([("BTCUSD_otc", 95, True, "2026-11-28 12:00:00")])
    restore = _setup(db)
    try:
        q = FakeQuery("menu_market")
        asyncio.run(handlers._run_market(q, "BTCUSD_otc"))
        result = q.texts[-1]
        assert "Market Live" in result, result
        assert "Live price:" in result, result
        assert "Last 5:" in result, result
        # The last 5 candles are all listed (09:55-09:59).
        assert "09:55" in result and "09:59" in result, result
        assert "Resistance:" in result, result
        assert "Support:" in result, result
        assert "Payout (1M): 95%" in result, result
        assert "Signal hint:" in result, result
    finally:
        restore()
    print("PASS i) Market Live: live price, last 5, S/R, payout, signal hint")


def _seed_filter_days(db):
    """Past 2 days (2026-11-20/21) for BTCUSD_otc with a controlled
    pattern, so the rebuilt filter's three checks are separable:
    08:59 PUT + 09:00 CALL on both days (Blackout: T opposite T-1),
    09:04 CALL + 09:05 PUT on both days."""
    from datetime import datetime as _dt
    rows = []
    for date in ("2026-11-20", "2026-11-21"):
        d = _dt.strptime(date, "%Y-%m-%d").replace(tzinfo=DHAKA)
        # 08:59 PUT, 09:00 CALL (Blackout pair).
        rows.append({"ts": d + timedelta(hours=8, minutes=59),
                     "open": 100.2, "high": 100.25, "low": 99.95,
                     "close": 100.0})
        rows.append({"ts": d + timedelta(hours=9, minutes=0),
                     "open": 100.0, "high": 100.25, "low": 99.95,
                     "close": 100.2})
        # 09:04 CALL, 09:05 PUT (Blackout pair, the other way).
        rows.append({"ts": d + timedelta(hours=9, minutes=4),
                     "open": 100.0, "high": 100.25, "low": 99.95,
                     "close": 100.2})
        rows.append({"ts": d + timedelta(hours=9, minutes=5),
                     "open": 100.2, "high": 100.25, "low": 99.95,
                     "close": 100.0})
    return db.insert_candles("BTCUSD_otc", rows)


def test_signal_filter_rebuilt():
    """j) The owner, 2026-11-28: Signal Filter rebuilt — the pasted
    list is analysed from the DB (win % + trend + Blackout/Whiteout),
    days and % are asked, only the passing lines are kept, and a
    download-style progress bar is shown while filtering."""
    import bot.handlers as handlers
    db, tmp = _tmp_db()
    _seed_filter_days(db)
    _load_today(db, "BTCUSD_otc")  # today: 60 candles, trend UP.
    restore = _setup(db)
    try:
        ctx = FakeContext()
        q = FakeQuery("menu_filter")
        _drive(ctx, q, "menu_filter")
        assert "Send the signal list" in q.texts[-1], q.texts[-1]
        upd = _send_text(ctx, "BTCUSD-OTC,09:00 M1 CALL\n"
                              "BTCUSD-OTC,09:00 M1 PUT\n"
                              "BTCUSD-OTC,09:05 M1 PUT")
        assert "How many days" in upd.message.texts[-1], upd.message.texts
        _drive(ctx, q, "filter_days_2")
        assert "minimum win rate" in q.texts[-1], q.texts[-1]
        upd2 = _send_text(ctx, "70")
        # The progress bar was shown on the working message, then the
        # working message was edited into the filtered result.
        working_texts = upd2.message.working.texts
        assert any("Filtering..." in t for t in working_texts), working_texts
        result = working_texts[-1]
        assert "Filter Results" in result, result
        assert "BTCUSD-OTC,09:00 M1 CALL" in result, result
        assert "BTCUSD-OTC,09:00 M1 PUT" not in result, result
        assert "BTCUSD-OTC,09:05 M1 PUT" not in result, result
        assert "Filtered: 1/3 kept" in result, result
    finally:
        restore()
    print("PASS j) Signal Filter rebuilt: win% + trend + Blackout, "
          "days + %, progress bar, input format kept")


def test_checker_loss_candle_report():
    """k) The owner, 2026-11-28: after a CHECKER result, a SHOW LOSS
    CANDLE button sends the list's LOSS candles one by one as chart
    images built from the DB candles — pair, time (M1), direction,
    LOSS CANDLE n/total. Only on tap, never automatically."""
    import bot.handlers as handlers
    from tests.synthetic_data import force_candle, make_day, to_raw
    db, tmp = _tmp_db()
    day = make_day("BTCUSD_otc", "2026-11-21", seed=7)
    force_candle(day, "09:00", "CALL")
    force_candle(day, "09:05", "PUT")
    force_candle(day, "09:10", "CALL")
    db.insert_candles("BTCUSD_otc", rows_from_raw(to_raw(day)))
    restore = _setup(db)
    try:
        ctx = FakeContext()
        ctx.user_data.update({"flow": "checker_list",
                              "checker_date": "2026-11-21",
                              "checker_mtg1": False,
                              "checker_mode": "OTC"})
        upd = _send_text(ctx, "BTCUSD-OTC,09:00 M1 CALL\n"
                              "BTCUSD-OTC,09:00 M1 PUT\n"
                              "BTCUSD-OTC,09:05 M1 CALL\n"
                              "BTCUSD-OTC,09:10 M1 PUT")
        # Nothing was sent automatically — no photos before the tap.
        assert ctx.bot.photos == [], ctx.bot.photos
        # The result's last part carries the SHOW LOSS CANDLE button.
        last_kb = upd.message.working.markups[-1] or upd.message.markups[-1]
        cbs = [b.callback_data for row in last_kb.inline_keyboard
               for b in row]
        assert "checker_loss_candles" in cbs, cbs
        # Tap it: the 3 losses are charted one by one (the WIN is not).
        q = FakeQuery("checker_loss_candles")
        _drive(ctx, q, "checker_loss_candles")
        assert len(ctx.bot.photos) == 3, len(ctx.bot.photos)
        captions = [c for _cid, _png, c in ctx.bot.photos]
        assert "LOSS CANDLE 1/3" in captions[0], captions
        assert "LOSS CANDLE 2/3" in captions[1], captions
        assert "LOSS CANDLE 3/3" in captions[2], captions
        assert "09:00 M1 PUT" in captions[0], captions
        assert "09:05 M1 CALL" in captions[1], captions
        assert "09:10 M1 PUT" in captions[2], captions
        # Each photo is a real PNG.
        for _cid, png, _c in ctx.bot.photos:
            assert png[:8] == b"\x89PNG\r\n\x1a\n", png[:8]
    finally:
        restore()
    print("PASS k) Loss Candle report: SHOW LOSS CANDLE charts the "
          "losses one by one, on tap only")


def test_botstatus_refresh():
    db, tmp = _tmp_db()
    _load_today(db, "BTCUSD_otc")
    restore = _setup(db)
    old_admins = config.ADMIN_USER_IDS
    try:
        # Bot Status is owner-only (the owner, 2026-11-27).
        config.ADMIN_USER_IDS = {999}  # FakeUser id
        ctx = FakeContext()
        q = FakeQuery("menu_botstatus")
        _drive(ctx, q, "menu_botstatus")
        assert "BOT STATUS" in q.texts[-1], q.texts[-1]
        assert _button(q, "refresh") is not None, q.texts[-1]
        n_before = len(q.texts)
        _drive(ctx, q, "refresh")
        assert len(q.texts) > n_before, q.texts
        assert "BOT STATUS" in q.texts[-1], q.texts[-1]
        # The main menu shows Bot Status ONLY to the owner.
        import bot.handlers as handlers
        admin_btns = [b for row in handlers.main_keyboard(admin=True).inline_keyboard
                      for b in row]
        assert any(b.callback_data == "menu_botstatus" for b in admin_btns)
        plain_btns = [b for row in handlers.main_keyboard(admin=False).inline_keyboard
                      for b in row]
        assert not any(b.callback_data == "menu_botstatus" for b in plain_btns)
        # A non-admin tapping the button (or a crafted callback)
        # gets a denial, not the status — and Refresh on a Bot
        # Status message is a no-op for them.
        config.ADMIN_USER_IDS = {5618029321}
        q2 = FakeQuery("menu_botstatus")
        _drive(ctx, q2, "menu_botstatus")
        assert "Only the owner can see Bot Status." in q2.texts[-1], q2.texts[-1]
        n2 = len(q2.texts)
        _drive(ctx, q2, "refresh")
        assert len(q2.texts) == n2, q2.texts
    finally:
        config.ADMIN_USER_IDS = old_admins
        restore()
    print("PASS g) Bot Status has Refresh and re-reads the database (owner only)")


def test_payouts_as_buttons():
    db, tmp = _tmp_db()
    _load_today(db, "BTCUSD_otc")
    _load_today(db, "ETHUSD_otc")
    db.set_payouts([("BTCUSD_otc", 95.0, True, "2026-11-26 11:48:34"),
                    ("ETHUSD_otc", 93.0, True, "2026-11-26 11:48:34"),
                    ("XAUUSD_otc", 91.0, True, "2026-11-26 11:48:34")])
    restore = _setup(db)
    try:
        ctx = FakeContext()
        q = FakeQuery("menu_payouts")
        _drive(ctx, q, "menu_payouts")
        text = q.texts[-1]
        # No dead list: the payouts are buttons with the % on them.
        assert "BTCUSD-OTC: 95%" not in text, text
        assert "Updated: 2026-11-26 11:48:34" in text, text
        btc = _button(q, "payout_BTCUSD_otc")
        eth = _button(q, "payout_ETHUSD_otc")
        assert btc is not None and eth is not None, q.texts[-1]
        assert "95%" in btc.text and "BTCUSD-OTC" in btc.text, btc.text
        assert "93%" in eth.text, eth.text
        # Every payout button carries a premium money icon, best first.
        payout_btns = [b for b in _last_buttons(q)
                       if b.callback_data.startswith("payout_")]
        assert len(payout_btns) == 3, payout_btns
        assert all(b.icon_custom_emoji_id == "6267068789146260253"
                   for b in payout_btns), payout_btns
        assert payout_btns[0].callback_data == "payout_BTCUSD_otc", payout_btns
        # Tapping one opens that market's Market Live detail.
        _drive(ctx, q, "payout_BTCUSD_otc")
        assert "Market Live" in q.texts[-1], q.texts[-1]
        assert "BTCUSD-OTC" in q.texts[-1], q.texts[-1]
        # The board itself refreshes (go back and refresh it).
        _drive(ctx, q, "menu_payouts")
        n_before = len(q.texts)
        _drive(ctx, q, "refresh")
        assert len(q.texts) > n_before, q.texts
        assert "Live Payouts" in q.texts[-1], q.texts[-1]
    finally:
        restore()
    print("PASS h) payouts are buttons with % + premium icon; tap opens Market Live")


if __name__ == "__main__":
    test_analysis_multiselect_toggle_and_next()
    test_analysis_next_empty_warns()
    test_futures_multiselect()
    test_futures_list_matches_single_asset()
    test_futures_select_all_toggle()
    test_refresh_reruns_same_result()
    test_ai_next_signal_agrees_on_call()
    test_backtest_manual_inputs()
    test_market_live_improved()
    test_signal_filter_rebuilt()
    test_checker_loss_candle_report()
    test_botstatus_refresh()
    test_payouts_as_buttons()
    print("ALL MULTI-SELECT TESTS PASSED")
