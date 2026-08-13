from __future__ import annotations

import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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

    inventory_items: Mapped[list["InventoryItem"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    pending_photos: Mapped[list["PendingPhoto"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def reputation_level(self) -> int:
        return 1 + self.reputation_xp // 100

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
