"""Background candle collector (Render / db mode).

Anti-429 design (agreed with Saiful):
- ONE shared Quotex session per collector run: login once, fetch
  all assets on that session, close. Signals and CHECKER never
  log in — they read only from the database.
- Live fetching is 3 assets at a time with a 1s pause between
  batches (the 3-at-a-time rule is ONLY for the live collector).
- On HTTP 429: stop the run immediately, log it, wait 10 minutes
  and retry once. Never hammer Quotex.
- Collection window is the FULL Asia/Dhaka day 00:00-23:59
  (1440 one-minute candles per asset per day). Only candles inside
  the window are stored.
- Resumable: UNIQUE(asset, ts) ignores candles already in the DB,
  and every 2-hourly run re-fetches the last 10 minutes as overlap.
  The backfill saves progress per batch: each batch of 3 assets is
  fetched AND stored immediately, and assets whose backfill span is
  already complete are skipped — so a Render sleep/restart resumes
  where the last run stopped instead of starting over from asset 1.

Runs:
- Backfill (once, on startup, background task): the last 5
  completed days (full Dhaka days each) for all open assets,
  plus today 00:00-now — so CHECKER can verify today's signals.
  One get_historical_candles() call per asset covers the whole
  5-day span; days are split locally.
- Incremental (every 2 hours): the last 2 hours + 10 min overlap
  for all open assets.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import candles_db, config, quotex_client
from .candles_db import DHAKA, utc_iso

logger = logging.getLogger("littlematrix.collector")

def _safe_set_state(db, key, value):
    """Write one collector_state row; never raise.

    A state write is bookkeeping, not the collection itself. Render's
    Postgres closes idle connections, and if that write fails the
    collector loop used to die silently from _one_run's finally block
    while the bot kept answering Telegram — Bot Status then froze at
    the last successful run. Swallow + log instead.
    """
    try:
        db.set_state(key, value)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not kill the loop
        logger.warning("collector: could not save state %s: %s", key, exc)


# Backfill window version, stored in collector_state. When the window
# changes (e.g. 00:00-11:59 -> full day), the next startup re-runs the
# backfill once so the missing half of every day gets collected,
# instead of skipping it because backfill_done is already "true".
BACKFILL_WINDOW = "full-day"

# One full Dhaka day = 1440 one-minute candles.
CANDLES_PER_DAY = 1440


# ---------------------------------------------------------------------------
# Window math (pure functions — unit tested)
# ---------------------------------------------------------------------------
def dhaka_now():
    return datetime.now(DHAKA)


def backfill_dates(n_days=None, now=None):
    """Date strings of the last n completed days (yesterday first).

    A day is complete once its full Dhaka day (00:00-23:59) has passed,
    so every day before today is complete. Returns newest first.
    """
    n_days = n_days if n_days is not None else config.BACKFILL_DAYS
    now = now or dhaka_now()
    today = now.date()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(1, n_days + 1)]


def day_window_utc(date_str):
    """Half-open UTC range for one Dhaka date's collection window."""
    return candles_db.dhaka_date_range_utc(date_str)


def today_window_utc(now=None):
    """UTC range [today 00:00 Dhaka, min(now, tomorrow 00:00 Dhaka))."""
    now = now or dhaka_now()
    today = now.strftime("%Y-%m-%d")
    start, end = day_window_utc(today)
    now_utc = now.astimezone(timezone.utc)
    return start, min(now_utc, end)


def recent_window_utc(now=None, hours=2, overlap_minutes=None):
    """UTC range for one incremental run: last `hours` + overlap."""
    overlap = (overlap_minutes if overlap_minutes is not None
               else config.COLLECTOR_OVERLAP_MINUTES)
    now = now or dhaka_now()
    now_utc = now.astimezone(timezone.utc)
    start = now_utc - timedelta(hours=hours, minutes=overlap)
    return start, now_utc


def in_collection_window(dt_dhaka):
    """True when a Dhaka datetime is inside the collection window.
    The window is the FULL Dhaka day (00:00-23:59), so every minute
    of the day counts."""
    return True


def fetch_seconds_back(window_start_utc, now=None):
    """Seconds for get_historical_candles(): it fetches back from NOW,
    so the amount must reach from now back to the window start
    (+120s buffer so the first candle of the window is included)."""
    now = (now or dhaka_now()).astimezone(timezone.utc)
    seconds = int((now - window_start_utc).total_seconds()) + 120
    return max(seconds, 3600)


def rows_from_raw(raw_candles):
    """pyquotex/sample raw candles -> DB rows, filtered to the
    collection window (the full Dhaka day 00:00-23:59).

    Input: list of dicts with unix 'time' + open/high/low/close
    (or 'datetime' + O/H/L/C). Output: list of dicts with keys
    ts (aware UTC datetime), open, high, low, close.
    """
    rows = []
    for c in raw_candles or []:
        try:
            if c.get("time") is not None:
                ts_utc = datetime.fromtimestamp(int(c["time"]), tz=timezone.utc)
                o = c.get("open", c.get("O"))
                h = c.get("high", c.get("H"))
                low = c.get("low", c.get("L"))
                cl = c.get("close", c.get("C"))
            else:
                dt = datetime.strptime(c["datetime"], "%Y-%m-%d %H:%M:%S")
                ts_utc = dt.replace(tzinfo=DHAKA).astimezone(timezone.utc)
                o, h, low, cl = c.get("O"), c.get("H"), c.get("L"), c.get("C")
            if None in (o, h, low, cl):
                continue
            if not in_collection_window(ts_utc.astimezone(DHAKA)):
                continue
            rows.append({"ts": ts_utc, "open": float(o), "high": float(h),
                         "low": float(low), "close": float(cl)})
        except Exception:
            continue
    rows.sort(key=lambda r: r["ts"])
    return rows


# ---------------------------------------------------------------------------
# Shared Quotex session — ONE login per collector run
# ---------------------------------------------------------------------------
class SharedQuotexSession:
    """One pyquotex client for a whole collector run.

    Duck-typed interface used by run_backfill/run_incremental:
    async get_open_assets() -> list[str] | None
    async fetch_asset_span(asset, seconds_back) -> list of raw candles
    async close()
    Tests pass a FakeSession with the same methods (no real login).
    """

    def __init__(self):
        self.client = None
        self.connected = False
        self.last_login = None
        self.last_error = ""

    async def connect(self):
        from pyquotex.stable_api import Quotex

        # QUOTEX_PROXY (if set) routes the HTTP login through the proxy
        # — this is what avoids the 403 on Render's datacenter IP.
        # QUOTEX_HOST (if set) overrides the broker host. Neither value
        # is ever logged (the proxy URL may contain a password).
        self.client = Quotex(email=config.QUOTEX_EMAIL,
                             password=config.QUOTEX_PASSWORD,
                             proxies=config.quotex_proxies_dict(),
                             host=config.QUOTEX_HOST)
        logger.info("collector: Quotex session connecting (proxy=%s, host=%s)",
                    "set" if config.QUOTEX_PROXY else "none",
                    config.QUOTEX_HOST or "market-qx.info (default)")
        check, message = await self.client.connect()
        if not check:
            msg = str(message or "")
            self.last_error = msg
            if "429" in msg or "temporarily" in msg.lower() or "blocked" in msg.lower():
                raise quotex_client.QuotexRateLimited(
                    "Quotex temporarily blocked us (429). Please wait 15-30 minutes, "
                    "do not retry.")
            raise RuntimeError(f"Quotex login failed: {message}")
        self.connected = True
        self.last_login = dhaka_now()
        logger.info("collector: Quotex session connected (one login for this run)")
        return True

    async def get_open_assets(self):
        """Open assets from the shared session (no extra login)."""
        instruments = await self.client.get_instruments()
        open_assets = []
        for row in instruments or []:
            try:
                if len(row) > 14 and row[14] is True and row[1]:
                    open_assets.append(str(row[1]))
            except Exception:
                continue
        return sorted(set(open_assets))

    async def fetch_asset_span(self, asset, seconds_back):
        """All 1-min candles from now back `seconds_back` seconds."""
        candles = await self.client.get_historical_candles(
            asset=asset,
            amount_of_seconds=int(seconds_back),
            period=60,
            max_workers=5,
        )
        return quotex_client._to_raw_candles(candles)

    async def close(self):
        self.connected = False
        if self.client is not None:
            try:
                await self.client.close()
            except Exception:
                pass
            self.client = None


# ---------------------------------------------------------------------------
# Collection runs (session injected — real or fake for tests)
# ---------------------------------------------------------------------------
async def _fetch_assets_3_at_a_time(session, assets, seconds_back):
    """Fetch one span per asset, 3 at a time, 1s pause between batches.
    Returns {asset: raw_candles or None}. Raises QuotexRateLimited."""
    results = {}
    for i in range(0, len(assets), 3):
        batch = assets[i:i + 3]
        for asset in batch:
            try:
                results[asset] = await session.fetch_asset_span(asset, seconds_back)
            except quotex_client.QuotexRateLimited:
                raise
            except Exception as exc:
                logger.warning("collector: fetch failed for %s: %s", asset, exc)
                results[asset] = None
        await asyncio.sleep(1)
    return results


def _asset_backfill_complete(db, asset, dates, now):
    """True when an asset already has its full backfill span in the DB:
    every completed day in `dates` at all 1440 candles, and today from
    00:00 up to now. Such assets are skipped on a re-run,
    so a restart resumes instead of re-fetching everything. Today is
    included because a restart must still top today up."""
    for d in dates:
        if db.count_candles(asset, d) < CANDLES_PER_DAY:
            return False
    end_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    expected_today = int((min(now, end_of_day) - start).total_seconds() // 60)
    if expected_today > 0:
        if db.count_candles(asset, now.strftime("%Y-%m-%d")) < expected_today:
            return False
    return True


def _store_rows(db, asset, raw, ts_min, ts_max=None):
    """Convert raw candles to rows, keep those in [ts_min, ts_max),
    insert into the DB. Returns (rows_in_window, new_in_db)."""
    rows = rows_from_raw(raw)
    rows = [r for r in rows
            if r["ts"] >= ts_min and (ts_max is None or r["ts"] < ts_max)]
    if not rows:
        return 0, 0
    return len(rows), db.insert_candles(asset, rows)


async def run_backfill(db, session, days=None, now=None):
    """Collect the last `days` completed days + today 00:00-now.

    One fetch per asset covers the whole span; days are split locally.
    Progress is saved per batch: each batch of 3 assets is fetched
    and stored immediately, and assets whose span is already complete
    are skipped — so a restart (Render sleep, crash, redeploy) resumes
    where the last run stopped instead of starting over from asset 1.
    Returns (assets_done, candles_inserted). Raises QuotexRateLimited.
    """
    days = days if days is not None else config.BACKFILL_DAYS
    now = now or dhaka_now()
    dates = backfill_dates(days, now)
    oldest_start, _ = day_window_utc(dates[-1])
    seconds_back = fetch_seconds_back(oldest_start, now)

    assets = await session.get_open_assets()
    if not assets:
        logger.warning("collector: no open assets from Quotex, using sample list")
        assets = list(config.SAMPLE_ASSETS)
    logger.info("collector: backfill starting — %d assets, %d days (%s..%s), "
                "span %d seconds back",
                len(assets), days, dates[-1], dates[0], seconds_back)

    total = 0
    done = 0
    for i in range(0, len(assets), 3):
        batch = assets[i:i + 3]
        todo = [a for a in batch
                if not _asset_backfill_complete(db, a, dates, now)]
        if not todo:
            logger.info("collector: backfill batch %s — already in DB, skipped",
                        ", ".join(batch))
            done += len(batch)
            continue
        raw_by_asset = await _fetch_assets_3_at_a_time(session, todo, seconds_back)
        for asset, raw in raw_by_asset.items():
            if not raw:
                continue
            n_rows, inserted = _store_rows(db, asset, raw, oldest_start)
            total += inserted
            done += 1
            logger.info("collector: backfill %s — %d candles in window, %d new in DB",
                        asset, n_rows, inserted)

    _safe_set_state(db, "last_backfill", utc_iso(now))
    _safe_set_state(db, "backfill_done", "true")
    _safe_set_state(db, "backfill_window", BACKFILL_WINDOW)
    _safe_set_state(db, "last_collection", utc_iso(now))
    nxt = now + timedelta(seconds=config.COLLECTOR_INTERVAL_SECONDS)
    _safe_set_state(db, "next_collection", utc_iso(nxt))
    logger.info("collector: backfill finished — %d assets, %d new candles, "
                "next run at %s (Dhaka)", done, total, nxt.strftime("%H:%M"))
    return done, total


async def run_incremental(db, session, now=None):
    """Collect the last 2 hours (+10 min overlap) for all open assets.

    Each batch of 3 assets is fetched and stored immediately, so a
    restart keeps what was already collected.
    Returns (assets_done, candles_inserted). Raises QuotexRateLimited.
    """
    now = now or dhaka_now()
    start, end = recent_window_utc(now)
    seconds_back = fetch_seconds_back(start, now)

    assets = await session.get_open_assets()
    if not assets:
        logger.warning("collector: no open assets from Quotex, using sample list")
        assets = list(config.SAMPLE_ASSETS)
    logger.info("collector: 2-hourly run starting — %d assets, window %s..%s Dhaka",
                len(assets),
                start.astimezone(DHAKA).strftime("%H:%M"),
                end.astimezone(DHAKA).strftime("%H:%M"))

    total = 0
    done = 0
    for i in range(0, len(assets), 3):
        batch = assets[i:i + 3]
        raw_by_asset = await _fetch_assets_3_at_a_time(session, batch, seconds_back)
        for asset, raw in raw_by_asset.items():
            if not raw:
                continue
            n_rows, inserted = _store_rows(db, asset, raw, start, end)
            total += inserted
            done += 1
    _safe_set_state(db, "last_collection", utc_iso(now))
    nxt = now + timedelta(seconds=config.COLLECTOR_INTERVAL_SECONDS)
    _safe_set_state(db, "next_collection", utc_iso(nxt))
    logger.info("collector: 2-hourly run finished — %d assets, %d new candles, "
                "next run %s Dhaka",
                done, total, nxt.strftime("%H:%M"))
    return done, total


# ---------------------------------------------------------------------------
# Status summary (used by the Bot Status button + backfill log)
# ---------------------------------------------------------------------------
def get_status_summary(db, now=None):
    """Dict for the Bot Status view. No Quotex login here — DB only."""
    now = now or dhaka_now()
    total = db.count_total()
    by_asset = db.count_by_asset()
    dates = db.get_dates()
    today_count, today_str = db.today_coverage(now)
    last_collection = db.get_state("last_collection")
    next_collection = db.get_state("next_collection")
    last_backfill = db.get_state("last_backfill")
    last_login = db.get_state("quotex_last_login")
    session_state = db.get_state("quotex_session", "not connected yet")
    return {
        "mode": config.DATA_MODE,
        "backend": db.backend,
        "total_candles": total,
        "by_asset": by_asset,
        "dates": dates,
        "today_date": today_str,
        "today_count": today_count,
        "last_collection": last_collection,
        "next_collection": next_collection,
        "last_backfill": last_backfill,
        "quotex_last_login": last_login,
        "quotex_session": session_state,
        "now_dhaka": now.strftime("%Y-%m-%d %H:%M"),
    }


def log_status_summary(db, prefix="collector status"):
    """Log a one-block status summary (called after backfill)."""
    s = get_status_summary(db)
    logger.info("%s — mode=%s db=%s candles=%d assets=%d dates=%s "
                "today(%s)=%d last_collection=%s next=%s quotex=%s",
                prefix, s["mode"], s["backend"], s["total_candles"],
                len(s["by_asset"]), ",".join(s["dates"]) or "-",
                s["today_date"], s["today_count"],
                s["last_collection"], s["next_collection"],
                s["quotex_session"])


# ---------------------------------------------------------------------------
# Background loop (started from main.py post_init)
# ---------------------------------------------------------------------------
async def collector_loop(db=None, session_factory=None, sleep=None):
    """Backfill once, then an incremental run every 2 hours, forever.

    session_factory: callable returning a SharedQuotexSession
    (tests inject a fake one). sleep: awaitable seconds (tests inject
    a fast one). Never raises out of the loop — errors are logged
    and the next cycle is tried.
    """
    db = db or candles_db.get_candle_db()
    session_factory = session_factory or SharedQuotexSession
    sleep = sleep or asyncio.sleep
    interval = config.COLLECTOR_INTERVAL_SECONDS
    retry_wait = config.COLLECTOR_429_RETRY_MINUTES * 60

    logger.info("collector: starting (backfill %d days, then every %d minutes)",
                config.BACKFILL_DAYS, interval // 60)

    async def _one_run(kind):
        """One connected run (backfill or incremental). Returns True on
        success, 'rate_limited' on 429, False on other errors."""
        session = session_factory()
        try:
            await session.connect()
            _safe_set_state(db, "quotex_last_login", utc_iso(dhaka_now()))
            _safe_set_state(db, "quotex_session", "connected")
            if kind == "backfill":
                await run_backfill(db, session)
            else:
                await run_incremental(db, session)
            return True
        except quotex_client.QuotexRateLimited as exc:
            _safe_set_state(db, "quotex_session", f"rate limited (429): {exc}")
            logger.warning("collector: 429 — %s", exc)
            return "rate_limited"
        except Exception as exc:
            _safe_set_state(db, "quotex_session", f"error: {exc}")
            logger.warning("collector: run failed: %s", exc)
            return False
        finally:
            try:
                await session.close()
            except Exception:
                pass
            _safe_set_state(db, "quotex_session", "disconnected (between runs)")

    # --- backfill first (skip if already done today) ---
    # The backfill also re-runs when the window version changed
    # (e.g. half-day -> full-day), so the missing half of every day
    # gets collected instead of being skipped as "done".
    done = db.get_state("backfill_done") == "true"
    window_ok = db.get_state("backfill_window") == BACKFILL_WINDOW
    last_bf = db.get_state("last_backfill")
    if done and window_ok and last_bf and last_bf[:10] == dhaka_now().strftime("%Y-%m-%d"):
        logger.info("collector: backfill already done today (%s), skipping", last_bf)
    else:
        try:
            result = await _one_run("backfill")
        except Exception as exc:
            logger.warning("collector: backfill run crashed: %s", exc)
            result = False
        if result == "rate_limited":
            logger.info("collector: backfill hit 429, retrying in %d minutes",
                        config.COLLECTOR_429_RETRY_MINUTES)
            await sleep(retry_wait)
            result = await _one_run("backfill")
            if result == "rate_limited":
                logger.warning("collector: backfill hit 429 again, "
                               "will retry on the next 2-hour cycle")
        if result is True:
            log_status_summary(db, "collector: backfill done")

    # --- then every 2 hours, forever ---
    while True:
        await sleep(interval)
        try:
            result = await _one_run("incremental")
        except Exception as exc:
            logger.warning("collector: incremental run crashed: %s", exc)
            continue
        if result == "rate_limited":
            logger.info("collector: incremental run hit 429, retrying in %d minutes",
                        config.COLLECTOR_429_RETRY_MINUTES)
            await sleep(retry_wait)
            try:
                await _one_run("incremental")
            except Exception as exc:
                logger.warning("collector: incremental retry crashed: %s", exc)
