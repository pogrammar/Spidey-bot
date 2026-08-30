from __future__ import annotations

import datetime
import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Brew, User
from services.cooldowns import format_remaining, is_bypass_enabled
from services.economy import add_wallet
from services.inventory_service import add_item
from services.loot_tables import rand_range
from services.server_perks import NO_PERKS, ServerPerks

BREW_COST = 30
BREW_DURATION = datetime.timedelta(minutes=5)

# Quicker Lab Brewing (community server Level 10 perk). 5min -> 3min.
#
# Note the arithmetic isn't linear the way the copy makes it sound: -40% on the timer is
# +67% on throughput. 1.5min was rejected as the target because it undercuts Organic
# Webbing — brew fast enough and free vials stop being worth having (GAME_DESIGN.md 9.5).
QUICKER_BREW_DURATION = datetime.timedelta(minutes=3)

YIELD_RANGE = [2, 4]
MUTATION_CHANCE = 0.08
VIAL_ITEM_KEY = "web_fluid_vial"
MUTATION_ITEM_KEY = "unstable_web_fluid"


def brew_duration(perks: ServerPerks) -> datetime.timedelta:
    return QUICKER_BREW_DURATION if perks.quicker_brewing else BREW_DURATION


@dataclass
class CollectResult:
    vials: int
    mutated: bool


async def _get_active_brew(session: AsyncSession, user_id: int) -> Brew | None:
    stmt = select(Brew).where(Brew.user_id == user_id)
    return (await session.execute(stmt)).scalars().first()


async def get_brew_status(session: AsyncSession, user_id: int) -> Brew | None:
    return await _get_active_brew(session, user_id)


async def force_ready(session: AsyncSession, user_id: int) -> bool:
    """Admin override — instantly finishes an in-progress brew. Returns False if
    there's nothing brewing."""
    brew = await _get_active_brew(session, user_id)
    if brew is None:
        return False
    brew.ready_at = datetime.datetime.utcnow()
    await session.commit()
    return True


async def clear_brew(session: AsyncSession, user_id: int) -> bool:
    """Admin override — cancels a stuck brew outright (no refund; use force_ready
    instead if the goal is just to unblock /lab collect). Returns False if there's
    nothing brewing."""
    brew = await _get_active_brew(session, user_id)
    if brew is None:
        return False
    await session.delete(brew)
    await session.commit()
    return True


async def start_brew(
    session: AsyncSession, user: User, perks: ServerPerks = NO_PERKS
) -> tuple[bool, str]:
    """Quicker Lab Brewing is stamped into ready_at here and never re-read, so unlike the
    ally-decay perk there's no window this can be wrong about: the batch was started in the
    server, so the batch is quick. Leaving the server mid-brew doesn't slow it back down."""
    if await _get_active_brew(session, user.discord_id) is not None:
        return False, "You've already got a batch cooking. Check /lab status."
    if user.wallet < BREW_COST:
        return False, f"Brewing chemicals cost ${BREW_COST} and your wallet's short."

    duration = brew_duration(perks)
    await add_wallet(session, user, -BREW_COST, reason="brewing:start")
    ready_at = datetime.datetime.utcnow() + duration
    session.add(Brew(user_id=user.discord_id, ready_at=ready_at))
    await session.commit()
    return True, f"Batch started for ${BREW_COST}. Ready in {format_remaining(duration.total_seconds())}."


async def collect_brew(session: AsyncSession, user: User) -> tuple[bool, str, CollectResult | None]:
    brew = await _get_active_brew(session, user.discord_id)
    if brew is None:
        return False, "Nothing's brewing. Start one with /lab brew.", None

    now = datetime.datetime.utcnow()
    if brew.ready_at > now and not is_bypass_enabled(user.discord_id):
        remaining = (brew.ready_at - now).total_seconds()
        minutes = int(remaining // 60)
        return False, f"Still cooking — about {minutes} more minutes.", None

    vials = rand_range(YIELD_RANGE)
    mutated = random.random() < MUTATION_CHANCE

    await add_item(session, user.discord_id, VIAL_ITEM_KEY, vials)
    if mutated:
        await add_item(session, user.discord_id, MUTATION_ITEM_KEY, 1)

    await session.delete(brew)
    await session.commit()
    return True, "", CollectResult(vials=vials, mutated=mutated)
