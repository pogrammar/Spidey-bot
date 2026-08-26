"""one patreon account per discord account

Revision ID: c1f9a0d4b73e
Revises: 70d8b7995e71
Create Date: 2026-08-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1f9a0d4b73e'
down_revision: Union[str, Sequence[str], None] = '70d8b7995e71'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = 'ix_patreon_links_patreon_user_id'


def upgrade() -> None:
    """Upgrade schema.

    patreon_links.discord_id is the primary key, so one Discord account could only ever hold
    one Patreon link. The reverse was unenforced: patreon_user_id had no constraint, so a
    single Patreon subscription could be linked to any number of Discord accounts and grant
    the full tier's perks to every one of them.

    A unique *index*, not ADD CONSTRAINT — SQLite has no ALTER TABLE ADD CONSTRAINT, and the
    index form is what every backend accepts. db/models.py declares the column
    `unique=True, index=True` so a freshly created database gets this same index under this
    same name rather than an inline UNIQUE constraint.
    """
    bind = op.get_bind()

    # Deduplicate BEFORE indexing. Ordering matters more than it looks: run_migrations() is
    # called at bot.py's module level, before asyncio.run(main()), so an exception raised in
    # here means the bot never reaches the gateway at all. Creating a unique index over
    # already-duplicated rows would do exactly that, on every start, until someone SSHes in.
    rows = bind.execute(sa.text(
        "SELECT patreon_user_id, discord_id, linked_at FROM patreon_links "
        "ORDER BY patreon_user_id, linked_at, discord_id"
    )).fetchall()

    # Grouped in Python rather than with a window function: portable across the SQLite dev
    # database and whatever the deployment runs, and it gives us the keep/delete pairs to log.
    groups: dict[str, list[tuple[int, object]]] = {}
    for patreon_user_id, discord_id, linked_at in rows:
        groups.setdefault(patreon_user_id, []).append((discord_id, linked_at))

    doomed: list[int] = []
    for patreon_user_id, members in groups.items():
        if len(members) < 2:
            continue
        # The ORDER BY already put the oldest linked_at first, with the lowest discord_id
        # breaking ties, so "keep the first" is deterministic rather than whatever the
        # storage engine happened to return.
        keep, *rest = members
        print(
            f"  patreon_user_id={patreon_user_id!r}: keeping discord_id={keep[0]} "
            f"(linked_at={keep[1]})"
        )
        for discord_id, linked_at in rest:
            # Printed per row so the deploy log is the record of who lost a link — the row
            # itself is gone after this and nothing else in the schema references it.
            print(f"    dropping duplicate discord_id={discord_id} (linked_at={linked_at})")
            doomed.append(discord_id)

    if doomed:
        # Deleting rows is safe: no table in db/models.py has a foreign key pointing at
        # patreon_links. The Patreon pledge itself is untouched — what's lost is the row's
        # growth_perk_choice, and /patreon link rebuilds the rest.
        bind.execute(
            sa.text("DELETE FROM patreon_links WHERE discord_id IN :ids").bindparams(
                sa.bindparam("ids", value=doomed, expanding=True)
            )
        )
        print(f"  removed {len(doomed)} duplicate patreon link(s)")

    op.create_index(INDEX_NAME, 'patreon_links', ['patreon_user_id'], unique=True)


def downgrade() -> None:
    """Downgrade schema.

    Drops the index only. The duplicate rows upgrade() deleted are NOT recoverable — the
    affected users have to re-run /patreon link. Downgrading restores the ability to create
    new duplicates, not the old ones.
    """
    op.drop_index(INDEX_NAME, table_name='patreon_links')
