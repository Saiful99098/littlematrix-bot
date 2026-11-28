"""Candlestick chart screenshots, drawn locally from DB candles.

No paid API, no network — the bot draws the chart itself with
Pillow and sends it as a Telegram photo. Used by Live Auto Signal
(the signal screenshot and the result screenshot, with a tick on a
winning candle and a cross on a losing one) and by the Loss Candle
report. Candles are normalized dicts (see signal_engine.
normalize_candle): {"datetime", "hhmm", "open", "high", "low",
"close"}.
"""
from __future__ import annotations

import io

BG = (22, 22, 26)        # dark ash, like the candle viewer
GRID = (58, 58, 68)
TEXT = (237, 238, 242)
MUTED = (154, 156, 168)
GREEN = (34, 197, 94)
RED = (244, 63, 94)
AMBER = (245, 158, 11)
WICK = (200, 200, 210)


def _fmt_price(v):
    """Same price format as the candle viewer: >=1000 -> 2 decimals,
    >=100 -> 3 decimals, else 5 decimals."""
    if abs(v) >= 1000:
        return f"{v:,.2f}"
    if abs(v) >= 100:
        return f"{v:.3f}"
    return f"{v:.5f}"


def _check_mark(draw, cx, y, size, color):
    """A tick (checkmark) drawn with lines, centered at (cx, y)."""
    draw.line([(cx - size, y), (cx - size // 3, y + size),
               (cx + size, y - size)], fill=color, width=4)


def _cross_mark(draw, cx, y, size, color):
    """A cross (X) drawn with lines, centered at (cx, y)."""
    draw.line([(cx - size, y - size), (cx + size, y + size)],
              fill=color, width=4)
    draw.line([(cx - size, y + size), (cx + size, y - size)],
              fill=color, width=4)


def draw_candles_png(candles, highlight_idx=None, mark=None,
                     title=None, width=720, height=420):
    """Draw `candles` (a list of normalized candle dicts) as a PNG.

    `highlight_idx` marks one candle (the signal candle); `mark` is
    "win" (a green tick above it) or "loss" (a red cross above it).
    Returns the PNG as bytes. Raises ValueError when there are no
    candles to draw."""
    from PIL import Image, ImageDraw

    rows = list(candles or [])
    if not rows:
        raise ValueError("no candles to draw")
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)
    top = 34 if title else 10
    bottom = height - 26
    left = 8
    right = width - 74  # room for price labels
    lo = min(c["low"] for c in rows)
    hi = max(c["high"] for c in rows)
    if hi <= lo:
        hi = lo + 1
    pad = (hi - lo) * 0.08 or 1
    lo -= pad
    hi += pad

    def y_of(price):
        return top + (hi - price) / (hi - lo) * (bottom - top)

    # Horizontal gridlines + price labels.
    for i in range(5):
        price = lo + (hi - lo) * i / 4
        y = int(y_of(price))
        draw.line([(left, y), (right, y)], fill=GRID, width=1)
        draw.text((right + 6, y - 6), _fmt_price(price), fill=MUTED)
    if title:
        draw.text((left, 8), title, fill=TEXT)
    # Candlesticks.
    n = len(rows)
    slot = (right - left) / n
    body_w = max(2, int(slot * 0.7))
    for i, c in enumerate(rows):
        cx = int(left + slot * (i + 0.5))
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        color = GREEN if cl > o else (RED if cl < o else AMBER)
        draw.line([(cx, int(y_of(h))), (cx, int(y_of(l)))],
                  fill=WICK, width=1)
        y_o, y_c = int(y_of(o)), int(y_of(cl))
        if y_o == y_c:
            draw.line([(cx - body_w // 2, y_o), (cx + body_w // 2, y_o)],
                      fill=color, width=2)
        else:
            draw.rectangle([cx - body_w // 2, min(y_o, y_c),
                            cx + body_w // 2, max(y_o, y_c)],
                           fill=color)
        if highlight_idx is not None and i == highlight_idx and mark:
            my = int(y_of(h)) - 22
            if mark == "win":
                _check_mark(draw, cx, my, 9, GREEN)
            elif mark == "loss":
                _cross_mark(draw, cx, my, 9, RED)
    # Time labels: first, middle, last.
    for i in (0, n // 2, n - 1):
        hhmm = rows[i].get("hhmm", "")
        if not hhmm:
            continue
        cx = int(left + slot * (i + 0.5))
        draw.text((max(left, min(cx - 14, right - 30)), height - 20),
                  hhmm, fill=MUTED)
    # Last close price, bottom-right.
    last = rows[-1]
    draw.text((right + 6, bottom - 6), _fmt_price(last["close"]),
              fill=GREEN if last["close"] >= last["open"] else RED)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
