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
    status TEXT DEFAULT 'pending',
    days INTEGER NOT NULL DEFAULT 30
);
-- Free-user daily usage (the owner, 2026-11-28): one row per
-- (user, feature, date); count is how many times the free user ran
-- that feature that day. Premium users are unlimited and never
-- counted.
CREATE TABLE IF NOT EXISTS usage (
    telegram_id BIGINT,
    feature TEXT,
    date TEXT,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (telegram_id, feature, date)
);
-- MATRIX referral (the owner, 2026-11-27, Option B pricing):
-- one invite link per referrer; one invited user counts once, ever
-- (invited_id UNIQUE), so leave-and-rejoin cannot farm coins.
CREATE TABLE IF NOT EXISTS referral_links (
    referrer_id BIGINT PRIMARY KEY,
    invite_link TEXT NOT NULL,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS referrals (
    id SERIAL PRIMARY KEY,
    referrer_id BIGINT NOT NULL,
    invited_id BIGINT NOT NULL UNIQUE,
    invite_link TEXT,
    joined_at TEXT,
    coins_awarded INTEGER NOT NULL DEFAULT 5
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
    status TEXT DEFAULT 'pending',
    days INTEGER NOT NULL DEFAULT 30
);
CREATE TABLE IF NOT EXISTS usage (
    telegram_id INTEGER,
    feature TEXT,
    date TEXT,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (telegram_id, feature, date)
);
CREATE TABLE IF NOT EXISTS referral_links (
    referrer_id INTEGER PRIMARY KEY,
    invite_link TEXT NOT NULL,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER NOT NULL,
    invited_id INTEGER NOT NULL UNIQUE,
    invite_link TEXT,
    joined_at TEXT,
    coins_awarded INTEGER NOT NULL DEFAULT 5
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


def _ensure_auto_asset_column():
    """Add the Live Auto Signal market column to a users table created
    before per-market auto signals existed. Same tolerant pattern as
    matrix_coins: a migration failure must never break the bot."""
    try:
        if _backend() == "postgres":
            _execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS auto_asset TEXT")
        else:
            try:
                _execute("ALTER TABLE users ADD COLUMN auto_asset TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise
    except Exception:
        pass  # a failed migration must never break the bot


def _ensure_menu_started_at_column():
    """Add the /start timestamp column to a users table created
    before it existed. Same tolerant pattern as auto_mtg1: a
    migration failure must never break the bot."""
    try:
        if _backend() == "postgres":
            _execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS menu_started_at TEXT")
        else:
            try:
                _execute("ALTER TABLE users ADD COLUMN menu_started_at TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise
    except Exception:
        pass  # a failed migration must never break the bot


def _ensure_auto_mtg1_column():
    """Add the Live Auto Signal MTG mode column to a users table
    created before it existed. Same tolerant pattern as
    auto_asset: a migration failure must never break the bot."""
    try:
        if _backend() == "postgres":
            _execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS auto_mtg1 INTEGER NOT NULL DEFAULT 0")
        else:
            try:
                _execute("ALTER TABLE users ADD COLUMN auto_mtg1 INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise
    except Exception:
        pass  # a failed migration must never break the bot


def _ensure_matrix_coins_column():
    """Add the MATRIX coin balance column to a users table created
    before referrals existed. Same tolerant pattern as last_seen:
    a migration failure must never break the bot."""
    try:
        if _backend() == "postgres":
            _execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS matrix_coins INTEGER NOT NULL DEFAULT 0")
        else:
            try:
                _execute("ALTER TABLE users ADD COLUMN matrix_coins INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise
    except Exception:
        import logging
        logging.getLogger("littlematrix.database").warning(
            "matrix_coins column migration failed", exc_info=True)



def _ensure_premium_days_column():
    """Add the premium plan length (days) to premium_requests,
    created before per-plan requests existed. Same tolerant pattern
    as auto_mtg1: a migration failure must never break the bot."""
    try:
        if _backend() == "postgres":
            _execute("ALTER TABLE premium_requests ADD COLUMN IF NOT EXISTS days INTEGER NOT NULL DEFAULT 30")
        else:
            try:
                _execute("ALTER TABLE premium_requests ADD COLUMN days INTEGER NOT NULL DEFAULT 30")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise
    except Exception:
        pass  # a failed migration must never break the bot


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
    _ensure_matrix_coins_column()
    _ensure_auto_asset_column()
    _ensure_auto_mtg1_column()
    _ensure_menu_started_at_column()
    _ensure_premium_days_column()
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


def save_premium_request(telegram_id, txn_hash, days=30):
    """Store a premium purchase request, with the plan length
    (days) the user picked — the admin approves exactly that plan
    (the owner, 2026-11-28: 4 plans, $2/1 day, $3/5, $5/15, $8/30)."""
    now = datetime.now().isoformat(timespec="seconds")
    _execute(
        "INSERT INTO premium_requests (telegram_id, txn_hash, created_at, days) "
        "VALUES (?, ?, ?, ?)", (telegram_id, txn_hash, now, int(days)))
    row = _execute(
        "SELECT id FROM premium_requests WHERE telegram_id=? AND txn_hash=? "
        "ORDER BY id DESC LIMIT 1", (telegram_id, txn_hash)).fetchone()
    return row[0] if row else None


def get_premium_request(request_id):
    row = _execute(
        "SELECT id, telegram_id, txn_hash, created_at, status, days "
        "FROM premium_requests WHERE id=?", (request_id,)).fetchone()
    if not row:
        return None
    return {"id": row[0], "telegram_id": row[1], "txn_hash": row[2],
            "created_at": row[3], "status": row[4],
            "days": int(row[5]) if len(row) > 5 and row[5] is not None else 30}


def get_pending_premium_requests():
    cur = _execute(
        "SELECT id, telegram_id, txn_hash, created_at, days FROM premium_requests "
        "WHERE status='pending' ORDER BY id")
    return [{"id": r[0], "telegram_id": r[1], "txn_hash": r[2],
             "created_at": r[3],
             "days": int(r[4]) if len(r) > 4 and r[4] is not None else 30}
            for r in cur.fetchall()]


def set_premium_request_status(request_id, status):
    _execute("UPDATE premium_requests SET status=? WHERE id=?",
             (status, request_id))


def get_usage(telegram_id, feature, date_str):
    """How many times a free user ran `feature` on `date_str`
    (0 when never). Never raises — a DB error reads as 0, so a
    broken usage table never blocks a user."""
    try:
        row = _execute(
            "SELECT count FROM usage WHERE telegram_id=? AND feature=? "
            "AND date=?", (telegram_id, feature, date_str)).fetchone()
    except Exception:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def increment_usage(telegram_id, feature, date_str, amount=1):
    """Add `amount` to a free user's daily count for `feature` and
    return the new count. An update that touches no row inserts one
    instead; a race (two inserts at once) or any other failure never
    raises — the current count is returned instead."""
    try:
        cur = _execute(
            "UPDATE usage SET count = count + ? WHERE telegram_id=? "
            "AND feature=? AND date=?",
            (amount, telegram_id, feature, date_str))
        if cur.rowcount == 0:
            try:
                _execute(
                    "INSERT INTO usage (telegram_id, feature, date, count) "
                    "VALUES (?, ?, ?, ?)",
                    (telegram_id, feature, date_str, amount))
            except Exception:
                # Lost a race with a concurrent insert for the same
                # (user, feature, date) — the row exists now.
                pass
    except Exception:
        pass
    return get_usage(telegram_id, feature, date_str)


def set_auto_signals(telegram_id, on):
    get_or_create_user(telegram_id)
    _execute("UPDATE users SET auto_signals=? WHERE telegram_id=?",
             (1 if on else 0, telegram_id))


def is_auto_subscriber(telegram_id):
    return bool(get_or_create_user(telegram_id).get("auto_signals"))


def set_auto_asset(telegram_id, asset):
    """Set the market a user's Live Auto Signal watches (None clears
    it). The column is added by _ensure_auto_asset_column; a users
    table that predates it gets the column on connect. Single-asset
    wrapper around set_auto_assets (the owner, 2026-11-28, picked
    multi-asset select)."""
    set_auto_assets(telegram_id, [asset] if asset else [])


def get_auto_asset(telegram_id):
    """The first market a user's Live Auto Signal watches, or None."""
    assets = get_auto_assets(telegram_id)
    return assets[0] if assets else None


def set_auto_assets(telegram_id, assets):
    """Set the markets a user's Live Auto Signal watches, as a
    comma-separated list in the auto_asset column (an empty list
    clears it)."""
    get_or_create_user(telegram_id)
    value = ",".join(a for a in (assets or []) if a) or None
    _execute("UPDATE users SET auto_asset=? WHERE telegram_id=?",
             (value, telegram_id))


def get_auto_assets(telegram_id):
    """The markets a user's Live Auto Signal watches, as a list."""
    try:
        cur = _execute("SELECT auto_asset FROM users WHERE telegram_id=?",
                       (telegram_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            return []
        return [a for a in row[0].split(",") if a]
    except Exception:
        return []


def set_auto_mtg1(telegram_id, mtg1):
    """Set whether a user's Live Auto Signal trades MTG1 (True) or
    Non-MTG / M1 only (False). The column is added by
    _ensure_auto_mtg1_column."""
    get_or_create_user(telegram_id)
    _execute("UPDATE users SET auto_mtg1=? WHERE telegram_id=?",
             (1 if mtg1 else 0, telegram_id))


def get_auto_mtg1(telegram_id):
    """True when a user's Live Auto Signal trades MTG1, False for
    Non-MTG (the default)."""
    try:
        cur = _execute("SELECT auto_mtg1 FROM users WHERE telegram_id=?",
                       (telegram_id,))
        row = cur.fetchone()
        return bool(row[0]) if row else False
    except Exception:
        return False


def set_menu_started_at(telegram_id, ts):
    """Record when a user last started the menu fresh with /start
    (Unix seconds). Buttons on messages sent before that moment are
    stale and no longer work. The column is added by
    _ensure_menu_started_at_column."""
    get_or_create_user(telegram_id)
    _execute("UPDATE users SET menu_started_at=? WHERE telegram_id=?",
             (str(float(ts)), telegram_id))


def get_menu_started_at(telegram_id):
    """Unix seconds of a user's last fresh /start, or None when the
    user never pressed /start (or the column is missing)."""
    try:
        cur = _execute("SELECT menu_started_at FROM users WHERE telegram_id=?",
                       (telegram_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            return None
        return float(row[0])
    except Exception:
        return None


def get_auto_subscribers():
    """Live Auto Signal subscribers as (telegram_id, auto_asset)
    pairs — only users who are ON and have picked at least one
    market. A user with several markets yields one pair per market,
    so the loop watches each of their markets."""
    try:
        cur = _execute(
            "SELECT telegram_id, auto_asset FROM users "
            "WHERE auto_signals=1 AND auto_asset IS NOT NULL "
            "AND auto_asset != ''")
        pairs = []
        for telegram_id, raw in cur.fetchall():
            for asset in (raw or "").split(","):
                if asset:
                    pairs.append((telegram_id, asset))
        return pairs
    except Exception:
        return []


# ---------------------------------------------------------------------------
# MATRIX referral (the owner, 2026-11-27, Option B pricing)
# ---------------------------------------------------------------------------
# Anti-abuse rules, enforced here in the database, not in the UI:
# - one invited user counts once, ever (invited_id UNIQUE) — a
#   leave-and-rejoin, or being invited by a second referrer, pays 0.
# - a user inviting themself pays 0.
# - coins can only be spent down to 0, never below, and never
#   transferred to another user.
# A person with many separate Telegram accounts can still look like
# many new members — Telegram gives the bot only a user id, no
# device/IP — so this is a real limit, not a bug in these rules.

def get_coin_balance(telegram_id):
    """MATRIX coin balance (0 for a user who never earned any)."""
    get_or_create_user(telegram_id)
    try:
        row = _execute("SELECT matrix_coins FROM users WHERE telegram_id=?",
                       (telegram_id,)).fetchone()
    except Exception:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def add_coins(telegram_id, amount):
    """Add coins; returns the new balance."""
    get_or_create_user(telegram_id)
    _execute("UPDATE users SET matrix_coins = COALESCE(matrix_coins, 0) + ? "
             "WHERE telegram_id=?", (amount, telegram_id))
    return get_coin_balance(telegram_id)


def spend_coins(telegram_id, amount):
    """Deduct `amount` coins if the balance covers it.
    Returns (True, new_balance) or (False, current_balance)."""
    balance = get_coin_balance(telegram_id)
    if balance < amount:
        return False, balance
    _execute("UPDATE users SET matrix_coins = COALESCE(matrix_coins, 0) - ? "
             "WHERE telegram_id=?", (amount, telegram_id))
    return True, get_coin_balance(telegram_id)


def extend_premium(telegram_id, days):
    """Premium for `days`, counted from the current expiry when the
    user is already premium (so paid days are never lost), else now.
    Returns the new premium_until ISO string."""
    user = get_or_create_user(telegram_id)
    start = datetime.now()
    if user.get("premium_until"):
        try:
            current = datetime.fromisoformat(user["premium_until"])
            if current > start:
                start = current
        except ValueError:
            pass
    until = (start + timedelta(days=days)).isoformat(timespec="seconds")
    set_premium_until(telegram_id, until)
    return until


def get_referral_link(telegram_id):
    row = _execute("SELECT invite_link FROM referral_links WHERE referrer_id=?",
                   (telegram_id,)).fetchone()
    return row[0] if row else None


def save_referral_link(telegram_id, invite_link):
    now = datetime.now().isoformat(timespec="seconds")
    if _backend() == "postgres":
        _execute("INSERT INTO referral_links (referrer_id, invite_link, created_at) "
                 "VALUES (?, ?, ?) ON CONFLICT (referrer_id) DO UPDATE "
                 "SET invite_link=EXCLUDED.invite_link, created_at=EXCLUDED.created_at",
                 (telegram_id, invite_link, now))
    else:
        _execute("INSERT OR REPLACE INTO referral_links "
                 "(referrer_id, invite_link, created_at) VALUES (?, ?, ?)",
                 (telegram_id, invite_link, now))


def get_referrer_for_link(invite_link):
    row = _execute("SELECT referrer_id FROM referral_links WHERE invite_link=?",
                   (invite_link,)).fetchone()
    return int(row[0]) if row else None


def record_referral(referrer_id, invited_id, invite_link, coins=5):
    """Record a channel join via a referral link.

    Returns (status, coins_awarded, new_balance):
    - ("awarded", 5, balance): first-ever join of this user — coins paid.
    - ("duplicate", 0, balance): this user already counted once.
    - ("self", 0, balance): the referrer joined via their own link.
    """
    if referrer_id == invited_id:
        return "self", 0, get_coin_balance(referrer_id)
    existing = _execute("SELECT referrer_id FROM referrals WHERE invited_id=?",
                        (invited_id,)).fetchone()
    if existing:
        return "duplicate", 0, get_coin_balance(referrer_id)
    now = datetime.now().isoformat(timespec="seconds")
    try:
        _execute("INSERT INTO referrals (referrer_id, invited_id, invite_link, "
                 "joined_at, coins_awarded) VALUES (?, ?, ?, ?, ?)",
                 (referrer_id, invited_id, invite_link, now, coins))
    except Exception:
        # Lost a race with a second join event for the same user:
        # the UNIQUE(invited_id) row already exists — pay nothing.
        return "duplicate", 0, get_coin_balance(referrer_id)
    return "awarded", coins, add_coins(referrer_id, coins)


def get_referral_stats(telegram_id):
    """{balance, joins, coins_earned} for one referrer."""
    joins = _execute("SELECT COUNT(*), COALESCE(SUM(coins_awarded), 0) "
                     "FROM referrals WHERE referrer_id=?",
                     (telegram_id,)).fetchone()
    return {"balance": get_coin_balance(telegram_id),
            "joins": int(joins[0]) if joins else 0,
            "coins_earned": int(joins[1]) if joins else 0}
