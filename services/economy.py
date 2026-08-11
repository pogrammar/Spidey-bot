from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import InventoryItem, Item, Transaction, User

STARTER_CAMERA_KEY = "camera"

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


async def add_reputation(session: AsyncSession, user: User, xp: int) -> None:
    user.reputation_xp += max(0, xp)
    await session.commit()


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
