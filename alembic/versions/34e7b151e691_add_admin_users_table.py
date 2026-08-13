"""add admin_users table

Revision ID: 34e7b151e691
Revises: e83fd19455a5
Create Date: 2026-08-13 11:26:35.222895

"""
from typing import Sequence, Union

from alembic import op

from db.models import AdminUser

# revision identifiers, used by Alembic.
revision: str = '34e7b151e691'
down_revision: Union[str, Sequence[str], None] = 'e83fd19455a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """checkfirst=True (not a plain op.create_table) because the baseline migration's
    Base.metadata.create_all() dynamically reflects whatever's *currently* in
    db/models.py, not a frozen snapshot — so a brand new database already has this
    table by the time baseline finishes, and a plain create_table here would collide
    with it. On an existing database that was migrated before AdminUser existed,
    this is what actually creates the table."""
    AdminUser.__table__.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    AdminUser.__table__.drop(op.get_bind(), checkfirst=True)
