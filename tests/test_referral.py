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


def main():
    test_first_join_awards_5_coins()
    test_same_user_counts_only_once()
    test_self_join_pays_nothing()
    test_unknown_link_pays_nothing()
    test_spend_option_b_prices()
    test_spend_rejects_other_amounts()
    test_referral_stats()
    print("ALL TESTS PASSED (7/7)")


if __name__ == "__main__":
    main()
