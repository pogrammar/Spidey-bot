from __future__ import annotations

import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Cooldown

# In-memory, per-process — resets on bot restart. This is a dev/testing toggle, not
# game state, so it deliberately doesn't live in the database. See cogs/admin_cog.py.
_BYPASS_USER_IDS: set[int] = set()


def enable_bypass(user_id: int) -> None:
    _BYPASS_USER_IDS.add(user_id)


def disable_bypass(user_id: int) -> None:
    _BYPASS_USER_IDS.discard(user_id)


def is_bypass_enabled(user_id: int) -> bool:
    return user_id in _BYPASS_USER_IDS


async def get_remaining_seconds(session: AsyncSession, user_id: int, command_key: str) -> float:
    if is_bypass_enabled(user_id):
        return 0.0
    cooldown = await session.get(Cooldown, (user_id, command_key))
    if cooldown is None:
        return 0.0
    remaining = (cooldown.expires_at - datetime.datetime.utcnow()).total_seconds()
    return max(0.0, remaining)


async def set_cooldown(
    session: AsyncSession, user_id: int, command_key: str, seconds: float
) -> None:
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)
    cooldown = await session.get(Cooldown, (user_id, command_key))
    if cooldown is None:
        session.add(Cooldown(user_id=user_id, command_key=command_key, expires_at=expires_at))
    else:
        cooldown.expires_at = expires_at
    await session.commit()


def format_remaining(seconds: float) -> str:
    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
