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
        assert bot.photos[0][1][:8] == PNG_MAGIC, "signal photo is not a PNG"
        # The 12:01 candle closes as a WIN for the CALL.
        db.insert_candles("BTCUSD_otc", [{
            "ts": datetime(2026, 11, 28, 12, 1, tzinfo=DHAKA),
            "open": 100.0, "high": 100.3, "low": 99.9, "close": 100.2}])
        # Advance the clock past 12:03 (the result grace) and tick.
        clock.now += 150
        asyncio.run(auto_live._auto_live_tick(bot, db, now_fn=_now_fn(clock)))
        assert len(bot.photos) == 2, bot.photos
        assert "WIN" in bot.photos[1][2], bot.photos[1][2]
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


def test_auto_live_menu_pick_and_toggle():
    """e) The menu: pick a market, then Turn ON — the subscriber list
    gains (uid, market); turning OFF removes it."""
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
        # Toggling ON without a market asks to pick one first.
        _drive(ctx, q, "autolive_toggle")
        assert "Pick a market first" in q.texts[-1], q.texts[-1]
        _drive(ctx, q, "autolive_pick")
        assert "pick your market" in q.texts[-1], q.texts[-1]
        _drive(ctx, q, "autolive_asset_BTCUSD_otc")
        assert "BTCUSD-OTC" in q.texts[-1], q.texts[-1]
        assert database.get_auto_asset(999) == "BTCUSD_otc"
        _drive(ctx, q, "autolive_toggle")
        assert "Status: ON" in q.texts[-1], q.texts[-1]
        assert database.get_auto_subscribers() == [(999, "BTCUSD_otc")]
        _drive(ctx, q, "autolive_toggle")
        assert "Status: OFF" in q.texts[-1], q.texts[-1]
        assert database.get_auto_subscribers() == []
    finally:
        restore()
        restore_users()
    print("PASS e) auto live menu: pick market + ON/OFF toggle")


if __name__ == "__main__":
    test_auto_live_signal_and_result()
    test_auto_live_weak_analysis_sends_nothing()
    test_chart_png_marks_signal_candle()
    test_auto_live_menu_pick_and_toggle()
    print("ALL AUTO-LIVE TESTS PASSED")
