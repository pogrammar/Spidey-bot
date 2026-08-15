import random

import discord
from discord import Option
from discord.ext import commands

from db.base import async_session
from services.economy import deposit, get_or_create_user, withdraw
from utils.embeds import error_embed
from utils.icons import item_label
from utils.item_display import badge
from utils.v2_embeds import StaticView

LEDGER_FOOTERS = [
    "J. Jonah Jameson would call this 'still not enough.'",
    "Every cent of this came from getting punched in an alley.",
    "Uncle Ben never taught you accounting, unfortunately.",
    "May's rent doesn't care about your reputation level.",
]
INVENTORY_FOOTERS = [
    "Somewhere in here is a receipt Aunt May would not approve of.",
    "Pockets held together with web fluid and hope.",
    "Nothing in here is technically evidence. Probably.",
    "A very specific kind of hoarding.",
]
BANK_FOOTERS = [
    "The bank doesn't ask where superhero money comes from.",
    "Safer than your apartment. Low bar, but still.",
    "Somewhere, a teller is not thinking about your day job.",
    "Interest rates don't care about Parker Luck.",
]


class EconomyCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="balance", description="Check your wallet, bank, and reputation.")
    async def balance(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)

        view = StaticView(
            "Your Ledger",
            field_groups=[
                (
                    "Money",
                    [
                        ("Wallet", f"${user.wallet:,}"),
                        ("Bank", f"${user.bank:,} / ${user.bank_capacity:,}"),
                    ],
                ),
                (
                    "Standing",
                    [
                        ("Reputation", f"Level {user.reputation_level} ({user.reputation_xp} XP)"),
                        ("Suit Integrity", f"{user.suit_integrity}%"),
                    ],
                ),
            ],
            bulleted=True,
            footer_lines=[
                "Cash on hand is stealable. Bank cash isn't — deposit what you can.",
                random.choice(LEDGER_FOOTERS),
            ],
            icon_key="wallet",
        )
        await ctx.respond(view=view, files=view.files)

    @discord.slash_command(name="inventory", description="See what you're carrying.")
    async def inventory(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            items = await user.awaitable_attrs.inventory_items

            rows = []
            for inv_item in items:
                item_def = await inv_item.awaitable_attrs.item
                display_name = item_def.name if item_def is not None else inv_item.item_key
                category = item_def.category if item_def is not None else "misc"
                rows.append((inv_item.item_key, display_name, category, inv_item.quantity, inv_item.durability, inv_item.equipped))

        if not rows:
            await ctx.respond(embed=error_embed("Your pockets are empty."))
            return

        category_order = ["gadget", "tool", "component", "collectible", "gift"]
        category_labels = {
            "gadget": "Gadgets",
            "tool": "Tools",
            "component": "Components",
            "collectible": "Collectibles",
            "gift": "Gifts",
        }
        by_category: dict[str, list[tuple[str, str]]] = {}
        for item_key, display_name, category, quantity, durability_val, equipped_val in rows:
            durability = f" ({durability_val} durability)" if durability_val is not None else ""
            equipped = " — equipped" if equipped_val else ""
            by_category.setdefault(category, []).append(
                (f"{badge(item_key)}{item_label(item_key, display_name)}", f"x{quantity}{durability}{equipped}")
            )

        ordered_categories = [c for c in category_order if c in by_category]
        ordered_categories += [c for c in by_category if c not in category_order]
        field_groups = [(category_labels.get(c, c.title()), by_category[c]) for c in ordered_categories]

        view = StaticView(
            "Inventory",
            field_groups=field_groups,
            footer_lines=[random.choice(INVENTORY_FOOTERS)],
            icon_key="gear_category",
        )
        await ctx.respond(view=view, files=view.files)

    bank = discord.SlashCommandGroup("bank", "Manage your secure bank stash.")

    @bank.command(name="deposit", description="Move cash from your wallet into your bank.")
    async def bank_deposit(
        self, ctx: discord.ApplicationContext, amount: Option(int, "Amount to deposit", min_value=1)
    ):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await deposit(session, user, amount)
        if ok:
            view = StaticView("Bank", description=message, footer_lines=[random.choice(BANK_FOOTERS)], icon_key="bank")
            await ctx.respond(view=view, files=view.files)
        else:
            await ctx.respond(embed=error_embed(message))

    @bank.command(name="withdraw", description="Move cash from your bank into your wallet.")
    async def bank_withdraw(
        self, ctx: discord.ApplicationContext, amount: Option(int, "Amount to withdraw", min_value=1)
    ):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await withdraw(session, user, amount)
        if ok:
            view = StaticView("Bank", description=message, footer_lines=[random.choice(BANK_FOOTERS)], icon_key="bank")
            await ctx.respond(view=view, files=view.files)
        else:
            await ctx.respond(embed=error_embed(message))


def setup(bot: discord.Bot):
    bot.add_cog(EconomyCog(bot))
