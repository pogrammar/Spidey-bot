"""add ad impressions

Revision ID: 70d8b7995e71
Revises: a7c41e93b508
Create Date: 2026-08-25 17:38:33.239051

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '70d8b7995e71'
down_revision: Union[str, Sequence[str], None] = 'a7c41e93b508'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default alongside the model-level default: existing rows predate the column
    # and a NULL here would break the modulo that picks which promo a user sees next
    # (see services/ads_service.py). New rows get 0 from either mechanism.
    op.add_column(
        'users',
        sa.Column('ad_impressions', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'ad_impressions')
