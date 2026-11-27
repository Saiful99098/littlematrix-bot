"""Telegram handlers — Blue Fish Prime (Render / db mode for members).

All flows are state machines stored in context.user_data.
Data source:
- DATA_MODE=db (Render): signals + CHECKER + every analysis tool read
  ONLY from the candle database (Postgres on Render, SQLite locally).
  A background collector fills the DB (5-day backfill on startup,
  then every 2 hours). No Quotex login happens for signals, CHECKER
  or any menu button — that is the anti-429 design, and what lets
  many members use the bot at once.
- DATA_MODE=live + Quotex login in .env: fetch yesterday's 1-day
  candles live from Quotex with get_historical_candles() for ANY
  asset the user picks (3-at-a-time for SELECT ALL), cached to
  data/candles_<asset>_<date>.json so a second request does not
  log in again. See bot/quotex_client.py.
- Channel gate: when REQUIRED_CHANNEL is set, /start and every menu
  tap first check the user is a member of that channel (the bot must
  be an admin there); non-members get a Join button instead.
"""
from __future__ import annotations

import asyncio
import html
import inspect
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("littlematrix.handlers")

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from . import candles_db, collector, config, database, quotex_client
from .checker import (build_lookup, check_signals, dot_date,
                      format_check_results, parse_checker_date,
                      parse_signal_line)
from .filters import filter_personal, format_filter_result
from .news import events_for_dhaka_dates
from .premium_emoji import BUTTON_ICONS, pe
from .signal_engine import (
    available_signal_dates,
    candle_direction,
    cross_asset_filter,
    diagnose_window,
    display_asset,
    ema,
    format_signals_list,
    generate_futures_signals,
    generate_futures_signals_from_db,
    generate_prev_minute_signals,
    generate_prev_minute_signals_from_db,
    load_days_from_db,
    load_json_candles,
    normalize_candle,
    rsi,
    trend_direction,
)
from .text_tools import convert_timezone, format_signal_list, formatter, swap_call_put, text_font

# Premium animated warning (CARDERS pack) — a <tg-emoji> tag, so every
# send site that includes DISCLAIMER must pass parse_mode="HTML"
# (all 7 sites do: start, menu_market, menu_about, _run_futures x2
# via _html_mode, _run_ai_signal, _run_backtest).
DISCLAIMER = f"{pe('warning')} No signal guarantees profit. Signals are based on past patterns."
CONTACT_LINE = f"{pe('message')} Contact for more: <a href=\"https://t.me/LittleMatrix\">@LittleMatrix</a>"

# Feature detection for Bot API 9.4+ button fields (python-telegram-bot
# 22.7+). If a slightly older 22.x is installed, icon/style kwargs are
# simply not passed and the buttons fall back to plain text — the bot
# must never crash on a button it cannot render.
_BTN_PARAMS = inspect.signature(InlineKeyboardButton.__init__).parameters
_BTN_SUPPORTS_ICON = "icon_custom_emoji_id" in _BTN_PARAMS
_BTN_SUPPORTS_STYLE = "style" in _BTN_PARAMS


def _btn(text, callback_data, icon=None, style=None):
    """Build an InlineKeyboardButton, with a premium icon when available.

    icon: key into BUTTON_ICONS. The icon renders BEFORE the text.
    Button text is plain (no Unicode emoji): the owner wants premium-only
    buttons, so the premium icon is the button's only emoji.
    style: 'primary' only for the Premium button, 'success' only for
    SELECT ALL; every other button uses no style (clean look)."""
    kwargs = {}
    if icon and _BTN_SUPPORTS_ICON:
        kwargs["icon_custom_emoji_id"] = BUTTON_ICONS[icon]
    if style and _BTN_SUPPORTS_STYLE:
        kwargs["style"] = style
    return InlineKeyboardButton(text, callback_data=callback_data, **kwargs)


def _url_btn(text, url, icon=None):
    """Build a URL InlineKeyboardButton, with a premium icon when
    available. Same icon rule as _btn: the icon is the button's only
    emoji, the text stays plain."""
    kwargs = {}
    if icon and _BTN_SUPPORTS_ICON:
        kwargs["icon_custom_emoji_id"] = BUTTON_ICONS[icon]
    return InlineKeyboardButton(text, url=url, **kwargs)


# Main menu: all 21 buttons carry a premium icon; their text is
# plain (premium-only, per the owner). If an icon does not render
# (old client / old library), the button still shows its text.
MAIN_MENU = [
    [_btn("Check Signals", "menu_checker", icon="checker"),
     _btn("AI Signal", "menu_ai", icon="ai")],
    [_btn("Future Signals", "menu_futures", icon="futures"),
     _btn("Market Live", "menu_market", icon="market")],
    [_btn("OTC Board", "menu_otc", icon="otc"),
     _btn("Payouts", "menu_payouts", icon="payouts")],
    [_btn("Candle Scan", "menu_counter", icon="counter"),
     _btn("Backtest Lab", "menu_backtest", icon="backtest")],
    [_btn("Trend Scan", "menu_trend", icon="trend"),
     _btn("News Desk", "menu_news", icon="news")],
    [_btn("Swap C/P", "menu_swap", icon="swap"),
     _btn("Signal Formatter", "menu_formatter", icon="formatter")],
    [_btn("Timezone", "menu_timezone", icon="timezone"),
     _btn("Font Lab", "menu_font", icon="font")],
    [_btn("Signal Filter", "menu_filter", icon="filter"),
     _btn("My Status", "menu_status", icon="status")],
    [_btn("About", "menu_about", icon="about"),
     _btn("Auto Signals", "menu_autolive", icon="autolive")],
    [_btn("Premium — 5 USDT", "menu_premium", icon="premium", style="primary")],
    [_url_btn("Contact Owner", "https://t.me/LittleMatrix", icon="contact")],
]


def main_keyboard(admin=False):
    """The main menu. The Admin Dashboard and Bot Status rows are
    appended ONLY for the owner (the owner, 2026-11-27) — no other
    user, premium or free, ever sees those options. `admin` is the
    caller's check (user.id in config.ADMIN_USER_IDS)."""
    rows = [list(r) for r in MAIN_MENU]
    if admin:
        rows.append([_btn("Admin Dashboard", "menu_admin", icon="admin")])
        rows.append([_btn("Bot Status", "menu_botstatus", icon="bot_status")])
    return InlineKeyboardMarkup(rows)


def _is_admin(user_id):
    return user_id in config.ADMIN_USER_IDS


# ---------------------------------------------------------------------------
# Admin Dashboard (the owner, 2026-11-27)
# ---------------------------------------------------------------------------
# user_id -> {"task": asyncio.Task} for the live dashboard refresh.
# The refresh loop edits the dashboard message every 10 seconds with
# fresh counts; it stops after ~30 min, when the dashboard is closed
# (Back), or when the message can no longer be edited.
_ADMIN_DASH = {}
_ADMIN_DASH_MAX_RUNS = 180  # 10s x 180 = ~30 min
_ADMIN_DASH_INTERVAL_SECONDS = 10  # patched to ~0.05 in tests


def _admin_dashboard_text(stats):
    now = datetime.now(candles_db.DHAKA).strftime("%H:%M:%S")
    return (f"{pe('diamond')}{pe('crown')} BLUE FISH PRIME • ADMIN DASHBOARD\n{DIVIDER}\n"
            "User counts — auto-refreshes every 10 seconds.\n"
            f"Updated: {now} (Dhaka)")


def _admin_dashboard_keyboard(stats):
    """The counts themselves are display-only buttons (like the
    payouts grid): tapping one does nothing (admin_stat_noop)."""
    return InlineKeyboardMarkup([
        [_btn(f"Total Users: {stats['total']}", "admin_stat_noop", icon="info"),
         _btn(f"Online Now: {stats['online']}", "admin_stat_noop", icon="green")],
        [_btn(f"Premium: {stats['premium']}", "admin_stat_noop", icon="premium"),
         _btn(f"Free: {stats['free']}", "admin_stat_noop", icon="status")],
        [_btn("Back to Menu", "menu_main", icon="back")]])


async def _admin_dash_loop(bot, chat_id, message_id, user_id):
    """Re-read the user counts every 10s and edit the dashboard
    message in place. The Updated clock changes on every edit, so
    Telegram never answers 'Message is not modified'."""
    try:
        for _ in range(_ADMIN_DASH_MAX_RUNS):
            await asyncio.sleep(_ADMIN_DASH_INTERVAL_SECONDS)
            entry = _ADMIN_DASH.get(user_id)
            if not entry or entry.get("task") is not asyncio.current_task():
                return
            stats = database.get_user_stats()
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id,
                    text=_admin_dashboard_text(stats),
                    reply_markup=_admin_dashboard_keyboard(stats),
                    parse_mode="HTML")
            except Exception as exc:
                logger.info("admin dashboard refresh stopped: %s", exc)
                return
    except asyncio.CancelledError:
        pass
    finally:
        entry = _ADMIN_DASH.get(user_id)
        if entry and entry.get("task") is asyncio.current_task():
            _ADMIN_DASH.pop(user_id, None)


def _cancel_admin_dash(user_id):
    entry = _ADMIN_DASH.pop(user_id, None)
    if entry and entry.get("task"):
        entry["task"].cancel()


async def _run_admin_dashboard(query, context, user_id):
    """Show the dashboard and start its 10-second refresh loop."""
    _cancel_admin_dash(user_id)
    stats = database.get_user_stats()
    await query.edit_message_text(
        _admin_dashboard_text(stats),
        reply_markup=_admin_dashboard_keyboard(stats), parse_mode="HTML")
    task = asyncio.create_task(_admin_dash_loop(
        context.bot, query.message.chat_id, query.message.message_id,
        user_id))
    _ADMIN_DASH[user_id] = {"task": task}


def back_keyboard():
    """Single Back button only — result messages must NOT carry the
    full main menu below a long list (the user had to scroll past
    everything to find the menu). The main menu stays as its own
    message above the results in chat history."""
    return InlineKeyboardMarkup(
        [[_btn("Back to Menu", "menu_main", icon="back")]])


def _generated_line():
    """When this result was built (Dhaka time). A Telegram message is
    frozen once sent — a 12:52 AI Signal is still 12:52 at 12:57 —
    so every result carries its build time and is never mistaken
    for live data."""
    return f"Generated: {datetime.now(candles_db.DHAKA).strftime('%H:%M:%S')} (Dhaka)"


def _candle_age_text(last):
    """How old the last candle is ('2 min ago'), from its Dhaka
    datetime string. '' when it cannot be parsed."""
    try:
        dt = datetime.strptime(last["datetime"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=candles_db.DHAKA)
        mins = max(0, int((datetime.now(candles_db.DHAKA) - dt).total_seconds() // 60))
    except Exception:
        return ""
    return "just now" if mins == 0 else f"{mins} min ago"


def _refresh_keyboard():
    """Refresh + Back, for result messages. Refresh re-runs exactly
    the result shown in that message (see _store_result_spec)."""
    return InlineKeyboardMarkup([
        [_btn("Refresh", "refresh", icon="refresh")],
        [_btn("Back to Menu", "menu_main", icon="back")]])


def _store_result_spec(context, query_or_mid, spec):
    """Remember what a result message shows, keyed by its message id,
    so its Refresh button re-runs exactly that result — not whatever
    the user picked afterwards. `query_or_mid` is a CallbackQuery (its
    message id is used) or a raw message id (the custom-time text
    flow's working message). Keeps the last 10 results."""
    if isinstance(query_or_mid, int):
        mid = query_or_mid
    else:
        try:
            mid = query_or_mid.message.message_id if query_or_mid.message else None
        except Exception:
            mid = None
    if mid is None:
        return
    store = context.user_data.setdefault("result_specs", {})
    store[mid] = spec
    while len(store) > 10:
        store.pop(next(iter(store)))


def _fs_asset_label(asset):
    """Label for the futures progress lines: 'SELECT ALL', one asset,
    or 'N assets' for a multi-asset selection."""
    if asset == "ALL":
        return "SELECT ALL"
    if isinstance(asset, list):
        if len(asset) == 1:
            return display_asset(asset[0])
        return f"{len(asset)} assets"
    return display_asset(asset)


DIVIDER = "━━━━━━━━━━━━━━━"


def _html_mode(text):
    """parse_mode for a message: 'HTML' if it carries a premium-emoji
    tag, else None (plain). Used for results built dynamically
    (futures), so error texts without any tag are sent plain and can
    never break HTML parsing."""
    return "HTML" if "<tg-emoji" in (text or "") else None


ASSETS_PER_PAGE = 12


def _is_otc(asset):
    return asset.lower().endswith("_otc")


def _filter_assets_by_mode(assets, mode):
    """OTC FS / BLACKOUT FS / WHITEOUT FS -> only _otc assets.
    REAL MARKET FS -> only non-_otc assets."""
    if mode == "REAL":
        return [a for a in assets if not _is_otc(a)]
    return [a for a in assets if _is_otc(a)]


def _asset_picker_keyboard(assets, page, mode, selected=None):
    """Paginated MULTI-SELECT asset picker: tap an asset to toggle it
    (a premium check appears on selected buttons), SELECT ALL toggles
    everything, Next continues with the selection. The picker stays
    on the same page — it is re-rendered in place on every tap."""
    selected = selected or set()
    kb = [[_btn("SELECT ALL", "fs_asset_ALL", icon="select_all", style="success")]]
    total_pages = max(1, (len(assets) + ASSETS_PER_PAGE - 1) // ASSETS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    chunk = assets[page * ASSETS_PER_PAGE:(page + 1) * ASSETS_PER_PAGE]
    for i in range(0, len(chunk), 2):
        row = [_btn(display_asset(a), f"fs_asset_{a}",
                    icon="check" if a in selected else "asset")
               for a in chunk[i:i + 2]]
        kb.append(row)
    nav = []
    if page > 0:
        nav.append(_btn("Prev Page", f"fs_assets_page_{page - 1}", icon="back"))
    if page < total_pages - 1:
        nav.append(_btn("Next Page", f"fs_assets_page_{page + 1}", icon="next"))
    if nav:
        kb.append(nav)
    kb.append([_btn("Next", "fs_next", icon="next")])
    kb.append([_btn("Back", "menu_main", icon="back")])
    return InlineKeyboardMarkup(kb), page, total_pages


def _asset_picker_text(assets, page, total_pages, mode, live, selected=None):
    if config.DATA_MODE == "db":
        src = (f"{pe('diamond')} Database assets ({len(assets)} for {mode}) — "
               "from the candle database, no Quotex login.")
    elif live:
        src = f"{pe('diamond')} Live open assets ({len(assets)} for {mode})"
    else:
        src = (f"{pe('diamond')} Local test — only these 6 assets here.\n"
               "Live mode shows every open asset from Quotex.")
    return (f"{pe('diamond')} BLUE FISH PRIME • CHOOSE ASSET\n{DIVIDER}\n"
            f"{src}\nPage {page + 1}/{total_pages}\n"
            f"Selected: {len(selected or [])} — tap an asset to toggle it, then tap Next.")


# Analysis tools (AI Signal, Candle Scan, Trend Scan, Market Live) ask
# the user which market to look at first, instead of silently using
# the default asset. Same picker shape as the futures picker, but
# single-asset (no SELECT ALL) and keyed by tool.
_ANALYSIS_TOOLS = {"ai": "AI Signal", "counter": "Candle Scan",
                   "trend": "Trend Scan", "market": "Market Live"}


def _analysis_assets():
    """DB assets for the analysis picker: OTC first (sorted), then
    real-market assets (sorted). Empty when the DB has no candles yet."""
    try:
        assets = _db().get_assets()
    except Exception:
        return []
    otc = sorted(a for a in assets if _is_otc(a))
    real = sorted(a for a in assets if not _is_otc(a))
    return otc + real


def _analysis_picker_keyboard(assets, page, tool, selected=None):
    """Paginated MULTI-SELECT asset picker for one analysis tool:
    tap a market to toggle it (a premium check appears on selected
    buttons), SELECT ALL toggles everything, Next runs the tool for
    the selection. The picker stays on the same page — it is
    re-rendered in place on every tap."""
    selected = selected or set()
    kb = [[_btn("SELECT ALL", f"an_all_{tool}", icon="select_all", style="success")]]
    total_pages = max(1, (len(assets) + ASSETS_PER_PAGE - 1) // ASSETS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    chunk = assets[page * ASSETS_PER_PAGE:(page + 1) * ASSETS_PER_PAGE]
    for i in range(0, len(chunk), 2):
        kb.append([_btn(display_asset(a), f"an_asset_{tool}_{a}",
                        icon="check" if a in selected else "asset")
                   for a in chunk[i:i + 2]])
    nav = []
    if page > 0:
        nav.append(_btn("Prev Page", f"an_page_{tool}_{page - 1}", icon="back"))
    if page < total_pages - 1:
        nav.append(_btn("Next Page", f"an_page_{tool}_{page + 1}", icon="next"))
    if nav:
        kb.append(nav)
    kb.append([_btn("Next", f"an_next_{tool}", icon="next")])
    kb.append([_btn("Back", "menu_main", icon="back")])
    return InlineKeyboardMarkup(kb), page, total_pages


def _analysis_picker_text(tool, page, total_pages, selected=None):
    return (f"{pe('diamond')} {_ANALYSIS_TOOLS[tool]} — pick markets {pe('chart')}\n"
            f"{DIVIDER}\nPage {page + 1}/{total_pages}\n"
            f"Selected: {len(selected or [])} — tap a market to toggle it, then tap Next.")


async def _rerender_an_picker(query, context, tool, warn=False):
    """Re-render the analysis picker in place (same page, updated
    check marks). Used on every toggle tap."""
    assets = _analysis_assets()
    selected = context.user_data.get("an_selected") or set()
    page = context.user_data.get("an_page", 0)
    kb, page, total_pages = _analysis_picker_keyboard(assets, page, tool, selected)
    text = _analysis_picker_text(tool, page, total_pages, selected)
    if warn:
        text += f"\n\n{pe('warning')} Select at least 1 market first."
    await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")


async def _show_analysis_picker(query, context, tool):
    """Show the asset picker for one analysis tool, or the empty-DB
    message when no candles have been collected yet. Starts a fresh
    (empty) selection."""
    context.user_data["an_tool"] = tool
    context.user_data["an_selected"] = set()
    context.user_data["an_page"] = 0
    assets = _analysis_assets()
    if not assets:
        await query.edit_message_text(
            f"{pe('cross')} No candles in the database yet.\n"
            "The backfill collects the last 5 days first.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    kb, page, total_pages = _analysis_picker_keyboard(assets, 0, tool)
    await query.edit_message_text(
        _analysis_picker_text(tool, page, total_pages),
        reply_markup=kb, parse_mode="HTML")


def parse_custom_time(text):
    """Parse '09:00 10:59' / '09:00-10:59' / '9:00-10:59' / '09:00,10:59'.

    Returns ("HH:MM", "HH:MM") normalised, or None if invalid
    (bad hour/minute, or end <= start)."""
    found = re.findall(r"(\d{1,2}):(\d{1,2})", text or "")
    if len(found) != 2:
        return None
    times = []
    for h, m in found:
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        times.append(f"{h:02d}:{m:02d}")
    if times[1] <= times[0]:
        return None
    return times[0], times[1]


def _yesterday_date_str():
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Dhaka")
    except Exception:
        tz = timezone(timedelta(hours=6))
    now = datetime.now(tz)
    return (now - timedelta(days=1)).strftime("%Y-%m-%d")


def _today_date_str():
    """Today's date in Asia/Dhaka (UTC+6)."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Dhaka")
    except Exception:
        tz = timezone(timedelta(hours=6))
    return datetime.now(tz).strftime("%Y-%m-%d")


def _db():
    """The candle DB (Postgres on Render, SQLite locally)."""
    return candles_db.get_candle_db()


def _effective_date():
    """Live mode: yesterday (real date). db mode: today (DB covers it)."""
    if config.DATA_MODE == "db":
        return _today_date_str()
    return _yesterday_date_str()


async def get_candles_for(asset, date_str):
    """Fetch candles for ANY asset (async — must be awaited).

    Order: candle DB first (DATA_MODE=db — no Quotex login) ->
    live Quotex (DATA_MODE=live + login, yesterday only) ->
    data/candles_<asset>_<date>.json if collected before.
    Returns (candles list, source_str). Candles are raw dicts with
    datetime + O/H/L/C (or open/high/low/close + time).
    Login/fetch failures return (None, 'error: ...') with the reason,
    instead of silent 'no-data', so the user can see what failed.
    """
    asset = asset.replace("-", "_")
    # 0) Candle database (db mode): signals + CHECKER read from here,
    #    never from Quotex. Covers today 00:00-now too.
    if config.DATA_MODE == "db":
        try:
            db_candles = _db().get_candles(asset, date_str)
        except Exception as exc:
            return None, f"error: database read failed: {exc}"
        if db_candles:
            return db_candles, "db"
    # 1) Live fetch (also checks memory + data/ cache inside).
    if config.DATA_MODE == "live":
        try:
            live = await quotex_client.fetch_yesterday_candles(asset, date_str)
        except quotex_client.QuotexRateLimited as exc:
            return None, str(exc)
        except Exception as exc:
            return None, f"error: {exc}"
        if live:
            return live, "live"
    # 2) Collected JSON file in data/ (e.g. from collect_yesterday.py).
    try:
        from pathlib import Path
        path = Path(config.BASE_DIR) / "data" / f"candles_{asset}_{date_str}.json"
        if path.exists():
            _, candles = load_json_candles(path)
            # load_json_candles returns normalized candles; convert back
            # to raw-like dicts the engine also accepts (it normalizes).
            raw = [{"datetime": c["datetime"], "O": c["open"], "H": c["high"],
                    "L": c["low"], "C": c["close"]} for c in candles]
            if raw:
                return raw, "file"
    except Exception:
        pass
    return None, "no-data"


# ---------------------------------------------------------------------------
# Channel membership gate
# ---------------------------------------------------------------------------
_member_cache = {}  # user_id -> (unix_ts, is_member); 10-min TTL


def _channel_join_url():
    invite = (config.CHANNEL_INVITE_LINK or "").strip()
    if invite:
        return invite
    ch = (config.REQUIRED_CHANNEL or "").strip()
    if not ch:
        return None
    if ch.startswith("https://"):
        return ch
    if ch.startswith("@"):
        return "https://t.me/" + ch[1:]
    return "https://t.me/" + ch


async def _is_channel_member(bot, user_id, use_cache=True):
    """True when the user may use the bot: no gate configured, or the
    user is a member/admin/creator of REQUIRED_CHANNEL. A check that
    itself fails (bot not admin in the channel, wrong id) fails OPEN
    with a loud log — a config mistake must never lock out members."""
    ch = (config.REQUIRED_CHANNEL or "").strip()
    if not ch:
        return True
    import time as _time
    if use_cache:
        hit = _member_cache.get(user_id)
        if hit and _time.time() - hit[0] < 600:
            return hit[1]
    try:
        member = await bot.get_chat_member(chat_id=ch, user_id=user_id)
        ok = member.status in ("member", "administrator", "creator", "restricted")
    except Exception as exc:
        logger.warning("channel gate check failed (%s): %s — letting user through",
                       ch, exc)
        return True
    _member_cache[user_id] = (_time.time(), ok)
    return ok


def _join_keyboard():
    rows = []
    url = _channel_join_url()
    if url:
        rows.append([_url_btn("Join Now", url, icon="join")])
    rows.append([_btn("I Joined — Check Again", "check_member", icon="joined")])
    rows.append([_btn("Back", "menu_main", icon="back")])
    return InlineKeyboardMarkup(rows)


def _join_gate_text():
    return (f"{pe('diamond')} Join the channel to use this bot {pe('fire')}\n{DIVIDER}\n\n"
            "BLUE FISH PRIME is free for members of the Blue Fish trading community.\n"
            "Join the channel first, then tap \"I Joined\" — the bot will welcome you "
            "and unlock the tools.")


# ---------------------------------------------------------------------------
# DB-backed analysis tools (AI Signal / Candle Scan / Trend Scan / Backtest)
# ---------------------------------------------------------------------------
def _default_analysis_asset():
    """Asset for the one-tap analysis tools: BTCUSD-OTC first (most
    members watch it), else the OTC asset with the most candles today,
    else any asset with the most candles today. None when the DB is empty."""
    db = _db()
    try:
        today = _today_date_str()
        counts = {}
        for a in db.get_assets():
            n = db.count_candles(a, today)
            if n:
                counts[a] = n
        if not counts:
            return None
        for pref in ("BTCUSD_otc", "ETHUSD_otc", "EURUSD_otc"):
            if counts.get(pref, 0) >= 60:
                return pref
        otc = {a: n for a, n in counts.items() if a.endswith("_otc")}
        pool = otc or counts
        return max(pool, key=pool.get)
    except Exception:
        return None


def _db_recent_candles(asset, n):
    """Last `n` normalized candles of today for one asset, or []."""
    try:
        candles = _db().get_candles(asset, _today_date_str())
    except Exception:
        return []
    return [normalize_candle(c) for c in candles[-n:]]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    _cancel_admin_dash(user.id)
    database.get_or_create_user(user.id, user.username or "")
    if not await _is_channel_member(context.bot, user.id):
        text = _join_gate_text()
        if update.message:
            await update.message.reply_text(text, reply_markup=_join_keyboard(),
                                            parse_mode="HTML")
        elif update.callback_query:
            await update.callback_query.edit_message_text(
                text, reply_markup=_join_keyboard(), parse_mode="HTML")
        return
    text = (f"{pe('diamond')}{pe('lightning')} BLUE FISH PRIME — Trading Terminal {pe('sparkle')}\n"
            f"Created by LITTLE MATRIX {pe('crown')}\n"
            f"Pick a tool below to start. {pe('fire')}\n"
            f"{CONTACT_LINE}\n\n" + DISCLAIMER)
    if update.message:
        await update.message.reply_text(text, reply_markup=main_keyboard(admin=_is_admin(user.id)),
                                        parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=main_keyboard(admin=_is_admin(user.id)), parse_mode="HTML")


async def _rerender_fs_picker(query, context, warn=False):
    """Re-render the futures asset picker in place (same page, updated
    check marks). Used on every toggle tap."""
    assets = context.user_data.get("fs_assets") or config.SAMPLE_ASSETS
    mode = context.user_data.get("fs_mode", "OTC FS")
    live = config.DATA_MODE == "live" and config.has_quotex_login()
    page = context.user_data.get("fs_assets_page", 0)
    selected = context.user_data.get("fs_selected") or set()
    kb, page, total_pages = _asset_picker_keyboard(assets, page, mode, selected)
    text = _asset_picker_text(assets, page, total_pages, mode, live, selected)
    if warn:
        text += f"\n\n{pe('warning')} Select at least 1 asset first."
    await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")


async def _show_futures_days_question(query, context):
    """Days question — shown after the asset selection (Next)."""
    context.user_data["flow"] = "futures_days"
    if config.DATA_MODE == "db":
        kb = [[_btn("1 Day", "fs_days_1", icon="days"),
               _btn("2 Days", "fs_days_2", icon="days"),
               _btn("3 Days", "fs_days_3", icon="days"),
               _btn("5 Days", "fs_days_5", icon="days")],
              [_btn("Back", "menu_main", icon="back")]]
        await query.edit_message_text(
            f"{pe('diamond')} How many days of history should shape the signals? {pe('chart')}\n"
            "The database holds the last 5 full days (00:00-23:59 each day). "
            "Signals are built from those days at the same HH:MM.",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        return
    kb = [[_btn("1 Day", "fs_days_1", icon="days"),
           _btn("2 Days", "fs_days_2", icon="days"),
           _btn("3 Days", "fs_days_3", icon="days"),
           _btn("10 Days", "fs_days_10", icon="days")],
          [_btn("Back", "menu_main", icon="back")]]
    await query.edit_message_text(
        f"{pe('diamond')} How many days of history should shape the signals? {pe('chart')}\n"
        "Local test uses 1 day of sample data. Picking 2/3/10 days "
        "shows the 1-day result for now — multi-day voting needs "
        "the 20-day database (Render step).",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


def _parse_fs_pct(text):
    """User-typed win-rate % (70-100) for the FUTURES SIGNAL filter.
    Returns the int, or None when it is not a whole number in range
    (a trailing % sign is allowed: '80%')."""
    s = (text or "").strip().rstrip("%").strip()
    try:
        n = int(s)
    except ValueError:
        return None
    if 70 <= n <= 100:
        return n
    return None


def _futures_time_keyboard():
    return InlineKeyboardMarkup([
        [_btn("09:00-10:59", "fs_time_09:00-10:59", icon="time"),
         _btn("00:00-01:59", "fs_time_00:00-01:59", icon="time")],
        [_btn("Custom Time", "fs_time_custom", icon="custom_time")],
        [_btn("Back", "menu_main", icon="back")]])


def _futures_time_text():
    return f"{pe('pin')} Choose a time window (UTC+6) — or set your own:"


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        # "Query is too old" / timeout (slow instance under backfill
        # load): the tap still works — the menu is edited below.
        pass
    data = query.data
    user = update.effective_user
    # The dashboard refresh runs ONLY while the owner is looking at
    # the dashboard (the owner, 2026-11-27): any other tap means he
    # navigated away, so stop the loop here — otherwise it keeps
    # editing that message back into the dashboard every 10 seconds.
    # The dashboard's own buttons are exempt: a stat tap
    # (admin_stat_noop) means he is still on the dashboard, and
    # menu_admin re-opens it (which restarts the loop itself). This
    # runs before the channel gate, so even a gate-blocked tap
    # stops the loop.
    if data not in ("menu_admin", "admin_stat_noop"):
        _cancel_admin_dash(user.id)
    database.get_or_create_user(user.id, user.username or "")

    # Channel gate: members only (except the re-check itself and
    # admin premium decisions, which must always work).
    if (data != "check_member"
            and not data.startswith("premium_approve_")
            and not data.startswith("premium_reject_")):
        if not await _is_channel_member(context.bot, user.id):
            await query.edit_message_text(
                _join_gate_text(), reply_markup=_join_keyboard(),
                parse_mode="HTML")
            return

    if data == "refresh":
        # Re-run exactly the result shown in THIS message (stored by
        # _store_result_spec under its message id) — so a 12:52 result
        # never silently becomes "current" data at 12:57.
        mid = query.message.message_id if query.message else None
        spec = (context.user_data.get("result_specs") or {}).get(mid)
        if not spec:
            await query.edit_message_text(
                f"{pe('warning')} This result is too old to refresh — "
                "open it again from the menu.",
                reply_markup=back_keyboard(), parse_mode="HTML")
            return
        if spec[0] == "an":
            _, tool, assets = spec
            if tool == "ai":
                await _run_ai_signal(query, context, assets, animate=False)
            elif tool == "counter":
                await _run_counter(query, assets, context)
            elif tool == "trend":
                await _run_trend(query, assets, context)
            elif tool == "market":
                await _run_market(query, assets, context)
        elif spec[0] == "fs":
            if len(spec) == 7:
                _, assets, days, mode, pct, start, end = spec
            else:  # pre-% specs (cannot survive a restart, defensive)
                _, assets, days, mode, start, end = spec
                pct = 70
            context.user_data["fs_asset"] = assets
            context.user_data["fs_days"] = days
            context.user_data["fs_mode"] = mode
            context.user_data["fs_pct"] = pct
            await _run_futures(query, context, start, end, animate=False)
        elif spec[0] == "botstatus":
            # Bot Status is owner-only (the owner, 2026-11-27) — a
            # crafted callback from anyone else gets nothing.
            if not _is_admin(user.id):
                return
            await _run_bot_status(query, context)
        elif spec[0] == "payouts":
            await _run_payouts(query, context)
        return

    if data == "check_member":
        _member_cache.pop(user.id, None)
        if await _is_channel_member(context.bot, user.id, use_cache=False):
            await query.edit_message_text(
                f"{pe('diamond')}{pe('lightning')} Welcome to BLUE FISH PRIME! {pe('fire')}\n"
                "You joined the Blue Fish trading community — the tools are now unlocked.\n"
                "Pick a tool below to start.",
                reply_markup=main_keyboard(admin=_is_admin(user.id)), parse_mode="HTML")
        else:
            await query.edit_message_text(
                _join_gate_text() + "\n\nStill not a member — join the channel first, "
                "then tap \"I Joined\" again.",
                reply_markup=_join_keyboard(), parse_mode="HTML")
        return

    if data.startswith("premium_approve_") or data.startswith("premium_reject_"):
        if user.id not in config.ADMIN_USER_IDS:
            await query.edit_message_text(
                "Only the owner can approve premium requests.",
                reply_markup=back_keyboard(), parse_mode="HTML")
            return
        approve = data.startswith("premium_approve_")
        try:
            req_id = int(data.rsplit("_", 1)[1])
        except ValueError:
            return
        req = database.get_premium_request(req_id)
        if not req:
            await query.edit_message_text("Request not found.",
                                          reply_markup=back_keyboard(), parse_mode="HTML")
            return
        if approve:
            until = database.grant_premium(req["telegram_id"], days=30)
            database.set_premium_request_status(req_id, "approved")
            verdict = f"Approved — premium until {until}."
            try:
                await context.bot.send_message(
                    chat_id=req["telegram_id"],
                    text=f"{pe('crown')}{pe('diamond')} Your BLUE FISH PRIME Premium is active! "
                         f"Valid until {until}. {pe('fire')}",
                    parse_mode="HTML")
            except Exception:
                pass
        else:
            database.set_premium_request_status(req_id, "rejected")
            verdict = "Rejected."
            try:
                await context.bot.send_message(
                    chat_id=req["telegram_id"],
                    text=f"{pe('cross')} Your premium request was not approved. "
                         "Contact @LittleMatrix if you think this is a mistake.",
                    parse_mode="HTML")
            except Exception:
                pass
        await query.edit_message_text(
            f"Premium request #{req_id} ({html.escape(str(req['txn_hash']))}): {verdict}",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return

    if data == "menu_main":
        # Back to the menu abandons any half-finished text flow
        # (%, date, list, ...) and stops the dashboard refresh —
        # otherwise the next typed message would be read as that
        # flow's input, or the refresh would overwrite the menu.
        _cancel_admin_dash(user.id)
        context.user_data.pop("flow", None)
        await query.edit_message_text(
            f"{pe('diamond')}{pe('lightning')} BLUE FISH PRIME — Trading Terminal {pe('sparkle')}\n"
            f"Pick a tool below. {pe('fire')}",
            reply_markup=main_keyboard(admin=_is_admin(user.id)), parse_mode="HTML")
        return

    if data == "menu_admin":
        # The button is hidden from everyone else, but a crafted
        # callback could still reach this — check again here.
        if not _is_admin(user.id):
            await query.edit_message_text(
                "Only the owner can open the Admin Dashboard.",
                reply_markup=back_keyboard(), parse_mode="HTML")
            return
        await _run_admin_dashboard(query, context, user.id)
        return

    if data == "admin_stat_noop":
        # Display-only stat button: tapping does nothing (the tap
        # itself was already answered at the top of menu_callback).
        return

    if data == "menu_futures":
        context.user_data["flow"] = "futures_mode"
        kb = [[_btn("OTC FS", "fs_mode_OTC FS", icon="mode_otc"),
               _btn("BLACKOUT FS", "fs_mode_BLACKOUT FS", icon="mode_blackout")],
              [_btn("WHITEOUT FS", "fs_mode_WHITEOUT FS", icon="mode_whiteout"),
               _btn("REAL MARKET FS", "fs_mode_REAL", icon="mode_real")],
              [_btn("Back", "menu_main", icon="back")]]
        await query.edit_message_text(
            f"{pe('diamond')}{pe('lightning')} BLUE FISH PRIME • FUTURE SIGNALS {pe('fire')}\n"
            "Choose a signal mode:",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        return

    if data.startswith("fs_mode_"):
        context.user_data["fs_mode"] = data[len("fs_mode_"):]
        context.user_data["flow"] = "futures_asset"
        context.user_data["fs_selected"] = set()
        mode = context.user_data["fs_mode"]
        # db mode: assets come from the candle database — no Quotex
        # login here. If the backfill has not finished yet, the DB
        # may be empty; the picker then shows a message instead.
        if config.DATA_MODE == "db":
            db_assets = _db().get_assets()
            assets = _filter_assets_by_mode(db_assets, mode)
            if assets:
                live = False
                context.user_data["open_assets"] = db_assets
                context.user_data["fs_assets"] = assets
                context.user_data["fs_assets_page"] = 0
                kb, page, total_pages = _asset_picker_keyboard(assets, 0, mode)
                await query.edit_message_text(
                    _asset_picker_text(assets, page, total_pages, mode, live),
                    reply_markup=kb, parse_mode="HTML")
                return
            await query.edit_message_text(
                f"{pe('hourglass')} The database is still collecting — no candles yet. {pe('diamond')}\n"
                "The backfill collects the last 5 days first. "
                "Please try again in a bit.",
                reply_markup=back_keyboard(), parse_mode="HTML")
            return
        # Live mode: show EVERY open asset from Quotex (get_all_assets).
        # No login / fetch failure: fall back to the 6 known assets.
        assets = None
        live = False
        if config.DATA_MODE == "live" and config.has_quotex_login():
            try:
                await query.edit_message_text(
                    f"{pe('hourglass')} Connecting to Quotex... {pe('diamond')}",
                    parse_mode="HTML")
                await asyncio.sleep(0.4)
                open_assets = await quotex_client.get_open_assets()
            except quotex_client.QuotexRateLimited as exc:
                await query.edit_message_text(f"{pe('warning')} {html.escape(str(exc))}",
                                              reply_markup=back_keyboard(), parse_mode="HTML")
                return
            except Exception:
                open_assets = None
            if open_assets:
                context.user_data["open_assets"] = open_assets
                assets = _filter_assets_by_mode(open_assets, mode)
                if assets:
                    live = True
        if not assets:
            assets = _filter_assets_by_mode(config.SAMPLE_ASSETS, mode)
            if not assets:
                # No real-market assets; show the 6 known assets
                # instead of an empty picker.
                assets = list(config.SAMPLE_ASSETS)
            context.user_data["open_assets"] = list(config.SAMPLE_ASSETS)
        context.user_data["fs_assets"] = assets
        context.user_data["fs_assets_page"] = 0
        kb, page, total_pages = _asset_picker_keyboard(assets, 0, mode)
        await query.edit_message_text(
            _asset_picker_text(assets, page, total_pages, mode, live),
            reply_markup=kb, parse_mode="HTML")
        return

    if data.startswith("fs_assets_page_"):
        page = int(data[len("fs_assets_page_"):])
        assets = context.user_data.get("fs_assets") or config.SAMPLE_ASSETS
        mode = context.user_data.get("fs_mode", "OTC FS")
        live = config.DATA_MODE == "live" and config.has_quotex_login()
        context.user_data["fs_assets_page"] = page
        selected = context.user_data.get("fs_selected") or set()
        kb, page, total_pages = _asset_picker_keyboard(assets, page, mode, selected)
        await query.edit_message_text(
            _asset_picker_text(assets, page, total_pages, mode, live, selected),
            reply_markup=kb, parse_mode="HTML")
        return

    if data == "fs_asset_ALL":
        # SELECT ALL toggles everything (on, or off when all selected).
        assets = context.user_data.get("fs_assets") or config.SAMPLE_ASSETS
        selected = set(context.user_data.get("fs_selected") or set())
        if set(assets) <= selected:
            selected = set()
        else:
            selected = set(assets)
        context.user_data["fs_selected"] = selected
        await _rerender_fs_picker(query, context)
        return

    if data.startswith("fs_asset_"):
        # Toggle one asset — the picker stays on the same page and
        # shows a premium check on selected buttons.
        asset = data[len("fs_asset_"):]
        selected = set(context.user_data.get("fs_selected") or set())
        if asset in selected:
            selected.discard(asset)
        else:
            selected.add(asset)
        context.user_data["fs_selected"] = selected
        await _rerender_fs_picker(query, context)
        return

    if data == "fs_next":
        assets = context.user_data.get("fs_assets") or config.SAMPLE_ASSETS
        selected = set(context.user_data.get("fs_selected") or set())
        if not selected:
            await _rerender_fs_picker(query, context, warn=True)
            return
        if set(assets) <= selected:
            context.user_data["fs_asset"] = "ALL"
        else:
            # Keep the picker's order (OTC first, sorted).
            context.user_data["fs_asset"] = [a for a in assets if a in selected]
        await _show_futures_days_question(query, context)
        return

    if data.startswith("fs_days_"):
        context.user_data["fs_days"] = int(data[len("fs_days_"):])
        # Next step: the user TYPES the win-rate % (70-100) — the
        # owner, 2026-11-27. The % replaces the 7-min gap filter.
        context.user_data["flow"] = "futures_pct"
        await query.edit_message_text(
            f"{pe('diamond')} Win rate filter {pe('chart')}\n{DIVIDER}\n\n"
            "Type the minimum win rate a signal must have — a whole number from 70 to 100.",
            reply_markup=InlineKeyboardMarkup(
                [[_btn("Back", "menu_main", icon="back")]]),
            parse_mode="HTML")
        return

    if data == "fs_time_custom":
        context.user_data["flow"] = "futures_custom_time"
        await query.edit_message_text(
            f"{pe('sparkle')} Custom Time {pe('pin')}\n\n"
            "Send the start and end time, e.g.:\n"
            "09:00 10:59\nor 09:00-10:59",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return

    if data.startswith("fs_time_"):
        start_end = data[len("fs_time_"):]
        start, end = start_end.split("-")
        await _run_futures(query, context, start, end)
        return

    if data == "menu_botstatus":
        # Bot Status is owner-only (the owner, 2026-11-27). The
        # button is hidden from everyone else, but a crafted
        # callback could still reach this — check again here.
        if not _is_admin(user.id):
            await query.edit_message_text(
                "Only the owner can see Bot Status.",
                reply_markup=back_keyboard(), parse_mode="HTML")
            return
        await _run_bot_status(query, context)
        return
    if data == "menu_ai":
        await _show_analysis_picker(query, context, "ai")
        return
    if data == "menu_checker":
        # First: which checker? OTC / Real / Blackout / Whiteout
        # (the owner, 2026-11-27). The MTG question comes next.
        kb = [[_btn("OTC Checker", "checker_mode_OTC", icon="mode_otc"),
               _btn("Real Market Checker", "checker_mode_REAL", icon="mode_real")],
              [_btn("Blackout Checker", "checker_mode_BLACKOUT", icon="mode_blackout"),
               _btn("Whiteout Checker", "checker_mode_WHITEOUT", icon="mode_whiteout")],
              [_btn("Back", "menu_main", icon="back")]]
        await query.edit_message_text(
            f"{pe('search')}{pe('diamond')} Blue Fish Prime Check\n{DIVIDER}\n\n"
            "Which checker do you want?",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        return
    if data.startswith("checker_mode_"):
        context.user_data["checker_mode"] = data[len("checker_mode_"):]
        # Ask: MTG1 results or non-MTG (M1 only)? The choice is
        # stored and used when the pasted list is checked — exactly
        # as selected, in every checker mode (the owner, 2026-11-27).
        kb = [[_btn("MTG1", "checker_mtg_1", icon="mtg"),
               _btn("Non-MTG", "checker_mtg_0", icon="nomtg")],
              [_btn("Back", "menu_main", icon="back")]]
        await query.edit_message_text(
            f"{pe('search')}{pe('diamond')} Blue Fish Prime Check\n{DIVIDER}\n\n"
            "Do you want MTG1 results or non-MTG (M1 only)?",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        return
    if data in ("checker_mtg_1", "checker_mtg_0"):
        context.user_data["checker_mtg1"] = (data == "checker_mtg_1")
        if config.DATA_MODE == "db":
            # Ask for the date: the user types it, or taps Today.
            context.user_data["flow"] = "checker_date"
            kb = [[_btn("Today", "checker_date_today", icon="days")],
                  [_btn("Back", "menu_main", icon="back")]]
            await query.edit_message_text(
                _checker_date_prompt(),
                reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        else:
            context.user_data["checker_date"] = _yesterday_date_str()
            context.user_data["flow"] = "checker_list"
            await query.edit_message_text(
                _checker_list_prompt(_yesterday_date_str(),
                                     context.user_data.get("checker_mode", "OTC")),
                reply_markup=back_keyboard(), parse_mode="HTML")
        return
    if data == "checker_date_today":
        today = _today_date_str()
        context.user_data["checker_date"] = today
        context.user_data["flow"] = "checker_list"
        await query.edit_message_text(
            _checker_list_prompt(today,
                                 context.user_data.get("checker_mode", "OTC")),
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    if data == "menu_market":
        await _show_analysis_picker(query, context, "market")
        return
    if data == "menu_otc":
        await _run_otc_board(query, context)
        return
    if data == "menu_payouts":
        await _run_payouts(query, context)
        return
    if data.startswith("payout_"):
        # A payout button opens that market's Market Live detail.
        await _run_market(query, data[len("payout_"):], context)
        return
    if data == "menu_counter":
        await _show_analysis_picker(query, context, "counter")
        return
    if data == "menu_trend":
        await _show_analysis_picker(query, context, "trend")
        return
    if data.startswith("an_page_"):
        # an_page_<tool>_<page> — re-render the analysis picker page.
        _, _, tool, page = data.split("_")
        context.user_data["an_page"] = int(page)
        assets = _analysis_assets()
        selected = context.user_data.get("an_selected") or set()
        kb, page, total_pages = _analysis_picker_keyboard(
            assets, int(page), tool, selected)
        await query.edit_message_text(
            _analysis_picker_text(tool, page, total_pages, selected),
            reply_markup=kb, parse_mode="HTML")
        return
    if data.startswith("an_all_"):
        # SELECT ALL toggles every market (on, or off when all selected).
        tool = data[len("an_all_"):]
        assets = _analysis_assets()
        selected = set(context.user_data.get("an_selected") or set())
        if set(assets) <= selected:
            selected = set()
        else:
            selected = set(assets)
        context.user_data["an_selected"] = selected
        await _rerender_an_picker(query, context, tool)
        return
    if data.startswith("an_asset_"):
        # an_asset_<tool>_<asset> — toggle the market. The picker stays
        # on the same page and shows a premium check on selected buttons.
        rest = data[len("an_asset_"):]
        tool, asset = rest.split("_", 1)
        selected = set(context.user_data.get("an_selected") or set())
        if asset in selected:
            selected.discard(asset)
        else:
            selected.add(asset)
        context.user_data["an_selected"] = selected
        await _rerender_an_picker(query, context, tool)
        return
    if data.startswith("an_next_"):
        # Run the tool for the selected markets (1..N).
        tool = data[len("an_next_"):]
        selected = set(context.user_data.get("an_selected") or set())
        if not selected:
            await _rerender_an_picker(query, context, tool, warn=True)
            return
        assets = [a for a in _analysis_assets() if a in selected]
        if tool == "ai":
            await _run_ai_signal(query, context, assets)
        elif tool == "counter":
            await _run_counter(query, assets, context)
        elif tool == "trend":
            await _run_trend(query, assets, context)
        elif tool == "market":
            await _run_market(query, assets, context)
        return
    if data == "menu_backtest":
        await _run_backtest(query)
        return
    if data == "menu_news":
        await _run_news(query)
        return
    if data == "menu_formatter":
        context.user_data["flow"] = "formatter"
        await query.edit_message_text(
            f"{pe('sparkle')}{pe('diamond')} Signal Formatter — send a signal list.\n"
            "I will flip its format:\n"
            "USDMXN-OTC,09:02 M1 CALL -> USDMXN_otc,09:02 M1 CALL\n"
            "USDMXN_otc,09:02 M1 CALL -> USDMXN-OTC,09:02 M1 CALL",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    if data == "menu_swap":
        context.user_data["flow"] = "swap"
        await query.edit_message_text(
            f"{pe('refresh')} Swap C/P — send a signal list and I will swap CALL and PUT.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    if data == "menu_timezone":
        context.user_data["flow"] = "timezone"
        await query.edit_message_text(
            f"{pe('pin')} Timezone — send a time (HH:MM). I will convert it from UTC+6 to UTC+0.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    if data == "menu_font":
        context.user_data["flow"] = "font"
        await query.edit_message_text(
            f"{pe('sparkle')} Text Font — send some text and I will make it bold.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    if data == "menu_filter":
        context.user_data["flow"] = "filter_aston"
        await query.edit_message_text(
            f"{pe('chart')}{pe('diamond')} Blue Fish Prime Filter (separate system — not part of signal generation)\n\n"
            "Send the Aston list first. Then I will ask for the Newexe list.\n"
            "Rules: remove 14:00-19:00, remove both signals of any 1-4 minute pair "
            "in each list, after merging remove only the Aston one.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    if data == "menu_status":
        user = update.effective_user
        u = database.get_or_create_user(user.id, user.username or "")
        premium = f"{pe('diamond')}{pe('crown')} Premium" if database.is_premium(user.id) else "Free"
        until = u.get("premium_until") or "—"
        await query.edit_message_text(
            f"{pe('crown')}{pe('diamond')} My Status\n{DIVIDER}\nID: {user.id}\nStatus: {premium}\n"
            f"Premium until: {until}",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    if data == "menu_about":
        await query.edit_message_text(
            f"{pe('diamond')}{pe('lightning')} Blue Fish Prime — Trading Terminal {pe('sparkle')}\n"
            f"Created by LITTLE MATRIX. Signals are based on past patterns, not guarantees.\n"
            f"{CONTACT_LINE}\n\n" + DISCLAIMER,
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    if data == "menu_autolive":
        on = database.is_auto_subscriber(user.id)
        toggle = _btn("Turn OFF" if on else "Turn ON",
                      "autolive_toggle",
                      icon="toggle_off" if on else "toggle_on")
        await query.edit_message_text(
            f"{pe('bell')} Auto Live Signal\n{DIVIDER}\n"
            f"Status: {'ON' if on else 'OFF'}\n\n"
            "When ON, the bot sends you the next 2-hour window's top "
            "signals right after every collection run.",
            reply_markup=InlineKeyboardMarkup([[toggle],
                                               [_btn("Back", "menu_main", icon="back")]]),
            parse_mode="HTML")
        return
    if data == "autolive_toggle":
        on = not database.is_auto_subscriber(user.id)
        database.set_auto_signals(user.id, on)
        toggle = _btn("Turn OFF" if on else "Turn ON",
                      "autolive_toggle",
                      icon="toggle_off" if on else "toggle_on")
        await query.edit_message_text(
            f"{pe('bell')} Auto Live Signal\n{DIVIDER}\n"
            f"Status: {'ON' if on else 'OFF'}\n\n"
            "When ON, the bot sends you the next 2-hour window's top "
            "signals right after every collection run.",
            reply_markup=InlineKeyboardMarkup([[toggle],
                                               [_btn("Back", "menu_main", icon="back")]]),
            parse_mode="HTML")
        return
    if data == "menu_premium":
        context.user_data["flow"] = "premium_txn"
        pay_id = config.BINANCE_PAY_ID
        if pay_id:
            pay_text = (f"Pay {config.USDT_AMOUNT} USDT via Binance Pay ID:\n"
                        f"{html.escape(pay_id)}\n\n")
        else:
            pay_text = "Ask the owner (@LittleMatrix) for the Binance Pay ID.\n\n"
        await query.edit_message_text(
            f"{pe('diamond')}{pe('crown')} Blue Fish Prime Premium — 1 month, {config.USDT_AMOUNT} USDT {pe('fire')}\n\n"
            + pay_text +
            "After paying, paste the transaction ID here. The owner will check and "
            "approve it, then your 30-day Premium starts.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return


def _fs_mode_title(mode):
    """Display title for a futures mode."""
    return {"OTC FS": "OTC FS", "BLACKOUT FS": "BLACKOUT FS",
            "WHITEOUT FS": "WHITEOUT FS",
            "REAL": "REAL MARKET FS"}.get(mode, mode)


def _fs_pairs_label(mode):
    return "Real Market Pairs" if mode == "REAL" else "OTC Pairs"


def _futures_template_head(mode, date_dot, pct):
    """The owner's FUTURE SIGNALS template header (2026-11-27) —
    premium emojis, no extra lines, a blank line between the header
    lines (the owner, 2026-11-27: the packed header looked cramped).
    The date is the day the signals are FOR (today), in DD.MM.YYYY.
    The 'Avoid Below' % is the user-typed threshold. The MARTINGALE
    line is replaced by 'MTG 1 STEP' (the owner, 2026-11-27). Where
    the owner's template has a Unicode emoji with no premium match
    in the packs, it is dropped, not kept as Unicode (premium-only
    rule): 🇧🇩 (no premium flag), 🤪 (no zany face), ➕ (no plus sign).
    Signal lines stay packed under the header (the owner, 2026-11-27)
    — see _fit_futures_text."""
    return (f"{pe('diamond')}{pe('lightning')}• FUTURE SIGNALS — {_fs_mode_title(mode)} {pe('fire')}\n\n"
            f"{DIVIDER}\n\n"
            f"{pe('sparkle')}{date_dot}{pe('star')}\n\n"
            f"{pe('hourglass')}TIMEZONE: UTC/GMT +6:00 [DHAKA, BANGLADESH]\n\n"
            f"{pe('warning')}Filtered {_fs_pairs_label(mode)} — Avoid Below {pct}% &amp; Doji Candles\n\n"
            f"{pe('red')} MTG 1 STEP\n\n"
            f"{pe('next')}1 MINUTES:\n\n"
            f" {pe('fire')} BLUE FISH PRIME {pe('fire')}")


def _rules_footer(mode):
    """Bengali 'Rules Of Signal' footer — blackout/whiteout lists
    only (the owner, 2026-11-27). The owner's 👇 (point down) and
    🖥 (computer) have no premium match in the packs, so they are
    dropped, not kept as Unicode (premium-only rule)."""
    if mode == "BLACKOUT FS":
        return (f"\n\n{pe('bell')}Rules Of Signal\n"
                f"আগের কেন্ডেল যেটা হবে তার Opposite Direction এ Entry নিবেন{pe('sparkle')}\n\n"
                f"{pe('chart')}যেমন :- 14:50 এর ক্যান্ডেল যা হবে 14:51 এ তার বিপরীত DIRECTION এ Entry নিতে হবে {pe('check')}")
    if mode == "WHITEOUT FS":
        return (f"\n\n{pe('bell')}Rules Of Signal\n"
                f"আগের কেন্ডেল যেটা হবে তার Same Direction এ Entry নিবেন{pe('sparkle')}\n\n"
                f"{pe('chart')}যেমন :- 14:50 এর ক্যান্ডেল যা হবে 14:51 এ তার একই DIRECTION এ Entry নিতে হবে {pe('check')}")
    return ""


def _fit_futures_text(header, lines, footer, limit=3950):
    """Join header + signal lines + footer, dropping whole lines
    from the end when the Telegram 4096-char cap would cut a line
    (or a <code> tag) in half. Dropped lines are counted, never
    silently lost. One blank line sits between BLUE FISH PRIME and
    the first signal (the owner, 2026-11-27); signals stay packed
    after that, one per line. The gaps live between the HEADER
    lines, not between signals (see _futures_template_head)."""
    body = header
    kept = 0
    for ln in lines:
        sep = "\n\n" if kept == 0 else "\n"
        if len(body) + len(sep) + len(ln) + len(footer) > limit:
            break
        body += sep + ln
        kept += 1
    dropped = len(lines) - kept
    if dropped:
        body += f"\n… +{dropped} more (narrow the window or raise the %)"
    return body + footer


async def _compute_futures_text_db(context, start, end):
    """FUTURES SIGNALS from the candle database (Render / db mode).

    No Quotex login anywhere in this function — every candle comes
    from the DB (backfill + 2-hourly collector). Single asset:
    majority vote over the last N completed days with data.
    SELECT ALL: the same for every asset in the DB for this mode,
    then the cross-asset filter (win rate + strength, max 6/hour,
    max 12 total — NO 7-min gap; the owner replaced it with the
    user-typed % threshold, 2026-11-27). Blackout/whiteout use the
    T-1 engine (see signal_engine.generate_prev_minute_signals) and
    print WITHOUT a direction. The result follows the owner's
    template exactly: no Assets/Dates/Days line, no Time/Source
    line, no Signals-count line, no disclaimer, no Generated line."""
    asset = context.user_data.get("fs_asset", "USDMXN_otc")
    if isinstance(asset, list) and len(asset) == 1:
        # One picked asset behaves exactly like the old single pick
        # (no cross-asset filter — there is nothing to rank across).
        asset = asset[0]
    days = context.user_data.get("fs_days", 1)
    mode = context.user_data.get("fs_mode", "OTC FS")
    pct = context.user_data.get("fs_pct", 70)
    min_wr = pct / 100.0
    db = _db()
    today = _today_date_str()
    date_dot = dot_date(today)
    is_bw = mode in ("BLACKOUT FS", "WHITEOUT FS")
    bw_mode = {"BLACKOUT FS": "BLACKOUT",
               "WHITEOUT FS": "WHITEOUT"}.get(mode)

    def _gen(a, dates):
        if is_bw:
            return generate_prev_minute_signals_from_db(
                db, a, dates, start, end, mode=bw_mode, min_win_rate=min_wr)
        return generate_futures_signals_from_db(
            db, a, dates, start, end, min_win_rate=min_wr, apply_gap=False)

    # Multi-asset selection: a list of assets, or "ALL" for every asset
    # in this mode. Both go through the cross-asset filter (win rate +
    # strength, max 6/hour, max 12 total — no gap).
    if isinstance(asset, list) or asset == "ALL":
        if asset == "ALL":
            db_assets = _filter_assets_by_mode(db.get_assets(), mode)
        else:
            # Only the picked assets, in the user's pick order.
            all_mode = _filter_assets_by_mode(db.get_assets(), mode)
            order = {a: i for i, a in enumerate(asset)}
            db_assets = [a for a in all_mode if a in order]
            db_assets.sort(key=lambda a: order.get(a, 999))
        if not db_assets:
            return (f"{pe('cross')} No candles for the selected assets in the database yet.\n"
                    "The backfill collects the last 5 days first. "
                    "Please try again in a bit.")
        all_dates = db.get_dates()
        completed = [d for d in all_dates if d < today] or all_dates
        dates = completed[-days:]
        all_signals = []
        for a in db_assets:
            all_signals.extend(_gen(a, dates))
        if not all_signals:
            return (f"{pe('cross')} No signals found after the filter.\n"
                    "Try another window, or wait for the next collection run.")
        kept = cross_asset_filter(all_signals, min_win_rate=min_wr,
                                  apply_gap=False)
        lines = [f"<code>{ln}</code>" for ln in
                 format_signals_list(kept, show_direction=not is_bw)]
        return _fit_futures_text(
            _futures_template_head(mode, date_dot, pct), lines,
            _rules_footer(mode))

    dates, _available = available_signal_dates(db, asset, days)
    if not dates:
        return (f"{pe('cross')} No candles for {display_asset(asset)} in the database yet.\n"
                "The backfill collects the last 5 days first. "
                "Please try again in a bit.")
    signals = _gen(asset, dates)
    lines = [f"<code>{ln}</code>" for ln in
             format_signals_list(signals, show_direction=not is_bw)]
    if not lines:
        diag_text = ""
        if not is_bw:
            try:
                d = diagnose_window(load_days_from_db(db, asset, dates), asset, start, end)
                diag_text = (f"\n\n{pe('search')} Diagnostics:\n"
                             f"Candles: {d['total']} | Raw: {d['raw']} | {pe('minus')}: {d['doji']}\n"
                             f"Median body: {d['median_body']:.5f} | Median range: {d['median_range']:.5f}\n"
                             f"Median body/range: {d['median_ratio']:.2f} | Price: {d['median_price']:.4f}\n"
                             f"Too few strong candles in this window, so the filter removed them all. "
                             f"Try another window (e.g. 00:00-01:59), or wait for the next collection run.")
            except Exception:
                pass
        return f"{pe('cross')} No signals found after the filter." + diag_text
    return _fit_futures_text(
        _futures_template_head(mode, date_dot, pct), lines,
        _rules_footer(mode))


async def _compute_futures_text(context, start, end):
    """Build the FUTURE SIGNALS result text (no Telegram UI here).

    Returns the full result text. Used by both the callback flow
    (_run_futures, which edits the tapped message) and the text flow
    (custom time, which edits a 'Working...' message). The result
    follows the owner's template exactly (see _futures_template_head):
    no Assets/Dates/Days line, no Time/Source line, no Signals-count
    line, no disclaimer, no Generated line."""
    asset = context.user_data.get("fs_asset", "USDMXN_otc")
    if isinstance(asset, list) and len(asset) == 1:
        # One picked asset behaves exactly like the old single pick
        # (no cross-asset filter — there is nothing to rank across).
        asset = asset[0]
    days = context.user_data.get("fs_days", 1)
    mode = context.user_data.get("fs_mode", "OTC FS")
    pct = context.user_data.get("fs_pct", 70)
    min_wr = pct / 100.0
    eff_date = _effective_date()
    date_dot = dot_date(_today_date_str())
    is_bw = mode in ("BLACKOUT FS", "WHITEOUT FS")
    bw_mode = {"BLACKOUT FS": "BLACKOUT",
               "WHITEOUT FS": "WHITEOUT"}.get(mode)
    # db mode: everything comes from the candle database (no login).
    if config.DATA_MODE == "db":
        return await _compute_futures_text_db(context, start, end)

    def _gen_one(a, day_cs):
        if is_bw:
            return generate_prev_minute_signals(
                [day_cs], a, start, end, mode=bw_mode, min_win_rate=min_wr)
        return generate_futures_signals(
            [day_cs], a, start, end, min_win_rate=min_wr, apply_gap=False)

    # SELECT ALL: fetch the open assets for this mode (live), 3-at-a-time,
    # then the cross-asset filter (win rate + real strength, max 6/hour,
    # max 12 total — no gap). Without a live login only USDMXN is shown.
    # A list (multi-asset selection) takes the same path, restricted
    # to the picked assets.
    if asset == "ALL" or isinstance(asset, list):
        if isinstance(asset, list):
            assets = list(asset)
        else:
            assets = context.user_data.get("fs_assets") or config.SAMPLE_ASSETS
        if config.DATA_MODE != "live" or not config.has_quotex_login():
            # No live login: fall through to the single-asset path
            # with the first picked asset.
            asset = assets[0] if isinstance(asset, list) else "USDMXN_otc"
        else:
            all_signals = []
            failed = []
            async def _one(a):
                try:
                    cs, src = await get_candles_for(a, eff_date)
                except quotex_client.QuotexRateLimited:
                    raise
                except Exception as exc:
                    failed.append(f"{a}: {exc}")
                    return []
                if not cs:
                    failed.append(f"{a}: {src}")
                    return []
                day_cs = [c for c in cs if c.get("datetime", "").startswith(eff_date)]
                # Real strength (body/range) is set inside
                # generate_futures_signals — do NOT overwrite it with
                # win_rate (for 1-day signals win_rate is always 1.0).
                return _gen_one(a, day_cs)
            try:
                results = await quotex_client.fetch_batch_3_at_a_time_async(assets, _one)
            except quotex_client.QuotexRateLimited as exc:
                return f"{pe('cross')} {html.escape(str(exc))}"
            for a, sigs in results.items():
                all_signals.extend(sigs or [])
            if not all_signals:
                detail = "; ".join(html.escape(str(f)) for f in failed[:3]) if failed else "No reason reported"
                return (f"{pe('cross')} No data found for any asset.\n{detail}\n"
                        "Check the Quotex login / 429 and try again.")
            kept = cross_asset_filter(all_signals, min_win_rate=min_wr,
                                      apply_gap=False)
            lines = [f"<code>{ln}</code>" for ln in
                     format_signals_list(kept, show_direction=not is_bw)]
            return _fit_futures_text(
                _futures_template_head(mode, date_dot, pct), lines,
                _rules_footer(mode))
    candles, source = await get_candles_for(asset, eff_date)
    if candles is None:
        return (f"{pe('cross')} No data found: {html.escape(str(source))}\n"
                "Check the Quotex login in live mode, or put yesterday's JSON file "
                "for this asset in the data/ folder with collect_yesterday.py.\n"
                "If it says 'login failed', check the email/password in .env.")
    # Filter candles to the requested date + window for the engine input.
    day_candles = [c for c in candles
                   if c.get("datetime", "").startswith(eff_date)]
    signals = _gen_one(asset, day_candles)
    lines = [f"<code>{ln}</code>" for ln in
             format_signals_list(signals, show_direction=not is_bw)]
    # If still 0, show diagnostics so the user can see WHY (not just "0").
    if not lines:
        diag_text = ""
        if not is_bw:
            try:
                d = diagnose_window([day_candles], asset, start, end)
                diag_text = (f"\n\n{pe('search')} Diagnostics:\n"
                             f"Candles: {d['total']} | Raw: {d['raw']} | {pe('minus')}: {d['doji']}\n"
                             f"Median body: {d['median_body']:.5f} | Median range: {d['median_range']:.5f}\n"
                             f"Median body/range: {d['median_ratio']:.2f} | Price: {d['median_price']:.4f}\n"
                             f"Too few strong candles in this window, so the filter removed them all. "
                             f"Try another window (e.g. 00:00-01:59), or send "
                             f"data/candles_{asset}_{eff_date}.json so I can check it.")
            except Exception:
                pass
        return f"{pe('cross')} No signals found after the filter." + diag_text
    return _fit_futures_text(
        _futures_template_head(mode, date_dot, pct), lines,
        _rules_footer(mode))


async def _run_futures(query, context, start, end, animate=True):
    """Callback flow: animated progress, then the final result.
    The result follows the owner's template exactly (no Generated
    line — removed 2026-11-27); a Refresh button re-runs exactly
    this selection."""
    asset = context.user_data.get("fs_asset", "USDMXN_otc")
    days = context.user_data.get("fs_days", 1)
    mode = context.user_data.get("fs_mode", "OTC FS")
    pct = context.user_data.get("fs_pct", 70)
    label = _fs_asset_label(asset)
    step1 = ("Reading database..." if config.DATA_MODE == "db"
             else "Connecting to Quotex...")
    if animate:
        try:
            await query.edit_message_text(
                f"{pe('hourglass')} {step1} {pe('diamond')} {label} | {days} day(s) | {start}-{end}",
                parse_mode="HTML")
            await asyncio.sleep(0.5)
            await query.edit_message_text(
                f"{pe('refresh')} Fetching candles... {pe('chart')} {label} | {start}-{end}",
                parse_mode="HTML")
            await asyncio.sleep(0.5)
            await query.edit_message_text(
                f"{pe('lightning')} Generating signals... {pe('chart')} {label} | {start}-{end}",
                parse_mode="HTML")
            await asyncio.sleep(0.5)
        except Exception:
            pass  # progress edits are cosmetic; never block the result
    text = await _compute_futures_text(context, start, end)
    await query.edit_message_text(text, reply_markup=_refresh_keyboard(),
                                  parse_mode=_html_mode(text))
    _store_result_spec(context, query,
                       ("fs", asset, days, mode, pct, start, end))


async def _run_market(query, assets=None, context=None):
    """Market Live detail for each picked market: last candle (Dhaka
    time, close, direction, and how old it is), today's high/low and
    candle count, EMA trend + RSI. DB only — the live stream keeps
    the last candle at most ~1 minute old, the 2-hourly collector
    fills any gap. `assets` comes from the picker (str, list or None);
    None keeps the old default-asset behaviour."""
    assets = _resolve_analysis_assets(assets)
    if not assets:
        await query.edit_message_text(
            f"{pe('diamond')} Market Live {pe('chart')}\n"
            "No candles in the database yet — the backfill is still running.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    today = _today_date_str()
    sections = []
    for asset in assets:
        try:
            candles = _db().get_candles(asset, today)
        except Exception:
            candles = []
        if not candles:
            sections.append(f"{display_asset(asset)}: No candles today yet. "
                            "Try again after the next collection.")
            continue
        norm = [normalize_candle(c) for c in candles]
        last = norm[-1]
        direction = "CALL" if last["close"] > last["open"] else "PUT"
        hi = max(c["high"] for c in norm)
        lo = min(c["low"] for c in norm)
        closes = [c["close"] for c in norm]
        if len(closes) >= 21:
            trend = trend_direction(closes)
            trend_emoji = pe("up") if trend == "UP" else pe("down")
            trend_line = (f"{trend_emoji} Trend (EMA9/21): {trend} | "
                          f"{pe('chart')} RSI(14): {rsi(closes):.1f}")
        else:
            trend_line = "Not enough candles for trend/RSI yet."
        sections.append(
            f"{display_asset(asset)}\n"
            f"Last: {last['datetime'][11:16]} | {last['close']} | {direction} ({_candle_age_text(last)})\n"
            f"Today: {len(norm)} candles | High: {hi} | Low: {lo}\n"
            f"{trend_line}")
    title = (f" — {display_asset(assets[0])}" if len(assets) == 1
             else f" — {len(assets)} markets")
    text = (f"{pe('diamond')} Market Live{title} {pe('chart')}\n"
            f"{DIVIDER}\n"
            + "\n\n".join(sections)
            + "\n\nData updates every minute via the live stream.\n"
            + _generated_line())
    await query.edit_message_text(text, reply_markup=_refresh_keyboard(), parse_mode="HTML")
    if context is not None:
        _store_result_spec(context, query, ("an", "market", assets))


async def _run_payouts(query, context=None):
    """Payouts from the DB snapshot the collector stores each run.

    Shown as buttons, not a dead list: each button carries the asset
    + its 1M payout % with a premium money icon (the owner, 2026-11-26).
    Tapping one opens that market's Market Live detail — a high payout
    is worth a look before trading it. Best payout first, top 15.
    """
    try:
        rows = _db().get_payouts()
    except Exception:
        rows = []
    if not rows:
        text = (f"{pe('money')} Live Payouts {pe('dollar')}\n"
                "No payout snapshot yet — the collector stores one on every run. "
                "Try again after the next collection.")
        await query.edit_message_text(text, reply_markup=back_keyboard(), parse_mode="HTML")
        return
    shown = rows[:15]
    updated = rows[0][3] if rows and rows[0][3] else "—"
    text = (f"{pe('money')} Live Payouts (1M) {pe('dollar')}\n{DIVIDER}\n"
            "Tap a market to see its live detail.\n\n"
            f"Updated: {updated} (Dhaka, next update with the next collection)")
    kb = []
    for i in range(0, len(shown), 2):
        row = []
        for a, p, _o, _u in shown[i:i + 2]:
            label = (f"{display_asset(a)} {p:.0f}%" if p is not None
                     else f"{display_asset(a)} —")
            row.append(_btn(label, f"payout_{a}", icon="money"))
        kb.append(row)
    kb.append([_btn("Refresh", "refresh", icon="refresh")])
    kb.append([_btn("Back to Menu", "menu_main", icon="back")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb),
                                  parse_mode="HTML")
    if context is not None:
        _store_result_spec(context, query, ("payouts",))


async def _run_news(query):
    """News Desk: today + tomorrow's High/Medium impact events from
    the ForexFactory calendar (cached 1h, times converted to Dhaka)."""
    today = _today_date_str()
    tomorrow = (datetime.strptime(today, "%Y-%m-%d")
                + timedelta(days=1)).strftime("%Y-%m-%d")
    events = events_for_dhaka_dates({today, tomorrow})
    if events is None:
        text = (f"{pe('bell')}{pe('diamond')} Blue Fish Prime News\n"
                "News feed unavailable right now — try again later.")
    elif not events:
        text = (f"{pe('bell')}{pe('diamond')} Blue Fish Prime News\n"
                "No High/Medium impact events today or tomorrow.")
    else:
        lines = []
        for ev in events[:20]:
            when = ev["dhaka"][5:16] if ev["dhaka"] else f"{ev['date']} {ev['time']}"
            lines.append(f"{ev['impact']} | {when} | {ev['country']} — {ev['title']}")
        text = (f"{pe('bell')}{pe('diamond')} Blue Fish Prime News (Dhaka time)\n{DIVIDER}\n"
                + "\n".join(lines))
    await query.edit_message_text(text, reply_markup=back_keyboard(), parse_mode="HTML")


async def _run_bot_status(query, context=None):
    """Bot Status view: mode, database, days, per-asset candles,
    last/next collection, Quotex session, live stream, today's
    coverage. DB only — no Quotex login here. The result carries a
    Refresh button that re-reads the database on demand."""
    s = collector.get_status_summary(_db())
    if config.DATA_MODE == "db":
        mode_text = "Render / Database"
    elif config.DATA_MODE == "live":
        mode_text = "Live (Quotex)"
    else:
        mode_text = "Sample (local)"
    db_text = (f"{'PostgreSQL' if s['backend'] == 'postgres' else 'SQLite'} — "
               f"{s['total_candles']} candles")
    dates_text = ", ".join(s["dates"]) if s["dates"] else "none yet (backfill running)"
    per_asset = "\n".join(
        f"{display_asset(a)}: {n}" for a, n in sorted(s["by_asset"].items())) or "—"
    stream_status = s.get("live_stream_status")
    stream_last = s.get("live_stream_last_seen")
    if stream_status == "connected":
        stream_line = f"connected — last candle {stream_last or '—'} (Dhaka)"
    elif stream_status:
        stream_line = f"{stream_status} — last candle {stream_last or '—'} (Dhaka)"
    else:
        stream_line = "not running (stream off or not started yet)"
    text = (f"{pe('green')}{pe('diamond')} BLUE FISH PRIME • BOT STATUS {pe('sparkle')}\n{DIVIDER}\n"
            f"Mode: {mode_text}\n"
            f"Database: {db_text}\n"
            f"Days in DB: {dates_text}\n"
            f"Candles per asset:\n{per_asset}\n"
            f"Last collection: {s['last_collection'] or '—'}\n"
            f"Next collection: {s['next_collection'] or '—'}\n"
            f"Quotex session: {html.escape(str(s['quotex_session']))}\n"
            f"Live stream: {stream_line}\n"
            f"Last Quotex login: {s['quotex_last_login'] or '—'}\n"
            f"Today ({s['today_date']}) 00:00-now: {s['today_count']} candles\n"
            f"Time now (Dhaka): {s['now_dhaka']}")
    await query.edit_message_text(text, reply_markup=_refresh_keyboard(),
                                  parse_mode="HTML")
    if context is not None:
        _store_result_spec(context, query, ("botstatus",))


async def _run_otc_board(query, context):
    """OTC Board: live open assets when available, else the 6 sample names."""
    # db mode: assets come from the candle database, no Quotex login.
    if config.DATA_MODE == "db":
        db_assets = [a for a in _db().get_assets() if _is_otc(a)]
        if db_assets:
            shown = ", ".join(display_asset(a) for a in db_assets)
            text = (f"{pe('chart')}{pe('diamond')} OTC Board — {len(db_assets)} assets in database {pe('green')}\n{DIVIDER}\n{shown}")
            await query.edit_message_text(text, reply_markup=back_keyboard(), parse_mode="HTML")
            return
    assets = None
    if config.DATA_MODE == "live" and config.has_quotex_login():
        try:
            await query.edit_message_text(
                f"{pe('hourglass')} Loading OTC board... {pe('diamond')}", parse_mode="HTML")
            await asyncio.sleep(0.4)
            open_assets = await quotex_client.get_open_assets()
            if open_assets:
                assets = [a for a in open_assets if _is_otc(a)]
        except quotex_client.QuotexRateLimited as exc:
            await query.edit_message_text(f"{pe('warning')} {html.escape(str(exc))}",
                                          reply_markup=back_keyboard(), parse_mode="HTML")
            return
        except Exception:
            assets = None
    if assets:
        shown = ", ".join(display_asset(a) for a in assets)
        text = (f"{pe('chart')}{pe('diamond')} OTC Board — {len(assets)} open assets {pe('green')}\n{DIVIDER}\n{shown}")
    else:
        text = (f"{pe('chart')}{pe('diamond')} OTC Board\n"
                "OTC assets: USDMXN-OTC, EURUSD-OTC, GBPUSD-OTC, "
                "USDPKR-OTC, AUDCAD-OTC, EURJPY-OTC\n"
                "Live mode shows every open asset from Quotex.")
    await query.edit_message_text(text, reply_markup=back_keyboard(), parse_mode="HTML")


def _normalize_assets_arg(assets):
    """A single asset str, a list of assets, or None -> list (or None)."""
    if assets is None:
        return None
    if isinstance(assets, str):
        return [assets]
    return list(assets)


def _resolve_analysis_assets(assets):
    """Picker selection (str/list/None) -> list of assets. None keeps
    the old default-asset behaviour (BTCUSD first)."""
    assets = _normalize_assets_arg(assets)
    if not assets:
        default = _default_analysis_asset()
        return [default] if default else []
    return assets


async def _run_ai_signal(query, context, assets=None, animate=True):
    """AI Signal from the DB: each picked market's last candle (today),
    EMA9/21 trend and RSI(14) over its last 60 candles. No Quotex
    login, no sample file. `assets` comes from the picker (str, list
    or None); None keeps the old default-asset behaviour. The result
    carries its build time and a Refresh button, so a 12:52 result is
    never mistaken for live data at 12:57."""
    if animate:
        try:
            await query.edit_message_text(
                f"{pe('hourglass')} Analysing candles... {pe('star')}", parse_mode="HTML")
            await asyncio.sleep(0.5)
            await query.edit_message_text(
                f"{pe('lightning')} AI Signal — checking trend &amp; RSI... {pe('chart')}",
                parse_mode="HTML")
            await asyncio.sleep(0.5)
        except Exception:
            pass
    assets = _resolve_analysis_assets(assets)
    if not assets:
        await query.edit_message_text(
            f"{pe('cross')} No candles in the database yet.\n"
            "The backfill collects the last 5 days first. "
            "Please try again in a bit.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    sections = []
    for asset in assets:
        recent = _db_recent_candles(asset, 60)
        if len(recent) < 21:
            sections.append(f"{display_asset(asset)}: Not enough candles yet "
                            f"({len(recent)} today). Try again after the next collection.")
            continue
        closes = [c["close"] for c in recent]
        last = recent[-1]
        direction = "CALL" if last["close"] > last["open"] else "PUT"
        trend = trend_direction(closes)
        value = rsi(closes)
        trend_emoji = pe("up") if trend == "UP" else pe("down")
        sections.append(
            f"{display_asset(asset)}\n"
            f"Last: {last['datetime']} M1 {direction} ({_candle_age_text(last)})\n"
            f"{trend_emoji} Trend (EMA9/21): {trend} | {pe('chart')} RSI(14): {value:.1f}\n"
            f"{pe('money')} Close: {last['close']} | Open: {last['open']}")
    title = (f" — {display_asset(assets[0])}" if len(assets) == 1
             else f" — {len(assets)} markets")
    await query.edit_message_text(
        f"{pe('star')}{pe('diamond')} BLUE FISH PRIME • AI SIGNAL{title} {pe('sparkle')}\n{DIVIDER}\n"
        + "\n\n".join(sections)
        + f"\n\n{DISCLAIMER}\n{_generated_line()}",
        reply_markup=_refresh_keyboard(), parse_mode="HTML")
    _store_result_spec(context, query, ("an", "ai", assets))


async def _run_counter(query, assets=None, context=None):
    """Candle Scan from the DB: green/red count of each picked
    market's last 10 candles today. `assets` comes from the picker
    (str, list or None); None keeps the old default-asset behaviour."""
    assets = _resolve_analysis_assets(assets)
    if not assets:
        await query.edit_message_text(
            f"{pe('cross')} No candles in the database yet.\n"
            "The backfill collects the last 5 days first.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    sections = []
    for asset in assets:
        recent = _db_recent_candles(asset, 10)
        if not recent:
            sections.append(f"{display_asset(asset)}: No candles today yet.")
            continue
        green = sum(1 for c in recent if c["close"] > c["open"])
        red = sum(1 for c in recent if c["close"] < c["open"])
        sections.append(f"{display_asset(asset)} (last {len(recent)} today)\n"
                        f"{pe('green')} Green: {green} | {pe('red')} Red: {red}")
    title = (f" — {display_asset(assets[0])}" if len(assets) == 1
             else f" — {len(assets)} markets")
    await query.edit_message_text(
        f"{pe('chart')}{pe('diamond')} CANDLE SCAN{title}\n{DIVIDER}\n"
        + "\n\n".join(sections)
        + f"\n\n{_generated_line()}",
        reply_markup=_refresh_keyboard(), parse_mode="HTML")
    if context is not None:
        _store_result_spec(context, query, ("an", "counter", assets))


async def _run_trend(query, assets=None, context=None):
    """Trend Scan from the DB: EMA9/21 of each picked market's
    last 60 candles today. `assets` comes from the picker (str, list
    or None); None keeps the old default-asset behaviour."""
    assets = _resolve_analysis_assets(assets)
    if not assets:
        await query.edit_message_text(
            f"{pe('cross')} No candles in the database yet.\n"
            "The backfill collects the last 5 days first.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    sections = []
    for asset in assets:
        recent = _db_recent_candles(asset, 60)
        if len(recent) < 21:
            sections.append(f"{display_asset(asset)}: Not enough candles yet "
                            f"({len(recent)} today).")
            continue
        closes = [c["close"] for c in recent]
        trend = trend_direction(closes)
        e9 = ema(closes, 9)[-1]
        e21 = ema(closes, 21)[-1]
        trend_emoji = pe("up") if trend == "UP" else pe("down")
        sections.append(f"{display_asset(asset)}\n"
                        f"{trend_emoji} Trend: {trend}\n"
                        f"EMA9: {e9:.5f} | EMA21: {e21:.5f}")
    title = (f" — {display_asset(assets[0])}" if len(assets) == 1
             else f" — {len(assets)} markets")
    await query.edit_message_text(
        f"{pe('diamond')} TREND SCAN{title}\n{DIVIDER}\n"
        + "\n\n".join(sections)
        + f"\n\n{_generated_line()}",
        reply_markup=_refresh_keyboard(), parse_mode="HTML")
    if context is not None:
        _store_result_spec(context, query, ("an", "trend", assets))


async def _run_backtest(query):
    """Backtest Lab from the DB — a real backtest this time: signals
    for yesterday 09:00-10:59 built from the 2 days BEFORE yesterday
    (majority vote), then checked against yesterday's actual candles."""
    try:
        await query.edit_message_text(
            f"{pe('hourglass')} Running backtest... {pe('chart')}", parse_mode="HTML")
        await asyncio.sleep(0.6)
    except Exception:
        pass
    asset = _default_analysis_asset()
    if asset is None:
        await query.edit_message_text(
            f"{pe('cross')} No candles in the database yet.\n"
            "The backfill collects the last 5 days first.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    db = _db()
    today = _today_date_str()
    yesterday = (datetime.strptime(today, "%Y-%m-%d")
                 - timedelta(days=1)).strftime("%Y-%m-%d")
    all_dates = [d for d in db.get_dates() if d < today]
    if yesterday not in all_dates:
        await query.edit_message_text(
            f"{pe('cross')} No candles for {yesterday} yet — the backtest "
            "needs yesterday's full day. Try again tomorrow.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    train = [d for d in all_dates if d < yesterday][-2:]
    if not train:
        await query.edit_message_text(
            f"{pe('cross')} Not enough history yet — the backtest needs "
            "at least 2 days before yesterday.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    signals = generate_futures_signals_from_db(db, asset, train, "09:00", "10:59")
    lines = format_signals_list(signals)
    if not lines:
        await query.edit_message_text(
            f"{pe('chart')}{pe('diamond')} BACKTEST LAB — {display_asset(asset)} {yesterday} 09:00-10:59\n"
            f"{DIVIDER}\n"
            f"{pe('chart')} Filtered signals: 0\n"
            "The filter removed every signal for this window. Try another asset later.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    lookup = build_lookup(db.get_candles(asset, yesterday), asset)
    results, summary = check_signals(lines, lookup, yesterday, with_mtg1=False)
    text = format_check_results(results, summary, header=False)
    await query.edit_message_text(
        f"{pe('chart')}{pe('diamond')} BACKTEST LAB — {display_asset(asset)} {yesterday} 09:00-10:59\n"
        f"Signals built from {', '.join(train)} (majority vote), checked against {yesterday}.\n"
        f"{DIVIDER}\n{text}",
        reply_markup=back_keyboard(), parse_mode="HTML")


def _checker_list_prompt(date_str, mode="OTC"):
    """Prompt text for pasting the signal list (after mode + MTG +
    date). Blackout/whiteout lines carry no direction — the example
    shows that, and the hint says the checker reads the previous
    minute's candle for the direction."""
    mode_name = {"OTC": "OTC", "REAL": "Real Market",
                 "BLACKOUT": "Blackout", "WHITEOUT": "Whiteout"}.get(mode, "OTC")
    if mode in ("BLACKOUT", "WHITEOUT"):
        example = "USDARS-OTC,09:07 M1\nUSDARS-OTC,09:11 M1"
        hint = ("\nNo CALL/PUT needed — the checker reads the previous "
                "minute's candle for the direction.")
    else:
        example = "USDMXN-OTC,09:02 M1 CALL\nUSDMXN-OTC,09:11 M1 CALL"
        hint = ""
    return (f"{pe('search')}{pe('diamond')} Blue Fish Prime Check — {mode_name} — Date: {date_str}\n{DIVIDER}\n\n"
            "Paste your signal list, e.g.:\n" + example + hint)


def _checker_date_prompt():
    """Prompt text for typing the date to check."""
    return (f"{pe('search')}{pe('diamond')} Blue Fish Prime Check\n{DIVIDER}\n\n"
            "Which date do you want to check?\n"
            "Send it like 2026-11-25, or tap Today.")


async def _build_checker_lookup(lines, date_str):
    """Lookup {(asset, datetime): candle} for every asset in the
    pasted lines. DB first in db mode (today's candles included),
    then data/*.json cache. No Quotex login in db mode —
    get_candles_for() reads the DB there."""
    lookup = {}
    assets = set()
    for line in lines:
        parsed = parse_signal_line(line)
        if parsed:
            assets.add(parsed[0].replace("-", "_"))
    for asset in sorted(assets):
        try:
            candles, _src = await get_candles_for(asset, date_str)
        except Exception:
            candles = None
        if candles:
            lookup.update(build_lookup(candles, asset))
    return lookup


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flow = context.user_data.get("flow")
    text = update.message.text or ""
    user = update.effective_user
    # A typed message means the owner is not looking at the dashboard
    # any more — stop its refresh (the owner, 2026-11-27).
    _cancel_admin_dash(user.id)

    # Channel gate: members only (same as /start and the menu).
    if not await _is_channel_member(context.bot, user.id):
        await update.message.reply_text(
            _join_gate_text(), reply_markup=_join_keyboard(), parse_mode="HTML")
        return
    # Every text message counts as activity for the Admin Dashboard's
    # 'Online' count (last_seen is touched inside get_or_create_user).
    database.get_or_create_user(user.id, user.username or "")

    if flow == "futures_pct":
        # The user typed the win-rate % (70-100) for the FUTURES
        # SIGNAL filter. Invalid input asks again, keeping the flow.
        pct = _parse_fs_pct(text)
        if pct is None:
            await update.message.reply_text(
                f"{pe('warning')} That is not a win rate from 70 to 100. "
                "Send a whole number like 80.",
                reply_markup=back_keyboard(), parse_mode="HTML")
            return  # keep the flow so the user can retry
        context.user_data["fs_pct"] = pct
        context.user_data["flow"] = "futures_time"
        await update.message.reply_text(
            _futures_time_text(),
            reply_markup=_futures_time_keyboard(), parse_mode="HTML")
        return

    if flow == "futures_custom_time":
        parsed = parse_custom_time(text)
        if parsed is None:
            await update.message.reply_text(
                f"{pe('warning')} That time is not valid. Please send start and end time like this:\n"
                "09:00 10:59\nor 09:00-10:59\n\nEnd time must be after start time.",
                reply_markup=back_keyboard(), parse_mode="HTML")
            return  # keep the flow so the user can retry
        start, end = parsed
        context.user_data["flow"] = None
        step1 = ("Reading database..." if config.DATA_MODE == "db"
                 else "Connecting to Quotex...")
        working = await update.message.reply_text(
            f"{pe('hourglass')} {step1} {pe('diamond')} {start}-{end}",
            parse_mode="HTML")
        try:
            await asyncio.sleep(0.5)
            await working.edit_text(
                f"{pe('refresh')} Fetching candles... {pe('chart')} {start}-{end}",
                parse_mode="HTML")
            await asyncio.sleep(0.5)
            await working.edit_text(
                f"{pe('lightning')} Generating signals... {pe('chart')} {start}-{end}",
                parse_mode="HTML")
            await asyncio.sleep(0.5)
        except Exception:
            pass
        result = await _compute_futures_text(context, start, end)
        await working.edit_text(result, reply_markup=_refresh_keyboard(),
                                parse_mode=_html_mode(result))
        _store_result_spec(context, working.message_id,
                           ("fs", context.user_data.get("fs_asset", "USDMXN_otc"),
                            context.user_data.get("fs_days", 1),
                            context.user_data.get("fs_mode", "OTC FS"),
                            context.user_data.get("fs_pct", 70),
                            start, end))
        return

    if flow == "checker_date":
        # The user typed the date to check (db mode only).
        date_str = parse_checker_date(text)
        if not date_str:
            await update.message.reply_text(
                "That date does not look right — send it like 2026-11-25.",
                reply_markup=back_keyboard(), parse_mode="HTML")
            return
        if date_str > _today_date_str():
            await update.message.reply_text(
                "That date is in the future — send today or an earlier date.",
                reply_markup=back_keyboard(), parse_mode="HTML")
            return
        try:
            have_dates = candles_db.get_candle_db().get_dates()
        except Exception:
            have_dates = []
        if have_dates and date_str not in have_dates:
            await update.message.reply_text(
                f"No candles for {date_str} in the database — "
                "send another date.",
                reply_markup=back_keyboard(), parse_mode="HTML")
            return
        context.user_data["checker_date"] = date_str
        context.user_data["flow"] = "checker_list"
        await update.message.reply_text(
            _checker_list_prompt(date_str,
                                 context.user_data.get("checker_mode", "OTC")),
            reply_markup=back_keyboard(), parse_mode="HTML")
        return

    if flow == "checker_list":
        working = await update.message.reply_text(
            f"{pe('hourglass')} Checking signals... {pe('search')}", parse_mode="HTML")
        try:
            await asyncio.sleep(0.6)
        except Exception:
            pass
        lines = [x for x in text.splitlines() if x.strip()]
        check_date = context.user_data.get("checker_date") or _today_date_str()
        with_mtg1 = context.user_data.get("checker_mtg1", False)
        mode = context.user_data.get("checker_mode", "OTC")
        lookup = await _build_checker_lookup(lines, check_date)
        results, summary = check_signals(lines, lookup, check_date,
                                         with_mtg1=with_mtg1, mode=mode)
        await working.edit_text(
            format_check_results(results, summary, date_str=check_date,
                                 mode=mode),
            reply_markup=back_keyboard(), parse_mode="HTML")
        context.user_data["flow"] = None
        return

    if flow == "filter_aston":
        context.user_data["aston_lines"] = text.splitlines()
        context.user_data["flow"] = "filter_newexe"
        await update.message.reply_text(
            f"{pe('sparkle')} Now send the Newexe list.", parse_mode="HTML")
        return

    if flow == "filter_newexe":
        aston = context.user_data.get("aston_lines", [])
        final, counts = filter_personal(aston, text.splitlines())
        await update.message.reply_text(
            f"{pe('chart')}{pe('diamond')} Filter Results:\n{DIVIDER}\n\n"
            + format_filter_result(final, counts),
            reply_markup=back_keyboard(), parse_mode="HTML")
        context.user_data["flow"] = None
        return

    if flow == "formatter":
        formatted = format_signal_list(text)
        await update.message.reply_text(
            f"{pe('sparkle')}{pe('diamond')} Formatter — standard format:\n"
            f"{html.escape(formatted)}",
            reply_markup=back_keyboard(), parse_mode="HTML")
        context.user_data["flow"] = None
        return

    if flow == "swap":
        await update.message.reply_text(
            f"{pe('refresh')} Swap C/P:\n{html.escape(swap_call_put(text))}",
            reply_markup=back_keyboard(), parse_mode="HTML")
        context.user_data["flow"] = None
        return

    if flow == "timezone":
        hhmm = text.strip()[:5]
        await update.message.reply_text(
            f"{pe('pin')} {html.escape(hhmm)} UTC+6 → UTC+0: "
            f"{html.escape(convert_timezone(hhmm, 6, 0))}",
            reply_markup=back_keyboard(), parse_mode="HTML")
        context.user_data["flow"] = None
        return

    if flow == "font":
        await update.message.reply_text(
            f"{pe('sparkle')} Bold:\n{html.escape(text_font(text, 'bold'))}",
            reply_markup=back_keyboard(), parse_mode="HTML")
        context.user_data["flow"] = None
        return

    if flow == "premium_txn":
        req_id = database.save_premium_request(user.id, text.strip())
        await update.message.reply_text(
            f"{pe('check')}{pe('diamond')} Transaction ID saved. The owner will check and approve it, "
            f"then your 30-day Premium starts. {pe('crown')}",
            reply_markup=back_keyboard(), parse_mode="HTML")
        for admin_id in config.ADMIN_USER_IDS:
            try:
                kb = InlineKeyboardMarkup([[
                    _btn("Approve", f"premium_approve_{req_id}",
                         icon="approve"),
                    _btn("Reject", f"premium_reject_{req_id}",
                         icon="reject")]])
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(f"Premium request #{req_id} from "
                          f"@{user.username or user.id} (id {user.id}):\n"
                          f"{html.escape(text.strip())}"),
                    reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
        context.user_data["flow"] = None
        return

    # No active flow: show menu.
    await update.message.reply_text(
        f"Tap a button in the menu. {pe('diamond')}",
        reply_markup=main_keyboard(admin=_is_admin(user.id)), parse_mode="HTML")
