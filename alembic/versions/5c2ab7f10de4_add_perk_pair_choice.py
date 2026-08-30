"""add perk pair choice

Revision ID: 5c2ab7f10de4
Revises: c1f9a0d4b73e
Create Date: 2026-08-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5c2ab7f10de4'
down_revision: Union[str, Sequence[str], None] = 'c1f9a0d4b73e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable with no server_default, unlike ad_impressions: NULL is a meaningful third
    # state here, not a gap to backfill. It means "this member has never run /perks choose",
    # and services/server_perks.ServerPerks._pair resolves that to a default which depends
    # on their live Patreon pledge. Filling existing rows with either value would silently
    # lock every current level 10 member out of that default.
    op.add_column('users', sa.Column('perk_pair_choice', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'perk_pair_choice')
