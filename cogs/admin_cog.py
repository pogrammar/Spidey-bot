import datetime

import discord
from discord import Option
from discord.ext import commands
from sqlalchemy import select

import config
from db.base import async_session
from db.models import Cooldown, Item, User
from services.admin_service import grant_admin, is_granted_admin, list_granted_admins, revoke_admin
from services.ally_service import ALLY_NAMES, reset_ally, set_happiness
from services.brewing_service import clear_brew, force_ready
from services.cooldowns import (
    disable_bypass,
    enable_bypass,
    format_remaining,
    is_bypass_enabled,
    set_cooldown,
)
from services.economy import add_bank, add_wallet, get_or_create_user, wipe_user
from services.gadget_service import list_all_gadgets
from services.inventory_service import add_item, get_editable_row, remove_item
from services.market_service import admin_cancel_listing
from utils.embeds import base_embed, error_embed
from utils.icons import item_label, thumbnail
from utils.v2_embeds import StaticView, add_field_groups

ALLY_CHOICES = [discord.OptionChoice(name=name, value=key) for key, name in ALLY_NAMES.items()]

COOLDOWN_CHOICES = [
    discord.OptionChoice(name="Patrol", value="patrol"),
    discord.OptionChoice(name="Bugle Submit", value="bugle_submit"),
    discord.OptionChoice(name="Tutoring", value="tutoring"),
    discord.OptionChoice(name="Shakedown (attacker)", value="shakedown"),
    discord.OptionChoice(name="Shakedown (target protection)", value="shakedown_target"),
    discord.OptionChoice(name="Daily Claim", value="daily"),
]

# Categories for /admin help — kept in sync by hand since this is a small, fixed
# command set; not worth generating dynamically from the command tree for 24 commands.
HELP_SECTIONS = [
    ("General", [
        ("/admin bypass", "Toggle cooldown + brew-time bypass for yourself."),
        ("/admin bypass-status", "Check whether your bypass is on."),
        ("/admin user-info", "Full profile dump for a user."),
        ("/admin wipe", "Permanently reset a user's entire profile. Destructive."),
    ]),
    ("Economy", [
        ("/admin economy give-cash", "Add cash to a user's wallet or bank."),
        ("/admin economy set-bank-capacity", "Force a user's bank capacity."),
    ]),
    ("Profile", [
        ("/admin profile set-reputation", "Set reputation XP directly."),
        ("/admin profile set-suit", "Set suit integrity."),
        ("/admin profile set-crime-level", "Set city crime level."),
        ("/admin profile set-eviction", "Set the eviction meter."),
        ("/admin profile set-rent-due", "Push the next rent due date."),
        ("/admin profile set-streak", "Set daily streak (current + longest)."),
    ]),
    ("Inventory", [
        ("/admin inventory give-item", "Give any item by key + quantity."),
        ("/admin inventory remove-item", "Take an item away."),
        ("/admin inventory set-durability", "Repair or break a specific owned item."),
        ("/admin inventory set-upgrade", "Force a gadget's upgrade level."),
    ]),
    ("Cooldown", [
        ("/admin cooldown reset", "Clear one of a user's active cooldowns."),
        ("/admin cooldown set", "Force a cooldown on, for testing."),
    ]),
    ("Ally", [
        ("/admin ally set-happiness", "Set Aunt May/MJ happiness directly."),
        ("/admin ally reset", "Clear gift-streak/usage history for one ally."),
    ]),
    ("Brew", [
        ("/admin brew force-ready", "Instantly finish a user's brew."),
        ("/admin brew clear", "Cancel a stuck brew (no refund)."),
    ]),
    ("Market", [
        ("/admin market delete-listing", "Remove a listing, refund the seller."),
    ]),
    ("Admins", [
        ("/admin admins add", "Grant a user /admin access (persists across restarts)."),
        ("/admin admins remove", "Revoke a user's runtime-granted admin access."),
        ("/admin admins list", "List everyone with /admin access."),
    ]),
]


async def _is_admin(ctx: discord.ApplicationContext) -> bool:
    """Fails closed: an empty ADMIN_DISCORD_IDS (and no runtime grants) denies
    everyone, not everyone-but-checked. ADMIN_DISCORD_IDS (from .env) are root —
    always trusted, checked first so they never need a DB round-trip. Everyone else
    with access is in the admin_users table via /admin admins add."""
    if ctx.author.id in config.ADMIN_DISCORD_IDS:
        return True
    async with async_session() as session:
        return await is_granted_admin(session, ctx.author.id)


async def _check_owner(ctx: discord.ApplicationContext) -> bool:
    if not await _is_admin(ctx):
        await ctx.respond(embed=error_embed("This isn't for you."), ephemeral=True)
        return False
    return True


async def _reply(ctx: discord.ApplicationContext, message: str) -> None:
    view = StaticView("Admin", message, icon_key="admin_badge")
    await ctx.respond(view=view, files=view.files, ephemeral=True)


def _add_admin_header(container: discord.ui.Container, title: str) -> discord.File | None:
    """Section+badge header — same pattern as patrol/gadget-panel/shop-browse. Real
    admin_badge art if it exists, else the same disabled-text-badge fallback this
    project used before any icons existed — never an error either way."""
    result = thumbnail("admin_badge")
    if result is not None:
        thumb, file = result
        container.add_section(discord.ui.TextDisplay(f"# {title}"), accessory=thumb)
        return file
    container.add_section(
        discord.ui.TextDisplay(f"# {title}"),
        accessory=discord.ui.Button(label="🛠️ Admin", style=discord.ButtonStyle.secondary, disabled=True),
    )
    return None


class AdminHelpSelect(discord.ui.Select):
    def __init__(self, panel: "AdminHelpView"):
        options = [
            discord.SelectOption(label=section, value=section, default=(section == panel.section))
            for section, _ in HELP_SECTIONS
        ]
        super().__init__(placeholder="Choose a category...", options=options)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        self.panel.section = self.values[0]
        self.panel._render()
        await interaction.response.edit_message(view=self.panel)


class AdminHelpView(discord.ui.DesignerView):
    """Dropdown jumps straight to a category instead of dumping all 27 commands into
    one wall of text — same reasoning as the player-facing /help browser."""

    def __init__(self, author_id: int, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.section = HELP_SECTIONS[0][0]
        self.message: discord.Message | None = None
        self.files: list[discord.File] = []
        self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your menu.", ephemeral=True)
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

    def _render(self) -> None:
        self.clear_items()
        commands_ = next(cmds for section, cmds in HELP_SECTIONS if section == self.section)

        container = discord.ui.Container()
        file = _add_admin_header(container, f"Admin — {self.section}")
        self.files = [file] if file else []
        container.add_separator()
        container.add_text("\n".join(f"• `{name}` — {desc}" for name, desc in commands_))
        container.add_separator()
        container.add_row(AdminHelpSelect(self))

        self.add_item(container)


class RefreshUserInfoButton(discord.ui.Button):
    def __init__(self, panel: "UserInfoView"):
        super().__init__(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        async with async_session() as session:
            found = await self.panel._render(session)
        if not found:
            await interaction.response.edit_message(
                embed=error_embed(f"{self.panel.target_mention} no longer has a profile."), view=None
            )
            return
        await interaction.response.edit_message(view=self.panel)


class UserInfoView(discord.ui.DesignerView):
    def __init__(self, target_id: int, target_display: str, target_mention: str, author_id: int, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.target_id = target_id
        self.target_display = target_display
        self.target_mention = target_mention
        self.author_id = author_id
        self.message: discord.Message | None = None
        self.files: list[discord.File] = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your menu.", ephemeral=True)
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

    async def _render(self, session) -> bool:
        """Returns False (view left empty) if the profile's gone — e.g. wiped between
        loading this panel and hitting Refresh."""
        profile = await session.get(User, self.target_id)
        if profile is None:
            self.clear_items()
            return False

        stmt = select(Cooldown).where(Cooldown.user_id == self.target_id)
        cooldowns = list((await session.execute(stmt)).scalars())

        now = datetime.datetime.utcnow()
        rent_seconds = (profile.next_rent_due - now).total_seconds()
        rent_display = f"due in {format_remaining(rent_seconds)}" if rent_seconds > 0 else "OVERDUE"

        active = [c for c in cooldowns if c.expires_at > now]
        cooldown_display = (
            "\n".join(f"`{c.command_key}`: {format_remaining((c.expires_at - now).total_seconds())}" for c in active)
            if active
            else "None"
        )

        field_groups = [
            (
                "Economy",
                [
                    ("Wallet", f"${profile.wallet:,}"),
                    ("Bank", f"${profile.bank:,} / ${profile.bank_capacity:,}"),
                ],
            ),
            (
                "Profile",
                [
                    ("Reputation", f"Level {profile.reputation_level} ({profile.reputation_xp:,} XP)"),
                    ("Suit Integrity", f"{profile.suit_integrity}%"),
                    ("Crime Level", f"{profile.crime_level}/100"),
                    ("Eviction Meter", f"{profile.eviction_meter}/100"),
                    ("Rent", rent_display),
                    ("Daily Streak", f"{profile.daily_streak} (longest {profile.daily_longest_streak})"),
                ],
            ),
            ("Cooldowns", [("", cooldown_display)]),
        ]

        self.clear_items()
        container = discord.ui.Container()
        file = _add_admin_header(container, f"Admin — {self.target_display}")
        self.files = [file] if file else []
        add_field_groups(container, field_groups)
        container.add_separator()
        container.add_text(f"-# discord_id: {self.target_id}")
        container.add_separator()
        container.add_row(RefreshUserInfoButton(self))
        self.add_item(container)
        return True


class RevokeAdminSelect(discord.ui.Select):
    def __init__(self, panel: "AdminsListView", granted: list):
        if granted:
            options = [
                discord.SelectOption(
                    label=str(row.discord_id), value=str(row.discord_id), description=f"granted by {row.granted_by}"
                )
                for row in granted
            ]
        else:
            options = [discord.SelectOption(label="No runtime-granted admins", value="none")]
        super().__init__(placeholder="Select an admin to revoke...", options=options, disabled=not granted)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        self.panel.selected_id = int(self.values[0])
        async with async_session() as session:
            await self.panel._render(session)
        await interaction.response.edit_message(view=self.panel)


class RevokeSelectedButton(discord.ui.Button):
    def __init__(self, panel: "AdminsListView", *, disabled: bool):
        super().__init__(label="Revoke Selected", emoji="🗑️", style=discord.ButtonStyle.danger, disabled=disabled)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        async with async_session() as session:
            await revoke_admin(session, self.panel.selected_id)
            self.panel.selected_id = None
            await self.panel._render(session)
        await interaction.response.edit_message(view=self.panel)


class AdminsListView(discord.ui.DesignerView):
    """Live add/revoke panel — /admin admins add still exists for granting (needs a
    real user picker, which a Select can't autocomplete from arbitrary server
    members), but revoking is fully click-driven from here."""

    def __init__(self, author_id: int, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.selected_id: int | None = None
        self.message: discord.Message | None = None
        self.files: list[discord.File] = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your menu.", ephemeral=True)
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

    async def _render(self, session) -> None:
        self.clear_items()
        granted = await list_granted_admins(session)

        container = discord.ui.Container()
        file = _add_admin_header(container, "Admin — Access List")
        self.files = [file] if file else []
        container.add_separator()

        root_lines = "\n".join(f"• <@{uid}>" for uid in sorted(config.ADMIN_DISCORD_IDS)) or "None configured."
        container.add_text(f"-# ROOT (.env)\n{root_lines}")
        container.add_separator(divider=False)
        granted_lines = (
            "\n".join(f"• <@{row.discord_id}> — granted by <@{row.granted_by}>" for row in granted)
            if granted
            else "None."
        )
        container.add_text(f"-# RUNTIME-GRANTED\n{granted_lines}")

        container.add_separator()
        container.add_row(RevokeAdminSelect(self, granted))
        container.add_row(RevokeSelectedButton(self, disabled=self.selected_id is None))

        self.add_item(container)


async def all_item_autocomplete(ctx: discord.AutocompleteContext) -> list[discord.OptionChoice]:
    async with async_session() as session:
        items = list((await session.execute(select(Item).order_by(Item.category, Item.name))).scalars())
    typed = (ctx.value or "").lower()
    return [
        discord.OptionChoice(name=f"{item.name} ({item.category})", value=item.key)
        for item in items
        if typed in item.name.lower()
    ][:25]


async def gadget_autocomplete(ctx: discord.AutocompleteContext) -> list[discord.OptionChoice]:
    async with async_session() as session:
        gadgets = await list_all_gadgets(session)
    typed = (ctx.value or "").lower()
    return [discord.OptionChoice(name=g.name, value=g.key) for g in gadgets if typed in g.name.lower()][:25]


class WipeConfirmView(discord.ui.View):
    """A plain V1 confirm/cancel — /admin stays classic-embed styled on purpose, and
    this is destructive enough to need a real second click, not just a slash option."""

    def __init__(self, target_id: int, target_mention: str, author_id: int, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.target_id = target_id
        self.target_mention = target_mention
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Confirm Wipe", style=discord.ButtonStyle.danger)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        async with async_session() as session:
            wiped = await wipe_user(session, self.target_id)
        message = (
            f"Wiped {self.target_mention}'s entire profile."
            if wiped
            else f"{self.target_mention} had no profile to wipe."
        )
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=base_embed("Admin", message), view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=base_embed("Admin", "Wipe cancelled."), view=self)


class AdminCog(commands.Cog):
    """Owner-only dev tools. Deliberately left out of /help — not for players."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    admin = discord.SlashCommandGroup("admin", "Owner-only dev tools.")
    economy = admin.create_subgroup("economy", "Wallet, bank, and cash admin tools.")
    profile = admin.create_subgroup("profile", "Reputation, suit, crime, eviction, streak admin tools.")
    inventory = admin.create_subgroup("inventory", "Give, remove, and edit items.")
    cooldown = admin.create_subgroup("cooldown", "Reset or force cooldowns.")
    ally = admin.create_subgroup("ally", "Aunt May / MJ happiness admin tools.")
    brew = admin.create_subgroup("brew", "Chem Lab brew admin tools.")
    market = admin.create_subgroup("market", "Trade Post moderation.")
    admins = admin.create_subgroup("admins", "Grant or revoke runtime admin access.")

    # ---- general ----------------------------------------------------------

    @admin.command(name="help", description="List every admin command.")
    async def admin_help(self, ctx: discord.ApplicationContext):
        if not await _check_owner(ctx):
            return
        view = AdminHelpView(author_id=ctx.author.id)
        await ctx.respond(view=view, files=view.files, ephemeral=True)
        view.message = await ctx.interaction.original_response()

    @admin.command(name="bypass", description="Toggle cooldown + brew-time bypass for yourself.")
    async def bypass(self, ctx: discord.ApplicationContext, enabled: Option(bool, "Turn bypass on or off")):
        if not await _check_owner(ctx):
            return

        if enabled:
            enable_bypass(ctx.author.id)
            message = (
                "Cooldown bypass is **ON** — every command's cooldown is skipped, and "
                "/lab collect ignores brew time, until you turn it off."
            )
        else:
            disable_bypass(ctx.author.id)
            message = "Cooldown bypass is **OFF**."

        await _reply(ctx, message)

    @admin.command(name="bypass-status", description="Check whether your cooldown bypass is on.")
    async def bypass_status(self, ctx: discord.ApplicationContext):
        if not await _check_owner(ctx):
            return
        state = "ON" if is_bypass_enabled(ctx.author.id) else "OFF"
        await _reply(ctx, f"Cooldown bypass is **{state}**.")

    @admin.command(name="user-info", description="Full profile dump for a user.")
    async def user_info(
        self,
        ctx: discord.ApplicationContext,
        user: Option(discord.Member, "Whose profile (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author

        view = UserInfoView(target.id, target.display_name, target.mention, ctx.author.id)
        async with async_session() as session:
            found = await view._render(session)
        if not found:
            await ctx.respond(embed=error_embed(f"{target.mention} has no profile yet."), ephemeral=True)
            return

        await ctx.respond(view=view, files=view.files, ephemeral=True)
        view.message = await ctx.interaction.original_response()

    @admin.command(name="wipe", description="Permanently reset a user's ENTIRE profile. Destructive.")
    async def wipe(
        self,
        ctx: discord.ApplicationContext,
        user: Option(discord.Member, "Whose profile to wipe (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author
        view = WipeConfirmView(target.id, target.mention, ctx.author.id)
        await ctx.respond(
            embed=error_embed(
                f"This will **permanently delete** {target.mention}'s entire profile — wallet, bank, "
                "inventory, reputation, streak, allies, brew, listings, everything. This cannot be undone."
            ),
            view=view,
            ephemeral=True,
        )

    # ---- economy ------------------------------------------------------------

    @economy.command(name="give-cash", description="Add cash to a user's wallet or bank.")
    async def give_cash(
        self,
        ctx: discord.ApplicationContext,
        amount: Option(int, "Amount (negative to remove)"),
        user: Option(discord.Member, "Who gets it (defaults to you)", required=False),
        account: Option(str, "Wallet or bank", choices=["wallet", "bank"], default="wallet"),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author
        async with async_session() as session:
            profile = await get_or_create_user(session, target.id)
            if account == "wallet":
                delta = await add_wallet(session, profile, amount, reason=f"admin:{ctx.author.id}")
                new_balance = profile.wallet
            else:
                delta = await add_bank(session, profile, amount, reason=f"admin:{ctx.author.id}")
                new_balance = profile.bank

        await _reply(ctx, f"{target.mention}'s {account} changed by ${delta:,}. New {account}: ${new_balance:,}.")

    @economy.command(name="set-bank-capacity", description="Force a user's bank capacity.")
    async def set_bank_capacity(
        self,
        ctx: discord.ApplicationContext,
        capacity: Option(int, "New bank capacity", min_value=0),
        user: Option(discord.Member, "Whose bank (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author
        async with async_session() as session:
            profile = await get_or_create_user(session, target.id)
            profile.bank_capacity = capacity
            await session.commit()
        await _reply(ctx, f"{target.mention}'s bank capacity set to ${capacity:,}.")

    # ---- profile --------------------------------------------------------------

    @profile.command(name="set-reputation", description="Set reputation XP directly.")
    async def set_reputation(
        self,
        ctx: discord.ApplicationContext,
        xp: Option(int, "New reputation XP", min_value=0),
        user: Option(discord.Member, "Whose reputation (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author
        async with async_session() as session:
            profile = await get_or_create_user(session, target.id)
            profile.reputation_xp = xp
            await session.commit()
            level = profile.reputation_level
        await _reply(ctx, f"{target.mention}'s reputation set to {xp:,} XP (Level {level}).")

    @profile.command(name="set-suit", description="Set suit integrity.")
    async def set_suit(
        self,
        ctx: discord.ApplicationContext,
        value: Option(int, "0-100", min_value=0, max_value=100),
        user: Option(discord.Member, "Whose suit (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author
        async with async_session() as session:
            profile = await get_or_create_user(session, target.id)
            profile.suit_integrity = value
            await session.commit()
        await _reply(ctx, f"{target.mention}'s suit integrity set to {value}%.")

    @profile.command(name="set-crime-level", description="Set city crime level.")
    async def set_crime_level(
        self,
        ctx: discord.ApplicationContext,
        value: Option(int, "0-100", min_value=0, max_value=100),
        user: Option(discord.Member, "Whose crime level (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author
        async with async_session() as session:
            profile = await get_or_create_user(session, target.id)
            profile.crime_level = value
            await session.commit()
        await _reply(ctx, f"{target.mention}'s crime level set to {value}/100.")

    @profile.command(name="set-eviction", description="Set the eviction meter.")
    async def set_eviction(
        self,
        ctx: discord.ApplicationContext,
        value: Option(int, "0-100", min_value=0, max_value=100),
        user: Option(discord.Member, "Whose eviction meter (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author
        async with async_session() as session:
            profile = await get_or_create_user(session, target.id)
            profile.eviction_meter = value
            await session.commit()
        await _reply(ctx, f"{target.mention}'s eviction meter set to {value}/100.")

    @profile.command(name="set-rent-due", description="Push the next rent due date.")
    async def set_rent_due(
        self,
        ctx: discord.ApplicationContext,
        days_from_now: Option(int, "Days from now (negative = overdue)"),
        user: Option(discord.Member, "Whose rent (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author
        async with async_session() as session:
            profile = await get_or_create_user(session, target.id)
            profile.next_rent_due = datetime.datetime.utcnow() + datetime.timedelta(days=days_from_now)
            await session.commit()
        await _reply(ctx, f"{target.mention}'s rent due date set to {days_from_now} day(s) from now.")

    @profile.command(name="set-streak", description="Set daily streak (current + longest).")
    async def set_streak(
        self,
        ctx: discord.ApplicationContext,
        streak: Option(int, "New current streak", min_value=0),
        user: Option(discord.Member, "Whose streak (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author
        async with async_session() as session:
            profile = await get_or_create_user(session, target.id)
            profile.daily_streak = streak
            profile.daily_longest_streak = max(profile.daily_longest_streak, streak)
            await session.commit()
        await _reply(ctx, f"{target.mention}'s daily streak set to {streak}.")

    # ---- inventory --------------------------------------------------------------

    @inventory.command(name="give-item", description="Give any item by key + quantity.")
    async def give_item(
        self,
        ctx: discord.ApplicationContext,
        item: Option(str, "Which item?", autocomplete=all_item_autocomplete),
        quantity: Option(int, "How many", min_value=1, default=1),
        user: Option(discord.Member, "Who gets it (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author

        async with async_session() as session:
            item_def = await session.get(Item, item)
            if item_def is None:
                await ctx.respond(embed=error_embed(f"No such item: `{item}`."), ephemeral=True)
                return
            await get_or_create_user(session, target.id)
            await add_item(session, target.id, item, quantity)

        await _reply(ctx, f"Gave {target.mention} {quantity}x {item_label(item, item_def.name)}.")

    @inventory.command(name="remove-item", description="Take an item away.")
    async def remove_item_cmd(
        self,
        ctx: discord.ApplicationContext,
        item: Option(str, "Which item?", autocomplete=all_item_autocomplete),
        quantity: Option(int, "How many", min_value=1, default=1),
        user: Option(discord.Member, "From whom (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author

        async with async_session() as session:
            item_def = await session.get(Item, item)
            if item_def is None:
                await ctx.respond(embed=error_embed(f"No such item: `{item}`."), ephemeral=True)
                return
            await get_or_create_user(session, target.id)
            removed = await remove_item(session, target.id, item, quantity)

        if removed:
            await _reply(ctx, f"Removed {quantity}x {item_label(item, item_def.name)} from {target.mention}.")
        else:
            await ctx.respond(
                embed=error_embed(
                    f"{target.mention} doesn't have {quantity}x {item_label(item, item_def.name)} unequipped."
                ),
                ephemeral=True,
            )

    @inventory.command(name="set-durability", description="Repair or break a specific owned item.")
    async def set_durability(
        self,
        ctx: discord.ApplicationContext,
        item: Option(str, "Which item?", autocomplete=all_item_autocomplete),
        value: Option(int, "New durability", min_value=0),
        user: Option(discord.Member, "Whose item (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author

        async with async_session() as session:
            item_def = await session.get(Item, item)
            if item_def is None:
                await ctx.respond(embed=error_embed(f"No such item: `{item}`."), ephemeral=True)
                return
            row = await get_editable_row(session, target.id, item)
            if row is None:
                await ctx.respond(
                    embed=error_embed(f"{target.mention} doesn't own a {item_label(item, item_def.name)}."),
                    ephemeral=True,
                )
                return

            capped = min(value, item_def.max_durability) if item_def.max_durability is not None else value
            row.durability = capped
            await session.commit()

        note = " (capped to max)" if item_def.max_durability is not None and capped != value else ""
        await _reply(ctx, f"{target.mention}'s {item_label(item, item_def.name)} durability set to {capped}%{note}.")

    @inventory.command(name="set-upgrade", description="Force a gadget's upgrade level.")
    async def set_upgrade(
        self,
        ctx: discord.ApplicationContext,
        item: Option(str, "Which gadget?", autocomplete=gadget_autocomplete),
        level: Option(int, "New upgrade level", min_value=0, max_value=3),
        user: Option(discord.Member, "Whose gadget (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author

        async with async_session() as session:
            item_def = await session.get(Item, item)
            if item_def is None or item_def.category != "gadget":
                await ctx.respond(embed=error_embed(f"`{item}` isn't a gadget."), ephemeral=True)
                return
            row = await get_editable_row(session, target.id, item)
            if row is None:
                await ctx.respond(
                    embed=error_embed(f"{target.mention} doesn't own a {item_label(item, item_def.name)}."),
                    ephemeral=True,
                )
                return
            row.upgrade_level = level
            await session.commit()

        await _reply(ctx, f"{target.mention}'s {item_label(item, item_def.name)} upgrade level set to {level}.")

    # ---- cooldown --------------------------------------------------------------

    @cooldown.command(name="reset", description="Clear one of a user's active cooldowns.")
    async def reset_cooldown(
        self,
        ctx: discord.ApplicationContext,
        key: Option(str, "Which cooldown?", choices=COOLDOWN_CHOICES),
        user: Option(discord.Member, "Whose cooldown (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author
        async with async_session() as session:
            await set_cooldown(session, target.id, key, 0)
        await _reply(ctx, f"Cleared {target.mention}'s `{key}` cooldown.")

    @cooldown.command(name="set", description="Force a cooldown on, for testing lockout states.")
    async def set_cooldown_cmd(
        self,
        ctx: discord.ApplicationContext,
        key: Option(str, "Which cooldown?", choices=COOLDOWN_CHOICES),
        seconds: Option(int, "Seconds from now", min_value=1),
        user: Option(discord.Member, "Whose cooldown (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author
        async with async_session() as session:
            await set_cooldown(session, target.id, key, seconds)
        await _reply(ctx, f"Set {target.mention}'s `{key}` cooldown to {format_remaining(seconds)}.")

    # ---- ally --------------------------------------------------------------

    @ally.command(name="set-happiness", description="Set Aunt May/MJ happiness directly.")
    async def set_ally_happiness(
        self,
        ctx: discord.ApplicationContext,
        ally_key: Option(str, "Which ally?", choices=ALLY_CHOICES),
        value: Option(int, "0-100", min_value=0, max_value=100),
        user: Option(discord.Member, "Whose ally (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author
        async with async_session() as session:
            await get_or_create_user(session, target.id)
            new_value = await set_happiness(session, target.id, ally_key, value)
        await _reply(ctx, f"{target.mention}'s {ALLY_NAMES[ally_key]} happiness set to {new_value}/100.")

    @ally.command(name="reset", description="Clear gift-streak/usage history for one ally.")
    async def reset_ally_cmd(
        self,
        ctx: discord.ApplicationContext,
        ally_key: Option(str, "Which ally?", choices=ALLY_CHOICES),
        user: Option(discord.Member, "Whose ally (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author
        async with async_session() as session:
            await get_or_create_user(session, target.id)
            await reset_ally(session, target.id, ally_key)
        await _reply(ctx, f"Cleared {target.mention}'s gift history with {ALLY_NAMES[ally_key]}.")

    # ---- brew --------------------------------------------------------------

    @brew.command(name="force-ready", description="Instantly finish a user's brew.")
    async def brew_force_ready(
        self,
        ctx: discord.ApplicationContext,
        user: Option(discord.Member, "Whose brew (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author
        async with async_session() as session:
            ok = await force_ready(session, target.id)
        if ok:
            await _reply(ctx, f"{target.mention}'s brew is now ready to collect.")
        else:
            await ctx.respond(embed=error_embed(f"{target.mention} has nothing brewing."), ephemeral=True)

    @brew.command(name="clear", description="Cancel a stuck brew (no refund).")
    async def brew_clear(
        self,
        ctx: discord.ApplicationContext,
        user: Option(discord.Member, "Whose brew (defaults to you)", required=False),
    ):
        if not await _check_owner(ctx):
            return
        target = user or ctx.author
        async with async_session() as session:
            ok = await clear_brew(session, target.id)
        if ok:
            await _reply(ctx, f"Cleared {target.mention}'s brew.")
        else:
            await ctx.respond(embed=error_embed(f"{target.mention} has nothing brewing."), ephemeral=True)

    # ---- market --------------------------------------------------------------

    @market.command(name="delete-listing", description="Remove a listing, refund the seller.")
    async def market_delete_listing(
        self, ctx: discord.ApplicationContext, listing_id: Option(int, "Listing ID")
    ):
        if not await _check_owner(ctx):
            return
        async with async_session() as session:
            ok, message = await admin_cancel_listing(session, listing_id)
        if ok:
            await _reply(ctx, message)
        else:
            await ctx.respond(embed=error_embed(message), ephemeral=True)

    # ---- admins --------------------------------------------------------------

    @admins.command(name="add", description="Grant a user /admin access (persists across restarts).")
    async def admins_add(
        self, ctx: discord.ApplicationContext, user: Option(discord.Member, "Who to grant access to")
    ):
        if not await _check_owner(ctx):
            return
        if user.id in config.ADMIN_DISCORD_IDS:
            await _reply(ctx, f"{user.mention} is already a root admin (set in .env).")
            return

        async with async_session() as session:
            granted = await grant_admin(session, user.id, granted_by=ctx.author.id)

        if granted:
            await _reply(ctx, f"Granted {user.mention} admin access.")
        else:
            await _reply(ctx, f"{user.mention} already has admin access.")

    @admins.command(name="remove", description="Revoke a user's runtime-granted admin access.")
    async def admins_remove(
        self, ctx: discord.ApplicationContext, user: Option(discord.Member, "Who to revoke access from")
    ):
        if not await _check_owner(ctx):
            return
        if user.id in config.ADMIN_DISCORD_IDS:
            await ctx.respond(
                embed=error_embed(f"{user.mention} is a root admin (set in .env) — can't be revoked here."),
                ephemeral=True,
            )
            return

        async with async_session() as session:
            revoked = await revoke_admin(session, user.id)

        if revoked:
            await _reply(ctx, f"Revoked {user.mention}'s admin access.")
        else:
            await ctx.respond(
                embed=error_embed(f"{user.mention} doesn't have runtime-granted admin access."), ephemeral=True
            )

    @admins.command(name="list", description="List everyone with /admin access, with a click-to-revoke panel.")
    async def admins_list(self, ctx: discord.ApplicationContext):
        if not await _check_owner(ctx):
            return
        view = AdminsListView(author_id=ctx.author.id)
        async with async_session() as session:
            await view._render(session)
        await ctx.respond(view=view, files=view.files, ephemeral=True)
        view.message = await ctx.interaction.original_response()


def setup(bot: discord.Bot):
    bot.add_cog(AdminCog(bot))
