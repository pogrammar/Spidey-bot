import datetime
import random

import discord
from discord.ext import commands

from db.base import async_session
from services.brewing_service import BREW_COST, collect_brew, get_brew_status, start_brew
from services.economy import get_or_create_user
from utils.embeds import error_embed
from utils.icons import item_label
from utils.v2_embeds import StaticView

LAB_ICON = "lab"
VIAL_ICON = "web_fluid_vial"
MUTATION_ICON = "unstable_web_fluid"

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
                icon_key=LAB_ICON,
            )
            await ctx.respond(view=view, files=view.files)
            return

        now = datetime.datetime.utcnow()
        if brew.ready_at <= now:
            status_text = "Ready to collect — run /lab collect."
        else:
            minutes = int((brew.ready_at - now).total_seconds() // 60)
            status_text = f"Still cooking, about {minutes} minutes left."
        view = StaticView("Chem Lab", status_text, footer_lines=[random.choice(LAB_FOOTERS)], icon_key=LAB_ICON)
        await ctx.respond(view=view, files=view.files)

    @lab.command(name="brew", description="Start a new Web-Fluid batch.")
    async def brew(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await start_brew(session, user)
        if ok:
            view = StaticView(
                "Chem Lab", description=message, footer_lines=[random.choice(LAB_FOOTERS)], icon_key=VIAL_ICON
            )
            await ctx.respond(view=view, files=view.files)
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
            mutation_label = item_label(MUTATION_ICON, "Unstable Web-Fluid")
            fields.append((
                "Mutation!", f"One vial came out wrong — an {mutation_label}. Might be worth something.",
            ))
        view = StaticView(
            "Chem Lab — Batch Ready",
            f"You collect {result.vials}x {item_label(VIAL_ICON, 'Web-Fluid Vial')}.",
            fields=fields,
            footer_lines=[random.choice(LAB_FOOTERS)],
            icon_key=MUTATION_ICON if result.mutated else VIAL_ICON,
        )
        await ctx.respond(view=view, files=view.files)


def setup(bot: discord.Bot):
    bot.add_cog(LabCog(bot))
