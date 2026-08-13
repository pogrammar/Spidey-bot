import datetime
import random

import discord
from discord.ext import commands

from db.base import async_session
from services.brewing_service import BREW_COST, collect_brew, get_brew_status, start_brew
from services.economy import get_or_create_user
from utils.embeds import error_embed
from utils.v2_embeds import StaticView

LAB_FOOTERS = [
    "ESU's chem lab was not built for this.",
    "Somewhere, a professor is very confused about the missing beakers.",
    "Web fluid: 10% chemistry, 90% vibes.",
    "Curt Connors would not approve of these safety standards.",
]


class LabCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    lab = discord.SlashCommandGroup("lab", "Empire State University's chem lab — brew Web-Fluid on the side.")

    @lab.command(name="status", description="Check on your current batch.")
    async def status(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            brew = await get_brew_status(session, user.discord_id)

        if brew is None:
            view = StaticView(
                "Chem Lab",
                f"Nothing brewing. Start a batch for ${BREW_COST} with /lab brew.",
                footer_lines=[random.choice(LAB_FOOTERS)],
            )
            await ctx.respond(view=view)
            return

        now = datetime.datetime.utcnow()
        if brew.ready_at <= now:
            status_text = "Ready to collect — run /lab collect."
        else:
            minutes = int((brew.ready_at - now).total_seconds() // 60)
            status_text = f"Still cooking, about {minutes} minutes left."
        view = StaticView("Chem Lab", status_text, footer_lines=[random.choice(LAB_FOOTERS)])
        await ctx.respond(view=view)

    @lab.command(name="brew", description="Start a new Web-Fluid batch.")
    async def brew(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await start_brew(session, user)
        if ok:
            view = StaticView("Chem Lab", description=message, footer_lines=[random.choice(LAB_FOOTERS)])
            await ctx.respond(view=view)
        else:
            await ctx.respond(embed=error_embed(message))

    @lab.command(name="collect", description="Collect your batch, if it's ready.")
    async def collect(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message, result = await collect_brew(session, user)

        if not ok:
            await ctx.respond(embed=error_embed(message))
            return

        fields = []
        if result.mutated:
            fields.append((
                "Mutation!", "One vial came out wrong — an Unstable Web-Fluid. Might be worth something.",
            ))
        view = StaticView(
            "Chem Lab — Batch Ready",
            f"You collect {result.vials}x Web-Fluid Vial.",
            fields=fields,
            footer_lines=[random.choice(LAB_FOOTERS)],
        )
        await ctx.respond(view=view)


def setup(bot: discord.Bot):
    bot.add_cog(LabCog(bot))
