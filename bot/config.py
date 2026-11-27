"""Config — loads .env, never contains real secrets in the repo.

Only placeholders live in .env.example. the owner copies it to .env
on his PC and puts his own token there. On Render, the same values
come from Render's Environment Variables (never from git).

Modes (DATA_MODE):
- db:     signals + CHECKER read ONLY from the candle database
          (Postgres on Render when DATABASE_URL is set, SQLite file
          locally). A background collector fills the DB: on startup
          it backfills the last 5 completed days (Asia/Dhaka
          00:00-23:59 each day) + today 00:00-now, then every 30 minutes
          it collects the last 30 minutes (+10 min overlap) into the DB.
          Signals and CHECKER never log in to Quotex in db mode —
          that is the anti-429 design.
- live:   fetch yesterday's candles live from Quotex (local PC test).
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
# Proxy for the Quotex collector session only (Render 403 fix).
# Format: http://user:pass@host:port (or http://host:port).
# Empty = no proxy (direct connection, as before). The value is
# NEVER logged (it may contain a password). Only the collector's
# Quotex session uses it — signals and CHECKER never log in.
QUOTEX_PROXY = os.getenv("QUOTEX_PROXY", "").strip()
# Optional broker host override for the collector's Quotex session
# (passed as host= to Quotex()). Empty = default market-qx.info.
# No alternative host is verified to avoid the Render 403 — this is
# only an escape hatch to try (e.g. qxbroker.com) if proxy + region
# both fail. Do NOT set this unless asked to try it.
QUOTEX_HOST = os.getenv("QUOTEX_HOST", "").strip() or None


def quotex_proxies_dict():
    """QUOTEX_PROXY env -> {"http": ..., "https": ...} for Quotex().

    Returns None when no proxy is set (direct connection) or when the
    value has an unsupported scheme (only http/https/socks5/socks5h).
    """
    if not QUOTEX_PROXY:
        return None
    proxy = QUOTEX_PROXY.strip()
    if "://" not in proxy:
        proxy = "http://" + proxy
    scheme = proxy.split("://", 1)[0].lower()
    if scheme not in ("http", "https", "socks5", "socks5h"):
        import logging
        logging.getLogger("littlematrix.config").warning(
            "QUOTEX_PROXY has unsupported scheme %r — ignoring it "
            "(use http://, https:// or socks5://)", scheme)
        return None
    return {"http": proxy, "https": proxy}
# Channel membership gate: only members of this Telegram channel/group
# may use the bot for free. Set to the channel username (e.g.
# "@BlueFishFutureSignal") or numeric chat id (e.g. "-1001234567890").
# Empty = gate off (bot works for everyone, local tests).
# The bot must be an ADMIN in the channel, otherwise Telegram will
# not let it check who joined.
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()
# Telegram user ids allowed to approve premium requests (comma
# separated, e.g. "123456789"). The owner's id goes here. Empty = no
# admin commands; premium requests are only stored for manual check.
ADMIN_USER_IDS = {int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",")
                  if x.strip().isdigit()}
# Premium payment: Binance Pay ID members send the amount to.
# Empty = Premium shows "ask the owner" instead.
BINANCE_PAY_ID = os.getenv("BINANCE_PAY_ID", "").strip()
USDT_AMOUNT = os.getenv("USDT_AMOUNT", "5").strip() or "5"
# Join link for the required channel (e.g. https://t.me/+HDiWRJothc5hNTQ9).
# The Join button uses this link. When empty, the button links to
# t.me/<REQUIRED_CHANNEL> instead.
CHANNEL_INVITE_LINK = os.getenv("CHANNEL_INVITE_LINK", "").strip()
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
# Repeating collection interval, seconds (30 minutes = 1800 — the
# owner, 2026-11-27: 2 hours left gaps open too long).
COLLECTOR_INTERVAL_SECONDS = int(os.getenv("COLLECTOR_INTERVAL_SECONDS", "1800"))
# Minutes of overlap on each run (re-fetch the last 10 min
# so a slow/failed run cannot leave a gap; duplicates are ignored
# by the UNIQUE(asset, ts) index).
COLLECTOR_OVERLAP_MINUTES = int(os.getenv("COLLECTOR_OVERLAP_MINUTES", "10"))
# Minutes to wait and retry after a Quotex 429 inside the collector.
COLLECTOR_429_RETRY_MINUTES = int(os.getenv("COLLECTOR_429_RETRY_MINUTES", "10"))
# Seconds to wait before re-fetching, once, the assets whose fetch
# failed inside one run (a single timeout used to leave a hole in
# that asset — the owner, 2026-11-27).
COLLECTOR_FAILED_ASSET_RETRY_SECONDS = int(os.getenv(
    "COLLECTOR_FAILED_ASSET_RETRY_SECONDS", "30") or 30)
# Seconds to wait and retry, once, a whole collector run that failed
# (e.g. the login got a transient 403 — the owner, 2026-11-27: the
# 17:45 run died on "Error on access page: 403" and the next run was
# 30 minutes away, while the stream logged in fine 2 minutes later).
# A 429 has its own 10-minute retry; this covers every other failure.
COLLECTOR_FAILED_RUN_RETRY_SECONDS = int(os.getenv(
    "COLLECTOR_FAILED_RUN_RETRY_SECONDS", "180") or 180)
# Live candle stream (hybrid): one persistent Quotex WebSocket that
# writes each closed 1-minute candle for every open asset into the DB
# as it arrives. The batch collector stays the backbone
# (gap-filling source of truth); the stream only makes data fresher.
# Set LIVE_STREAM_ENABLED=false to turn the stream off — the bot then
# works exactly as before, from the batch collector alone.
LIVE_STREAM_ENABLED = os.getenv("LIVE_STREAM_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
# Candle period the stream subscribes to (60 = 1-minute candles).
LIVE_STREAM_PERIOD = int(os.getenv("LIVE_STREAM_PERIOD", "60") or 60)
# Seconds between subscribe requests (flooding the server with 90
# subscribes at once risks a ban/rate-limit — see pyquotex README).
LIVE_STREAM_SUBSCRIBE_DELAY = float(os.getenv("LIVE_STREAM_SUBSCRIBE_DELAY", "0.2") or 0.2)
# Seconds between checks for newly closed candles.
LIVE_STREAM_POLL_SECONDS = float(os.getenv("LIVE_STREAM_POLL_SECONDS", "5") or 5)
# Minutes with no new candle from any asset before the stream treats
# the connection as dead and reconnects.
LIVE_STREAM_STALE_MINUTES = int(os.getenv("LIVE_STREAM_STALE_MINUTES", "10") or 10)
# TEST MODE for the silent-stream investigation (2026-11-26): when
# set to one asset name (e.g. BTCUSD-OTC), the stream subscribes to
# that asset only — if events flow for one asset, the server caps
# bulk subscribes. Empty (default) = stream all open assets.
LIVE_STREAM_TEST_ASSET = os.getenv("LIVE_STREAM_TEST_ASSET", "").strip()
# How many days of history the day picker may offer (1-20 in code;
# the Render-test backfill gives 5, so the picker shows 1-5 in db mode).
MAX_SIGNAL_DAYS = int(os.getenv("MAX_SIGNAL_DAYS", "20"))

DB_PATH = BASE_DIR / "data" / "littlematrix.db"
CANDLES_DB_PATH = BASE_DIR / "data" / "candles.db"
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


def live_stream_active():
    """True when the live candle stream must run.

    db mode + collector + stream flag. The stream shares the
    collector's Quotex login, so it never runs without the collector.
    """
    return collector_active() and LIVE_STREAM_ENABLED
