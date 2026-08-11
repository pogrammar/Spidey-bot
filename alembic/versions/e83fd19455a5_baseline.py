"""baseline

Revision ID: e83fd19455a5
Revises: 
Create Date: 2026-08-11 22:44:52.966766

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from db.models import Base

# revision identifiers, used by Alembic.
revision: str = 'e83fd19455a5'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Creates every table currently in db/models.py. create_all() is idempotent
    (checkfirst=True by default), so this is safe to run against a brand new empty
    database *or* an existing one that already has these tables from the pre-Alembic
    create_all()-on-every-boot days — either way nothing gets duplicated or dropped,
    and no existing data is touched."""
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
