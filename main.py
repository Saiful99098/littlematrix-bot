"""Blue Fish Prime Telegram bot — polling (local PC) or webhook (Render).

- Local PC: no WEBHOOK_URL in .env -> polling mode.
  Run: python main.py
- Render: WEBHOOK_URL is set (e.g. https://blue-fish-prime-bot.onrender.com)
  -> webhook mode: Telegram calls <WEBHOOK_URL>/webhook, the bot
  listens on 0.0.0.0:$PORT (Render gives PORT, default 10000).
  Needs python-telegram-bot[webhooks] (tornado) — see requirements.txt.
- In db mode (DATA_MODE=db + COLLECTOR_ENABLED=true) a background
  collector starts after the bot is built (post_init): it backfills
  the last 5 days of candles, then collects every 2 hours.
  No real token/secret is in this repo — only placeholders.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

from bot import collector, config

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("littlematrix")


def _ensure_event_loop():
    """Create + set an event loop in the main thread, BEFORE any
    telegram/httpx object is built.

    Root cause of the first failed Render deploy (2026-11-25):
    Render built with Python 3.14 instead of the pinned 3.12.7.
    Python 3.14 removed the implicit loop — asyncio.get_event_loop()
    now raises RuntimeError when no loop is set (CPython gh-126353).
    The first Telegram API call (getMe during initialize, setWebhook
    during the webhook bootstrap) goes through httpx -> httpcore ->
    anyio, and anyio raises NoEventLoopError when it cannot find a
    running loop in the current thread. PTB 22.8 does create a loop
    inside run_webhook()/run_polling(), but the HTTPXRequest client
    (httpx.AsyncClient) is built earlier, inside build_application()
    — and Tornado's IOLoop.current() also reads the loop at server
    construction. So the loop must already exist before
    build_application() runs. This is called there (and again in
    main() before run_webhook/run_polling, belt and suspenders).
    Safe to call twice; a no-op when a loop already exists.
    Works on 3.12, 3.14 and 3.15.
    """
    try:
        asyncio.get_event_loop()
        return
    except RuntimeError:
        pass
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)


def should_use_webhook():
    """Webhook mode when WEBHOOK_URL is set (Render)."""
    return config.use_webhook()


def webhook_config():
    """(listen, port, url_path, webhook_url) for run_webhook()."""
    return ("0.0.0.0", config.PORT, "webhook",
            config.WEBHOOK_URL.rstrip("/") + "/webhook")


_TASK_RESTART_DELAY_SECONDS = 60  # patched to ~0.05 in tests
_TASK_RESTART_LIMIT = 5  # max restarts within the window below
_TASK_RESTART_WINDOW_SECONDS = 600  # 10 minutes


def _restart_task(app, key, start_fn, label):
    """(Re)start a background task, re-attaching the death callback
    so it is supervised for the life of the bot."""
    task = asyncio.create_task(start_fn())
    task.add_done_callback(
        lambda t: _background_task_done(t, app, key, start_fn, label))
    app.bot_data[key] = task


def _background_task_done(task, app, key, start_fn, label):
    """A background task ended: log it loudly and restart it.

    The collector and the live stream are forever-loops that never
    raise by design — but if one ever does die (a bug, a bad
    deploy), the bot would keep answering Telegram while silently
    collecting nothing, and only the next deploy would revive it
    (the owner, 2026-11-27: candles stopped at 12:13 while the
    market stayed open). So a dead task restarts itself after a
    minute. A restart budget (5 deaths in 10 minutes) stops a
    tight crash loop: after that the task stays dead and the log
    says the bot needs a restart from Render.
    """
    try:
        task.result()
    except asyncio.CancelledError:
        return  # shutting down — do not restart
    except Exception:
        logger.exception("%s task died — restarting in %d seconds",
                         label, _TASK_RESTART_DELAY_SECONDS)
    else:
        logger.warning("%s task ended — restarting in %d seconds",
                       label, _TASK_RESTART_DELAY_SECONDS)
    restarts = app.bot_data.setdefault(key + "_restarts", [])
    now = time.monotonic()
    restarts[:] = [t for t in restarts
                   if now - t < _TASK_RESTART_WINDOW_SECONDS]
    if len(restarts) >= _TASK_RESTART_LIMIT:
        logger.error("%s task died %d times in 10 minutes — NOT "
                     "restarting, restart the bot from Render",
                     label, len(restarts) + 1)
        return
    restarts.append(now)
    asyncio.get_event_loop().call_later(
        _TASK_RESTART_DELAY_SECONDS, _restart_task, app, key, start_fn, label)


async def post_init(app):
    """Runs after the Application is built, before polling/webhook
    starts. Starts the background candle collector (db mode only).
    The collector is a background asyncio task — it never blocks
    the bot from answering Telegram."""
    if config.collector_active():
        logger.info("collector enabled (DATA_MODE=db) — starting background task")
        _restart_task(app, "collector_task",
                      lambda: collector.collector_loop(bot=app.bot), "collector")
    else:
        logger.info("collector not active (DATA_MODE=%s, COLLECTOR_ENABLED=%s)",
                    config.DATA_MODE, config.COLLECTOR_ENABLED)
    if config.live_stream_active():
        from bot import live_stream
        logger.info("live stream enabled — starting background task")
        _restart_task(app, "live_stream_task",
                      live_stream.live_stream_loop, "live stream")
    else:
        logger.info("live stream not active (DATA_MODE=%s, COLLECTOR_ENABLED=%s, "
                    "LIVE_STREAM_ENABLED=%s)",
                    config.DATA_MODE, config.COLLECTOR_ENABLED,
                    config.LIVE_STREAM_ENABLED)


async def channel_member_update(update, context):
    """Auto-detect a channel join: when a user joins the required
    channel, welcome them and unlock the bot. Fires only when the
    bot is an admin in that channel; the "I Joined" button is the
    fallback when it does not fire."""
    if not config.REQUIRED_CHANNEL:
        return
    member_update = update.chat_member
    if member_update is None:
        return
    chat = member_update.chat
    channel = (config.REQUIRED_CHANNEL or "").strip().lstrip("@")
    chat_matches = (str(chat.id) == channel
                    or (chat.username or "") == channel)
    if not chat_matches:
        return
    old = member_update.old_chat_member.status
    new = member_update.new_chat_member.status
    if old in ("left", "kicked") and new in ("member", "administrator",
                                             "creator", "restricted"):
        user = member_update.new_chat_member.user
        try:
            from bot.handlers import _is_admin, main_keyboard, pe as _pe
            await context.bot.send_message(
                chat_id=user.id,
                text=(f"{_pe('diamond')}{_pe('lightning')} Welcome to BLUE FISH PRIME! {_pe('fire')}\n"
                      "You joined the Blue Fish trading community — the tools are now unlocked.\n"
                      "Pick a tool below to start."),
                reply_markup=main_keyboard(admin=_is_admin(user.id)), parse_mode="HTML")
        except Exception:
            pass
        # MATRIX referral (the owner, 2026-11-27, Option B): when the
        # join came through a referral invite link, pay the link's
        # owner 5 coins — once per invited user, never for self-joins
        # or re-joins (enforced in database.record_referral). A
        # failure here must never break the welcome above.
        try:
            invite = getattr(member_update, "invite_link", None)
            link = getattr(invite, "invite_link", None) if invite else None
            if link:
                from bot import referral
                status, coins, referrer_id, balance = referral.attribute_join(
                    user.id, link)
                if status == "awarded" and referrer_id:
                    from bot.handlers import pe as _pe2
                    await context.bot.send_message(
                        chat_id=referrer_id,
                        text=(f"{_pe2('diamond')} A new member joined through your "
                              f"referral link — +{coins} MATRIX coins! "
                              f"Balance: {balance} coins."),
                        parse_mode="HTML")
        except Exception:
            pass


async def my_chat_member_update(update, context):
    """Log the channel id when the bot is added to a channel as admin.

    The membership gate needs the channel's numeric id (-100...) in
    REQUIRED_CHANNEL — a private channel has no @username, so the id
    cannot be guessed. When the owner adds the bot as admin, Telegram
    sends this update with the chat id; it is logged here so the owner
    can copy it from the Render logs into REQUIRED_CHANNEL.
    """
    member_update = update.my_chat_member
    if member_update is None:
        return
    chat = member_update.chat
    new = member_update.new_chat_member.status
    old = member_update.old_chat_member.status
    if chat.type == "channel" and old in ("left", "kicked") and new in (
            "administrator", "member"):
        logger.warning(
            "Bot added to channel '%s' (id: %s) — set this id as "
            "REQUIRED_CHANNEL to turn the membership gate on.",
            chat.title, chat.id)


def build_application():
    """Build the telegram Application (no polling started here).

    The event loop is ensured FIRST (see _ensure_event_loop):
    Application.builder().build() constructs the Bot and its
    HTTPXRequest client (httpx.AsyncClient) eagerly, and on
    Python 3.14 that construction — and Tornado's IOLoop.current()
    later in run_webhook — needs a loop to already exist in the
    main thread. Without this, the first Telegram POST failed on
    Render with anyio NoEventLoopError.
    """
    _ensure_event_loop()
    # Some environments set NO_PROXY with bracketed IPv6 entries that
    # httpx cannot parse (it raises InvalidURL on '[::1]'). Telegram
    # does not need the proxy list to build the client, so sanitise
    # it before constructing the Application. Safe on a normal PC.
    for _var in ("NO_PROXY", "no_proxy"):
        if _var in os.environ:
            os.environ[_var] = "localhost,127.0.0.1"
    from telegram.ext import (Application, CallbackQueryHandler,
                              ChatMemberHandler, CommandHandler,
                              MessageHandler, filters)
    from bot.handlers import menu_callback, start, text_message

    if not config.has_telegram_token():
        logger.warning("BOT_TOKEN is not set. Copy .env.example to .env and set it.")
    app = (Application.builder()
           .token(config.BOT_TOKEN or "PLACEHOLDER")
           .post_init(post_init)
           .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback))
    # Auto-detect channel joins (fires only when the bot is an admin
    # in the required channel — see channel_member_update).
    app.add_handler(ChatMemberHandler(channel_member_update,
                                      ChatMemberHandler.CHAT_MEMBER))
    # Log the channel id when the bot is added as admin — the owner
    # copies it into REQUIRED_CHANNEL (see my_chat_member_update).
    app.add_handler(ChatMemberHandler(my_chat_member_update,
                                      ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    return app


def main():
    if "--check" in sys.argv:
        app = build_application()
        print("OK: imports work, handlers registered:",
              len(app.handlers.get(0, [])))
        print("DATA_MODE:", config.DATA_MODE,
              "| COLLECTOR_ENABLED:", config.COLLECTOR_ENABLED,
              "| collector active:", config.collector_active(),
              "| token set:", config.has_telegram_token())
        if should_use_webhook():
            print("MODE: webhook (%s)" % webhook_config()[3])
        else:
            print("MODE: polling (local)")
        return 0
    if not config.has_telegram_token():
        print("ERROR: BOT_TOKEN is not set.")
        print("1) copy .env.example to .env")
        print("2) put your BotFather token in .env as BOT_TOKEN=...")
        print("3) run: python main.py")
        return 1
    app = build_application()
    _ensure_event_loop()
    if should_use_webhook():
        listen, port, url_path, webhook_url = webhook_config()
        logger.info("Blue Fish Prime bot starting (webhook, Render) — "
                    "listening %s:%d, webhook %s", listen, port, webhook_url)
        app.run_webhook(listen=listen, port=port, url_path=url_path,
                        webhook_url=webhook_url)
    else:
        logger.info("Blue Fish Prime bot starting (polling, local test)...")
        app.run_polling()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
