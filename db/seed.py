import json
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Item

ITEMS_PATH = Path(__file__).resolve().parent.parent / "data" / "items.json"


async def seed_items(session: AsyncSession) -> None:
    """Upserts item definitions from data/items.json. Safe to call on every startup."""
    with open(ITEMS_PATH, encoding="utf-8") as f:
        entries = json.load(f)

    for entry in entries:
        existing = await session.get(Item, entry["key"])
        if existing is None:
            session.add(Item(**entry))
        else:
            existing.name = entry["name"]
            existing.category = entry["category"]
            existing.description = entry.get("description", "")
            existing.max_durability = entry.get("max_durability")
            existing.price = entry.get("price")
            existing.happiness_boost = entry.get("happiness_boost")
            existing.unlock_level = entry.get("unlock_level")

    await session.commit()
