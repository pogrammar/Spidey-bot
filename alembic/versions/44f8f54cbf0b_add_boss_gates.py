"""add boss gates

Revision ID: 44f8f54cbf0b
Revises: 89d746b6d6ec
Create Date: 2026-08-15 23:56:59.615074

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import select, update

from db.models import User
from utils.leveling import level_for_xp

# revision identifiers, used by Alembic.
revision: str = '44f8f54cbf0b'
down_revision: Union[str, Sequence[str], None] = '89d746b6d6ec'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BOSS_LEVEL_INTERVAL = 5


def upgrade() -> None:
    """Reputation leveling now gates every 5th level behind a boss fight (see
    services/economy.py's at_boss_gate/next_boss_gate_level). Existing players
    aren't sent back to fight every gate they've already passed — boss_clears is
    backfilled to whichever gate they're already at or past, so only the next
    uncleared one ever applies going forward."""
    op.add_column("users", sa.Column("boss_clears", sa.Integer(), nullable=False, server_default="0"))

    conn = op.get_bind()
    rows = conn.execute(select(User.discord_id, User.reputation_xp)).fetchall()
    for discord_id, xp in rows:
        boss_clears = max(0, (level_for_xp(xp) - 1) // BOSS_LEVEL_INTERVAL)
        if boss_clears:
            conn.execute(update(User.__table__).where(User.discord_id == discord_id).values(boss_clears=boss_clears))


def downgrade() -> None:
    """No lossy transform to reverse — boss_clears is a new column with no prior
    data of its own, so dropping it is a clean inverse of upgrade()."""
    op.drop_column("users", "boss_clears")
