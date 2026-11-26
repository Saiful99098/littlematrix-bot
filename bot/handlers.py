"""Telegram handlers — Blue Fish Prime local-test bot (built by LittleMatrix) (polling mode).

All flows are state machines stored in context.user_data.
Data source:
- DATA_MODE=db (Render): signals + CHECKER read ONLY from the
  candle database (Postgres on Render, SQLite locally). A background
  collector fills the DB (5-day backfill on startup, then every
  2 hours). No Quotex login happens for signals or CHECKER.
- DATA_MODE=live + Quotex login in .env: fetch yesterday's 1-day
  candles live from Quotex with get_historical_candles() for ANY
  asset the user picks (3-at-a-time for SELECT ALL), cached to
  data/candles_<asset>_<date>.json so a second request does not
  log in again. See bot/quotex_client.py.
- Otherwise: yesterday's 1-day sample file
  (data/sample_candles_USDMXN_otc_2026-11-21.json, USDMXN_otc only).
"""
from __future__ import annotations

import asyncio
import html
import inspect
import re
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from . import candles_db, collector, config, database, quotex_client
from .checker import (build_lookup, check_signals, format_check_results,
                      parse_checker_date, parse_signal_line)
from .filters import filter_personal, format_filter_result
from .premium_emoji import BUTTON_ICONS, pe
from .signal_engine import (
    available_signal_dates,
    cross_asset_filter,
    diagnose_window,
    display_asset,
    ema,
    format_signals_list,
    generate_futures_signals,
    generate_futures_signals_from_db,
    load_days_from_db,
    load_json_candles,
    rsi,
    trend_direction,
)
from .text_tools import convert_timezone, format_signal_list, formatter, swap_call_put, text_font

SAMPLE_DATE = "2026-11-21"
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
    Button text is plain (no Unicode emoji): Saiful wants premium-only
    buttons, so the premium icon is the button's only emoji.
    style: 'primary' only for the Premium button, 'success' only for
    SELECT ALL; every other button uses no style (clean look)."""
    kwargs = {}
    if icon and _BTN_SUPPORTS_ICON:
        kwargs["icon_custom_emoji_id"] = BUTTON_ICONS[icon]
    if style and _BTN_SUPPORTS_STYLE:
        kwargs["style"] = style
    return InlineKeyboardButton(text, callback_data=callback_data, **kwargs)


# Main menu: all 19 buttons carry a premium icon; their text is
# plain (premium-only, per Saiful). If an icon does not render
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
    [_btn("Bot Status", "menu_botstatus", icon="bot_status")],
    [_btn("Premium — 5 USDT", "menu_premium", icon="premium", style="primary")],
]


def main_keyboard():
    return InlineKeyboardMarkup(MAIN_MENU)


def back_keyboard():
    """Single Back button only — result messages must NOT carry the
    full main menu below a long list (the user had to scroll past
    everything to find the menu). The main menu stays as its own
    message above the results in chat history."""
    return InlineKeyboardMarkup(
        [[_btn("Back to Menu", "menu_main", icon="back")]])


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


def _asset_picker_keyboard(assets, page, mode):
    """Paginated asset picker: SELECT ALL on top, 2 assets per row,
    12 per page, Prev/Next navigation."""
    kb = [[_btn("SELECT ALL", "fs_asset_ALL", icon="select_all", style="success")]]
    total_pages = max(1, (len(assets) + ASSETS_PER_PAGE - 1) // ASSETS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    chunk = assets[page * ASSETS_PER_PAGE:(page + 1) * ASSETS_PER_PAGE]
    for i in range(0, len(chunk), 2):
        row = [InlineKeyboardButton(display_asset(a), callback_data=f"fs_asset_{a}")
               for a in chunk[i:i + 2]]
        kb.append(row)
    nav = []
    if page > 0:
        nav.append(_btn("Prev", f"fs_assets_page_{page - 1}", icon="back"))
    if page < total_pages - 1:
        nav.append(_btn("Next", f"fs_assets_page_{page + 1}", icon="next"))
    if nav:
        kb.append(nav)
    kb.append([_btn("Back", "menu_main", icon="back")])
    return InlineKeyboardMarkup(kb), page, total_pages


def _asset_picker_text(assets, page, total_pages, mode, live):
    if config.DATA_MODE == "db":
        src = (f"{pe('diamond')} Database assets ({len(assets)} for {mode}) — "
               "from the candle database, no Quotex login.")
    elif live:
        src = f"{pe('diamond')} Live open assets ({len(assets)} for {mode})"
    else:
        src = (f"{pe('diamond')} Sample mode — only these 6 assets here.\n"
               "Live mode shows every open asset from Quotex.")
    return (f"{pe('diamond')} BLUE FISH PRIME • CHOOSE ASSET\n{DIVIDER}\n"
            f"{src}\nPage {page + 1}/{total_pages}")


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


def load_sample():
    """Return (asset, candles) from the bundled sample file."""
    return load_json_candles(config.SAMPLE_CANDLES)


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
    """Live mode: yesterday (real date). Sample mode: 2026-11-21."""
    if config.DATA_MODE == "live" and config.has_quotex_login():
        return _yesterday_date_str()
    return SAMPLE_DATE


async def get_candles_for(asset, date_str):
    """Fetch candles for ANY asset (async — must be awaited).

    Order: candle DB first (DATA_MODE=db — no Quotex login) ->
    live Quotex (DATA_MODE=live + login, yesterday only) ->
    data/candles_<asset>_<date>.json if collected before ->
    sample file for USDMXN_otc.
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
    # 3) Sample fallback (USDMXN only).
    if asset in ("USDMXN_otc", "USDMXN-OTC"):
        _, candles = load_sample()
        # load_sample returns normalized candles; return raw-like form.
        raw = [{"datetime": c["datetime"], "O": c["open"], "H": c["high"],
                "L": c["low"], "C": c["close"]} for c in candles]
        return raw, "sample"
    return None, "no-data"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    database.get_or_create_user(user.id, user.username or "")
    text = (f"{pe('diamond')}{pe('lightning')} BLUE FISH PRIME — Trading Terminal {pe('sparkle')}\n"
            f"Created by LITTLE MATRIX {pe('crown')}\n"
            f"Pick a tool below to start. {pe('fire')}\n"
            f"{CONTACT_LINE}\n\n" + DISCLAIMER)
    if update.message:
        await update.message.reply_text(text, reply_markup=main_keyboard(),
                                        parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=main_keyboard(), parse_mode="HTML")


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
    database.get_or_create_user(user.id, user.username or "")

    if data == "menu_main":
        await query.edit_message_text(
            f"{pe('diamond')}{pe('lightning')} BLUE FISH PRIME — Trading Terminal {pe('sparkle')}\n"
            f"Pick a tool below. {pe('fire')}",
            reply_markup=main_keyboard(), parse_mode="HTML")
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
                "Press Bot Status to see the progress, then try again.",
                reply_markup=back_keyboard(), parse_mode="HTML")
            return
        # Live mode: show EVERY open asset from Quotex (get_all_assets).
        # Sample mode / fetch failure: fall back to the 6 sample assets.
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
                # Sample mode has no real-market assets; show the 6
                # sample assets instead of an empty picker.
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
        kb, page, total_pages = _asset_picker_keyboard(assets, page, mode)
        await query.edit_message_text(
            _asset_picker_text(assets, page, total_pages, mode, live),
            reply_markup=kb, parse_mode="HTML")
        return

    if data.startswith("fs_asset_"):
        context.user_data["fs_asset"] = data[len("fs_asset_"):]
        context.user_data["flow"] = "futures_days"
        if config.DATA_MODE == "db":
            kb = [[InlineKeyboardButton("1 Day", callback_data="fs_days_1"),
                   InlineKeyboardButton("2 Days", callback_data="fs_days_2"),
                   InlineKeyboardButton("3 Days", callback_data="fs_days_3"),
                   InlineKeyboardButton("5 Days", callback_data="fs_days_5")],
                  [_btn("Back", "menu_main", icon="back")]]
            await query.edit_message_text(
                f"{pe('diamond')} How many days of history should shape the signals? {pe('chart')}\n"
                "The database holds the last 5 full days (00:00-23:59 each day). "
                "Signals are built from those days at the same HH:MM.",
                reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
            return
        kb = [[InlineKeyboardButton("1 Day", callback_data="fs_days_1"),
               InlineKeyboardButton("2 Days", callback_data="fs_days_2"),
               InlineKeyboardButton("3 Days", callback_data="fs_days_3"),
               InlineKeyboardButton("10 Days", callback_data="fs_days_10")],
              [_btn("Back", "menu_main", icon="back")]]
        await query.edit_message_text(
            f"{pe('diamond')} How many days of history should shape the signals? {pe('chart')}\n"
            "Local test uses 1 day of sample data. Picking 2/3/10 days "
            "shows the 1-day result for now — multi-day voting needs "
            "the 20-day database (Render step).",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        return

    if data.startswith("fs_days_"):
        context.user_data["fs_days"] = int(data[len("fs_days_"):])
        context.user_data["flow"] = "futures_time"
        kb = [[InlineKeyboardButton("09:00-10:59", callback_data="fs_time_09:00-10:59"),
               InlineKeyboardButton("00:00-01:59", callback_data="fs_time_00:00-01:59")],
              [_btn("Custom Time", "fs_time_custom", icon="custom_time")],
              [_btn("Back", "menu_main", icon="back")]]
        await query.edit_message_text(
            f"{pe('pin')} Choose a time window (UTC+6) — or set your own:",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        return

    if data == "fs_time_custom":
        context.user_data["flow"] = "futures_custom_time"
        await query.edit_message_text(
            f"{pe('sparkle')} Custom Time {pe('pin')}\n\n"
            "Send the start and end time, e.g.:\n"
            "09:00 10:59\nor 09:00-10:59\n\nI will build the signals for that window only.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return

    if data.startswith("fs_time_"):
        start_end = data[len("fs_time_"):]
        start, end = start_end.split("-")
        await _run_futures(query, context, start, end)
        return

    if data == "menu_botstatus":
        await _run_bot_status(query)
        return
    if data == "menu_ai":
        await _run_ai_signal(query, context)
        return
    if data == "menu_checker":
        # Ask first: MTG1 results or non-MTG (M1 only)? The choice is
        # stored and used when the pasted list is checked. The date
        # comes next (db mode asks for it, other modes use the sample).
        kb = [[InlineKeyboardButton("MTG1", callback_data="checker_mtg_1"),
               InlineKeyboardButton("Non-MTG", callback_data="checker_mtg_0")],
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
            kb = [[InlineKeyboardButton("Today", callback_data="checker_date_today")],
                  [_btn("Back", "menu_main", icon="back")]]
            await query.edit_message_text(
                _checker_date_prompt(),
                reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        else:
            context.user_data["checker_date"] = SAMPLE_DATE
            context.user_data["flow"] = "checker_list"
            await query.edit_message_text(
                _checker_list_prompt(SAMPLE_DATE),
                reply_markup=back_keyboard(), parse_mode="HTML")
        return
    if data == "checker_date_today":
        today = _today_date_str()
        context.user_data["checker_date"] = today
        context.user_data["flow"] = "checker_list"
        await query.edit_message_text(
            _checker_list_prompt(today),
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    if data == "menu_market":
        await query.edit_message_text(
            f"{pe('diamond')} Market Live {pe('chart')}\n"
            "Live market status needs a Quotex login.\n"
            "Sample mode: USDMXN_otc — data available (2026-11-21).\n\n" + DISCLAIMER,
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    if data == "menu_otc":
        await _run_otc_board(query, context)
        return
    if data == "menu_payouts":
        await query.edit_message_text(
            f"{pe('money')} Live Payouts {pe('dollar')}\n"
            "Live payouts need a Quotex login.\n"
            "In live mode payouts are cached for 60 seconds.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    if data == "menu_counter":
        await _run_counter(query)
        return
    if data == "menu_trend":
        await _run_trend(query)
        return
    if data == "menu_backtest":
        await _run_backtest(query)
        return
    if data == "menu_news":
        await query.edit_message_text(
            f"{pe('bell')}{pe('diamond')} Blue Fish Prime News\nNo outside news feed is connected yet. "
            "It will be added in the Render step.",
            reply_markup=back_keyboard(), parse_mode="HTML")
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
        await query.edit_message_text(
            f"{pe('bell')} Auto Live Signal\nOff in local test. It runs once the bot "
            "is live 24/7 on Render.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return
    if data == "menu_premium":
        context.user_data["flow"] = "premium_txn"
        await query.edit_message_text(
            f"{pe('diamond')}{pe('crown')} Blue Fish Prime Premium — 1 month, 5 USDT {pe('fire')}\n\n"
            "Send USDT and paste the txn hash here. Saiful will check and "
            "approve it, then your 30-day Premium starts.",
            reply_markup=back_keyboard(), parse_mode="HTML")
        return


async def _compute_futures_text_db(context, start, end):
    """FUTURES SIGNALS from the candle database (Render / db mode).

    No Quotex login anywhere in this function — every candle comes
    from the DB (backfill + 2-hourly collector). Single asset:
    majority vote over the last N completed days with data.
    SELECT ALL: the same for every asset in the DB for this mode,
    then the cross-asset filter (win rate + strength, >= 7 min gap
    between any two kept signals, max 6/hour, max 12 total)."""
    asset = context.user_data.get("fs_asset", "USDMXN_otc")
    days = context.user_data.get("fs_days", 1)
    mode = context.user_data.get("fs_mode", "OTC FS")
    db = _db()
    today = _today_date_str()
    note = ""

    if asset == "ALL":
        db_assets = _filter_assets_by_mode(db.get_assets(), mode)
        if not db_assets:
            return (f"{pe('cross')} No candles in the database yet.\n"
                    "The backfill collects the last 5 days first. "
                    "Press Bot Status to see the progress, then try again.")
        all_dates = db.get_dates()
        completed = [d for d in all_dates if d < today] or all_dates
        dates = completed[-days:]
        if len(dates) < days:
            note = (f"\n\n{pe('info')} You picked {days} days, but the database "
                    f"has only {len(dates)} day(s) so far — using {len(dates)} day(s).")
        all_signals = []
        for a in db_assets:
            all_signals.extend(
                generate_futures_signals_from_db(db, a, dates, start, end))
        if not all_signals:
            return (f"{pe('cross')} No signals found after the filter.\n"
                    f"Dates: {', '.join(dates)} | Time: {start}-{end}\n"
                    "Try another window, or wait for the next collection run.")
        kept = cross_asset_filter(all_signals)
        lines = format_signals_list(kept)
        header = (f"{pe('diamond')}{pe('lightning')} BLUE FISH PRIME • FUTURE SIGNALS — {mode} {pe('fire')}\n{DIVIDER}\n"
                  f"Assets: SELECT ALL ({len(db_assets)} assets) | Dates: {dates[0]} to {dates[-1]} | Days: {len(dates)} (database)\n"
                  f"Time: {start}-{end} | UTC+6 | M1 | Quotex | Source: db\n"
                  f"{pe('chart')} Signals: {len(lines)} (cross-asset, accuracy-first, min 7-min gap)\n\n")
        body = "\n".join(lines) if lines else "No signals found after the filter."
        return (header + body + f"\n\n{DISCLAIMER}" + note)[:4000]

    dates, _available = available_signal_dates(db, asset, days)
    if not dates:
        return (f"{pe('cross')} No candles for {display_asset(asset)} in the database yet.\n"
                "The backfill collects the last 5 days first. "
                "Press Bot Status to see the progress, then try again.")
    if len(dates) < days:
        note = (f"\n\n{pe('info')} You picked {days} days, but the database "
                f"has only {len(dates)} day(s) for this asset — using {len(dates)} day(s).")
    signals = generate_futures_signals_from_db(db, asset, dates, start, end)
    lines = format_signals_list(signals)
    diag_text = ""
    if not lines:
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
    header = (f"{pe('diamond')}{pe('lightning')} BLUE FISH PRIME • FUTURE SIGNALS — {mode} {pe('fire')}\n{DIVIDER}\n"
              f"Asset: {display_asset(asset)} | Dates: {dates[0]} to {dates[-1]} | Days: {len(dates)} (database)\n"
              f"Time: {start}-{end} | UTC+6 | M1 | Quotex | Source: db\n"
              f"{pe('chart')} Signals: {len(lines)}\n\n")
    body = "\n".join(lines) if lines else "No signals found after the filter." + diag_text
    return (header + body + f"\n\n{DISCLAIMER}" + note)[:4000]


async def _compute_futures_text(context, start, end):
    """Build the FUTURE SIGNALS result text (no Telegram UI here).

    Returns the full result text. Used by both the callback flow
    (_run_futures, which edits the tapped message) and the text flow
    (custom time, which edits a 'Working...' message)."""
    asset = context.user_data.get("fs_asset", "USDMXN_otc")
    days = context.user_data.get("fs_days", 1)
    mode = context.user_data.get("fs_mode", "OTC FS")
    note = ""
    eff_date = _effective_date()
    # db mode: everything comes from the candle database (no login).
    if config.DATA_MODE == "db":
        return await _compute_futures_text_db(context, start, end)
    # SELECT ALL: fetch the open assets for this mode (live), 3-at-a-time,
    # then the cross-asset filter (win rate + real strength + >= 7 min
    # gap between ANY two kept signals, max 6/hour, max 12 total).
    # In sample mode only USDMXN exists.
    if asset == "ALL":
        if config.DATA_MODE != "live" or not config.has_quotex_login():
            note = (f"\n\n{pe('info')} Sample mode has only 1 day of USDMXN_otc sample data. "
                    "Full cross-asset SELECT ALL needs live mode (Quotex login). "
                    "Showing the USDMXN result for now.")
            asset = "USDMXN_otc"
        else:
            assets = context.user_data.get("fs_assets") or config.SAMPLE_ASSETS
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
                return generate_futures_signals([day_cs], a, start, end)
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
            kept = cross_asset_filter(all_signals)
            lines = format_signals_list(kept)
            header = (f"{pe('diamond')}{pe('lightning')} BLUE FISH PRIME • FUTURE SIGNALS — {mode} {pe('fire')}\n{DIVIDER}\n"
                      f"Assets: SELECT ALL ({len(assets)} assets) | Date: {eff_date} | Days: 1 (live)\n"
                      f"Time: {start}-{end} | UTC+6 | M1 | Quotex | Source: live\n"
                      f"{pe('chart')} Signals: {len(lines)} (cross-asset, accuracy-first, min 7-min gap)\n\n")
            body = "\n".join(lines) if lines else "No signals found after the filter."
            text = header + body + f"\n\n{DISCLAIMER}"
            return text[:4000]
    candles, source = await get_candles_for(asset, eff_date)
    if candles is None:
        return (f"{pe('cross')} No data found: {html.escape(str(source))}\n"
                "Check the Quotex login in live mode, or put yesterday's JSON file "
                "for this asset in the data/ folder with collect_yesterday.py.\n"
                "If it says 'login failed', check the email/password in .env.")
    if days > 1:
        note += (f"\n\n{pe('info')} You picked {days} days, but local test has only 1 day of "
                 "data, so this is the 1-day result. "
                 "Multi-day voting (2-0 / 3-0 / 2-1) needs the 20-day database in the Render step.")
    # Filter candles to the requested date + window for the engine input.
    day_candles = [c for c in candles
                   if c.get("datetime", "").startswith(eff_date)]
    # Sample fallback: if live date has no candles but sample does, use sample date.
    if not day_candles and source == "sample":
        day_candles = [c for c in candles
                       if c.get("datetime", "").startswith(SAMPLE_DATE)]
        eff_date = SAMPLE_DATE
    signals = generate_futures_signals([day_candles], asset, start, end)
    lines = format_signals_list(signals)
    # If still 0, show diagnostics so the user can see WHY (not just "0").
    diag_text = ""
    if not lines:
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
    header = (f"{pe('diamond')}{pe('lightning')} BLUE FISH PRIME • FUTURE SIGNALS — {mode} {pe('fire')}\n{DIVIDER}\n"
              f"Asset: {display_asset(asset)} | Date: {eff_date} | Days: 1 (local)\n"
              f"Time: {start}-{end} | UTC+6 | M1 | Quotex | Source: {source}\n"
              f"{pe('chart')} Signals: {len(lines)}\n\n")
    body = "\n".join(lines) if lines else "No signals found after the filter." + diag_text
    text = header + body + f"\n\n{DISCLAIMER}" + note
    return text[:4000]


async def _run_futures(query, context, start, end):
    """Callback flow: animated progress, then the final result.
    Only a Back button is attached — the main menu stays above."""
    asset = context.user_data.get("fs_asset", "USDMXN_otc")
    days = context.user_data.get("fs_days", 1)
    step1 = ("Reading database..." if config.DATA_MODE == "db"
             else "Connecting to Quotex...")
    try:
        await query.edit_message_text(
            f"{pe('hourglass')} {step1} {pe('diamond')} {display_asset(asset)} | {days} day(s) | {start}-{end}",
            parse_mode="HTML")
        await asyncio.sleep(0.5)
        await query.edit_message_text(
            f"{pe('refresh')} Fetching candles... {pe('chart')} {display_asset(asset)} | {start}-{end}",
            parse_mode="HTML")
        await asyncio.sleep(0.5)
        await query.edit_message_text(
            f"{pe('lightning')} Generating signals... {pe('chart')} {display_asset(asset)} | {start}-{end}",
            parse_mode="HTML")
        await asyncio.sleep(0.5)
    except Exception:
        pass  # progress edits are cosmetic; never block the result
    text = await _compute_futures_text(context, start, end)
    await query.edit_message_text(text, reply_markup=back_keyboard(),
                                  parse_mode=_html_mode(text))


async def _run_bot_status(query):
    """Bot Status view: mode, database, days, per-asset candles,
    last/next collection, Quotex session, today's coverage.
    DB only — no Quotex login here."""
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
    text = (f"{pe('green')}{pe('diamond')} BLUE FISH PRIME • BOT STATUS {pe('sparkle')}\n{DIVIDER}\n"
            f"Mode: {mode_text}\n"
            f"Database: {db_text}\n"
            f"Days in DB: {dates_text}\n"
            f"Candles per asset:\n{per_asset}\n"
            f"Last collection: {s['last_collection'] or '—'}\n"
            f"Next collection: {s['next_collection'] or '—'}\n"
            f"Quotex session: {html.escape(str(s['quotex_session']))}\n"
            f"Last Quotex login: {s['quotex_last_login'] or '—'}\n"
            f"Today ({s['today_date']}) 00:00-now: {s['today_count']} candles\n"
            f"Time now (Dhaka): {s['now_dhaka']}")
    await query.edit_message_text(text, reply_markup=back_keyboard(),
                                  parse_mode="HTML")


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


async def _run_ai_signal(query, context):
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
    _, candles = load_sample()
    recent = candles[-60:]
    closes = [c["close"] for c in recent]
    last = recent[-1]
    direction = "CALL" if last["close"] > last["open"] else "PUT"
    trend = trend_direction(closes)
    value = rsi(closes)
    trend_emoji = pe("up") if trend == "UP" else pe("down")
    await query.edit_message_text(
        f"{pe('star')}{pe('diamond')} BLUE FISH PRIME • AI SIGNAL — USDMXN-OTC (sample) {pe('sparkle')}\n{DIVIDER}\n"
        f"Last: {last['datetime']} M1 {direction}\n"
        f"{trend_emoji} Trend (EMA9/21): {trend} | {pe('chart')} RSI(14): {value:.1f}\n"
        f"{pe('money')} Close: {last['close']} | Open: {last['open']}\n\n{DISCLAIMER}",
        reply_markup=back_keyboard(), parse_mode="HTML")


async def _run_counter(query):
    _, candles = load_sample()
    recent = candles[-10:]
    green = sum(1 for c in recent if c["close"] > c["open"])
    red = sum(1 for c in recent if c["close"] < c["open"])
    await query.edit_message_text(
        f"{pe('chart')}{pe('diamond')} CANDLE SCAN — USDMXN-OTC (last 10)\n{DIVIDER}\n{pe('green')} Green: {green} | {pe('red')} Red: {red}",
        reply_markup=back_keyboard(), parse_mode="HTML")


async def _run_trend(query):
    _, candles = load_sample()
    closes = [c["close"] for c in candles[-60:]]
    trend = trend_direction(closes)
    e9 = ema(closes, 9)[-1]
    e21 = ema(closes, 21)[-1]
    trend_emoji = pe("up") if trend == "UP" else pe("down")
    await query.edit_message_text(
        f"{trend_emoji}{pe('diamond')} TREND SCAN — USDMXN-OTC\n{DIVIDER}\nTrend: {trend}\nEMA9: {e9:.5f} | EMA21: {e21:.5f}",
        reply_markup=back_keyboard(), parse_mode="HTML")


async def _run_backtest(query):
    try:
        await query.edit_message_text(
            f"{pe('hourglass')} Running backtest... {pe('chart')}", parse_mode="HTML")
        await asyncio.sleep(0.6)
    except Exception:
        pass
    _, candles = load_sample()
    day = [c for c in candles if c.get("datetime", "").startswith(SAMPLE_DATE)]
    signals = generate_futures_signals([day], "USDMXN_otc", "09:00", "10:59")
    await query.edit_message_text(
        f"{pe('chart')}{pe('diamond')} BACKTEST LAB — USDMXN-OTC 2026-11-21 09:00-10:59 (sample)\n"
        f"{DIVIDER}\n"
        f"{pe('chart')} Filtered signals: {len(signals)}\n"
        "Note: this checks the same day's data against itself, it is not a real backtest. "
        "A real backtest needs the multi-day database (Render step).\n\n" + DISCLAIMER,
        reply_markup=back_keyboard(), parse_mode="HTML")


def _checker_list_prompt(date_str):
    """Prompt text for pasting the signal list (after MTG + date)."""
    return (f"{pe('search')}{pe('diamond')} Blue Fish Prime Check — Date: {date_str}\n{DIVIDER}\n\n"
            "Paste your signal list, e.g.:\n"
            "USDMXN-OTC,09:02 M1 CALL\nUSDMXN-OTC,09:11 M1 CALL")


def _checker_date_prompt():
    """Prompt text for typing the date to check."""
    return (f"{pe('search')}{pe('diamond')} Blue Fish Prime Check\n{DIVIDER}\n\n"
            "Which date do you want to check?\n"
            "Send it like 2026-11-25, or tap Today.")


async def _build_checker_lookup(lines, date_str):
    """Lookup {(asset, datetime): candle} for every asset in the
    pasted lines. DB first in db mode (today's candles included),
    then data/*.json cache, then the sample file. No Quotex login
    in db mode — get_candles_for() reads the DB there."""
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
    if not lookup:
        # Fall back to the sample file (USDMXN only) so the checker
        # still answers in sample mode / before the backfill finishes.
        try:
            _, candles = load_sample()
            lookup.update(build_lookup(candles, "USDMXN_otc"))
        except Exception:
            pass
    return lookup


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flow = context.user_data.get("flow")
    text = update.message.text or ""
    user = update.effective_user

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
        await working.edit_text(result, reply_markup=back_keyboard(),
                                parse_mode=_html_mode(result))
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
            _checker_list_prompt(date_str),
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
        check_date = context.user_data.get("checker_date") or SAMPLE_DATE
        with_mtg1 = context.user_data.get("checker_mtg1", False)
        lookup = await _build_checker_lookup(lines, check_date)
        results, summary = check_signals(lines, lookup, check_date,
                                         with_mtg1=with_mtg1)
        await working.edit_text(
            f"{pe('search')}{pe('diamond')} Check Results:\n{DIVIDER}\n\n"
            + format_check_results(results, summary),
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
        database.save_premium_request(user.id, text.strip())
        await update.message.reply_text(
            f"{pe('check')}{pe('diamond')} Txn hash saved. Saiful will check and approve it, "
            f"then your 30-day Premium starts. {pe('crown')}",
            reply_markup=back_keyboard(), parse_mode="HTML")
        context.user_data["flow"] = None
        return

    # No active flow: show menu.
    await update.message.reply_text(
        f"Tap a button in the menu. {pe('diamond')}",
        reply_markup=main_keyboard(), parse_mode="HTML")
