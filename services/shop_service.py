from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import InventoryItem, Item, User
from services.economy import add_wallet
from services.inventory_service import add_item
from services.patreon_service import (
    GATED_ITEM_MIN_RANK,
    get_tier_rank,
    tier_requirement_label,
)
from services.patrol_service import (
    CAMERA_BRONZE_ITEM_KEY,
    CAMERA_FAMILY_KEYS,
    get_equipped_camera,
)
from services.server_perks import ServerPerks
from utils.icons import item_label

# GATED_ITEM_MIN_RANK moved to patreon_service on 2026-08-23 — it's re-checked at use
# time now, not just at purchase, and patrol_service (which consults it for cameras)
# can't import this module without creating a cycle. buy_item below is still the only
# thing that blocks a *purchase* on it.


async def list_shop_items(session: AsyncSession) -> list[Item]:
    stmt = select(Item).where(Item.price.is_not(None))
    return list((await session.execute(stmt)).scalars())


async def _get_tool_row(session: AsyncSession, user_id: int, item_key: str) -> InventoryItem | None:
    stmt = select(InventoryItem).where(
        InventoryItem.user_id == user_id, InventoryItem.item_key == item_key
    )
    return (await session.execute(stmt)).scalars().first()


async def install_tool(session: AsyncSession, user_id: int, item: Item) -> list[str]:
    """Put a tool in someone's hands the way a tool has to be held: equipped, at full
    durability, with any lower-tier sibling in its family deleted.

    Returns the item_keys it destroyed, so the caller can *say so*. That return value is
    not optional politeness: this function silently deletes paid-for gear, and a player
    who buys a $3,000 Gold body and is told only "Bought Gold-Grade Camera" will report
    the missing $1,000 Silver as a bug — which is the same class of report that produced
    the deletion in the first place. Callers that hand a tool to a player should render it.

    Extracted from buy_item on 2026-08-24 because it was the *only* implementation, and
    /admin inventory give-item wasn't using it — it called inventory_service.add_item,
    which is a stacking primitive for consumables. That created the row with
    equipped=False and durability=NULL, and touched no siblings, so an admin-granted
    Gold camera was completely inert (get_effective_camera reads only equipped rows)
    while the Silver one it was supposed to replace kept taking the photos. Every path
    that hands over a tool goes through here now.

    Does NOT commit — the caller owns the transaction, since both callers have other
    work in the same one.
    """
    scrapped: list[str] = []
    if item.key in CAMERA_FAMILY_KEYS:
        # Only one camera body survives an upgrade, and the retired one is deleted.
        #
        # Retiring used to mean unequipping and keeping the row. That was intended as a
        # lapsed-pledge fallback, but it never worked as one and it read as a bug: the row
        # is unreachable (get_equipped_camera filters on equipped, and there is no equip
        # command to turn it back on), get_effective_camera's lapse path falls back to
        # base-camera *stats* on whatever body IS equipped rather than to the retired row,
        # and /inventory rendered it with no annotation at all — economy_cog only marks a
        # tool line when it's equipped or tier-locked, so a $1,000 Silver sat next to the
        # Gold looking exactly like working gear. Deleted outright per the owner, 2026-08-24.
        #
        # Strictly LOWER tiers only. CAMERA_FAMILY_KEYS is ordered cheapest-first, so the
        # index IS the tier. Deleting in both directions would mean a $1,000 Silver purchase
        # destroys a $3,000 Gold body; buy_item refuses that purchase outright, and the
        # slice below is the second lock on it rather than a restatement of the first.
        incoming_tier = CAMERA_FAMILY_KEYS.index(item.key)
        stmt = select(InventoryItem).where(
            InventoryItem.user_id == user_id,
            InventoryItem.item_key.in_(CAMERA_FAMILY_KEYS[:incoming_tier]),
        )
        for retired in (await session.execute(stmt)).scalars():
            scrapped.append(retired.item_key)
            await session.delete(retired)

    existing = await _get_tool_row(session, user_id, item.key)
    if existing is not None:
        existing.quantity = 1
        existing.durability = item.max_durability
        existing.equipped = True
    else:
        session.add(
            InventoryItem(
                user_id=user_id,
                item_key=item.key,
                quantity=1,
                durability=item.max_durability,
                equipped=True,
            )
        )
    return scrapped


async def scrap_note(session: AsyncSession, scrapped: list[str]) -> str:
    """A trailing sentence naming the bodies install_tool destroyed, or "" if none.

    Kept next to install_tool rather than in a cog because both call sites need the same
    sentence, and the labels have to come from the database (Item.name) the same way
    every other item mention does.
    """
    if not scrapped:
        return ""
    labels = []
    for key in scrapped:
        row = await session.get(Item, key)
        labels.append(item_label(key, row.name if row is not None else key))
    return f" Stripped {' and '.join(labels)} for parts."


async def grant_bronze_camera(
    session: AsyncSession, user_id: int, perks: ServerPerks
) -> str | None:
    """Hands over the Bronze camera the community server's Level 10 role earns, once.

    Returns a sentence to show the player, or None if there was nothing to do — which is
    the overwhelmingly common case, since this is checked on a live role rather than
    recorded as claimed anywhere. Idempotency comes from the inventory itself: whoever
    already shoots on Bronze or better is left alone.

    Automatic rather than a claim button, per the owner: nothing in this track should make
    the player pick something. That decision is *why* this returns a sentence instead of
    just committing. install_tool deletes the base camera body, and its contract is that a
    caller handing over a tool has to say what was destroyed — a silent auto-grant would
    scrap a $150 camera with nothing rendered anywhere, which is the exact bug report that
    contract exists to prevent.

    Someone already on Silver or Gold is skipped rather than downgraded. install_tool only
    ever scraps *lower* tiers, so a grant here couldn't destroy their paid body — but it
    would demote a $1,000 camera to a free one, which is the same trade buy_item refuses
    outright. Nothing to grant is the right answer when the player already has better.

    Does not commit — see install_tool; the caller owns the transaction.
    """
    if not perks.bronze_camera:
        return None

    equipped = await get_equipped_camera(session, user_id)
    if equipped is not None and CAMERA_FAMILY_KEYS.index(
        equipped.item_key
    ) >= CAMERA_FAMILY_KEYS.index(CAMERA_BRONZE_ITEM_KEY):
        return None

    item = await session.get(Item, CAMERA_BRONZE_ITEM_KEY)
    if item is None:
        # data/items.json is seeded on every startup (db.seed), so this only happens if
        # the entry was removed. Silently doing nothing beats raising inside a
        # before_invoke hook that must never block the command the player actually ran.
        return None

    scrapped = await scrap_note(session, await install_tool(session, user_id, item))
    return (
        f"Level 10 in the server — {item_label(item.key, item.name)} is yours, fitted and "
        f"ready.{scrapped} It shoots at stock numbers if you ever lose the role, and comes "
        f"straight back with it."
    )


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
                f"Patreon perk. /patreon subscribe has the tiers; /patreon link connects a "
                f"pledge you already have."
            )

    if item.category == "tool":
        existing = await _get_tool_row(session, user.discord_id, item_key)
        if existing is not None and existing.equipped:
            return False, f"You've already got a working {item_label(item.key, item.name)} equipped."
        # A camera purchase deletes every lower-tier body (install_tool), so selling a
        # downgrade would quietly destroy the better one — $1,000 of Silver eating $3,000
        # of Gold. Refused rather than allowed-and-guarded: a downgrade was already a pure
        # loss before deletion (there's no equip command to switch back), it just wasn't a
        # destructive one, so nothing of value is being taken away by blocking it here.
        if item.key in CAMERA_FAMILY_KEYS:
            equipped_camera = await get_equipped_camera(session, user.discord_id)
            if equipped_camera is not None and CAMERA_FAMILY_KEYS.index(
                equipped_camera.item_key
            ) > CAMERA_FAMILY_KEYS.index(item.key):
                current = await session.get(Item, equipped_camera.item_key)
                current_label = item_label(
                    equipped_camera.item_key,
                    current.name if current is not None else equipped_camera.item_key,
                )
                return False, (
                    f"Your {current_label} is the better body — buying "
                    f"{item_label(item.key, item.name)} would scrap it for a downgrade."
                )

    if user.wallet < item.price:
        return False, f"{item_label(item.key, item.name)} costs ${item.price:,} and your wallet's short."

    await add_wallet(session, user, -item.price, reason=f"shop:buy:{item_key}")

    scrapped_note = ""
    if item.category == "tool":
        scrapped_note = await scrap_note(
            session, await install_tool(session, user.discord_id, item)
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
    return True, (
        f"Bought {item_label(item.key, item.name)} for ${item.price:,}.{scrapped_note}"
    )
