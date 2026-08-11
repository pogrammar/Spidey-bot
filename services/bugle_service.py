from __future__ import annotations

import random
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import PendingPhoto, User
from services.ally_service import earnings_penalty_multiplier
from services.economy import add_wallet
from services.gadget_service import roll_gadget_effect
from services.loot_tables import LOOT_TABLES, rand_float_range, rand_range

BUGLE_COOLDOWN_SECONDS = 60

# Another "secret usage" moment — on the way to drop off film, someone tries to
# grab the bag. Handle it with a gadget and walk away clean; otherwise some of the
# payout doesn't make it to the desk.
JAM_CHANCE = 0.12
JAM_LOSS_FRACTION = 0.2
JAM_HANDLED_CASH_RANGE = [15, 35]


@dataclass
class BugleResult:
    photos_sold: int = 0
    total_cash: int = 0
    breakdown: dict[str, int] = field(default_factory=dict)  # quality -> count
    ally_earnings_penalty: bool = False
    jam_flavor: str | None = None
    jam_handled: bool = False
    jam_amount: int = 0


@dataclass
class PendingSummary:
    breakdown: dict[str, int] = field(default_factory=dict)  # quality -> count
    estimated_min: int = 0
    estimated_max: int = 0


async def _fetch_pending(session: AsyncSession, user: User) -> list[PendingPhoto]:
    stmt = select(PendingPhoto).where(PendingPhoto.user_id == user.discord_id)
    return list((await session.execute(stmt)).scalars())


async def get_pending_summary(session: AsyncSession, user: User) -> PendingSummary:
    """Read-only look at what's sitting in the pending-photos queue, with an estimated
    sell range (actual payout on submit still has JJJ's haggling randomness baked in)."""
    pending = await _fetch_pending(session, user)
    payouts = LOOT_TABLES["bugle_payouts"]
    jjj_lo, jjj_hi = LOOT_TABLES["jjj_multiplier"]
    earnings_multiplier = await earnings_penalty_multiplier(session, user.discord_id)

    summary = PendingSummary()
    for photo in pending:
        summary.breakdown[photo.quality] = summary.breakdown.get(photo.quality, 0) + 1
        lo, hi = payouts[photo.quality]
        summary.estimated_min += round(lo * jjj_lo * user.reputation_multiplier * earnings_multiplier)
        summary.estimated_max += round(hi * jjj_hi * user.reputation_multiplier * earnings_multiplier)

    return summary


async def submit_photos(session: AsyncSession, user: User) -> BugleResult | None:
    """Sells every pending photo. Returns None if there's nothing to submit."""
    pending = await _fetch_pending(session, user)

    if not pending:
        return None

    earnings_multiplier = await earnings_penalty_multiplier(session, user.discord_id)
    result = BugleResult(ally_earnings_penalty=earnings_multiplier < 1.0)
    payouts = LOOT_TABLES["bugle_payouts"]

    for photo in pending:
        base = rand_range(payouts[photo.quality])
        jjj_factor = rand_float_range(LOOT_TABLES["jjj_multiplier"])
        payout = round(base * jjj_factor * user.reputation_multiplier * earnings_multiplier)

        result.total_cash += payout
        result.photos_sold += 1
        result.breakdown[photo.quality] = result.breakdown.get(photo.quality, 0) + 1

        await session.delete(photo)

    if random.random() < JAM_CHANCE:
        gadget_effect = await roll_gadget_effect(session, user.discord_id)
        if gadget_effect is not None:
            result.jam_handled = True
            result.jam_amount = rand_range(JAM_HANDLED_CASH_RANGE)
            result.total_cash += result.jam_amount
            result.jam_flavor = (
                f"Someone tries to snatch the bag on the way over — {gadget_effect.gadget_name} "
                "handles it before JJJ even hears about it."
            )
        else:
            result.jam_amount = round(result.total_cash * JAM_LOSS_FRACTION)
            result.total_cash -= result.jam_amount
            result.jam_flavor = "You get jumped for the bag on the way in — some of it doesn't make it to the desk."

    await add_wallet(session, user, result.total_cash, reason="bugle:submit")
    await session.commit()
    return result
