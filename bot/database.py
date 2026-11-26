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
    created_at TEXT
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
    created_at TEXT
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


def get_or_create_user(telegram_id, username=""):
    row = _execute(
        "SELECT telegram_id, username, premium_until, auto_signals "
        "FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    if row is None:
        now = datetime.now().isoformat(timespec="seconds")
        _execute(
            "INSERT INTO users (telegram_id, username, premium_until, auto_signals, created_at) "
            "VALUES (?, ?, NULL, 0, ?)", (telegram_id, username, now))
        return {"telegram_id": telegram_id, "username": username,
                "premium_until": None, "auto_signals": False}
    return {"telegram_id": row[0], "username": row[1],
            "premium_until": row[2], "auto_signals": bool(row[3])}


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
