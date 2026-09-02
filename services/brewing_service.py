from __future__ import annotations

import datetime
import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Brew, User
from services.cooldowns import is_bypass_enabled
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


# Below this, a wait is described rather than counted. See format_brew_remaining.
BREW_SOON_SECONDS = 60


def format_brew_remaining(seconds: float) -> str:
    """How long is left on a batch, as a phrase that reads correctly both after "Ready in"
    and before "left" — "a few seconds", "about a minute", "about 4 minutes".

    Deliberately vaguer than cooldowns.format_remaining, which is a live countdown
    ("4m 32s") for things you are waiting to retry right now. A brew is a background timer
    you come back to, so the copy rounds; that was always the intent and this only fixes
    how it rounds.

    Both callers used to floor the seconds into whole minutes inline, which meant the last
    minute of every brew reported **"about 0 minutes"** — read by players as a stuck or
    broken timer rather than as "nearly done", and reported as such. Flooring also made the
    minute before that say "about 1 minutes". A phrase for the sub-minute case fixes the
    first and the singular fixes the second, and keeping both here rather than in the two
    call sites is what stops /lab status and /lab collect from drifting apart again."""
    seconds = max(0, int(seconds))
    if seconds < BREW_SOON_SECONDS:
        # No number at all on purpose: the exact count is worthless at this range (it's
        # stale the moment it renders) and "about 40 seconds" invites a stopwatch.
        return "a few seconds"
    minutes = round(seconds / 60)
    return "about a minute" if minutes == 1 else f"about {minutes} minutes"


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
    # Phrased, not counted, for the same reason /lab status is: this used to render
    # cooldowns.format_remaining, which spells a whole number of minutes "5m 0s" and then
    # disagreed with the "about 5 minutes left" the player saw a second later on /lab status.
    return True, f"Batch started for ${BREW_COST}. Ready in {format_brew_remaining(duration.total_seconds())}."


async def collect_brew(session: AsyncSession, user: User) -> tuple[bool, str, CollectResult | None]:
    brew = await _get_active_brew(session, user.discord_id)
    if brew is None:
        return False, "Nothing's brewing. Start one with /lab brew.", None

    now = datetime.datetime.utcnow()
    if brew.ready_at > now and not is_bypass_enabled(user.discord_id):
        remaining = (brew.ready_at - now).total_seconds()
        return False, f"Still cooking — {format_brew_remaining(remaining)} left.", None

    vials = rand_range(YIELD_RANGE)
    mutated = random.random() < MUTATION_CHANCE

    await add_item(session, user.discord_id, VIAL_ITEM_KEY, vials)
    if mutated:
        await add_item(session, user.discord_id, MUTATION_ITEM_KEY, 1)

    await session.delete(brew)
    await session.commit()
    return True, "", CollectResult(vials=vials, mutated=mutated)
