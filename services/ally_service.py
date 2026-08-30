from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Ally, GiftUsage, Item, User
from services.biomorphic_service import ACTIVITY_ALLY_VISIT, AmbientScavenge, roll_ambient_scavenge
from services.inventory_service import remove_item
from services.patreon_service import (
    TIER_RANK_ARACHNID,
    TIER_RANK_NONE,
    get_tier_rank,
)
from services.server_perks import NO_PERKS, ServerPerks
from utils.icons import item_label

ALLY_NAMES = {"aunt_may": "Aunt May", "mj": "MJ"}

# Decays on purpose — this needs to be a thing you actually check on, not a number
# that quietly takes care of itself. Written as a full-drain duration rather than a
# raw per-hour rate because the duration is the actual design decision; the rate is
# just what falls out of it. At 24h: thriving (70+) reaches neglected (<30) after
# 9.6h away, and a full meter drops out of thriving after 7.2h.
FULL_DECAY_HOURS = 24.0
DECAY_PER_HOUR = 100 / FULL_DECAY_HOURS  # 4.17/hour

# Supportive Allies (server Level 10 perk) — on the 24h baseline this stretches the full
# drain to ~34h, and thriving->neglected from 9.6h to ~13.7h.
#
# Mutually exclusive with Higher Reputation XP, enforced in ServerPerks._pair rather than
# here: held together they'd compound, because a longer thriving window is itself an XP
# bonus via reputation_xp_multiplier below.
SUPPORTIVE_ALLIES_DECAY_MULTIPLIER = 0.7

# The Arachnid tier's one drawback. The narrative isn't neglect and it isn't the
# allies being needy — they're holding onto Peter Parker on purpose, because the
# further the bond takes him the less of him comes back. Visiting is what keeps
# Peter *Peter* instead of letting the thing underneath off its leash. Same reason
# the cost scales with the tier: the deeper the bond, the harder they have to hold.
# +50% takes the full drain from 24h down to 16h (24 / 1.5 = 16), and
# thriving->neglected from 9.6h to 6.4h. Always-on for tier_rank >= ARACHNID, no
# choice involved — unlike Supportive Allies above, this isn't opt-in.
ARACHNID_ALLY_DECAY_INCREASE = 0.5

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

# Visiting deliberately does NOT raise city crime. /tutoring is the single source of
# crime_level now, and /patrol the single sink — keeping it to one lever each is what
# makes the meter legible. A visit still costs you real time (it blocks /patrol via
# the shared "busy" lock), which is cost enough on its own.


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
    # Biomorphic Webbing's ambient pickup (Symbiote+). None both for non-subscribers and
    # for a missed roll — see biomorphic_service.roll_ambient_scavenge.
    scavenged: AmbientScavenge | None = None


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


async def _decayed_happiness(
    session: AsyncSession, user_id: int, ally: Ally, perks: ServerPerks = NO_PERKS
) -> int:
    """Happiness right now, integrated over the time since the last visit.

    Two rate modifiers, and they are deliberately sourced differently:

    - Supportive Allies comes from `perks`, so it only applies to a command run inside the
      perks guild. That means the same ally can read slightly higher in-server than it
      does in a DM at the same instant, because the perk applies to the *reading*, not to
      the window. That's the honest consequence of a guild-scoped perk on a
      time-integrated value: the alternative is stamping a rate on the row at visit time
      (a new column and a migration), which would be worth doing only if players start
      mixing contexts enough to notice.
    - The Arachnid drawback reads the live tier directly and NOT perks.tier_rank, which
      would be TIER_RANK_NONE outside the guild. Perks are guild-scoped; a drawback the
      subscriber accepted is not, and letting someone shed it by running /ally in a DM
      would make the tier strictly better outside the server than in it.
    """
    decay_rate = DECAY_PER_HOUR
    if perks.supportive_allies:
        decay_rate *= SUPPORTIVE_ALLIES_DECAY_MULTIPLIER
    if await get_tier_rank(session, user_id) >= TIER_RANK_ARACHNID:
        decay_rate *= 1 + ARACHNID_ALLY_DECAY_INCREASE
    hours_elapsed = (datetime.datetime.utcnow() - ally.last_visited_at).total_seconds() / 3600
    decayed = ally.banked_happiness - decay_rate * hours_elapsed
    return max(0, min(100, round(decayed)))


async def get_current_happiness(
    session: AsyncSession, user_id: int, ally_key: str, perks: ServerPerks = NO_PERKS
) -> int:
    ally = await _get_or_create_ally(session, user_id, ally_key)
    return await _decayed_happiness(session, user_id, ally, perks)


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


async def _all_happiness(
    session: AsyncSession, user_id: int, perks: ServerPerks = NO_PERKS
) -> list[int]:
    return [await get_current_happiness(session, user_id, key, perks) for key in ALLY_NAMES]


async def reputation_xp_multiplier(
    session: AsyncSession, user_id: int, perks: ServerPerks = NO_PERKS
) -> float:
    """Both Aunt May and MJ thriving (>=70) sharpens his focus — bonus reputation XP
    from /patrol and /tutoring. Requires both, not just one, to actually earn it.

    Takes perks because Supportive Allies decides how much has decayed, and this band is
    the reason the two perks can't be held together — see ServerPerks._pair."""
    happiness = await _all_happiness(session, user_id, perks)
    if all(h >= THRIVING_HAPPINESS_THRESHOLD for h in happiness):
        return XP_BONUS_MULTIPLIER
    return 1.0


async def earnings_penalty_multiplier(
    session: AsyncSession, user_id: int, perks: ServerPerks = NO_PERKS
) -> float:
    """Neglecting either one (<30) costs focus — worse Bugle photos, worse tutoring
    sessions, both paying out less. Either relationship suffering is enough to apply."""
    happiness = await _all_happiness(session, user_id, perks)
    if any(h < LOW_HAPPINESS_THRESHOLD for h in happiness):
        return EARNINGS_PENALTY_MULTIPLIER
    return 1.0


async def list_gift_items(session: AsyncSession) -> list[Item]:
    stmt = select(Item).where(Item.category == "gift")
    return list((await session.execute(stmt)).scalars())


async def visit_ally(
    session: AsyncSession, user: User, ally_key: str, gift_key: str | None,
    tier_rank: int = TIER_RANK_NONE, perks: ServerPerks = NO_PERKS,
) -> tuple[bool, str, VisitResult | None]:
    if ally_key not in ALLY_NAMES:
        return False, "Never heard of them.", None

    ally = await _get_or_create_ally(session, user.discord_id, ally_key)
    current = await _decayed_happiness(session, user.discord_id, ally, perks)
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

    # After the commit, deliberately: a bonus pickup must never be able to roll back a
    # visit whose gift has already been consumed and happiness already banked.
    scavenged = await roll_ambient_scavenge(
        session, user.discord_id, tier_rank, ACTIVITY_ALLY_VISIT
    )

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
            scavenged=scavenged,
        ),
    )
