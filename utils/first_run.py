"""Fires a short welcome nudge the first time a user ever runs any command —
registered once as a global bot.before_invoke hook (see bot.py), so no individual
cog needs to know about this. Never blocks or replaces the command the user
actually ran; it's a bonus message sent alongside it.

pycord only supports one before_invoke slot, so this is also where every other
"run on every command" need rides along rather than adding a second hook:

- the global "last active" stamp (see User.last_active_at) that Stealth Mode's
  inactivity window reads from;
- resolving the invoker's Patreon tier accent for this command (see
  utils/tier_accent.py) so every Components V2 container can pick it up without
  being passed a colour by hand."""

import datetime

import discord

from db.base import async_session
from db.models import User
from services.patreon_service import accent_for_rank, get_tier_rank
from utils.embeds import SPIDEY_BLUE, base_embed
from utils.tier_accent import set_current_accent

# /start already gives the full welcome experience on its own — firing this too
# would just be a redundant second welcome message for the same interaction.
FIRST_RUN_SKIP_COMMANDS = {"start"}


async def announce_if_first_time(ctx: discord.ApplicationContext) -> None:
    async with async_session() as session:
        # Before any early return below, and on the session that was being opened
        # anyway: the accent has to be set for /start (which skips the rest of this
        # function) and for a brand-new user's very first command alike, or those are
        # the two places a subscriber's colour would be missing.
        set_current_accent(accent_for_rank(await get_tier_rank(session, ctx.author.id)))

        if ctx.command.qualified_name in FIRST_RUN_SKIP_COMMANDS:
            return

        existing = await session.get(User, ctx.author.id)
        if existing is not None:
            existing.last_active_at = datetime.datetime.utcnow()
            await session.commit()
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
