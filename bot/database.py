"""User database — Postgres on Render (DATABASE_URL set), SQLite file
locally. Same two-backend pattern as candles_db: '?' placeholders are
translated to '%s' for Postgres, and a dead Postgres connection is
reconnected once and retried.

Why Postgres here: on Render the SQLite file lives inside the web
service container, so every deploy/restart wipes users, premium
status and auto-signal subscriptions. The candle data already lives
in Postgres; users belong there too.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import config

try:
    import psycopg2 as _psycopg2
    _CONNECTION_ERRORS = (_psycopg2.InterfaceError, _psycopg2.OperationalError,
                          sqlite3.ProgrammingError)
except ImportError:  # pragma: no cover - no psycopg2 installed locally
    _CONNECTION_ERRORS = (sqlite3.ProgrammingError,)

SCHEMA_USERS_PG = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    username TEXT,
    premium_until TEXT,
    auto_signals INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS premium_requests (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT,
    txn_hash TEXT,
    created_at TEXT,
    status TEXT DEFAULT 'pending'
);
"""

SCHEMA_USERS_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    premium_until TEXT,
    auto_signals INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS premium_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER,
    txn_hash TEXT,
    created_at TEXT,
    status TEXT DEFAULT 'pending'
);
"""


def _backend():
    return "postgres" if (config.DATABASE_URL or "").strip() else "sqlite"


_CONN = None


def _ensure_last_seen_column():
    """Add the last_seen column to a users table created before it
    existed (Render's Postgres already has the table). Postgres
    supports ADD COLUMN IF NOT EXISTS; SQLite raises
    'duplicate column name' when it already exists — both are fine."""
    try:
        if _backend() == "postgres":
            _execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen TEXT")
        else:
            try:
                _execute("ALTER TABLE users ADD COLUMN last_seen TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise
    except Exception:
        # A users table that cannot be migrated must never break the
        # bot — the dashboard simply reports what it can.
        import logging
        logging.getLogger("littlematrix.database").warning(
            "last_seen column migration failed", exc_info=True)


def _connect():
    global _CONN
    if _CONN is not None:
        return _CONN
    if _backend() == "postgres":
        import psycopg2
        _CONN = psycopg2.connect(config.DATABASE_URL)
        _CONN.autocommit = True
        cur = _CONN.cursor()
        cur.execute(SCHEMA_USERS_PG)
    else:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONN = sqlite3.connect(str(config.DB_PATH))
        _CONN.execute("PRAGMA journal_mode=WAL")
        _CONN.executescript(SCHEMA_USERS_SQLITE)
        _CONN.commit()
    _ensure_last_seen_column()
    return _CONN


def _q(sql):
    return sql.replace("?", "%s") if _backend() == "postgres" else sql


def _execute(sql, params=()):
    """One statement, reconnecting once on a dead connection."""
    try:
        return _execute_once(sql, params)
    except _CONNECTION_ERRORS:
        global _CONN
        _CONN = None
        return _execute_once(sql, params)


def _execute_once(sql, params=()):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q(sql), params)
    if _backend() == "sqlite":
        conn.commit()
    return cur


def connect():
    """Raw connection (backwards compatible helper)."""
    return _connect()


def touch_last_seen(telegram_id):
    """Mark a user as active right now. Called on every interaction
    (via get_or_create_user) — the Admin Dashboard's 'Online' count
    is users whose last_seen is within the last 10 minutes."""
    try:
        now = datetime.now().isoformat(timespec="seconds")
        _execute("UPDATE users SET last_seen=? WHERE telegram_id=?",
                 (now, telegram_id))
    except Exception:
        pass  # activity tracking must never break the bot


def get_or_create_user(telegram_id, username=""):
    row = _execute(
        "SELECT telegram_id, username, premium_until, auto_signals "
        "FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    if row is None:
        now = datetime.now().isoformat(timespec="seconds")
        _execute(
            "INSERT INTO users (telegram_id, username, premium_until, auto_signals, created_at, last_seen) "
            "VALUES (?, ?, NULL, 0, ?, ?)", (telegram_id, username, now, now))
        return {"telegram_id": telegram_id, "username": username,
                "premium_until": None, "auto_signals": False}
    touch_last_seen(telegram_id)
    return {"telegram_id": row[0], "username": row[1],
            "premium_until": row[2], "auto_signals": bool(row[3])}


def get_user_stats():
    """{total, premium, free, online} for the Admin Dashboard.

    premium: premium_until in the future. free: total - premium.
    online: last_seen within the last 10 minutes — Telegram gives
    no presence, so 'online' means 'used the bot recently'
    (the owner, 2026-11-27)."""
    now = datetime.now()
    try:
        rows = _execute(
            "SELECT premium_until, last_seen FROM users").fetchall()
    except Exception:
        return {"total": 0, "premium": 0, "free": 0, "online": 0}
    total = len(rows)
    premium = 0
    online = 0
    for until, last_seen in rows:
        if until:
            try:
                if datetime.fromisoformat(until) > now:
                    premium += 1
            except ValueError:
                pass
        if last_seen:
            try:
                if (now - datetime.fromisoformat(last_seen)).total_seconds() <= 600:
                    online += 1
            except ValueError:
                pass
    return {"total": total, "premium": premium,
            "free": total - premium, "online": online}


def is_premium(telegram_id):
    user = get_or_create_user(telegram_id)
    if not user.get("premium_until"):
        return False
    try:
        return datetime.fromisoformat(user["premium_until"]) > datetime.now()
    except ValueError:
        return False


def set_premium_until(telegram_id, until_iso):
    """Admin approve: set premium_until (ISO string) for a user."""
    get_or_create_user(telegram_id)
    _execute("UPDATE users SET premium_until=? WHERE telegram_id=?",
             (until_iso, telegram_id))


def grant_premium(telegram_id, days=30):
    """Approve a premium request: premium starts now, lasts `days`."""
    until = (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds")
    set_premium_until(telegram_id, until)
    return until


def save_premium_request(telegram_id, txn_hash):
    now = datetime.now().isoformat(timespec="seconds")
    _execute(
        "INSERT INTO premium_requests (telegram_id, txn_hash, created_at) "
        "VALUES (?, ?, ?)", (telegram_id, txn_hash, now))
    row = _execute(
        "SELECT id FROM premium_requests WHERE telegram_id=? AND txn_hash=? "
        "ORDER BY id DESC LIMIT 1", (telegram_id, txn_hash)).fetchone()
    return row[0] if row else None


def get_premium_request(request_id):
    row = _execute(
        "SELECT id, telegram_id, txn_hash, created_at, status "
        "FROM premium_requests WHERE id=?", (request_id,)).fetchone()
    if not row:
        return None
    return {"id": row[0], "telegram_id": row[1], "txn_hash": row[2],
            "created_at": row[3], "status": row[4]}


def get_pending_premium_requests():
    cur = _execute(
        "SELECT id, telegram_id, txn_hash, created_at FROM premium_requests "
        "WHERE status='pending' ORDER BY id")
    return [{"id": r[0], "telegram_id": r[1], "txn_hash": r[2],
             "created_at": r[3]} for r in cur.fetchall()]


def set_premium_request_status(request_id, status):
    _execute("UPDATE premium_requests SET status=? WHERE id=?",
             (status, request_id))


def set_auto_signals(telegram_id, on):
    get_or_create_user(telegram_id)
    _execute("UPDATE users SET auto_signals=? WHERE telegram_id=?",
             (1 if on else 0, telegram_id))


def is_auto_subscriber(telegram_id):
    return bool(get_or_create_user(telegram_id).get("auto_signals"))


def get_auto_subscribers():
    cur = _execute(
        "SELECT telegram_id FROM users WHERE auto_signals=1")
    return [r[0] for r in cur.fetchall()]
