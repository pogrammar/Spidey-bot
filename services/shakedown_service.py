from __future__ import annotations

import datetime
import logging
import random
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ShakedownAttempt, User
from services.cooldowns import get_remaining_seconds, set_cooldown
from services.economy import add_wallet
from services.loot_tables import rand_float_range, rand_range
from services.patreon_service import TIER_RANK_NONE, TIER_RANK_SYMBIOTE, get_tier_rank

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
    # True only when stealth_protected AND the target's DM throttle window was free — the
    # cog uses this as "send the DM", so the throttle is decided here rather than there.
    # See claim_stealth_dm_slot: the slot is already claimed by the time this is True, so a
    # caller that ignores the flag silently costs its owner one notification.
    notify_target: bool = False
    # The TARGET's tier, not the thief's — /shakedown is the one place a perk belongs to
    # somebody other than the person reading the message, so the cog can't look this up off
    # ctx.author. Returned rather than re-read in the cog because get_tier_rank consults the
    # Patreon cache, which a background re-check can move (see patreon_service); the badge
    # must name the rank the gate actually judged, not a second reading of it.
    target_tier_rank: int = TIER_RANK_NONE


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


async def count_stealth_protections(target_id: int) -> int:
    """How many /shakedown attempts Stealth Mode has turned away for this player.

    This exists because the perk was, until 2026-08-25, completely invisible to the person
    paying for it. A protected attempt renders a panel for the *thief* and nothing at all
    for the target — no DM, no ping, not even a transactions row — and by construction it
    only fires while the target is away from Discord. So a subscriber could hold the tier
    for a month, have Stealth Mode block a dozen shakedowns, and have no way to learn that
    any of it happened. The rows were already being written for threshold analysis; this
    just reads them back to the one person with a stake in them.

    Takes no session and swallows everything, exactly like log_shakedown_attempt above and
    for the same two reasons: this table is allowed to be missing (migration not yet run),
    and a failed query on a borrowed session poisons the caller's transaction. The caller
    is /patreon perks, where a perk *count* must never be able to take down the panel that
    lists the perks. 0 on any error, which renders as "no count yet" — the pre-2026-08-25
    behaviour — rather than as a wrong number.
    """
    from db.base import async_session  # local: avoids a circular import at module load

    try:
        async with async_session() as session:
            stmt = (
                select(func.count())
                .select_from(ShakedownAttempt)
                .where(
                    ShakedownAttempt.target_id == target_id,
                    ShakedownAttempt.outcome == OUTCOME_PROTECTED,
                )
            )
            return int((await session.execute(stmt)).scalar_one())
    except Exception:
        log.exception("Failed to count Stealth Mode protections; the panel renders without the count")
        return 0


# Stealth Mode's DM to its owner, and how often it's allowed to fire.
#
# The perk only fires while the target has been idle 20+ minutes, so by construction they
# are not at their keyboard to read anything — which means a run of attempts by different
# thieves during one absence arrives as a stack of near-identical DMs waiting for them
# rather than as a live feed. This collapses that stack.
#
# The real floor isn't this constant alone: /shakedown sets a TARGET_PROTECTION cooldown on
# the victim for *every* outcome, so any one target can be attempted at most once every two
# minutes by anyone. 15 minutes turns a worst case of ~30 DMs/hour into at most 4, while an
# ordinary once-in-a-while attempt still always gets its own DM.
#
# Throttling is lossless in aggregate, which is the reason it's acceptable at all: the DM
# quotes count_stealth_protections()'s ALL-TIME total rather than "this is number N", so a
# subscriber attempted five times and DMed once still learns the true figure — and it's the
# same figure /patreon perks gives them.
STEALTH_DM_COOLDOWN_SECONDS = 15 * 60
STEALTH_DM_COOLDOWN_KEY = "stealth_dm"


async def claim_stealth_dm_slot(session: AsyncSession, target_id: int) -> bool:
    """True if Stealth Mode may DM this target right now, claiming the slot when it does.

    Side-effecting on purpose, hence "claim" rather than "should": the read and the write
    have to be one step, or two thieves resolving back-to-back both pass the check.

    Call this **only** on OUTCOME_PROTECTED. An ordinary shakedown must not burn the
    window — the target would then miss the next attempt the perk actually turned away,
    which is the one thing the DM exists to tell them about.

    Takes the caller's session, unlike log_shakedown_attempt and count_stealth_protections
    above, and is allowed to raise. Those two are telemetry, where losing a row beats
    failing a command; this is a throttle on player-visible behaviour, writing to a
    `cooldowns` table /shakedown is already touching twice in the same transaction. If the
    write fails, the shakedown should fail with it rather than quietly leave the throttle
    unset and notify on every attempt from then on.

    Note that `cooldowns` honours the admin bypass, keyed on the *target*: switching it on
    for the account being shaken down makes every protected attempt DM, which is how you
    smoke-test this without waiting out 15 minutes between tries.
    """
    if await get_remaining_seconds(session, target_id, STEALTH_DM_COOLDOWN_KEY) > 0:
        return False
    await set_cooldown(session, target_id, STEALTH_DM_COOLDOWN_KEY, STEALTH_DM_COOLDOWN_SECONDS)
    return True


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
        # Claimed here rather than in the cog so the throttle can't be forgotten by a second
        # caller, and only on this branch: see claim_stealth_dm_slot on why an ordinary
        # shakedown must not consume the window. Deliberately AFTER the log write, because
        # the DM quotes the all-time protection count and that count has to include the
        # attempt being reported.
        notify = await claim_stealth_dm_slot(session, target.discord_id)
        return ShakedownResult(success=False, amount=0, chance=0.0, stealth_protected=True,
                               target_tier_rank=tier_rank, notify_target=notify)

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
    return ShakedownResult(success=success, amount=amount, chance=chance, target_tier_rank=tier_rank)
