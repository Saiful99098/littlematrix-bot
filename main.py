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
    """Python 3.14/3.15 fix: asyncio.get_event_loop() no longer creates
    a loop when none exists, so python-telegram-bot's run_polling()
    crashes with 'There is no current event loop in thread MainThread'.
    Create and set one here, before run_polling() is called.
    Safe on Python 3.12 too.
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


async def post_init(app):
    """Runs after the Application is built, before polling/webhook
    starts. Starts the background candle collector (db mode only).
    The collector is a background asyncio task — it never blocks
    the bot from answering Telegram."""
    if config.collector_active():
        logger.info("collector enabled (DATA_MODE=db) — starting background task")
        task = asyncio.create_task(collector.collector_loop())
        app.bot_data["collector_task"] = task
    else:
        logger.info("collector not active (DATA_MODE=%s, COLLECTOR_ENABLED=%s)",
                    config.DATA_MODE, config.COLLECTOR_ENABLED)


def build_application():
    """Build the telegram Application (no polling started here)."""
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
