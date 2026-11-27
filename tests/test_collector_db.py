"""Tests for the Render phase: candle DB, collector windows, DB signals,
DB checker, webhook branch. Run: python tests/test_collector_db.py

Must pass:
a) window math — 5 backfill days, each a full Dhaka day = 1440 minutes.
b) DB round-trip on SQLite — insert, UNIQUE blocks duplicates, load by
   asset+date returns the right candles.
b2) DB recovers after the connection is closed (Render's Postgres
   closes idle connections) — the next read/write reconnects once.
b3) DB asset names are case-insensitive: candles stored as
   'BTCUSD_otc' are found when the checker asks for 'BTCUSD-OTC'
   (pasted signal form) — the 2026-11-25 all-NO-DATA bug.
c) signals FROM the DB (synthetic USDMXN 2026-11-21 loaded in) give the
   same 16 signals for 09:00-10:59 as tests/test_signal.py.
d) checker lookup from the DB returns the right result.
e) webhook mode branches on WEBHOOK_URL (no real bind, no real login).
f) collector backfill with a fake session inserts 1440/day/asset in the
   window; a second run inserts 0 new; a 429 stops the run.
g) backfill resumes after a mid-run crash: assets stored before the
   crash stay in the DB, the next run skips them and finishes the rest.

The old bundled sample file is deleted — tests c/d/h now load
deterministic synthetic days from tests/synthetic_data.py.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot import collector, config
from bot.candles_db import CandleDB, DHAKA, dhaka_date_range_utc, utc_iso
from bot.checker import build_lookup, check_signals
from bot.collector import (
    backfill_dates,
    day_window_utc,
    fetch_seconds_back,
    recent_window_utc,
    rows_from_raw,
    run_backfill,
    run_incremental,
    today_window_utc,
)
from bot.quotex_client import QuotexRateLimited
from bot.signal_engine import generate_futures_signals_from_db
from synthetic_data import force_candle, make_day, to_raw

ROOT = Path(__file__).resolve().parent.parent

# Same regression list as tests/test_signal.py (synthetic 2026-11-21,
# seed 8, window 09:00-10:59, 1-day vote).
EXPECTED_16 = [
    ("09:00", "CALL"), ("09:07", "PUT"), ("09:14", "CALL"),
    ("09:21", "PUT"), ("09:28", "CALL"), ("09:35", "CALL"),
    ("09:43", "PUT"), ("09:50", "PUT"), ("09:58", "PUT"),
    ("10:05", "PUT"), ("10:13", "CALL"), ("10:22", "PUT"),
    ("10:30", "PUT"), ("10:39", "CALL"), ("10:47", "CALL"),
    ("10:57", "CALL"),
]
# 2-day vote (2026-11-20 seed 7 + 2026-11-21 seed 8), same window.
EXPECTED_2DAY_14 = [
    ("09:00", "CALL"), ("09:07", "PUT"), ("09:14", "CALL"),
    ("09:21", "PUT"), ("09:28", "CALL"), ("09:38", "PUT"),
    ("09:47", "PUT"), ("09:54", "CALL"), ("10:10", "PUT"),
    ("10:18", "PUT"), ("10:30", "PUT"), ("10:41", "PUT"),
    ("10:49", "PUT"), ("10:57", "CALL"),
]


def _tmp_db():
    tmp = tempfile.TemporaryDirectory()
    db = CandleDB(database_url="", sqlite_path=Path(tmp.name) / "candles.db").init()
    db._tmpdir = tmp  # keep the temp dir alive with the db
    return db


def test_window_math():
    now = datetime(2026, 11, 25, 15, 30, tzinfo=DHAKA)
    dates = backfill_dates(5, now)
    assert dates == ["2026-11-24", "2026-11-23", "2026-11-22",
                     "2026-11-21", "2026-11-20"], dates
    for d in dates:
        start, end = day_window_utc(d)
        assert (end - start) == timedelta(hours=24), (d, end - start)
        assert start.astimezone(DHAKA).strftime("%H:%M") == "00:00", d
        assert end.astimezone(DHAKA).strftime("%H:%M") == "00:00", d
    # 1440 one-minute candles per full day in the window.
    assert int((day_window_utc(dates[0])[1]
                - day_window_utc(dates[0])[0]).total_seconds() // 60) == 1440
    # today window ends at now when now is inside the day (morning).
    morning = datetime(2026, 11, 25, 9, 30, tzinfo=DHAKA)
    s, e = today_window_utc(morning)
    assert e == morning.astimezone(timezone.utc), (e, morning)
    assert s.astimezone(DHAKA).strftime("%H:%M") == "00:00"
    # ... and in the afternoon too (the old 12:00 cap is gone).
    afternoon = datetime(2026, 11, 25, 15, 0, tzinfo=DHAKA)
    s, e = today_window_utc(afternoon)
    assert e == afternoon.astimezone(timezone.utc), (e, afternoon)
    # recent window: last interval (30 min) + 10 min overlap.
    s, e = recent_window_utc(now)
    assert (e - s) == timedelta(minutes=40), (e - s)
    # explicit hours still override the interval default.
    s, e = recent_window_utc(now, hours=2)
    assert (e - s) == timedelta(hours=2, minutes=10), (e - s)
    # fetch seconds: from now back to the oldest backfill day 00:00 Dhaka.
    oldest_start, _ = day_window_utc(dates[-1])
    secs = fetch_seconds_back(oldest_start, now)
    assert secs > 5 * 24 * 3600 and secs < 6 * 24 * 3600, secs
    print("PASS a) window math — 5 backfill days, 1440 min/day, 30m+10m window")


def test_db_roundtrip():
    db = _tmp_db()
    rows = [
        {"ts": datetime(2026, 11, 21, 3, 2, tzinfo=DHAKA), "open": 19.9,
         "high": 19.95, "low": 19.88, "close": 19.93},
        {"ts": datetime(2026, 11, 21, 3, 3, tzinfo=DHAKA), "open": 19.93,
         "high": 19.96, "low": 19.90, "close": 19.91},
    ]
    assert db.insert_candles("USDMXN_otc", rows) == 2
    # Same rows again: UNIQUE(asset, ts) blocks duplicates.
    assert db.insert_candles("USDMXN_otc", rows) == 0
    assert db.count_total() == 2
    got = db.get_candles("USDMXN_otc", "2026-11-21")
    assert len(got) == 2, got
    # Dhaka datetimes come back in raw-like dict format.
    assert got[0]["datetime"] == "2026-11-21 03:02:00", got[0]
    assert got[0]["O"] == 19.9 and got[0]["C"] == 19.93
    assert got[0]["time"] == int(datetime(2026, 11, 21, 3, 2,
                                          tzinfo=DHAKA).timestamp())
    assert db.get_candle("USDMXN_otc", "2026-11-21", "03:03")["C"] == 19.91
    assert db.get_candle("USDMXN_otc", "2026-11-21", "03:04") is None
    assert db.get_dates("USDMXN_otc") == ["2026-11-21"]
    assert db.get_assets() == ["USDMXN_otc"]
    # Collector state round-trip.
    db.set_state("last_collection", "2026-11-25 09:00:00")
    assert db.get_state("last_collection") == "2026-11-25 09:00:00"
    assert db.get_state("missing_key") is None
    print("PASS b) DB round-trip — insert, UNIQUE blocks dupes, load by date")


def test_db_recovers_after_connection_closed():
    db = _tmp_db()
    rows = [
        {"ts": datetime(2026, 11, 21, 3, 2, tzinfo=DHAKA), "open": 19.9,
         "high": 19.95, "low": 19.88, "close": 19.93},
        {"ts": datetime(2026, 11, 21, 3, 3, tzinfo=DHAKA), "open": 19.93,
         "high": 19.96, "low": 19.90, "close": 19.91},
    ]
    assert db.insert_candles("USDMXN_otc", rows) == 2
    # Simulate Render's Postgres closing the idle connection: the
    # connection object is closed but still referenced by the DB.
    db.connect().close()
    # The next read must reconnect once and return the right result
    # (this is the Bot Status crash: InterfaceError on count_total).
    assert db.count_total() == 2
    got = db.get_candles("USDMXN_otc", "2026-11-21")
    assert len(got) == 2, got
    # Writes must recover the same way (executemany path).
    db.connect().close()
    assert db.insert_candles("USDMXN_otc", rows) == 0  # dupes ignored
    assert db.count_total() == 2
    print("PASS b2) DB recovers after the connection is closed — "
          "reconnects once, reads and writes work")


def test_db_asset_name_case_insensitive():
    # 2026-11-25: CHECKER on BTCUSD-OTC showed all NO DATA even though
    # Bot Status showed 4319 BTCUSD candles. Cause: the DB stores
    # 'BTCUSD_otc' (lower _otc) but the checker asked for 'BTCUSD-OTC'
    # (all upper) and WHERE asset = ? is case-sensitive.
    db = _tmp_db()
    rows = [
        {"ts": datetime(2026, 11, 25, 9, 6, tzinfo=DHAKA), "open": 50000,
         "high": 50010, "low": 49990, "close": 49990},   # PUT
        {"ts": datetime(2026, 11, 25, 9, 20, tzinfo=DHAKA), "open": 49990,
         "high": 50000, "low": 49980, "close": 50000},   # CALL
        {"ts": datetime(2026, 11, 25, 9, 29, tzinfo=DHAKA), "open": 50000,
         "high": 50005, "low": 49995, "close": 50000},   # DOJI
    ]
    # Inserted under the stored form, as the collector does.
    assert db.insert_candles("BTCUSD_otc", rows) == 3
    # Every pasted-signal spelling must find the same rows.
    for spelling in ("BTCUSD-OTC", "BTCUSD_OTC", "btcusd_otc", "BTCUSD_otc"):
        got = db.get_candles(spelling, "2026-11-25")
        assert len(got) == 3, (spelling, got)
        assert db.count_candles(spelling, "2026-11-25") == 3, spelling
    # Checker end-to-end with the pasted list: real results, no NO DATA.
    candles = db.get_candles("BTCUSD-OTC", "2026-11-25")
    lookup = build_lookup(candles, "BTCUSD_OTC")
    lines = ["BTCUSD-OTC,09:06 M1 PUT", "BTCUSD-OTC,09:20 M1 CALL",
             "BTCUSD-OTC,09:29 M1 PUT"]
    results, summary = check_signals(lines, lookup, "2026-11-25")
    assert [r["result"] for r in results] == ["WIN", "WIN", "DOJI"], results
    assert summary["no_data"] == 0 and summary["win"] == 2, summary
    print("PASS b3) DB asset names case-insensitive — pasted BTCUSD-OTC "
          "finds stored BTCUSD_otc, checker gives WIN/WIN/DOJI")


def _load_synth_into_db(db):
    """Load two synthetic full days (2026-11-20 seed 7, 2026-11-21
    seed 8) into the DB: 2 x 1440 = 2880 candles. 09:02 on the 21st
    is forced CALL and 00:38 forced DOJI, like the old sample file."""
    d20 = make_day("USDMXN_otc", "2026-11-20", seed=7)
    d21 = make_day("USDMXN_otc", "2026-11-21", seed=8)
    force_candle(d21, "09:02", "CALL")
    force_candle(d21, "00:38", "DOJI")
    rows = rows_from_raw(to_raw(d20) + to_raw(d21))
    return db.insert_candles("USDMXN_otc", rows)


def test_signals_from_db():
    db = _tmp_db()
    inserted = _load_synth_into_db(db)
    # Two full synthetic days: 2 x 1440 = 2880 candles.
    assert inserted == 2880, inserted
    assert len(db.get_candles("USDMXN_otc", "2026-11-21")) == 1440
    signals = generate_futures_signals_from_db(
        db, "USDMXN_otc", ["2026-11-21"], "09:00", "10:59")
    got = [(s["hhmm"], s["direction"]) for s in signals]
    assert got == EXPECTED_16, f"DB signals mismatch:\n got={got}\n exp={EXPECTED_16}"
    print("PASS c) signals from DB — same 16 for 09:00-10:59 as the file test")


def test_checker_from_db():
    db = _tmp_db()
    _load_synth_into_db(db)
    candles = db.get_candles("USDMXN_otc", "2026-11-21")
    lookup = build_lookup(candles, "USDMXN_otc")
    results, summary = check_signals(
        ["USDMXN-OTC,09:02 M1 CALL", "USDMXN-OTC,09:02 M1 PUT",
         "USDMXN-OTC,00:38 M1 CALL"],
        lookup, "2026-11-21")
    # 09:02 is CALL (forced); 00:38 is a DOJI candle (O == C).
    assert [r["result"] for r in results] == ["WIN", "LOSS", "DOJI"], results
    assert summary["win"] == 1 and summary["loss"] == 1 and summary["doji"] == 1
    assert summary["win_pct"] == 50.0, summary
    print("PASS d) checker from DB — WIN / LOSS / DOJI from DB candles")


def test_webhook_branch():
    import main as main_mod

    old_url = config.WEBHOOK_URL
    try:
        config.WEBHOOK_URL = ""
        assert main_mod.should_use_webhook() is False
        config.WEBHOOK_URL = "https://blue-fish-prime-bot.onrender.com"
        assert main_mod.should_use_webhook() is True
        listen, port, path, url = main_mod.webhook_config()
        assert (listen, path) == ("0.0.0.0", "webhook"), (listen, path)
        assert url == "https://blue-fish-prime-bot.onrender.com/webhook", url
        assert port == config.PORT
    finally:
        config.WEBHOOK_URL = old_url
    print("PASS e) webhook branch — WEBHOOK_URL on/off switches mode, no bind")


class FakeSession:
    """Fake Quotex session: deterministic 1-min candles, no login.

    Generates candles for every minute in the requested span
    (00:00-23:59 Dhaka), alternating CALL/PUT, so the collector
    logic (window filter, insert, dedupe) can be tested offline.
    """

    def __init__(self, raise_429=False, now=None):
        self.raise_429 = raise_429
        # Fixed "now" so backfill counts are deterministic in tests.
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
        while t <= now:
            base = 19.90 if asset == "USDMXN_otc" else 1.08
            # Minute index since the epoch: the same minute always
            # gets the same candle, whatever span was fetched (like
            # the real server). A running counter made values depend
            # on the span start, so a 30-min re-fetch of already
            # stored minutes wrongly counted as new rows.
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


class CrashySession(FakeSession):
    """Fake session that dies with a 429 after `crash_after` fetches
    (simulates a Render sleep/restart mid-backfill). `assets` is the
    open-asset list; crash_after=None means a healthy session."""

    def __init__(self, assets, crash_after=None, **kwargs):
        super().__init__(**kwargs)
        self._assets = list(assets)
        self._crash_after = crash_after
        self._fetched = 0

    async def get_open_assets(self):
        return list(self._assets)

    async def fetch_asset_span(self, asset, seconds_back):
        self._fetched += 1
        if (self._crash_after is not None
                and self._fetched > self._crash_after):
            raise QuotexRateLimited("Quotex temporarily blocked us (429).")
        return await super().fetch_asset_span(asset, seconds_back)


def test_collector_backfill_fake():
    db = _tmp_db()
    now = datetime(2026, 11, 25, 12, 0, tzinfo=DHAKA)
    done, total = asyncio.run(
        run_backfill(db, FakeSession(now=now), days=5, now=now))
    assert done == 2, done
    # 5 completed days x 1440 + today 00:00-12:00 (721 — the fake
    # session includes the candle at exactly `now`) = 7921 per asset.
    assert total == 2 * 7921, total
    assert db.count_total() == 2 * 7921
    assert db.get_dates() == ["2026-11-20", "2026-11-21", "2026-11-22",
                              "2026-11-23", "2026-11-24", "2026-11-25"]
    assert len(db.get_candles("USDMXN_otc", "2026-11-22")) == 1440
    assert db.get_state("backfill_done") == "true"
    # Second identical backfill: everything already in the DB.
    done2, total2 = asyncio.run(
        run_backfill(db, FakeSession(now=now), days=5, now=now))
    assert total2 == 0, total2
    # The backfill stamps its window version, so a later restart with
    # the same window skips it instead of re-collecting.
    assert db.get_state("backfill_window") == "full-day"
    # Incremental run at 12:00: last 30m+10m = 11:20-12:00, all already
    # stored -> 0 new.
    done3, total3 = asyncio.run(
        run_incremental(db, FakeSession(now=now), now=now))
    assert total3 == 0, total3
    # Incremental run at 09:00 with an empty DB: 08:20-09:00 = 40 min.
    db2 = _tmp_db()
    morning = datetime(2026, 11, 25, 9, 0, tzinfo=DHAKA)
    _, total4 = asyncio.run(
        run_incremental(db2, FakeSession(now=morning), now=morning))
    assert total4 == 2 * 40, total4
    print("PASS f) collector backfill (fake session) — 1440/day/asset, "
          "re-run inserts 0, incremental window correct")


def test_backfill_resumes_after_crash():
    db = _tmp_db()
    now = datetime(2026, 11, 25, 12, 0, tzinfo=DHAKA)
    assets = ["AAA_otc", "BBB_otc", "CCC_otc", "DDD_otc"]
    # Run 1 dies on the 4th fetch (first asset of batch 2): batch 1
    # (AAA/BBB/CCC) must already be stored, not lost with the crash.
    try:
        asyncio.run(run_backfill(
            db, CrashySession(assets, crash_after=3, now=now),
            days=5, now=now))
    except QuotexRateLimited:
        pass
    else:
        raise AssertionError("backfill did not stop on 429")
    assert db.count_total() == 3 * 7921, db.count_total()
    assert db.get_state("backfill_done") != "true"
    # Run 2 (healthy) skips the 3 done assets and finishes DDD only.
    done, total = asyncio.run(run_backfill(
        db, CrashySession(assets, crash_after=None, now=now),
        days=5, now=now))
    assert done == 4, done
    assert total == 7921, total
    assert db.count_total() == 4 * 7921
    assert db.get_state("backfill_done") == "true"
    print("PASS f3) backfill resumes after crash — done assets kept, "
          "restart skips them and finishes the rest")


class _StopLoop(Exception):
    """Raised by the fake sleep to end collector_loop after one pass."""


async def _stop_sleep(_seconds):
    raise _StopLoop


def test_backfill_reruns_after_window_change():
    db = _tmp_db()
    # collector_loop uses collector.dhaka_now() internally (real clock),
    # so a fixed fake "now" disagrees with it the day after the test
    # was written (backfill dates shift). Freeze the clock instead.
    now = datetime(2026, 11, 25, 12, 0, tzinfo=DHAKA)
    # Simulate the OLD half-day state: backfill marked done today,
    # but no window version recorded (old code never set it).
    db.set_state("backfill_done", "true")
    db.set_state("last_backfill", utc_iso(now))
    # collector_loop must NOT skip the backfill: the window changed,
    # so the missing afternoon halves must be collected.
    _real_now = collector.dhaka_now
    collector.dhaka_now = lambda: now
    try:
        try:
            asyncio.run(collector.collector_loop(
                db=db, session_factory=lambda: FakeSession(now=now),
                sleep=_stop_sleep))
        except _StopLoop:
            pass
    finally:
        collector.dhaka_now = _real_now
    assert db.count_total() == 2 * 7921, db.count_total()
    assert db.get_state("backfill_window") == "full-day"
    print("PASS f4) backfill re-runs after window change — old half-day "
          "state is topped up to full days")


def test_collector_429_stops_run():
    db = _tmp_db()
    now = datetime(2026, 11, 25, 12, 0, tzinfo=DHAKA)
    try:
        asyncio.run(run_backfill(db, FakeSession(raise_429=True), days=5, now=now))
    except QuotexRateLimited:
        pass
    else:
        raise AssertionError("backfill did not raise on 429")
    assert db.count_total() == 0
    assert db.get_state("backfill_done") is None
    print("PASS f2) collector 429 — run stops, nothing stored, no retry loop")


def test_post_init_starts_collector_only_when_active():
    import main as main_mod

    calls = []

    async def fake_loop(*args, **kwargs):
        calls.append(True)

    real_loop = collector.collector_loop
    real_mode, real_flag = config.DATA_MODE, config.COLLECTOR_ENABLED
    try:
        collector.collector_loop = fake_loop
        config.DATA_MODE = "db"
        config.COLLECTOR_ENABLED = True

        class FakeApp:
            bot = None
            bot_data = {}

        async def run():
            await main_mod.post_init(FakeApp())
            await asyncio.sleep(0)  # let the created task run
            task = FakeApp.bot_data.get("collector_task")
            assert task is not None, "no collector task in bot_data"
            await task  # fake_loop returns at once

        asyncio.run(run())
        assert calls, "collector_loop was not started in db mode"
        # Inactive mode: no task is created.
        config.DATA_MODE = "live"
        config.COLLECTOR_ENABLED = False

        class FakeApp2:
            bot = None
            bot_data = {}

        async def run2():
            await main_mod.post_init(FakeApp2())
            await asyncio.sleep(0)

        asyncio.run(run2())
        assert "collector_task" not in FakeApp2.bot_data
    finally:
        collector.collector_loop = real_loop
        config.DATA_MODE, config.COLLECTOR_ENABLED = real_mode, real_flag
    print("PASS g) post_init starts the collector only in db mode")


def test_handlers_db_end_to_end():
    """Full handler path in db mode: futures (single + SELECT ALL),
    checker lookup, and the Bot Status summary.

    The FS result is now the user's template (edit round 2026-11-27):
    no 'Source: db' / 'Days:' / 'SELECT ALL' / fewer-days note lines.
    The 7-min gap is gone, so 1-day voting (every signal 100%) lets
    many more minutes through — 81 of the 120 in this synthetic
    window pass the strength filter; 2-day voting (5 requested, 2 in
    the DB) lets 45 through. SELECT ALL still caps at 12 per 2h."""
    import bot.candles_db as candles_db_mod
    import bot.handlers as handlers

    db = _tmp_db()
    _load_synth_into_db(db)
    real_get_db = candles_db_mod.get_candle_db
    real_mode = config.DATA_MODE
    try:
        candles_db_mod.get_candle_db = lambda: db
        config.DATA_MODE = "db"

        class Ctx:
            user_data = {"fs_asset": "USDMXN_otc", "fs_days": 1,
                         "fs_mode": "OTC FS"}

        text = asyncio.run(
            handlers._compute_futures_text(Ctx(), "09:00", "10:59"))
        assert "FUTURE SIGNALS — OTC FS" in text, text
        assert "MTG 1 STEP" in text, text
        assert "Avoid Below 70%" in text, text
        assert "Source: db" not in text, text
        assert "Days:" not in text, text
        assert "Generated:" not in text, text
        assert "<code>USDMXN-OTC,09:00 M1 CALL</code>" in text, text
        sig_lines = [ln for ln in text.splitlines()
                     if "<code>USDMXN-OTC," in ln]
        assert len(sig_lines) == 81, text

        # 5 days requested, only 2 in the DB: NO note any more (the
        # template has no room for it); the vote is 2-day.
        Ctx.user_data["fs_days"] = 5
        text5 = asyncio.run(
            handlers._compute_futures_text(Ctx(), "09:00", "10:59"))
        assert "has only 2 day(s)" not in text5, text5
        assert "FUTURE SIGNALS — OTC FS" in text5, text5
        sig_lines5 = [ln for ln in text5.splitlines()
                      if "<code>USDMXN-OTC," in ln]
        assert len(sig_lines5) == 45, text5

        # SELECT ALL from the DB (no Quotex login anywhere).
        Ctx.user_data = {"fs_asset": "ALL", "fs_days": 1, "fs_mode": "OTC FS"}
        text_all = asyncio.run(
            handlers._compute_futures_text(Ctx(), "09:00", "10:59"))
        assert "SELECT ALL" not in text_all, text_all
        # Cross-asset filter still caps at 12 per 2h window.
        sig_all = [ln for ln in text_all.splitlines()
                   if "<code>USDMXN-OTC," in ln]
        assert len(sig_all) == 12, text_all

        # Checker lookup from the DB for the synthetic date.
        lines = ["USDMXN-OTC,09:02 M1 CALL", "USDMXN-OTC,09:02 M1 PUT"]
        lookup = asyncio.run(
            handlers._build_checker_lookup(lines, "2026-11-21"))
        results, summary = check_signals(lines, lookup, "2026-11-21")
        assert summary == {"total": 2, "win": 1, "loss": 1, "doji": 0,
                           "no_data": 0, "win_pct": 50.0}, summary

        # Bot Status summary from the same DB.
        s = collector.get_status_summary(db)
        assert s["total_candles"] == 2880, s
        assert s["dates"] == ["2026-11-20", "2026-11-21"], s
        assert s["by_asset"] == {"USDMXN_otc": 2880}, s
        assert s["backend"] == "sqlite", s
    finally:
        candles_db_mod.get_candle_db = real_get_db
        config.DATA_MODE = real_mode
    print("PASS h) handlers db end-to-end — futures template, "
          "SELECT ALL cap, checker, status summary")


class FailOnceSession(FakeSession):
    """EURUSD's first fetch times out (like the 12:00 run on
    2026-11-27) — the retry inside the same run must still
    collect it, instead of leaving a 2-hour hole."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = {}

    async def fetch_asset_span(self, asset, seconds_back):
        self.calls[asset] = self.calls.get(asset, 0) + 1
        if asset == "EURUSD_otc" and self.calls[asset] == 1:
            raise TimeoutError("fetch timed out")
        return await super().fetch_asset_span(asset, seconds_back)


def test_incremental_retries_failed_assets():
    import bot.config as config_mod

    db = _tmp_db()
    morning = datetime(2026, 11, 25, 9, 0, tzinfo=DHAKA)
    session = FailOnceSession(now=morning)
    old_retry = config_mod.COLLECTOR_FAILED_ASSET_RETRY_SECONDS
    config_mod.COLLECTOR_FAILED_ASSET_RETRY_SECONDS = 0
    try:
        done, total = asyncio.run(
            run_incremental(db, session, now=morning))
    finally:
        config_mod.COLLECTOR_FAILED_ASSET_RETRY_SECONDS = old_retry
    # Both assets collected in the SAME run (2 x 40 minutes),
    # EURUSD fetched twice (failed once, retried once).
    assert done == 2, done
    assert total == 2 * 40, total
    assert session.calls["EURUSD_otc"] == 2, session.calls
    assert len(db.get_candles("EURUSD_otc", "2026-11-25")) == 40
    print("PASS i) incremental run retries a failed asset in the same run")


class _FakeBot:
    """Records telegram send_message calls (auto-signal test)."""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))


def test_auto_signals_sent_once_per_window():
    """j) The owner, 2026-11-27: the collector now runs every 30 min,
    so two runs in one hour would send the same next-hour window
    twice. One window is sent only once — the second call for the
    same window sends nothing. Three synthetic days feed the
    generator; strong CALLs are forced at the next hour's :00/:07/:14
    on every day so the window always has signals."""
    from bot.collector import _send_auto_signals, dhaka_now
    import bot.database as users_db
    from synthetic_data import force_candle, make_day, to_raw

    db = _tmp_db()
    now = dhaka_now()
    start_dt = (now + timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0)
    hh = start_dt.strftime("%H")
    today = now.date()
    dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d")
             for i in (3, 2, 1)]
    for k, d in enumerate(dates):
        day = make_day("USDMXN_otc", d, seed=7 + k)
        for mm in ("00", "07", "14"):
            force_candle(day, f"{hh}:{mm}", "CALL")
        rows = rows_from_raw(to_raw(day))
        db.insert_candles("USDMXN_otc", rows)
    bot = _FakeBot()
    old = users_db.get_auto_subscribers
    users_db.get_auto_subscribers = lambda: [123]
    try:
        asyncio.run(_send_auto_signals(bot, db))
        assert len(bot.sent) == 1, bot.sent
        assert "AUTO SIGNALS" in bot.sent[0][1], bot.sent[0][1][:80]
        end_hh = (start_dt + timedelta(hours=1)).strftime("%H")
        key = db.get_state("auto_signal_window")
        assert key == (start_dt.strftime("%Y-%m-%d")
                       + f" {hh}:00-{end_hh}:59"), key
        asyncio.run(_send_auto_signals(bot, db))
        assert len(bot.sent) == 1, "second call re-sent the same window"
    finally:
        users_db.get_auto_subscribers = old
    print("PASS j) auto signals sent once per window (30-min runs)")


def main():
    test_window_math()
    test_db_roundtrip()
    test_db_recovers_after_connection_closed()
    test_db_asset_name_case_insensitive()
    test_signals_from_db()
    test_checker_from_db()
    test_webhook_branch()
    test_collector_backfill_fake()
    test_backfill_resumes_after_crash()
    test_backfill_reruns_after_window_change()
    test_collector_429_stops_run()
    test_post_init_starts_collector_only_when_active()
    test_handlers_db_end_to_end()
    test_incremental_retries_failed_assets()
    test_auto_signals_sent_once_per_window()
    print("ALL TESTS PASSED (15/15)")


if __name__ == "__main__":
    main()
