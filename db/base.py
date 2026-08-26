import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import DB_URL
from db.models import Base

log = logging.getLogger("spidey")

_IS_SQLITE = DB_URL.startswith("sqlite")

# How long a connection waits for another connection's write lock before giving up with
# OperationalError("database is locked"). SQLite permits exactly ONE writer at a time
# across the whole file, so under concurrent load every writer past the first one waits
# here — this is the number that decides whether a burst queues or throws.
#
# sqlite3's default is 5 seconds, which is what the bot ran on until 2026-08-27, and it
# is not enough once enough users are issuing commands at once: it isn't the write itself
# that takes the time, it's the queue in front of it.
BUSY_TIMEOUT_SECONDS = 30

# WAL is the load-bearing change, not the timeout. Under the default DELETE journal a
# COMMIT needs an EXCLUSIVE lock, which cannot be taken while ANY connection holds a
# read lock — so readers block writers. This bot is extremely read-heavy in a way that's
# easy to miss: seven autocomplete handlers each open a session and SELECT once per
# KEYSTROKE (cogs/{admin,ally,gadget,market,shop}_cog.py), on top of /leaderboard and
# /inventory. Under sustained keystroke traffic the moment when zero readers hold a lock
# never arrives, and writers time out — which is the "database is locked" being fixed.
#
# In WAL mode readers and the writer never block each other at all; readers see the last
# committed snapshot while a write is in flight. Only writer-vs-writer contention is
# left, and that resolves in well under a millisecond here because no code path in this
# repo holds an uncommitted write across an await on network I/O (verified by AST scan
# 2026-08-27 — keep it that way, it's what makes BUSY_TIMEOUT_SECONDS generous enough).
#
# Two things to know about WAL:
#   * The mode is stored in the DB file header, so it persists once set. Re-issuing the
#     PRAGMA on every connection is a no-op that returns "wal".
#   * It needs to create -wal and -shm files next to the DB and requires working shared
#     memory, so the DB must be on a LOCAL filesystem. On a network mount the PRAGMA
#     silently reports the old mode instead of failing — hence the check below.
JOURNAL_MODE = "WAL"

# synchronous=NORMAL is safe in WAL mode specifically: it cannot corrupt the database,
# it only means the last few committed transactions could be lost if the OS or machine
# dies (a process crash is still safe). That trade buys a large reduction in fsync
# traffic per commit, which is the other half of why writers were queueing. Set this
# back to FULL if losing a few seconds of cash movement to a host power-cut ever matters
# more than throughput.
SYNCHRONOUS = "NORMAL"

_connect_args = {}
if _IS_SQLITE:
    # sqlite3.connect(timeout=) is in SECONDS, and it's what installs the busy handler
    # for the connection's own opening statements. The PRAGMA below sets the same thing
    # in milliseconds for everything after; both are set on purpose.
    _connect_args["timeout"] = BUSY_TIMEOUT_SECONDS

# Sized for concurrent OPEN SESSIONS, not for write throughput — under WAL extra readers
# cost nothing in contention. The reason the default 5+10 is tight: nine command bodies
# hold their session open across an `await ctx.respond()` (cogs/patrol_cog.py:680,
# cogs/pvp_cog.py:112 and seven others), so a connection can stay checked out for a whole
# Discord round-trip. Exhausting the pool raises "QueuePool limit ... connection timed
# out", which is a DIFFERENT error from "database is locked" — if that one starts
# appearing, raise these rather than the busy timeout. Each SQLite connection carries its
# own ~2MB page cache, so 30 is roughly 60MB; check the container's memory limit before
# raising it further.
POOL_SIZE = 10
MAX_OVERFLOW = 20

engine = create_async_engine(
    DB_URL,
    echo=False,
    connect_args=_connect_args,
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
)

# expire_on_commit=False is load-bearing for services.patreon_service.refresh_stale_links,
# which commits per-link inside a loop over ORM objects. With expiry on, reading the next
# link's access_token after a commit would need implicit IO and raise MissingGreenlet
# under asyncio. See the comment in that function before changing this.
async_session = async_sessionmaker(engine, expire_on_commit=False)

_pragmas_logged = False


@event.listens_for(engine.sync_engine, "connect")
def _apply_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """Applies the concurrency PRAGMAs to every new pooled connection.

    Has to be per-connection rather than once at startup: busy_timeout and synchronous
    are connection-scoped settings, so a connection created later by pool overflow would
    otherwise run on sqlite3's 5-second default and be the one that throws.
    """
    if not _IS_SQLITE:
        return

    global _pragmas_logged

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}")
        # Returns the resulting mode, which is NOT necessarily the one asked for.
        cursor.execute(f"PRAGMA journal_mode = {JOURNAL_MODE}")
        row = cursor.fetchone()
        cursor.execute(f"PRAGMA synchronous = {SYNCHRONOUS}")

        if not _pragmas_logged:
            _pragmas_logged = True
            active = (row[0] if row else "unknown")
            if str(active).lower() != JOURNAL_MODE.lower():
                # Not fatal — the bot works, it just works the way it did when it was
                # locking under load, so this needs to be loud rather than swallowed.
                log.warning(
                    "SQLite journal_mode is %r, not %s. Readers will block writers and "
                    "'database is locked' can come back under load. Usual cause: the DB "
                    "file is on a network mount, which WAL can't use. DB_URL=%s",
                    active, JOURNAL_MODE, DB_URL,
                )
            else:
                log.info(
                    "SQLite: journal_mode=%s, synchronous=%s, busy_timeout=%ds, pool=%d+%d",
                    active, SYNCHRONOUS, BUSY_TIMEOUT_SECONDS, POOL_SIZE, MAX_OVERFLOW,
                )
    finally:
        cursor.close()


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
