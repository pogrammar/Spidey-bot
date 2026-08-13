import random

import discord
from discord.ext import commands

from db.base import async_session
from services.busy import get_busy, set_busy
from services.cooldowns import format_remaining, get_remaining_seconds, set_cooldown
from services.economy import get_or_create_user
from services.tutoring_service import TUTORING_LOCK_SECONDS, run_tutoring_session
from utils.embeds import error_embed
from utils.v2_embeds import StaticView

TUTORING_FOOTERS = [
    "Calculus doesn't stop just because the city needs you.",
    "Somewhere a student is failing because of you. Worth it.",
    "Peter Parker: genius by day, questionable life choices by night.",
    "ESU tuition isn't going to pay itself.",
]


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

        cash_note = " (neglected ally penalty)" if result.ally_earnings_penalty else ""
        xp_note = " (thriving allies bonus)" if result.ally_xp_bonus else ""
        earnings_fields = [
            ("Cash", f"+${result.cash:,}{cash_note}"),
            ("Reputation XP", f"+{result.xp}{xp_note}"),
        ]
        cost_fields = [
            (
                "City Crime Level",
                f"+{result.crime_rise} (now {result.new_crime_level}/100) — nobody's out there while you're stuck inside.",
            )
        ]
        if result.ally_earnings_penalty:
            cost_fields.append((
                "Distracted",
                "Someone in your life needs attention and it's costing you focus — check /ally check.",
            ))
        if result.jam_flavor:
            value = result.jam_flavor
            if result.jam_handled:
                value += f" (+${result.jam_cash:,})"
            cost_fields.append(("Close Call", value))

        view = StaticView(
            "Tutoring Session",
            "You duck into a study room and drill calculus into someone who'd rather be anywhere else.",
            field_groups=[("Earnings", earnings_fields), ("Cost", cost_fields)],
            footer_lines=["You can't /patrol again for 2 minutes.", random.choice(TUTORING_FOOTERS)],
        )
        await ctx.respond(view=view)


def setup(bot: discord.Bot):
    bot.add_cog(TutoringCog(bot))
