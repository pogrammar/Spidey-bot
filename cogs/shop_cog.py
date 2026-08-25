import random

import discord
from discord import Option
from discord.ext import commands

from db.base import async_session
from db.models import Item
from services.economy import get_or_create_user
from services.patreon_service import GATED_ITEM_MIN_RANK, tier_requirement_badges
from services.shop_service import buy_item, list_shop_items
from utils.embeds import error_embed
from utils.icons import emoji, item_label
from utils.tier_accent import current_accent
from utils.v2_embeds import PaginatedView, StaticView, add_field_groups, make_container

# Section grouping for /shop browse. Keeps the dropdown small and scannable instead
# of dumping every item — tools, gifts, and gadgets — into one long list. Name and
# icon are kept separate (not pre-joined into one decorated string) because the
# decorated form only renders correctly in message text (headers, field headings) —
# a Select's placeholder and closed-box text are plain strings that can't render
# custom Discord emoji markup, so those call sites need the bare name instead.
SHOP_SECTIONS = [
    ("Gear", "gear_category", ("tool", "component")),
    ("Gifts", "gifts_category", ("gift",)),
    ("Gadgets", "gadgets_category", ("gadget",)),
]
_SECTION_FALLBACK_EMOJI = {"gear_category": "🛠️", "gifts_category": "🎁", "gadgets_category": "🦾"}


def _section_label(name: str, icon_key: str) -> str:
    """Decorated section name — only for contexts that render real message
    content (TextDisplay headers, field-group headings), never a Select
    placeholder or OptionChoice label."""
    icon = emoji(icon_key) or _SECTION_FALLBACK_EMOJI.get(icon_key, "")
    return f"{icon} {name}".strip()

SHOP_FOOTERS = [
    "The guy behind the counter has seen weirder purchases.",
    "No receipts. No refunds. No questions about the web fluid stains.",
    "Retail therapy, hero edition.",
    "Somewhere, a shopkeeper is not asking why you need this.",
]


def _is_locked(item: Item, user_level: int) -> bool:
    return item.category == "gadget" and item.unlock_level is not None and user_level < item.unlock_level


def _patreon_branding(item: Item) -> str:
    """Patreon-gated items (see patreon_service.GATED_ITEM_MIN_RANK) are visible to
    everyone, same as a reputation-locked gadget — this just marks which ones need a
    subscription to actually buy, using tier emoji and no tier-name text, per the same
    attribution convention battle text uses.

    A shop listing is a *catalog* entry, not attribution: it's read by every tier at
    once, so it wears every badge that clears the gate rather than only the badge of the
    tier that sets it. An Arachnid-gated item showing a lone Arachnid badge (which is
    what this did until 2026-08-23) reads as "Arachnid only" to a Symbiote subscriber who
    can in fact buy it. Symbiote-only items still show one badge, because only one tier
    clears them — so the badge count itself distinguishes the two gates, and nobody reads
    an Arachnid badge and assumes an Arachnid pledge is enough for a Gold camera."""
    min_rank = GATED_ITEM_MIN_RANK.get(item.key)
    if min_rank is None:
        return ""
    badges = tier_requirement_badges(min_rank)
    return f"{badges} Patreon exclusive" if badges else "Patreon exclusive"


def _item_field(item: Item, user_level: int) -> tuple[str, str]:
    if _is_locked(item, user_level):
        lock_emoji = emoji("locked") or "🔒"
        return (
            item_label(item.key, item.name),
            f"{lock_emoji} (gadget not unlocked — needs reputation level {item.unlock_level})",
        )
    branding = _patreon_branding(item)
    description = f"{branding}\n{item.description}" if branding else item.description
    return (f"{item_label(item.key, item.name)} — ${item.price:,}", description)


def _build_shop_pages(items: list[Item], user_level: int) -> list[dict]:
    """One page per section (Gear / Gifts / Gadgets) — the catalog's grown past
    fitting comfortably in one message, and this mirrors the section split
    /shop browse already uses instead of inventing a separate fixed-item-count
    page size."""
    pages = []
    for name, icon_key, categories in SHOP_SECTIONS:
        section_items = [item for item in items if item.category in categories]
        if section_items:
            pages.append({
                "title": f"General Store — {_section_label(name, icon_key)}",
                "fields": [_item_field(item, user_level) for item in section_items],
            })
    return pages


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


def _shop_options(items: list[Item], selected_key: str | None) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label=f"{item.name} — ${item.price:,}",
            value=item.key,
            description=(item.description[:100] if item.description else None),
            emoji=emoji(item.key),
            default=(item.key == selected_key),
        )
        for item in items
    ]


class PrevSectionButton(discord.ui.Button):
    def __init__(self, panel: "ShopBrowseView", *, disabled: bool):
        super().__init__(label="Previous", emoji="◀", style=discord.ButtonStyle.secondary, disabled=disabled)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        self.panel.section_index -= 1
        self.panel.selected_key = None
        self.panel._render()
        await interaction.response.edit_message(view=self.panel)


class NextSectionButton(discord.ui.Button):
    def __init__(self, panel: "ShopBrowseView", *, disabled: bool):
        super().__init__(label="Next", emoji="▶", style=discord.ButtonStyle.secondary, disabled=disabled)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        self.panel.section_index += 1
        self.panel.selected_key = None
        self.panel._render()
        await interaction.response.edit_message(view=self.panel)


class ShopItemSelect(discord.ui.Select):
    def __init__(self, panel: "ShopBrowseView", section_name: str, options: list[discord.SelectOption]):
        super().__init__(placeholder=f"Choose an item in {section_name}...", options=options)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        self.panel.selected_key = self.values[0]
        self.panel._render()
        await interaction.response.edit_message(view=self.panel)


class BuyButton(discord.ui.Button):
    def __init__(self, panel: "ShopBrowseView", *, item: Item | None):
        label = f"Buy for ${item.price:,}" if item else "Buy"
        super().__init__(label=label, emoji="🛒", style=discord.ButtonStyle.success, disabled=item is None)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        async with async_session() as session:
            user = await get_or_create_user(session, interaction.user.id)
            ok, message = await buy_item(session, user, self.panel.selected_key)
        self.panel._render(banner=message)
        await interaction.response.edit_message(view=self.panel)


class ShopBrowseView(discord.ui.DesignerView):
    """Prev/Next buttons flip between category sections (Gear / Gifts / Gadgets), each
    with its own small dropdown + Buy button — easier to scan than one long list."""

    def __init__(self, sections: list[tuple[str, str, list[Item]]], author_id: int, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.sections = sections
        self.author_id = author_id
        self.section_index = 0
        self.selected_key: str | None = None
        self.message: discord.Message | None = None
        # Before _render below: the Buy/Prev/Next/select callbacks all re-render from
        # their own task, where the ambient per-command accent is already gone.
        self.accent = current_accent()
        self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your shop menu.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.children:
            self.children[0].disable_all_items()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def _selected_item(self, items: list[Item]) -> Item | None:
        return next((i for i in items if i.key == self.selected_key), None)

    def _render(self, banner: str | None = None) -> None:
        self.clear_items()
        name, icon_key, items = self.sections[self.section_index]
        item = self._selected_item(items)

        container = make_container(self.accent)
        if item is not None:
            title = item_label(item.key, item.name)
        else:
            title = f"General Store — {_section_label(name, icon_key)}"
        if banner:
            body = banner
        elif item is not None:
            branding = _patreon_branding(item)
            body = f"{branding}\n{item.description}" if branding else (item.description or "")
        else:
            body = "Pick something from the dropdown below."
        header_text = f"# {title}"
        if body:
            header_text += f"\n{body}"
        container.add_section(
            discord.ui.TextDisplay(header_text),
            accessory=discord.ui.Button(
                label=f"{self.section_index + 1}/{len(self.sections)}",
                style=discord.ButtonStyle.secondary,
                disabled=True,
            ),
        )

        if item is not None:
            add_field_groups(container, [(None, [("Price", f"${item.price:,}")])])
        else:
            container.add_separator()

        container.add_row(
            PrevSectionButton(self, disabled=self.section_index == 0),
            NextSectionButton(self, disabled=self.section_index == len(self.sections) - 1),
        )
        container.add_row(ShopItemSelect(self, name, _shop_options(items, self.selected_key)))
        container.add_row(BuyButton(self, item=item))

        self.add_item(container)


class ShopCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    shop = discord.SlashCommandGroup("shop", "Buy the basics — including a replacement camera.")

    @shop.command(name="list", description="See what's for sale.")
    async def list_items(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            items = await list_shop_items(session)

        pages = _build_shop_pages(items, user.reputation_level)
        footer_lines = [random.choice(SHOP_FOOTERS)]

        if not pages:
            view = StaticView("General Store", "Nothing's in stock right now.", footer_lines=footer_lines, icon_key="store")
            await ctx.respond(view=view, files=view.files)
            return

        if len(pages) == 1:
            view = StaticView(pages[0]["title"], fields=pages[0]["fields"], footer_lines=footer_lines, icon_key="store")
            await ctx.respond(view=view, files=view.files)
            return

        view = PaginatedView(pages, author_id=ctx.author.id, footer_lines=footer_lines)
        await ctx.respond(view=view)
        view.message = await ctx.interaction.original_response()

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
        for name, icon_key, categories in SHOP_SECTIONS:
            section_items = [item for item in buyable if item.category in categories]
            if section_items:
                sections.append((name, icon_key, section_items))

        view = ShopBrowseView(sections, ctx.author.id)
        await ctx.respond(view=view)
        view.message = await ctx.interaction.original_response()

    @shop.command(name="buy", description="Buy an item from the store.")
    async def buy(
        self,
        ctx: discord.ApplicationContext,
        item: Option(str, "What do you want to buy?", autocomplete=shop_item_autocomplete),
    ):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await buy_item(session, user, item)
        if ok:
            view = StaticView(
                "General Store", description=message, footer_lines=[random.choice(SHOP_FOOTERS)], icon_key="store"
            )
            await ctx.respond(view=view, files=view.files)
        else:
            await ctx.respond(embed=error_embed(message))


def setup(bot: discord.Bot):
    bot.add_cog(ShopCog(bot))
