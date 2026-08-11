import discord
from discord import Option
from discord.ext import commands

from db.base import async_session
from db.models import Item
from services.economy import get_or_create_user
from services.shop_service import buy_item, list_shop_items
from utils.embeds import SPIDEY_BLUE, base_embed, error_embed

# Section grouping for /shop browse. Keeps the dropdown small and scannable instead
# of dumping every item — tools, gifts, and gadgets — into one long list.
SHOP_SECTIONS = [
    ("🛠️ Gear", ("tool", "component")),
    ("🎁 Gifts", ("gift",)),
    ("🦾 Gadgets", ("gadget",)),
]


def _is_locked(item: Item, user_level: int) -> bool:
    return item.category == "gadget" and item.unlock_level is not None and user_level < item.unlock_level


async def shop_item_autocomplete(ctx: discord.AutocompleteContext) -> list[discord.OptionChoice]:
    """Live-queries the shop catalog so the picker always reflects what's actually for
    sale — no typing required, and it'll stay correct as more items/sections get added.
    Gadgets the user hasn't unlocked yet are left out entirely, not just marked."""
    async with async_session() as session:
        user = await get_or_create_user(session, ctx.interaction.user.id)
        items = await list_shop_items(session)

    typed = (ctx.value or "").lower()
    choices = [
        discord.OptionChoice(name=f"{item.name} — ${item.price:,}", value=item.key)
        for item in items
        if typed in item.name.lower() and not _is_locked(item, user.reputation_level)
    ]
    return choices[:25]


class ShopBrowseView(discord.ui.View):
    """Prev/Next buttons flip between category sections (Gear / Gifts / Gadgets), each
    with its own small dropdown + Buy button — easier to scan than one long list."""

    def __init__(self, sections: list[tuple[str, list[Item]]], author_id: int, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.sections = sections
        self.author_id = author_id
        self.section_index = 0
        self.items_by_key: dict[str, Item] = {}
        self.selected_key: str | None = None
        self._sync_section()

    def _sync_section(self) -> None:
        name, items = self.sections[self.section_index]
        self.items_by_key = {item.key: item for item in items}
        self.selected_key = None

        self.item_select.placeholder = f"Choose an item in {name}..."
        self.item_select.options = [
            discord.SelectOption(
                label=f"{item.name} — ${item.price:,}",
                value=item.key,
                description=(item.description[:100] if item.description else None),
            )
            for item in items
        ]

        self.buy_button.disabled = True
        self.buy_button.label = "Buy"
        self.previous_section.disabled = self.section_index == 0
        self.next_section.disabled = self.section_index == len(self.sections) - 1
        self.section_label.label = f"{name} ({self.section_index + 1}/{len(self.sections)})"

    def _section_embed(self, banner: str | None = None) -> discord.Embed:
        name, _ = self.sections[self.section_index]
        return base_embed(f"General Store — {name}", banner or "Pick something from the dropdown below.", colour=SPIDEY_BLUE)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your shop menu.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=0)
    async def previous_section(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.section_index -= 1
        self._sync_section()
        await interaction.response.edit_message(embed=self._section_embed(), view=self)

    @discord.ui.button(label="Section 1/1", style=discord.ButtonStyle.secondary, disabled=True, row=0)
    async def section_label(self, button: discord.ui.Button, interaction: discord.Interaction):
        pass  # display only

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_section(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.section_index += 1
        self._sync_section()
        await interaction.response.edit_message(embed=self._section_embed(), view=self)

    @discord.ui.select(placeholder="Choose an item...", row=1)
    async def item_select(self, select: discord.ui.Select, interaction: discord.Interaction):
        self.selected_key = select.values[0]
        item = self.items_by_key[self.selected_key]

        embed = self._section_embed()
        embed.add_field(name=item.name, value=item.description or "", inline=False)
        embed.add_field(name="Price", value=f"${item.price:,}")

        self.buy_button.disabled = False
        self.buy_button.label = f"Buy for ${item.price:,}"
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Buy", style=discord.ButtonStyle.success, disabled=True, row=2)
    async def buy_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if self.selected_key is None:
            return
        async with async_session() as session:
            user = await get_or_create_user(session, interaction.user.id)
            ok, message = await buy_item(session, user, self.selected_key)

        embed = self._section_embed(banner=message) if ok else error_embed(message)
        await interaction.response.edit_message(embed=embed, view=self)


class ShopCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    shop = discord.SlashCommandGroup("shop", "Buy the basics — including a replacement camera.")

    @shop.command(name="list", description="See what's for sale.")
    async def list_items(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            items = await list_shop_items(session)

        embed = base_embed("General Store", colour=SPIDEY_BLUE)
        for item in items:
            if _is_locked(item, user.reputation_level):
                embed.add_field(
                    name=item.name,
                    value=f"🔒 (gadget not unlocked — needs reputation level {item.unlock_level})",
                    inline=False,
                )
            else:
                embed.add_field(name=f"{item.name} — ${item.price:,}", value=item.description, inline=False)
        await ctx.respond(embed=embed)

    @shop.command(name="browse", description="Browse the store section by section and buy with one click.")
    async def browse(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            items = await list_shop_items(session)

        buyable = [item for item in items if not _is_locked(item, user.reputation_level)]
        if not buyable:
            await ctx.respond(embed=error_embed("Nothing available to browse right now."))
            return

        sections = []
        for label, categories in SHOP_SECTIONS:
            section_items = [item for item in buyable if item.category in categories]
            if section_items:
                sections.append((label, section_items))

        view = ShopBrowseView(sections, ctx.author.id)
        await ctx.respond(embed=view._section_embed(), view=view)

    @shop.command(name="buy", description="Buy an item from the store.")
    async def buy(
        self,
        ctx: discord.ApplicationContext,
        item: Option(str, "What do you want to buy?", autocomplete=shop_item_autocomplete),
    ):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await buy_item(session, user, item)
        await ctx.respond(embed=base_embed("General Store", message) if ok else error_embed(message))


def setup(bot: discord.Bot):
    bot.add_cog(ShopCog(bot))
