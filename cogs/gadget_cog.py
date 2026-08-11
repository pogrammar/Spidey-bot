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
from utils.embeds import SPIDEY_BLUE, base_embed, error_embed


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
        status = " — equipped" if view.equipped else f" ({view.durability}% durability)"
        choices.append(discord.OptionChoice(name=f"{view.name}{status}", value=view.item_key))
    return choices[:25]


async def equipped_gadget_autocomplete(ctx: discord.AutocompleteContext) -> list[discord.OptionChoice]:
    """For /gadget unequip and /gadget upgrade — only gadgets currently in your
    loadout, since those are the only ones either command can act on."""
    async with async_session() as session:
        views = await list_owned_gadget_views(session, ctx.interaction.user.id)

    typed = (ctx.value or "").lower()
    return [
        discord.OptionChoice(name=f"{view.name} (lvl {view.upgrade_level}/{MAX_UPGRADE_LEVEL})", value=view.item_key)
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
        status = "equipped" if view.equipped else f"{view.durability}% durability"
        options.append(
            discord.SelectOption(
                label=view.name,
                value=view.item_key,
                description=f"{status}, upgrade lvl {view.upgrade_level}/{MAX_UPGRADE_LEVEL}",
                default=(view.item_key == selected_key),
            )
        )
    return options


class GadgetPanelView(discord.ui.View):
    """Dropdown of owned gadgets + Equip/Unequip/Upgrade buttons, refreshing in place
    after each action so the panel always reflects your current loadout."""

    def __init__(self, views: list[OwnedGadgetView], author_id: int, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.selected_key: str | None = None
        self.gadget_select.options = _dedupe_options(views, None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your gadget panel.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True

    def _best_copy(self, views: list[OwnedGadgetView], key: str) -> OwnedGadgetView | None:
        for view in views:
            if view.item_key == key:
                return view
        return None

    async def _render(self, session, interaction: discord.Interaction, banner: str | None = None) -> None:
        views = await list_owned_gadget_views(session, self.author_id)
        self.gadget_select.options = _dedupe_options(views, self.selected_key)

        best = self._best_copy(views, self.selected_key) if self.selected_key else None
        if best is None:
            embed = base_embed("Your Gadgets", banner or "Pick a gadget from the dropdown.", colour=SPIDEY_BLUE)
            self.equip_button.disabled = True
            self.unequip_button.disabled = True
            self.upgrade_button.disabled = True
        else:
            embed = base_embed(best.name, banner or "", colour=SPIDEY_BLUE)
            embed.add_field(name="Durability", value=f"{best.durability}%")
            embed.add_field(name="Upgrade Level", value=f"{best.upgrade_level}/{MAX_UPGRADE_LEVEL}")
            embed.add_field(name="Equipped", value="Yes" if best.equipped else "No")
            self.equip_button.disabled = best.equipped
            self.unequip_button.disabled = not best.equipped
            self.upgrade_button.disabled = not best.equipped or best.upgrade_level >= MAX_UPGRADE_LEVEL

        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.select(placeholder="Choose a gadget you own...")
    async def gadget_select(self, select: discord.ui.Select, interaction: discord.Interaction):
        self.selected_key = select.values[0]
        async with async_session() as session:
            await self._render(session, interaction)

    @discord.ui.button(label="Equip", style=discord.ButtonStyle.primary, disabled=True)
    async def equip_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if self.selected_key is None:
            return
        async with async_session() as session:
            user = await get_or_create_user(session, interaction.user.id)
            _, message = await equip_gadget(session, user, self.selected_key)
            await self._render(session, interaction, banner=message)

    @discord.ui.button(label="Unequip", style=discord.ButtonStyle.secondary, disabled=True)
    async def unequip_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if self.selected_key is None:
            return
        async with async_session() as session:
            user = await get_or_create_user(session, interaction.user.id)
            _, message = await unequip_gadget(session, user, self.selected_key)
            await self._render(session, interaction, banner=message)

    @discord.ui.button(label="Upgrade", style=discord.ButtonStyle.success, disabled=True)
    async def upgrade_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if self.selected_key is None:
            return
        async with async_session() as session:
            user = await get_or_create_user(session, interaction.user.id)
            _, message = await upgrade_gadget(session, user, self.selected_key)
            await self._render(session, interaction, banner=message)


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

        embed = base_embed("Your Gadgets", colour=SPIDEY_BLUE)
        for view in views:
            equipped_note = " — **equipped**" if view.equipped else ""
            embed.add_field(
                name=view.name,
                value=f"{view.durability}% durability, upgrade level {view.upgrade_level}/{MAX_UPGRADE_LEVEL}{equipped_note}",
                inline=False,
            )
        embed.set_footer(text=f"You can have up to {MAX_EQUIPPED_GADGETS} equipped at once.")
        await ctx.respond(embed=embed)

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
        embed = base_embed("Your Gadgets", "Pick a gadget from the dropdown.", colour=SPIDEY_BLUE)
        await ctx.respond(embed=embed, view=view)

    @gadget.command(name="equip", description="Equip a gadget you own.")
    async def equip(
        self,
        ctx: discord.ApplicationContext,
        gadget: Option(str, "Which gadget?", autocomplete=owned_gadget_autocomplete),
    ):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await equip_gadget(session, user, gadget)
        await ctx.respond(embed=base_embed("Gadgets", message) if ok else error_embed(message))

    @gadget.command(name="unequip", description="Unequip a gadget to free up a loadout slot.")
    async def unequip(
        self,
        ctx: discord.ApplicationContext,
        gadget: Option(str, "Which equipped gadget?", autocomplete=equipped_gadget_autocomplete),
    ):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await unequip_gadget(session, user, gadget)
        await ctx.respond(embed=base_embed("Gadgets", message) if ok else error_embed(message))

    @gadget.command(name="upgrade", description="Spend cash to upgrade one of your equipped gadgets.")
    async def upgrade(
        self,
        ctx: discord.ApplicationContext,
        gadget: Option(str, "Which equipped gadget?", autocomplete=equipped_gadget_autocomplete),
    ):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await upgrade_gadget(session, user, gadget)
        await ctx.respond(embed=base_embed("Gadgets", message) if ok else error_embed(message))


def setup(bot: discord.Bot):
    bot.add_cog(GadgetCog(bot))
