"""add shakedown attempts

Revision ID: a7c41e93b508
Revises: df532d94924d
Create Date: 2026-08-23 00:00:00.000000

Instrumentation for the Stealth Mode inactivity threshold. A stealth-protected
/shakedown returns before charging anyone, so it left no transactions row and the
perk's real firing rate was unobservable — see the ShakedownAttempt docstring.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c41e93b508'
down_revision: Union[str, Sequence[str], None] = 'df532d94924d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('shakedown_attempts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('thief_id', sa.BigInteger(), nullable=False),
    sa.Column('target_id', sa.BigInteger(), nullable=False),
    sa.Column('outcome', sa.String(), nullable=False),
    # Nullable: the target has never run a command, which is distinct from "idle 0s".
    sa.Column('target_idle_seconds', sa.Integer(), nullable=True),
    sa.Column('target_tier_rank', sa.Integer(), nullable=False),
    sa.Column('amount', sa.Integer(), nullable=False),
    sa.Column('target_wallet', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['thief_id'], ['users.discord_id'], ),
    sa.ForeignKeyConstraint(['target_id'], ['users.discord_id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('shakedown_attempts')
