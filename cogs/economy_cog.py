import discord
from discord import Option
from discord.ext import commands

from db.base import async_session
from services.economy import deposit, get_or_create_user, withdraw
from utils.embeds import SPIDEY_BLUE, base_embed, error_embed
from utils.icons import icon_file, set_thumbnail
from utils.item_display import badge


class EconomyCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="balance", description="Check your wallet, bank, and reputation.")
    async def balance(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)

        embed = base_embed("Your Ledger", colour=SPIDEY_BLUE)
        embed.set_footer(text="Cash on hand is stealable. Bank cash isn't — deposit what you can.")
        embed.add_field(name="Wallet", value=f"${user.wallet:,}")
        embed.add_field(name="Bank", value=f"${user.bank:,} / ${user.bank_capacity:,}")
        embed.add_field(
            name="Reputation", value=f"Level {user.reputation_level} ({user.reputation_xp} XP)"
        )
        embed.add_field(name="Suit Integrity", value=f"{user.suit_integrity}%")

        file = icon_file("wallet")
        set_thumbnail(embed, file)
        await ctx.respond(embed=embed, file=file) if file else await ctx.respond(embed=embed)

    @discord.slash_command(name="inventory", description="See what you're carrying.")
    async def inventory(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            items = await user.awaitable_attrs.inventory_items

            rows = []
            for inv_item in items:
                item_def = await inv_item.awaitable_attrs.item
                display_name = item_def.name if item_def is not None else inv_item.item_key
                rows.append((inv_item.item_key, display_name, inv_item.quantity, inv_item.durability, inv_item.equipped))

        if not rows:
            await ctx.respond(embed=error_embed("Your pockets are empty."))
            return

        embed = base_embed("Inventory", colour=SPIDEY_BLUE)
        for item_key, display_name, quantity, durability_val, equipped_val in rows:
            durability = f" ({durability_val} durability)" if durability_val is not None else ""
            equipped = " — equipped" if equipped_val else ""
            embed.add_field(
                name=f"{badge(item_key)}{display_name}",
                value=f"x{quantity}{durability}{equipped}",
                inline=False,
            )
        await ctx.respond(embed=embed)

    bank = discord.SlashCommandGroup("bank", "Manage your secure bank stash.")

    @bank.command(name="deposit", description="Move cash from your wallet into your bank.")
    async def bank_deposit(
        self, ctx: discord.ApplicationContext, amount: Option(int, "Amount to deposit", min_value=1)
    ):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await deposit(session, user, amount)
        await ctx.respond(embed=base_embed("Bank", message) if ok else error_embed(message))

    @bank.command(name="withdraw", description="Move cash from your bank into your wallet.")
    async def bank_withdraw(
        self, ctx: discord.ApplicationContext, amount: Option(int, "Amount to withdraw", min_value=1)
    ):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await withdraw(session, user, amount)
        await ctx.respond(embed=base_embed("Bank", message) if ok else error_embed(message))


def setup(bot: discord.Bot):
    bot.add_cog(EconomyCog(bot))
