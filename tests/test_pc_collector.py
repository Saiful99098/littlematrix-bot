"""Tests for the PC-side collector (collect_to_db.py).

Run: python tests/test_pc_collector.py

Must pass:
a) normalize_database_url — sslmode=require is added for Render's
   external Postgres URL, never duplicated, non-postgres URLs untouched.
b) run_once with a fake session + temp SQLite DB — backfill inserts
   5 days x 1440 + today 721 (noon, boundary candle included) per
   asset; a second run inserts 0 new (UNIQUE index); Bot-Status-style
   summary sees the candles.
c) run_once with a 429 fake session — retries once after 10 minutes,
   then returns False; nothing stored; no exception escapes.
d) run_once with a 403 fake session — the 403 is logged as one clear
   line, the run returns False, the loop stays alive (no raise).
e) main() refuses to run without DATABASE_URL or without Quotex
   credentials in .env (exit code 2) — it must never silently write
   to the local SQLite file when the Render DB URL is missing.

No real Quotex login is attempted anywhere here.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import collect_to_db
from bot import collector, config
from bot.candles_db import CandleDB, DHAKA
from bot.quotex_client import QuotexRateLimited

FIXED_NOW = datetime(2026, 11, 25, 12, 0, tzinfo=DHAKA)


def _tmp_db():
    tmp = tempfile.TemporaryDirectory()
    db = CandleDB(database_url="", sqlite_path=Path(tmp.name) / "candles.db").init()
    db._tmpdir = tmp  # keep the temp dir alive with the db
    return db


class FakeSession:
    """Fake Quotex session (same pattern as tests/test_collector_db.py):
    deterministic 1-min candles for every minute in the span."""

    def __init__(self, raise_429=False, now=None):
        self.raise_429 = raise_429
        self.now = now or datetime.now(DHAKA)
        self.connected = False

    async def connect(self):
        self.connected = True
        return True

    async def get_open_assets(self):
        return ["USDMXN_otc", "EURUSD_otc"]

    async def fetch_asset_span(self, asset, seconds_back):
        if self.raise_429:
            raise QuotexRateLimited("Quotex temporarily blocked us (429).")
        now = self.now
        start = now - timedelta(seconds=int(seconds_back))
        out = []
        t = start.replace(second=0, microsecond=0)
        i = 0
        while t <= now:
            base = 19.90 if asset == "USDMXN_otc" else 1.08
            o = base + (i % 10) * 0.001
            c = o + 0.001 if i % 2 == 0 else o - 0.001
            out.append({"time": int(t.timestamp()), "open": o,
                        "high": max(o, c) + 0.001, "low": min(o, c) - 0.001,
                        "close": c})
            t += timedelta(minutes=1)
            i += 1
        return out

    async def close(self):
        self.connected = False


class FakeSession403:
    """Fake session whose login fails with Quotex 403 (Render's error)."""

    def __init__(self, now=None):
        self.now = now or FIXED_NOW
        self.connected = False

    async def connect(self):
        raise RuntimeError("Quotex login failed: Error on access page: 403")

    async def get_open_assets(self):
        raise AssertionError("must not be reached after a failed login")

    async def fetch_asset_span(self, asset, seconds_back):
        raise AssertionError("must not be reached after a failed login")

    async def close(self):
        self.connected = False


def _fake_sleep_factory(calls):
    async def fake_sleep(seconds):
        calls.append(seconds)
    return fake_sleep


def test_normalize_database_url():
    assert collect_to_db.normalize_database_url("") == ""
    assert collect_to_db.normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"
    url = "postgres://u:p@host:5432/db"
    assert collect_to_db.normalize_database_url(url) == url + "?sslmode=require", \
        collect_to_db.normalize_database_url(url)
    # Already has sslmode: unchanged.
    url2 = "postgres://u:p@host:5432/db?sslmode=require"
    assert collect_to_db.normalize_database_url(url2) == url2
    # Other query params present: sslmode is appended, params kept.
    url3 = "postgres://u:p@host:5432/db?connect_timeout=10"
    got3 = collect_to_db.normalize_database_url(url3)
    assert "sslmode=require" in got3 and "connect_timeout=10" in got3, got3
    print("PASS a) normalize_database_url — sslmode added once, never duplicated")


def test_run_once_fake():
    db = _tmp_db()
    sleeps = []
    backfill_ok, inc_ok = asyncio.run(collect_to_db.run_once(
        db, session_factory=lambda: FakeSession(now=FIXED_NOW),
        sleep=_fake_sleep_factory(sleeps), now=FIXED_NOW))
    assert (backfill_ok, inc_ok) == (True, True), (backfill_ok, inc_ok)
    # 5 completed days x 1440 + today 00:00-12:00 (721) = 7921 per asset.
    assert db.count_total() == 2 * 7921, db.count_total()
    assert db.get_dates() == ["2026-11-20", "2026-11-21", "2026-11-22",
                              "2026-11-23", "2026-11-24", "2026-11-25"]
    assert len(db.get_candles("USDMXN_otc", "2026-11-22")) == 1440
    assert db.get_state("backfill_done") == "true"
    # Bot-Status-style summary sees the same candles.
    s = collector.get_status_summary(db, now=FIXED_NOW)
    assert s["total_candles"] == 2 * 7921, s
    assert s["today_count"] == 2 * 720, s
    # Second identical run: backfill skipped (done today), incremental
    # finds nothing new — 0 new candles, no extra sleeps.
    backfill_ok2, inc_ok2 = asyncio.run(collect_to_db.run_once(
        db, session_factory=lambda: FakeSession(now=FIXED_NOW),
        sleep=_fake_sleep_factory(sleeps), now=FIXED_NOW))
    assert (backfill_ok2, inc_ok2) == (True, True), (backfill_ok2, inc_ok2)
    assert db.count_total() == 2 * 7921, db.count_total()
    assert sleeps == [], sleeps
    print("PASS b) run_once (fake session) — 7921/asset in DB, re-run 0 new")


def test_run_once_429_retries_once():
    db = _tmp_db()
    sleeps = []
    backfill_ok, inc_ok = asyncio.run(collect_to_db.run_once(
        db, session_factory=lambda: FakeSession(raise_429=True, now=FIXED_NOW),
        sleep=_fake_sleep_factory(sleeps), now=FIXED_NOW))
    assert (backfill_ok, inc_ok) == (False, False), (backfill_ok, inc_ok)
    # One 10-minute retry sleep for the backfill, one for the incremental.
    assert sleeps == [600, 600], sleeps
    assert db.count_total() == 0
    assert db.get_state("backfill_done") is None
    print("PASS c) run_once 429 — retried once after 10 min, nothing stored")


def test_run_once_403_logged_not_crashing():
    db = _tmp_db()
    records = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    pc_logger = logging.getLogger("littlematrix.pc_collector")
    handler = ListHandler()
    pc_logger.addHandler(handler)
    pc_logger.setLevel(logging.INFO)
    try:
        backfill_ok, inc_ok = asyncio.run(collect_to_db.run_once(
            db, session_factory=lambda: FakeSession403(now=FIXED_NOW),
            sleep=_fake_sleep_factory([]), now=FIXED_NOW))
    finally:
        pc_logger.removeHandler(handler)
    # No exception escaped; both passes report failure honestly.
    assert (backfill_ok, inc_ok) == (False, False), (backfill_ok, inc_ok)
    assert db.count_total() == 0
    # The 403 got its own clear one-line explanation (not a bare traceback).
    joined = "\n".join(records)
    assert "403" in joined, records
    assert "quotex.com" in joined, records
    assert "next cycle" in joined, records
    print("PASS d) run_once 403 — logged clearly, loop stays alive, no crash")


def test_main_refuses_without_config():
    real_url = config.DATABASE_URL
    real_email = config.QUOTEX_EMAIL
    real_password = config.QUOTEX_PASSWORD
    try:
        # No DATABASE_URL: exit 2, even with Quotex credentials present.
        config.DATABASE_URL = ""
        config.QUOTEX_EMAIL = "pc@test.example"
        config.QUOTEX_PASSWORD = "dummy"
        assert collect_to_db.main(["--once"]) == 2
        # No Quotex credentials: exit 2, even with a DATABASE_URL present.
        config.DATABASE_URL = "postgres://u:p@host:5432/db"
        config.QUOTEX_EMAIL = ""
        config.QUOTEX_PASSWORD = ""
        assert collect_to_db.main(["--once"]) == 2
    finally:
        config.DATABASE_URL = real_url
        config.QUOTEX_EMAIL = real_email
        config.QUOTEX_PASSWORD = real_password
    print("PASS e) main() refuses without DATABASE_URL / without Quotex login")


def main():
    test_normalize_database_url()
    test_run_once_fake()
    test_run_once_429_retries_once()
    test_run_once_403_logged_not_crashing()
    test_main_refuses_without_config()
    print("ALL TESTS PASSED (5/5)")


if __name__ == "__main__":
    main()
