from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User
from services.economy import add_wallet
from services.inventory_service import get_quantity, remove_item
from services.patreon_service import TIER_RANK_NONE, TIER_RANK_SYMBIOTE
from utils.icons import item_label

REPAIR_COST_PER_POINT = 6
SPANDEX_ITEM_KEY = "spandex_fabric"
ELECTRONICS_ITEM_KEY = "micro_electronics"
ELECTRONICS_THRESHOLD = 50  # missing integrity at/above this also needs Micro-Electronics
LOW_SUIT_WARNING_THRESHOLD = 30

# Symbiote-only reskin (GAME_DESIGN.md §9.3). THE MECHANICS ABOVE ARE UNTOUCHED — same
# REPAIR_COST_PER_POINT, same single Spandex Fabric, same Micro-Electronics threshold, same
# eviction gate. Only the words change.
#
# The premise: for a Symbiote subscriber the thing on his back isn't a suit he sewed. It's
# alive, Peter doesn't understand it, and it *still* wants exactly the two components a
# fabric suit wanted — which is the joke, and the reason the reskin can't drift into
# implying a different cost. A subscriber who reads this as its own mechanic will go
# hunting for a shop item that doesn't exist, so every Symbiote string below names the same
# two components and points at the same two ways to get them.
#
# Deliberately NOT reskinned: the words "Suit Integrity". That label is the game's shared
# stat name and also renders in /balance, the boss gate and /admin profile — reskinning it
# in one panel of four would make the same number look like two different stats. What gets
# reskinned is the act of repairing, which is what was asked for.


def _is_symbiote(tier_rank: int) -> bool:
    return tier_rank >= TIER_RANK_SYMBIOTE


@dataclass
class RepairResult:
    success: bool
    message: str
    cash_cost: int = 0
    restored: int = 0
    used_electronics: bool = False


async def repair_suit(
    session: AsyncSession, user: User, tier_rank: int = TIER_RANK_NONE
) -> RepairResult:
    symbiote = _is_symbiote(tier_rank)
    spandex = item_label(SPANDEX_ITEM_KEY, "Spandex Fabric")
    electronics = item_label(ELECTRONICS_ITEM_KEY, "Micro-Electronics")

    if user.eviction_meter >= 100:
        return RepairResult(
            False,
            # Same gate, different reason it bites: a workbench you can't reach vs. nowhere
            # private to let the thing come apart and put itself back together.
            "You need somewhere private to let it unspool, and the landlord's changed the "
            "locks. Clear your eviction status first — /apartment pay."
            if symbiote
            else "The landlord's changed the locks on your workbench. Clear your eviction "
                 "status first — /apartment pay.",
        )

    missing = 100 - user.suit_integrity
    if missing <= 0:
        return RepairResult(
            False,
            "It's whole. Whatever it wants, it isn't asking for anything right now."
            if symbiote
            else "Suit's already in perfect shape.",
        )

    needs_electronics = missing >= ELECTRONICS_THRESHOLD

    if await get_quantity(session, user.discord_id, SPANDEX_ITEM_KEY) < 1:
        return RepairResult(
            False,
            f"It's asking for {spandex} and you have none. You don't know why something "
            "from another planet wants stretch fabric — it won't close up without it. "
            "Scavenge some on patrol, or /shop buy spandex_fabric."
            if symbiote
            else f"You're out of {spandex}. Scavenge some on patrol, "
                 "or /shop buy spandex_fabric.",
        )

    if needs_electronics and await get_quantity(session, user.discord_id, ELECTRONICS_ITEM_KEY) < 1:
        return RepairResult(
            False,
            f"Torn this deep it wants {electronics} too, and you have none. Whatever it "
            "does with circuitry, it isn't explaining. Scavenge some on patrol, or "
            "/shop buy micro_electronics."
            if symbiote
            else f"Damage this bad needs {electronics} you don't have. "
                 "Scavenge some on patrol, or /shop buy micro_electronics.",
        )

    cash_cost = missing * REPAIR_COST_PER_POINT
    if user.wallet < cash_cost:
        return RepairResult(
            False,
            f"Giving it what it's asking for runs ${cash_cost:,} and your wallet's short."
            if symbiote
            else f"Repair runs ${cash_cost:,} and your wallet's short.",
        )

    await add_wallet(session, user, -cash_cost, reason="workbench:repair")
    await remove_item(session, user.discord_id, SPANDEX_ITEM_KEY, 1)
    if needs_electronics:
        await remove_item(session, user.discord_id, ELECTRONICS_ITEM_KEY, 1)

    user.suit_integrity = 100
    await session.commit()

    return RepairResult(
        True,
        "It takes what you offered, drinks it in, and knits itself shut over the gaps. "
        "Back to 100% — and you're still no closer to knowing how."
        if symbiote
        else "Suit's back to 100%.",
        cash_cost=cash_cost,
        restored=missing,
        used_electronics=needs_electronics,
    )


async def repair_readiness_warning(
    session: AsyncSession, user: User, tier_rank: int = TIER_RANK_NONE
) -> str | None:
    """Called after /patrol so a beat-up suit with no way to fix it doesn't sneak up
    on someone — surfaces the gap before it becomes an emergency, not after."""
    if user.suit_integrity > LOW_SUIT_WARNING_THRESHOLD:
        return None

    symbiote = _is_symbiote(tier_rank)
    missing = 100 - user.suit_integrity
    needs_electronics = missing >= ELECTRONICS_THRESHOLD
    spandex = item_label(SPANDEX_ITEM_KEY, "Spandex Fabric")
    electronics = item_label(ELECTRONICS_ITEM_KEY, "Micro-Electronics")

    if await get_quantity(session, user.discord_id, SPANDEX_ITEM_KEY) < 1:
        if symbiote:
            return (
                f"It's down to {user.suit_integrity}% and it's been asking for {spandex} "
                "you don't have. Keep patrolling like this and there'll be nothing between "
                "you and the city — or /shop buy spandex_fabric."
            )
        return (
            f"Suit integrity's at {user.suit_integrity}% and you're out of "
            f"{spandex}. Keep patrolling and you'll be fighting "
            "crime with zero protection — or /shop buy spandex_fabric."
        )

    if needs_electronics and await get_quantity(session, user.discord_id, ELECTRONICS_ITEM_KEY) < 1:
        if symbiote:
            return (
                f"It's down to {user.suit_integrity}% and torn deep enough that it wants "
                f"{electronics} you don't have. Check the Trade Post, or "
                "/shop buy micro_electronics."
            )
        return (
            f"Suit integrity's at {user.suit_integrity}% and this damage needs "
            f"{electronics} you don't have. Check the Trade Post, "
            "or /shop buy micro_electronics."
        )

    return None
