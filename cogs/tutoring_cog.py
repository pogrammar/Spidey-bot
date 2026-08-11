import discord
from discord.ext import commands

from db.base import async_session
from services.busy import get_busy, set_busy
from services.cooldowns import format_remaining, get_remaining_seconds, set_cooldown
from services.economy import get_or_create_user
from services.tutoring_service import TUTORING_LOCK_SECONDS, run_tutoring_session
from utils.embeds import base_embed, error_embed


class TutoringCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(
        name="tutoring",
        description="Pick up a tutoring session at ESU. Safe, steady cash — but you're off the streets for 2 minutes.",
    )
    async def tutoring(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)

            busy = await get_busy(session, user.discord_id)
            if busy is not None:
                label, remaining = busy
                await ctx.respond(
                    embed=error_embed(f"You're busy {label} right now. Free in {format_remaining(remaining)}.")
                )
                return

            remaining = await get_remaining_seconds(session, user.discord_id, "tutoring")
            if remaining > 0:
                await ctx.respond(
                    embed=error_embed(
                        f"You just wrapped a session. Try again in {format_remaining(remaining)}."
                    )
                )
                return

            result = await run_tutoring_session(session, user)
            await set_cooldown(session, user.discord_id, "tutoring", TUTORING_LOCK_SECONDS)
            await set_busy(session, user.discord_id, "tutoring", TUTORING_LOCK_SECONDS)

        embed = base_embed(
            "Tutoring Session",
            "You duck into a study room and drill calculus into someone who'd rather be anywhere else.",
        )
        cash_note = " (neglected ally penalty)" if result.ally_earnings_penalty else ""
        xp_note = " (thriving allies bonus)" if result.ally_xp_bonus else ""
        embed.add_field(name="Cash", value=f"+${result.cash:,}{cash_note}")
        embed.add_field(name="Reputation XP", value=f"+{result.xp}{xp_note}")
        embed.add_field(
            name="City Crime Level",
            value=f"+{result.crime_rise} (now {result.new_crime_level}/100) — nobody's out there while you're stuck inside.",
            inline=False,
        )
        if result.ally_earnings_penalty:
            embed.add_field(
                name="Distracted",
                value="Someone in your life needs attention and it's costing you focus — check /ally check.",
                inline=False,
            )
        if result.jam_flavor:
            value = result.jam_flavor
            if result.jam_handled:
                value += f" (+${result.jam_cash:,})"
            embed.add_field(name="Close Call", value=value, inline=False)
        embed.set_footer(text="You can't /patrol again for 2 minutes.")
        await ctx.respond(embed=embed)


def setup(bot: discord.Bot):
    bot.add_cog(TutoringCog(bot))
