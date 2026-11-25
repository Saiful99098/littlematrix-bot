"""Collect yesterday's 1-day candles for assets YOU give, save as JSON.

Usage on Windows PC (in the littlematrix-bot-local folder):
    python collect_yesterday.py --assets USDMXN_otc EURUSD_otc GBPUSD_otc
    python collect_yesterday.py --assets USDPKR_otc
    python collect_yesterday.py --assets USDMXN_otc EURUSD_otc --days 1

- Login comes from .env (QUOTEX_EMAIL / QUOTEX_PASSWORD), never typed here.
- One get_historical_candles() call per asset (2 days = 172800s, period 60,
  max_workers 5), filtered to yesterday, 3 assets at a time,
  1s pause between batches (anti-429). 2 days is required: 1 day only
  reaches back to the same time yesterday, so yesterday 00:00 would be missed.
- Saves data/candles_<asset>_<date>.json with time + Dhaka datetime
  + H/L/O/C per candle. The bot reads these files first, so after
  collecting once, FUTURES SIGNAL works without logging in again.
- 1 day = up to 1439 candles (current candle excluded).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass

import os

DEFAULT_ASSETS = ["USDMXN_otc", "EURUSD_otc", "GBPUSD_otc",
                  "USDPKR_otc", "AUDCAD_otc", "EURJPY_otc"]


def dhaka_now():
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Dhaka")
    except Exception:
        tz = timezone(timedelta(hours=6))
    return datetime.now(tz)


def to_raw(candles):
    seen = {}
    for c in candles or []:
        if not isinstance(c, dict) or "time" not in c:
            continue
        ts = int(c["time"])
        if ts in seen:
            continue
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Asia/Dhaka")
        except Exception:
            tz = timezone(timedelta(hours=6))
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
        seen[ts] = {"time": ts, "datetime": dt, "H": c.get("high"),
                    "L": c.get("low"), "O": c.get("open"), "C": c.get("close")}
    return [seen[ts] for ts in sorted(seen)]


async def fetch_one(client, asset, days):
    # Fetch 2 days back to cover a full yesterday, then filter.
    # days=1 means "yesterday" (fetch 2 days, filter to yesterday).
    fetch_days = max(2, days + 1) if days == 1 else days
    candles = await client.get_historical_candles(
        asset=asset,
        amount_of_seconds=fetch_days * 86400,
        period=60,
        max_workers=5,
    )
    return to_raw(candles)


async def main_async(assets, days):
    from pyquotex.stable_api import Quotex

    email = os.getenv("QUOTEX_EMAIL", "")
    password = os.getenv("QUOTEX_PASSWORD", "")
    if not email or not password:
        print("ERROR: .env-এ QUOTEX_EMAIL / QUOTEX_PASSWORD বসানো নেই।")
        return 1

    client = Quotex(email=email, password=password)
    check, message = await client.connect()
    if not check:
        print(f"Quotex login failed: {message}")
        try:
            await client.close()
        except Exception:
            pass
        return 1

    yesterday = (dhaka_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    data_dir = BASE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    try:
        for i in range(0, len(assets), 3):
            batch = assets[i:i + 3]
            for asset in batch:
                print(f"Fetching {asset} ...", flush=True)
                try:
                    raw = await fetch_one(client, asset, days)
                except Exception as exc:
                    if "429" in str(exc):
                        print(f"  429 — Quotex blocked. 15-30 min wait করুন, আর চেষ্টা করবেন না।")
                        return 1
                    print(f"  Failed: {exc}")
                    continue
                if not raw:
                    print(f"  No candles for {asset}")
                    continue
                # Filter to yesterday only (fetch was 2 days back).
                raw_y = [c for c in raw if c.get("datetime", "").startswith(yesterday)]
                if raw_y:
                    raw = raw_y
                print(f"  Yesterday {yesterday}: {len(raw)} candles", flush=True)
                last_date = raw[-1]["datetime"][:10]
                # Save under yesterday (and last date if different).
                for d in {last_date, yesterday}:
                    path = data_dir / f"candles_{asset}_{d}.json"
                    # Only save yesterday's file with yesterday's candles.
                    save_raw = [c for c in raw if c.get("datetime", "").startswith(d)] or raw
                    payload = {"asset": asset, "period": 60, "count": len(save_raw), "candles": save_raw}
                    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                print(f"  Saved {len(raw)} candles, {raw[0]['datetime']} to {raw[-1]['datetime']}")
            if i + 3 < len(assets):
                time.sleep(1)
    finally:
        try:
            await client.close()
        except Exception:
            pass
    print("Done. Bot-এ FUTURES SIGNAL চালালে এই ফাইলগুলো থেকেই সিগন্যাল আসবে।")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Collect yesterday candles for given assets")
    parser.add_argument("--assets", nargs="+", default=DEFAULT_ASSETS,
                        help="Asset list, e.g. USDMXN_otc EURUSD_otc")
    parser.add_argument("--days", type=int, default=1, help="Days to fetch (default 1)")
    args = parser.parse_args()
    return asyncio.run(main_async(args.assets, args.days))


if __name__ == "__main__":
    raise SystemExit(main())
