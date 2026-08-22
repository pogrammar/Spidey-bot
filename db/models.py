from __future__ import annotations

import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from utils.leveling import level_for_xp


class Base(AsyncAttrs, DeclarativeBase):
    """AsyncAttrs lets us do `await some_obj.awaitable_attrs.relationship_name`
    instead of hitting MissingGreenlet errors on lazy-loaded relationships."""

    pass


class User(Base):
    """A player's persistent profile. discord_id doubles as the primary key
    since the economy is global across the one server this bot runs in."""

    __tablename__ = "users"

    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    wallet: Mapped[int] = mapped_column(Integer, default=0)
    bank: Mapped[int] = mapped_column(Integer, default=0)
    bank_capacity: Mapped[int] = mapped_column(Integer, default=5000)
    reputation_xp: Mapped[int] = mapped_column(Integer, default=0)
    suit_integrity: Mapped[int] = mapped_column(Integer, default=100)
    crime_level: Mapped[int] = mapped_column(Integer, default=0)
    boss_clears: Mapped[int] = mapped_column(Integer, default=0)
    eviction_meter: Mapped[int] = mapped_column(Integer, default=0)
    next_rent_due: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.datetime.utcnow() + datetime.timedelta(days=7),
        index=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )
    daily_streak: Mapped[int] = mapped_column(Integer, default=0)
    daily_longest_streak: Mapped[int] = mapped_column(Integer, default=0)
    daily_last_claimed: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    # Stamped on every command via utils/first_run.py's global before_invoke hook
    # (the cheapest existing "runs before every command" hook — pycord only
    # supports one before_invoke slot, so this rides along rather than adding a
    # second one). Powers Stealth Mode's inactivity window — see
    # services/shakedown_service.py.
    last_active_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    inventory_items: Mapped[list["InventoryItem"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    pending_photos: Mapped[list["PendingPhoto"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def reputation_level(self) -> int:
        return level_for_xp(self.reputation_xp)

    @property
    def reputation_multiplier(self) -> float:
        """+5% payout per reputation level above 1."""
        return 1 + 0.05 * (self.reputation_level - 1)


class Item(Base):
    """Static item definitions, seeded from data/items.json at startup."""

    __tablename__ = "items"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)  # tool | consumable | component | collectible
    description: Mapped[str] = mapped_column(String, default="")
    max_durability: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = not sold in the shop
    happiness_boost: Mapped[int | None] = mapped_column(Integer, nullable=True)  # gifts only
    unlock_level: Mapped[int | None] = mapped_column(Integer, nullable=True)  # gadgets: min reputation level


class InventoryItem(Base):
    __tablename__ = "inventory_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"))
    item_key: Mapped[str] = mapped_column(String, ForeignKey("items.key"))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    durability: Mapped[int | None] = mapped_column(Integer, nullable=True)
    equipped: Mapped[bool] = mapped_column(Boolean, default=False)
    upgrade_level: Mapped[int] = mapped_column(Integer, default=0)  # gadgets only

    user: Mapped["User"] = relationship(back_populates="inventory_items")
    item: Mapped["Item"] = relationship()


class PendingPhoto(Base):
    """A captured-but-unsold photo from a patrol crime encounter, waiting on /bugle submit."""

    __tablename__ = "pending_photos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"))
    quality: Mapped[str] = mapped_column(String)  # bronze | gold
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )

    user: Mapped["User"] = relationship(back_populates="pending_photos")


class Transaction(Base):
    """Audit log for every cash-changing event. Not optional at economy-bot scale."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"))
    balance_type: Mapped[str] = mapped_column(String)  # wallet | bank
    amount: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )


class Cooldown(Base):
    __tablename__ = "cooldowns"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    command_key: Mapped[str] = mapped_column(String, primary_key=True)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime)


class MarketListing(Base):
    """A standing sell offer on the Trade Post. Items are held in escrow — removed
    from the seller's inventory the moment the listing is created, not on sale."""

    __tablename__ = "market_listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    seller_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"))
    item_key: Mapped[str] = mapped_column(String, ForeignKey("items.key"))
    quantity: Mapped[int] = mapped_column(Integer)
    price_per_unit: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )


class Ally(Base):
    """Aunt May / MJ. Happiness decays continuously (computed from last_visited_at)
    rather than needing its own background tick — see services/ally_service.py."""

    __tablename__ = "allies"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"), primary_key=True)
    ally_key: Mapped[str] = mapped_column(String, primary_key=True)  # aunt_may | mj
    banked_happiness: Mapped[int] = mapped_column(Integer, default=100)
    last_visited_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )
    consecutive_gift_visits: Mapped[int] = mapped_column(Integer, default=0)


class GiftUsage(Base):
    """How many times a specific gift has been given to a specific ally — the more a
    gift's repeated, the less it lands. See ally_service.py's diminishing-returns math."""

    __tablename__ = "gift_usage"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"), primary_key=True)
    ally_key: Mapped[str] = mapped_column(String, primary_key=True)
    gift_key: Mapped[str] = mapped_column(String, ForeignKey("items.key"), primary_key=True)
    times_given: Mapped[int] = mapped_column(Integer, default=0)


class Brew(Base):
    """A single in-progress Chem Lab batch. One active brew per user at a time."""

    __tablename__ = "brews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"), unique=True)
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )
    ready_at: Mapped[datetime.datetime] = mapped_column(DateTime)


class PatreonLink(Base):
    """A user's linked Patreon account — independent of any Discord server or role,
    since these perks are meant to be worldwide (see services/patreon_service.py).

    tier is re-read from Patreon on a schedule rather than only at link time, so a
    lapsed or downgraded pledge actually stops granting perks — refresh_stale_links()
    picks up the oldest last_checked_at rows and the scheduler cog drains that queue.
    Only a successful read may rewrite tier; a Patreon outage stamps last_checked_at
    and leaves the tier alone, so a paying subscriber never loses perks to a 503."""

    __tablename__ = "patreon_links"

    discord_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"), primary_key=True)
    patreon_user_id: Mapped[str] = mapped_column(String)
    tier: Mapped[str | None] = mapped_column(String, nullable=True)  # None = linked but no active pledge
    # Accelerated Growth (Arachnid+ perk) — "xp" or "allies", mutually exclusive by
    # construction (one field, one value at a time). None = not chosen yet. Reset
    # to None if a resubscribe or manual grant ever needs a clean slate — see
    # services/patreon_service.py's get_growth_choice() for how a lapsed tier makes
    # this inert without needing to clear the field.
    growth_perk_choice: Mapped[str | None] = mapped_column(String, nullable=True)
    access_token: Mapped[str] = mapped_column(String)
    refresh_token: Mapped[str] = mapped_column(String)
    token_expires_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    linked_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    last_checked_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)


class AdminUser(Base):
    """Runtime-granted /admin access via /admin admins add, on top of the hardcoded
    ADMIN_DISCORD_IDS in .env — those act as root (always trusted, can't be revoked
    through a command); rows here are everyone else who's been granted access and
    can also be revoked the same way they were granted."""

    __tablename__ = "admin_users"

    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    granted_by: Mapped[int] = mapped_column(BigInteger)
    granted_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )


class ShakedownAttempt(Base):
    """One row per /shakedown that actually reached the resolver, existing for a single
    question: is Stealth Mode's 20-minute inactivity threshold the right number?

    It couldn't be answered before this table. A protected attempt returns early and
    charges nobody, so unlike a success or a fail it left **no trace at all** — not even
    a `transactions` row — and the perk's real firing rate was unobservable. Unlike Venom
    Blast's multiplier (validated by simulation in scratch/combat_sim.py), this threshold
    depends on how long real players step away from Discord, which no simulation can
    supply. Only recorded attempts can settle it.

    `target_idle_seconds` is stamped on **every** attempt, protected or not, and that's
    the design: it makes any candidate threshold answerable from data already collected,
    instead of needing a fresh instrumentation deploy per number under consideration.
    Storing only a `was_protected` flag would answer for 20 minutes and nothing else.

    Written outside the caller's transaction (see log_shakedown_attempt) so instrumentation
    can never roll back real cash movement, and swallowing its own errors so a broken
    telemetry write can never fail a player's command.
    """

    __tablename__ = "shakedown_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thief_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"))
    target_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"))
    # protected | success | caught — see shakedown_service.OUTCOME_*
    outcome: Mapped[str] = mapped_column(String)
    # None when the target has never run a command (last_active_at IS NULL), which is
    # itself the reason Stealth Mode can't fire for them — distinct from "idle 0 seconds".
    target_idle_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The target's tier rank at attempt time. Needed to separate "wasn't idle long enough"
    # from "wasn't subscribed", and to read the idle distribution of non-subscribers as a
    # stand-in population while the Symbiote sample is still small.
    target_tier_rank: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer)
    target_wallet: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )

