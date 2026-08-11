from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Transaction, User

RENT_AMOUNT = 400
RENT_PERIOD = datetime.timedelta(days=7)
EVICTION_INCREMENT = 25
TICK_INTERVAL_MINUTES = 10


@dataclass
class RentTickResult:
    user_id: int
    paid: bool
    eviction_meter: int


async def pay_rent_manually(session: AsyncSession, user: User) -> tuple[bool, str]:
    if user.bank < RENT_AMOUNT:
        return False, f"Rent is ${RENT_AMOUNT:,} and your bank's short. Deposit more first."

    user.bank -= RENT_AMOUNT
    session.add(
        Transaction(
            user_id=user.discord_id,
            balance_type="bank",
            amount=-RENT_AMOUNT,
            reason="apartment:rent_manual",
        )
    )
    user.eviction_meter = 0
    user.next_rent_due = datetime.datetime.utcnow() + RENT_PERIOD
    await session.commit()
    return True, f"Rent paid — ${RENT_AMOUNT:,} out of the bank. You're clear for another week."


async def process_due_rents(session: AsyncSession) -> list[RentTickResult]:
    """Called on a timer (see cogs/scheduler_cog.py). Auto-debits rent from bank for
    everyone whose due date has passed; missed payments push the eviction meter up."""
    now = datetime.datetime.utcnow()
    stmt = select(User).where(User.next_rent_due <= now)
    due_users = list((await session.execute(stmt)).scalars())

    results = []
    for user in due_users:
        if user.bank >= RENT_AMOUNT:
            user.bank -= RENT_AMOUNT
            session.add(
                Transaction(
                    user_id=user.discord_id,
                    balance_type="bank",
                    amount=-RENT_AMOUNT,
                    reason="apartment:rent_auto",
                )
            )
            user.eviction_meter = 0
            paid = True
        else:
            user.eviction_meter = min(100, user.eviction_meter + EVICTION_INCREMENT)
            paid = False

        user.next_rent_due = now + RENT_PERIOD
        results.append(RentTickResult(user_id=user.discord_id, paid=paid, eviction_meter=user.eviction_meter))

    await session.commit()
    return results
