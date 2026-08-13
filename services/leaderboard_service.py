from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User

LEADERBOARD_LIMIT = 10

# Each category pairs a SQL sort expression (for the top-N query) with a plain-Python
# equivalent (for scoring a single already-fetched User, when computing "your rank").
# reputation ranks by raw XP rather than the derived level so ties within a level
# still break sensibly; streak ranks by the *current* streak (not longest) since
# that's the one with real stakes — miss a day and you drop off the board.
CATEGORIES = {
    "wealth": {
        "label": "Wealth",
        "emoji": "💰",
        "column": User.wallet + User.bank,
        "value_of": lambda u: u.wallet + u.bank,
        "format": lambda v: f"${v:,}",
    },
    "reputation": {
        "label": "Reputation",
        "emoji": "⭐",
        "column": User.reputation_xp,
        "value_of": lambda u: u.reputation_xp,
        "format": lambda v: f"Level {1 + v // 100} ({v:,} XP)",
    },
    "streak": {
        "label": "Daily Streak",
        "emoji": "🔥",
        "column": User.daily_streak,
        "value_of": lambda u: u.daily_streak,
        "format": lambda v: f"{v} day{'s' if v != 1 else ''}",
    },
}


@dataclass
class LeaderboardEntry:
    discord_id: int
    value: int


async def get_leaderboard(
    session: AsyncSession, category: str, limit: int = LEADERBOARD_LIMIT
) -> list[LeaderboardEntry]:
    column = CATEGORIES[category]["column"]
    stmt = select(User.discord_id, column.label("value")).order_by(column.desc()).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [LeaderboardEntry(discord_id=row[0], value=row[1]) for row in rows]


async def get_rank(session: AsyncSession, category: str, user: User) -> int:
    """1-based standard competition rank (ties share a rank; the next distinct
    value skips ahead accordingly) — how many people rank strictly above you, +1."""
    column = CATEGORIES[category]["column"]
    my_value = CATEGORIES[category]["value_of"](user)
    stmt = select(func.count()).select_from(User).where(column > my_value)
    higher_count = (await session.execute(stmt)).scalar_one()
    return higher_count + 1
