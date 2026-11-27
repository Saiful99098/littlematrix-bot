"""MATRIX referral — BLUE FISH PRIME (the owner, 2026-11-27).

Pricing chosen by the owner: Option B (strict).
- 1 new member joining via a user's link = 5 MATRIX coins.
- 10 coins = 1 day Premium.
- 50 coins = 30 days Premium.

How it works:
1. A free user taps Referral in the menu. The bot creates ONE
   invite link for that user (Telegram createChatInviteLink on the
   required channel) and stores it — the same link is reused, so a
   user never gets a second link to share around.
2. Someone joins the channel via that link. Telegram sends the bot
   a ChatMember update carrying the invite link used, and
   attribute_join() looks up who owns it.
3. Coins are paid only by database.record_referral(), which enforces
   the anti-abuse rules: one invited user counts once ever, and
   self-joins pay nothing. See bot/database.py for those rules.

The bot must be an ADMIN in the channel with the 'Add members'
permission, or Telegram refuses to create invite links.
"""
from __future__ import annotations

import logging

from . import config, database

logger = logging.getLogger("littlematrix.referral")

COINS_PER_JOIN = 5
# (coins, premium days) — Option B, as the owner chose.
REWARDS = ((50, 30), (10, 1))


def spend_reward(telegram_id, coins):
    """Spend `coins` on Premium. Returns (ok, days, until_or_balance):
    - (True, days, until_iso) when the coins covered it.
    - (False, 0, current_balance) when the balance is too low.
    Only the two Option B prices are accepted — any other amount is
    refused, so a crafted button cannot buy Premium for 1 coin."""
    days = dict((c, d) for c, d in REWARDS).get(coins)
    if days is None:
        return False, 0, database.get_coin_balance(telegram_id)
    ok, balance = database.spend_coins(telegram_id, coins)
    if not ok:
        return False, 0, balance
    until = database.extend_premium(telegram_id, days)
    return True, days, until


def attribute_join(invited_id, invite_link):
    """Award coins for a channel join via a referral link.

    Returns (status, coins, referrer_id, new_balance) — status is
    "awarded", "duplicate", "self", or "unknown_link" (the link is
    not a referral link, e.g. the public channel invite).
    Never raises: a referral failure must never break the welcome
    message that runs alongside it."""
    try:
        if not invite_link:
            return "unknown_link", 0, None, 0
        referrer_id = database.get_referrer_for_link(invite_link)
        if referrer_id is None:
            return "unknown_link", 0, None, 0
        status, coins, balance = database.record_referral(
            referrer_id, invited_id, invite_link, coins=COINS_PER_JOIN)
        return status, coins, referrer_id, balance
    except Exception:
        logger.exception("referral attribution failed for %s", invited_id)
        return "error", 0, None, 0


async def get_my_invite_link(bot, user_id):
    """The user's referral link, creating it on first use.

    Returns (link, error): link is the invite URL, or None with an
    error string when the channel is not set or Telegram refuses
    (bot is not a channel admin with 'Add members')."""
    if not config.REQUIRED_CHANNEL:
        return None, "Channel is not set — the owner must set REQUIRED_CHANNEL first."
    stored = database.get_referral_link(user_id)
    if stored:
        return stored, None
    try:
        created = await bot.create_chat_invite_link(
            chat_id=config.REQUIRED_CHANNEL, name=f"ref-{user_id}")
        link = created.invite_link
    except Exception as exc:
        logger.warning("create_chat_invite_link failed for %s: %s", user_id, exc)
        return None, ("Could not create your invite link — the bot needs to be "
                      "a channel admin with the 'Add members' permission.")
    database.save_referral_link(user_id, link)
    return link, None


def referral_text(stats, link):
    """The Referral screen (English — the bot is English-only)."""
    lines = [
        "MATRIX Referral",
        "Share your link — when a new member joins the channel through "
        "it, you earn 5 MATRIX coins.",
        "",
        f"Your link: {link}",
        "",
        f"Coins: {stats['balance']}  •  Members joined: {stats['joins']}  •  "
        f"Coins earned: {stats['coins_earned']}",
        "",
        "Spend coins on Premium:",
        "10 coins = 1 day Premium",
        "50 coins = 30 days Premium",
        "",
        "One member's join counts only once — leaving and rejoining "
        "earns nothing, and your own join earns nothing.",
    ]
    return "\n".join(lines)
