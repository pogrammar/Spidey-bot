from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Ally, Brew, Cooldown, GiftUsage, InventoryItem, Item, MarketListing, PendingPhoto, Transaction, User
from utils.leveling import xp_for_level

STARTER_CAMERA_KEY = "camera"

# Every 5th reputation level is boss-gated — XP still accrues from patrols, but is
# pinned at the gate level's floor until the boss guarding it is beaten. boss_clears
# tracks how many gates a user has cleared; the next one is always 5 levels further
# out. Existing players are grandfathered in past migration (see
# alembic/versions/*_add_boss_gates.py) so nobody gets knocked back down to fight a
# gate they're already well past — only ever the next uncleared one.
BOSS_LEVEL_INTERVAL = 5


def next_boss_gate_level(user: User) -> int:
    return BOSS_LEVEL_INTERVAL * (user.boss_clears + 1)


def at_boss_gate(user: User) -> bool:
    return user.reputation_level >= next_boss_gate_level(user)


# A city left to run wild costs you focus — crime_level rises when you skip patrol
# for /tutoring or /ally visit, and decays back down whenever you actually patrol
# (see patrol_service.py / battle_service.py / ally_service.py). Past this
# threshold, reputation XP gains take a flat cut, same threshold-based shape as the
# existing ally-happiness penalty. Doesn't apply to a boss-clear promotion (that's
# a direct level-floor snap, not a scaled XP grant — same boundary the booster XP
# perk respects).
HIGH_CRIME_THRESHOLD = 70
CRIME_XP_PENALTY_MULTIPLIER = 0.8

# Bank capacity auto-expands instead of ever hard-blocking a deposit — sized to
# reputation level so it keeps pace with how much a higher-level player actually
# earns. Triggers reactively (a deposit that wouldn't fit) and proactively (bank
# already near full after a deposit that did fit), so the *next* deposit doesn't
# hit the ceiling either.
BANK_UPGRADE_BASE = 2000
BANK_UPGRADE_PER_LEVEL = 500
BANK_AUTO_UPGRADE_THRESHOLD = 0.9


async def get_or_create_user(session: AsyncSession, discord_id: int) -> User:
    """Fetches a profile, creating one (with a starter camera equipped) on first contact.
    Every Peter's had this beat-up camera since he first started swinging out."""
    user = await session.get(User, discord_id)
    if user is not None:
        return user

    user = User(discord_id=discord_id)
    session.add(user)

    camera_def = await session.get(Item, STARTER_CAMERA_KEY)
    session.add(
        InventoryItem(
            user_id=discord_id,
            item_key=STARTER_CAMERA_KEY,
            quantity=1,
            durability=camera_def.max_durability if camera_def else None,
            equipped=True,
        )
    )
    await session.commit()
    return user


async def add_wallet(session: AsyncSession, user: User, amount: int, reason: str) -> int:
    """Applies a wallet delta (positive or negative), clamped so it never goes below 0.
    Returns the actual delta applied (may differ from `amount` if clamped)."""
    before = user.wallet
    user.wallet = max(0, user.wallet + amount)
    actual_delta = user.wallet - before

    session.add(
        Transaction(
            user_id=user.discord_id,
            balance_type="wallet",
            amount=actual_delta,
            reason=reason,
        )
    )
    await session.commit()
    return actual_delta


async def add_bank(session: AsyncSession, user: User, amount: int, reason: str) -> int:
    """Admin-only direct bank injection — mirrors add_wallet but for the bank balance.
    Deliberately not clamped to bank_capacity: a limit meant to gate normal deposits
    shouldn't reject an admin grant."""
    before = user.bank
    user.bank = max(0, user.bank + amount)
    actual_delta = user.bank - before

    session.add(
        Transaction(user_id=user.discord_id, balance_type="bank", amount=actual_delta, reason=reason)
    )
    await session.commit()
    return actual_delta


async def add_reputation(session: AsyncSession, user: User, xp: int) -> int:
    """Clamped at the next uncleared boss gate — a losing or below-gate patrol still
    grants XP normally, but nothing pushes you past a gate you haven't beaten yet.
    Also cut by CRIME_XP_PENALTY_MULTIPLIER while crime_level is high. Returns the
    actual XP applied (may be less than requested if capped or penalized) — same
    "return what really happened" contract as add_wallet, so callers can display
    the real number instead of the pre-adjustment roll."""
    gained = max(0, xp)
    if user.crime_level >= HIGH_CRIME_THRESHOLD:
        gained = round(gained * CRIME_XP_PENALTY_MULTIPLIER)
    cap = xp_for_level(next_boss_gate_level(user))
    before = user.reputation_xp
    user.reputation_xp = min(user.reputation_xp + gained, cap)
    actual_delta = user.reputation_xp - before
    await session.commit()
    return actual_delta


def _upgrade_bank_capacity(user: User, extra_needed: int = 0) -> int:
    """Bumps bank_capacity and returns the increase applied."""
    increment = BANK_UPGRADE_BASE + BANK_UPGRADE_PER_LEVEL * (user.reputation_level - 1)
    increase = max(increment, extra_needed)
    user.bank_capacity += increase
    return increase


async def deposit(session: AsyncSession, user: User, amount: int) -> tuple[bool, str]:
    if amount <= 0:
        return False, "Deposit amount must be positive."
    if amount > user.wallet:
        return False, "You don't have that much cash on hand."

    room = user.bank_capacity - user.bank
    upgraded = False
    if amount > room:
        _upgrade_bank_capacity(user, extra_needed=amount - room)
        upgraded = True

    user.wallet -= amount
    user.bank += amount
    session.add(
        Transaction(user_id=user.discord_id, balance_type="bank", amount=amount, reason="deposit")
    )
    session.add(
        Transaction(
            user_id=user.discord_id, balance_type="wallet", amount=-amount, reason="deposit"
        )
    )

    if not upgraded and user.bank >= BANK_AUTO_UPGRADE_THRESHOLD * user.bank_capacity:
        _upgrade_bank_capacity(user)
        upgraded = True

    await session.commit()

    if upgraded:
        return True, f"Deposited ${amount:,}. Bank capacity grew to ${user.bank_capacity:,} to make room."
    return True, f"Deposited ${amount:,}."


async def withdraw(session: AsyncSession, user: User, amount: int) -> tuple[bool, str]:
    if amount <= 0:
        return False, "Withdraw amount must be positive."
    if amount > user.bank:
        return False, "You don't have that much stashed away."

    user.bank -= amount
    user.wallet += amount
    session.add(
        Transaction(
            user_id=user.discord_id, balance_type="bank", amount=-amount, reason="withdraw"
        )
    )
    session.add(
        Transaction(
            user_id=user.discord_id, balance_type="wallet", amount=amount, reason="withdraw"
        )
    )
    await session.commit()
    return True, f"Withdrew ${amount:,}."


async def wipe_user(session: AsyncSession, user_id: int) -> bool:
    """Permanently erases a profile — inventory, cooldowns, allies, brew, listings,
    the works. Returns False if there was nothing to wipe. Transaction rows (the
    audit log) are deliberately left alone even on a wipe. Not the ORM's cascade
    (relationship cascades don't reliably fire for every deletion path we care about
    here) — explicit bulk deletes for every table that references this user."""
    user = await session.get(User, user_id)
    if user is None:
        return False

    for model in (InventoryItem, PendingPhoto, Cooldown, Ally, GiftUsage, Brew):
        await session.execute(delete(model).where(model.user_id == user_id))
    await session.execute(delete(MarketListing).where(MarketListing.seller_id == user_id))

    await session.delete(user)
    await session.commit()
    return True
