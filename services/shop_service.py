from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import InventoryItem, Item, User
from services.economy import add_wallet
from services.inventory_service import add_item
from services.patreon_service import (
    TIER_RANK_ARACHNID,
    TIER_RANK_SYMBIOTE,
    get_tier_rank,
    tier_requirement_label,
)
from services.patrol_service import CAMERA_FAMILY_KEYS
from utils.icons import item_label

# Patreon-gated purchasables, each mapped to the MINIMUM tier rank allowed to buy it.
# Everyone still *sees* these in the shop (same as any reputation-locked gadget) —
# only the purchase is blocked. list_shop_items applies no tier filter at all; this
# map is consulted exclusively by buy_item.
#
# A rank map rather than one set per tier: buy_item needs exactly one lookup no matter
# how many tiers exist, the refusal message names the tier that actually applies
# instead of hardcoding "Arachnid+", and GATED_ITEM_KEYS below derives from it so a
# new gated item can't be added here and silently omitted from /shop's branding or
# /patreon perks' ownership checklist. Compared with `<`, never `==` — Symbiote is a
# strict superset of Arachnid and must satisfy an Arachnid gate.
GATED_ITEM_MIN_RANK: dict[str, int] = {
    "spider_bots": TIER_RANK_ARACHNID,
    "electric_webbing": TIER_RANK_ARACHNID,
    "camera_silver": TIER_RANK_ARACHNID,
    "camera_gold": TIER_RANK_SYMBIOTE,
}

# For the four callers that only ask "is this item gated at all?" without caring which
# tier — /shop's branding note, the perk tag in battle, /patreon perks' ownership
# query. Deliberately NOT named ARACHNID_GATED_ITEM_KEYS any more (it was until
# 2026-08-22): the moment a Symbiote-gated item joins the map that name is a lie.
GATED_ITEM_KEYS = frozenset(GATED_ITEM_MIN_RANK)


async def list_shop_items(session: AsyncSession) -> list[Item]:
    stmt = select(Item).where(Item.price.is_not(None))
    return list((await session.execute(stmt)).scalars())


async def _get_tool_row(session: AsyncSession, user_id: int, item_key: str) -> InventoryItem | None:
    stmt = select(InventoryItem).where(
        InventoryItem.user_id == user_id, InventoryItem.item_key == item_key
    )
    return (await session.execute(stmt)).scalars().first()


async def buy_item(session: AsyncSession, user: User, item_key: str) -> tuple[bool, str]:
    item = await session.get(Item, item_key)
    if item is None or item.price is None:
        return False, "That's not something the shop sells."

    if item.category == "gadget" and item.unlock_level and user.reputation_level < item.unlock_level:
        return False, (
            f"{item_label(item.key, item.name)} unlocks at reputation level {item.unlock_level}. "
            f"You're level {user.reputation_level}."
        )

    min_rank = GATED_ITEM_MIN_RANK.get(item.key)
    if min_rank is not None:
        tier_rank = await get_tier_rank(session, user.discord_id)
        if tier_rank < min_rank:
            return False, (
                f"{item_label(item.key, item.name)} is a {tier_requirement_label(min_rank)} "
                f"Patreon perk. Subscribe and link your account with /patreon link to buy it."
            )

    if item.category == "tool":
        existing = await _get_tool_row(session, user.discord_id, item_key)
        if existing is not None and existing.equipped:
            return False, f"You've already got a working {item_label(item.key, item.name)} equipped."

    if user.wallet < item.price:
        return False, f"{item_label(item.key, item.name)} costs ${item.price:,} and your wallet's short."

    await add_wallet(session, user, -item.price, reason=f"shop:buy:{item_key}")

    if item.category == "tool":
        if item_key in CAMERA_FAMILY_KEYS:
            # Only one camera tier can ever be equipped at a time — buying either
            # one swaps it in and retires whatever camera you had equipped before.
            stmt = select(InventoryItem).where(
                InventoryItem.user_id == user.discord_id,
                InventoryItem.item_key.in_(CAMERA_FAMILY_KEYS),
                InventoryItem.item_key != item_key,
                InventoryItem.equipped.is_(True),
            )
            for other in (await session.execute(stmt)).scalars():
                other.equipped = False

        existing = await _get_tool_row(session, user.discord_id, item_key)
        if existing is not None:
            existing.quantity = 1
            existing.durability = item.max_durability
            existing.equipped = True
        else:
            session.add(
                InventoryItem(
                    user_id=user.discord_id,
                    item_key=item_key,
                    quantity=1,
                    durability=item.max_durability,
                    equipped=True,
                )
            )
    elif item.category == "gadget":
        # each purchase is its own instance (own durability, own upgrade level) —
        # never auto-equipped, and never merged into an existing stack, so buying a
        # spare after one breaks is a real thing you can do.
        session.add(
            InventoryItem(
                user_id=user.discord_id,
                item_key=item_key,
                quantity=1,
                durability=item.max_durability,
                equipped=False,
            )
        )
    else:
        await add_item(session, user.discord_id, item_key, 1)

    await session.commit()
    return True, f"Bought {item_label(item.key, item.name)} for ${item.price:,}."
