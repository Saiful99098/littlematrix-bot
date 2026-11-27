"""Tests for the MATRIX referral system (the owner, 2026-11-27,
Option B pricing: 1 join = 5 coins; 10 coins = 1 day Premium;
50 coins = 30 days Premium).

Run: python -m pytest tests/test_referral.py -q
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config, database, referral


def _fresh_db():
    """Point the users DB at a throwaway SQLite file."""
    config.DATABASE_URL = ""
    config.DB_PATH = Path(tempfile.mkdtemp()) / "users.db"
    database._CONN = None
    database._connect()


def test_first_join_awards_5_coins():
    _fresh_db()
    database.save_referral_link(111, "https://t.me/+ref111")
    status, coins, referrer, balance = referral.attribute_join(
        222, "https://t.me/+ref111")
    assert (status, coins, referrer) == ("awarded", 5, 111)
    assert balance == 5
    assert database.get_coin_balance(111) == 5


def test_same_user_counts_only_once():
    _fresh_db()
    database.save_referral_link(111, "https://t.me/+ref111")
    referral.attribute_join(222, "https://t.me/+ref111")
    # Leave and rejoin: same invited user, second time pays nothing.
    status, coins, _, balance = referral.attribute_join(
        222, "https://t.me/+ref111")
    assert (status, coins) == ("duplicate", 0)
    assert balance == 5
    # A second referrer cannot claim the same user either.
    database.save_referral_link(333, "https://t.me/+ref333")
    status, coins, _, _ = referral.attribute_join(222, "https://t.me/+ref333")
    assert (status, coins) == ("duplicate", 0)


def test_self_join_pays_nothing():
    _fresh_db()
    database.save_referral_link(111, "https://t.me/+ref111")
    status, coins, _, balance = referral.attribute_join(
        111, "https://t.me/+ref111")
    assert (status, coins, balance) == ("self", 0, 0)


def test_unknown_link_pays_nothing():
    _fresh_db()
    status, coins, referrer, _ = referral.attribute_join(
        222, "https://t.me/+public-invite")
    assert (status, coins, referrer) == ("unknown_link", 0, None)


def test_spend_option_b_prices():
    _fresh_db()
    # 5 coins is not enough for either Option B reward.
    database.add_coins(111, 5)
    ok, days, balance = referral.spend_reward(111, 10)
    assert (ok, days) == (False, 0)
    assert balance == 5
    # 10 coins = 1 day Premium.
    database.add_coins(111, 5)
    ok, days, until = referral.spend_reward(111, 10)
    assert (ok, days) == (True, 1)
    assert database.is_premium(111) is True
    assert database.get_coin_balance(111) == 0
    # 50 coins = 30 days Premium, extending the 1 day already held.
    database.add_coins(111, 50)
    ok, days, until2 = referral.spend_reward(111, 50)
    assert (ok, days) == (True, 30)
    assert until2 > until
    assert database.get_coin_balance(111) == 0


def test_spend_rejects_other_amounts():
    _fresh_db()
    database.add_coins(111, 100)
    # 1 coin, 5 coins, 15 coins are not Option B prices — refused,
    # and the balance is untouched.
    for amount in (1, 5, 15, 30):
        ok, _, balance = referral.spend_reward(111, amount)
        assert ok is False
    assert database.get_coin_balance(111) == 100


def test_referral_stats():
    _fresh_db()
    database.save_referral_link(111, "https://t.me/+ref111")
    referral.attribute_join(222, "https://t.me/+ref111")
    referral.attribute_join(333, "https://t.me/+ref111")
    stats = database.get_referral_stats(111)
    assert stats == {"balance": 10, "joins": 2, "coins_earned": 10}


class _FakeTGUser:
    def __init__(self, user_id):
        self.id = user_id


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id
        self.username = None


class _FakeChatMember:
    def __init__(self, status, user=None):
        self.status = status
        self.user = user


class _FakeInviteLink:
    def __init__(self, link):
        self.invite_link = link


class _FakeChatMemberUpdate:
    def __init__(self, chat_id, old, new, user, invite_link=None):
        self.chat = _FakeChat(chat_id)
        self.old_chat_member = _FakeChatMember(old)
        self.new_chat_member = _FakeChatMember(new, user)
        self.invite_link = (_FakeInviteLink(invite_link)
                            if invite_link else None)


class _FakeUpdate:
    def __init__(self, chat_member):
        self.chat_member = chat_member


class _FakeBot:
    def __init__(self):
        self.sends = []

    async def send_message(self, chat_id=None, text=None,
                           reply_markup=None, parse_mode=None, **kwargs):
        self.sends.append((chat_id, text))
        return None


class _FakeContext:
    def __init__(self, bot):
        self.bot = bot


def test_channel_join_via_referral_link_awards_coins():
    """8) The owner, 2026-11-27: referral coins were never credited —
    the webhook never delivered chat_member updates (allowed_updates
    omitted Telegram's default, which excludes chat_member), so the
    join handler never fired. A join via a referral link must award
    5 coins to the link's owner at once and DM them."""
    import asyncio
    import main as bot_main

    _fresh_db()
    database.save_referral_link(111, "https://t.me/+ref111")
    old_channel = config.REQUIRED_CHANNEL
    config.REQUIRED_CHANNEL = "-100123"
    bot = _FakeBot()
    update = _FakeUpdate(_FakeChatMemberUpdate(
        -100123, "left", "member", _FakeTGUser(222),
        invite_link="https://t.me/+ref111"))
    try:
        asyncio.run(bot_main.channel_member_update(update, _FakeContext(bot)))
    finally:
        config.REQUIRED_CHANNEL = old_channel
    assert database.get_coin_balance(111) == 5, database.get_coin_balance(111)
    assert any(cid == 111 and "+5 MATRIX coins" in (t or "")
               for cid, t in bot.sends), bot.sends
    assert any(cid == 222 and "Welcome" in (t or "")
               for cid, t in bot.sends), bot.sends
    print("PASS 8) channel join via a referral link awards 5 coins at once")


def test_webhook_requests_chat_member_updates():
    """9) The owner, 2026-11-27: the root cause of the missing coins —
    main() called run_webhook/run_polling without allowed_updates, so
    Telegram's default (all types EXCEPT chat_member) applied and the
    join handler never fired. The webhook and polling calls must
    request chat_member (and my_chat_member) explicitly."""
    import inspect
    import main as bot_main

    assert "chat_member" in bot_main.ALLOWED_UPDATES, bot_main.ALLOWED_UPDATES
    assert "my_chat_member" in bot_main.ALLOWED_UPDATES, bot_main.ALLOWED_UPDATES
    assert "message" in bot_main.ALLOWED_UPDATES, bot_main.ALLOWED_UPDATES
    assert "callback_query" in bot_main.ALLOWED_UPDATES, bot_main.ALLOWED_UPDATES
    src = inspect.getsource(bot_main.main)
    assert "allowed_updates=ALLOWED_UPDATES" in src, src
    assert src.count("allowed_updates=ALLOWED_UPDATES") >= 2, src
    print("PASS 9) webhook and polling request chat_member updates")


def main():
    test_first_join_awards_5_coins()
    test_same_user_counts_only_once()
    test_self_join_pays_nothing()
    test_unknown_link_pays_nothing()
    test_spend_option_b_prices()
    test_spend_rejects_other_amounts()
    test_referral_stats()
    test_channel_join_via_referral_link_awards_coins()
    test_webhook_requests_chat_member_updates()
    print("ALL TESTS PASSED (9/9)")


if __name__ == "__main__":
    main()
