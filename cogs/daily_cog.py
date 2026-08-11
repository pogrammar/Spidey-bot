import discord
from discord.ext import commands

from db.base import async_session
from services.cooldowns import format_remaining
from services.daily_service import claim_daily, get_streak_status
from services.economy import get_or_create_user
from utils.embeds import SPIDEY_BLUE, SPIDEY_GREEN, base_embed, error_embed


def _progress_bar(streak: int, target: int, segments: int = 10) -> str:
    if target <= 0:
        return "🟩" * segments
    filled = max(0, min(segments, round((streak / target) * segments)))
    return "🟩" * filled + "⬜" * (segments - filled)


class DailyCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    daily = discord.SlashCommandGroup("daily", "Come back every day — the streak is worth it.")

    @daily.command(name="claim", description="Claim today's reward and keep your streak alive.")
    async def claim(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message, result = await claim_daily(session, user)
            status = await get_streak_status(session, user) if ok else None

        if not ok:
            await ctx.respond(embed=error_embed(message))
            return

        broke_streak = not result.streak_continued and result.longest_streak > 1
        headline = f"🔥 Day {result.streak}"
        if broke_streak:
            headline += " — streak reset, back to day 1"

        colour = SPIDEY_GREEN if result.bonus_key not in ("none",) or result.milestone_label else SPIDEY_BLUE
        embed = base_embed(headline, colour=colour)

        embed.add_field(name="Cash", value=f"+${result.cash_gained:,}")
        embed.add_field(name="Reputation XP", value=f"+{result.xp_gained}")
        embed.add_field(name="Longest Streak", value=f"{result.longest_streak} days")

        if result.bonus_flavor:
            embed.add_field(name="🎁 Bonus Pull", value=result.bonus_flavor, inline=False)

        if result.milestone_label:
            embed.add_field(
                name="🏆 Milestone!",
                value=f"**{result.milestone_label}** — included above: +${result.milestone_cash:,}, +{result.milestone_xp} XP",
                inline=False,
            )

        if status and status.next_milestone:
            embed.add_field(
                name=f"Next milestone: Day {status.next_milestone}",
                value=f"{_progress_bar(result.streak, status.next_milestone)}  ({status.days_to_next_milestone} to go)",
                inline=False,
            )

        embed.set_footer(text="Miss more than 48 hours and the streak resets — come back tomorrow.")
        await ctx.respond(embed=embed)

    @daily.command(name="status", description="Check your streak without claiming.")
    async def status(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            status = await get_streak_status(session, user)

        embed = base_embed(f"🔥 {status.streak}-Day Streak", colour=SPIDEY_BLUE)
        embed.add_field(name="Longest Streak", value=f"{status.longest_streak} days")

        if status.can_claim:
            embed.add_field(name="Status", value="✅ Ready to claim — run /daily claim", inline=False)
        else:
            risk_note = " (miss it and your streak resets)" if status.at_risk else ""
            embed.add_field(
                name="Status",
                value=f"Next claim in {format_remaining(status.seconds_until_claim)}{risk_note}",
                inline=False,
            )

        if status.next_milestone:
            embed.add_field(
                name=f"Next milestone: Day {status.next_milestone}",
                value=f"{_progress_bar(status.streak, status.next_milestone)}  ({status.days_to_next_milestone} to go)",
                inline=False,
            )
        await ctx.respond(embed=embed)


def setup(bot: discord.Bot):
    bot.add_cog(DailyCog(bot))
