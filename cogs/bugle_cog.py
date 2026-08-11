import discord
from discord.ext import commands

from db.base import async_session
from services.bugle_service import BUGLE_COOLDOWN_SECONDS, get_pending_summary, submit_photos
from services.cooldowns import format_remaining, get_remaining_seconds, set_cooldown
from services.economy import get_or_create_user
from utils.embeds import base_embed, error_embed


class BugleCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    bugle = discord.SlashCommandGroup("bugle", "Deal with J. Jonah Jameson.")

    @bugle.command(name="photos", description="See what photos you're sitting on before you sell them.")
    async def photos(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            summary = await get_pending_summary(session, user)

        if not summary.breakdown:
            await ctx.respond(
                embed=error_embed("No photos on you right now. Get out there and /patrol first.")
            )
            return

        embed = base_embed("Your Camera Roll", "Not sold yet — run /bugle submit to cash these in.")
        for quality, count in summary.breakdown.items():
            embed.add_field(name=quality.title(), value=f"x{count}", inline=True)
        embed.add_field(
            name="Estimated Total",
            value=f"${summary.estimated_min:,} - ${summary.estimated_max:,}",
            inline=False,
        )
        await ctx.respond(embed=embed)

    @bugle.command(name="submit", description="Sell your captured photos to the Daily Bugle.")
    async def submit(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)

            remaining = await get_remaining_seconds(session, user.discord_id, "bugle_submit")
            if remaining > 0:
                await ctx.respond(
                    embed=error_embed(
                        f"Jameson's still fuming about your last pitch. "
                        f"Try again in {format_remaining(remaining)}."
                    )
                )
                return

            result = await submit_photos(session, user)
            if result is None:
                await ctx.respond(
                    embed=error_embed("You don't have any photos to sell. Get out there and /patrol first.")
                )
                return

            await set_cooldown(session, user.discord_id, "bugle_submit", BUGLE_COOLDOWN_SECONDS)

        embed = base_embed(
            "Daily Bugle — Sold!", f"JJJ grumbles but pays up for {result.photos_sold} photo(s)."
        )
        embed.add_field(name="Payout", value=f"${result.total_cash:,}")
        breakdown = ", ".join(f"{count}x {quality}" for quality, count in result.breakdown.items())
        embed.add_field(name="Breakdown", value=breakdown)
        if result.ally_earnings_penalty:
            embed.add_field(
                name="Distracted",
                value="Someone in your life needs attention — these photos came out worse for it. Check /ally check.",
                inline=False,
            )
        if result.jam_flavor:
            sign = "+" if result.jam_handled else "-"
            embed.add_field(name="Close Call", value=f"{result.jam_flavor} ({sign}${result.jam_amount:,})", inline=False)
        await ctx.respond(embed=embed)


def setup(bot: discord.Bot):
    bot.add_cog(BugleCog(bot))
