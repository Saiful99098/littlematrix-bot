"""Live Auto Signal loop + chart screenshots (the owner, 2026-11-28).

Run: python tests/test_auto_live.py

a) The loop sends a signal + chart photo only when all four AI
   checks agree (strong analysis), never every minute, and only one
   live signal at a time per market.
b) After the signal minute closes, a result photo follows with a
   tick (WIN) or a cross (LOSS) on the signal candle.
c) A weak analysis sends nothing.
d) draw_candles_png returns a valid PNG with the signal candle
   marked.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot import auto_live, config, database
from bot.candles_db import CandleDB, DHAKA
from bot.chart import draw_candles_png
from bot.signal_engine import normalize_candle

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class _StopLoop(Exception):
    pass


class _FakeClock:
    def __init__(self, start):
        self.now = start  # unix seconds

    def time(self):
        return float(self.now)


class _DelaySleep:
    """Advance the fake clock by the delay the loop asks for; stop
    after max_calls."""

    def __init__(self, clock, max_calls=10):
        self.clock = clock
        self.max_calls = max_calls
        self.calls = 0

    async def __call__(self, delay):
        self.calls += 1
        if self.calls > self.max_calls:
            raise _StopLoop()
        self.clock.now += delay


class FakeBot:
    def __init__(self):
        self.photos = []  # (chat_id, png_bytes, caption)
        self.messages = []  # (chat_id, text)

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        self.photos.append((chat_id, photo, caption))
        return None

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))
        return None


def _tmp_candle_db():
    return CandleDB(database_url="", sqlite_path=Path(tempfile.mkdtemp()) / "c.db").init()


def _setup_users():
    old_url, old_path = config.DATABASE_URL, config.DB_PATH
    config.DATABASE_URL = ""
    config.DB_PATH = Path(tempfile.mkdtemp()) / "users.db"
    database._CONN = None

    def restore():
        config.DATABASE_URL, config.DB_PATH = old_url, old_path
        database._CONN = None

    return restore


def _seed_bullish(db, asset, now):
    """Today: 60 rising candles (up 0.10 twice, down 0.10 once per 3
    min — EMA up, RSI ~67, last 3 majority CALL). Past 5 days: CALL
    candles in a +-60 min window around `now`, so the next minute's
    HH:MM votes CALL 5-0."""
    today = now.strftime("%Y-%m-%d")
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
    base = now.replace(second=0, microsecond=0)
    for back in range(1, 6):
        day_rows = []
        for off in range(-60, 61):
            ts = base - timedelta(days=back) + timedelta(minutes=off)
            day_rows.append({"ts": ts, "open": 100.0, "high": 100.3,
                             "low": 99.9, "close": 100.2})
        db.insert_candles(asset, day_rows)


def _now_fn(clock):
    def _fn():
        return datetime.fromtimestamp(clock.now, tz=DHAKA)
    return _fn


def test_auto_live_signal_and_result():
    """a+b) A strong analysis sends a signal + chart photo for the
    next minute; after that minute closes as a WIN, a result photo
    follows with the tick."""
    db = _tmp_candle_db()
    restore = _setup_users()
    try:
        start = datetime(2026, 11, 28, 12, 0, tzinfo=DHAKA)
        clock = _FakeClock(start.timestamp())
        _seed_bullish(db, "BTCUSD_otc", start)
        database.set_auto_asset(123, "BTCUSD_otc")
        database.set_auto_signals(123, True)
        bot = FakeBot()
        # Tick 1 (12:00:05): signal for 12:01. The sleep advances
        # the clock to ~12:01:05; tick 2 finds the 12:01 minute too
        # young for a result, and no new signal (one live at a time).
        # Store the 12:01 candle as a WIN (CALL) before tick 3.
        loop = auto_live.auto_live_loop(
            bot, db, sleep=_DelaySleep(clock, max_calls=2),
            now_fn=_now_fn(clock), max_ticks=3)
        try:
            asyncio.run(loop)
        except _StopLoop:
            pass
        assert len(bot.photos) == 1, bot.photos
        assert "12:01" in bot.photos[0][2] and "CALL" in bot.photos[0][2], bot.photos[0][2]
        # Fix 5 (2026-11-28): no "all four checks agree" in the caption.
        assert "all four checks agree" not in bot.photos[0][2], bot.photos[0][2]
        assert bot.photos[0][1][:8] == PNG_MAGIC, "signal photo is not a PNG"
        # The 12:01 candle closes as a WIN for the CALL.
        db.insert_candles("BTCUSD_otc", [{
            "ts": datetime(2026, 11, 28, 12, 1, tzinfo=DHAKA),
            "open": 100.0, "high": 100.3, "low": 99.9, "close": 100.2}])
        # Advance the clock past the result time (ready 5s after the
        # 12:01 candle closes, i.e. 12:02:05) and tick.
        clock.now += 150
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        assert len(bot.photos) == 2, bot.photos
        # Fix 1 (2026-11-28): no "WIN" text — only the premium
        # check sticker.
        assert "WIN" not in bot.photos[1][2], bot.photos[1][2]
        assert "tg-emoji" in bot.photos[1][2], bot.photos[1][2]
        assert bot.photos[1][1][:8] == PNG_MAGIC, "result photo is not a PNG"
    finally:
        restore()
    print("PASS a+b) auto live: signal + chart, then result photo with WIN")


def test_auto_live_weak_analysis_sends_nothing():
    """c) A flat market (no agreement) sends no signal at all."""
    db = _tmp_candle_db()
    restore = _setup_users()
    try:
        start = datetime(2026, 11, 28, 12, 0, tzinfo=DHAKA)
        clock = _FakeClock(start.timestamp())
        # Flat candles today: no trend, RSI ~50, no agreement.
        today = start.strftime("%Y-%m-%d")
        d = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=DHAKA)
        rows = [{"ts": d + timedelta(hours=9, minutes=i),
                 "open": 100.0, "high": 100.05, "low": 99.95,
                 "close": 100.0} for i in range(60)]
        db.insert_candles("BTCUSD_otc", rows)
        database.set_auto_asset(123, "BTCUSD_otc")
        database.set_auto_signals(123, True)
        bot = FakeBot()
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        assert bot.photos == [] and bot.messages == [], (bot.photos, bot.messages)
    finally:
        restore()
    print("PASS c) auto live: weak analysis sends nothing")


def test_chart_png_marks_signal_candle():
    """d) draw_candles_png returns a valid PNG; a win mark and a loss
    mark both draw without error."""
    rows = []
    price = 100.0
    for i in range(30):
        price += 0.1 if i % 2 == 0 else -0.05
        rows.append(normalize_candle({
            "datetime": f"2026-11-28 11:{i:02d}:00",
            "open": price - 0.05, "high": price + 0.1,
            "low": price - 0.1, "close": price}))
    for mark in (None, "win", "loss"):
        png = draw_candles_png(rows, highlight_idx=29, mark=mark,
                               title="BTCUSD-OTC 11:29 M1 CALL")
        assert png[:8] == PNG_MAGIC, png[:8]
        assert len(png) > 1000, len(png)
    try:
        draw_candles_png([])
        raise AssertionError("empty candles should raise")
    except ValueError:
        pass
    print("PASS d) chart PNG draws with win/loss marks; empty raises")


def _setup_candles(db):
    """Patch handlers to use the temp candle DB in db mode — WITHOUT
    neutering get_or_create_user (the auto-live menu needs the real
    users DB)."""
    from bot import candles_db as candles_db_mod
    from bot import config as config_mod
    real_get_db = candles_db_mod.get_candle_db
    real_mode = config_mod.DATA_MODE
    candles_db_mod.get_candle_db = lambda: db
    config_mod.DATA_MODE = "db"

    def restore():
        candles_db_mod.get_candle_db = real_get_db
        config_mod.DATA_MODE = real_mode

    return restore


def test_auto_live_result_within_5s_of_close():
    """f) The result follows within 5 seconds of the signal candle
    closing (the owner, 2026-11-28): the old 2-minute grace made
    every result 1 minute late. The signal caption carries a premium
    sticker too."""
    db = _tmp_candle_db()
    restore = _setup_users()
    try:
        start = datetime(2026, 11, 28, 12, 0, tzinfo=DHAKA)
        clock = _FakeClock(start.timestamp())
        _seed_bullish(db, "BTCUSD_otc", start)
        database.set_auto_asset(123, "BTCUSD_otc")
        database.set_auto_signals(123, True)
        bot = FakeBot()
        # Tick at 12:00:05 -> signal for 12:01, then stop new
        # signals so the result ticks are measured cleanly.
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        assert len(bot.photos) == 1, bot.photos
        assert "tg-emoji" in bot.photos[0][2], "signal caption has no premium sticker"
        # Fix 5 (2026-11-28): no "all four checks agree" in the caption.
        assert "all four checks agree" not in bot.photos[0][2], bot.photos[0][2]
        # Stop new signals WITHOUT turning OFF: turning OFF would
        # cancel the pending result (Fix 2, 2026-11-28). Clearing
        # the picked markets stops new signals but keeps the
        # subscriber flag, so the pending result is still sent.
        database.set_auto_assets(123, [])
        db.insert_candles("BTCUSD_otc", [{
            "ts": datetime(2026, 11, 28, 12, 1, tzinfo=DHAKA),
            "open": 100.0, "high": 100.3, "low": 99.9, "close": 100.2}])
        # 12:01:05 — the minute has not closed yet: no result.
        clock.now += 60
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        assert len(bot.photos) == 1, bot.photos
        # 12:02:05 — 5s after the 12:01 candle closed: the result.
        clock.now += 60
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        assert len(bot.photos) == 2, bot.photos
        # Fix 1: no "WIN" text — only the premium check sticker.
        assert "WIN" not in bot.photos[1][2], bot.photos[1][2]
        assert "tg-emoji" in bot.photos[1][2], "result caption has no premium sticker"
        # The sticker sits beside the direction (after CALL/PUT),
        # not before the asset (the owner, 2026-11-28 16:10).
        cap = bot.photos[1][2]
        assert cap.index("CALL") < cap.index("tg-emoji"), cap
        assert not cap.startswith("<tg-emoji"), cap
    finally:
        restore()
    print("PASS f) auto live: result within 5s of the signal candle closing")


def test_auto_live_mtg1_dot_and_timing():
    """g) MTG1 mode: the result waits for the next minute, and a
    MTG1 win carries the dot (check + dot), like the CHECKER."""
    db = _tmp_candle_db()
    restore = _setup_users()
    try:
        start = datetime(2026, 11, 28, 12, 0, tzinfo=DHAKA)
        clock = _FakeClock(start.timestamp())
        _seed_bullish(db, "BTCUSD_otc", start)
        database.set_auto_asset(123, "BTCUSD_otc")
        database.set_auto_mtg1(123, True)
        database.set_auto_signals(123, True)
        bot = FakeBot()
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        assert len(bot.photos) == 1, bot.photos
        assert "(MTG1)" in bot.photos[0][2], bot.photos[0][2]
        # Stop new signals WITHOUT turning OFF (Fix 2): turning OFF
        # would cancel the pending result.
        database.set_auto_assets(123, [])
        # 12:01 loses (PUT against the CALL), 12:02 wins (CALL).
        db.insert_candles("BTCUSD_otc", [
            {"ts": datetime(2026, 11, 28, 12, 1, tzinfo=DHAKA),
             "open": 100.2, "high": 100.3, "low": 99.9, "close": 100.0},
            {"ts": datetime(2026, 11, 28, 12, 2, tzinfo=DHAKA),
             "open": 100.0, "high": 100.3, "low": 99.9, "close": 100.2}])
        # 12:02:05 — the MTG1 candle has not closed yet: no result.
        clock.now += 120
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        assert len(bot.photos) == 1, bot.photos
        # 12:03:05 — 5s after the MTG1 candle closed: the result.
        clock.now += 60
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        assert len(bot.photos) == 2, bot.photos
        # Fix 1: no "WIN (MTG1)" text — the MTG1 win is the premium
        # check sticker + a dot.
        assert "WIN" not in bot.photos[1][2], bot.photos[1][2]
        assert "</tg-emoji>." in bot.photos[1][2], bot.photos[1][2]
        assert "tg-emoji" in bot.photos[1][2], "result caption has no premium sticker"
    finally:
        restore()
    print("PASS g) auto live MTG1: result waits for MTG1, win carries the dot")


def test_auto_live_five_signal_summary():
    """h) After 5 completed signals the bot sends a 5-signal
    summary (premium stickers + accuracy), then the counter resets."""
    db = _tmp_candle_db()
    restore = _setup_users()
    try:
        start = datetime(2026, 11, 28, 12, 0, tzinfo=DHAKA)
        clock = _FakeClock(start.timestamp())
        # The pending signals belong to a subscriber (Fix 2's guard
        # drops results for users who turned OFF).
        database.set_auto_asset(123, "BTCUSD_otc")
        database.set_auto_signals(123, True)
        pending = []
        for m in range(1, 6):
            db.insert_candles("BTCUSD_otc", [{
                "ts": datetime(2026, 11, 28, 12, m, tzinfo=DHAKA),
                "open": 100.0, "high": 100.3, "low": 99.9, "close": 100.2}])
            pending.append({"uid": 123, "asset": "BTCUSD_otc",
                            "minute": f"2026-11-28 12:{m:02d}",
                            "direction": "CALL", "mtg1": False})
        auto_live._save_pending(db, pending)
        bot = FakeBot()
        # 12:06:05 — all 5 minutes are decided: 5 results + 1 summary.
        clock.now += 6 * 60 + 5
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        assert len(bot.photos) == 5, bot.photos
        assert len(bot.messages) == 1, bot.messages
        summary = bot.messages[0][1]
        # Fix 6 (2026-11-28): no "Signals Summary" — the header is
        # "Auto Live Signal" with a premium emoji on BOTH sides,
        # and there is a blank line directly below the DATE line.
        assert "Signals Summary" not in summary, summary
        assert "Accuracy: 5/5" in summary, summary
        assert "tg-emoji" in summary, "summary has no premium sticker"
        # Fix 3 (2026-11-28): CHECKER format — date at the top,
        # monospace signal lines, a gap below the header and above
        # the Accuracy line, signals packed together.
        assert "DATE:" in summary and "28.11.2026" in summary, summary
        assert "<code>" in summary, "summary signals are not monospace"
        lines = summary.split("\n")
        assert lines[0].startswith("DATE:"), lines
        assert lines[1] == "", lines  # blank line below the DATE line
        assert "Auto Live Signal" in lines[2], lines
        assert (lines[2].startswith("<tg-emoji")
                and lines[2].rstrip().endswith("</tg-emoji>")), lines
        assert lines[3] == "", lines  # gap below the header
        assert lines[-2] == "", lines  # gap above Accuracy
        assert "Accuracy:" in lines[-1], lines
        sig_lines = [ln for ln in lines if ln.startswith("<code>")]
        assert len(sig_lines) == 5, sig_lines
        assert auto_live.history_count(db, 123) == 0
        assert auto_live._load_pending(db) == []
    finally:
        restore()
    print("PASS h) auto live: 5-signal summary after 5 completed signals")


def test_auto_live_menu_pick_and_toggle():
    """e) The menu: multi-select markets (toggle + SELECT ALL +
    Next), then Turn ON — the subscriber list gains one (uid,
    market) pair per market; turning OFF removes them."""
    import bot.handlers as handlers
    from tests.test_multiselect import (
        FakeContext, FakeQuery, _drive, _load_today)
    db = _tmp_candle_db()
    _load_today(db, "BTCUSD_otc")
    _load_today(db, "ETHUSD_otc")
    restore = _setup_candles(db)
    restore_users = _setup_users()
    try:
        ctx = FakeContext()
        q = FakeQuery("menu_autolive")
        _drive(ctx, q, "menu_autolive")
        assert "not picked yet" in q.texts[-1], q.texts[-1]
        assert "Signals: 0/5" in q.texts[-1], q.texts[-1]
        # Toggling ON without a market asks to pick markets first.
        _drive(ctx, q, "autolive_toggle")
        assert "Pick markets first" in q.texts[-1], q.texts[-1]
        _drive(ctx, q, "autolive_pick")
        assert "pick markets" in q.texts[-1], q.texts[-1]
        # Toggle two markets (multi-select), then Next saves them.
        _drive(ctx, q, "autolive_asset_BTCUSD_otc")
        assert "Selected: 1" in q.texts[-1], q.texts[-1]
        _drive(ctx, q, "autolive_asset_ETHUSD_otc")
        assert "Selected: 2" in q.texts[-1], q.texts[-1]
        # SELECT ALL with everything picked clears the selection.
        _drive(ctx, q, "autolive_all")
        assert "Selected: 0" in q.texts[-1], q.texts[-1]
        _drive(ctx, q, "autolive_all")
        assert "Selected: 2" in q.texts[-1], q.texts[-1]
        _drive(ctx, q, "autolive_next")
        assert "BTCUSD-OTC" in q.texts[-1] and "ETHUSD-OTC" in q.texts[-1], q.texts[-1]
        assert database.get_auto_assets(999) == ["BTCUSD_otc", "ETHUSD_otc"]
        _drive(ctx, q, "autolive_toggle")
        assert "Status: ON" in q.texts[-1], q.texts[-1]
        assert database.get_auto_subscribers() == [(999, "BTCUSD_otc"), (999, "ETHUSD_otc")]
        _drive(ctx, q, "autolive_toggle")
        assert "Status: OFF" in q.texts[-1], q.texts[-1]
        assert database.get_auto_subscribers() == []
    finally:
        restore()
        restore_users()
    print("PASS e) auto live menu: multi-select markets + ON/OFF toggle")


def test_auto_live_menu_mtg_and_channel():
    """j) The dashboard: MTG1/Non-MTG toggle flips the stored mode;
    the Send to Channel toggle is owner-only."""
    import bot.handlers as handlers
    from tests.test_multiselect import (
        FakeContext, FakeQuery, _button, _drive, _load_today)
    from bot import config as config_mod
    db = _tmp_candle_db()
    _load_today(db, "BTCUSD_otc")
    restore = _setup_candles(db)
    restore_users = _setup_users()
    old_admins = config_mod.ADMIN_USER_IDS
    try:
        ctx = FakeContext()
        q = FakeQuery("menu_autolive")
        _drive(ctx, q, "menu_autolive")
        # Non-admin (999): no channel button.
        assert _button(q, "autolive_channel") is None
        assert _button(q, "autolive_mtg").text == "Mode: Non-MTG"
        _drive(ctx, q, "autolive_mtg")
        assert _button(q, "autolive_mtg").text == "Mode: MTG1"
        assert database.get_auto_mtg1(999) is True
        _drive(ctx, q, "autolive_mtg")
        assert database.get_auto_mtg1(999) is False
        # Owner (999 as admin): the channel button appears and toggles.
        config_mod.ADMIN_USER_IDS = {999}
        _drive(ctx, q, "menu_autolive")
        assert _button(q, "autolive_channel").text == "Send to Channel: OFF"
        _drive(ctx, q, "autolive_channel")
        assert _button(q, "autolive_channel").text == "Send to Channel: ON"
        assert auto_live.channel_send_on(db) is True
    finally:
        config_mod.ADMIN_USER_IDS = old_admins
        restore()
        restore_users()
    print("PASS j) auto live menu: MTG toggle + owner-only channel toggle")


def test_auto_live_back_cancels_pending():
    """i) Back from the dashboard cancels pending results — the bot
    stops sending them (the owner, 2026-11-28)."""
    import bot.handlers as handlers
    from tests.test_multiselect import FakeContext, FakeQuery, _drive
    db = _tmp_candle_db()
    restore = _setup_candles(db)
    restore_users = _setup_users()
    try:
        auto_live._save_pending(db, [{"uid": 999, "asset": "BTCUSD_otc",
                                      "minute": "2026-11-28 12:01",
                                      "direction": "CALL", "mtg1": False}])
        assert len(auto_live._load_pending(db)) == 1
        ctx = FakeContext()
        q = FakeQuery("autolive_back")
        _drive(ctx, q, "autolive_back")
        assert auto_live._load_pending(db) == []
        assert "BLUE FISH PRIME" in q.texts[-1], q.texts[-1]
    finally:
        restore()
        restore_users()
    print("PASS i) auto live Back cancels pending results")


def test_stale_button_after_start():
    """k) After a fresh /start, buttons on older messages no longer
    work (the owner, 2026-11-28)."""
    import bot.handlers as handlers
    from tests.test_multiselect import (
        FakeContext, FakeQuery, FakeUpdate, _drive)
    from datetime import datetime, timezone
    db = _tmp_candle_db()
    restore = _setup_candles(db)
    restore_users = _setup_users()
    try:
        ctx = FakeContext()
        now = datetime.now(timezone.utc).timestamp()
        ctx.user_data["menu_started_at"] = now
        # A button on a message sent 10 minutes before /start: dead.
        q = FakeQuery("menu_autolive")
        q.message.date = datetime.fromtimestamp(now - 600, tz=timezone.utc)
        asyncio.run(handlers.menu_callback(FakeUpdate(q), ctx))
        assert q.texts == [], q.texts
        # A button on a fresh message still works.
        q2 = FakeQuery("menu_autolive")
        q2.message.date = datetime.fromtimestamp(now + 5, tz=timezone.utc)
        _drive(ctx, q2, "menu_autolive")
        assert "Auto Live Signal" in q2.texts[-1], q2.texts[-1]
    finally:
        restore()
        restore_users()
    print("PASS k) stale buttons die after a fresh /start")


def test_auto_live_mtg1_m1_win_is_immediate():
    """Fix 4 (2026-11-28): MTG1 mode, M1 direct win — the result
    comes 5s after the signal candle closes, NOT after the MTG1
    candle. Only a lost M1 waits for MTG1."""
    db = _tmp_candle_db()
    restore = _setup_users()
    try:
        start = datetime(2026, 11, 28, 12, 0, tzinfo=DHAKA)
        clock = _FakeClock(start.timestamp())
        _seed_bullish(db, "BTCUSD_otc", start)
        database.set_auto_asset(123, "BTCUSD_otc")
        database.set_auto_mtg1(123, True)
        database.set_auto_signals(123, True)
        bot = FakeBot()
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        assert len(bot.photos) == 1, bot.photos
        assert "(MTG1)" in bot.photos[0][2], bot.photos[0][2]
        # Stop new signals WITHOUT turning OFF (Fix 2).
        database.set_auto_assets(123, [])
        # 12:01 wins directly (CALL).
        db.insert_candles("BTCUSD_otc", [{
            "ts": datetime(2026, 11, 28, 12, 1, tzinfo=DHAKA),
            "open": 100.0, "high": 100.3, "low": 99.9, "close": 100.2}])
        # 12:01:05 — the minute has not closed yet: no result.
        clock.now += 60
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        assert len(bot.photos) == 1, bot.photos
        # 12:02:05 — 5s after the 12:01 candle closed: the result
        # is already here (no MTG1 wait for a direct M1 win).
        clock.now += 60
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        assert len(bot.photos) == 2, bot.photos
        # Direct M1 win: the premium check sticker, NO dot (the dot
        # is only for a MTG1 win), NO "WIN" text (Fix 1).
        assert "tg-emoji" in bot.photos[1][2], bot.photos[1][2]
        assert "</tg-emoji>." not in bot.photos[1][2], bot.photos[1][2]
        assert "WIN" not in bot.photos[1][2], bot.photos[1][2]
    finally:
        restore()
    print("PASS Fix 4) auto live MTG1: direct M1 win sends the result at once")


def test_auto_live_toggle_off_cancels_pending():
    """Fix 2 (2026-11-28): turning Auto Live OFF cancels pending
    results — the bot stops sending them, just like Back."""
    import bot.handlers as handlers
    from tests.test_multiselect import FakeContext, FakeQuery, _drive
    db = _tmp_candle_db()
    restore = _setup_candles(db)
    restore_users = _setup_users()
    try:
        database.set_auto_assets(999, ["BTCUSD_otc"])
        database.set_auto_signals(999, True)
        auto_live._save_pending(db, [{"uid": 999, "asset": "BTCUSD_otc",
                                      "minute": "2026-11-28 12:01",
                                      "direction": "CALL", "mtg1": False}])
        assert len(auto_live._load_pending(db)) == 1
        ctx = FakeContext()
        q = FakeQuery("autolive_toggle")
        _drive(ctx, q, "autolive_toggle")
        assert auto_live._load_pending(db) == []
        assert database.get_auto_subscribers() == []
        assert "Status: OFF" in q.texts[-1], q.texts[-1]
    finally:
        restore()
        restore_users()
    print("PASS Fix 2) auto live toggle OFF cancels pending results")


def test_auto_live_off_user_gets_no_result():
    """Fix 2 guard (2026-11-28): a tick drops a pending result when
    the user has turned Auto Live OFF (the toggle already cancels
    pending; this covers the race where OFF lands mid-tick)."""
    db = _tmp_candle_db()
    restore = _setup_users()
    try:
        start = datetime(2026, 11, 28, 12, 0, tzinfo=DHAKA)
        clock = _FakeClock(start.timestamp())
        _seed_bullish(db, "BTCUSD_otc", start)
        database.set_auto_asset(123, "BTCUSD_otc")
        database.set_auto_signals(123, True)
        bot = FakeBot()
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        assert len(bot.photos) == 1, bot.photos
        # The user turns OFF; the pending signal is still in the DB
        # (the toggle's cancel missed it — the race).
        database.set_auto_signals(123, False)
        db.insert_candles("BTCUSD_otc", [{
            "ts": datetime(2026, 11, 28, 12, 1, tzinfo=DHAKA),
            "open": 100.0, "high": 100.3, "low": 99.9, "close": 100.2}])
        clock.now += 125  # 12:02:05 — the result would be due
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        assert len(bot.photos) == 1, bot.photos  # no result sent
        assert auto_live._load_pending(db) == []  # dropped, not kept
    finally:
        restore()
    print("PASS Fix 2 guard) auto live: OFF user gets no pending result")


def test_auto_live_summary_stickers_aligned():
    """Fix 3 (2026-11-28): the 5-signal summary pads every signal
    line to the longest one (CHECKER style), so the premium
    stickers form one straight column."""
    import re
    entries = [
        {"asset": "BTCUSD_otc", "hhmm": "12:01", "date": "2026-11-28",
         "direction": "CALL", "outcome": "WIN"},
        {"asset": "USDARS_otc", "hhmm": "12:02", "date": "2026-11-28",
         "direction": "PUT", "outcome": "LOSS"},
        {"asset": "ETHUSD_otc", "hhmm": "12:03", "date": "2026-11-28",
         "direction": "CALL", "outcome": "WIN_MTG1"},
        {"asset": "BTCUSD_otc", "hhmm": "12:04", "date": "2026-11-28",
         "direction": "PUT", "outcome": "DOJI"},
        {"asset": "USDARS_otc", "hhmm": "12:05", "date": "2026-11-28",
         "direction": "CALL", "outcome": "WIN"},
    ]
    text = auto_live._summary_text(entries)
    assert "DATE:" in text and "28.11.2026" in text, text
    # Fix 6 (2026-11-28): no "Signals Summary" — the header is
    # "Auto Live Signal" with a premium emoji on BOTH sides, and
    # there is a blank line directly below the DATE line.
    assert "Signals Summary" not in text, text
    lines = text.split("\n")
    assert lines[0].startswith("DATE:"), lines
    assert lines[1] == "", lines  # blank line below the DATE line
    header = [ln for ln in lines if "Auto Live Signal" in ln]
    assert len(header) == 1, lines
    assert (header[0].startswith("<tg-emoji")
            and header[0].rstrip().endswith("</tg-emoji>")), header
    sig_lines = [ln for ln in lines if ln.startswith("<code>")]
    assert len(sig_lines) == 5, sig_lines
    # Every monospace block has the same length (padded) — the
    # stickers all start at the same column.
    inner = [re.search(r"<code>(.*)</code>", ln).group(1) for ln in sig_lines]
    assert len({len(s) for s in inner}) == 1, inner
    # The MTG1 win carries the dot; accuracy excludes the DOJI.
    assert "</tg-emoji>." in text, text
    assert "Accuracy: 3/4" in text, text
    print("PASS Fix 3) auto live summary: stickers aligned in one column")


def test_auto_live_channel_copy_is_photo_plus_text():
    """Fix 1 (2026-11-28, REVERTED 2026-11-28 16:16): the channel
    gets a signal/result as ONE message — photo WITH the caption
    below it, exactly like the DM. The two-message split (photo
    without caption + caption as a separate text) did NOT make the
    premium emoji render in the channel (live test 16:05), so the
    owner asked for the caption back under the photo."""
    db = _tmp_candle_db()
    restore = _setup_users()
    old_admins = config.ADMIN_USER_IDS
    old_channel = config.REQUIRED_CHANNEL
    try:
        start = datetime(2026, 11, 28, 12, 0, tzinfo=DHAKA)
        clock = _FakeClock(start.timestamp())
        _seed_bullish(db, "BTCUSD_otc", start)
        config.ADMIN_USER_IDS = {123}
        config.REQUIRED_CHANNEL = "-100123"
        database.set_auto_asset(123, "BTCUSD_otc")
        database.set_auto_signals(123, True)
        auto_live.set_channel_send(db, True)
        bot = FakeBot()
        # Tick 1: signal for 12:01.
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        assert len(bot.photos) == 2, bot.photos
        dm_photo = [p for p in bot.photos if p[0] == 123]
        ch_photo = [p for p in bot.photos if p[0] == "-100123"]
        assert len(dm_photo) == 1 and len(ch_photo) == 1, bot.photos
        assert dm_photo[0][2] is not None and "tg-emoji" in dm_photo[0][2]
        # Channel: the caption rides WITH the photo (one message),
        # no separate caption text.
        assert ch_photo[0][2] is not None, ch_photo
        assert "12:01" in ch_photo[0][2] and "CALL" in ch_photo[0][2]
        assert "tg-emoji" in ch_photo[0][2], ch_photo
        assert not [m for m in bot.messages if m[0] == "-100123"], bot.messages
        # Stop new signals WITHOUT turning OFF (Fix 2), then the
        # 12:01 candle wins — the result follows the same form.
        database.set_auto_assets(123, [])
        db.insert_candles("BTCUSD_otc", [{
            "ts": datetime(2026, 11, 28, 12, 1, tzinfo=DHAKA),
            "open": 100.0, "high": 100.3, "low": 99.9, "close": 100.2}])
        clock.now += 125
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        assert len(bot.photos) == 4, bot.photos
        ch_result_photo = [p for p in bot.photos[2:] if p[0] == "-100123"]
        assert len(ch_result_photo) == 1, bot.photos
        assert ch_result_photo[0][2] is not None, ch_result_photo
        assert "WIN" not in ch_result_photo[0][2], ch_result_photo
        assert "tg-emoji" in ch_result_photo[0][2], ch_result_photo
        assert not [m for m in bot.messages if m[0] == "-100123"], bot.messages
    finally:
        config.ADMIN_USER_IDS = old_admins
        config.REQUIRED_CHANNEL = old_channel
        restore()
    print("PASS Fix 1) auto live channel copy: photo with caption below it")


def test_auto_live_free_minutes_limit():
    """Fix 10 (2026-11-28): a free user with 29 used Auto Live
    minutes is turned OFF after one tick and gets a limit message;
    a premium user with the same usage is not turned off. The
    owner (admin) is never limited either."""
    db = _tmp_candle_db()
    restore = _setup_users()
    old_admins = config.ADMIN_USER_IDS
    try:
        start = datetime(2026, 11, 28, 12, 0, tzinfo=DHAKA)
        clock = _FakeClock(start.timestamp())
        # Flat candles: no signal this tick — the minutes block is
        # what is being tested, not the analysis.
        today = start.strftime("%Y-%m-%d")
        d = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=DHAKA)
        rows = [{"ts": d + timedelta(hours=9, minutes=i),
                 "open": 100.0, "high": 100.05, "low": 99.95,
                 "close": 100.0} for i in range(60)]
        db.insert_candles("BTCUSD_otc", rows)
        database.set_auto_asset(123, "BTCUSD_otc")
        database.set_auto_signals(123, True)
        database.set_auto_asset(124, "BTCUSD_otc")
        database.set_auto_signals(124, True)
        database.grant_premium(124, days=30)
        # Admin 999: not premium, but the owner — never limited.
        config.ADMIN_USER_IDS = {999}
        database.set_auto_asset(999, "BTCUSD_otc")
        database.set_auto_signals(999, True)
        database.increment_usage(123, "auto_live", "2026-11-28", 29)
        database.increment_usage(124, "auto_live", "2026-11-28", 29)
        database.increment_usage(999, "auto_live", "2026-11-28", 29)
        bot = FakeBot()
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        # Free user 123: 29 + 1 = 30 -> OFF + limit message.
        assert database.is_auto_subscriber(123) is False
        assert database.get_usage(123, "auto_live", "2026-11-28") == 30
        limit_msgs = [m for m in bot.messages if m[0] == 123]
        assert len(limit_msgs) == 1, bot.messages
        assert "Your limit is reached" in limit_msgs[0][1], limit_msgs
        # Premium user 124: untouched, still ON, no message, and
        # their minutes were never counted.
        assert database.is_auto_subscriber(124) is True
        assert database.get_usage(124, "auto_live", "2026-11-28") == 29
        assert [m for m in bot.messages if m[0] == 124] == []
        # Admin 999: untouched, still ON, no message, minutes never
        # counted — the owner has no limits.
        assert database.is_auto_subscriber(999) is True
        assert database.get_usage(999, "auto_live", "2026-11-28") == 29
        assert [m for m in bot.messages if m[0] == 999] == []
    finally:
        config.ADMIN_USER_IDS = old_admins
        restore()
    print("PASS Fix 10) auto live: free user turned OFF at 30 minutes, premium untouched")


if __name__ == "__main__":
    test_auto_live_signal_and_result()
    test_auto_live_weak_analysis_sends_nothing()
    test_chart_png_marks_signal_candle()
    test_auto_live_result_within_5s_of_close()
    test_auto_live_mtg1_dot_and_timing()
    test_auto_live_five_signal_summary()
    test_auto_live_menu_pick_and_toggle()
    test_auto_live_menu_mtg_and_channel()
    test_auto_live_back_cancels_pending()
    test_stale_button_after_start()
    test_auto_live_mtg1_m1_win_is_immediate()
    test_auto_live_toggle_off_cancels_pending()
    test_auto_live_off_user_gets_no_result()
    test_auto_live_summary_stickers_aligned()
    test_auto_live_channel_copy_is_photo_plus_text()
    test_auto_live_free_minutes_limit()
    print("ALL AUTO-LIVE TESTS PASSED")
