"""Free-user daily limits + per-tier Bot ON/OFF + premium plans
(the owner, 2026-11-28).

Run: python tests/test_limits_premium.py

a) Usage get/increment round-trip.
b) _free_feature_allowed: a free user is blocked after 3 uses of
   one feature, a different feature is still allowed, and a premium
   user is unlimited.
c) Fix 8: _bot_on_for — admin always True, a free user is False
   when bot_on_free is "0", a premium user is False when
   bot_on_premium is "0"; the dashboard toggle flips the state.
d) Fix 9: save_premium_request stores the plan days and
   get_premium_request returns them; grant_premium with those days
   sets the right expiry.
e) My Status limits block: a free user sees every limited
   feature's uses today plus Auto Live minutes; a premium user
   gets an empty string (unlimited).
f) Admin exemption (the owner, 2026-11-28): the owner has NO
   limits — always allowed, never counted, no limits block.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot import config, database
from bot.candles_db import CandleDB


def _setup_users():
    old_url, old_path = config.DATABASE_URL, config.DB_PATH
    config.DATABASE_URL = ""
    config.DB_PATH = Path(tempfile.mkdtemp()) / "users.db"
    database._CONN = None

    def restore():
        config.DATABASE_URL, config.DB_PATH = old_url, old_path
        database._CONN = None

    return restore


def _tmp_candle_db():
    return CandleDB(database_url="",
                    sqlite_path=Path(tempfile.mkdtemp()) / "c.db").init()


def _setup_candles(db):
    """Patch handlers to use the temp candle DB — WITHOUT neutering
    get_or_create_user (the bot-off gate needs the real users DB)."""
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


def test_usage_round_trip():
    """a) get_usage starts at 0; increment_usage counts up and
    returns the new count."""
    restore = _setup_users()
    try:
        today = "2026-11-28"
        assert database.get_usage(123, "ai_signal", today) == 0
        assert database.increment_usage(123, "ai_signal", today) == 1
        assert database.increment_usage(123, "ai_signal", today) == 2
        assert database.increment_usage(123, "ai_signal", today, 5) == 7
        assert database.get_usage(123, "ai_signal", today) == 7
        # A different feature / day is a separate counter.
        assert database.get_usage(123, "news", today) == 0
        assert database.get_usage(123, "ai_signal", "2026-11-27") == 0
    finally:
        restore()
    print("PASS a) usage get/increment round-trip")


def test_free_feature_allowed():
    """b) A free user is blocked after 3 uses of one feature, a
    different feature is still allowed, and a premium user is
    unlimited (and never counted)."""
    import bot.handlers as handlers
    restore = _setup_users()
    try:
        today = handlers._today_date_str()
        assert handlers._free_feature_allowed(123, "ai_signal") is True
        for _ in range(3):
            handlers._free_feature_use(123, "ai_signal")
        assert database.get_usage(123, "ai_signal", today) == 3
        assert handlers._free_feature_allowed(123, "ai_signal") is False
        # A different feature is untouched.
        assert handlers._free_feature_allowed(123, "news") is True
        handlers._free_feature_use(123, "news")
        assert database.get_usage(123, "news", today) == 1
        assert handlers._free_feature_allowed(123, "news") is True
        # Premium: unlimited, and uses are never counted.
        database.grant_premium(124, days=30)
        for _ in range(5):
            handlers._free_feature_use(124, "ai_signal")
        assert database.get_usage(124, "ai_signal", today) == 0
        assert handlers._free_feature_allowed(124, "ai_signal") is True
        # The limit text carries the exact phrase.
        assert "Your limit is reached" in handlers._limit_text()
    finally:
        restore()
    print("PASS b) free user blocked after 3 uses; premium unlimited")


def test_bot_on_off_per_tier():
    """c) Fix 8: the owner is always on; a free user follows
    bot_on_free, a premium user follows bot_on_premium; the
    dashboard toggle flips the stored state."""
    import bot.handlers as handlers
    from tests.test_multiselect import FakeContext, FakeQuery, _button, _drive
    from bot import config as config_mod
    db = _tmp_candle_db()
    restore = _setup_candles(db)
    restore_users = _setup_users()
    old_admins = config_mod.ADMIN_USER_IDS
    try:
        config_mod.ADMIN_USER_IDS = {999}
        database.grant_premium(124, days=30)
        # Both switches ON: everyone is on.
        assert handlers._bot_on_for(999) is True
        assert handlers._bot_on_for(123) is True
        assert handlers._bot_on_for(124) is True
        # Free OFF: only the free user is off; premium + admin on.
        db.set_state("bot_on_free", "0")
        assert handlers._bot_on_for(123) is False
        assert handlers._bot_on_for(124) is True
        assert handlers._bot_on_for(999) is True
        assert handlers._bot_off_text(123) == (
            "The Blue Fish Prime was currently off for free user.")
        # Premium OFF: only the premium user is off.
        db.set_state("bot_on_free", "1")
        db.set_state("bot_on_premium", "0")
        assert handlers._bot_on_for(123) is True
        assert handlers._bot_on_for(124) is False
        assert handlers._bot_on_for(999) is True
        assert handlers._bot_off_text(124) == (
            "The Blue Fish Prime was currently off for premium user.")
        db.set_state("bot_on_premium", "1")
        # The dashboard toggle flips the stored state (admin only).
        ctx = FakeContext()
        q = FakeQuery("admin_bot_free")
        q.message.chat_id = 999  # _run_admin_dashboard needs it
        _drive(ctx, q, "admin_bot_free")
        assert db.get_state("bot_on_free") == "0"
        assert _button(q, "admin_bot_free").text == "Bot ON for Free Users: OFF"
        _drive(ctx, q, "admin_bot_free")
        assert db.get_state("bot_on_free") == "1"
        assert _button(q, "admin_bot_free").text == "Bot ON for Free Users: ON"
        _drive(ctx, q, "admin_bot_premium")
        assert db.get_state("bot_on_premium") == "0"
        assert _button(q, "admin_bot_premium").text == "Bot ON for Premium Users: OFF"
    finally:
        config_mod.ADMIN_USER_IDS = old_admins
        restore()
        restore_users()
    print("PASS c) Fix 8: bot ON/OFF per tier + dashboard toggle")


def test_premium_plan_days():
    """d) Fix 9: the request stores the plan days; approve grants
    exactly those days (not a hardcoded 30)."""
    restore = _setup_users()
    try:
        req_id = database.save_premium_request(123, "TXN5", days=5)
        req = database.get_premium_request(req_id)
        assert req["days"] == 5, req
        assert req["txn_hash"] == "TXN5"
        # No days given: the old default (30) still applies.
        req2_id = database.save_premium_request(123, "TXN30")
        assert database.get_premium_request(req2_id)["days"] == 30
        pending = database.get_pending_premium_requests()
        by_txn = {r["txn_hash"]: r["days"] for r in pending}
        assert by_txn["TXN5"] == 5 and by_txn["TXN30"] == 30, pending
        # grant_premium(5) expires ~5 days from now, not 30.
        # (2s slack: grant_premium truncates the ISO string to
        # whole seconds, so the stored value can sit just under
        # the exact now+5d.)
        before = datetime.now()
        until = database.grant_premium(123, days=5)
        after = datetime.now()
        got = datetime.fromisoformat(until)
        assert (before + timedelta(days=5) - timedelta(seconds=2)
                <= got <=
                after + timedelta(days=5) + timedelta(seconds=2)), until
        assert database.is_premium(123) is True
    finally:
        restore()
    print("PASS d) Fix 9: premium request stores days; grant uses them")


def test_today_limits_text():
    """e) My Status limits block: free user sees all 10 features
    (used/3) + Auto Live minutes (used/30); premium gets ""."""
    import bot.handlers as handlers
    restore = _setup_users()
    try:
        today = handlers._today_date_str()
        # Fresh free user: all zeros.
        text = handlers._today_limits_text(123)
        assert text.startswith("Today's limits:"), text
        for label in ("AI Signal", "Market Live", "Payouts",
                      "Candle Scan", "Trend Scan", "Backtest Lab",
                      "News Desk", "Swap C/P", "Signal Formatter",
                      "Signal Filter"):
            assert f"{label}: 0/3 used" in text, (label, text)
        assert "Auto Live: 0/30 min used" in text, text
        # After some uses the numbers move.
        handlers._free_feature_use(123, "ai_signal")
        handlers._free_feature_use(123, "ai_signal")
        database.increment_usage(123, "auto_live", today, 5)
        text = handlers._today_limits_text(123)
        assert "AI Signal: 2/3 used" in text, text
        assert "Auto Live: 5/30 min used" in text, text
        assert "News Desk: 0/3 used" in text, text
        # Premium: no block at all (unlimited).
        database.grant_premium(124, days=30)
        assert handlers._today_limits_text(124) == ""
    finally:
        restore()
    print("PASS e) My Status limits block: free sees uses, premium sees none")


def test_admin_has_no_limits():
    """f) Admin exemption: the owner (ADMIN_USER_IDS) is always
    allowed, uses are never counted, and My Status shows no
    limits block — even though the owner is NOT premium."""
    import bot.handlers as handlers
    from bot import config as config_mod
    restore = _setup_users()
    old_admins = config_mod.ADMIN_USER_IDS
    try:
        config_mod.ADMIN_USER_IDS = {999}
        today = handlers._today_date_str()
        # The owner is not premium in the DB…
        assert database.is_premium(999) is False
        # …yet is always allowed, even after 5 uses of a feature.
        for _ in range(5):
            handlers._free_feature_use(999, "ai_signal")
        assert database.get_usage(999, "ai_signal", today) == 0
        assert handlers._free_feature_allowed(999, "ai_signal") is True
        assert handlers._free_feature_allowed(999, "news") is True
        # And My Status shows no limits block for the owner.
        assert handlers._today_limits_text(999) == ""
        # A non-admin free user is still limited (sanity check).
        for _ in range(3):
            handlers._free_feature_use(123, "ai_signal")
        assert handlers._free_feature_allowed(123, "ai_signal") is False
        assert handlers._today_limits_text(123) != ""
    finally:
        config_mod.ADMIN_USER_IDS = old_admins
        restore()
    print("PASS f) admin has no limits: always allowed, never counted")


if __name__ == "__main__":
    test_usage_round_trip()
    test_free_feature_allowed()
    test_bot_on_off_per_tier()
    test_premium_plan_days()
    test_today_limits_text()
    test_admin_has_no_limits()
    print("ALL LIMITS/PREMIUM TESTS PASSED")
