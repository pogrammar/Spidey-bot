import random

import discord
from discord.ext import commands

from db.base import async_session
from services.economy import get_or_create_user
from services.inventory_service import get_quantity
from services.patreon_service import TIER_RANK_SYMBIOTE, get_tier_rank, tier_badge
from services.suit_service import (
    ELECTRONICS_ITEM_KEY,
    ELECTRONICS_THRESHOLD,
    REPAIR_COST_PER_POINT,
    SPANDEX_ITEM_KEY,
    repair_suit,
)
from utils.embeds import error_embed
from utils.icons import emoji, item_label
from utils.v2_embeds import StaticView

WORKBENCH_FOOTERS = [
    "Held together with duct tape and pure willpower.",
    "Aunt May still doesn't know what you do to this suit.",
    "Every rip has a story. Most of them end in a dumpster.",
    "Reed Richards would be appalled by this stitching.",
]

# The Symbiote reskin's presentation half — the prose lives in suit_service (see the block
# above REPAIR_COST_PER_POINT for why the mechanics are untouched). Nothing here is stitched
# or soldered: Peter puts the components down and the thing does the rest without telling him
# anything. The footers keep the same "you don't understand what you're wearing" register the
# tier's combat copy uses, rather than the workbench's competent-tinkerer one.
SYMBIOTE_WORKBENCH_FOOTERS = [
    "You put the components down. It does the rest without asking.",
    "Reed Richards would want a sample. He is not getting one.",
    "Whatever it's building in there, it isn't showing you the seams.",
    "It heals faster than you do. You've stopped finding that reassuring.",
]


def _panel_title(tier_rank: int) -> str:
    return "The Bond" if tier_rank >= TIER_RANK_SYMBIOTE else "Workbench"


def _panel_footers(tier_rank: int) -> list[str]:
    pool = SYMBIOTE_WORKBENCH_FOOTERS if tier_rank >= TIER_RANK_SYMBIOTE else WORKBENCH_FOOTERS
    return [random.choice(pool)]


def _cost_label(tier_rank: int) -> str:
    return "What It Wants" if tier_rank >= TIER_RANK_SYMBIOTE else "Full Repair Cost"


def _components_label(tier_rank: int) -> str:
    return (
        "What You Can Give It"
        if tier_rank >= TIER_RANK_SYMBIOTE
        else "Components on Hand"
    )


def _panel_intro(tier_rank: int) -> str | None:
    """The one line that tells a Symbiote subscriber their subscription is why this panel
    reads the way it does — GAME_DESIGN §9 attribution, since a reskin nobody can attribute
    is indistinguishable from the bot having been rewritten. Renders the badge alone, never
    the tier name, and degrades to the bare sentence if the emoji isn't uploaded."""
    if tier_rank < TIER_RANK_SYMBIOTE:
        return None
    badge = tier_badge(tier_rank)
    trail = f" {badge}" if badge else ""
    return (
        "It doesn't want thread and it doesn't want solder. It wants the same two things "
        f"the fabric did, and it has never once explained why.{trail}"
    )


class SuitCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    workbench = discord.SlashCommandGroup("workbench", "Patch up the suit.")

    @workbench.command(name="status", description="Check suit integrity, repair cost, and components on hand.")
    async def status(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            spandex = await get_quantity(session, user.discord_id, SPANDEX_ITEM_KEY)
            electronics = await get_quantity(session, user.discord_id, ELECTRONICS_ITEM_KEY)
            tier_rank = await get_tier_rank(session, user.discord_id)

        missing = 100 - user.suit_integrity
        # "Suit Integrity" stays "Suit Integrity" at every tier on purpose — it's the same
        # number /balance and the boss gate show, and renaming it here alone would make one
        # stat look like two. See the reskin note in services/suit_service.py.
        suit_fields = [(f"{emoji('suit_integrity') or ''} Suit Integrity".strip(), f"{user.suit_integrity}%")]
        if missing > 0:
            cost = missing * REPAIR_COST_PER_POINT
            extra = f" + 1 {item_label(ELECTRONICS_ITEM_KEY, 'Micro-Electronics')}" if missing >= ELECTRONICS_THRESHOLD else ""
            suit_fields.append(
                (_cost_label(tier_rank), f"${cost:,} + 1 {item_label(SPANDEX_ITEM_KEY, 'Spandex Fabric')}{extra}")
            )

        component_fields = [
            (item_label(SPANDEX_ITEM_KEY, "Spandex Fabric"), str(spandex)),
            (item_label(ELECTRONICS_ITEM_KEY, "Micro-Electronics"), str(electronics)),
        ]

        footer_lines = _panel_footers(tier_rank)
        if user.eviction_meter >= 100:
            footer_lines.insert(
                0,
                "Locked out — nowhere private to let it work until rent's paid."
                if tier_rank >= TIER_RANK_SYMBIOTE
                else "Locked out — workbench access cut off until rent's paid.",
            )

        view = StaticView(
            _panel_title(tier_rank),
            _panel_intro(tier_rank) or "",
            field_groups=[("Suit", suit_fields), (_components_label(tier_rank), component_fields)],
            footer_lines=footer_lines,
            icon_key="suit_integrity",
        )
        await ctx.respond(view=view, files=view.files)

    @workbench.command(name="repair", description="Repair your suit back to 100%.")
    async def repair(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            tier_rank = await get_tier_rank(session, user.discord_id)
            result = await repair_suit(session, user, tier_rank)

        if not result.success:
            await ctx.respond(embed=error_embed(result.message))
            return

        extra = f" + 1 {item_label(ELECTRONICS_ITEM_KEY, 'Micro-Electronics')}" if result.used_electronics else ""
        symbiote = tier_rank >= TIER_RANK_SYMBIOTE
        view = StaticView(
            _panel_title(tier_rank),
            result.message,
            fields=[
                (
                    "What It Took" if symbiote else "Cost",
                    f"${result.cash_cost:,} + 1 {item_label(SPANDEX_ITEM_KEY, 'Spandex Fabric')}{extra}",
                ),
                ("Closed Up" if symbiote else "Restored", f"+{result.restored}%"),
            ],
            footer_lines=_panel_footers(tier_rank),
            icon_key="suit_integrity",
        )
        await ctx.respond(view=view, files=view.files)


def setup(bot: discord.Bot):
    bot.add_cog(SuitCog(bot))
