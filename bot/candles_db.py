"""Candle database — one schema, two backends.

- Postgres when DATABASE_URL is set (Render free Postgres).
- SQLite file (data/candles.db) otherwise (local testing).

Tables:
    candles (asset TEXT, ts TIMESTAMP, open/high/low/close REAL,
             UNIQUE(asset, ts))
    collector_state (key TEXT PRIMARY KEY, value TEXT)

All timestamps are stored in UTC as 'YYYY-MM-DD HH:MM:SS'.
The collection window is Asia/Dhaka 00:00-11:59, so a Dhaka date D
covers UTC (D-1) 18:00 up to D 06:00 (half-open).

Only the candles module uses this file. The user/premium tables
stay in bot/database.py (SQLite) — moving them to Postgres is a
later step; on Render they reset when the service restarts.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from . import config

logger = logging.getLogger("littlematrix.candles_db")

# Errors that mean "the server closed our connection" — the query can
# be retried once on a fresh connection. psycopg2 is imported lazily
# (it is only installed where Postgres is used); sqlite raises
# ProgrammingError on a closed connection.
_CONNECTION_ERRORS = (sqlite3.ProgrammingError,)
try:
    import psycopg2 as _psycopg2
    _CONNECTION_ERRORS = (_psycopg2.InterfaceError, _psycopg2.OperationalError,
                          sqlite3.ProgrammingError)
except ImportError:
    pass


def normalize_asset_name(asset):
    """Asset name in the form the DB stores it.

    Quotex instruments are stored as e.g. 'BTCUSD_otc' (base upper,
    '_otc' lower) or 'EURUSD'. A pasted signal line gives 'BTCUSD-OTC'
    (all upper) — without this, `WHERE asset = ?` (case-sensitive on
    both Postgres and SQLite) finds nothing and the CHECKER reports
    NO DATA even though the candles are in the DB. Every method below
    normalizes its asset argument through this helper.
    """
    a = (asset or "").strip().upper().replace("-", "_")
    if a.endswith("_OTC"):
        a = a[:-4] + "_otc"
    return a

try:
    from zoneinfo import ZoneInfo
    DHAKA = ZoneInfo("Asia/Dhaka")
except Exception:
    DHAKA = timezone(timedelta(hours=6))

UTC = timezone.utc

SCHEMA_CANDLES = """
CREATE TABLE IF NOT EXISTS candles (
    asset TEXT NOT NULL,
    ts TIMESTAMP NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    UNIQUE(asset, ts)
);
CREATE INDEX IF NOT EXISTS idx_candles_asset_ts ON candles (asset, ts);
"""

SCHEMA_STATE = """
CREATE TABLE IF NOT EXISTS collector_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def utc_iso(dt):
    """Aware datetime -> 'YYYY-MM-DD HH:MM:SS' UTC string."""
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def dhaka_date_range_utc(date_str):
    """Half-open UTC range [start, end) for one Dhaka date's
    collection window (00:00-11:59 Dhaka = 18:00 prev day - 06:00
    UTC). Returns (start_dt, end_dt) as aware UTC datetimes."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    start_dhaka = datetime(d.year, d.month, d.day, 0, 0, tzinfo=DHAKA)
    end_dhaka = datetime(d.year, d.month, d.day, 12, 0, tzinfo=DHAKA)
    return start_dhaka.astimezone(UTC), end_dhaka.astimezone(UTC)


def _to_iso(value):
    """A DB timestamp value (str or datetime) -> ISO string."""
    if isinstance(value, datetime):
        return utc_iso(value)
    return str(value)


def _dhaka_str_from_utc_iso(iso_str):
    """UTC ISO string -> Dhaka 'YYYY-MM-DD HH:MM:SS' string."""
    dt = datetime.strptime(iso_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return dt.astimezone(DHAKA).strftime("%Y-%m-%d %H:%M:%S")


class CandleDB:
    """Thin wrapper over sqlite3 or psycopg2 with one SQL dialect.

    SQL is written with '?' placeholders (sqlite style) and translated
    to '%s' for Postgres. No literal '?' appears in any query here.
    """

    def __init__(self, database_url=None, sqlite_path=None):
        self.database_url = database_url or config.DATABASE_URL or ""
        self.sqlite_path = sqlite_path or config.CANDLES_DB_PATH
        self.backend = "postgres" if self.database_url else "sqlite"
        self._conn = None

    # -- connection ---------------------------------------------------
    def connect(self):
        if self._conn is not None:
            return self._conn
        if self.backend == "postgres":
            import psycopg2
            self._conn = psycopg2.connect(self.database_url)
            self._conn.autocommit = True
        else:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.sqlite_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def init(self):
        conn = self.connect()
        cur = conn.cursor()
        if self.backend == "postgres":
            # psycopg2 accepts several statements in one execute().
            cur.execute(SCHEMA_CANDLES)
            cur.execute(SCHEMA_STATE)
        else:
            cur.executescript(SCHEMA_CANDLES)
            cur.executescript(SCHEMA_STATE)
            conn.commit()
        return self

    # -- helpers ------------------------------------------------------
    def _q(self, sql):
        if self.backend == "postgres":
            return sql.replace("?", "%s")
        return sql

    def _execute(self, sql, params=()):
        try:
            return self._execute_once(sql, params)
        except _CONNECTION_ERRORS:
            # Render's Postgres closes idle connections (and restarts
            # the free DB) — the next query then raises InterfaceError:
            # "connection already closed". Reconnect once and retry.
            logger.warning("candles_db: connection lost, reconnecting once")
            self._conn = None
            return self._execute_once(sql, params)

    def _execute_once(self, sql, params=()):
        conn = self.connect()
        cur = conn.cursor()
        cur.execute(self._q(sql), params)
        if self.backend == "sqlite":
            conn.commit()
        return cur

    def _executemany(self, sql, rows):
        try:
            return self._executemany_once(sql, rows)
        except _CONNECTION_ERRORS:
            logger.warning("candles_db: connection lost, reconnecting once")
            self._conn = None
            return self._executemany_once(sql, rows)

    def _executemany_once(self, sql, rows):
        conn = self.connect()
        cur = conn.cursor()
        cur.executemany(self._q(sql), rows)
        if self.backend == "sqlite":
            conn.commit()
        return cur

    # -- candles ------------------------------------------------------
    def insert_candles(self, asset, rows):
        """Insert candles. rows: list of dicts with keys
        ts (aware datetime, UTC, or ISO string), open, high, low, close.
        Duplicates (same asset+ts) are ignored by the UNIQUE index.
        Returns the number of NEW rows inserted."""
        asset = normalize_asset_name(asset)
        if not rows:
            return 0
        data = []
        for r in rows:
            ts = r["ts"]
            if isinstance(ts, datetime):
                ts = utc_iso(ts)
            data.append((asset, ts, float(r["open"]), float(r["high"]),
                         float(r["low"]), float(r["close"])))
        if self.backend == "postgres":
            sql = ("INSERT INTO candles (asset, ts, open, high, low, close) "
                   "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (asset, ts) DO NOTHING")
        else:
            sql = ("INSERT OR IGNORE INTO candles (asset, ts, open, high, low, close) "
                   "VALUES (?, ?, ?, ?, ?, ?)")
        cur = self._executemany(sql, data)
        return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(data)

    def get_candles(self, asset, date_str):
        """Candles for one asset on one Dhaka date (00:00-11:59 window).

        Returns raw-like dicts in sample-file format, sorted by time:
        {"time": unix, "datetime": "YYYY-MM-DD HH:MM:SS" (Dhaka),
         "H", "L", "O", "C"} — signal_engine.normalize_candle reads
        exactly this shape.
        """
        asset = normalize_asset_name(asset)
        start, end = dhaka_date_range_utc(date_str)
        cur = self._execute(
            "SELECT ts, open, high, low, close FROM candles "
            "WHERE asset = ? AND ts >= ? AND ts < ? ORDER BY ts",
            (asset, utc_iso(start), utc_iso(end)))
        out = []
        for ts, o, h, low, c in cur.fetchall():
            iso = _to_iso(ts)
            dt_utc = datetime.strptime(iso, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            out.append({
                "time": int(dt_utc.timestamp()),
                "datetime": _dhaka_str_from_utc_iso(iso),
                "H": h, "L": low, "O": o, "C": c,
            })
        return out

    def get_candle(self, asset, date_str, hhmm):
        """One candle for asset + Dhaka date + HH:MM, or None."""
        for c in self.get_candles(asset, date_str):
            if c["datetime"][11:16] == hhmm:
                return c
        return None

    def get_candles_range(self, asset, start_utc, end_utc):
        """Candles for one asset in a UTC [start, end) range.
        Same raw-like dict format as get_candles()."""
        asset = normalize_asset_name(asset)
        cur = self._execute(
            "SELECT ts, open, high, low, close FROM candles "
            "WHERE asset = ? AND ts >= ? AND ts < ? ORDER BY ts",
            (asset, utc_iso(start_utc), utc_iso(end_utc)))
        out = []
        for ts, o, h, low, c in cur.fetchall():
            iso = _to_iso(ts)
            dt_utc = datetime.strptime(iso, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            out.append({
                "time": int(dt_utc.timestamp()),
                "datetime": _dhaka_str_from_utc_iso(iso),
                "H": h, "L": low, "O": o, "C": c,
            })
        return out

    def count_candles(self, asset, date_str):
        """Candles stored for one asset on one Dhaka date (00:00-11:59).
        Used by the collector to skip assets whose backfill span is
        already complete, so a restart resumes instead of re-fetching."""
        asset = normalize_asset_name(asset)
        start, end = dhaka_date_range_utc(date_str)
        cur = self._execute(
            "SELECT COUNT(*) FROM candles "
            "WHERE asset = ? AND ts >= ? AND ts < ?",
            (asset, utc_iso(start), utc_iso(end)))
        return cur.fetchone()[0]

    def get_assets(self):
        cur = self._execute("SELECT DISTINCT asset FROM candles ORDER BY asset")
        return [row[0] for row in cur.fetchall()]

    def get_dates(self, asset=None):
        """Dhaka dates (00:00-11:59 window) that have candles, sorted.
        A candle belongs to the Dhaka date of its Dhaka wall-clock time
        (UTC ts + 6h)."""
        if asset:
            asset = normalize_asset_name(asset)
            cur = self._execute(
                "SELECT ts FROM candles WHERE asset = ? ORDER BY ts", (asset,))
        else:
            cur = self._execute("SELECT ts FROM candles ORDER BY ts")
        dates = set()
        for (ts,) in cur.fetchall():
            iso = _to_iso(ts)
            dates.add(_dhaka_str_from_utc_iso(iso)[:10])
        return sorted(dates)

    def count_total(self):
        cur = self._execute("SELECT COUNT(*) FROM candles")
        return cur.fetchone()[0]

    def count_by_asset(self):
        cur = self._execute(
            "SELECT asset, COUNT(*) FROM candles GROUP BY asset ORDER BY asset")
        return {row[0]: row[1] for row in cur.fetchall()}

    def daily_counts(self):
        """{date_str: {asset: count}} for the Dhaka dates in the DB.
        Built in Python from the ts values so the SQL stays identical
        on both backends."""
        cur = self._execute("SELECT asset, ts FROM candles ORDER BY ts")
        out = {}
        for asset, ts in cur.fetchall():
            d = _dhaka_str_from_utc_iso(_to_iso(ts))[:10]
            out.setdefault(d, {}).setdefault(asset, 0)
            out[d][asset] += 1
        return out

    def today_coverage(self, now=None):
        """Candles stored for today (Dhaka) from 00:00 up to now.
        Returns (count, today_date_str)."""
        now = now or datetime.now(DHAKA)
        today = now.strftime("%Y-%m-%d")
        start, _ = dhaka_date_range_utc(today)
        cur = self._execute(
            "SELECT COUNT(*) FROM candles WHERE ts >= ? AND ts < ?",
            (utc_iso(start), utc_iso(now.astimezone(UTC))))
        return cur.fetchone()[0], today

    # -- collector state ----------------------------------------------
    def get_state(self, key, default=None):
        cur = self._execute(
            "SELECT value FROM collector_state WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else default

    def set_state(self, key, value):
        if self.backend == "postgres":
            self._execute(
                "INSERT INTO collector_state (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, str(value)))
        else:
            self._execute(
                "INSERT OR REPLACE INTO collector_state (key, value) VALUES (?, ?)",
                (key, str(value)))


# Module-level singleton (one DB per process, like database.py).
_DB = None


def get_candle_db():
    global _DB
    if _DB is None:
        _DB = CandleDB().init()
    return _DB


def reset_candle_db():
    """Tests only: drop the singleton so a fresh DB is built."""
    global _DB
    if _DB is not None:
        _DB.close()
    _DB = None
