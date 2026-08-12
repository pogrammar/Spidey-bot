"""Fires a short welcome nudge the first time a user ever runs any command —
registered once as a global bot.before_invoke hook (see bot.py), so no individual
cog needs to know about this. Never blocks or replaces the command the user
actually ran; it's a bonus message sent alongside it."""

import discord

from db.base import async_session
from db.models import User
from utils.embeds import SPIDEY_BLUE, base_embed

# /start already gives the full welcome experience on its own — firing this too
# would just be a redundant second welcome message for the same interaction.
FIRST_RUN_SKIP_COMMANDS = {"start"}


async def announce_if_first_time(ctx: discord.ApplicationContext) -> None:
    if ctx.command.qualified_name in FIRST_RUN_SKIP_COMMANDS:
        return

    async with async_session() as session:
        existing = await session.get(User, ctx.author.id)
    if existing is not None:
        return

    embed = base_embed(
        "Friendly Neighborhood Welcome",
        "First time swinging out? Run **/start** for a quick intro, or **/help** "
        "for the full rundown.",
        colour=SPIDEY_BLUE,
    )
    try:
        await ctx.channel.send(content=ctx.author.mention, embed=embed)
    except discord.HTTPException:
        pass
