import datetime
import random

import discord
from discord.ext import commands

from db.base import async_session
from services.apartment_service import RENT_AMOUNT, pay_rent_manually
from services.cooldowns import format_remaining
from services.economy import get_or_create_user
from utils.embeds import error_embed
from utils.v2_embeds import StaticView

APARTMENT_FOOTERS = [
    "The landlord doesn't accept 'I was busy fighting crime' as an excuse.",
    "Somewhere, a spreadsheet is judging you.",
    "Queens rent waits for no hero.",
    "Mr. Delmar would understand. Your landlord does not.",
]


class ApartmentCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    apartment = discord.SlashCommandGroup("apartment", "Deal with your landlord.")

    @apartment.command(name="status", description="Check your rent due date and eviction status.")
    async def status(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            seconds_until_due = (user.next_rent_due - datetime.datetime.utcnow()).total_seconds()

        if seconds_until_due > 0:
            description = f"${RENT_AMOUNT:,}/week — next payment due in {format_remaining(seconds_until_due)}."
        else:
            description = f"${RENT_AMOUNT:,}/week, and it's **overdue** — pay now before the eviction meter climbs further."

        footer_lines = [random.choice(APARTMENT_FOOTERS)]
        if user.eviction_meter >= 100:
            footer_lines.insert(0, "Locked out — your workbench access is cut off until you pay up.")

        view = StaticView(
            "Apartment",
            description,
            fields=[("Eviction Meter", f"{user.eviction_meter}/100")],
            footer_lines=footer_lines,
        )
        await ctx.respond(view=view)

    @apartment.command(name="pay", description="Pay your rent out of your bank.")
    async def pay(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await pay_rent_manually(session, user)
        if ok:
            view = StaticView("Apartment", description=message, footer_lines=[random.choice(APARTMENT_FOOTERS)])
            await ctx.respond(view=view)
        else:
            await ctx.respond(embed=error_embed(message))


def setup(bot: discord.Bot):
    bot.add_cog(ApartmentCog(bot))
