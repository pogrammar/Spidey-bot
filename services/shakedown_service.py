from __future__ import annotations

import datetime
import logging
import random
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ShakedownAttempt, User
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
#
# THE 20 IS STILL UNVALIDATED. It's a reasoned guess, not a measured number, and
# unlike VENOM_BLAST_DAMAGE_MULTIPLIER it can't be settled by simulation — it depends
# entirely on how long real players step away from Discord, which no sim can supply.
# Every attempt is now logged with the target's idle time at that moment (see
# log_shakedown_attempt and ShakedownAttempt), specifically so this number can be
# checked against real behaviour instead of re-argued: run
# `python scratch/analyze_stealth_mode.py` for the firing rate and the counterfactual
# rate at every candidate threshold. Do not move this constant off intuition now that
# there's a way to look.
STEALTH_MODE_INACTIVITY_THRESHOLD_SECONDS = 20 * 60

# Candidate thresholds the analysis script reports on, in minutes. Not read by any
# game logic — it lives here rather than in the script so the shipped value and the
# alternatives it's being judged against sit in the same place.
STEALTH_MODE_CANDIDATE_THRESHOLDS_MINUTES = [5, 10, 15, 20, 30, 45, 60, 120]

OUTCOME_PROTECTED = "protected"
OUTCOME_SUCCESS = "success"
OUTCOME_CAUGHT = "caught"

log = logging.getLogger("spidey")


@dataclass
class ShakedownResult:
    success: bool
    amount: int
    chance: float
    stealth_protected: bool = False


def success_chance(target_wallet: int) -> float:
    penalty = min(MAX_WALLET_PENALTY, (target_wallet / WALLET_PENALTY_SCALE) * MAX_WALLET_PENALTY)
    return max(0.15, BASE_CHANCE - penalty)


def target_idle_seconds(target: User) -> int | None:
    """How long since the target last ran any command, or None if they never have.

    Separate from the threshold comparison on purpose: the same number both decides
    whether Stealth Mode fires and gets recorded for every attempt, so the logged value
    is guaranteed to be the one the gate actually judged rather than a second reading
    taken a few milliseconds later.
    """
    if target.last_active_at is None:
        return None
    return int((datetime.datetime.utcnow() - target.last_active_at).total_seconds())


def stealth_mode_active(tier_rank: int, idle_seconds: int | None) -> bool:
    """Pure predicate — no DB, no clock. Both inputs are read once by the caller, which
    is what lets the analysis script replay this exact rule against logged rows at other
    candidate thresholds without duplicating the logic."""
    if tier_rank < TIER_RANK_SYMBIOTE or idle_seconds is None:
        return False
    return idle_seconds >= STEALTH_MODE_INACTIVITY_THRESHOLD_SECONDS


async def log_shakedown_attempt(
    thief_id: int,
    target_id: int,
    outcome: str,
    idle_seconds: int | None,
    tier_rank: int,
    amount: int,
    target_wallet: int,
) -> None:
    """Records one attempt for the Stealth Mode threshold analysis.

    Deliberately opens its **own** session rather than taking the caller's: a telemetry
    write must never be able to roll back, or be rolled back by, real cash movement.
    And it swallows everything — if this table is missing (migration not yet run) or the
    insert fails for any reason, a player's /shakedown must still work. Losing a
    measurement is an acceptable cost; failing a command to record one is not.
    """
    from db.base import async_session  # local: avoids a circular import at module load

    try:
        async with async_session() as session:
            session.add(ShakedownAttempt(
                thief_id=thief_id,
                target_id=target_id,
                outcome=outcome,
                target_idle_seconds=idle_seconds,
                target_tier_rank=tier_rank,
                amount=amount,
                target_wallet=target_wallet,
            ))
            await session.commit()
    except Exception:
        log.exception("Failed to log shakedown attempt; the attempt itself was unaffected")


async def attempt_shakedown(session: AsyncSession, thief: User, target: User) -> ShakedownResult:
    # Both read once, before anything resolves: these are what the gate judges AND what
    # gets logged, and target.wallet in particular is about to change on a success.
    tier_rank = await get_tier_rank(session, target.discord_id)
    idle_seconds = target_idle_seconds(target)
    wallet_at_attempt = target.wallet

    if stealth_mode_active(tier_rank, idle_seconds):
        # The attempt never really happens — no fail penalty either, since the
        # thief backs off before getting close enough to get caught.
        await log_shakedown_attempt(thief.discord_id, target.discord_id, OUTCOME_PROTECTED,
                                    idle_seconds, tier_rank, 0, wallet_at_attempt)
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
    # After the commit, so a telemetry failure can't strand a half-applied shakedown.
    await log_shakedown_attempt(
        thief.discord_id, target.discord_id,
        OUTCOME_SUCCESS if success else OUTCOME_CAUGHT,
        idle_seconds, tier_rank, amount, wallet_at_attempt,
    )
    return ShakedownResult(success=success, amount=amount, chance=chance)
