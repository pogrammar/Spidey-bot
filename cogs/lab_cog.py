import datetime

import discord
from discord.ext import commands

from db.base import async_session
from services.brewing_service import BREW_COST, collect_brew, get_brew_status, start_brew
from services.economy import get_or_create_user
from utils.embeds import SPIDEY_BLUE, base_embed, error_embed


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
            await ctx.respond(
                embed=base_embed("Chem Lab", f"Nothing brewing. Start a batch for ${BREW_COST} with /lab brew.", colour=SPIDEY_BLUE)
            )
            return

        now = datetime.datetime.utcnow()
        if brew.ready_at <= now:
            status_text = "Ready to collect — run /lab collect."
        else:
            minutes = int((brew.ready_at - now).total_seconds() // 60)
            status_text = f"Still cooking, about {minutes} minutes left."
        await ctx.respond(embed=base_embed("Chem Lab", status_text, colour=SPIDEY_BLUE))

    @lab.command(name="brew", description="Start a new Web-Fluid batch.")
    async def brew(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await start_brew(session, user)
        await ctx.respond(embed=base_embed("Chem Lab", message) if ok else error_embed(message))

    @lab.command(name="collect", description="Collect your batch, if it's ready.")
    async def collect(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message, result = await collect_brew(session, user)

        if not ok:
            await ctx.respond(embed=error_embed(message))
            return

        embed = base_embed("Chem Lab — Batch Ready", f"You collect {result.vials}x Web-Fluid Vial.")
        if result.mutated:
            embed.add_field(
                name="Mutation!", value="One vial came out wrong — an Unstable Web-Fluid. Might be worth something.", inline=False
            )
        await ctx.respond(embed=embed)


def setup(bot: discord.Bot):
    bot.add_cog(LabCog(bot))
