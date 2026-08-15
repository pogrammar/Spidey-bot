import random

import discord
from discord import Option
from discord.ext import commands

from db.base import async_session
from services.cooldowns import format_remaining, get_remaining_seconds, set_cooldown
from services.economy import get_or_create_user
from services.shakedown_service import (
    MIN_TARGET_WALLET,
    SHAKEDOWN_COOLDOWN_SECONDS,
    TARGET_PROTECTION_SECONDS,
    attempt_shakedown,
)
from utils.embeds import error_embed
from utils.v2_embeds import StaticView

SHAKEDOWN_FOOTERS = [
    "Not exactly hero material, but the rent's due.",
    "Parker Luck strikes again — this time in your favor.",
    "Even Spider-Man needs a side hustle sometimes.",
    "You'll feel bad about this later. Probably.",
]


class PvpCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="shakedown", description="Desperate for cash? Shake down another player.")
    async def shakedown(
        self,
        ctx: discord.ApplicationContext,
        target: Option(discord.Member, "Who are you shaking down?"),
    ):
        if target.id == ctx.author.id:
            await ctx.respond(embed=error_embed("You can't shake yourself down. That's just called stress."))
            return
        if target.bot:
            await ctx.respond(embed=error_embed("It's a bot. It has no wallet. Leave it alone."))
            return

        async with async_session() as session:
            thief = await get_or_create_user(session, ctx.author.id)
            victim = await get_or_create_user(session, target.id)

            remaining = await get_remaining_seconds(session, thief.discord_id, "shakedown")
            if remaining > 0:
                await ctx.respond(embed=error_embed(f"Lay low a bit. Try again in {format_remaining(remaining)}."))
                return

            protected = await get_remaining_seconds(session, victim.discord_id, "shakedown_target")
            if protected > 0:
                await ctx.respond(
                    embed=error_embed(
                        f"{target.display_name} is still on guard from the last hit. "
                        f"Try someone else, or wait {format_remaining(protected)}."
                    )
                )
                return

            if victim.wallet < MIN_TARGET_WALLET:
                await ctx.respond(
                    embed=error_embed(f"{target.display_name} is broker than you right now. Not worth the risk.")
                )
                return

            result = await attempt_shakedown(session, thief, victim)
            await set_cooldown(session, thief.discord_id, "shakedown", SHAKEDOWN_COOLDOWN_SECONDS)
            await set_cooldown(session, victim.discord_id, "shakedown_target", TARGET_PROTECTION_SECONDS)

        if result.success:
            view = StaticView(
                "Shakedown — Success",
                f"You corner {target.display_name} in an alley and lighten their pockets.",
                fields=[("Cash Taken", f"${result.amount:,}")],
                footer_lines=[random.choice(SHAKEDOWN_FOOTERS)],
                icon_key="pvp",
            )
            await ctx.respond(view=view, files=view.files)
        else:
            embed = error_embed(f"{target.display_name} fights back — or someone sees you. You bail, but not clean.")
            embed.add_field(name="Cash Lost", value=f"${result.amount:,}")
            await ctx.respond(embed=embed)


def setup(bot: discord.Bot):
    bot.add_cog(PvpCog(bot))
