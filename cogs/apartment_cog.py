import datetime

import discord
from discord.ext import commands

from db.base import async_session
from services.apartment_service import RENT_AMOUNT, pay_rent_manually
from services.cooldowns import format_remaining
from services.economy import get_or_create_user
from utils.embeds import SPIDEY_BLUE, base_embed, error_embed


class ApartmentCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    apartment = discord.SlashCommandGroup("apartment", "Deal with your landlord.")

    @apartment.command(name="status", description="Check your rent due date and eviction status.")
    async def status(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            seconds_until_due = (user.next_rent_due - datetime.datetime.utcnow()).total_seconds()

        embed = base_embed("Apartment", colour=SPIDEY_BLUE)
        embed.add_field(name="Rent", value=f"${RENT_AMOUNT:,} / week")
        if seconds_until_due > 0:
            embed.add_field(name="Due In", value=format_remaining(seconds_until_due))
        else:
            embed.add_field(name="Status", value="**Overdue** — pay now before the eviction meter climbs further.")
        embed.add_field(name="Eviction Meter", value=f"{user.eviction_meter}/100")
        if user.eviction_meter >= 100:
            embed.add_field(
                name="Locked Out", value="Your workbench access is cut off until you pay up.", inline=False
            )
        await ctx.respond(embed=embed)

    @apartment.command(name="pay", description="Pay your rent out of your bank.")
    async def pay(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await pay_rent_manually(session, user)
        await ctx.respond(embed=base_embed("Apartment", message) if ok else error_embed(message))


def setup(bot: discord.Bot):
    bot.add_cog(ApartmentCog(bot))
