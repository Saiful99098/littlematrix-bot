"""SQLite user database (local test). Render will use Postgres later."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    premium_until TEXT,
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


def connect():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def get_or_create_user(telegram_id, username=""):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT telegram_id, username, premium_until FROM users WHERE telegram_id=?",
            (telegram_id,)).fetchone()
        if row is None:
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO users (telegram_id, username, premium_until, created_at) "
                "VALUES (?, ?, NULL, ?)", (telegram_id, username, now))
            conn.commit()
            return {"telegram_id": telegram_id, "username": username,
                    "premium_until": None}
        return {"telegram_id": row[0], "username": row[1],
                "premium_until": row[2]}
    finally:
        conn.close()


def is_premium(telegram_id):
    user = get_or_create_user(telegram_id)
    if not user.get("premium_until"):
        return False
    try:
        return datetime.fromisoformat(user["premium_until"]) > datetime.now()
    except ValueError:
        return False


def save_premium_request(telegram_id, txn_hash):
    conn = connect()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO premium_requests (telegram_id, txn_hash, created_at) "
            "VALUES (?, ?, ?)", (telegram_id, txn_hash, now))
        conn.commit()
    finally:
        conn.close()
