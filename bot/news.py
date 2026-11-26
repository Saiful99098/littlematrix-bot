"""News Desk — ForexFactory economic calendar (free, no API key).

Source: https://nfs.faireconomy.media/ff_calendar_thisweek.xml
(the data host behind ForexFactory; the main site is Cloudflare
protected, this host answers a plain request). One-hour in-memory
cache, so a tap never fetches on every press. Times in the feed are
US Eastern; they are converted to Dhaka (EST +11h in winter,
EDT +10h in summer, US DST rules).
"""
from __future__ import annotations

import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

logger = logging.getLogger("littlematrix.news")

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
_CACHE_TTL = 3600  # seconds
_cache = {"ts": 0.0, "events": None}


def _us_dst_active(date):
    """True when US Eastern is on daylight time (EDT) for `date`.

    DST starts the second Sunday of March, ends the first Sunday
    of November.
    """
    def _nth_sunday(year, month, n):
        d = datetime(year, month, 1)
        # first Sunday
        while d.weekday() != 6:
            d += timedelta(days=1)
        return d + timedelta(weeks=n - 1)

    start = _nth_sunday(date.year, 3, 2)
    end = _nth_sunday(date.year, 11, 1)
    return start.date() <= date <= end.date()


def _et_to_dhaka(date_str, time_str):
    """'MM-DD-YYYY' + '9:45pm' (US Eastern) -> 'YYYY-MM-DD HH:MM' Dhaka.
    Returns None for 'All Day' / 'Tentative' / unparsable times."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([aApP][mM])\s*$", time_str or "")
    if not m:
        return None
    try:
        d = datetime.strptime(date_str.strip(), "%m-%d-%Y")
    except ValueError:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    et = d.replace(hour=hour, minute=minute)
    offset = 10 if _us_dst_active(et.date()) else 11
    return (et + timedelta(hours=offset)).strftime("%Y-%m-%d %H:%M")


def fetch_calendar_events():
    """All events this week (cached 1h). Raises on network/parse error."""
    import time
    now = time.time()
    if _cache["events"] is not None and now - _cache["ts"] < _CACHE_TTL:
        return _cache["events"]
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    events = []
    for ev in root.findall("event"):
        def _text(tag):
            el = ev.find(tag)
            return (el.text or "").strip() if el is not None else ""
        date_str = _text("date")
        time_str = _text("time")
        dhaka = _et_to_dhaka(date_str, time_str)
        events.append({
            "title": _text("title"),
            "country": _text("country"),
            "date": date_str,
            "time": time_str,
            "impact": _text("impact"),
            "forecast": _text("forecast"),
            "previous": _text("previous"),
            "dhaka": dhaka,  # 'YYYY-MM-DD HH:MM' or None
        })
    _cache["ts"] = now
    _cache["events"] = events
    return events


def events_for_dhaka_dates(dates):
    """Events whose Dhaka date is in `dates` (set of 'YYYY-MM-DD'),
    High impact first, then by Dhaka time. Events with no usable
    time are matched by their feed date (US date)."""
    try:
        events = fetch_calendar_events()
    except Exception as exc:
        logger.warning("news: calendar fetch failed: %s", exc)
        return None
    out = []
    for ev in events:
        d = ev["dhaka"][:10] if ev["dhaka"] else None
        if d is None:
            # No time to convert: fall back to the feed date.
            try:
                d = datetime.strptime(ev["date"], "%m-%d-%Y").strftime("%Y-%m-%d")
            except ValueError:
                continue
        if d in dates:
            out.append(ev)
    impact_rank = {"High": 0, "Medium": 1, "Low": 2}
    out.sort(key=lambda e: (impact_rank.get(e["impact"], 3),
                            e["dhaka"] or "99:99"))
    return out
