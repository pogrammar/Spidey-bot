from __future__ import annotations

import datetime
import random
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User
from services.economy import add_wallet
from services.loot_tables import rand_float_range, rand_range
from services.patreon_service import TIER_RANK_SYMBIOTE, get_tier_rank

SHAKEDOWN_COOLDOWN_SECONDS = 2 * 60  # how often the initiator can try
TARGET_PROTECTION_SECONDS = 2 * 60  # a hit target can't be targeted again for a while
MIN_TARGET_WALLET = 50
STEAL_PERCENT_RANGE = [0.10, 0.25]
FAIL_PENALTY_RANGE = [20, 60]

# Success chance drops as the target's wallet grows — a bigger score means more people
# and more attention around them.
BASE_CHANCE = 0.65
MAX_WALLET_PENALTY = 0.45
WALLET_PENALTY_SCALE = 4000

# Stealth Mode (Symbiote+ perk) — full shakedown immunity, but only while the
# target's been inactive this long (last_active_at, stamped on every command —
# see utils/first_run.py). Deliberately NOT permanent immunity (already rejected
# earlier as pay-to-win) — this reads as "protected while you're not even
# playing" rather than "safer while actively playing," since /shakedown can hit
# someone whether or not they're online. 20 minutes: long enough that it's
# clearly "stepped away," not just a pause between patrols.
STEALTH_MODE_INACTIVITY_THRESHOLD_SECONDS = 20 * 60


@dataclass
class ShakedownResult:
    success: bool
    amount: int
    chance: float
    stealth_protected: bool = False


def success_chance(target_wallet: int) -> float:
    penalty = min(MAX_WALLET_PENALTY, (target_wallet / WALLET_PENALTY_SCALE) * MAX_WALLET_PENALTY)
    return max(0.15, BASE_CHANCE - penalty)


async def _stealth_mode_active(session: AsyncSession, target: User) -> bool:
    tier_rank = await get_tier_rank(session, target.discord_id)
    if tier_rank < TIER_RANK_SYMBIOTE or target.last_active_at is None:
        return False
    idle_seconds = (datetime.datetime.utcnow() - target.last_active_at).total_seconds()
    return idle_seconds >= STEALTH_MODE_INACTIVITY_THRESHOLD_SECONDS


async def attempt_shakedown(session: AsyncSession, thief: User, target: User) -> ShakedownResult:
    if await _stealth_mode_active(session, target):
        # The attempt never really happens — no fail penalty either, since the
        # thief backs off before getting close enough to get caught.
        return ShakedownResult(success=False, amount=0, chance=0.0, stealth_protected=True)

    chance = success_chance(target.wallet)
    success = random.random() < chance

    if success:
        amount = round(target.wallet * rand_float_range(STEAL_PERCENT_RANGE))
        await add_wallet(session, target, -amount, reason="shakedown:victim")
        await add_wallet(session, thief, amount, reason="shakedown:thief")
    else:
        amount = rand_range(FAIL_PENALTY_RANGE)
        await add_wallet(session, thief, -amount, reason="shakedown:caught")

    await session.commit()
    return ShakedownResult(success=success, amount=amount, chance=chance)
