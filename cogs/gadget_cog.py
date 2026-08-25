import random

import discord
from discord import Option
from discord.ext import commands

from db.base import async_session
from services.economy import get_or_create_user
from services.gadget_service import (
    MAX_EQUIPPED_GADGETS,
    MAX_UPGRADE_LEVEL,
    OwnedGadgetView,
    equip_gadget,
    list_owned_gadget_views,
    unequip_gadget,
    upgrade_gadget,
)
from utils.embeds import error_embed
from utils.icons import emoji, item_label
from utils.tier_accent import current_accent
from utils.v2_embeds import StaticView, add_field_groups, make_container

GADGET_FOOTERS = [
    "Every gadget here started as a Stark-wannabe napkin sketch.",
    "Held together with web fluid and blind optimism.",
    "Tony Stark would have opinions about this workmanship.",
    "Field-tested. Mostly by accident.",
]


async def owned_gadget_autocomplete(ctx: discord.AutocompleteContext) -> list[discord.OptionChoice]:
    async with async_session() as session:
        views = await list_owned_gadget_views(session, ctx.interaction.user.id)

    typed = (ctx.value or "").lower()
    seen: set[str] = set()
    choices = []
    for view in views:
        if view.item_key in seen or typed not in view.name.lower():
            continue
        seen.add(view.item_key)
        if view.tier_locked:
            status = " — inactive (Patreon tier lapsed)"
        elif view.equipped:
            status = " — equipped"
        else:
            status = f" ({view.durability}% durability)"
        choices.append(discord.OptionChoice(name=f"{view.name}{status}", value=view.item_key))
    return choices[:25]


async def equipped_gadget_autocomplete(ctx: discord.AutocompleteContext) -> list[discord.OptionChoice]:
    """For /gadget unequip and /gadget upgrade — only gadgets currently in your
    loadout, since those are the only ones either command can act on. Tier-locked ones
    are still listed: unequip is exactly what you'd want to do with an inactive gadget,
    and upgrade_gadget explains itself rather than the name just being missing."""
    async with async_session() as session:
        views = await list_owned_gadget_views(session, ctx.interaction.user.id)

    typed = (ctx.value or "").lower()
    return [
        discord.OptionChoice(
            name=(
                f"{view.name} (lvl {view.upgrade_level}/{MAX_UPGRADE_LEVEL})"
                f"{' — inactive' if view.tier_locked else ''}"
            ),
            value=view.item_key,
        )
        for view in views
        if view.equipped and typed in view.name.lower()
    ][:25]


def _dedupe_options(views: list[OwnedGadgetView], selected_key: str | None) -> list[discord.SelectOption]:
    seen: set[str] = set()
    options = []
    for view in views:
        if view.item_key in seen:
            continue
        seen.add(view.item_key)
        if view.tier_locked:
            status = "inactive (Patreon tier lapsed)"
        elif view.equipped:
            status = "equipped"
        else:
            status = f"{view.durability}% durability"
        options.append(
            discord.SelectOption(
                label=view.name,
                value=view.item_key,
                description=f"{status}, upgrade lvl {view.upgrade_level}/{MAX_UPGRADE_LEVEL}",
                emoji=emoji(view.item_key),
                default=(view.item_key == selected_key),
            )
        )
    return options


class GadgetSelect(discord.ui.Select):
    def __init__(self, panel: "GadgetPanelView", options: list[discord.SelectOption]):
        super().__init__(placeholder="Choose a gadget you own...", options=options)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        self.panel.selected_key = self.values[0]
        async with async_session() as session:
            views = await list_owned_gadget_views(session, self.panel.author_id)
        self.panel._render(views)
        await interaction.response.edit_message(view=self.panel)


class EquipButton(discord.ui.Button):
    def __init__(self, panel: "GadgetPanelView", *, disabled: bool):
        super().__init__(
            label="Equip", emoji=emoji("ready") or "✅", style=discord.ButtonStyle.primary, disabled=disabled
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        async with async_session() as session:
            user = await get_or_create_user(session, interaction.user.id)
            _, message = await equip_gadget(session, user, self.panel.selected_key)
            views = await list_owned_gadget_views(session, self.panel.author_id)
        self.panel._render(views, banner=message)
        await interaction.response.edit_message(view=self.panel)


class UnequipButton(discord.ui.Button):
    def __init__(self, panel: "GadgetPanelView", *, disabled: bool):
        super().__init__(label="Unequip", emoji="➖", style=discord.ButtonStyle.secondary, disabled=disabled)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        async with async_session() as session:
            user = await get_or_create_user(session, interaction.user.id)
            _, message = await unequip_gadget(session, user, self.panel.selected_key)
            views = await list_owned_gadget_views(session, self.panel.author_id)
        self.panel._render(views, banner=message)
        await interaction.response.edit_message(view=self.panel)


class UpgradeButton(discord.ui.Button):
    def __init__(self, panel: "GadgetPanelView", *, disabled: bool):
        super().__init__(label="Upgrade", emoji="⬆️", style=discord.ButtonStyle.success, disabled=disabled)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        async with async_session() as session:
            user = await get_or_create_user(session, interaction.user.id)
            _, message = await upgrade_gadget(session, user, self.panel.selected_key)
            views = await list_owned_gadget_views(session, self.panel.author_id)
        self.panel._render(views, banner=message)
        await interaction.response.edit_message(view=self.panel)


class GadgetPanelView(discord.ui.DesignerView):
    """Dropdown of owned gadgets + Equip/Unequip/Upgrade buttons, refreshing in place
    after each action so the panel always reflects your current loadout."""

    def __init__(self, views: list[OwnedGadgetView], author_id: int, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.selected_key: str | None = None
        self.message: discord.Message | None = None
        # Before _render below, and captured rather than read per-render: every button
        # and select callback re-renders from its own task, where the ambient
        # per-command accent no longer exists.
        self.accent = current_accent()
        self._render(views)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your gadget panel.", ephemeral=True)
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

    def _best_copy(self, views: list[OwnedGadgetView], key: str) -> OwnedGadgetView | None:
        for view in views:
            if view.item_key == key:
                return view
        return None

    def _render(self, views: list[OwnedGadgetView], banner: str | None = None) -> None:
        self.clear_items()
        best = self._best_copy(views, self.selected_key) if self.selected_key else None
        equipped_count = sum(1 for v in views if v.equipped)

        container = make_container(self.accent)
        if best is not None:
            title = item_label(best.item_key, best.name)
        else:
            category_emoji = emoji("gadgets_category")
            title = f"{category_emoji} Your Gadgets" if category_emoji else "Your Gadgets"
        body = banner or ("Pick a gadget from the dropdown." if best is None else "")
        header_text = f"# {title}"
        if body:
            header_text += f"\n{body}"
        container.add_section(
            discord.ui.TextDisplay(header_text),
            accessory=discord.ui.Button(
                label=f"{equipped_count}/{MAX_EQUIPPED_GADGETS} Equipped",
                style=discord.ButtonStyle.success if equipped_count == MAX_EQUIPPED_GADGETS else discord.ButtonStyle.secondary,
                disabled=True,
            ),
        )

        if best is not None:
            stat_fields = [
                ("Durability", f"{best.durability}%" if best.durability is not None else "—"),
                ("Upgrade Level", f"{best.upgrade_level}/{MAX_UPGRADE_LEVEL}"),
                ("Equipped", "Yes" if best.equipped else "No"),
            ]
            if best.tier_locked:
                stat_fields.append((
                    "Status",
                    "Inactive — the Patreon tier that unlocked it isn't active on your "
                    "account. Still yours, upgrades intact; it works again when the pledge "
                    "is back. See /patreon status.",
                ))
            add_field_groups(container, [(None, stat_fields)])
        else:
            container.add_separator()

        container.add_row(GadgetSelect(self, _dedupe_options(views, self.selected_key)))
        container.add_row(
            # Equip/Unequip stay live for a tier-locked gadget — freeing the slot is the
            # one useful thing to do with an inactive one. Upgrade doesn't: it's a cash
            # spend on something that can't fire, and upgrade_gadget rejects it anyway.
            EquipButton(self, disabled=best is None or best.equipped),
            UnequipButton(self, disabled=best is None or not best.equipped),
            UpgradeButton(
                self,
                disabled=(
                    best is None
                    or not best.equipped
                    or best.tier_locked
                    or best.upgrade_level >= MAX_UPGRADE_LEVEL
                ),
            ),
        )

        self.add_item(container)


class GadgetCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    gadget = discord.SlashCommandGroup("gadget", "Your equipped gear for patrol.")

    @gadget.command(name="status", description="See the gadgets you own and what's equipped.")
    async def status(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            views = await list_owned_gadget_views(session, ctx.author.id)

        if not views:
            await ctx.respond(
                embed=error_embed("You don't own any gadgets yet. Check /shop list once you're leveled up.")
            )
            return

        equipped_fields = []
        stored_fields = []
        any_locked = False
        for view in views:
            detail = f"{view.durability}% durability, upgrade level {view.upgrade_level}/{MAX_UPGRADE_LEVEL}"
            if view.tier_locked:
                # Says so on the line itself rather than only in the footer — a gadget
                # that never fires and looks completely normal in this list is the one
                # thing guaranteed to be read as broken instead of revoked.
                any_locked = True
                detail += "\n-# Inactive — the Patreon tier that unlocked it isn't active on your account."
            (equipped_fields if view.equipped else stored_fields).append(
                (item_label(view.item_key, view.name), detail)
            )

        field_groups = []
        if equipped_fields:
            field_groups.append(("Equipped", equipped_fields))
        if stored_fields:
            field_groups.append(("In Storage", stored_fields))

        footer_lines = [f"You can have up to {MAX_EQUIPPED_GADGETS} equipped at once."]
        if any_locked:
            footer_lines.append(
                "Inactive gadgets stay yours and keep their upgrades — they switch straight "
                "back on when the pledge is. See /patreon status."
            )
        footer_lines.append(random.choice(GADGET_FOOTERS))

        v2_view = StaticView(
            "Your Gadgets",
            field_groups=field_groups,
            footer_lines=footer_lines,
            icon_key="gadgets_category",
        )
        await ctx.respond(view=v2_view, files=v2_view.files)

    @gadget.command(name="panel", description="Interactive gadget menu — pick, equip, upgrade.")
    async def panel(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            views = await list_owned_gadget_views(session, ctx.author.id)

        if not views:
            await ctx.respond(
                embed=error_embed("You don't own any gadgets yet. Check /shop list once you're leveled up.")
            )
            return

        view = GadgetPanelView(views, ctx.author.id)
        await ctx.respond(view=view)
        view.message = await ctx.interaction.original_response()

    @gadget.command(name="equip", description="Equip a gadget you own.")
    async def equip(
        self,
        ctx: discord.ApplicationContext,
        gadget: Option(str, "Which gadget?", autocomplete=owned_gadget_autocomplete),
    ):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await equip_gadget(session, user, gadget)
        if ok:
            view = StaticView(
                "Gadgets", description=message, footer_lines=[random.choice(GADGET_FOOTERS)], icon_key="gadgets_category"
            )
            await ctx.respond(view=view, files=view.files)
        else:
            await ctx.respond(embed=error_embed(message))

    @gadget.command(name="unequip", description="Unequip a gadget to free up a loadout slot.")
    async def unequip(
        self,
        ctx: discord.ApplicationContext,
        gadget: Option(str, "Which equipped gadget?", autocomplete=equipped_gadget_autocomplete),
    ):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await unequip_gadget(session, user, gadget)
        if ok:
            view = StaticView(
                "Gadgets", description=message, footer_lines=[random.choice(GADGET_FOOTERS)], icon_key="gadgets_category"
            )
            await ctx.respond(view=view, files=view.files)
        else:
            await ctx.respond(embed=error_embed(message))

    @gadget.command(name="upgrade", description="Spend cash to upgrade one of your equipped gadgets.")
    async def upgrade(
        self,
        ctx: discord.ApplicationContext,
        gadget: Option(str, "Which equipped gadget?", autocomplete=equipped_gadget_autocomplete),
    ):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await upgrade_gadget(session, user, gadget)
        if ok:
            view = StaticView(
                "Gadgets", description=message, footer_lines=[random.choice(GADGET_FOOTERS)], icon_key="gadgets_category"
            )
            await ctx.respond(view=view, files=view.files)
        else:
            await ctx.respond(embed=error_embed(message))


def setup(bot: discord.Bot):
    bot.add_cog(GadgetCog(bot))
