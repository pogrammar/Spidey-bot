from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Ally, GiftUsage, Item, User
from services.inventory_service import remove_item
from utils.icons import item_label

ALLY_NAMES = {"aunt_may": "Aunt May", "mj": "MJ"}

# Decays fast on purpose — this needs to be a thing you actually check on, not a
# number that quietly takes care of itself. At 6/hour, thriving (70+) erodes to
# neglected (<30) in under 7 hours of not showing up.
DECAY_PER_HOUR = 6.0

PLAIN_VISIT_BOOST = 20  # a gift-free visit — free, modest, resets gift burnout
LOW_HAPPINESS_THRESHOLD = 30  # any ally below this = neglected
THRIVING_HAPPINESS_THRESHOLD = 70  # both allies at/above this = thriving

# Bring the SAME gift over and over and it's worth less each time — 1st time full
# value, then multiplied down every repeat, floored so it's never literally zero.
GIFT_DIMINISH_RATE = 0.7
MIN_GIFT_MULTIPLIER = 0.2

# Bring ANY gift on too many visits in a row and it stops reading as thoughtful —
# past the streak threshold, gifts backfire outright instead of helping. A single
# gift-free visit resets the streak.
GIFT_STREAK_THRESHOLD = 3
GIFT_BACKFIRE_PENALTY = 15

# a visit takes real time, like /tutoring — and the longer you've let things slide,
# the longer it takes to patch up: quick check-in when happy, a real conversation
# when they're actually hurt. Blocks /patrol the same way tutoring does (shared
# "busy" cooldown key).
MIN_VISIT_SECONDS = 30
MAX_VISIT_SECONDS = 180

# neglected (either ally < 30): worse focus, Bugle photos and tutoring pay less
# thriving (both allies >= 70): head's clear, reputation grows faster
EARNINGS_PENALTY_MULTIPLIER = 0.8
XP_BONUS_MULTIPLIER = 1.2


def visit_duration_seconds(happiness_before_visit: int) -> int:
    fraction_neglected = (100 - happiness_before_visit) / 100
    return round(MIN_VISIT_SECONDS + fraction_neglected * (MAX_VISIT_SECONDS - MIN_VISIT_SECONDS))


@dataclass
class VisitResult:
    ally_key: str
    new_happiness: int
    happiness_delta: int
    gift_name: str | None = None
    backfired: bool = False
    visit_seconds: int = 0


async def _get_or_create_ally(session: AsyncSession, user_id: int, ally_key: str) -> Ally:
    ally = await session.get(Ally, (user_id, ally_key))
    if ally is None:
        ally = Ally(user_id=user_id, ally_key=ally_key)
        session.add(ally)
        await session.commit()
    return ally


async def _get_or_create_gift_usage(
    session: AsyncSession, user_id: int, ally_key: str, gift_key: str
) -> GiftUsage:
    usage = await session.get(GiftUsage, (user_id, ally_key, gift_key))
    if usage is None:
        usage = GiftUsage(user_id=user_id, ally_key=ally_key, gift_key=gift_key)
        session.add(usage)
        await session.commit()
    return usage


def _decayed_happiness(ally: Ally) -> int:
    hours_elapsed = (datetime.datetime.utcnow() - ally.last_visited_at).total_seconds() / 3600
    decayed = ally.banked_happiness - DECAY_PER_HOUR * hours_elapsed
    return max(0, min(100, round(decayed)))


async def get_current_happiness(session: AsyncSession, user_id: int, ally_key: str) -> int:
    ally = await _get_or_create_ally(session, user_id, ally_key)
    return _decayed_happiness(ally)


async def set_happiness(session: AsyncSession, user_id: int, ally_key: str, value: int) -> int:
    """Admin override — banks the value directly and resets the decay clock, same as
    a real visit does, so it reads correctly on the next /ally check."""
    ally = await _get_or_create_ally(session, user_id, ally_key)
    ally.banked_happiness = max(0, min(100, value))
    ally.last_visited_at = datetime.datetime.utcnow()
    await session.commit()
    return ally.banked_happiness


async def reset_ally(session: AsyncSession, user_id: int, ally_key: str) -> None:
    """Clears gift-streak and gift-usage history for one ally — does not touch
    happiness itself, just the diminishing-returns/backfire tracking around gifts."""
    ally = await _get_or_create_ally(session, user_id, ally_key)
    ally.consecutive_gift_visits = 0

    stmt = select(GiftUsage).where(GiftUsage.user_id == user_id, GiftUsage.ally_key == ally_key)
    for usage in (await session.execute(stmt)).scalars():
        await session.delete(usage)
    await session.commit()


async def _all_happiness(session: AsyncSession, user_id: int) -> list[int]:
    return [await get_current_happiness(session, user_id, key) for key in ALLY_NAMES]


async def reputation_xp_multiplier(session: AsyncSession, user_id: int) -> float:
    """Both Aunt May and MJ thriving (>=70) sharpens his focus — bonus reputation XP
    from /patrol and /tutoring. Requires both, not just one, to actually earn it."""
    happiness = await _all_happiness(session, user_id)
    if all(h >= THRIVING_HAPPINESS_THRESHOLD for h in happiness):
        return XP_BONUS_MULTIPLIER
    return 1.0


async def earnings_penalty_multiplier(session: AsyncSession, user_id: int) -> float:
    """Neglecting either one (<30) costs focus — worse Bugle photos, worse tutoring
    sessions, both paying out less. Either relationship suffering is enough to apply."""
    happiness = await _all_happiness(session, user_id)
    if any(h < LOW_HAPPINESS_THRESHOLD for h in happiness):
        return EARNINGS_PENALTY_MULTIPLIER
    return 1.0


async def list_gift_items(session: AsyncSession) -> list[Item]:
    stmt = select(Item).where(Item.category == "gift")
    return list((await session.execute(stmt)).scalars())


async def visit_ally(
    session: AsyncSession, user: User, ally_key: str, gift_key: str | None
) -> tuple[bool, str, VisitResult | None]:
    if ally_key not in ALLY_NAMES:
        return False, "Never heard of them.", None

    ally = await _get_or_create_ally(session, user.discord_id, ally_key)
    current = _decayed_happiness(ally)
    visit_seconds = visit_duration_seconds(current)

    gift_name: str | None = None
    backfired = False

    if gift_key:
        gift_item = await session.get(Item, gift_key)
        if gift_item is None or gift_item.category != "gift":
            return False, "That's not a gift.", None
        if not await remove_item(session, user.discord_id, gift_key, 1):
            return False, f"You don't have a {item_label(gift_key, gift_item.name)}. Buy one from /shop first.", None

        gift_name = item_label(gift_key, gift_item.name)

        if ally.consecutive_gift_visits >= GIFT_STREAK_THRESHOLD:
            backfired = True
            boost = -GIFT_BACKFIRE_PENALTY
        else:
            usage = await _get_or_create_gift_usage(session, user.discord_id, ally_key, gift_key)
            multiplier = max(MIN_GIFT_MULTIPLIER, GIFT_DIMINISH_RATE**usage.times_given)
            boost = round(gift_item.happiness_boost * multiplier)
            usage.times_given += 1

        ally.consecutive_gift_visits += 1
    else:
        boost = PLAIN_VISIT_BOOST
        ally.consecutive_gift_visits = 0

    ally.banked_happiness = max(0, min(100, current + boost))
    ally.last_visited_at = datetime.datetime.utcnow()
    await session.commit()

    return (
        True,
        "",
        VisitResult(
            ally_key=ally_key,
            new_happiness=ally.banked_happiness,
            happiness_delta=boost,
            gift_name=gift_name,
            backfired=backfired,
            visit_seconds=visit_seconds,
        ),
    )
