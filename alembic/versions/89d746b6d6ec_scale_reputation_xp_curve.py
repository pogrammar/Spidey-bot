"""scale reputation xp curve

Revision ID: 89d746b6d6ec
Revises: 34e7b151e691
Create Date: 2026-08-15 22:03:47.363731

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import select, update

from db.models import User
from utils.leveling import level_for_xp, xp_for_level

# revision identifiers, used by Alembic.
revision: str = '89d746b6d6ec'
down_revision: Union[str, Sequence[str], None] = '34e7b151e691'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# This account's 100,000 XP (level 1001 under the old flat formula) is leftover
# admin test data, not real progress — migrating it literally would produce a
# ~52-digit XP number. Left untouched; the owner will set it manually if needed.
_SKIP_DISCORD_IDS = {734641452214124674}


def _old_flat_level(xp: int) -> int:
    """The pre-migration flat formula (100 XP per level), hardcoded here rather
    than referenced from application code — db.models.User.reputation_level now
    computes the NEW curve, so this must stay frozen to what was actually live
    right before this migration runs, independent of future leveling changes."""
    return 1 + xp // 100


def upgrade() -> None:
    """Reputation leveling changed from flat (100 XP/level) to an accelerating
    curve (see utils/leveling.py). Existing players keep the level they'd already
    earned — XP is reset to that level's threshold under the new curve, not
    recalculated from the raw old XP total (which would demote almost everyone,
    since the new curve needs more XP per level than the old one did)."""
    conn = op.get_bind()
    rows = conn.execute(select(User.discord_id, User.reputation_xp)).fetchall()
    for discord_id, old_xp in rows:
        if discord_id in _SKIP_DISCORD_IDS:
            continue
        new_xp = xp_for_level(_old_flat_level(old_xp))
        conn.execute(update(User.__table__).where(User.discord_id == discord_id).values(reputation_xp=new_xp))


def downgrade() -> None:
    """Mirrors upgrade() in the opposite direction: read each player's level
    under the new curve, then set XP back to that level's flat-curve threshold."""
    conn = op.get_bind()
    rows = conn.execute(select(User.discord_id, User.reputation_xp)).fetchall()
    for discord_id, new_xp in rows:
        if discord_id in _SKIP_DISCORD_IDS:
            continue
        old_xp = (level_for_xp(new_xp) - 1) * 100
        conn.execute(update(User.__table__).where(User.discord_id == discord_id).values(reputation_xp=old_xp))
