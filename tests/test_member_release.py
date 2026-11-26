"""Tests for the member release (2026-11-26): channel gate, backfill
tail-fetch, payouts snapshot, Postgres/SQLite users DB, news calendar,
and the DB-backed analysis tools. Run: python tests/test_member_release.py
"""
from __future__ import annotations

import asyncio
import io
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot import candles_db as candles_db_mod
from bot import collector, config, database, handlers, news
from bot.candles_db import CandleDB, DHAKA, UTC
from synthetic_data import force_candle, make_day, to_raw


def _tmp_candle_db():
    return CandleDB(sqlite_path=Path(tempfile.mkdtemp()) / "c.db").init()


# ---------------------------------------------------------------------------
# Channel gate
# ---------------------------------------------------------------------------
class FakeBot:
    def __init__(self, status="member", fail=False):
        self.status = status
        self.fail = fail
        self.calls = 0

    async def get_chat_member(self, chat_id, user_id):
        self.calls += 1
        if self.fail:
            raise RuntimeError("Forbidden: bot is not a member of the chat")
        m = type("M", (), {"status": self.status})()
        return m


def test_channel_gate():
    old = config.REQUIRED_CHANNEL
    try:
        # Gate off: everyone may use the bot.
        config.REQUIRED_CHANNEL = ""
        assert asyncio.run(handlers._is_channel_member(FakeBot("left"), 1)) is True
        # Gate on: member / admin / creator pass; left / kicked do not.
        config.REQUIRED_CHANNEL = "@BlueFishTest"
        handlers._member_cache.clear()
        assert asyncio.run(handlers._is_channel_member(FakeBot("member"), 1)) is True
        handlers._member_cache.clear()
        assert asyncio.run(handlers._is_channel_member(FakeBot("administrator"), 1)) is True
        handlers._member_cache.clear()
        assert asyncio.run(handlers._is_channel_member(FakeBot("left"), 1)) is False
        handlers._member_cache.clear()
        assert asyncio.run(handlers._is_channel_member(FakeBot("kicked"), 1)) is False
        # A check that itself fails (bot not admin) fails OPEN.
        handlers._member_cache.clear()
        assert asyncio.run(handlers._is_channel_member(FakeBot(fail=True), 1)) is True
        # Cache: the second call for the same user does not hit Telegram.
        handlers._member_cache.clear()
        bot = FakeBot("member")
        asyncio.run(handlers._is_channel_member(bot, 7))
        asyncio.run(handlers._is_channel_member(bot, 7))
        assert bot.calls == 1, bot.calls
        # Join URL from the channel name.
        assert handlers._channel_join_url() == "https://t.me/BlueFishTest"
    finally:
        config.REQUIRED_CHANNEL = old
        handlers._member_cache.clear()
    print("PASS 1) channel gate — off/members pass, left/kicked blocked, "
          "failed check fails open, 10-min cache")


# ---------------------------------------------------------------------------
# Backfill tail-fetch
# ---------------------------------------------------------------------------
def test_tail_seconds_back():
    db = _tmp_candle_db()
    now = datetime(2026, 11, 26, 11, 0, tzinfo=DHAKA)
    oldest = datetime(2026, 11, 21, 0, 0, tzinfo=DHAKA).astimezone(UTC)
    full = collector.fetch_seconds_back(oldest, now)
    # No candles at all -> full span.
    assert collector._tail_seconds_back(db, "BTCUSD_otc", oldest, now) == full
    # Candles up to 2h ago -> tail only (~2h + 10 min overlap).
    d = make_day("BTCUSD_otc", "2026-11-26", seed=3)
    rows = collector.rows_from_raw(to_raw(d))
    # Keep only candles up to 09:00 Dhaka (03:00 UTC) — last stored 09:00.
    rows = [r for r in rows if r["ts"] <= datetime(2026, 11, 26, 3, 0, tzinfo=UTC)]
    db.insert_candles("BTCUSD_otc", rows)
    tail = collector._tail_seconds_back(db, "BTCUSD_otc", oldest, now)
    assert 2 * 3600 < tail < 3 * 3600, tail
    assert tail < full // 2, (tail, full)
    # Last candle older than the window -> full span again.
    db2 = _tmp_candle_db()
    d_old = make_day("BTCUSD_otc", "2026-11-15", seed=4)
    db2.insert_candles("BTCUSD_otc", collector.rows_from_raw(to_raw(d_old)))
    assert collector._tail_seconds_back(db2, "BTCUSD_otc", oldest, now) == full
    print("PASS 2) backfill tail-fetch — no data/full span, recent data/tail only, "
          "stale data/full span")


# ---------------------------------------------------------------------------
# Payouts snapshot
# ---------------------------------------------------------------------------
def test_payouts_snapshot():
    db = _tmp_candle_db()
    assert db.get_payouts() == []
    db.set_payouts([("EURUSD_otc", 91.0, True, "2026-11-26 11:00:00"),
                    ("BTCUSD_otc", 85.0, True, "2026-11-26 11:00:00"),
                    ("XAUUSD", 0.0, False, "2026-11-26 11:00:00")])
    rows = db.get_payouts()
    assert [r[0] for r in rows] == ["EURUSD_otc", "BTCUSD_otc", "XAUUSD"], rows
    assert rows[0][1] == 91.0 and rows[2][2] is False
    # A new snapshot replaces the old rows for the same asset.
    db.set_payouts([("BTCUSD_otc", 88.0, True, "2026-11-26 13:00:00")])
    rows = db.get_payouts()
    btc = [r for r in rows if r[0] == "BTCUSD_otc"][0]
    assert btc[1] == 88.0 and btc[3] == "2026-11-26 13:00:00", btc
    print("PASS 3) payouts snapshot — store, best-first order, replace on re-store")


# ---------------------------------------------------------------------------
# Users DB (SQLite path) — premium + auto signals
# ---------------------------------------------------------------------------
def test_users_db_sqlite():
    old_url, old_path = config.DATABASE_URL, config.DB_PATH
    try:
        config.DATABASE_URL = ""
        config.DB_PATH = Path(tempfile.mkdtemp()) / "users.db"
        database._CONN = None
        u = database.get_or_create_user(123, "tester")
        assert u["premium_until"] is None
        assert database.is_premium(123) is False
        until = database.grant_premium(123, days=30)
        assert database.is_premium(123) is True
        assert database.get_or_create_user(123)["premium_until"] == until
        # Auto signals subscribe / list.
        assert database.get_auto_subscribers() == []
        database.set_auto_signals(123, True)
        assert database.is_auto_subscriber(123) is True
        assert database.get_auto_subscribers() == [123]
        database.set_auto_signals(123, False)
        assert database.get_auto_subscribers() == []
        # Premium request round-trip + approve.
        req_id = database.save_premium_request(123, "abc123hash")
        pending = database.get_pending_premium_requests()
        assert len(pending) == 1 and pending[0]["id"] == req_id
        assert pending[0]["txn_hash"] == "abc123hash"
        database.set_premium_request_status(req_id, "approved")
        assert database.get_pending_premium_requests() == []
        assert database.get_premium_request(req_id)["status"] == "approved"
    finally:
        config.DATABASE_URL, config.DB_PATH = old_url, old_path
        database._CONN = None
    print("PASS 4) users DB — create, premium grant, auto-signal toggle, "
          "premium request approve flow")


# ---------------------------------------------------------------------------
# News calendar (ET -> Dhaka conversion + XML parse)
# ---------------------------------------------------------------------------
_CANNED_XML = """<?xml version="1.0" encoding="windows-1252"?>
<weeklyevents>
<event><title>Core Retail Sales q/q</title><country>NZD</country>
<date><![CDATA[11-22-2026]]></date><time><![CDATA[9:45pm]]></time>
<impact><![CDATA[Low]]></impact><forecast><![CDATA[0.8%]]></forecast>
<previous><![CDATA[0.7%]]></previous></event>
<event><title>Fed Chair Speech</title><country>USD</country>
<date><![CDATA[11-23-2026]]></date><time><![CDATA[10:00am]]></time>
<impact><![CDATA[High]]></impact><forecast><![CDATA[]]></forecast>
<previous><![CDATA[]]></previous></event>
</weeklyevents>"""


def test_news_calendar():
    # Nov 22 2026 is EST (DST ended Nov 1): 9:45pm ET + 11h = Nov 23 08:45 Dhaka.
    assert news._et_to_dhaka("11-22-2026", "9:45pm") == "2026-11-23 08:45"
    # Jul 2026 is EDT: 10:00am ET + 10h = 20:00 Dhaka.
    assert news._et_to_dhaka("07-15-2026", "10:00am") == "2026-07-15 20:00"
    assert news._et_to_dhaka("11-22-2026", "All Day") is None
    # Parse a canned feed through the real fetch path (urlopen mocked).
    real_urlopen = news.urllib.request.urlopen
    try:
        news.urllib.request.urlopen = lambda req, timeout=10: io.BytesIO(
            _CANNED_XML.encode("utf-8"))
        news._cache = {"ts": 0.0, "events": None}
        events = news.fetch_calendar_events()
        assert len(events) == 2, events
        assert events[0]["title"] == "Core Retail Sales q/q"
        assert events[0]["dhaka"] == "2026-11-23 08:45"
        # Nov 23 (Dhaka) has the Fed speech (High) first.
        got = news.events_for_dhaka_dates({"2026-11-23"})
        assert [e["title"] for e in got] == ["Fed Chair Speech", "Core Retail Sales q/q"], got
    finally:
        news.urllib.request.urlopen = real_urlopen
        news._cache = {"ts": 0.0, "events": None}
    print("PASS 5) news calendar — ET->Dhaka (EST +11h, EDT +10h), XML parse, "
          "High impact first")


# ---------------------------------------------------------------------------
# DB-backed analysis tools
# ---------------------------------------------------------------------------
def test_analysis_helpers():
    db = _tmp_candle_db()
    real_get_db = candles_db_mod.get_candle_db
    try:
        candles_db_mod.get_candle_db = lambda: db
        # Empty DB -> no asset.
        assert handlers._default_analysis_asset() is None
        # Today: BTCUSD_otc with a full day -> preferred asset.
        today = handlers._today_date_str()
        d = make_day("BTCUSD_otc", today, seed=5)
        db.insert_candles("BTCUSD_otc", collector.rows_from_raw(to_raw(d)))
        assert handlers._default_analysis_asset() == "BTCUSD_otc"
        recent = handlers._db_recent_candles("BTCUSD_otc", 10)
        assert len(recent) == 10, len(recent)
        assert recent[-1]["datetime"] == f"{today} 23:59:00"
        assert recent[-1]["close"] == d[-1]["close"]
    finally:
        candles_db_mod.get_candle_db = real_get_db
    print("PASS 6) analysis helpers — default asset (BTC first), recent candles")


def test_contact_button_and_join_link():
    # Main menu has a Contact Owner button linking to @LittleMatrix.
    urls = [b.url for row in handlers.MAIN_MENU for b in row
            if getattr(b, "url", None)]
    assert "https://t.me/LittleMatrix" in urls, urls
    # The Join button prefers the invite link over t.me/<channel>.
    old_ch, old_inv = config.REQUIRED_CHANNEL, config.CHANNEL_INVITE_LINK
    try:
        config.REQUIRED_CHANNEL = "@BlueFishTest"
        config.CHANNEL_INVITE_LINK = "https://t.me/+HDiWRJothc5hNTQ9"
        assert handlers._channel_join_url() == "https://t.me/+HDiWRJothc5hNTQ9"
        config.CHANNEL_INVITE_LINK = ""
        assert handlers._channel_join_url() == "https://t.me/BlueFishTest"
        config.REQUIRED_CHANNEL = ""
        assert handlers._channel_join_url() is None
    finally:
        config.REQUIRED_CHANNEL, config.CHANNEL_INVITE_LINK = old_ch, old_inv
    # No owner name anywhere in the bot's user-facing strings.
    import bot.handlers as h
    _name = "Sai" + "ful"  # built, not written out, so the word is absent
    assert _name not in h.DISCLAIMER and _name not in h.CONTACT_LINE
    print("PASS 7) Contact Owner button (@LittleMatrix), invite link preferred "
          "for Join, no owner name in bot strings")


def main():
    test_channel_gate()
    test_tail_seconds_back()
    test_payouts_snapshot()
    test_users_db_sqlite()
    test_news_calendar()
    test_analysis_helpers()
    test_contact_button_and_join_link()
    print("ALL TESTS PASSED (7/7)")


if __name__ == "__main__":
    main()
