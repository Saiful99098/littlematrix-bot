"""One-off gap-fill — fills middle-of-day candle holes in the Render Postgres DB.

Why this exists (2026-11-27): the tail-fill and the 30-minute collector
only fill the RECENT tail/window, so a hours-old hole in the middle of
a day (USDPHP_otc 02:41-08:32, SOLUSD_otc 01:28-08:32 on 2026-11-27)
is never healed by them. This script scans the DB for missing minute
ranges and fetches exactly those spans from Quotex, once per asset.

- Reads QUOTEX_EMAIL, QUOTEX_PASSWORD and DATABASE_URL from the local
  .env (never hardcode secrets, never print them). DATABASE_URL must be
  the Render Postgres EXTERNAL Database URL (same as collect_to_db.py).
- One Quotex login for the whole run; assets fetched 3 at a time with
  1s pauses (the collector's fetcher). Real-market assets that were
  closed (no candles at all on the date) are skipped automatically.
- On 429: wait 10 minutes and retry once, then stop. Never hammer Quotex.
- Run it from the PC (like collect_to_db.py) — the Render bot only
  reads the DB, so it keeps working while this runs. Do not run it at
  the same minute as a PC collector run (two logins at once risk 429).

Usage (in the littlematrix-bot-local folder on the PC):
    python fill_gaps.py                        # today, all assets
    python fill_gaps.py --date 2026-11-27     # one date
    python fill_gaps.py --asset USDPHP_otc    # one asset (repeatable)
    python fill_gaps.py --dry-run             # list the gaps, fill nothing
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))  # so `bot` imports work from any cwd

from bot import collector, config
from bot.candles_db import DHAKA
from bot.quotex_client import QuotexRateLimited
from collect_to_db import build_db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("littlematrix.gap_fill")


async def _run_once(db, date_str, assets, dry_run, session_factory=None):
    """One gap-fill pass. Returns (assets, gaps, candles) or False on
    a non-429 failure. Raises QuotexRateLimited so the caller can
    wait 10 minutes and retry once."""
    session_factory = session_factory or collector.SharedQuotexSession
    session = session_factory()
    try:
        await session.connect()
        return await collector.fill_gaps(
            db, session, date_str=date_str, assets=assets, dry_run=dry_run)
    finally:
        try:
            await session.close()
        except Exception:
            pass


async def run(db, date_str, assets, dry_run, session_factory=None, sleep=None):
    """Fill gaps once, with a single 429 retry after 10 minutes.
    Returns the fill_gaps() tuple, or None when the run failed."""
    sleep = sleep or asyncio.sleep
    try:
        return await _run_once(db, date_str, assets, dry_run, session_factory)
    except QuotexRateLimited as exc:
        logger.warning("gap-fill: 429 — %s", exc)
        logger.info("gap-fill: retrying once in %d minutes",
                    config.COLLECTOR_429_RETRY_MINUTES)
        await sleep(config.COLLECTOR_429_RETRY_MINUTES * 60)
        try:
            return await _run_once(db, date_str, assets, dry_run,
                                   session_factory)
        except QuotexRateLimited:
            logger.warning("gap-fill: 429 again — stopping. Re-run later.")
            return None
    except Exception as exc:
        logger.warning("gap-fill: run failed: %s", exc)
        return None


def _valid_date(value):
    """YYYY-MM-DD, not in the future (Dhaka). Returns the string or
    raises argparse.ArgumentTypeError."""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"bad date {value!r} — use YYYY-MM-DD, e.g. 2026-11-27")
    today = collector.dhaka_now().strftime("%Y-%m-%d")
    if value > today:
        raise argparse.ArgumentTypeError(
            f"date {value} is in the future (today is {today})")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fill middle-of-day candle holes in the Render "
                    "Postgres DB (one fetch per asset, only the missing "
                    "minute ranges).")
    parser.add_argument("--date", type=_valid_date, default=None,
                        help="Date to fill, YYYY-MM-DD (default: today)")
    parser.add_argument("--asset", action="append", default=None,
                        help="Fill only this asset (repeatable, e.g. "
                             "--asset USDPHP_otc --asset SOLUSD_otc)")
    parser.add_argument("--dry-run", action="store_true",
                        help="List the gaps without fetching or storing")
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
    date_str = args.date or collector.dhaka_now().strftime("%Y-%m-%d")
    logger.info("gap-fill: database backend=%s (Render Postgres)", db.backend)

    try:
        result = asyncio.run(run(db, date_str, args.asset, args.dry_run))
    finally:
        db.close()
    if result is None:
        logger.warning("gap-fill: finished with errors")
        return 1
    assets_filled, gaps_filled, candles = result
    logger.info("gap-fill: done — %d asset(s), %d gap(s), %d new candles",
                assets_filled, gaps_filled, candles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
