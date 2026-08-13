from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import InventoryItem


async def _get_stack(session: AsyncSession, user_id: int, item_key: str) -> InventoryItem | None:
    """Finds the unequipped stack for a stackable item (components, consumables)."""
    stmt = select(InventoryItem).where(
        InventoryItem.user_id == user_id,
        InventoryItem.item_key == item_key,
        InventoryItem.equipped.is_(False),
    )
    return (await session.execute(stmt)).scalars().first()


async def add_item(session: AsyncSession, user_id: int, item_key: str, quantity: int = 1) -> None:
    stack = await _get_stack(session, user_id, item_key)
    if stack is not None:
        stack.quantity += quantity
    else:
        session.add(InventoryItem(user_id=user_id, item_key=item_key, quantity=quantity))
    await session.commit()


async def remove_item(session: AsyncSession, user_id: int, item_key: str, quantity: int = 1) -> bool:
    """Returns False (no changes made) if the user doesn't have enough to remove."""
    stack = await _get_stack(session, user_id, item_key)
    if stack is None or stack.quantity < quantity:
        return False

    stack.quantity -= quantity
    if stack.quantity <= 0:
        await session.delete(stack)
    await session.commit()
    return True


async def get_quantity(session: AsyncSession, user_id: int, item_key: str) -> int:
    stack = await _get_stack(session, user_id, item_key)
    return stack.quantity if stack is not None else 0


async def get_editable_row(session: AsyncSession, user_id: int, item_key: str) -> InventoryItem | None:
    """For admin edits (durability, upgrade level) where a player might own more than
    one copy of the same item — the equipped copy if there is one, else whichever
    copy has the highest durability, matching the same "best copy" convention
    gadget_service uses for /gadget equip/upgrade."""
    stmt = (
        select(InventoryItem)
        .where(InventoryItem.user_id == user_id, InventoryItem.item_key == item_key)
        .order_by(InventoryItem.equipped.desc(), InventoryItem.durability.desc())
    )
    return (await session.execute(stmt)).scalars().first()
