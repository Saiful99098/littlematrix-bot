"""Config — loads .env, never contains real secrets in the repo.

Only placeholders live in .env.example. Saiful copies it to .env
on his PC and puts his own token there. On Render, the same values
come from Render's Environment Variables (never from git).

Modes (DATA_MODE):
- sample: bundled USDMXN 2026-11-21 sample file (no login).
- live:   fetch yesterday's candles live from Quotex (local PC test).
- db:     signals + CHECKER read ONLY from the candle database
          (Postgres on Render when DATABASE_URL is set, SQLite file
          locally). A background collector fills the DB: on startup
          it backfills the last 5 completed days (Asia/Dhaka
          00:00-11:59 each day) + today 00:00-now, then every 2 hours
          it collects the last 2 hours (+10 min overlap) into the DB.
          Signals and CHECKER never log in to Quotex in db mode —
          that is the anti-429 design.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass  # python-dotenv is optional for import; main.py warns if missing

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
QUOTEX_EMAIL = os.getenv("QUOTEX_EMAIL", "")
QUOTEX_PASSWORD = os.getenv("QUOTEX_PASSWORD", "")
DATA_MODE = os.getenv("DATA_MODE", "sample")  # sample | live | db
# Render webhook: set WEBHOOK_URL to the public HTTPS base URL of the
# Render web service, e.g. https://blue-fish-prime-bot.onrender.com
# (on Render it is set from $RENDER_EXTERNAL_HOSTNAME in render.yaml).
# When empty, the bot runs in polling mode (local PC).
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
# Postgres connection string. On Render it is auto-provided by the
# free Postgres database (DATABASE_URL in render.yaml). When empty,
# the candle DB falls back to a local SQLite file (local testing).
DATABASE_URL = os.getenv("DATABASE_URL", "")
# Port Render gives the web service (default 10000 on Render).
PORT = int(os.getenv("PORT", "10000"))
# Collector: on in db mode. Also needs COLLECTOR_ENABLED=true.
COLLECTOR_ENABLED = os.getenv("COLLECTOR_ENABLED", "False").lower() == "true"
# Backfill: how many completed days to collect on startup (Render test).
BACKFILL_DAYS = int(os.getenv("BACKFILL_DAYS", "5"))
# Collection window each day, Asia/Dhaka (UTC+6): 00:00-11:59.
# Only candles inside this window are stored (720 per asset per day).
COLLECTION_START = os.getenv("COLLECTION_START", "00:00")
COLLECTION_END = os.getenv("COLLECTION_END", "11:59")
# Repeating collection interval, seconds (2 hours = 7200).
COLLECTOR_INTERVAL_SECONDS = int(os.getenv("COLLECTOR_INTERVAL_SECONDS", "7200"))
# Minutes of overlap on each 2-hourly run (re-fetch the last 10 min
# so a slow/failed run cannot leave a gap; duplicates are ignored
# by the UNIQUE(asset, ts) index).
COLLECTOR_OVERLAP_MINUTES = int(os.getenv("COLLECTOR_OVERLAP_MINUTES", "10"))
# Minutes to wait and retry after a Quotex 429 inside the collector.
COLLECTOR_429_RETRY_MINUTES = int(os.getenv("COLLECTOR_429_RETRY_MINUTES", "10"))
# How many days of history the day picker may offer (1-20 in code;
# the Render-test backfill gives 5, so the picker shows 1-5 in db mode).
MAX_SIGNAL_DAYS = int(os.getenv("MAX_SIGNAL_DAYS", "20"))

DB_PATH = BASE_DIR / "data" / "littlematrix.db"
CANDLES_DB_PATH = BASE_DIR / "data" / "candles.db"
SAMPLE_CANDLES = BASE_DIR / "data" / "sample_candles_USDMXN_otc_2026-11-21.json"
DEFAULT_ASSET = "USDMXN_otc"
TIMEZONE = "UTC+6"

SAMPLE_ASSETS = ["USDMXN_otc", "EURUSD_otc", "GBPUSD_otc",
                 "USDPKR_otc", "AUDCAD_otc", "EURJPY_otc"]


def has_telegram_token():
    return bool(BOT_TOKEN)


def has_quotex_login():
    return bool(QUOTEX_EMAIL and QUOTEX_PASSWORD)


def use_webhook():
    """True when the bot must run in webhook mode (Render)."""
    return bool(WEBHOOK_URL)


def collector_active():
    """True when the background collector must run.

    db mode + enabled flag. In sample/live mode the collector never
    runs (local test keeps the old behaviour).
    """
    return DATA_MODE == "db" and COLLECTOR_ENABLED
