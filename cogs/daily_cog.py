import random

import discord
from discord.ext import commands

from db.base import async_session
from services.cooldowns import format_remaining
from services.daily_service import claim_daily, get_streak_status
from services.economy import get_or_create_user
from utils.embeds import error_embed
from utils.icons import emoji
from utils.v2_embeds import StaticView

DAILY_FOOTERS = [
    "Consistency: the real superpower nobody talks about.",
    "Miss a day and the whole bit falls apart. No pressure.",
    "J. Jonah Jameson doesn't believe in streaks. You do.",
    "Every hero needs a routine. This is apparently yours.",
]


def _progress_bar(streak: int, target: int, segments: int = 10) -> str:
    filled_emoji = emoji("streak_bar_filled") or "🟩"
    empty_emoji = emoji("streak_bar_empty") or "⬜"
    if target <= 0:
        return filled_emoji * segments
    filled = max(0, min(segments, round((streak / target) * segments)))
    return filled_emoji * filled + empty_emoji * (segments - filled)


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
        headline = f"Day {result.streak}"
        if broke_streak:
            headline += " — streak reset, back to day 1"

        field_groups = [
            (
                "Rewards",
                [
                    ("Cash", f"+${result.cash_gained:,}"),
                    (f"{emoji('reputation') or ''} Reputation XP".strip(), f"+{result.xp_gained}"),
                    ("Longest Streak", f"{result.longest_streak} days"),
                ],
            )
        ]

        if result.bonus_flavor:
            field_groups.append((f"{emoji('gift')} Bonus Pull", [("", result.bonus_flavor)]))

        if result.milestone_label:
            milestone_value = (
                f"**{result.milestone_label}** — included above: "
                f"+${result.milestone_cash:,}, +{result.milestone_xp} XP"
            )
            field_groups.append((f"{emoji('victory')} Milestone!", [("", milestone_value)]))

        if status and status.next_milestone:
            progress_value = f"{_progress_bar(result.streak, status.next_milestone)}  ({status.days_to_next_milestone} to go)"
            field_groups.append((None, [(f"Next milestone: Day {status.next_milestone}", progress_value)]))

        view = StaticView(
            headline,
            field_groups=field_groups,
            footer_lines=[
                "Miss more than 48 hours and the streak resets — come back tomorrow.",
                random.choice(DAILY_FOOTERS),
            ],
            icon_key="streak",
        )
        await ctx.respond(view=view, files=view.files)

    @daily.command(name="status", description="Check your streak without claiming.")
    async def status(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            status = await get_streak_status(session, user)

        fields = [("Longest Streak", f"{status.longest_streak} days")]

        ready_emoji = emoji("ready") or "✅"
        if status.can_claim:
            fields.append(("Status", f"{ready_emoji} Ready to claim — run /daily claim"))
        else:
            risk_note = " (miss it and your streak resets)" if status.at_risk else ""
            fields.append(("Status", f"Next claim in {format_remaining(status.seconds_until_claim)}{risk_note}"))

        if status.next_milestone:
            fields.append((
                f"Next milestone: Day {status.next_milestone}",
                f"{_progress_bar(status.streak, status.next_milestone)}  ({status.days_to_next_milestone} to go)",
            ))

        view = StaticView(
            f"{status.streak}-Day Streak",
            fields=fields,
            footer_lines=[random.choice(DAILY_FOOTERS)],
            icon_key="streak",
        )
        await ctx.respond(view=view, files=view.files)


def setup(bot: discord.Bot):
    bot.add_cog(DailyCog(bot))
