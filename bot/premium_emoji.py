"""Telegram Premium custom emojis for Blue Fish Prime.

the owner's emoji packs (all IDs below are REAL, fetched with
Telegram Bot API getStickerSet — never invent an ID, a wrong ID
renders as a broken/blank emoji):
- https://t.me/addemoji/ZEROX_660_by_TgEmodziBot (200 emojis)
- https://t.me/addemoji/emoji_XtremistQFS_by_TgEmodziBot (179 emojis)
- https://t.me/addemoji/NewsEmoji (100 emojis)
- https://t.me/addemoji/VariousAnimations9 (84 emojis)
- https://t.me/addemoji/marketment (200 emojis)
- https://t.me/addemoji/QUOTEX_VIP_SIGNAL1_by_TgEmodziBot (200 emojis)
- https://t.me/addemoji/RandomEmoj4 (200 emojis)
- https://t.me/addemoji/Emoji004_1912SocialMedia (172 emojis)
- https://t.me/addemoji/Decoration_Pack2 (158 emojis)
- https://t.me/addemoji/CARDERS_by_TgEmojis_bot (163 emojis)

Usage in handlers.py:
    from .premium_emoji import pe
    text = f"{pe('diamond')} BLUE FISH PRIME"
    await query.edit_message_text(text, parse_mode="HTML")

Button icons (Bot API 9.4+, python-telegram-bot 22.7+):
    from .premium_emoji import BUTTON_ICONS
    InlineKeyboardButton("Check Signals", callback_data="menu_checker",
                         icon_custom_emoji_id=BUTTON_ICONS["checker"])
    The icon renders BEFORE the button text, so an icon button must
    NOT also carry a Unicode emoji in its text (it would duplicate).
    Buttons without a matching custom emoji keep their Unicode emoji
    in the text and no icon. Only the Premium button uses
    style="primary" and only SELECT ALL uses style="success".
    Icons render only when the bot owner has Telegram Premium and
    the client is up to date; otherwise the plain text still shows.

Rules:
- pe(name) returns an HTML tag:
  <tg-emoji emoji-id="<id>"><fallback></tg-emoji>
  The fallback char is what non-Premium users see; Premium users
  see the animated custom emoji.
- EVERY reply_text / edit_message_text call whose text contains a
  pe() tag MUST pass parse_mode="HTML".
- NEVER put <tg-emoji> tags inside button labels
  (InlineKeyboardButton text is plain text, HTML is not supported
  there — buttons keep their Unicode emojis).
- NEVER put <tg-emoji> tags inside signal lines
  (USDMXN-OTC,09:02 M1 CALL) or CHECKER per-line results — those
  must stay plain text so users can copy-paste them and CHECKER
  can parse them. Premium emojis go in headers / summaries /
  status text around them.
"""
from __future__ import annotations

# name -> (custom_emoji_id, fallback_char)
PREMIUM_EMOJI = {
    "fire": ("6264785189394717307", "🔥"),
    "diamond": ("6264791387032523779", "💎"),
    "crown": ("6266995104687330978", "👑"),
    "lightning": ("6267107057304868214", "⚡"),
    "check": ("6267008582294705964", "✅"),
    "money": ("6267068789146260253", "💰"),
    "star": ("6264925819508888658", "⭐"),
    "sparkle": ("6285088169817805553", "✨"),
    # CARDERS_by_TgEmodziBot — premium animated warning (used for the
    # DISCLAIMER; the owner circled the plain ⚠️ and wants this one).
    "warning": ("6089079808187174973", "⚠️"),
    "bell": ("6264510702329797113", "🔔"),
    "pin": ("6267172559851099903", "📌"),
    "eyes": ("6267186570034419608", "👀"),
    "party": ("6267117038808870117", "🎉"),
    "chart": ("5231200819986047254", "📊"),
    "up": ("5244837092042750681", "📈"),
    "down": ("5246762912428603768", "📉"),
    "cross": ("5210952531676504517", "❌"),
    "search": ("5231012545799666522", "🔍"),
    "refresh": ("5375338737028841420", "🔄"),
    "dollar": ("5409048419211682843", "💵"),
    # ⏳ hourglass — marketment (progress / "working" messages)
    "hourglass": ("6010111371251815589", "⏳"),
    # 💬 message — ZEROX_660_by_TgEmodziBot (contact line)
    "message": ("6266794310671275367", "💬"),
    # 🟢 green circle — NewsEmoji (candle scan / open assets)
    "green": ("5416081784641168838", "🟢"),
    # 🔴 red circle — NewsEmoji (candle scan)
    "red": ("5411225014148014586", "🔴"),
    # ℹ️ info — NewsEmoji (same ID as the About button icon)
    "info": ("5334544901428229844", "ℹ️"),
    # ➖ minus — marketment (CHECKER DOJI symbol)
    "minus": ("5789644639749281149", "➖"),
    # ❓ question — ZEROX_660_by_TgEmodziBot — RETIRED, DO NOT USE:
    # renders as a gold "WIN?" sticker, misleading as CHECKER NO DATA.
    # Checker uses plain "?" instead (see bot/checker.py).
    "question": ("6282525354242348869", "❓"),
}


def pe(name):
    """Return the <tg-emoji> HTML tag for a premium emoji name.

    Raises KeyError for an unknown name — fail loudly in tests
    rather than silently sending a broken tag.
    """
    emoji_id, fallback = PREMIUM_EMOJI[name]
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


# ---------------------------------------------------------------------------
# Button icons — custom emoji shown BEFORE the button text
# (InlineKeyboardButton icon_custom_emoji_id, Bot API 9.4+,
# python-telegram-bot 22.7+). Keys are button names, values are REAL
# custom_emoji_id strings fetched with getStickerSet (never invented).
# A button is listed here ONLY when its Unicode emoji has a matching
# custom emoji in the owner's packs. All 21 main-menu buttons now have
# icons (the last 6 — Market Live, OTC Board, News Desk, Timezone,
# Signal Filter, My Status — were added from Decoration_Pack2,
# NewsEmoji and ZEROX_660_by_TgEmodziBot).
# Pack source is noted per button.
# ---------------------------------------------------------------------------
BUTTON_ICONS = {
    # --- main menu ---
    # 🔍 Check Signals — marketment
    "checker": "5803403369913520877",
    # 🤖 AI Signal — VariousAnimations9
    "ai": "5064851690025387277",
    # ⚡ Future Signals — VariousAnimations9
    "futures": "4997289591011544358",
    # 🌍 Market Live — Decoration_Pack2
    "market": "5314361729117855941",
    # 📊 OTC Board — NewsEmoji (board/chart fits)
    "otc": "5231200819986047254",
    # 💰 Payouts — marketment
    "payouts": "5967360061356380748",
    # 🕯 Candle Scan — NewsEmoji
    "counter": "5451882707875276247",
    # 🧪 Backtest Lab — RandomEmoj4
    "backtest": "5794242604103110904",
    # 📈 Trend Scan — NewsEmoji
    "trend": "5244837092042750681",
    # 📣 News Desk — ZEROX_660_by_TgEmodziBot (megaphone/announcement fits)
    "news": "6267129592998270736",
    # 🔄 Swap C/P — VariousAnimations9
    "swap": "5017470156276761427",
    # ✏️ Signal Formatter — NewsEmoji
    "formatter": "5395444784611480792",
    # ⏰ Timezone — ZEROX_660_by_TgEmodziBot (alarm clock/time fits)
    "timezone": "6285240160120477644",
    # 🔤 Font Lab — QUOTEX_VIP_SIGNAL1_by_TgEmodziBot
    "font": "6276282026572124702",
    # ⚙️ Signal Filter — NewsEmoji (gear/settings fits; NOT 🔍,
    # that emoji is already the Check Signals button icon)
    "filter": "5341715473882955310",
    # 😎 My Status — ZEROX_660_by_TgEmodziBot (cool face/profile fits)
    "status": "6264546492292273861",
    # ℹ️ About — NewsEmoji
    "about": "5334544901428229844",
    # 🔴 Auto Signals — VariousAnimations9
    "autolive": "5073651519669143988",
    # 💎 Premium — VariousAnimations9
    "premium": "4999002445444023072",
    # --- sub-flows ---
    # ✅ SELECT ALL — VariousAnimations9
    "select_all": "5021905410089550576",
    # 🟢 Bot Status — NewsEmoji green circle (same ID as pe("green"))
    "bot_status": "5416081784641168838",
    # ⬅️ Back — RandomEmoj4
    "back": "5789678368127455058",
    # ➡️ Next — QUOTEX_VIP_SIGNAL1_by_TgEmodziBot
    "next": "6336820492552377728",
    # 📊 OTC FS — QUOTEX_VIP_SIGNAL1_by_TgEmodziBot
    "mode_otc": "6336963167070984229",
    # 🌙 BLACKOUT FS — marketment
    "mode_blackout": "5924764134787782252",
    # ☀️ WHITEOUT FS — VariousAnimations9
    "mode_whiteout": "5019551308449841888",
    # 💵 REAL MARKET FS — marketment
    "mode_real": "5800720664620961831",
    # ✏️ Custom Time — NewsEmoji (same pencil as Signal Formatter)
    "custom_time": "5395444784611480792",
    # --- sub-flow buttons (live stream release) — all IDs are REAL,
    # reused from PREMIUM_EMOJI above, never invented ---
    # 💬 Contact Owner — same ID as pe("message")
    "contact": "6266794310671275367",
    # 🎉 Join Now — same ID as pe("party")
    "join": "6267117038808870117",
    # ✅ I Joined / Turn ON / Approve / Non-MTG — same ID as pe("check")
    "joined": "6267008582294705964",
    "toggle_on": "6267008582294705964",
    "approve": "6267008582294705964",
    "nomtg": "6267008582294705964",
    # ❌ Turn OFF / Reject — same ID as pe("cross")
    "toggle_off": "5210952531676504517",
    "reject": "5210952531676504517",
    # 📊 asset picker buttons — same ID as pe("chart")
    "asset": "5231200819986047254",
    # ⏳ day picker — same ID as pe("hourglass")
    "days": "6010111371251815589",
    # ⏰ time picker — same ID as the Timezone button icon
    "time": "6285240160120477644",
    # 🔄 MTG1 — same ID as the Swap button icon
    "mtg": "5017470156276761427",
    # 🔄 Refresh — same ID as pe("refresh")
    "refresh": "5375338737028841420",
    # ✅ selected asset — same ID as pe("check")
    "check": "6267008582294705964",
    # 💰 payout buttons — same ID as pe("money")
    "money": "6267068789146260253",
}
