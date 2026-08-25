from __future__ import annotations

import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import InventoryItem, Item, User
from services.economy import add_wallet
from services.patreon_service import locked_item_keys
from utils.icons import item_label

GADGET_CATEGORY = "gadget"
MAX_UPGRADE_LEVEL = 3
MAX_EQUIPPED_GADGETS = 2  # a real loadout choice — which two you bring matters
UPGRADE_COST_MULTIPLIER = 0.6  # cost of upgrading to level N = price * MULTIPLIER * N

GADGET_BASE_BREAK_CHANCE = 0.05  # per crime encounter while equipped, before level scaling

# What each gadget actually does on patrol. `kind` is read by battle_service.py to
# decide which effect branch to apply; `magnitude` is the effect's strength.
# `bonus_per_level` is how many extra percentage points of trigger chance each
# upgrade level adds — tiered by gadget quality (higher unlock level = bigger bonus
# per level), so upgrading a better gadget pays off more, not the same flat amount
# for everything. web_shooters is deliberately left at the original flat rate: it's
# pure defense (blocks a hit for zero offensive damage), so making it trigger more
# often trades away kill-securing rounds rather than helping win — bumping its
# chance further would make it worse, not better.
#
# base_chance is balanced against effect strength, NOT against unlock level — that's
# why web_grabber (cash) sits at 0.55 while upshot (+50% XP) sits at 0.30. A big
# effect earns a low chance; a small one has to be reliable to be worth a slot at all
# (there are only MAX_EQUIPPED_GADGETS of them).
GADGET_EFFECTS: dict[str, dict] = {
    "web_shooters": {"kind": "negate_damage", "base_chance": 0.25, "bonus_per_level": 0.05},
    "web_grabber": {"kind": "bonus_donation", "base_chance": 0.55, "bonus_per_level": 0.11, "cash_range": [30, 70]},
    "ricochet_web": {"kind": "scavenge_boost", "base_chance": 0.44, "bonus_per_level": 0.14, "magnitude": 0.25},
    "upshot": {"kind": "bonus_xp", "base_chance": 0.30, "bonus_per_level": 0.145, "magnitude": 0.5},
    "concussion_burst": {"kind": "group_defense", "base_chance": 0.38, "bonus_per_level": 0.19, "magnitude": 0.5},
    # Arachnid+ Patreon-exclusive (gated per-key in patreon_service.GATED_ITEM_MIN_RANK,
    # enforced both at purchase and, since 2026-08-23, at use time via
    # list_usable_gadgets)
    # — mechanically just regular gadgets otherwise, same Select/button flow as the five above.
    #
    # Both were 0.20/0.12 until 2026-08-22, which made them the least reliable gadgets
    # in the game outside the deliberately-exempt web_shooters: 1 in 5 at purchase, and
    # only 56% fully upgraded, against a free ladder that starts as high as 0.55 and
    # tops out near 0.90. Spider Bots cost MORE than ricochet_web ($550 vs $500) and
    # fired at under half its rate. The original reasoning — "these are paid, so keep
    # them under the 0.25-0.55 baseline" — guarded the right thing the wrong way: what
    # keeps a paid gadget from being pay-to-win is a modest *effect*, not unreliable
    # *delivery*. Firing 4 times in 5 doesn't read as balanced, it reads as broken.
    #
    # Their effects are flat damage adds (+5-12 / +8-15) — the weakest kind in the
    # table, weaker than ricochet_web's scavenge boost or upshot's +50% XP — so by the
    # rule above they belong at the reliable end. 0.45 seats them just above
    # ricochet_web's 0.44 without reaching web_grabber's 0.55, and neither one takes
    # the ceiling from concussion_burst. bonus_per_level now follows the same
    # unlock-level tiering as everything else, interpolated off the free ladder
    # (unlock 5 -> 0.11, 10 -> 0.14, 15 -> 0.145), which the old flat 0.12 ignored.
    "spider_bots": {"kind": "bonus_damage", "base_chance": 0.45, "bonus_per_level": 0.13, "bonus_range": [5, 12]},
    "electric_webbing": {"kind": "shock_burst", "base_chance": 0.45, "bonus_per_level": 0.145, "bonus_range": [8, 15]},
}


@dataclass
class GadgetEffectResult:
    gadget_name: str
    kind: str
    magnitude: float | None = None
    cash_range: list[int] | None = None
    bonus_range: list[int] | None = None


@dataclass
class OwnedGadgetView:
    item_key: str
    name: str
    durability: int | None
    equipped: bool
    upgrade_level: int
    tier_locked: bool = False
    """Owned, and possibly still equipped, but switched off because the Patreon tier that
    unlocked it has lapsed (see list_usable_gadgets). Worth surfacing rather than hiding:
    a gadget that silently never fires reads as a bug, and the player needs somewhere that
    says why so they can free the slot or resubscribe."""


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
    locked = await locked_item_keys(session, user_id, {inv.item_key for inv, _ in rows})
    return [
        OwnedGadgetView(
            item_key=inv.item_key,
            name=name,
            durability=inv.durability,
            equipped=inv.equipped,
            upgrade_level=inv.upgrade_level,
            tier_locked=inv.item_key in locked,
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


async def list_all_owned_gadgets(session: AsyncSession, user_id: int) -> list[InventoryItem]:
    """Boss fights only — every distinct gadget the player owns gets a button, not
    just the 2 equipped ones (see MAX_EQUIPPED_GADGETS), so a maxed-out loadout is
    actually worth something at the fights that matter most. One row per item_key
    (the best copy — highest upgrade level, then highest durability — if they own
    more than one), same convention as _owned_copies()."""
    stmt = (
        select(InventoryItem)
        .join(Item, InventoryItem.item_key == Item.key)
        .where(InventoryItem.user_id == user_id, Item.category == GADGET_CATEGORY)
        .order_by(InventoryItem.item_key, InventoryItem.upgrade_level.desc(), InventoryItem.durability.desc())
    )
    rows = list((await session.execute(stmt)).scalars())
    best: dict[str, InventoryItem] = {}
    for row in rows:
        best.setdefault(row.item_key, row)
    return list(best.values())


async def list_usable_gadgets(
    session: AsyncSession, user_id: int, all_owned: bool = False
) -> list[InventoryItem]:
    """The gadgets that may actually *fire* for this user right now.

    Every context that uses a gadget should come through here rather than calling
    list_equipped_gadgets/list_all_owned_gadgets directly — those two report what's
    equipped and owned, and have to stay honest about it, because equip_gadget counts
    them against MAX_EQUIPPED_GADGETS.

    Ownership isn't enough for the Patreon-gated ones (GATED_ITEM_MIN_RANK). A gated
    gadget whose pledge has lapsed stays owned, equipped and listed — it just stops
    doing anything. Inert rather than deleted, for two reasons: resubscribing switches
    it straight back on instead of asking someone to re-buy gear they already paid real
    money for, and the equip slots stay stable. Hiding it from list_equipped_gadgets
    would free a slot, let them equip a third gadget, and hand them three live gadgets
    the moment they resubscribed.

    The live tier is what's read, never anything stored at purchase time, and that's
    what makes one check cover both halves of revocation: a cancelled pledge clears the
    stored tier (refresh_stale_links) and unlink_account deletes the row outright, so
    get_tier_rank returns TIER_RANK_NONE either way.
    """
    rows = await (
        list_all_owned_gadgets(session, user_id) if all_owned
        else list_equipped_gadgets(session, user_id)
    )
    locked = await locked_item_keys(session, user_id, {row.item_key for row in rows})
    if not locked:
        return rows
    return [row for row in rows if row.item_key not in locked]


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
        return False, (
            f"{item_label(item.key, item.name)} unlocks at reputation level {item.unlock_level}. "
            f"You're level {user.reputation_level}."
        )

    candidates = await _owned_copies(session, user.discord_id, gadget_key)
    if not candidates:
        return False, f"You don't own a {item_label(item.key, item.name)}. Buy one from /shop first."

    best = candidates[0]
    if best.equipped:
        return False, f"{item_label(item.key, item.name)} is already equipped."

    equipped = await list_equipped_gadgets(session, user.discord_id)
    if len(equipped) >= MAX_EQUIPPED_GADGETS:
        return (
            False,
            f"Both gadget slots are full. Unequip one first with /gadget unequip "
            f"(you've got {MAX_EQUIPPED_GADGETS} max).",
        )

    best.equipped = True
    await session.commit()
    return True, f"{item_label(item.key, item.name)} equipped ({best.durability}% durability)."


async def unequip_gadget(session: AsyncSession, user: User, gadget_key: str) -> tuple[bool, str]:
    equipped = await list_equipped_gadgets(session, user.discord_id)
    match = next((e for e in equipped if e.item_key == gadget_key), None)
    if match is None:
        return False, "That gadget isn't equipped."

    item = await session.get(Item, gadget_key)
    display = item_label(item.key, item.name) if item else gadget_key
    match.equipped = False
    await session.commit()
    return True, f"{display} unequipped."


async def upgrade_gadget(session: AsyncSession, user: User, gadget_key: str) -> tuple[bool, str]:
    equipped = await list_equipped_gadgets(session, user.discord_id)
    match = next((e for e in equipped if e.item_key == gadget_key), None)
    if match is None:
        return False, "That gadget isn't equipped. Equip it first with /gadget equip."
    if match.upgrade_level >= MAX_UPGRADE_LEVEL:
        return False, "That gadget's already fully upgraded."

    # Checked here rather than by swapping the list above for list_usable_gadgets, so this
    # can say what's actually wrong. Filtering it out would reuse "that gadget isn't
    # equipped" for a gadget sitting visibly in a slot, and this is a *spend* — charging
    # real cash to upgrade something that can't fire is the worst version of the bug.
    if await locked_item_keys(session, user.discord_id, {gadget_key}):
        return False, (
            "That gadget's inactive — the Patreon tier that unlocked it isn't active on "
            "your account any more. It's still yours, and it starts working again the "
            "moment the pledge is back. See /patreon status."
        )

    item = await session.get(Item, match.item_key)
    next_level = match.upgrade_level + 1
    cost = round(item.price * UPGRADE_COST_MULTIPLIER * next_level)
    if user.wallet < cost:
        return False, f"Upgrading to level {next_level} costs ${cost:,} and your wallet's short."

    await add_wallet(session, user, -cost, reason=f"gadget:upgrade:{item.key}")
    match.upgrade_level = next_level
    await session.commit()
    return True, f"{item_label(item.key, item.name)} upgraded to level {next_level} for ${cost:,}."


async def roll_gadget_effect(
    session: AsyncSession, user_id: int, gadget_key: str | None = None, all_owned: bool = False
) -> GadgetEffectResult | None:
    """Rolls whether a gadget's effect fires. Pass gadget_key when the player is
    choosing which one to use (battle); leave it None for passive contexts (the
    /tutoring and /bugle "close call" events), which pick randomly among whatever's
    equipped. Returns None if nothing's equipped, the requested one isn't equipped,
    or the roll simply didn't hit. all_owned=True (boss fights only) searches every
    owned gadget instead of just the 2 equipped ones."""
    equipped = await list_usable_gadgets(session, user_id, all_owned=all_owned)
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

    chance = min(0.9, effect["base_chance"] + effect["bonus_per_level"] * target.upgrade_level)
    if random.random() >= chance:
        return None

    item = await session.get(Item, target.item_key)
    return GadgetEffectResult(
        gadget_name=item_label(item.key, item.name),
        kind=effect["kind"],
        magnitude=effect.get("magnitude"),
        cash_range=effect.get("cash_range"),
        bonus_range=effect.get("bonus_range"),
    )


async def roll_gadget_wearout(
    session: AsyncSession,
    user_id: int,
    difficulty_multiplier: float,
    gadget_key: str | None = None,
    all_owned: bool = False,
) -> str | None:
    """Chance for a gadget to break. Pass gadget_key for the one just used in battle;
    leave None for passive contexts, which pick randomly among whatever's equipped.
    Returns the gadget's name if it broke, else None. all_owned=True (boss fights
    only) searches every owned gadget instead of just the 2 equipped ones.

    Goes through list_usable_gadgets, so a tier-locked gadget can't break — it isn't
    doing anything, and losing it while it's switched off would make lapsing destroy
    gear rather than just pause it."""
    equipped = await list_usable_gadgets(session, user_id, all_owned=all_owned)
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
    name = item_label(item.key, item.name) if item else target.item_key
    if target.quantity > 1:
        # One copy broke, not the stack. Deleting the row here destroyed every copy at
        # once — a quantity=3 row went to zero on a single break. shop_service never
        # stacks gadget purchases (each buy is its own quantity=1 row), so a stacked
        # gadget only arrives via an /admin grant or seeded data, which is exactly the
        # case where nobody would notice the loss and blame the right thing.
        target.quantity -= 1
    else:
        await session.delete(target)
    await session.commit()
    return name
