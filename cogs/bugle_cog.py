import random

import discord
from discord.ext import commands

from db.base import async_session
from services.biomorphic_service import scavenge_subtext
from services.bugle_service import BUGLE_COOLDOWN_SECONDS, get_pending_summary, submit_photos
from services.cooldowns import format_remaining, get_remaining_seconds, set_cooldown
from services.economy import get_or_create_user
from services.patreon_service import get_tier_rank
from services.patrol_service import get_effective_camera
from utils.embeds import error_embed
from utils.v2_embeds import StaticView

BUGLE_FOOTERS = [
    "Jameson still thinks you're a menace. The check clears anyway.",
    "Front page material, allegedly.",
    "Betty Brant is the only one who actually likes you here.",
    "JJJ approved this payment through gritted teeth.",
]


class BugleCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    bugle = discord.SlashCommandGroup("bugle", "Deal with J. Jonah Jameson.")

    @bugle.command(name="photos", description="See what photos you're sitting on before you sell them.")
    async def photos(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            summary = await get_pending_summary(session, user)
            # Read inside the session so the card's icon is the body they actually shoot
            # on. "Your Camera Roll" showing a beat-up 35mm to somebody with a Gold camera
            # equipped was the most visible instance of the icon ignoring what they own.
            camera = await get_effective_camera(session, user.discord_id)

        if not summary.breakdown:
            await ctx.respond(
                embed=error_embed("No photos on you right now. Get out there and /patrol first.")
            )
            return

        quality_fields = [(quality.title(), f"x{count}") for quality, count in summary.breakdown.items()]
        description = (
            f"Worth an estimated ${summary.estimated_min:,} - ${summary.estimated_max:,}. "
            "Not sold yet — run /bugle submit to cash these in."
        )
        view = StaticView(
            "Your Camera Roll",
            description,
            field_groups=[("On Hand", quality_fields)],
            footer_lines=[random.choice(BUGLE_FOOTERS)],
            icon_key=camera.item_key,
        )
        await ctx.respond(view=view, files=view.files)

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

            tier_rank = await get_tier_rank(session, user.discord_id)
            result = await submit_photos(session, user, tier_rank)
            if result is None:
                await ctx.respond(
                    embed=error_embed("You don't have any photos to sell. Get out there and /patrol first.")
                )
                return

            await set_cooldown(session, user.discord_id, "bugle_submit", BUGLE_COOLDOWN_SECONDS)

        breakdown = ", ".join(f"{count}x {quality}" for quality, count in result.breakdown.items())
        complications = []
        if result.jam_flavor:
            sign = "+" if result.jam_handled else "-"
            complications.append(("Close Call", f"{result.jam_flavor} ({sign}${result.jam_amount:,})"))

        # Subtext under the payout rather than its own field — same attribution shape the
        # perk uses everywhere else (see patrol_cog._biomorphic_cash_line).
        payout_value = f"${result.total_cash:,}"
        if result.scavenged:
            payout_value += scavenge_subtext(result.scavenged, tier_rank)

        field_groups = [("Sale", [("Payout", payout_value), ("Breakdown", breakdown)])]
        if complications:
            field_groups.append(("Complications", complications))

        view = StaticView(
            "Daily Bugle — Sold!",
            f"JJJ grumbles but pays up for {result.photos_sold} photo(s).",
            field_groups=field_groups,
            footer_lines=[random.choice(BUGLE_FOOTERS)],
            icon_key="money",
        )
        await ctx.respond(view=view, files=view.files)


def setup(bot: discord.Bot):
    bot.add_cog(BugleCog(bot))
