from __future__ import annotations

import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import InventoryItem, User
from services.ally_service import (
    LOW_HAPPINESS_THRESHOLD,
    get_current_happiness,
    reputation_xp_multiplier,
)
from services.economy import add_reputation, add_wallet
from services.inventory_service import remove_item
from services.loot_tables import LOOT_TABLES, rand_range, weighted_choice

PATROL_COOLDOWN_SECONDS = 30
CAMERA_ITEM_KEY = "camera"
CRIME_LEVEL_WEIGHT_BONUS = 0.3  # each point of crime_level nudges crime-outcome odds up
CRIME_LEVEL_DECAY_RANGE = [3, 6]  # patrolling calms the city back down

# every patrol burns web fluid to get around — no vials on hand means paying cash
# instead (store-bought substitute fluid, worse and pricier), which is what actually
# makes /lab brew worth running rather than a side activity nobody needs
WEB_FLUID_ITEM_KEY = "web_fluid_vial"
WEB_FLUID_PER_PATROL = 1
NO_WEB_FLUID_TAX_RANGE = [20, 40]

# neglecting an ally makes their related "Parker Luck" hazard more likely to fire
HAZARD_ALLY_KEY = {"aunt_may_flowers": "aunt_may", "mj_birthday_gift": "mj"}
NEGLECT_HAZARD_MULTIPLIER = 2.5

# higher reputation level = tougher patrols: more suit damage, more XP, more wear on
# gear. Keeps the game from getting purely easier as you progress. 1.0x at level 1,
# +5% per level after that (level 11 -> 1.5x, level 21 -> 2.0x). Shared with
# battle_service.py, which scales enemy stats off the same curve.
DIFFICULTY_PER_LEVEL = 0.05


def difficulty_multiplier(level: int) -> float:
    return 1 + DIFFICULTY_PER_LEVEL * (level - 1)


@dataclass
class PatrolResult:
    """Covers the non-combat outcomes (nothing / scenic). Crime encounters are
    handled by services/battle_service.py's interactive flow instead — see
    cogs/patrol_cog.py for how the two connect."""

    outcome_key: str
    flavor: str
    xp_gained: int = 0
    cash_gained: int = 0
    crime_level: int = 0
    hazard_flavor: str | None = None
    hazard_cash: int = 0
    web_fluid_used: bool = False
    web_fluid_tax: int = 0
    ally_xp_bonus: bool = False
    difficulty_level: int = 1


@dataclass
class PatrolStart:
    """What every /patrol call resolves first, before branching on outcome type."""

    outcome: dict
    flavor: str
    web_fluid_used: bool
    web_fluid_tax: int
    difficulty: float
    xp_multiplier: float


async def begin_patrol(session: AsyncSession, user: User) -> PatrolStart:
    web_fluid_used = await remove_item(session, user.discord_id, WEB_FLUID_ITEM_KEY, WEB_FLUID_PER_PATROL)
    web_fluid_tax = 0
    if not web_fluid_used:
        web_fluid_tax = rand_range(NO_WEB_FLUID_TAX_RANGE)
        await add_wallet(session, user, -web_fluid_tax, reason="patrol:no_web_fluid")

    difficulty = difficulty_multiplier(user.reputation_level)
    outcome = _roll_patrol_outcome(user.crime_level)
    flavor = random.choice(outcome["flavor"])
    xp_multiplier = await reputation_xp_multiplier(session, user.discord_id)

    await session.commit()
    return PatrolStart(
        outcome=outcome,
        flavor=flavor,
        web_fluid_used=web_fluid_used,
        web_fluid_tax=web_fluid_tax,
        difficulty=difficulty,
        xp_multiplier=xp_multiplier,
    )


def compute_base_xp(start: PatrolStart) -> int:
    return round(rand_range(start.outcome["xp"]) * start.xp_multiplier * start.difficulty)


async def finish_noncombat_patrol(session: AsyncSession, user: User, start: PatrolStart) -> PatrolResult:
    """Resolves "nothing" and "scenic" outcomes instantly — no battle needed."""
    outcome = start.outcome
    xp = compute_base_xp(start)

    result = PatrolResult(
        outcome_key=outcome["key"],
        flavor=start.flavor,
        xp_gained=xp,
        web_fluid_used=start.web_fluid_used,
        web_fluid_tax=start.web_fluid_tax,
        ally_xp_bonus=start.xp_multiplier > 1.0,
        difficulty_level=user.reputation_level,
    )

    if "cash" in outcome:
        result.cash_gained = rand_range(outcome["cash"])
        await add_wallet(session, user, result.cash_gained, reason=f"patrol:{outcome['key']}")

    await add_reputation(session, user, xp)
    user.crime_level = max(0, user.crime_level - rand_range(CRIME_LEVEL_DECAY_RANGE))

    hazard = await roll_hazard(session, user.discord_id)
    if hazard is not None:
        hazard_cash = rand_range(hazard["cash"])
        await add_wallet(session, user, hazard_cash, reason=f"hazard:{hazard['key']}")
        result.hazard_flavor = hazard["flavor"]
        result.hazard_cash = hazard_cash

    result.crime_level = user.crime_level
    await session.commit()
    return result


def _roll_patrol_outcome(crime_level: int) -> dict:
    """Higher city crime_level (built up by skipping patrol for /tutoring) skews the
    odds toward crime encounters — more photo/donation opportunity, but more risk too."""
    biased = []
    for entry in LOOT_TABLES["patrol"]:
        weight = entry["weight"]
        if entry["key"] in ("crime_bronze", "crime_silver", "crime_gold"):
            weight += crime_level * CRIME_LEVEL_WEIGHT_BONUS
        biased.append({**entry, "weight": weight})
    return weighted_choice(biased)


async def get_equipped_camera(session: AsyncSession, user_id: int) -> InventoryItem | None:
    stmt = select(InventoryItem).where(
        InventoryItem.user_id == user_id,
        InventoryItem.item_key == CAMERA_ITEM_KEY,
        InventoryItem.equipped.is_(True),
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def roll_donation() -> dict | None:
    for donation in LOOT_TABLES["donations"]:
        if random.random() < donation["chance"]:
            return donation
    return None


async def roll_hazard(session: AsyncSession, user_id: int) -> dict | None:
    for hazard in LOOT_TABLES["hazards"]:
        chance = hazard["chance"]
        ally_key = HAZARD_ALLY_KEY.get(hazard["key"])
        if ally_key is not None:
            happiness = await get_current_happiness(session, user_id, ally_key)
            if happiness < LOW_HAPPINESS_THRESHOLD:
                chance *= NEGLECT_HAZARD_MULTIPLIER
        if random.random() < chance:
            return hazard
    return None
