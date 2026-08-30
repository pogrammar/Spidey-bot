from __future__ import annotations

import random
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User
from services.ally_service import earnings_penalty_multiplier, reputation_xp_multiplier
from services.biomorphic_service import ACTIVITY_TUTORING, AmbientScavenge, roll_ambient_scavenge
from services.economy import add_reputation, add_wallet
from services.gadget_service import roll_gadget_effect
from services.loot_tables import LOOT_TABLES, rand_range
from services.patreon_service import TIER_RANK_NONE
from services.server_perks import NO_PERKS, ServerPerks

TUTORING_LOCK_SECONDS = 2 * 60

# Rare mid-session emergency — a "secret usage" moment for equipped gadgets outside
# of patrol. A crisis breaks out nearby; handle it clean with a gadget, or eat a
# penalty for having to duck out and come back rattled.
JAM_CHANCE = 0.12
JAM_PENALTY_CRIME_RISE = 10
JAM_HANDLED_CASH_RANGE = [15, 35]


@dataclass
class TutoringResult:
    cash: int
    xp: int
    crime_rise: int
    new_crime_level: int
    ally_xp_bonus: bool = False
    ally_earnings_penalty: bool = False
    jam_flavor: str | None = None
    jam_handled: bool = False
    jam_cash: int = 0
    # Biomorphic Webbing's ambient pickup (Symbiote+). None both for non-subscribers and
    # for a missed roll — see biomorphic_service.roll_ambient_scavenge.
    scavenged: AmbientScavenge | None = None


async def run_tutoring_session(
    session: AsyncSession, user: User, tier_rank: int = TIER_RANK_NONE,
    perks: ServerPerks = NO_PERKS,
) -> TutoringResult:
    data = LOOT_TABLES["tutoring"]

    xp_multiplier = await reputation_xp_multiplier(session, user.discord_id, perks)
    earnings_multiplier = await earnings_penalty_multiplier(session, user.discord_id, perks)

    cash = round(rand_range(data["cash"]) * user.reputation_multiplier * earnings_multiplier)
    xp = round(rand_range(data["xp"]) * xp_multiplier)
    crime_rise = rand_range(data["crime_rise"])

    jam_flavor = None
    jam_handled = False
    jam_cash = 0

    if random.random() < JAM_CHANCE:
        gadget_effect = await roll_gadget_effect(session, user.discord_id)
        if gadget_effect is not None:
            jam_handled = True
            jam_cash = rand_range(JAM_HANDLED_CASH_RANGE)
            await add_wallet(session, user, jam_cash, reason="tutoring:jam_handled")
            jam_flavor = (
                f"Sirens outside — you slip out, {gadget_effect.gadget_name} handles it fast, "
                "and you're back before anyone notices."
            )
        else:
            crime_rise += JAM_PENALTY_CRIME_RISE
            jam_flavor = "Sirens outside mid-session. No way to deal with it quietly — you lose focus for the rest of the hour."

    user.crime_level = min(100, user.crime_level + crime_rise)
    await add_wallet(session, user, cash, reason="tutoring:session")
    # The actual applied amount, not the pre-penalty/pre-cap roll — a crime penalty
    # (crime_level was just raised above, same session) or a boss-gate ceiling can
    # both silently shrink this below `xp`.
    actual_xp = await add_reputation(session, user, xp, perks)
    await session.commit()

    # After the commit above, deliberately: the pickup is a bonus riding on a session that
    # already happened, so it must never be able to take the session down with it.
    scavenged = await roll_ambient_scavenge(session, user.discord_id, tier_rank, ACTIVITY_TUTORING)

    return TutoringResult(
        cash=cash,
        xp=actual_xp,
        crime_rise=crime_rise,
        new_crime_level=user.crime_level,
        ally_xp_bonus=xp_multiplier > 1.0,
        ally_earnings_penalty=earnings_multiplier < 1.0,
        jam_flavor=jam_flavor,
        jam_handled=jam_handled,
        jam_cash=jam_cash,
        scavenged=scavenged,
    )
