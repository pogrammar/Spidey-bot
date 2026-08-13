from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AdminUser


async def is_granted_admin(session: AsyncSession, discord_id: int) -> bool:
    return await session.get(AdminUser, discord_id) is not None


async def grant_admin(session: AsyncSession, discord_id: int, granted_by: int) -> bool:
    """Returns False if this user already has runtime-granted access."""
    if await session.get(AdminUser, discord_id) is not None:
        return False
    session.add(AdminUser(discord_id=discord_id, granted_by=granted_by))
    await session.commit()
    return True


async def revoke_admin(session: AsyncSession, discord_id: int) -> bool:
    """Returns False if this user had no runtime-granted access to revoke."""
    row = await session.get(AdminUser, discord_id)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def list_granted_admins(session: AsyncSession) -> list[AdminUser]:
    stmt = select(AdminUser).order_by(AdminUser.granted_at)
    return list((await session.execute(stmt)).scalars())
