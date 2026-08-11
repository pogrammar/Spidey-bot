import discord
from discord.ext import commands

from db.base import async_session
from services.economy import get_or_create_user
from services.inventory_service import get_quantity
from services.suit_service import (
    ELECTRONICS_ITEM_KEY,
    ELECTRONICS_THRESHOLD,
    REPAIR_COST_PER_POINT,
    SPANDEX_ITEM_KEY,
    repair_suit,
)
from utils.embeds import SPIDEY_BLUE, base_embed, error_embed


class SuitCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    workbench = discord.SlashCommandGroup("workbench", "Patch up the suit.")

    @workbench.command(name="status", description="Check suit integrity, repair cost, and components on hand.")
    async def status(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            spandex = await get_quantity(session, user.discord_id, SPANDEX_ITEM_KEY)
            electronics = await get_quantity(session, user.discord_id, ELECTRONICS_ITEM_KEY)

        missing = 100 - user.suit_integrity
        embed = base_embed("Workbench", colour=SPIDEY_BLUE)
        embed.add_field(name="Suit Integrity", value=f"{user.suit_integrity}%")

        if missing > 0:
            cost = missing * REPAIR_COST_PER_POINT
            extra = " + 1 Micro-Electronics" if missing >= ELECTRONICS_THRESHOLD else ""
            embed.add_field(name="Full Repair Cost", value=f"${cost:,} + 1 Spandex Fabric{extra}")

        embed.add_field(name="Spandex Fabric", value=str(spandex))
        embed.add_field(name="Micro-Electronics", value=str(electronics))

        if user.eviction_meter >= 100:
            embed.add_field(
                name="Locked Out", value="Workbench access cut off until rent's paid.", inline=False
            )
        await ctx.respond(embed=embed)

    @workbench.command(name="repair", description="Repair your suit back to 100%.")
    async def repair(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            result = await repair_suit(session, user)

        if not result.success:
            await ctx.respond(embed=error_embed(result.message))
            return

        embed = base_embed("Workbench", result.message)
        extra = " + 1 Micro-Electronics" if result.used_electronics else ""
        embed.add_field(name="Cost", value=f"${result.cash_cost:,} + 1 Spandex Fabric{extra}")
        embed.add_field(name="Restored", value=f"+{result.restored}%")
        await ctx.respond(embed=embed)


def setup(bot: discord.Bot):
    bot.add_cog(SuitCog(bot))
