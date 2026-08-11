import discord
from discord import Option
from discord.ext import commands

from db.base import async_session
from services.economy import get_or_create_user
from services.market_service import (
    ListingView,
    buy_listing,
    cancel_listing,
    create_listing,
    list_active,
    list_sellable_items,
)
from utils.embeds import SPIDEY_BLUE, base_embed, error_embed
from utils.item_display import badge
from utils.views import Paginator

LISTINGS_PER_PAGE = 5


async def sellable_autocomplete(ctx: discord.AutocompleteContext) -> list[discord.OptionChoice]:
    async with async_session() as session:
        sellable = await list_sellable_items(session, ctx.interaction.user.id)

    typed = (ctx.value or "").lower()
    return [
        discord.OptionChoice(name=f"{item.name} (have {item.quantity})", value=item.item_key)
        for item in sellable
        if typed in item.name.lower()
    ][:25]


def _build_listing_pages(listings: list[ListingView]) -> list[discord.Embed]:
    pages = []
    total_pages = max(1, (len(listings) - 1) // LISTINGS_PER_PAGE + 1)
    for page_num in range(total_pages):
        chunk = listings[page_num * LISTINGS_PER_PAGE : (page_num + 1) * LISTINGS_PER_PAGE]
        embed = base_embed("Trade Post", colour=SPIDEY_BLUE)
        for listing in chunk:
            embed.add_field(
                name=f"{badge(listing.item_key)}{listing.item_name}",
                value=f"**ID: {listing.id}**\nx{listing.quantity} @ ${listing.price_per_unit:,} each "
                f"(${listing.quantity * listing.price_per_unit:,} total)\nSeller: <@{listing.seller_id}>",
                inline=False,
            )
        embed.set_footer(text=f"Page {page_num + 1}/{total_pages} — buy with /market buy <id>")
        pages.append(embed)
    return pages


class SellModal(discord.ui.Modal):
    def __init__(self, item_key: str, item_name: str, owned_quantity: int):
        # Modals default to timeout=None (never expires). Without an explicit timeout,
        # an abandoned modal (opened, never submitted) stays registered in pycord's
        # ModalStore for the life of the process — a real leak at any real user count.
        super().__init__(title=f"Sell {item_name}"[:45], timeout=300)
        self.item_key = item_key

        self.quantity_input = discord.ui.InputText(
            label=f"Quantity (you have {owned_quantity})", placeholder="e.g. 3", required=True
        )
        self.price_input = discord.ui.InputText(
            label="Price per unit ($)", placeholder="e.g. 50", required=True
        )
        self.add_item(self.quantity_input)
        self.add_item(self.price_input)

    async def callback(self, interaction: discord.Interaction):
        try:
            quantity = int(self.quantity_input.value)
            price_per_unit = int(self.price_input.value)
        except ValueError:
            await interaction.response.send_message(
                embed=error_embed("Quantity and price both need to be whole numbers."), ephemeral=True
            )
            return

        async with async_session() as session:
            user = await get_or_create_user(session, interaction.user.id)
            ok, message = await create_listing(session, user, self.item_key, quantity, price_per_unit)

        embed = base_embed("Trade Post", message) if ok else error_embed(message)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class MarketCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    market = discord.SlashCommandGroup("market", "The Trade Post — buy and sell with other Spider-Men.")

    @market.command(name="listings", description="See what's for sale.")
    async def listings(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            listings = await list_active(session, limit=50)

        if not listings:
            await ctx.respond(embed=error_embed("Nothing's up for sale right now."))
            return

        pages = _build_listing_pages(listings)
        if len(pages) == 1:
            await ctx.respond(embed=pages[0])
            return

        view = Paginator(pages, author_id=ctx.author.id)
        await ctx.respond(embed=pages[0], view=view)

    @market.command(name="sell", description="List an item for sale.")
    async def sell(
        self,
        ctx: discord.ApplicationContext,
        item: Option(str, "Which item?", autocomplete=sellable_autocomplete),
    ):
        async with async_session() as session:
            sellable = await list_sellable_items(session, ctx.author.id)

        match = next((s for s in sellable if s.item_key == item), None)
        if match is None:
            await ctx.respond(embed=error_embed("You don't have that to sell."), ephemeral=True)
            return

        await ctx.send_modal(SellModal(match.item_key, match.name, match.quantity))

    @market.command(name="buy", description="Buy a listing by its ID.")
    async def buy(self, ctx: discord.ApplicationContext, listing_id: Option(int, "Listing ID")):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await buy_listing(session, user, listing_id)
        await ctx.respond(embed=base_embed("Trade Post", message) if ok else error_embed(message))

    @market.command(name="cancel", description="Cancel your own listing and get the items back.")
    async def cancel(self, ctx: discord.ApplicationContext, listing_id: Option(int, "Listing ID")):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await cancel_listing(session, user, listing_id)
        await ctx.respond(embed=base_embed("Trade Post", message) if ok else error_embed(message))


def setup(bot: discord.Bot):
    bot.add_cog(MarketCog(bot))
