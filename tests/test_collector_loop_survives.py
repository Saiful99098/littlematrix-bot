"""Regression test: collector loop survives a DB write failure.

Bug (2026-11-25, Render): Bot Status froze at last_collection 17:12:32
with next_collection stuck at 19:12:32, while the bot kept answering
Telegram. Cause: _one_run's finally block called db.set_state()
unprotected — when Render's Postgres had closed the idle connection,
that write raised out of _one_run and killed the background task
silently (create_task with no done-callback). Fix: state writes go
through _safe_set_state() (never raises) and _one_run call sites are
guarded, so the loop keeps its 2-hour cycle.
Run: python tests/test_collector_loop_survives.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import collector
from bot.candles_db import CandleDB, DHAKA, utc_iso


class _StopLoop(Exception):
    pass


async def _stop_sleep(_seconds):
    raise _StopLoop


class FakeSession:
    def __init__(self, now):
        self.now = now

    async def connect(self):
        return True

    async def get_open_assets(self):
        return ["AAA_otc"]

    async def fetch_asset_span(self, asset, seconds_back):
        ts = int(self.now.timestamp()) - 60
        return [{"time": ts, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05}]

    async def close(self):
        return None


class FlakyDB:
    """Real sqlite DB, except the 'disconnected' state write fails —
    like Render's Postgres raising 'connection already closed'."""

    def __init__(self, real):
        object.__setattr__(self, "_real", real)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)

    def set_state(self, key, value):
        if key == "quotex_session" and str(value).startswith("disconnected"):
            raise RuntimeError("connection already closed")
        return object.__getattribute__(self, "_real").set_state(key, value)


def test_loop_survives_db_write_failure():
    tmp = tempfile.TemporaryDirectory()
    real = CandleDB(database_url="", sqlite_path=Path(tmp.name) / "c.db").init()
    db = FlakyDB(real)
    now = datetime(2026, 11, 25, 17, 12, tzinfo=DHAKA)
    # Backfill already done today (per the stored state), so the loop
    # runs its pass and must reach the interval sleep even though the
    # "disconnected" state write fails — that is the regression.
    real.set_state("backfill_done", "true")
    real.set_state("backfill_window", "full-day")
    real.set_state("last_backfill", utc_iso(now))
    try:
        asyncio.run(collector.collector_loop(
            db=db, session_factory=lambda: FakeSession(now), sleep=_stop_sleep))
    except _StopLoop:
        pass  # expected: loop reached the 2-hour sleep = it survived
    else:
        raise AssertionError("loop ended without reaching its sleep")
    print("PASS h) collector loop survives a failed state write — "
          "next cycle still scheduled")


if __name__ == "__main__":
    test_loop_survives_db_write_failure()
