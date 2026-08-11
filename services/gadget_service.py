from __future__ import annotations

import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import InventoryItem, Item, User
from services.economy import add_wallet

GADGET_CATEGORY = "gadget"
MAX_UPGRADE_LEVEL = 3
MAX_EQUIPPED_GADGETS = 2  # a real loadout choice — which two you bring matters
UPGRADE_COST_MULTIPLIER = 0.6  # cost of upgrading to level N = price * MULTIPLIER * N
UPGRADE_CHANCE_BONUS_PER_LEVEL = 0.05  # each upgrade level adds +5 percentage points to trigger chance

GADGET_BASE_BREAK_CHANCE = 0.05  # per crime encounter while equipped, before level scaling

# What each gadget actually does on patrol. `kind` is read by battle_service.py to
# decide which effect branch to apply; `magnitude` is the effect's strength.
GADGET_EFFECTS: dict[str, dict] = {
    "web_shooters": {"kind": "negate_damage", "base_chance": 0.25},
    "web_grabber": {"kind": "bonus_donation", "base_chance": 0.20, "cash_range": [30, 70]},
    "ricochet_web": {"kind": "scavenge_boost", "base_chance": 0.30, "magnitude": 0.25},
    "upshot": {"kind": "bonus_xp", "base_chance": 0.25, "magnitude": 0.5},
    "concussion_burst": {"kind": "group_defense", "base_chance": 0.30, "magnitude": 0.5},
}


@dataclass
class GadgetEffectResult:
    gadget_name: str
    kind: str
    magnitude: float | None = None
    cash_range: list[int] | None = None


@dataclass
class OwnedGadgetView:
    item_key: str
    name: str
    durability: int | None
    equipped: bool
    upgrade_level: int


async def list_all_gadgets(session: AsyncSession) -> list[Item]:
    stmt = select(Item).where(Item.category == GADGET_CATEGORY).order_by(Item.price)
    return list((await session.execute(stmt)).scalars())


async def list_owned_gadget_views(session: AsyncSession, user_id: int) -> list[OwnedGadgetView]:
    """Plain-value rows built while the session is open — safe to use after it closes."""
    stmt = (
        select(InventoryItem, Item.name)
        .join(Item, InventoryItem.item_key == Item.key)
        .where(InventoryItem.user_id == user_id, Item.category == GADGET_CATEGORY)
        .order_by(InventoryItem.item_key, InventoryItem.durability.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        OwnedGadgetView(
            item_key=inv.item_key,
            name=name,
            durability=inv.durability,
            equipped=inv.equipped,
            upgrade_level=inv.upgrade_level,
        )
        for inv, name in rows
    ]


async def list_equipped_gadgets(session: AsyncSession, user_id: int) -> list[InventoryItem]:
    """Up to MAX_EQUIPPED_GADGETS rows — this is now a loadout, not a single slot."""
    stmt = (
        select(InventoryItem)
        .join(Item, InventoryItem.item_key == Item.key)
        .where(
            InventoryItem.user_id == user_id,
            Item.category == GADGET_CATEGORY,
            InventoryItem.equipped.is_(True),
        )
    )
    return list((await session.execute(stmt)).scalars())


async def _owned_copies(session: AsyncSession, user_id: int, gadget_key: str) -> list[InventoryItem]:
    stmt = (
        select(InventoryItem)
        .where(InventoryItem.user_id == user_id, InventoryItem.item_key == gadget_key)
        .order_by(InventoryItem.durability.desc())
    )
    return list((await session.execute(stmt)).scalars())


async def equip_gadget(session: AsyncSession, user: User, gadget_key: str) -> tuple[bool, str]:
    item = await session.get(Item, gadget_key)
    if item is None or item.category != GADGET_CATEGORY:
        return False, "That's not a gadget."
    if item.unlock_level and user.reputation_level < item.unlock_level:
        return False, f"{item.name} unlocks at reputation level {item.unlock_level}. You're level {user.reputation_level}."

    candidates = await _owned_copies(session, user.discord_id, gadget_key)
    if not candidates:
        return False, f"You don't own a {item.name}. Buy one from /shop first."

    best = candidates[0]
    if best.equipped:
        return False, f"{item.name} is already equipped."

    equipped = await list_equipped_gadgets(session, user.discord_id)
    if len(equipped) >= MAX_EQUIPPED_GADGETS:
        return (
            False,
            f"Both gadget slots are full. Unequip one first with /gadget unequip "
            f"(you've got {MAX_EQUIPPED_GADGETS} max).",
        )

    best.equipped = True
    await session.commit()
    return True, f"{item.name} equipped ({best.durability}% durability)."


async def unequip_gadget(session: AsyncSession, user: User, gadget_key: str) -> tuple[bool, str]:
    equipped = await list_equipped_gadgets(session, user.discord_id)
    match = next((e for e in equipped if e.item_key == gadget_key), None)
    if match is None:
        return False, "That gadget isn't equipped."

    item = await session.get(Item, gadget_key)
    match.equipped = False
    await session.commit()
    return True, f"{item.name if item else gadget_key} unequipped."


async def upgrade_gadget(session: AsyncSession, user: User, gadget_key: str) -> tuple[bool, str]:
    equipped = await list_equipped_gadgets(session, user.discord_id)
    match = next((e for e in equipped if e.item_key == gadget_key), None)
    if match is None:
        return False, "That gadget isn't equipped. Equip it first with /gadget equip."
    if match.upgrade_level >= MAX_UPGRADE_LEVEL:
        return False, "That gadget's already fully upgraded."

    item = await session.get(Item, match.item_key)
    next_level = match.upgrade_level + 1
    cost = round(item.price * UPGRADE_COST_MULTIPLIER * next_level)
    if user.wallet < cost:
        return False, f"Upgrading to level {next_level} costs ${cost:,} and your wallet's short."

    await add_wallet(session, user, -cost, reason=f"gadget:upgrade:{item.key}")
    match.upgrade_level = next_level
    await session.commit()
    return True, f"{item.name} upgraded to level {next_level} for ${cost:,}."


async def roll_gadget_effect(
    session: AsyncSession, user_id: int, gadget_key: str | None = None
) -> GadgetEffectResult | None:
    """Rolls whether a gadget's effect fires. Pass gadget_key when the player is
    choosing which one to use (battle); leave it None for passive contexts (the
    /tutoring and /bugle "close call" events), which pick randomly among whatever's
    equipped. Returns None if nothing's equipped, the requested one isn't equipped,
    or the roll simply didn't hit."""
    equipped = await list_equipped_gadgets(session, user_id)
    if not equipped:
        return None

    if gadget_key is not None:
        target = next((e for e in equipped if e.item_key == gadget_key), None)
    else:
        target = random.choice(equipped)

    if target is None:
        return None

    effect = GADGET_EFFECTS.get(target.item_key)
    if effect is None:
        return None

    chance = min(0.9, effect["base_chance"] + UPGRADE_CHANCE_BONUS_PER_LEVEL * target.upgrade_level)
    if random.random() >= chance:
        return None

    item = await session.get(Item, target.item_key)
    return GadgetEffectResult(
        gadget_name=item.name,
        kind=effect["kind"],
        magnitude=effect.get("magnitude"),
        cash_range=effect.get("cash_range"),
    )


async def roll_gadget_wearout(
    session: AsyncSession, user_id: int, difficulty_multiplier: float, gadget_key: str | None = None
) -> str | None:
    """Chance for a gadget to break. Pass gadget_key for the one just used in battle;
    leave None for passive contexts, which pick randomly among whatever's equipped.
    Returns the gadget's name if it broke, else None."""
    equipped = await list_equipped_gadgets(session, user_id)
    if not equipped:
        return None

    if gadget_key is not None:
        target = next((e for e in equipped if e.item_key == gadget_key), None)
    else:
        target = random.choice(equipped)

    if target is None:
        return None

    chance = min(0.9, GADGET_BASE_BREAK_CHANCE * difficulty_multiplier)
    if random.random() >= chance:
        return None

    item = await session.get(Item, target.item_key)
    await session.delete(target)
    return item.name if item else target.item_key
