"""Verify the webhook startup path on Python 3.14/3.15 (Render failure).

Reproduces the exact Render startup that crashed on 2026-11-25:
WEBHOOK_URL set, DATA_MODE=db, COLLECTOR_ENABLED=true, real PTB
run_webhook, mocked Telegram API (getMe/setWebhook succeed without
network) and a fake Quotex session that SUCCEEDS (returns the bundled
sample candles) so the collector backfill runs concurrently with the
webhook bootstrap — the interleaving that died on Render with:

    telegram.error.NetworkError: Unknown error in HTTP implementation:
    NoEventLoopError('Not currently running on any asynchronous event
    loop. Available async backends: asyncio, trio')

Run (FAKE token, no real login, temp SQLite DB):

    timeout -s TERM 25 python tests/verify_webhook_startup.py

SIGTERM makes PTB shut down cleanly and main() return 0.
Expected verdict: PASS — "Application started", webhook port
listening, backfill inserted candles, NO NoEventLoopError anywhere.
If NoEventLoopError appears (or the app never starts), FAIL, exit 1.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from pathlib import Path

# --- Fake credentials (never the real .env values) ---
os.environ["BOT_TOKEN"] = "FAKE_TOKEN_FOR_TEST_123456"
os.environ["WEBHOOK_URL"] = "https://blue-fish-prime-bot.onrender.com"
os.environ["DATA_MODE"] = "db"
os.environ["COLLECTOR_ENABLED"] = "true"
os.environ.pop("DATABASE_URL", None)
os.environ["PORT"] = "18099"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# --- Capture every log record: look for NoEventLoopError + "started" ---
seen = {"no_loop_error": False, "app_started": False, "backfill_done": False}


class WatchHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return
        if "NoEventLoopError" in msg:
            seen["no_loop_error"] = True
        if "Application started" in msg:
            seen["app_started"] = True
        if "backfill finished" in msg:
            seen["backfill_done"] = True


logging.getLogger().addHandler(WatchHandler())
# NOTE: adding a handler to root before main.py runs makes main's
# logging.basicConfig() a no-op, so set the level explicitly here —
# otherwise INFO records ("Application started", "backfill finished")
# are filtered out before they reach this handler.
logging.getLogger().setLevel(logging.INFO)

# --- Mock the Telegram API (no network) ---
from telegram.request import _httpxrequest  # noqa: E402


async def fake_do_request(self, url, method, request_data=None, **kwargs):
    if "getMe" in url:
        payload = {"id": 123456789, "is_bot": True, "first_name": "Test",
                   "username": "testbot"}
    else:
        payload = True
    body = json.dumps({"ok": True, "result": payload}).encode()
    return 200, body


_httpxrequest.HTTPXRequest.do_request = fake_do_request

# --- Fake Quotex session that SUCCEEDS (synthetic candles) ---
from bot import candles_db as candles_db_mod  # noqa: E402
from bot import collector as collector_mod  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from synthetic_data import make_day, to_raw  # noqa: E402

# Sample day = YESTERDAY, always inside the 5-day backfill window.
# (A fixed date drifts out of the window as days pass — that is why
# this test failed on 2026-11-27 with the old 2026-11-21 sample:
# the backfill filtered every candle out as too old.)
from datetime import date as _date, timedelta as _tdelta  # noqa: E402

SAMPLE_DATE = (_date.today() - _tdelta(days=1)).isoformat()
SAMPLE = to_raw(make_day("USDMXN_otc", SAMPLE_DATE, seed=8))


class FakeSession:
    def __init__(self):
        self.connected = False

    async def connect(self):
        self.connected = True
        return True

    async def get_open_assets(self):
        return ["USDMXN_otc"]

    async def fetch_asset_span(self, asset, seconds_back):
        return list(SAMPLE)

    async def close(self):
        self.connected = False


collector_mod.SharedQuotexSession = FakeSession

# --- Temp SQLite DB (never touches data/candles.db) ---
tmpdir = tempfile.mkdtemp(prefix="verify-webhook-")
db = candles_db_mod.CandleDB(
    database_url="", sqlite_path=Path(tmpdir) / "candles.db").init()
candles_db_mod.get_candle_db = lambda: db


# --- Poll the webhook port from a thread (proves Tornado is listening) ---
port_up = {"ok": False}


def _probe_port():
    for _ in range(30):
        time.sleep(0.5)
        try:
            urllib.request.urlopen("http://127.0.0.1:18099/webhook",
                                   timeout=2)
            port_up["ok"] = True
            return
        except urllib.error.HTTPError:
            # Any HTTP response (404/405) proves the server is listening.
            port_up["ok"] = True
            return
        except Exception:
            continue


threading.Thread(target=_probe_port, daemon=True).start()

import main as botmain  # noqa: E402

print("VERIFY: webhook startup on Python", sys.version.split()[0],
      "(fake token, mocked Telegram, fake Quotex backfill)", flush=True)
tb_text = ""
try:
    code = botmain.main()
    print("VERIFY: main() returned", code, flush=True)
except BaseException:
    tb_text = traceback.format_exc()
    print(tb_text, flush=True)
    if "NoEventLoopError" in tb_text:
        seen["no_loop_error"] = True

candles = db.count_total()
print(f"VERIFY: app_started={seen['app_started']} "
      f"port_listening={port_up['ok']} "
      f"backfill_done={seen['backfill_done']} candles_in_db={candles} "
      f"no_loop_error={seen['no_loop_error']}", flush=True)

ok = (seen["app_started"] and port_up["ok"] and candles > 0
      and not seen["no_loop_error"] and "NoEventLoopError" not in tb_text)
print("VERIFY:", "PASS — webhook startup works, no NoEventLoopError"
      if ok else "FAIL — see lines above", flush=True)
sys.exit(0 if ok else 1)
