"""PC-side candle collector — writes DIRECTLY into the Render Postgres DB.

Why this exists (2026-11-25): the Render bot is Live (webhook 200 OK),
but Quotex returns 403 ("Error on access page") for logins from Render's
datacenter IP, so the Render collector cannot collect any candles and
Bot Status shows 0 candles. The same Quotex login works from the owner's
home PC IP, so this script runs on his PC and writes candles straight
into the same Render Postgres database the bot reads from.

- Reads QUOTEX_EMAIL, QUOTEX_PASSWORD and DATABASE_URL from the local
  .env (never hardcode secrets, never print them). DATABASE_URL must be
  the Render Postgres EXTERNAL Database URL (Dashboard -> littlematrix-db
  -> Info -> External Database URL) — the internal URL only works inside
  Render. If DATABASE_URL is empty, the script stops with an error
  instead of silently writing to the local SQLite file.
- Reuses bot/candles_db.py (Postgres when DATABASE_URL is set) and
  bot/collector.py's run_backfill / run_incremental + SharedQuotexSession
  (one login per run, 3 assets at a time, 1s pauses, full Dhaka day
  00:00-23:59 window, past 5 days + today 00:00-now for backfill, then last 2h +
  10-min overlap every 2 hours).
- The Telegram bot does NOT need to run for this script to work, and
  the script never touches the bot's behaviour — the Render bot only
  reads the DB.

Usage (in the littlematrix-bot-local folder on the PC):
    python collect_to_db.py            # backfill once, then every 2 hours
    python collect_to_db.py --once     # one backfill + incremental pass, then exit

- On 429: wait 10 minutes and retry the run once, then wait for the
  next cycle. Never hammer Quotex.
- On 403: log one clear line and keep the loop alive for the next
  cycle — never crash-loop, never hammer.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))  # so `bot` imports work from any cwd

from bot import candles_db, collector, config
from bot.quotex_client import QuotexRateLimited

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("littlematrix.pc_collector")


def normalize_database_url(url):
    """Render's external Postgres URL needs SSL (sslmode=require).

    Appends it when the URL has no sslmode yet. Non-postgres URLs
    (including the empty string) are returned unchanged. The URL is
    never logged — it contains the database password.
    """
    if not url or not url.startswith("postgres"):
        return url
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query))
    if "sslmode" not in query:
        query["sslmode"] = "require"
        parts = parts._replace(query=urlencode(query))
        return urlunparse(parts)
    return url


def _log_run_error(exc):
    """One clear line for a failed run. 403 gets its own explanation."""
    msg = str(exc)
    if "403" in msg:
        logger.warning(
            "pc-collector: Quotex blocked this PC too (403 on access page). "
            "Will retry on the next cycle — first check that Quotex login "
            "works in the browser at quotex.com.")
    else:
        logger.warning("pc-collector: run failed: %s", msg)


async def _one_run(db, kind, session_factory=None, now=None):
    """One connected run (backfill or incremental).

    Returns True on success, 'rate_limited' on 429, False on any other
    error (including 403 — logged, never raised out of here).
    `now` is only used by tests (fixed clock); production passes None.
    """
    session_factory = session_factory or collector.SharedQuotexSession
    session = session_factory()
    try:
        await session.connect()
        db.set_state("quotex_last_login", candles_db.utc_iso(collector.dhaka_now()))
        db.set_state("quotex_session", "connected (PC collector)")
        if kind == "backfill":
            await collector.run_backfill(db, session, now=now)
        else:
            await collector.run_incremental(db, session, now=now)
        return True
    except QuotexRateLimited as exc:
        db.set_state("quotex_session", f"rate limited (429): {exc}")
        logger.warning("pc-collector: 429 — %s", exc)
        return "rate_limited"
    except Exception as exc:
        db.set_state("quotex_session", f"error: {exc}")
        _log_run_error(exc)
        return False
    finally:
        try:
            await session.close()
        except Exception:
            pass
        db.set_state("quotex_session", "disconnected (between runs)")


async def _run_with_429_retry(db, kind, session_factory=None, sleep=None,
                              now=None):
    """One run with a single 429 retry after 10 minutes.

    Returns True on success, False otherwise. 403 is NOT retried here
    (retrying a blocked IP immediately only makes it worse) — the next
    cycle tries again.
    """
    sleep = sleep or asyncio.sleep
    result = await _one_run(db, kind, session_factory, now=now)
    if result == "rate_limited":
        logger.info("pc-collector: %s hit 429, retrying in %d minutes",
                    kind, config.COLLECTOR_429_RETRY_MINUTES)
        await sleep(config.COLLECTOR_429_RETRY_MINUTES * 60)
        result = await _one_run(db, kind, session_factory, now=now)
        if result == "rate_limited":
            logger.warning("pc-collector: %s hit 429 again, "
                           "will retry on the next cycle", kind)
    return result is True


async def run_once(db, session_factory=None, sleep=None, now=None):
    """One backfill + one incremental pass.

    Backfill is skipped when it already ran today (same rule as the
    Render collector). Returns (backfill_ok, incremental_ok) as bools.
    Never raises for Quotex errors — they are logged and returned
    as False.
    """
    done = db.get_state("backfill_done") == "true"
    last_bf = db.get_state("last_backfill")
    today = (now or collector.dhaka_now()).strftime("%Y-%m-%d")
    if done and last_bf and last_bf[:10] == today:
        logger.info("pc-collector: backfill already done today (%s), skipping",
                    last_bf)
        backfill_ok = True
    else:
        logger.info("pc-collector: backfill starting — past %d days + today",
                    config.BACKFILL_DAYS)
        backfill_ok = await _run_with_429_retry(
            db, "backfill", session_factory, sleep, now=now)
        if backfill_ok:
            collector.log_status_summary(db, "pc-collector: backfill done")

    logger.info("pc-collector: incremental run starting (last 2 hours)")
    incremental_ok = await _run_with_429_retry(
        db, "incremental", session_factory, sleep, now=now)
    return backfill_ok, incremental_ok


async def run_forever(db, session_factory=None, sleep=None):
    """Backfill once, then an incremental run every 2 hours, forever."""
    sleep = sleep or asyncio.sleep
    interval = config.COLLECTOR_INTERVAL_SECONDS
    logger.info("pc-collector: starting (backfill %d days, then every %d minutes)",
                config.BACKFILL_DAYS, interval // 60)
    await run_once(db, session_factory=session_factory, sleep=sleep)
    while True:
        nxt = collector.dhaka_now() + timedelta(seconds=interval)
        logger.info("pc-collector: next run at %s (Dhaka)", nxt.strftime("%H:%M"))
        await sleep(interval)
        logger.info("pc-collector: incremental run starting (last 2 hours)")
        await _run_with_429_retry(db, "incremental", session_factory, sleep)


def build_db():
    """Connect to the Render Postgres DB from .env.

    Returns a CandleDB. Raises RuntimeError with a clear message when
    DATABASE_URL is missing (never silently fall back to local SQLite
    here — that would collect into the wrong database) or the
    connection fails.
    """
    if not config.DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set in .env. Copy the EXTERNAL Database URL "
            "from Render Dashboard -> littlematrix-db -> Info -> "
            "External Database URL and add it to .env as DATABASE_URL=... "
            "Without it this script would only write to the local SQLite "
            "file, and the Render bot would still show 0 candles.")
    db = candles_db.CandleDB(
        database_url=normalize_database_url(config.DATABASE_URL)).init()
    return db


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="PC-side candle collector — writes into the Render "
                    "Postgres DB (the Render bot only reads from it).")
    parser.add_argument("--once", action="store_true",
                        help="Run one backfill + incremental pass, then exit")
    args = parser.parse_args(argv)

    if not config.has_quotex_login():
        print("ERROR: QUOTEX_EMAIL / QUOTEX_PASSWORD are not set in .env.")
        print("Put your Quotex login in .env first (same lines the bot uses).")
        return 2
    try:
        db = build_db()
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 2
    except Exception as exc:
        print("ERROR: Could not connect to the database.")
        print("Check the DATABASE_URL in .env (Render Dashboard -> "
              "littlematrix-db -> Info -> External Database URL).")
        print(f"Database error: {exc}")
        return 2
    logger.info("pc-collector: database backend=%s (Render Postgres)", db.backend)

    try:
        if args.once:
            backfill_ok, incremental_ok = asyncio.run(run_once(db))
            logger.info("pc-collector: --once finished (backfill=%s, "
                        "incremental=%s)", backfill_ok, incremental_ok)
            return 0 if (backfill_ok and incremental_ok) else 1
        asyncio.run(run_forever(db))
    except KeyboardInterrupt:
        logger.info("pc-collector: stopped by user (Ctrl+C)")
        return 0
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
