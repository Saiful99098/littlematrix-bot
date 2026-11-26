"""Live stream + analysis pickers + premium-emoji audit.

Run: python tests/test_live_stream.py

a) The stream subscribes to every open asset, stores each closed
   1-min candle from the 'candle-generated' events, and survives a
   disconnect by reconnecting + tail-filling the gap.
b) AI Signal / Candle Scan / Trend Scan / Market Live ask the user
   which market first (asset picker), and run for the picked asset.
c) Every button in every keyboard carries a premium icon — the only
   raw InlineKeyboardButton constructions left are the two helpers
   (_btn / _url_btn) themselves.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config
from bot.candles_db import CandleDB, DHAKA
from bot import live_stream


class _StopLoop(BaseException):
    """Fake sleep raises this after N calls — it must propagate out
    of live_stream_loop untouched (BaseException, not Exception)."""


class _CountingSleep:
    def __init__(self, max_calls):
        self.calls = 0
        self.max_calls = max_calls

    async def __call__(self, _seconds):
        self.calls += 1
        if self.calls > self.max_calls:
            raise _StopLoop


EVENT_TS = int(datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc).timestamp())
TAIL_TS = EVENT_TS + 60


class FakeSession:
    """Fake Quotex session: scripted open assets, generated-candle
    events and connection drops. No login, no network."""

    def __init__(self, event_from_poll=1, connected_polls=None):
        self.connects = 0
        self.subscribed = []
        self.closed = 0
        self.polls = 0
        self.event_from_poll = event_from_poll
        # connected_polls=None -> stays connected; a number -> the
        # poll count after which is_connected() returns False.
        self.connected_polls = connected_polls

    async def connect(self):
        self.connects += 1
        return True

    async def get_open_assets(self):
        return ["BTCUSD_otc", "ETHUSD_otc"]

    async def start_asset_streams(self, assets, period=60):
        self.subscribed.append((list(assets), period))

    def generated_candles(self):
        if self.polls < self.event_from_poll:
            return {}
        return {"BTCUSD_otc": {60: {
            "asset": "BTCUSD_otc", "period": 60, "index": EVENT_TS,
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}}}

    async def is_connected(self):
        self.polls += 1
        if self.connected_polls is None:
            return True
        return self.polls <= self.connected_polls

    async def fetch_asset_span(self, asset, seconds_back):
        # Tail-fill: one candle, the minute after the stream event.
        return [{"time": TAIL_TS, "open": 100.5, "high": 101.5,
                 "low": 100.0, "close": 101.0}]

    async def close(self):
        self.closed += 1
        return None


def _tmp_db():
    tmp = tempfile.TemporaryDirectory()
    return CandleDB(database_url="", sqlite_path=Path(tmp.name) / "c.db").init(), tmp


def _candles_for(db, asset, date_str):
    return db.get_candles(asset, date_str)


def test_stream_stores_generated_candles():
    db, tmp = _tmp_db()
    session = FakeSession(event_from_poll=2)
    try:
        asyncio.run(live_stream.live_stream_loop(
            db=db, session_factory=lambda: session,
            sleep=_CountingSleep(max_calls=5)))
    except _StopLoop:
        pass
    else:
        raise AssertionError("loop ended without reaching the fake sleep")
    # Subscribed to every open asset, 1-min period.
    assert session.subscribed, session.subscribed
    assets, period = session.subscribed[0]
    assert assets == ["BTCUSD_otc", "ETHUSD_otc"], assets
    assert period == 60, period
    # The generated candle (12:00) AND the tail-fill candle (12:01)
    # are both in the DB.
    rows = _candles_for(db, "BTCUSD_otc", "2026-11-26")
    by_time = {r["time"]: r for r in rows}
    assert EVENT_TS in by_time, rows
    assert by_time[EVENT_TS]["C"] == 100.5, rows
    assert TAIL_TS in by_time, rows
    assert by_time[TAIL_TS]["C"] == 101.0, rows
    print("PASS a) stream stores generated candles for all open assets")


def test_stream_reconnects_and_tail_fills():
    db, tmp = _tmp_db()
    # Connected for 2 polls, then the connection drops.
    session = FakeSession(event_from_poll=1, connected_polls=2)
    try:
        asyncio.run(live_stream.live_stream_loop(
            db=db, session_factory=lambda: session,
            sleep=_CountingSleep(max_calls=6)))
    except _StopLoop:
        pass
    else:
        raise AssertionError("loop ended without reaching the fake sleep")
    # Reconnected at least once (the loop keeps going after a drop).
    assert session.connects >= 2, session.connects
    assert len(session.subscribed) >= 2, session.subscribed
    # The gap was tail-filled: the 12:01 candle is in the DB, once.
    rows = _candles_for(db, "BTCUSD_otc", "2026-11-26")
    assert [r["time"] for r in rows].count(TAIL_TS) == 1, rows
    print("PASS b) stream reconnects after a drop and tail-fills the gap")


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


class FakeQuery:
    def __init__(self):
        self.texts = []

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.texts.append((text, reply_markup))
        return None


def test_analysis_pickers_use_picked_asset():
    import bot.candles_db as candles_db_mod
    import bot.handlers as handlers

    db, tmp = _tmp_db()
    _load_today(db, "BTCUSD_otc")
    _load_today(db, "ETHUSD_otc")
    real_get_db = candles_db_mod.get_candle_db
    real_mode = config.DATA_MODE
    try:
        candles_db_mod.get_candle_db = lambda: db
        config.DATA_MODE = "db"
        # Picker lists OTC assets first, sorted, with an icon on
        # every button.
        assets = handlers._analysis_assets()
        assert assets == ["BTCUSD_otc", "ETHUSD_otc"], assets
        kb, page, total = handlers._analysis_picker_keyboard(assets, 0, "ai")
        assert total == 1 and page == 0
        buttons = [b for row in kb.inline_keyboard for b in row]
        assert any(b.callback_data == "an_asset_ai_ETHUSD_otc" for b in buttons)
        assert all(getattr(b, "icon_custom_emoji_id", None) for b in buttons), \
            [(b.text, b.callback_data) for b in buttons]
        # Each tool runs for the PICKED asset, not the default.
        q = FakeQuery()
        asyncio.run(handlers._run_ai_signal(q, object(), "ETHUSD_otc"))
        assert "AI SIGNAL — ETHUSD-OTC" in q.texts[-1][0], q.texts[-1][0]
        q = FakeQuery()
        asyncio.run(handlers._run_counter(q, "ETHUSD_otc"))
        assert "CANDLE SCAN — ETHUSD-OTC" in q.texts[-1][0], q.texts[-1][0]
        q = FakeQuery()
        asyncio.run(handlers._run_trend(q, "ETHUSD_otc"))
        assert "TREND SCAN — ETHUSD-OTC" in q.texts[-1][0], q.texts[-1][0]
        q = FakeQuery()
        asyncio.run(handlers._run_market(q, "ETHUSD_otc"))
        assert "Market Live — ETHUSD-OTC" in q.texts[-1][0], q.texts[-1][0]
        assert "Today: 60 candles" in q.texts[-1][0], q.texts[-1][0]
    finally:
        candles_db_mod.get_candle_db = real_get_db
        config.DATA_MODE = real_mode
    print("PASS c) analysis pickers run the picked market, not the default")


def test_bot_status_times_are_dhaka_and_stream_shown():
    """Bot Status times are Dhaka, not raw UTC: a stored 05:48 UTC
    last collection is 11:48 Dhaka. The live stream's own status is
    in the summary too (the 'Quotex session' line only covers the
    collector, which is disconnected between its 2-hourly runs)."""
    import bot.collector as collector

    db, tmp = _tmp_db()
    db.set_state("last_collection", "2026-11-26 05:48:35")  # UTC
    db.set_state("next_collection", "2026-11-26 07:48:35")  # UTC
    db.set_state("quotex_last_login", "2026-11-26 05:48:34")  # UTC
    db.set_state("quotex_session", "disconnected (between runs)")
    db.set_state("live_stream_status", "connected")
    db.set_state("live_stream_last_seen", "2026-11-26 06:54:14")  # UTC
    s = collector.get_status_summary(db)
    assert s["last_collection"] == "2026-11-26 11:48:35", s
    assert s["next_collection"] == "2026-11-26 13:48:35", s
    assert s["quotex_last_login"] == "2026-11-26 11:48:34", s
    assert s["quotex_session"] == "disconnected (between runs)", s
    assert s["live_stream_status"] == "connected", s
    assert s["live_stream_last_seen"] == "2026-11-26 12:54:14", s
    print("PASS e) Bot Status times are Dhaka, live stream status included")


def _all_buttons(keyboard):
    return [b for row in keyboard.inline_keyboard for b in row]


def test_every_button_has_premium_icon():
    import bot.handlers as handlers
    import bot.premium_emoji as pe_mod

    assert handlers._BTN_SUPPORTS_ICON, "PTB too old for button icons"
    # Main menu (21 buttons incl. Contact Owner), join gate, back.
    for kb in (handlers.main_keyboard(), handlers._join_keyboard(),
               handlers.back_keyboard()):
        for b in _all_buttons(kb):
            assert getattr(b, "icon_custom_emoji_id", None), \
                f"button without icon: {b.text!r} ({b.callback_data or b.url})"
    # Futures + analysis pickers (asset rows + nav + back).
    assets = [f"AAA{i}_otc" for i in range(13)]
    kb, _, _ = handlers._asset_picker_keyboard(assets, 0, "OTC FS")
    for b in _all_buttons(kb):
        assert getattr(b, "icon_custom_emoji_id", None), \
            f"futures picker button without icon: {b.text!r}"
    kb, _, _ = handlers._analysis_picker_keyboard(assets, 1, "trend")
    for b in _all_buttons(kb):
        assert getattr(b, "icon_custom_emoji_id", None), \
            f"analysis picker button without icon: {b.text!r}"
    # Every icon key resolves to a real ID from the owner's packs.
    for key, eid in pe_mod.BUTTON_ICONS.items():
        assert isinstance(eid, str) and eid.isdigit(), (key, eid)
    # No bare InlineKeyboardButton constructions left outside the
    # two helpers — every button in the bot goes through _btn/_url_btn.
    src = Path(__file__).resolve().parent.parent.joinpath(
        "bot", "handlers.py").read_text()
    assert src.count("InlineKeyboardButton(") == 2, \
        "raw InlineKeyboardButton( found outside _btn/_url_btn"
    print("PASS d) every button carries a premium icon")


if __name__ == "__main__":
    test_stream_stores_generated_candles()
    test_stream_reconnects_and_tail_fills()
    test_analysis_pickers_use_picked_asset()
    test_every_button_has_premium_icon()
    test_bot_status_times_are_dhaka_and_stream_shown()
    print("ALL LIVE-STREAM TESTS PASSED")
