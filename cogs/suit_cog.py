import random

import discord
from discord.ext import commands

from db.base import async_session
from services.economy import get_or_create_user
from services.inventory_service import get_quantity
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

        missing = 100 - user.suit_integrity
        suit_fields = [(f"{emoji('suit_integrity') or ''} Suit Integrity".strip(), f"{user.suit_integrity}%")]
        if missing > 0:
            cost = missing * REPAIR_COST_PER_POINT
            extra = f" + 1 {item_label(ELECTRONICS_ITEM_KEY, 'Micro-Electronics')}" if missing >= ELECTRONICS_THRESHOLD else ""
            suit_fields.append(
                ("Full Repair Cost", f"${cost:,} + 1 {item_label(SPANDEX_ITEM_KEY, 'Spandex Fabric')}{extra}")
            )

        component_fields = [
            (item_label(SPANDEX_ITEM_KEY, "Spandex Fabric"), str(spandex)),
            (item_label(ELECTRONICS_ITEM_KEY, "Micro-Electronics"), str(electronics)),
        ]

        footer_lines = [random.choice(WORKBENCH_FOOTERS)]
        if user.eviction_meter >= 100:
            footer_lines.insert(0, "Locked out — workbench access cut off until rent's paid.")

        view = StaticView(
            "Workbench",
            field_groups=[("Suit", suit_fields), ("Components on Hand", component_fields)],
            footer_lines=footer_lines,
            icon_key="suit_integrity",
        )
        await ctx.respond(view=view, files=view.files)

    @workbench.command(name="repair", description="Repair your suit back to 100%.")
    async def repair(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            result = await repair_suit(session, user)

        if not result.success:
            await ctx.respond(embed=error_embed(result.message))
            return

        extra = f" + 1 {item_label(ELECTRONICS_ITEM_KEY, 'Micro-Electronics')}" if result.used_electronics else ""
        view = StaticView(
            "Workbench",
            result.message,
            fields=[
                ("Cost", f"${result.cash_cost:,} + 1 {item_label(SPANDEX_ITEM_KEY, 'Spandex Fabric')}{extra}"),
                ("Restored", f"+{result.restored}%"),
            ],
            footer_lines=[random.choice(WORKBENCH_FOOTERS)],
            icon_key="suit_integrity",
        )
        await ctx.respond(view=view, files=view.files)


def setup(bot: discord.Bot):
    bot.add_cog(SuitCog(bot))
