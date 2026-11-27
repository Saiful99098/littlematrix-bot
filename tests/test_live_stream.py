"""Live stream + analysis pickers + premium-emoji audit.

Run: python tests/test_live_stream.py

a) The stream subscribes to every open asset, stores each closed
   1-min candle from the 'candle-generated' events, and survives a
   disconnect by reconnecting + tail-filling the gap.
b) AI Signal / Candle Scan / Trend Scan / Market Live ask the user
   which market first (asset picker), and run for the picked asset.
c) Every button in every keyboard carries a premium icon — the only
   raw InlineKeyboardButton constructions left are the two helpers
   (_btn / _url_btn) themselves.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config
from bot.candles_db import CandleDB, DHAKA
from bot import live_stream


class _StopLoop(BaseException):
    """Fake sleep raises this after N calls — it must propagate out
    of live_stream_loop untouched (BaseException, not Exception)."""


class _CountingSleep:
    def __init__(self, max_calls):
        self.calls = 0
        self.max_calls = max_calls

    async def __call__(self, _seconds):
        self.calls += 1
        if self.calls > self.max_calls:
            raise _StopLoop


EVENT_TS = int(datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc).timestamp())
TAIL_TS = EVENT_TS + 60


class FakeSession:
    """Fake Quotex session: scripted open assets, generated-candle
    events and connection drops. No login, no network.

    event_dict overrides the emitted event (e.g. one without 'open'
    for the seed test); tail_candles overrides the tail-fill rows
    ([] disables the tail-fill so the stream is tested alone).
    """

    def __init__(self, event_from_poll=1, connected_polls=None,
                 event_dict=None, tail_candles="default", ticks=None):
        self.connects = 0
        self.subscribed = []
        self.closed = 0
        self.polls = 0
        self.event_from_poll = event_from_poll
        # connected_polls=None -> stays connected; a number -> the
        # poll count after which is_connected() returns False.
        self.connected_polls = connected_polls
        self.event_dict = event_dict
        self.tail_candles = tail_candles
        # ticks: {asset: [{"time": unix-seconds, "price": float}]}
        # returned by generated_ticks() every poll (default: none).
        self.ticks = ticks or {}

    async def connect(self):
        self.connects += 1
        return True

    async def get_open_assets(self):
        return ["BTCUSD_otc", "ETHUSD_otc"]

    async def start_asset_streams(self, assets, period=60):
        self.subscribed.append((list(assets), period))

    def generated_candles(self):
        if self.polls < self.event_from_poll:
            return {}
        ev = self.event_dict if self.event_dict is not None else {
            "asset": "BTCUSD_otc", "period": 60, "index": EVENT_TS,
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}
        return {"BTCUSD_otc": {60: ev}}

    def generated_ticks(self):
        return {a: list(v) for a, v in self.ticks.items() if v}

    async def is_connected(self):
        self.polls += 1
        if self.connected_polls is None:
            return True
        return self.polls <= self.connected_polls

    async def fetch_asset_span(self, asset, seconds_back):
        if self.tail_candles != "default":
            return list(self.tail_candles)
        # Tail-fill: one candle, the minute after the stream event.
        return [{"time": TAIL_TS, "open": 100.5, "high": 101.5,
                 "low": 100.0, "close": 101.0}]

    async def close(self):
        self.closed += 1
        return None


def _tmp_db():
    tmp = tempfile.TemporaryDirectory()
    return CandleDB(database_url="", sqlite_path=Path(tmp.name) / "c.db").init(), tmp


def _candles_for(db, asset, date_str):
    return db.get_candles(asset, date_str)


class _RealSleep:
    """Counting sleep that also yields real time, so a background
    task (the tail-fill) gets to run and finish before the loop
    stops — the plain _CountingSleep never yields, so the background
    task would be cancelled before it stores anything. Used only by
    the tests that assert a tail-fill candle reached the DB."""

    def __init__(self, max_calls, delay=1.0):
        self.calls = 0
        self.max_calls = max_calls
        self.delay = delay

    async def __call__(self, seconds):
        self.calls += 1
        if self.calls > self.max_calls:
            raise _StopLoop
        await asyncio.sleep(self.delay)


def test_stream_stores_generated_candles():
    db, tmp = _tmp_db()
    session = FakeSession(event_from_poll=2)
    try:
        asyncio.run(live_stream.live_stream_loop(
            db=db, session_factory=lambda: session,
            sleep=_RealSleep(max_calls=5)))
    except _StopLoop:
        pass
    else:
        raise AssertionError("loop ended without reaching the fake sleep")
    # Subscribed to every open asset, 1-min period.
    assert session.subscribed, session.subscribed
    assets, period = session.subscribed[0]
    assert assets == ["BTCUSD_otc", "ETHUSD_otc"], assets
    assert period == 60, period
    # The generated candle (12:00) AND the tail-fill candle (12:01)
    # are both in the DB.
    rows = _candles_for(db, "BTCUSD_otc", "2026-11-26")
    by_time = {r["time"]: r for r in rows}
    assert EVENT_TS in by_time, rows
    assert by_time[EVENT_TS]["C"] == 100.5, rows
    assert TAIL_TS in by_time, rows
    assert by_time[TAIL_TS]["C"] == 101.0, rows
    print("PASS a) stream stores generated candles for all open assets")


def test_stream_reconnects_and_tail_fills():
    db, tmp = _tmp_db()
    # Connected for 2 polls, then the connection drops.
    session = FakeSession(event_from_poll=1, connected_polls=2)
    try:
        asyncio.run(live_stream.live_stream_loop(
            db=db, session_factory=lambda: session,
            sleep=_RealSleep(max_calls=6)))
    except _StopLoop:
        pass
    else:
        raise AssertionError("loop ended without reaching the fake sleep")
    # Reconnected at least once (the loop keeps going after a drop).
    assert session.connects >= 2, session.connects
    assert len(session.subscribed) >= 2, session.subscribed
    # The gap was tail-filled: the 12:01 candle is in the DB, once.
    rows = _candles_for(db, "BTCUSD_otc", "2026-11-26")
    assert [r["time"] for r in rows].count(TAIL_TS) == 1, rows
    print("PASS b) stream reconnects after a drop and tail-fills the gap")


def _load_today(db, asset, n=60):
    """n 1-min candles today (09:00-09:59 Dhaka) for one asset."""
    import bot.handlers as handlers
    today = handlers._today_date_str()
    d = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=DHAKA)
    rows = []
    price = 100.0
    for i in range(n):
        price += 0.1 if i % 2 == 0 else -0.05
        rows.append({"ts": d + timedelta(hours=9, minutes=i),
                     "open": price - 0.05, "high": price + 0.1,
                     "low": price - 0.1, "close": price})
    return db.insert_candles(asset, rows)


class FakeQuery:
    def __init__(self):
        self.texts = []

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.texts.append((text, reply_markup))
        return None


def test_analysis_pickers_use_picked_asset():
    import bot.candles_db as candles_db_mod
    import bot.handlers as handlers

    db, tmp = _tmp_db()
    _load_today(db, "BTCUSD_otc")
    _load_today(db, "ETHUSD_otc")
    real_get_db = candles_db_mod.get_candle_db
    real_mode = config.DATA_MODE
    try:
        candles_db_mod.get_candle_db = lambda: db
        config.DATA_MODE = "db"
        # Picker lists OTC assets first, sorted, with an icon on
        # every button.
        assets = handlers._analysis_assets()
        assert assets == ["BTCUSD_otc", "ETHUSD_otc"], assets
        kb, page, total = handlers._analysis_picker_keyboard(assets, 0, "ai")
        assert total == 1 and page == 0
        buttons = [b for row in kb.inline_keyboard for b in row]
        assert any(b.callback_data == "an_asset_ai_ETHUSD_otc" for b in buttons)
        assert all(getattr(b, "icon_custom_emoji_id", None) for b in buttons), \
            [(b.text, b.callback_data) for b in buttons]
        # Each tool runs for the PICKED asset, not the default.
        q = FakeQuery()
        asyncio.run(handlers._run_ai_signal(q, object(), "ETHUSD_otc"))
        assert "AI SIGNAL — ETHUSD-OTC" in q.texts[-1][0], q.texts[-1][0]
        q = FakeQuery()
        asyncio.run(handlers._run_counter(q, "ETHUSD_otc"))
        assert "CANDLE SCAN — ETHUSD-OTC" in q.texts[-1][0], q.texts[-1][0]
        q = FakeQuery()
        asyncio.run(handlers._run_trend(q, "ETHUSD_otc"))
        assert "TREND SCAN — ETHUSD-OTC" in q.texts[-1][0], q.texts[-1][0]
        q = FakeQuery()
        asyncio.run(handlers._run_market(q, "ETHUSD_otc"))
        assert "Market Live — ETHUSD-OTC" in q.texts[-1][0], q.texts[-1][0]
        assert "Today: 60 candles" in q.texts[-1][0], q.texts[-1][0]
    finally:
        candles_db_mod.get_candle_db = real_get_db
        config.DATA_MODE = real_mode
    print("PASS c) analysis pickers run the picked market, not the default")


def test_bot_status_times_are_dhaka_and_stream_shown():
    """Bot Status times are Dhaka, not raw UTC: a stored 05:48 UTC
    last collection is 11:48 Dhaka. The live stream's own status is
    in the summary too (the 'Quotex session' line only covers the
    collector, which is disconnected between its 2-hourly runs)."""
    import bot.collector as collector

    db, tmp = _tmp_db()
    db.set_state("last_collection", "2026-11-26 05:48:35")  # UTC
    db.set_state("next_collection", "2026-11-26 07:48:35")  # UTC
    db.set_state("quotex_last_login", "2026-11-26 05:48:34")  # UTC
    db.set_state("quotex_session", "disconnected (between runs)")
    db.set_state("live_stream_status", "connected")
    db.set_state("live_stream_last_seen", "2026-11-26 06:54:14")  # UTC
    s = collector.get_status_summary(db)
    assert s["last_collection"] == "2026-11-26 11:48:35", s
    assert s["next_collection"] == "2026-11-26 13:48:35", s
    assert s["quotex_last_login"] == "2026-11-26 11:48:34", s
    assert s["quotex_session"] == "disconnected (between runs)", s
    assert s["live_stream_status"] == "connected", s
    assert s["live_stream_last_seen"] == "2026-11-26 12:54:14", s
    print("PASS e) Bot Status times are Dhaka, live stream status included")


def _all_buttons(keyboard):
    return [b for row in keyboard.inline_keyboard for b in row]


def test_every_button_has_premium_icon():
    import bot.handlers as handlers
    import bot.premium_emoji as pe_mod

    assert handlers._BTN_SUPPORTS_ICON, "PTB too old for button icons"
    # Main menu (21 buttons incl. Contact Owner), join gate, back.
    for kb in (handlers.main_keyboard(), handlers._join_keyboard(),
               handlers.back_keyboard()):
        for b in _all_buttons(kb):
            assert getattr(b, "icon_custom_emoji_id", None), \
                f"button without icon: {b.text!r} ({b.callback_data or b.url})"
    # Futures + analysis pickers (asset rows + nav + back).
    assets = [f"AAA{i}_otc" for i in range(13)]
    kb, _, _ = handlers._asset_picker_keyboard(assets, 0, "OTC FS")
    for b in _all_buttons(kb):
        assert getattr(b, "icon_custom_emoji_id", None), \
            f"futures picker button without icon: {b.text!r}"
    kb, _, _ = handlers._analysis_picker_keyboard(assets, 1, "trend")
    for b in _all_buttons(kb):
        assert getattr(b, "icon_custom_emoji_id", None), \
            f"analysis picker button without icon: {b.text!r}"
    # Every icon key resolves to a real ID from the owner's packs.
    for key, eid in pe_mod.BUTTON_ICONS.items():
        assert isinstance(eid, str) and eid.isdigit(), (key, eid)
    # No bare InlineKeyboardButton constructions left outside the
    # two helpers — every button in the bot goes through _btn/_url_btn.
    src = Path(__file__).resolve().parent.parent.joinpath(
        "bot", "handlers.py").read_text()
    assert src.count("InlineKeyboardButton(") == 2, \
        "raw InlineKeyboardButton( found outside _btn/_url_btn"
    print("PASS d) every button carries a premium icon")


def test_stream_seeds_open_from_db():
    """f) An event without 'open' used to be skipped forever: the
    first event per asset has no previous stream candle, so the
    open-fill had nothing to use. The loop now seeds the previous
    candle from the DB — when the event continues it exactly (60s),
    its close becomes the missing open and the candle is stored."""
    db, tmp = _tmp_db()
    db.insert_candles("BTCUSD_otc", [{
        "ts": datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc),
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}])
    no_open = {"asset": "BTCUSD_otc", "period": 60, "index": TAIL_TS,
               "high": 101.5, "low": 100.0, "close": 101.0}
    session = FakeSession(event_from_poll=1, event_dict=no_open,
                          tail_candles=[])
    try:
        asyncio.run(live_stream.live_stream_loop(
            db=db, session_factory=lambda: session,
            sleep=_CountingSleep(max_calls=3)))
    except _StopLoop:
        pass
    else:
        raise AssertionError("loop ended without reaching the fake sleep")
    rows = _candles_for(db, "BTCUSD_otc", "2026-11-26")
    by_time = {r["time"]: r for r in rows}
    assert TAIL_TS in by_time, rows
    assert by_time[TAIL_TS]["O"] == 100.5, rows  # DB close became open
    assert by_time[TAIL_TS]["C"] == 101.0, rows
    assert db.get_state("live_stream_last_seen"), "last_seen never set"
    print("PASS f) stream seeds a missing 'open' from the DB's last candle")


class _CaptureHandler(logging.Handler):
    """Collects log records from a logger."""

    def __init__(self, level=logging.WARNING):
        super().__init__(level=level)
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


def _run_with_log_capture(db, session, max_calls):
    cap = _CaptureHandler()
    logger = logging.getLogger("littlematrix.live_stream")
    logger.addHandler(cap)
    try:
        asyncio.run(live_stream.live_stream_loop(
            db=db, session_factory=lambda: session,
            sleep=_CountingSleep(max_calls=max_calls)))
    except _StopLoop:
        pass
    finally:
        logger.removeHandler(cap)
    return cap.records


def test_stream_logs_dropped_events():
    """g) Events arriving but 0 candles stored for ~60s logs one
    diagnostic warning with a sample of the dropped event (keys +
    whether 'open' is present) — the log, not silence, tells the
    owner why Bot Status shows last candle '—'."""
    db, tmp = _tmp_db()
    no_open = {"asset": "BTCUSD_otc", "period": 60, "index": TAIL_TS,
               "high": 101.5, "low": 100.0, "close": 101.0}
    session = FakeSession(event_from_poll=1, event_dict=no_open,
                          tail_candles=[])
    msgs = _run_with_log_capture(db, session, max_calls=15)
    hit = [m for m in msgs if "0 candles stored" in m]
    assert hit, msgs
    assert "open=False" in hit[0], hit
    assert "BTCUSD_otc" in hit[0], hit
    print("PASS g) stream logs a diagnostic when events are dropped")


def test_stream_logs_silence():
    """h) No candle-generated events at all for ~60s logs one
    diagnostic warning naming the subscribed asset count — the
    server is silent or the subscription is not delivering."""
    db, tmp = _tmp_db()
    session = FakeSession(event_from_poll=999, tail_candles=[])
    msgs = _run_with_log_capture(db, session, max_calls=15)
    hit = [m for m in msgs if "no candle-generated events" in m]
    assert hit, msgs
    assert "2 assets subscribed" in hit[0], hit
    print("PASS h) stream logs a diagnostic when no events arrive")


def test_stream_subscribe_also_follows_by_code():
    """i) The name-based subscribe alone never delivered an event,
    so start_asset_streams also follows with the asset's NUMERIC
    code (what pyquotex's own one-stream helper does). The code map
    starts EMPTY in a real session — get_all_assets() is the only
    thing that fills it, and nothing in the bot called it before —
    so the method fills the map first; without that step zero
    follows are sent. A failed map fill still subscribes fine."""
    import bot.collector as collector_mod

    class FakeApi:
        def __init__(self):
            self.followed = []

        async def follow_candle(self, code):
            self.followed.append(code)

    class FakeClient:
        def __init__(self, codes, fill):
            self.started = []
            self.codes_asset = dict(codes)
            self.api = FakeApi()
            self._fill = fill  # dict get_all_assets() fills, or None

        async def get_all_assets(self):
            if self._fill is None:
                raise RuntimeError("no instruments")
            self.codes_asset.update(self._fill)
            return self.codes_asset

        async def start_candles_stream(self, asset, period=60):
            self.started.append((asset, period))

    async def _run(codes, fill):
        session = collector_mod.SharedQuotexSession()
        session.client = FakeClient(codes, fill)
        await session.start_asset_streams(
            ["BTCUSD_otc", "ETHUSD_otc"], 60)
        return session.client

    # Production case: the map starts empty, get_all_assets fills it.
    client = asyncio.run(_run({}, {"BTCUSD_otc": "99", "ETHUSD_otc": "42"}))
    assert client.started == [("BTCUSD_otc", 60), ("ETHUSD_otc", 60)], \
        client.started
    assert client.api.followed == ["99", "42"], client.api.followed
    # Map fill fails: subscribe still works, nothing followed.
    client = asyncio.run(_run({}, None))
    assert client.started == [("BTCUSD_otc", 60), ("ETHUSD_otc", 60)], \
        client.started
    assert client.api.followed == [], client.api.followed
    print("PASS i) stream subscribe fills the code map, then follows by code")


def test_candle_generated_handler_logs_and_stores():
    """j) The vendored handler stores the event AND logs the first
    few received (proving events arrive + showing their shape);
    an event without asset/period is logged as a warning instead
    of vanishing silently."""
    from collections import defaultdict
    from pyquotex.api import QuotexAPI

    api = object.__new__(QuotexAPI)
    api.candle_generated_check = defaultdict(lambda: defaultdict(dict))
    api.candle_generated_all_size_check = {}
    cap = _CaptureHandler(level=logging.INFO)
    logger = logging.getLogger("pyquotex.api")
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(cap)
    try:
        asyncio.run(api._h_candle_generated({
            "asset": "BTCUSD_otc", "period": 60, "index": EVENT_TS,
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}))
        assert api.candle_generated_check["BTCUSD_otc"][60]["close"] == 100.5
        assert any("candle-generated event: asset=BTCUSD_otc" in m
                   for m in cap.records), cap.records
        asyncio.run(api._h_candle_generated(
            {"index": EVENT_TS, "close": 1.0}))
        assert any("without asset/period" in m for m in cap.records), \
            cap.records
    finally:
        logger.removeHandler(cap)
        logger.setLevel(old_level)
    print("PASS j) candle-generated handler logs first events + bad shape")


def test_stream_test_asset_picks_one():
    """k) TEST MODE: LIVE_STREAM_TEST_ASSET names one asset and the
    stream subscribes to that asset only ('-' and '_' spellings both
    match); an unknown name falls back to all assets with a warning;
    empty (default) streams everything."""
    from bot.live_stream import _pick_stream_assets

    assets = ["BTCUSD_otc", "ETHUSD_otc", "EURUSD_otc"]
    assert _pick_stream_assets(assets, "") == assets
    assert _pick_stream_assets(assets, "BTCUSD-OTC") == ["BTCUSD_otc"]
    assert _pick_stream_assets(assets, "ethusd_otc") == ["ETHUSD_otc"]
    assert _pick_stream_assets(assets, "NOPE_otc") == assets
    print("PASS k) test-asset picker streams one asset only")


def test_ws_logs_unknown_control_events():
    """l) The stream never receives 'candle-generated' — if the
    server answers under a different event name, the dispatch must
    log the first few unknown names instead of dropping them
    silently. Known events still route to their handler."""
    from types import SimpleNamespace
    from pyquotex.api import QuotexAPI
    from pyquotex.global_value import AuthStatus

    calls = []

    async def _fake_candle(data):
        calls.append(data)

    api = object.__new__(QuotexAPI)
    api.state = SimpleNamespace(auth_status=AuthStatus.AUTHENTICATED)
    api._temp_status = ""
    api._control_handlers = {"candle-generated": _fake_candle}
    cap = _CaptureHandler(level=logging.INFO)
    logger = logging.getLogger("pyquotex.api")
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(cap)
    try:
        asyncio.run(api._on_message('42["mystery-event", {"x": 1}]'))
        assert any("WS unknown control event: mystery-event" in m
                   for m in cap.records), cap.records
        assert calls == []
        asyncio.run(api._on_message(
            '42["candle-generated", {"asset": "BTCUSD_otc", "period": 60}]'))
        assert len(calls) == 1 and calls[0]["asset"] == "BTCUSD_otc"
        assert not any("unknown control event: candle-generated" in m
                       for m in cap.records), cap.records
        # Throttle: at most 5 unknown-event lines in total.
        for i in range(6):
            asyncio.run(api._on_message(f'42["other-{i}", {{}}]'))
        unknown = [m for m in cap.records
                   if m.startswith("WS unknown control event:")]
        assert len(unknown) == 5, unknown
    finally:
        logger.removeHandler(cap)
        logger.setLevel(old_level)
    print("PASS l) WS dispatch logs unknown control event names (max 5)")


def test_stream_builds_candles_from_ticks():
    """m) The server never sends 'candle-generated' — but the same
    subscribe delivers price ticks, so closed minutes are built
    from ticks (open = first tick, close = latest, high/low =
    extremes). The current (open) minute is never stored, and a
    minute is stored exactly once."""
    import time as _time

    db, tmp = _tmp_db()
    now = int(_time.time())
    closed_min = now - (now % 60) - 60  # the just-closed minute
    cur_min = closed_min + 60
    ticks = {"BTCUSD_otc": [
        {"time": closed_min + 5, "price": 100.0},
        {"time": closed_min + 20, "price": 101.0},
        {"time": closed_min + 40, "price": 99.5},
        {"time": closed_min + 55, "price": 100.5},
        {"time": cur_min + 5, "price": 102.0},
    ]}
    # No events at all: the event channel is dead in this test.
    session = FakeSession(event_from_poll=999, ticks=ticks,
                          tail_candles=[])
    try:
        asyncio.run(live_stream.live_stream_loop(
            db=db, session_factory=lambda: session,
            sleep=_CountingSleep(max_calls=4)))
    except _StopLoop:
        pass
    else:
        raise AssertionError("loop ended without reaching the fake sleep")
    date_str = datetime.fromtimestamp(
        closed_min, tz=timezone.utc).astimezone(DHAKA).strftime("%Y-%m-%d")
    rows = _candles_for(db, "BTCUSD_otc", date_str)
    by_time = {r["time"]: r for r in rows}
    assert closed_min in by_time, rows
    candle = by_time[closed_min]
    assert (candle["O"], candle["H"], candle["L"], candle["C"]) == \
        (100.0, 101.0, 99.5, 100.5), candle
    # The current minute was NOT stored, and the closed minute once.
    assert cur_min not in by_time, rows
    assert [r["time"] for r in rows].count(closed_min) == 1, rows
    print("PASS m) stream builds closed-minute candles from live ticks")


def test_batch_corrects_tick_rows_but_not_vice_versa():
    """n) A tick-built row is an approximation: it must never
    overwrite batch/event truth, and the batch must correct it.
    insert_candles(on_conflict="update") overwrites; the default
    ("nothing") leaves the existing row alone."""
    db, tmp = _tmp_db()
    ts = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
    tick_row = {"ts": ts, "open": 100.0, "high": 101.0,
                "low": 99.0, "close": 100.5}
    batch_row = {"ts": ts, "open": 100.2, "high": 101.2,
                 "low": 99.2, "close": 100.8}
    assert db.insert_candles("BTCUSD_otc", [tick_row]) == 1
    # A second tick insert for the same minute changes nothing.
    assert db.insert_candles("BTCUSD_otc", [tick_row]) == 0
    rows = _candles_for(db, "BTCUSD_otc", "2026-11-26")
    assert rows[0]["O"] == 100.0 and rows[0]["C"] == 100.5, rows
    # The batch corrects the minute with the true values.
    assert db.insert_candles("BTCUSD_otc", [batch_row],
                             on_conflict="update") == 1
    rows = _candles_for(db, "BTCUSD_otc", "2026-11-26")
    assert rows[0]["O"] == 100.2 and rows[0]["C"] == 100.8, rows
    # And a late tick insert can never overwrite the batch row.
    assert db.insert_candles("BTCUSD_otc", [tick_row]) == 0
    rows = _candles_for(db, "BTCUSD_otc", "2026-11-26")
    assert rows[0]["O"] == 100.2, rows
    print("PASS n) batch corrects tick rows; ticks never overwrite truth")


class _FakeClock:
    """Controllable clock for the stream tests: time() and
    monotonic() both read the same fake 'now'."""

    def __init__(self, start):
        self.now = float(start)

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _ClockSleep:
    """Fake sleep that advances the fake clock instead of waiting —
    and raises _StopLoop after max_calls, like _CountingSleep."""

    def __init__(self, clock, max_calls):
        self.clock = clock
        self.calls = 0
        self.max_calls = max_calls

    async def __call__(self, seconds):
        self.calls += 1
        if self.calls > self.max_calls:
            raise _StopLoop
        self.clock.advance(seconds)


class _TickScriptSession(FakeSession):
    """Ticks for BTCUSD always; ticks for ETHUSD only while the
    fake clock is before `eth_stop` — then the asset goes silent
    mid-run, like EURUSD-OTC did on 2026-11-27."""

    def __init__(self, clock, eth_stop):
        super().__init__(event_from_poll=999, tail_candles=[])
        self.clock = clock
        self.eth_stop = eth_stop

    def generated_ticks(self):
        now = int(self.clock.time())
        cur_min = now - (now % 60)
        ticks = {"BTCUSD_otc": [{"time": cur_min + 5, "price": 100.0}]}
        if now < self.eth_stop:
            ticks["ETHUSD_otc"] = [{"time": cur_min + 5, "price": 200.0}]
        return ticks


def test_stream_reconnects_when_one_asset_goes_silent():
    """o) The owner, 2026-11-27: EURUSD-OTC and USDJPY-OTC stopped
    at 10:44 and BTCUSD-OTC at 12:13 while USDPKR-OTC kept ticking
    — the old GLOBAL-only staleness check never fired, because
    last_any resets on ANY asset's candle. The loop must reconnect
    when an asset that ticked in this run goes quiet for the whole
    stale window while others still tick."""
    db, tmp = _tmp_db()
    clock = _FakeClock(1_700_000_000)
    session = _TickScriptSession(clock, eth_stop=1_700_000_000 + 180)
    real_time = live_stream.time
    live_stream.time = clock
    cap = _CaptureHandler()
    logger = logging.getLogger("littlematrix.live_stream")
    logger.addHandler(cap)
    try:
        asyncio.run(live_stream.live_stream_loop(
            db=db, session_factory=lambda: session,
            sleep=_ClockSleep(clock, max_calls=300)))
    except _StopLoop:
        pass
    finally:
        live_stream.time = real_time
        logger.removeHandler(cap)
    # The loop reconnected: the per-asset check fired while BTCUSD
    # kept ticking (the old global-only check never would).
    assert session.connects >= 2, session.connects
    hit = [m for m in cap.records if "silent for 3 minutes" in m]
    assert hit, cap.records
    assert "ETHUSD_otc" in hit[0], hit
    print("PASS o) stream reconnects when one asset goes silent "
          "while others still tick")


def test_stream_ignores_never_ticked_asset():
    """p) An asset that NEVER ticked in this run (closed market)
    must NOT trigger a reconnect, or the loop would flap forever
    every few minutes."""
    db, tmp = _tmp_db()
    clock = _FakeClock(1_700_000_000)
    # eth_stop == start: ETH never ticks at all.
    session = _TickScriptSession(clock, eth_stop=1_700_000_000)
    real_time = live_stream.time
    live_stream.time = clock
    try:
        asyncio.run(live_stream.live_stream_loop(
            db=db, session_factory=lambda: session,
            sleep=_ClockSleep(clock, max_calls=100)))
    except _StopLoop:
        pass
    finally:
        live_stream.time = real_time
    assert session.connects == 1, session.connects
    print("PASS p) stream does not reconnect for an asset that never ticked")


def test_dead_background_task_restarts():
    """q) A background task that dies (collector / live stream)
    restarts itself after a short delay — the bot must not sit
    silent until the next deploy. The restart budget (5 in 10
    minutes) stops a tight crash loop."""
    import main as main_mod
    from types import SimpleNamespace

    app = SimpleNamespace(bot_data={})
    started = []

    async def _dying():
        started.append(1)
        raise RuntimeError("boom")

    old_delay = main_mod._TASK_RESTART_DELAY_SECONDS
    main_mod._TASK_RESTART_DELAY_SECONDS = 0.05
    try:
        async def _scenario():
            main_mod._restart_task(app, "test_task", _dying, "test")
            await asyncio.sleep(0.5)

        asyncio.run(_scenario())
    finally:
        main_mod._TASK_RESTART_DELAY_SECONDS = old_delay
    # 1 original start + 5 budgeted restarts, then it stops.
    assert len(started) == 6, started
    print("PASS q) a dead background task restarts itself (budget 5 in 10 min)")


def _never_ticked_setup(clock_start, eth_candle_ts):
    """Fake clock at `clock_start` + a DB with one ETHUSD candle at
    `eth_candle_ts` (or None for no candle). Returns (db, clock)."""
    db, tmp = _tmp_db()
    clock = _FakeClock(clock_start)
    if eth_candle_ts is not None:
        db.insert_candles("ETHUSD_otc", [{
            "ts": datetime.fromtimestamp(eth_candle_ts, tz=timezone.utc),
            "open": 200.0, "high": 201.0, "low": 199.0, "close": 200.5}])
    return db, tmp, clock


def test_stream_reconnects_when_subscribed_asset_never_ticks():
    """r) The owner, 2026-11-27: 20 major forex OTC pairs sat at
    10:44 while exotics kept ticking — they never ticked in the new
    run, so neither the global check nor the per-asset silence
    check could fire for them. A subscribed asset that was trading
    recently but delivers no tick at all in this run has a broken
    subscription — the loop reconnects so the tail-fill can heal it."""
    start = int(datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc).timestamp())
    db, tmp, clock = _never_ticked_setup(
        start, start - 1800)  # ETH candle 30 min before the run
    session = _TickScriptSession(clock, eth_stop=start)  # ETH never ticks
    real_time = live_stream.time
    live_stream.time = clock
    cap = _CaptureHandler()
    logger = logging.getLogger("littlematrix.live_stream")
    logger.addHandler(cap)
    try:
        asyncio.run(live_stream.live_stream_loop(
            db=db, session_factory=lambda: session,
            sleep=_ClockSleep(clock, max_calls=300)))
    except _StopLoop:
        pass
    finally:
        live_stream.time = real_time
        logger.removeHandler(cap)
    assert session.connects >= 2, session.connects
    hit = [m for m in cap.records if "never ticked" in m]
    assert hit, cap.records
    assert "ETHUSD_otc" in hit[0], hit
    print("PASS r) stream reconnects when a subscribed asset never ticks")


def test_stream_ignores_never_ticked_asset_with_old_candle():
    """s) A subscribed asset whose last candle is days old (delisted
    or long-closed) must NOT trigger a reconnect — only recently
    trading assets count as broken subscriptions."""
    start = int(datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc).timestamp())
    db, tmp, clock = _never_ticked_setup(
        start, start - 2 * 86400)  # ETH candle 2 days before the run
    session = _TickScriptSession(clock, eth_stop=start)  # ETH never ticks
    real_time = live_stream.time
    live_stream.time = clock
    try:
        asyncio.run(live_stream.live_stream_loop(
            db=db, session_factory=lambda: session,
            sleep=_ClockSleep(clock, max_calls=100)))
    except _StopLoop:
        pass
    finally:
        live_stream.time = real_time
    assert session.connects == 1, session.connects
    print("PASS s) stream ignores a never-ticked asset whose last candle is old")


def test_never_ticked_cooldown_bounds_reconnects():
    """t) After a never-ticked reconnect, the same asset must not
    re-trigger until its cooldown expires — otherwise a persistently
    broken subscription would flap the stream every few minutes."""
    start = int(datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc).timestamp())
    db, tmp, clock = _never_ticked_setup(start, start - 1800)
    session = _TickScriptSession(clock, eth_stop=start)  # ETH never ticks
    real_time = live_stream.time
    live_stream.time = clock
    old_cool = live_stream._NEVER_TICKED_COOLDOWN_SECONDS
    live_stream._NEVER_TICKED_COOLDOWN_SECONDS = 60
    try:
        asyncio.run(live_stream.live_stream_loop(
            db=db, session_factory=lambda: session,
            sleep=_ClockSleep(clock, max_calls=300)))
    except _StopLoop:
        pass
    finally:
        live_stream.time = real_time
        live_stream._NEVER_TICKED_COOLDOWN_SECONDS = old_cool
    # Initial connect + two never-ticked reconnects (one per grace
    # window once the 60s test cooldown expired).
    assert session.connects >= 3, session.connects
    print("PASS t) never-ticked cooldown bounds reconnects")


def test_stream_assets_includes_missing_recent_otc():
    """u) The owner, 2026-11-27: EURUSD-OTC stopped at 10:44 and the
    stream never fetched it again, because the open list Quotex
    returns had dropped it. The stream now adds recently-trading OTC
    assets from the DB to the subscribe list, so they keep streaming
    and tail-filling. Real-market and stale assets are NOT added."""
    db, tmp = _tmp_db()
    now = datetime.now(DHAKA)

    def _seed(asset, ts):
        db.insert_candles(asset, [{"ts": ts, "open": 1.0, "high": 1.1,
                                   "low": 0.9, "close": 1.05}],
                          on_conflict="nothing")

    _seed("EURUSD_otc", now - timedelta(minutes=70))
    _seed("XAUUSD", now - timedelta(minutes=70))
    _seed("USDPHP_otc", now - timedelta(days=2))
    # The fake session's open list is BTCUSD + ETHUSD only — EURUSD
    # is missing from it.
    session = FakeSession()
    assets = asyncio.run(live_stream._stream_assets(db, session))
    assert "EURUSD_otc" in assets, assets
    assert "XAUUSD" not in assets, assets
    assert "USDPHP_otc" not in assets, assets
    assert "BTCUSD_otc" in assets and "ETHUSD_otc" in assets, assets
    print("PASS u) stream adds recently-trading OTC assets missing "
          "from the open list")


def test_tail_fill_skips_current_assets():
    """v) The owner, 2026-11-27: the tail-fill fetched every asset's
    full 10-min floor span even when the asset was current — 90
    assets in batches of 3 is what froze the stream for ~10 min at
    startup (the 20:41 freeze). It must skip assets whose last
    candle is within the skip window and fetch only the stale ones."""
    db, tmp = _tmp_db()
    now = datetime(2026, 11, 27, 20, 40, tzinfo=DHAKA)
    for asset, age in (("BTCUSD_otc", 60), ("ETHUSD_otc", 1800)):
        db.insert_candles(asset, [{
            "ts": now.astimezone(timezone.utc) - timedelta(seconds=age),
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}])
    fetched = []

    class _RecSession(FakeSession):
        async def fetch_asset_span(self, asset, seconds_back):
            fetched.append(asset)
            return []

    session = _RecSession(tail_candles=[])
    asyncio.run(live_stream._tail_fill(
        db, session, ["BTCUSD_otc", "ETHUSD_otc"], now=now))
    assert fetched == ["ETHUSD_otc"], fetched
    print("PASS v) tail-fill skips current assets, fetches only stale ones")


class _YieldingClockSleep(_ClockSleep):
    """Like _ClockSleep, but yields to the event loop once, so a
    background task (the tail-fill) gets to start before the poll
    loop's fake clock runs it to the stop signal."""

    def __init__(self, clock, max_calls):
        super().__init__(clock, max_calls)
        self.yielded = False

    async def __call__(self, seconds):
        if not self.yielded:
            self.yielded = True
            await asyncio.sleep(0)
        return await super().__call__(seconds)


def test_poll_loop_runs_while_tail_fill_is_slow():
    """w) The owner, 2026-11-27: the 20:41 freeze — the tail-fill ran
    BEFORE the poll loop, so a slow tail-fill (timed-out fetches)
    froze the stream for ~10 min and the staleness trigger could not
    fire. Now the tail-fill runs in the background: the poll loop
    must store live tick candles even while the tail-fill is still
    fetching."""
    db, tmp = _tmp_db()
    start = int(datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc).timestamp())
    clock = _FakeClock(start)
    fetched = []

    class _SlowTailSession(_TickScriptSession):
        async def fetch_asset_span(self, asset, seconds_back):
            fetched.append(asset)
            # A slow history fetch (real time) — the old code would
            # have blocked the poll loop on this for seconds.
            await asyncio.sleep(2)
            return []

    session = _SlowTailSession(clock, eth_stop=start + 10000)
    real_time = live_stream.time
    live_stream.time = clock
    try:
        asyncio.run(live_stream.live_stream_loop(
            db=db, session_factory=lambda: session,
            sleep=_YieldingClockSleep(clock, max_calls=30)))
    except _StopLoop:
        pass
    finally:
        live_stream.time = real_time
    # The tail-fill started (it is slow, not skipped)...
    assert fetched, fetched
    # ...and the poll loop still stored live tick candles while it ran.
    rows = db.get_candles("BTCUSD_otc", "2026-11-27")
    assert rows, rows
    print("PASS w) poll loop stores live ticks while the tail-fill is slow")


if __name__ == "__main__":
    test_stream_stores_generated_candles()
    test_stream_reconnects_and_tail_fills()
    test_analysis_pickers_use_picked_asset()
    test_every_button_has_premium_icon()
    test_bot_status_times_are_dhaka_and_stream_shown()
    test_stream_seeds_open_from_db()
    test_stream_logs_dropped_events()
    test_stream_logs_silence()
    test_stream_subscribe_also_follows_by_code()
    test_candle_generated_handler_logs_and_stores()
    test_stream_test_asset_picks_one()
    test_ws_logs_unknown_control_events()
    test_stream_builds_candles_from_ticks()
    test_batch_corrects_tick_rows_but_not_vice_versa()
    test_stream_reconnects_when_one_asset_goes_silent()
    test_stream_ignores_never_ticked_asset()
    test_dead_background_task_restarts()
    test_stream_reconnects_when_subscribed_asset_never_ticks()
    test_stream_ignores_never_ticked_asset_with_old_candle()
    test_never_ticked_cooldown_bounds_reconnects()
    test_stream_assets_includes_missing_recent_otc()
    test_tail_fill_skips_current_assets()
    test_poll_loop_runs_while_tail_fill_is_slow()
    print("ALL LIVE-STREAM TESTS PASSED")
