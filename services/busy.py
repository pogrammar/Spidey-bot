from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from services.cooldowns import get_remaining_seconds, set_cooldown

# Anything that takes Peter off the streets registers itself here under its own
# source key, so /patrol (and anything else that checks) can say specifically what
# he's doing right now instead of a vague "something else."
BUSY_LABELS = {
    "tutoring": "tutoring",
    "ally:aunt_may": "visiting Aunt May",
    "ally:mj": "visiting MJ",
}


def _busy_key(source: str) -> str:
    return f"busy:{source}"


async def set_busy(session: AsyncSession, user_id: int, source: str, seconds: float) -> None:
    await set_cooldown(session, user_id, _busy_key(source), seconds)


async def get_busy(session: AsyncSession, user_id: int) -> tuple[str, float] | None:
    """Returns (label, remaining_seconds) for whichever busy-lock is active, or None."""
    for source, label in BUSY_LABELS.items():
        remaining = await get_remaining_seconds(session, user_id, _busy_key(source))
        if remaining > 0:
            return label, remaining
    return None
