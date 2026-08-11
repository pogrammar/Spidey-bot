from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import InventoryItem, Item, MarketListing, User
from services.economy import add_wallet
from services.inventory_service import add_item, remove_item

MAX_ACTIVE_LISTINGS_PER_USER = 10
NOT_TRADEABLE_CATEGORIES = ("tool", "gadget")


@dataclass
class ListingView:
    id: int
    seller_id: int
    item_key: str
    item_name: str
    quantity: int
    price_per_unit: int


@dataclass
class SellableView:
    item_key: str
    name: str
    quantity: int


async def list_sellable_items(session: AsyncSession, user_id: int) -> list[SellableView]:
    """What this user could actually list — owned, unequipped, and a tradeable category."""
    stmt = (
        select(InventoryItem, Item.name)
        .join(Item, InventoryItem.item_key == Item.key)
        .where(
            InventoryItem.user_id == user_id,
            InventoryItem.equipped.is_(False),
            Item.category.notin_(NOT_TRADEABLE_CATEGORIES),
            InventoryItem.quantity > 0,
        )
    )
    rows = (await session.execute(stmt)).all()
    return [SellableView(item_key=inv.item_key, name=name, quantity=inv.quantity) for inv, name in rows]


async def create_listing(
    session: AsyncSession, seller: User, item_key: str, quantity: int, price_per_unit: int
) -> tuple[bool, str]:
    if quantity <= 0 or price_per_unit <= 0:
        return False, "Quantity and price both need to be positive."

    item = await session.get(Item, item_key)
    if item is None:
        return False, f"No such item: `{item_key}`."
    if item.category == "tool":
        return False, "Tools can't be listed while equipped — nothing to trade there yet."
    if item.category == "gadget":
        return False, "Gadgets track their own durability and upgrades per copy — can't be traded through here yet."

    count_stmt = select(MarketListing).where(MarketListing.seller_id == seller.discord_id)
    active_count = len((await session.execute(count_stmt)).scalars().all())
    if active_count >= MAX_ACTIVE_LISTINGS_PER_USER:
        return False, f"You've already got {MAX_ACTIVE_LISTINGS_PER_USER} listings up. Cancel one first."

    if not await remove_item(session, seller.discord_id, item_key, quantity):
        return False, f"You don't have {quantity}x {item.name}."

    listing = MarketListing(
        seller_id=seller.discord_id,
        item_key=item_key,
        quantity=quantity,
        price_per_unit=price_per_unit,
    )
    session.add(listing)
    await session.commit()
    return True, f"Listed {quantity}x {item.name} at ${price_per_unit:,} each. Listing ID: **#{listing.id}** — cancel with /market cancel."


async def cancel_listing(session: AsyncSession, seller: User, listing_id: int) -> tuple[bool, str]:
    listing = await session.get(MarketListing, listing_id)
    if listing is None:
        return False, "That listing doesn't exist."
    if listing.seller_id != seller.discord_id:
        return False, "That's not your listing."

    await add_item(session, seller.discord_id, listing.item_key, listing.quantity)
    await session.delete(listing)
    await session.commit()
    return True, "Listing cancelled, items back in your inventory."


async def buy_listing(session: AsyncSession, buyer: User, listing_id: int) -> tuple[bool, str]:
    listing = await session.get(MarketListing, listing_id)
    if listing is None:
        return False, "That listing doesn't exist — someone may have already bought it."
    if listing.seller_id == buyer.discord_id:
        return False, "You can't buy your own listing."

    total_cost = listing.quantity * listing.price_per_unit
    if buyer.wallet < total_cost:
        return False, f"That's ${total_cost:,} and your wallet's short."

    seller = await session.get(User, listing.seller_id)
    await add_wallet(session, buyer, -total_cost, reason=f"market:buy:{listing.item_key}")
    await add_wallet(session, seller, total_cost, reason=f"market:sell:{listing.item_key}")
    await add_item(session, buyer.discord_id, listing.item_key, listing.quantity)

    item = await session.get(Item, listing.item_key)
    message = f"Bought {listing.quantity}x {item.name} for ${total_cost:,}."

    await session.delete(listing)
    await session.commit()
    return True, message


async def list_active(session: AsyncSession, limit: int = 15) -> list[ListingView]:
    stmt = select(MarketListing).order_by(MarketListing.created_at.desc()).limit(limit)
    listings = list((await session.execute(stmt)).scalars())

    views = []
    for listing in listings:
        item = await session.get(Item, listing.item_key)
        views.append(
            ListingView(
                id=listing.id,
                seller_id=listing.seller_id,
                item_key=listing.item_key,
                item_name=item.name if item else listing.item_key,
                quantity=listing.quantity,
                price_per_unit=listing.price_per_unit,
            )
        )
    return views
