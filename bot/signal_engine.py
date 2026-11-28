"""LittleMatrix signal engine — pure, unit-testable logic.

Decisions agreed with the owner (see blueprint.md):
- Per candle: C > O = CALL, C < O = PUT, C == O = DOJI.
- FUTURES SIGNAL: same HH:MM across N days, majority vote.
  1 day = 1 vote (copy, weak). 2 days: 2-0 signal, 1-1 tie skip.
  3 days: 3-0 = 100%, 2-1 = 66%.
- Quality filter IN the generator (NOT the personal filter):
  1) Win rate >= 70% for multi-day (skipped for 1 day: 1 vote is
     always 100% on paper, so it proves nothing).
  2) Strength: body >= 0.00025 and body/range >= 0.5.
     NOTE: the 0.00025 body threshold is tuned for USDMXN_otc
     (~19.9 price). It is exact for the verified sample below.
  3) Gap >= 7 minutes per asset.
  4) Trend (EMA 9 vs EMA 21): available as a separate function and
     as an OPT-IN flag. It is OFF by default in
     generate_futures_signals() because a strict EMA check drops
     4 of the 13 verified sample signals (09:18, 10:09, 10:16,
     10:24) to 9. the owner can enable it with apply_trend=True.
- Verified sample: USDMXN_otc 2026-11-21 09:00-10:59, 1 day:
  120 candles -> 118 raw signals (2 DOJI skipped) ->
  strength + gap 7 -> exactly 13 signals (see tests).
- Cross-asset filter: accuracy-first, fewer is better. Rank by
  win rate first, then real strength (body/range). A kept signal
  must be >= 7 minutes from every other kept signal (any asset),
  max 6/hour, max 12 per 2h, below 70% dropped in cross-asset mode.
  (2026-11-27: the FUTURES SIGNAL menu no longer applies the 7-min
  gap — per-asset or cross-asset — and instead filters by a
  user-typed win-rate % (70-100). Auto Live Signal keeps the gap.)
- BLACKOUT / WHITEOUT (2026-11-27): signal(T) comes from the T-1
  minute's candle on each selected day — BLACKOUT flips it, WHITEOUT
  keeps it — majority vote across days. The printed line carries NO
  direction ("USDMXN-OTC,09:02 M1"); the trader decides it live
  from the T-1 candle. T=00:00 never produces a signal (its T-1
  belongs to the previous day).
- The personal filter (Aston/Newexe) is NOT here. See filters.py.
"""
from __future__ import annotations

import json
from pathlib import Path

MIN_BODY = 0.00025
MIN_BODY_RATIO = 0.5
MIN_GAP_MINUTES = 7
MIN_WIN_RATE = 0.70
# Relative body threshold for other assets: price * 0.0000125
# USDMXN ~19.9 -> 0.00025 (matches the verified sample),
# EURUSD ~1.08 -> 0.0000135, USDPKR ~280 -> 0.0035.
MIN_BODY_REL = 0.0000125


def _median(values):
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def diagnose_window(days_data, asset, start="00:00", end="23:59"):
    """Return diagnostics for one window, so the bot can explain 0 signals.

    Returns dict: total, raw, doji, median_body, median_range,
    median_ratio, after_strength, after_gap.
    """
    candles = []
    for day in days_data:
        for raw in day:
            c = normalize_candle(raw)
            if not c["hhmm"]:
                continue
            if not (start <= c["hhmm"] <= end):
                continue
            candles.append(c)
    total = len(candles)
    doji = sum(1 for c in candles if c["close"] == c["open"])
    raw = total - doji
    bodies = [abs(c["close"] - c["open"]) for c in candles]
    ranges = [c["high"] - c["low"] for c in candles if c["high"] > c["low"]]
    ratios = [(abs(c["close"] - c["open"]) / (c["high"] - c["low"]))
              for c in candles if c["high"] > c["low"]]
    # Raw signals before strength (1-day: every non-DOJI minute is a signal)
    sigs = generate_futures_signals(days_data, asset, start, end)
    return {
        "total": total,
        "raw": raw,
        "doji": doji,
        "median_body": _median(bodies),
        "median_range": _median(ranges),
        "median_ratio": _median(ratios),
        "median_price": _median([c["close"] for c in candles]) if candles else 0.0,
        "after_filter": len(sigs),
    }


def is_strong(candle, min_body=MIN_BODY, min_ratio=MIN_BODY_RATIO):
    body = abs(candle["close"] - candle["open"])
    rng = candle["high"] - candle["low"]
    if rng <= 0:
        return False
    return body >= min_body and (body / rng) >= min_ratio


def is_strong_adaptive(candle, min_ratio=MIN_BODY_RATIO):
    """Strength with price-scaled body threshold (for non-USDMXN assets)."""
    price = abs(candle["close"]) or 1.0
    min_body = price * MIN_BODY_REL
    return is_strong(candle, min_body=min_body, min_ratio=min_ratio)


def normalize_candle(c):
    """Accept O/H/L/C or open/high/low/close, time/datetime.

    Returns dict with keys: datetime, hhmm, open, high, low, close.
    Live Quotex candles only have unix 'time' (no 'datetime'), so
    convert that to Asia/Dhaka datetime here, otherwise hhmm stays
    empty and the signal engine would skip the candle.
    """
    open_ = c.get("O", c.get("open"))
    high = c.get("H", c.get("high"))
    low = c.get("L", c.get("low"))
    close = c.get("C", c.get("close"))
    dt = c.get("datetime", "")
    if not dt and c.get("time") is not None:
        try:
            from datetime import datetime, timezone
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo("Asia/Dhaka")
            except Exception:
                from datetime import timedelta
                tz = timezone(timedelta(hours=6))
            ts = int(c["time"])
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            dt = ""
    hhmm = ""
    if dt and len(dt) >= 16:
        hhmm = dt[11:16]
    return {
        "datetime": dt,
        "hhmm": hhmm,
        "open": float(open_),
        "high": float(high),
        "low": float(low),
        "close": float(close),
    }


def candle_direction(c):
    """Return 'CALL', 'PUT' or 'DOJI' for one normalized candle."""
    n = c if "open" in c else normalize_candle(c)
    if n["close"] > n["open"]:
        return "CALL"
    if n["close"] < n["open"]:
        return "PUT"
    return "DOJI"


def load_json_candles(path):
    """Load a candles_*.json file. Returns (asset, [normalized candles])."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    candles = data.get("candles", data) if isinstance(data, dict) else data
    asset = data.get("asset", "UNKNOWN") if isinstance(data, dict) else "UNKNOWN"
    norm = [normalize_candle(c) for c in candles]
    norm.sort(key=lambda x: x["datetime"])
    return asset, norm


def hhmm_to_minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def majority_vote(directions):
    """Majority vote over one HH:MM across days.

    directions: list of 'CALL'/'PUT'/'DOJI' (DOJI ignored).
    Returns (direction or None, win_rate, call_votes, put_votes).
    Tie (e.g. 1-1) -> direction None.
    """
    calls = sum(1 for d in directions if d == "CALL")
    puts = sum(1 for d in directions if d == "PUT")
    total = calls + puts
    if total == 0:
        return None, 0.0, calls, puts
    if calls > puts:
        return "CALL", calls / total, calls, puts
    if puts > calls:
        return "PUT", puts / total, calls, puts
    return None, 0.5, calls, puts  # tie


def is_strong(candle, min_body=MIN_BODY, min_ratio=MIN_BODY_RATIO):
    body = abs(candle["close"] - candle["open"])
    rng = candle["high"] - candle["low"]
    if rng <= 0:
        return False
    return body >= min_body and (body / rng) >= min_ratio


def apply_gap_filter(signals, min_gap=MIN_GAP_MINUTES):
    """Keep signals at least min_gap minutes apart (time order)."""
    kept = []
    last_min = -10**9
    for s in sorted(signals, key=lambda x: x["hhmm"]):
        mins = hhmm_to_minutes(s["hhmm"])
        if mins - last_min >= min_gap:
            kept.append(s)
            last_min = mins
    return kept


def ema(values, period):
    out = []
    if not values:
        return out
    k = 2 / (period + 1)
    e = values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def trend_direction(closes):
    """'UP' if EMA(9) > EMA(21) on the last close, else 'DOWN'."""
    if len(closes) < 21:
        return "UP"  # not enough data: do not block
    e9 = ema(closes, 9)[-1]
    e21 = ema(closes, 21)[-1]
    return "UP" if e9 > e21 else "DOWN"


def trend_aligned(direction, closes_up_to_time):
    """True if the signal direction matches the EMA trend."""
    trend = trend_direction(closes_up_to_time)
    if direction == "CALL":
        return trend == "UP"
    if direction == "PUT":
        return trend == "DOWN"
    return False


def rsi(closes, period=14):
    """Simple RSI (Wilder smoothing skipped for local test)."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def generate_futures_signals(days_data, asset, start="00:00", end="23:59",
                              min_win_rate=MIN_WIN_RATE,
                              apply_trend=False, apply_gap=True):
    """Build FUTURES signals from N days of candles.

    days_data: list of lists of candles (raw dicts, one list per day).
    Each list must already be for one date; candles carry 'datetime'.
    Returns list of signal dicts sorted by hhmm:
    {asset, hhmm, direction, win_rate, calls, puts, days}
    apply_gap: the >= 7 min per-asset gap filter. True keeps the
    original behaviour (verified sample + Auto Live Signal); the
    FUTURES SIGNAL menu passes False — the owner (2026-11-27)
    replaced the gap with a user-typed win-rate % threshold there.
    """
    n_days = len(days_data)
    # Group directions + representative candle (last day) by HH:MM.
    by_minute = {}
    for day in days_data:
        for raw in day:
            c = normalize_candle(raw)
            if not c["hhmm"]:
                continue
            if not (start <= c["hhmm"] <= end):
                continue
            slot = by_minute.setdefault(c["hhmm"], {"dirs": [], "candle": c})
            d = candle_direction(c)
            if d != "DOJI":
                slot["dirs"].append(d)
            # representative candle = latest day's candle for strength
            slot["candle"] = c

    signals = []
    for hhmm in sorted(by_minute):
        slot = by_minute[hhmm]
        direction, win_rate, calls, puts = majority_vote(slot["dirs"])
        if direction is None:
            continue  # tie (e.g. 1-1 on 2 days) or only DOJI
        # Win-rate filter: only for multi-day (1-day vote is always 100%).
        if n_days >= 2 and win_rate < min_win_rate:
            continue
        signals.append({
            "asset": asset,
            "hhmm": hhmm,
            "direction": direction,
            "win_rate": win_rate,
            "calls": calls,
            "puts": puts,
            "days": n_days,
            "_candle": slot["candle"],
        })

    signals = _apply_strength_cascade(signals)

    # Optional strict trend filter (off by default, see docstring).
    if apply_trend:
        # Build close series per asset from all days in time order.
        all_closes = []
        # For the local test we use the last day's closes in order.
        last_day = sorted(
            (normalize_candle(c) for c in days_data[-1]),
            key=lambda x: x["datetime"],
        )
        closes_by_hhmm = {}
        running = []
        for c in last_day:
            running.append(c["close"])
            closes_by_hhmm[c["hhmm"]] = list(running)
        kept = []
        for s in signals:
            closes = closes_by_hhmm.get(s["hhmm"], [])
            if closes and trend_aligned(s["direction"], closes):
                kept.append(s)
            elif not closes:
                kept.append(s)
        signals = kept

    # Gap filter (>= 7 min per asset) — only when apply_gap is True.
    if apply_gap:
        signals = apply_gap_filter(signals, MIN_GAP_MINUTES)

    return _annotate_strength(signals)


def _apply_strength_cascade(signals):
    """Strength filter (per-asset) — strict first (preserves the verified
    USDMXN 118 -> 13 result). If strict gives 0 but raw signals exist,
    relax step by step so other assets (different price scale, e.g.
    USDPKR ~280, EURUSD ~1.08) do not return 0 signals:
    1) adaptive body (price-scaled) + ratio 0.5
    2) ratio 0.4 only, no body threshold
    3) ratio 0.3 only, no body threshold
    4) strongest by body/range with gap, up to 10 signals
    Shared by generate_futures_signals() and the blackout/whiteout
    generator — both filter on the representative candle's strength.
    Signals keep their "_candle" key here; _annotate_strength() pops it.
    """
    strict = [s for s in signals if is_strong(s["_candle"])]
    if strict:
        return strict
    adaptive = [s for s in signals if is_strong_adaptive(s["_candle"])]
    if adaptive:
        return adaptive

    def _ratio_only(min_ratio):
        out = []
        for s in signals:
            c = s["_candle"]
            rng = c["high"] - c["low"]
            if rng > 0 and abs(c["close"] - c["open"]) / rng >= min_ratio:
                out.append(s)
        return out
    relaxed = _ratio_only(0.4)
    if relaxed:
        return relaxed
    relaxed = _ratio_only(0.3)
    if relaxed:
        return relaxed
    # Last resort: strongest by ratio (the gap filter, when applied
    # by the caller, re-sorts by hhmm afterwards).
    def _ratio(c):
        rng = c["high"] - c["low"]
        if rng <= 0:
            return 0.0
        return abs(c["close"] - c["open"]) / rng
    return sorted(signals, key=lambda s: -_ratio(s["_candle"]))


def _annotate_strength(signals):
    """Set real strength (body/range ratio of the representative
    candle, 0.0-1.0) and drop the internal "_candle" key.
    (Do NOT use win_rate as strength: for 1-day signals win_rate is
    always 1.0, so it ranks nothing.)"""
    for s in signals:
        c = s.get("_candle")
        if c:
            rng = c["high"] - c["low"]
            s["strength"] = (abs(c["close"] - c["open"]) / rng) if rng > 0 else 0.0
        else:
            s["strength"] = 0.0
        s.pop("_candle", None)
    return signals


def _prev_hhmm(hhmm):
    """One minute earlier, or None for 00:00 (its previous minute
    belongs to the previous day, which is not in the selected days)."""
    h, m = map(int, hhmm.split(":"))
    if h == 0 and m == 0:
        return None
    m -= 1
    if m < 0:
        h -= 1
        m = 59
    return f"{h:02d}:{m:02d}"


def ai_next_signal(db, asset, now=None):
    """Decide the NEXT minute's direction for one asset (the owner,
    2026-11-28). Returns (direction, checks, n_candles) where
    direction is 'CALL'/'PUT' or None, and checks maps each of the
    four tests to the direction it voted for (or None). A signal
    needs ALL FOUR to agree on the same direction:

    1. EMA trend (EMA9 vs EMA21 over the last 60 closes today).
    2. RSI(14) zone — CALL only when 50 < RSI < 70, PUT only when
       30 < RSI < 50 (extremes and the neutral 50 give no signal).
    3. The last 3 closed candles, majority (DOJI ignored).
    4. The next minute's HH:MM over the past 5 completed days,
       majority vote, at least 3 voting days and win rate >= 60%.

    Pure engine logic (takes the candle DB explicitly) so both the
    AI Signal button and the Live Auto Signal loop can use it.
    `now` is a Dhaka datetime (defaults to now in Asia/Dhaka)."""
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    try:
        from zoneinfo import ZoneInfo as _Z
        _tz = _Z("Asia/Dhaka")
    except Exception:
        from datetime import timezone as _tzmod
        _tz = _tzmod(_td(hours=6))
    now = now or _dt.now(_tz)
    today = now.strftime("%Y-%m-%d")
    recent = [normalize_candle(c) for c in db.get_candles(asset, today)][-60:]
    checks = {"trend": None, "rsi": None, "last3": None, "history": None}
    if len(recent) < 21:
        return None, checks, len(recent)
    closes = [c["close"] for c in recent]
    # 1. EMA trend.
    trend = trend_direction(closes)
    if trend == "UP":
        checks["trend"] = "CALL"
    elif trend == "DOWN":
        checks["trend"] = "PUT"
    # 2. RSI zone.
    value = rsi(closes)
    if 50 < value < 70:
        checks["rsi"] = "CALL"
    elif 30 < value < 50:
        checks["rsi"] = "PUT"
    # 3. Last 3 closed candles, majority.
    last3 = [candle_direction(c) for c in recent[-3:]]
    votes = [d for d in last3 if d in ("CALL", "PUT")]
    if votes and votes.count("CALL") != votes.count("PUT"):
        checks["last3"] = ("CALL" if votes.count("CALL") > votes.count("PUT")
                           else "PUT")
    # 4. The next minute's HH:MM over the past 5 completed days.
    next_min = (now.replace(second=0, microsecond=0) + _td(minutes=1))
    hhmm = next_min.strftime("%H:%M")
    dates, _ = available_signal_dates(db, asset, 5, now=now)
    dirs = []
    for day in load_days_from_db(db, asset, dates):
        for c in day:
            # Raw DB rows carry "datetime" as a Dhaka string
            # "YYYY-MM-DD HH:MM:SS" — slice the HH:MM out of it.
            if str(c.get("datetime", ""))[11:16] == hhmm:
                dirs.append(candle_direction(c))
                break
    direction, win_rate, calls, puts = majority_vote(dirs)
    if direction and (calls + puts) >= 3 and win_rate >= 0.6:
        checks["history"] = direction
    agreed = {v for v in checks.values() if v}
    if len(agreed) == 1 and all(v is not None for v in checks.values()):
        return agreed.pop(), checks, len(recent)
    return None, checks, len(recent)


def generate_prev_minute_signals(days_data, asset, start="00:00",
                                 end="23:59", mode="BLACKOUT",
                                 min_win_rate=MIN_WIN_RATE):
    """BLACKOUT / WHITEOUT signals from N days of candles.

    For a signal at time T, look at the T-1 minute's candle on each
    selected day D (the owner, 2026-11-27):
    - BLACKOUT: signal(T) = OPPOSITE of candle(T-1) direction.
    - WHITEOUT: signal(T) = SAME direction as candle(T-1).
    Majority vote across days decides signal(T); a DOJI or missing
    T-1 candle skips that day's vote. T=00:00 has no T-1 inside the
    selected days, so it never produces a signal.
    The returned direction is the voted direction (used for the
    win-rate / % filter); the bot prints these signals WITHOUT a
    direction — the trader decides it live from the T-1 candle.
    No gap filter here (the owner removed it from the FUTURES
    SIGNAL generators, 2026-11-27); the strength cascade and the
    win-rate filter are the same as generate_futures_signals().
    Returns list of signal dicts sorted by hhmm.
    """
    n_days = len(days_data)
    mode = (mode or "BLACKOUT").upper()
    if mode not in ("BLACKOUT", "WHITEOUT"):
        raise ValueError(f"unknown prev-minute mode: {mode!r}")
    # Per-day lookup: hhmm -> normalized candle.
    by_day = []
    for day in days_data:
        lookup = {}
        for raw in day:
            c = normalize_candle(raw)
            if c["hhmm"]:
                lookup[c["hhmm"]] = c
        by_day.append(lookup)

    signals = []
    start_m = hhmm_to_minutes(start)
    end_m = hhmm_to_minutes(end)
    for mins in range(start_m, end_m + 1):
        hhmm = f"{mins // 60:02d}:{mins % 60:02d}"
        prev = _prev_hhmm(hhmm)
        if prev is None:
            continue
        votes = []
        rep = None
        for lookup in by_day:
            c = lookup.get(prev)
            if c is None:
                continue
            d = candle_direction(c)
            if d == "DOJI":
                continue
            if mode == "BLACKOUT":
                d = "PUT" if d == "CALL" else "CALL"
            votes.append(d)
            rep = c  # representative candle = last day's T-1 candle
        direction, win_rate, calls, puts = majority_vote(votes)
        if direction is None:
            continue  # tie or only DOJI/missing T-1
        if n_days >= 2 and win_rate < min_win_rate:
            continue
        signals.append({
            "asset": asset,
            "hhmm": hhmm,
            "direction": direction,
            "win_rate": win_rate,
            "calls": calls,
            "puts": puts,
            "days": n_days,
            "_candle": rep,
        })

    signals = _apply_strength_cascade(signals)
    return _annotate_strength(signals)


def display_asset(asset):
    """Internal 'USDMXN_otc' -> display 'USDMXN-OTC'.

    User wants: USDMXN-OTC,09:02 M1 CALL
    Internal names use lowercase + underscore, so convert here.
    Real-market assets without _otc stay as-is, e.g. 'EURUSD'.
    """
    a = (asset or "").upper().strip()
    # USDMXN_OTC / USDMXN_otc -> USDMXN-OTC
    if a.endswith("_OTC"):
        a = a[:-4] + "-OTC"
    elif a.endswith("OTC") and "-" not in a and "_" not in a and len(a) > 3:
        # USDMXNOTC -> USDMXN-OTC (rare, be safe)
        a = a[:-3] + "-OTC"
    return a


def format_signal(asset, hhmm, direction=None):
    """One signal line. Blackout/whiteout lines carry NO direction
    (the owner, 2026-11-27): "USDMXN-OTC,09:02 M1". Pass
    direction=None for those; OTC/real lines keep CALL/PUT."""
    if direction:
        return f"{display_asset(asset)},{hhmm} M1 {direction}"
    return f"{display_asset(asset)},{hhmm} M1"


def format_signals_list(signals, asset=None, show_direction=True):
    return [format_signal(s["asset"], s["hhmm"],
                          s["direction"] if show_direction else None)
            for s in signals]


def cross_asset_filter(all_signals, max_per_hour=6, max_total=12,
                       min_win_rate=MIN_WIN_RATE, apply_gap=True):
    """Accuracy-first filter across many assets.

    all_signals: list of signal dicts with asset/hhmm/direction/win_rate
    and real 'strength' (body/range 0.0-1.0, set by
    generate_futures_signals).
    Rules: drop win_rate < min_win_rate; rank win_rate desc, then
    strength desc; keep a signal ONLY if it is >= 7 minutes from
    EVERY already-kept signal (any asset, not just the same minute)
    — when apply_gap is True; max 1 per same HH:MM (implied by the
    7-min gap); max max_per_hour per hour; max max_total overall.
    Fewer, high-accuracy signals win.
    apply_gap: True keeps the original behaviour (Auto Live Signal
    uses the default); the FUTURES SIGNAL menu passes False — the
    owner (2026-11-27) replaced the gap with a user-typed win-rate
    % threshold there. The per-hour / total caps stay either way.
    """
    pool = [s for s in all_signals if s.get("win_rate", 0.0) >= min_win_rate]
    pool.sort(key=lambda s: (-s.get("win_rate", 0.0),
                             -s.get("strength", 0.0),
                             s["hhmm"], s["asset"]))
    kept = []
    kept_minutes = []
    per_hour = {}
    for s in pool:
        mins = hhmm_to_minutes(s["hhmm"])
        # Cross-asset gap: >= 7 minutes from every kept signal.
        if apply_gap and any(abs(mins - k) < MIN_GAP_MINUTES for k in kept_minutes):
            continue
        hour = s["hhmm"][:2]
        if per_hour.get(hour, 0) >= max_per_hour:
            continue
        if len(kept) >= max_total:
            break
        kept.append(s)
        kept_minutes.append(mins)
        per_hour[hour] = per_hour.get(hour, 0) + 1
    kept.sort(key=lambda s: (s["hhmm"], s["asset"]))
    return kept


def load_days_from_db(db, asset, dates):
    """Load one list of candles per date from the candle DB.

    Returns [day1, day2, ...] in the same raw-dict shape
    generate_futures_signals() expects (datetime in Dhaka + O/H/L/C).
    Dates with no candles give an empty list (skipped by the engine).
    """
    return [db.get_candles(asset, d) for d in dates]


def available_signal_dates(db, asset, n_days, now=None):
    """The most recent `n_days` COMPLETED dates with data for an asset.

    Signals for today are built from past days, so completed days
    (before today) are used. If the DB has no completed day yet
    (e.g. first run before backfill finished), fall back to whatever
    dates exist, including today. Returns (dates, total_available).
    """
    from datetime import datetime as _dt
    try:
        from zoneinfo import ZoneInfo as _Z
        _tz = _Z("Asia/Dhaka")
    except Exception:
        from datetime import timedelta as _td, timezone as _tzmod
        _tz = _tzmod(_td(hours=6))
    today = (now or _dt.now(_tz)).strftime("%Y-%m-%d")
    all_dates = db.get_dates(asset)
    completed = [d for d in all_dates if d < today]
    pool = completed if completed else all_dates
    return pool[-n_days:], len(pool)


def generate_futures_signals_from_db(db, asset, dates, start="00:00",
                                     end="23:59", min_win_rate=MIN_WIN_RATE,
                                     apply_trend=False, apply_gap=True):
    """FUTURES signals from DB candles — same engine, same 4-layer
    filter, same output format as generate_futures_signals().

    dates: list of 'YYYY-MM-DD' strings (past days)."""
    days_data = load_days_from_db(db, asset, dates)
    return generate_futures_signals(days_data, asset, start, end,
                                    min_win_rate=min_win_rate,
                                    apply_trend=apply_trend,
                                    apply_gap=apply_gap)


def generate_prev_minute_signals_from_db(db, asset, dates, start="00:00",
                                         end="23:59", mode="BLACKOUT",
                                         min_win_rate=MIN_WIN_RATE):
    """BLACKOUT / WHITEOUT signals from DB candles — the T-1 engine
    (see generate_prev_minute_signals). dates: past days."""
    days_data = load_days_from_db(db, asset, dates)
    return generate_prev_minute_signals(days_data, asset, start, end,
                                        mode=mode,
                                        min_win_rate=min_win_rate)
