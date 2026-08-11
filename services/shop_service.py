from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import InventoryItem, Item, User
from services.economy import add_wallet
from services.inventory_service import add_item


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
        return False, f"{item.name} unlocks at reputation level {item.unlock_level}. You're level {user.reputation_level}."

    if item.category == "tool":
        existing = await _get_tool_row(session, user.discord_id, item_key)
        if existing is not None and existing.equipped:
            return False, f"You've already got a working {item.name} equipped."

    if user.wallet < item.price:
        return False, f"{item.name} costs ${item.price:,} and your wallet's short."

    await add_wallet(session, user, -item.price, reason=f"shop:buy:{item_key}")

    if item.category == "tool":
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
    return True, f"Bought {item.name} for ${item.price:,}."
