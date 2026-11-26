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


def _collector_task_done(task):
    """Log a dead collector task instead of losing it silently.

    A task that raises only prints "Task exception was never retrieved"
    when garbage-collected — easy to miss in Render logs — while the
    bot keeps answering Telegram, so a dead collector just looks like
    a frozen Bot Status. This makes the death loud.
    """
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("collector task died — Bot Status will freeze until restart")



async def post_init(app):
    """Runs after the Application is built, before polling/webhook
    starts. Starts the background candle collector (db mode only).
    The collector is a background asyncio task — it never blocks
    the bot from answering Telegram."""
    if config.collector_active():
        logger.info("collector enabled (DATA_MODE=db) — starting background task")
        task = asyncio.create_task(collector.collector_loop())
        task.add_done_callback(_collector_task_done)
        app.bot_data["collector_task"] = task
    else:
        logger.info("collector not active (DATA_MODE=%s, COLLECTOR_ENABLED=%s)",
                    config.DATA_MODE, config.COLLECTOR_ENABLED)


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
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
    from bot.handlers import menu_callback, start, text_message

    if not config.has_telegram_token():
        logger.warning("BOT_TOKEN is not set. Copy .env.example to .env and set it.")
    app = (Application.builder()
           .token(config.BOT_TOKEN or "PLACEHOLDER")
           .post_init(post_init)
           .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback))
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
