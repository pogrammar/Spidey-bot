from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User
from services.economy import add_wallet
from services.inventory_service import get_quantity, remove_item
from utils.icons import item_label

REPAIR_COST_PER_POINT = 6
SPANDEX_ITEM_KEY = "spandex_fabric"
ELECTRONICS_ITEM_KEY = "micro_electronics"
ELECTRONICS_THRESHOLD = 50  # missing integrity at/above this also needs Micro-Electronics
LOW_SUIT_WARNING_THRESHOLD = 30


@dataclass
class RepairResult:
    success: bool
    message: str
    cash_cost: int = 0
    restored: int = 0
    used_electronics: bool = False


async def repair_suit(session: AsyncSession, user: User) -> RepairResult:
    if user.eviction_meter >= 100:
        return RepairResult(
            False,
            "The landlord's changed the locks on your workbench. Clear your eviction status first — /apartment pay.",
        )

    missing = 100 - user.suit_integrity
    if missing <= 0:
        return RepairResult(False, "Suit's already in perfect shape.")

    needs_electronics = missing >= ELECTRONICS_THRESHOLD

    if await get_quantity(session, user.discord_id, SPANDEX_ITEM_KEY) < 1:
        return RepairResult(
            False,
            f"You're out of {item_label(SPANDEX_ITEM_KEY, 'Spandex Fabric')}. Scavenge some on patrol, "
            "or /shop buy spandex_fabric.",
        )

    if needs_electronics and await get_quantity(session, user.discord_id, ELECTRONICS_ITEM_KEY) < 1:
        return RepairResult(
            False,
            f"Damage this bad needs {item_label(ELECTRONICS_ITEM_KEY, 'Micro-Electronics')} you don't have. "
            "Scavenge some on patrol, or /shop buy micro_electronics.",
        )

    cash_cost = missing * REPAIR_COST_PER_POINT
    if user.wallet < cash_cost:
        return RepairResult(False, f"Repair runs ${cash_cost:,} and your wallet's short.")

    await add_wallet(session, user, -cash_cost, reason="workbench:repair")
    await remove_item(session, user.discord_id, SPANDEX_ITEM_KEY, 1)
    if needs_electronics:
        await remove_item(session, user.discord_id, ELECTRONICS_ITEM_KEY, 1)

    user.suit_integrity = 100
    await session.commit()

    return RepairResult(
        True, "Suit's back to 100%.", cash_cost=cash_cost, restored=missing, used_electronics=needs_electronics
    )


async def repair_readiness_warning(session: AsyncSession, user: User) -> str | None:
    """Called after /patrol so a beat-up suit with no way to fix it doesn't sneak up
    on someone — surfaces the gap before it becomes an emergency, not after."""
    if user.suit_integrity > LOW_SUIT_WARNING_THRESHOLD:
        return None

    missing = 100 - user.suit_integrity
    needs_electronics = missing >= ELECTRONICS_THRESHOLD

    if await get_quantity(session, user.discord_id, SPANDEX_ITEM_KEY) < 1:
        return (
            f"Suit integrity's at {user.suit_integrity}% and you're out of "
            f"{item_label(SPANDEX_ITEM_KEY, 'Spandex Fabric')}. Keep patrolling and you'll be fighting "
            "crime with zero protection — or /shop buy spandex_fabric."
        )

    if needs_electronics and await get_quantity(session, user.discord_id, ELECTRONICS_ITEM_KEY) < 1:
        return (
            f"Suit integrity's at {user.suit_integrity}% and this damage needs "
            f"{item_label(ELECTRONICS_ITEM_KEY, 'Micro-Electronics')} you don't have. Check the Trade Post, "
            "or /shop buy micro_electronics."
        )

    return None
