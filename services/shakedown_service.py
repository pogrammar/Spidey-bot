from __future__ import annotations

import random
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User
from services.economy import add_wallet
from services.loot_tables import rand_float_range, rand_range

SHAKEDOWN_COOLDOWN_SECONDS = 2 * 60  # how often the initiator can try
TARGET_PROTECTION_SECONDS = 2 * 60  # a hit target can't be targeted again for a while
MIN_TARGET_WALLET = 50
STEAL_PERCENT_RANGE = [0.10, 0.25]
FAIL_PENALTY_RANGE = [20, 60]

# Success chance drops as the target's wallet grows — a bigger score means more people
# and more attention around them. No defense items exist yet; this is where a future
# "Stealth Patch"-style consumable would subtract from `chance` before the roll.
BASE_CHANCE = 0.65
MAX_WALLET_PENALTY = 0.45
WALLET_PENALTY_SCALE = 4000


@dataclass
class ShakedownResult:
    success: bool
    amount: int
    chance: float


def success_chance(target_wallet: int) -> float:
    penalty = min(MAX_WALLET_PENALTY, (target_wallet / WALLET_PENALTY_SCALE) * MAX_WALLET_PENALTY)
    return max(0.15, BASE_CHANCE - penalty)


async def attempt_shakedown(session: AsyncSession, thief: User, target: User) -> ShakedownResult:
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
