"""Background candle collector (Render / db mode).

Anti-429 design (agreed with the owner):
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

# One Quotex login at a time, across the whole process: the 2-hourly
# collector runs and the live stream's reconnects share this lock, so
# they can never log in simultaneously (two logins at once is what
# trips Quotex 429). Only the login call itself is locked, never a
# whole collection run.
_CONNECT_LOCK = asyncio.Lock()

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
        # One login at a time across the process (see _CONNECT_LOCK):
        # the collector runs and the live stream's reconnects share it.
        async with _CONNECT_LOCK:
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

    async def get_payouts(self):
        """{asset: (payout_1m, is_open)} from the live instruments list.

        Same WS call as get_open_assets. The 1M payout is row[-9]
        (falling back to row[5], the base payment). The collector
        stores one snapshot per run, so the Payouts button reads from
        the DB and never logs in to Quotex.
        """
        instruments = await self.client.get_instruments()
        out = {}
        for row in instruments or []:
            try:
                if not row or len(row) < 15 or not row[1]:
                    continue
                payout = None
                for idx in (-9, 5):
                    try:
                        payout = float(row[idx])
                        break
                    except (TypeError, ValueError, IndexError):
                        continue
                out[str(row[1])] = (payout, bool(row[14]))
            except Exception:
                continue
        return out

    async def fetch_asset_span(self, asset, seconds_back):
        """All 1-min candles from now back `seconds_back` seconds."""
        candles = await self.client.get_historical_candles(
            asset=asset,
            amount_of_seconds=int(seconds_back),
            period=60,
            max_workers=5,
        )
        return quotex_client._to_raw_candles(candles)

    async def start_asset_streams(self, assets, period=60):
        """Subscribe to closed-candle events for `assets` (live stream).

        One subscribe per asset, with a small delay between them —
        sending 90 subscribes at once risks a ban/rate-limit (see the
        pyquotex README warning). A failed subscribe is logged and
        skipped; the 2-hourly batch still collects that asset.
        """
        # 2026-11-26: the name-based subscribe alone never delivered
        # a single 'candle-generated' event (live diagnostic: "no
        # candle-generated events in the last 60s" on every instance),
        # while the history/fetch path on the same session works. The
        # pyquotex helper that waits for the first event
        # (start_candles_one_stream) follows with the asset's NUMERIC
        # code, not its name — so also follow with the code, best
        # effort only. A missing code map or a failed follow must
        # never break the stream (the batch still collects).
        code_follows = 0
        for asset in assets:
            try:
                await self.client.start_candles_stream(asset, period)
            except Exception as exc:
                logger.warning("collector: stream subscribe failed for %s: %s",
                               asset, exc)
            try:
                codes = getattr(self.client, "codes_asset", None) or {}
                code = codes.get(asset)
                api = getattr(self.client, "api", None)
                if code is not None and api is not None:
                    await api.follow_candle(code)
                    code_follows += 1
            except Exception as exc:
                logger.warning("collector: stream code follow failed for %s: %s",
                               asset, exc)
            await asyncio.sleep(config.LIVE_STREAM_SUBSCRIBE_DELAY)
        if code_follows:
            logger.info("collector: stream code follow sent for %d assets",
                        code_follows)

    async def is_connected(self):
        """True when the WebSocket is still up (state check, no network)."""
        try:
            return bool(await self.client.check_connect())
        except Exception:
            return False

    def generated_candles(self):
        """Latest closed-candle event per asset: {asset: {period: event}}.

        The Quotex server sends a 'candle-generated' event each time a
        candle closes; pyquotex stores the newest per asset+period.
        An event carries index (unix ts), open, high, low, close.
        """
        try:
            return self.client.api.candle_generated_check or {}
        except Exception:
            return {}

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
    `seconds_back` may be one number (same span for all) or a dict
    {asset: seconds} for per-asset tail fetches.
    Returns {asset: raw_candles or None}. Raises QuotexRateLimited."""
    results = {}
    for i in range(0, len(assets), 3):
        batch = assets[i:i + 3]
        for asset in batch:
            sb = seconds_back[asset] if isinstance(seconds_back, dict) else seconds_back
            try:
                results[asset] = await session.fetch_asset_span(asset, sb)
            except quotex_client.QuotexRateLimited:
                raise
            except Exception as exc:
                logger.warning("collector: fetch failed for %s: %s", asset, exc)
                results[asset] = None
        await asyncio.sleep(1)
    return results


async def _capture_payouts(db, session):
    """Store one payout snapshot from the live session. Nice-to-have:
    it must never fail a collection run, so every error is swallowed."""
    try:
        payouts = await session.get_payouts()
    except AttributeError:
        return  # FakeSession in tests has no get_payouts()
    except Exception as exc:
        logger.warning("collector: payout capture failed: %s", exc)
        return
    if not payouts:
        return
    try:
        # Dhaka time string — the Payouts button shows it as-is.
        now = dhaka_now().strftime("%Y-%m-%d %H:%M:%S")
        db.set_payouts([(a, p, o, now) for a, (p, o) in payouts.items()])
        logger.info("collector: payouts stored for %d assets", len(payouts))
    except Exception as exc:
        logger.warning("collector: payout store failed: %s", exc)


def _tail_seconds_back(db, asset, oldest_start, now):
    """Seconds to fetch for one asset: only the missing tail.

    When the asset already has candles up to `last`, only the span
    from `last` (minus a 10-min overlap) to now is fetched — the daily
    top-up becomes a few batches instead of the whole 5-day span
    (~40 batches). Assets with no candles, or whose last candle is
    older than the backfill window, still get the full span.
    """
    last = db.last_candle_time(asset)
    if not last:
        return fetch_seconds_back(oldest_start, now)
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return fetch_seconds_back(oldest_start, now)
    if last_dt < oldest_start:
        return fetch_seconds_back(oldest_start, now)
    tail_start = last_dt - timedelta(minutes=10)
    return max(600, int((now.astimezone(timezone.utc) - tail_start).total_seconds()))


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
        # Tail fetch: each asset fetches only its missing part
        # (last stored candle -> now), not the whole 5-day span.
        spans = {a: _tail_seconds_back(db, a, oldest_start, now) for a in todo}
        raw_by_asset = await _fetch_assets_3_at_a_time(session, todo, spans)
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
def _dhaka_display(utc_iso_str):
    """Stored UTC 'YYYY-MM-DD HH:MM:SS' -> Dhaka string for display.

    Bot Status used to show the raw UTC value next to a Dhaka 'Time
    now', so every time looked 6 hours stale (last collection 05:48
    read as 5 AM when it was really 11:48 Dhaka).
    """
    if not utc_iso_str:
        return None
    try:
        dt = datetime.strptime(str(utc_iso_str), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc)
        return dt.astimezone(DHAKA).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(utc_iso_str)


def get_status_summary(db, now=None):
    """Dict for the Bot Status view. No Quotex login here — DB only.

    Times are Dhaka (the stored values are UTC). The live stream's
    own status is included too — the 'Quotex session' line only
    covers the collector, which is deliberately disconnected between
    its 2-hourly runs.
    """
    now = now or dhaka_now()
    total = db.count_total()
    by_asset = db.count_by_asset()
    dates = db.get_dates()
    today_count, today_str = db.today_coverage(now)
    last_collection = _dhaka_display(db.get_state("last_collection"))
    next_collection = _dhaka_display(db.get_state("next_collection"))
    last_backfill = _dhaka_display(db.get_state("last_backfill"))
    last_login = _dhaka_display(db.get_state("quotex_last_login"))
    session_state = db.get_state("quotex_session", "not connected yet")
    stream_status = db.get_state("live_stream_status")
    stream_last_seen = _dhaka_display(db.get_state("live_stream_last_seen"))
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
        "live_stream_status": stream_status,
        "live_stream_last_seen": stream_last_seen,
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
async def _send_auto_signals(bot, db):
    """After a 2-hourly collection, send the next window's top signals
    to Auto Signal subscribers. Best-effort: failures are logged,
    never raised — the collector loop must keep running."""
    if bot is None:
        return
    try:
        from . import database as users_db
        subs = users_db.get_auto_subscribers()
    except Exception:
        return
    if not subs:
        return
    try:
        from .signal_engine import (
            cross_asset_filter, format_signals_list,
            generate_futures_signals_from_db)
        from .premium_emoji import pe as _pe
        now = dhaka_now()
        start_dt = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        end_dt = start_dt + timedelta(hours=1, minutes=59)
        start, end = start_dt.strftime("%H:%M"), end_dt.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")
        all_dates = db.get_dates()
        completed = [d for d in all_dates if d < today] or all_dates
        dates = completed[-3:]
        assets = [a for a in db.get_assets() if a.endswith("_otc")]
        all_signals = []
        for a in assets:
            all_signals.extend(
                generate_futures_signals_from_db(db, a, dates, start, end))
        kept = cross_asset_filter(all_signals)
        lines = format_signals_list(kept)
        if not lines:
            return
        text = (f"{_pe('diamond')}{_pe('lightning')} BLUE FISH PRIME • AUTO SIGNALS {start}-{end} (UTC+6)\n"
                f"Dates: {dates[0]} to {dates[-1]} | M1 | Quotex | Source: db\n\n"
                + "\n".join(lines))[:4000]
        for uid in subs:
            try:
                await bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
                await asyncio.sleep(0.1)
            except Exception as exc:
                logger.warning("collector: auto signal to %s failed: %s", uid, exc)
    except Exception as exc:
        logger.warning("collector: auto signal send failed: %s", exc)


async def collector_loop(db=None, session_factory=None, sleep=None, bot=None):
    """Backfill once, then an incremental run every 2 hours, forever.

    session_factory: callable returning a SharedQuotexSession
    (tests inject a fake one). sleep: awaitable seconds (tests inject
    a fast one). bot: telegram Bot for Auto Signal sends (None = off).
    Never raises out of the loop — errors are logged
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
            await _capture_payouts(db, session)
            if kind == "backfill":
                await run_backfill(db, session)
            else:
                await run_incremental(db, session)
                await _send_auto_signals(bot, db)
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
