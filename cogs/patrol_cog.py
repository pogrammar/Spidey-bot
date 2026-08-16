import random

import discord
from discord.ext import commands

from db.base import async_session
from services.battle_service import (
    BattleReport,
    BattleState,
    finalize_battle,
    resolve_attack,
    resolve_evade,
    resolve_gadget,
    start_battle,
)
from services.busy import get_busy
from services.cooldowns import format_remaining, get_remaining_seconds, set_cooldown
from services.economy import at_boss_gate, get_or_create_user
from services.gadget_service import list_all_owned_gadgets, list_equipped_gadgets
from services.patrol_service import (
    PATROL_COOLDOWN_SECONDS,
    PatrolResult,
    begin_boss_patrol,
    begin_patrol,
    boss_flavor_lines,
    boss_name,
    compute_base_xp,
    finish_noncombat_patrol,
)
from services.suit_service import repair_readiness_warning
from utils.embeds import error_embed
from utils.icons import emoji, item_label, thumbnail
from utils.v2_embeds import StaticView, add_field_groups

# Round decisions get a full 30 seconds — this is a choice, not a reflex test. Outcomes
# depend on which action you pick + a dice roll behind it, never on how fast you click,
# specifically so server/network lag can never be the reason someone loses a fight.
BATTLE_ROUND_TIMEOUT = 30.0

# The lock covering a battle's whole possible window (so a second /patrol can't
# sneak in and start an overlapping fight) is computed per-fight from its actual
# round count (BATTLE_ROUND_TIMEOUT * state.max_rounds), not a fixed constant —
# a fixed number here previously went stale the moment boss fights (always 10
# rounds) shipped, since it had only ever been sized for crime tiers' 5-7 round
# range. Reset to the normal PATROL_COOLDOWN_SECONDS the instant the battle
# actually ends (see PatrolBattleView._advance/on_timeout) — this margin only
# matters as a safety ceiling for while the fight is still theoretically open,
# never something a player actually has to wait out.
BATTLE_LOCK_MARGIN_SECONDS = 40

# The advanced suit is still trashed — you went out in the old homemade one instead.
UNPROTECTED_FLAVOR = [
    "The advanced suit's still in pieces, so you went out in the old homemade one.",
    "Still no advanced suit, so it's the beat-up first-generation one tonight.",
    "The good suit's in the shop — you're stuck with the sewing-kit special.",
]

TIER_KEYS = {
    "crime_bronze": ("Bronze", "tier_bronze"),
    "crime_silver": ("Silver", "tier_silver"),
    "crime_gold": ("Gold", "tier_gold"),
    "boss": ("Boss", "tier_boss"),
}
TIER_BUTTON_STYLES = {
    "Gold": discord.ButtonStyle.success,
    "Silver": discord.ButtonStyle.primary,
    "Bronze": discord.ButtonStyle.secondary,
    "Boss": discord.ButtonStyle.danger,
}


def _bar(current: int, maximum: int, filled_emoji: str, segments: int = 10) -> str:
    if maximum <= 0:
        return "⬜" * segments
    filled = max(0, min(segments, round((current / maximum) * segments)))
    return filled_emoji * filled + "⬜" * (segments - filled)


def _crime_line(crime_level: int, crime_level_delta: int) -> str:
    """Small subtext meant to sit right under a Reputation XP field's value — not
    its own field, just a quiet ambient readout of how patrolling nudged the city's
    crime_level (see services/economy.py's HIGH_CRIME_THRESHOLD for the payoff)."""
    return f"\n-# City crime: {crime_level}/100 ({crime_level_delta:+d})"


def _cap(name: str) -> str:
    """Capitalizes just the first letter — str.capitalize() also lowercases the rest,
    which mangles names with their own internal capitals (e.g. "a Sable mercenary")."""
    return name[0].upper() + name[1:] if name else name


class AttackButton(discord.ui.Button):
    def __init__(self, battle_view: "PatrolBattleView", *, disabled: bool):
        super().__init__(
            label="Attack", emoji=emoji("attack") or "⚡", style=discord.ButtonStyle.danger, disabled=disabled
        )
        self.battle_view = battle_view

    async def callback(self, interaction: discord.Interaction):
        await self.battle_view._advance(interaction, resolve_attack(self.battle_view.state))


class EvadeButton(discord.ui.Button):
    def __init__(self, battle_view: "PatrolBattleView", *, disabled: bool):
        super().__init__(
            label="Evade", emoji=emoji("evade") or "🛡️", style=discord.ButtonStyle.primary, disabled=disabled
        )
        self.battle_view = battle_view

    async def callback(self, interaction: discord.Interaction):
        await self.battle_view._advance(interaction, resolve_evade(self.battle_view.state))


class GadgetActionButton(discord.ui.Button):
    """One of these gets added per equipped gadget (up to 2) — a real loadout choice
    instead of a single fixed button, since which gadget to spend matters."""

    def __init__(self, gadget_key: str, gadget_name: str, battle_view: "PatrolBattleView", *, disabled: bool):
        super().__init__(
            label=gadget_name,
            emoji=emoji(gadget_key) or emoji("gadget_use") or "🔧",
            style=discord.ButtonStyle.success,
            disabled=disabled,
        )
        self.gadget_key = gadget_key
        self.battle_view = battle_view

    async def callback(self, interaction: discord.Interaction):
        async with async_session() as session:
            line = await resolve_gadget(session, self.battle_view.author_id, self.battle_view.state, self.gadget_key)
        await self.battle_view._advance(interaction, line)


class GadgetSelect(discord.ui.Select):
    """Boss fights only — every owned gadget goes in one dropdown instead of a
    button each. A full kit (5 gadgets today, more later as new abilities land)
    would blow past Discord's 5-per-row button limit fast; a Select scales to 25
    options without ever getting cluttered. Broken gadgets are just left out of the
    list — a Select can't gray out individual options the way a button can."""

    def __init__(self, battle_view: "PatrolBattleView", options_data: list[tuple[str, str]]):
        options = [
            discord.SelectOption(label=name, value=key, emoji=emoji(key) or emoji("gadget_use"))
            for key, name in options_data
        ]
        super().__init__(placeholder="Use a gadget...", options=options)
        self.battle_view = battle_view

    async def callback(self, interaction: discord.Interaction):
        gadget_key = self.values[0]
        async with async_session() as session:
            line = await resolve_gadget(session, self.battle_view.author_id, self.battle_view.state, gadget_key)
        await self.battle_view._advance(interaction, line)


class PatrolBattleView(discord.ui.DesignerView):
    def __init__(self, state: BattleState, author_id: int, intro_banner: str):
        super().__init__(timeout=BATTLE_ROUND_TIMEOUT)
        self.state = state
        self.author_id = author_id
        self.message: discord.Message | None = None
        self.files: list[discord.File] = []
        self._render(banner=intro_banner)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your fight.", ephemeral=True)
            return False
        return True

    def _tier(self) -> tuple[str, str]:
        return TIER_KEYS[self.state.outcome_key]

    def _header_accessory(
        self, icon_key: str, fallback_label: str, fallback_style: discord.ButtonStyle
    ) -> tuple[discord.ui.Thumbnail | discord.ui.Button, discord.File | None]:
        """Real badge art if it exists, else the same disabled-text-badge fallback
        this project used before any icons existed — never an error either way."""
        result = thumbnail(icon_key)
        if result is not None:
            return result
        return discord.ui.Button(label=fallback_label, style=fallback_style, disabled=True), None

    def _render(self, banner: str | None = None) -> None:
        """Round-in-progress card: live HP/suit meters, the last few log lines under a
        large-spacing divider (visually splitting 'what's true right now' from 'what
        just happened'), and a real ActionRow — no embed involved at all."""
        self.clear_items()
        tier, tier_icon_key = self._tier()
        round_num = min(self.state.round_number, self.state.max_rounds)

        container = discord.ui.Container()
        header_text = f"# Patrol Battle — Round {round_num}/{self.state.max_rounds}"
        if banner:
            header_text += f"\n{banner}"
        accessory, file = self._header_accessory(
            tier_icon_key, tier, TIER_BUTTON_STYLES.get(tier, discord.ButtonStyle.secondary)
        )
        container.add_section(discord.ui.TextDisplay(header_text), accessory=accessory)
        self.files = [file] if file else []
        container.add_separator()

        container.add_text(
            f"**{_cap(self.state.enemy_name)}**\n"
            f"{_bar(self.state.enemy_hp, self.state.enemy_max_hp, '🟥')}  "
            f"{self.state.enemy_hp}/{self.state.enemy_max_hp} HP"
        )
        container.add_separator(divider=False)
        container.add_text(
            f"**Your Suit**\n{_bar(self.state.suit_remaining, 100, '🟩')}  {self.state.suit_remaining}%"
        )

        if self.state.combo_ready:
            container.add_separator(divider=False)
            combo_emoji = emoji("attack") or "⚡"
            container.add_text(f"{combo_emoji} **Combo Ready** — next Attack is a guaranteed hit for bonus damage.")

        if self.state.log:
            container.add_separator(spacing=discord.SeparatorSpacingSize.large)
            log_lines = "\n".join(f"• {line.strip()}" for line in self.state.log[-3:])
            container.add_text(f"-# BATTLE LOG\n{log_lines}")

        container.add_separator()
        if self.state.outcome_key == "boss":
            container.add_row(AttackButton(self, disabled=False), EvadeButton(self, disabled=False))
            usable_gadgets = [
                (key, name)
                for key, name in self.state.available_gadgets
                if key not in self.state.broken_gadget_keys
            ]
            if usable_gadgets:
                container.add_row(GadgetSelect(self, usable_gadgets))
            elif self.state.available_gadgets:
                container.add_separator(divider=False)
                container.add_text("-# Every gadget you brought is broken for this fight.")
            else:
                container.add_separator(divider=False)
                container.add_text("-# No gadgets owned — buy some from /shop.")
        else:
            container.add_row(
                AttackButton(self, disabled=False),
                EvadeButton(self, disabled=False),
                *(
                    GadgetActionButton(key, name, self, disabled=key in self.state.broken_gadget_keys)
                    for key, name in self.state.available_gadgets
                ),
            )
            if not self.state.available_gadgets:
                container.add_separator()
                container.add_text("-# No gadgets equipped this fight.")

        self.add_item(container)

    def _render_final(self, report: BattleReport, suit_warning: str | None, timed_out: bool = False) -> None:
        self.clear_items()
        tier, _ = self._tier()

        if timed_out:
            result_icon_key, result_label = "timeout", "Timed Out"
            headline = "You hesitate too long and the moment passes."
            badge_style = discord.ButtonStyle.secondary
        elif report.won_clean:
            result_icon_key, result_label = "victory", "Victory"
            headline = f"{_cap(self.state.enemy_name)} goes down clean."
            badge_style = discord.ButtonStyle.success
        elif self.state.end_reason == "suit_depleted":
            result_icon_key, result_label = "retreat", "Defeated"
            headline = f"Your suit gives out — {_cap(self.state.enemy_name)} leaves you no room to keep fighting."
            badge_style = discord.ButtonStyle.danger
        else:
            result_icon_key, result_label = "retreat", "Retreated"
            headline = f"{_cap(self.state.enemy_name)} is still standing — you disengage."
            badge_style = discord.ButtonStyle.danger

        container = discord.ui.Container()
        accessory, file = self._header_accessory(result_icon_key, result_label, badge_style)
        container.add_section(discord.ui.TextDisplay(f"# Patrol Battle — {tier} — Over\n{headline}"), accessory=accessory)
        self.files = [file] if file else []

        crime_line = _crime_line(report.crime_level, report.crime_level_delta)
        outcome_fields = [(f"{emoji('reputation') or ''} Reputation XP".strip(), f"+{report.xp_gained}{crime_line}")]
        if report.cash_gained:
            outcome_fields.append(("Cash", f"+${report.cash_gained:,}"))
        outcome_fields.append((f"{emoji('suit_damage') or ''} Suit Damage".strip(), f"-{report.suit_damage}%"))
        field_groups = [("Outcome", outcome_fields)]

        if report.photo_banked:
            camera_label = item_label("camera", "Camera")
            caught = f"{camera_label} broke mid-shot!" if report.camera_broke else "Photo saved for the Bugle."
            camera_emoji = emoji("camera")
            heading = f"{camera_emoji} {report.photo_quality.title()} Photo Op" if camera_emoji else f"{report.photo_quality.title()} Photo Op"
            field_groups.append((heading, [("", caught)]))

        if report.unprotected_penalty:
            value = (
                f"{random.choice(UNPROTECTED_FLAVOR)} No plating, no reinforcement — "
                f"it cost you: -${report.unprotected_penalty:,}"
            )
            field_groups.append(("🧵 Homemade Suit", [("", value)]))

        if report.item_found:
            found_label = item_label(report.item_found, report.item_found.replace("_", " ").title())
            field_groups.append(("Scavenged", [("", found_label)]))

        if report.gadgets_broken:
            value = f"{', '.join(report.gadgets_broken)} took too much punishment and gave out. Check /shop."
            gadget_emoji = emoji("gadget_use")
            heading = f"{gadget_emoji} Gadget Broke" if gadget_emoji else "Gadget Broke"
            field_groups.append((heading, [("", value)]))

        if report.boss_new_level:
            boss_emoji = emoji("tier_boss")
            heading = f"{boss_emoji} Boss Cleared" if boss_emoji else "Boss Cleared"
            field_groups.append(
                (heading, [("", f"You break through — Reputation Level {report.boss_new_level} unlocked.")])
            )

        if report.boss_cash_reward:
            money_emoji = emoji("money")
            heading = f"{money_emoji} City Thanks You" if money_emoji else "City Thanks You"
            value = f"Taking down a threat like that doesn't go unnoticed. (+${report.boss_cash_reward:,})"
            field_groups.append((heading, [("", value)]))

        if report.donation_flavor:
            money_emoji = emoji("money")
            heading = f"{money_emoji} City Thanks You" if money_emoji else "City Thanks You"
            field_groups.append((heading, [("", f"{report.donation_flavor} (+${report.donation_cash:,})")]))

        if report.hazard_flavor:
            field_groups.append(("Parker Luck", [("", f"{report.hazard_flavor} (${report.hazard_cash:,})")]))

        if suit_warning:
            warning_emoji = emoji("suit_warning") or "⚠️"
            field_groups.append((f"{warning_emoji} Suit Warning", [("", suit_warning)]))

        add_field_groups(container, field_groups)
        self.add_item(container)

    async def _advance(self, interaction: discord.Interaction, line: str) -> None:
        self.state.log.append(line)

        if self.state.enemy_hp <= 0:
            self.state.ended = True
            self.state.end_reason = "won"
        elif self.state.outcome_key == "boss" and self.state.suit_remaining <= 0:
            # Boss fights only — suit integrity doubles as your HP here (crime
            # tiers don't require full suit to start, so 0% mid-fight there stays
            # cosmetic, not a loss condition). A killing blow always skips the
            # enemy's counter roll in the same round (see resolve_attack/
            # resolve_gadget), so this can never fire on the same round you win.
            self.state.ended = True
            self.state.end_reason = "suit_depleted"
        elif self.state.round_number >= self.state.max_rounds:
            self.state.ended = True
            self.state.end_reason = "rounds_exhausted"
        else:
            self.state.round_number += 1

        if not self.state.ended:
            # Tier badge doesn't change round to round, so the attachment from the
            # initial send stays valid — no need to touch files/attachments here.
            self._render()
            await interaction.response.edit_message(view=self)
            return

        async with async_session() as session:
            user = await get_or_create_user(session, self.author_id)
            report = await finalize_battle(session, user, self.state)
            await set_cooldown(session, self.author_id, "patrol", PATROL_COOLDOWN_SECONDS)
            suit_warning = await repair_readiness_warning(session, user)

        self.stop()
        self._render_final(report, suit_warning)
        # Swapping from the tier badge to the outcome badge — a genuinely different
        # image, so attachments=[] clears the old one and files=self.files adds the new.
        await interaction.response.edit_message(view=self, files=self.files, attachments=[])

    async def on_timeout(self) -> None:
        if self.state.ended:
            return
        self.state.ended = True
        self.state.end_reason = "timeout"

        async with async_session() as session:
            user = await get_or_create_user(session, self.author_id)
            report = await finalize_battle(session, user, self.state)
            await set_cooldown(session, self.author_id, "patrol", PATROL_COOLDOWN_SECONDS)

        self._render_final(report, suit_warning=None, timed_out=True)
        if self.message is not None:
            try:
                await self.message.edit(view=self, files=self.files, attachments=[])
            except discord.HTTPException:
                pass


def _boss_gate_view(bracket: int, suit_integrity: int) -> StaticView:
    description = random.choice(boss_flavor_lines(bracket))
    return StaticView(
        "Boss Incoming",
        description,
        fields=[("Suit Integrity", f"{suit_integrity}% — need 100% to take this fight")],
        footer_lines=["/workbench repair when you're ready."],
        icon_key="tier_boss",
    )


def _noncombat_view(result: PatrolResult, suit_warning: str | None) -> StaticView:
    fluid_field_name = item_label("web_fluid_vial", "Web Fluid")
    fields = []
    if result.web_fluid_used:
        fields.append((fluid_field_name, "-1 vial"))
    else:
        fields.append((
            fluid_field_name,
            f"Out of vials — improvised with store-bought fluid: -${result.web_fluid_tax:,}. "
            f"Brew more with /lab brew.",
        ))

    xp_note = " (thriving allies bonus)" if result.ally_xp_bonus else ""
    crime_line = _crime_line(result.crime_level, result.crime_level_delta)
    fields.append((f"{emoji('reputation') or ''} Reputation XP".strip(), f"+{result.xp_gained}{xp_note}{crime_line}"))

    if result.cash_gained:
        fields.append(("Cash", f"+${result.cash_gained:,}"))

    field_groups = [(None, fields)]
    if result.hazard_flavor:
        field_groups.append(("Parker Luck", [("", f"{result.hazard_flavor} (${result.hazard_cash:,})")]))
    if suit_warning:
        warning_emoji = emoji("suit_warning") or "⚠️"
        field_groups.append((f"{warning_emoji} Suit Warning", [("", suit_warning)]))

    return StaticView("Patrol Report", result.flavor, field_groups=field_groups, icon_key="web_fluid_vial")


class PatrolCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(
        name="patrol", description="Swing out and see what's happening in the city."
    )
    async def patrol(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)

            busy = await get_busy(session, user.discord_id)
            if busy is not None:
                label, remaining_busy = busy
                await ctx.respond(
                    embed=error_embed(
                        f"You're busy {label} right now. Back on the streets in {format_remaining(remaining_busy)}."
                    )
                )
                return

            remaining = await get_remaining_seconds(session, user.discord_id, "patrol")
            if remaining > 0:
                await ctx.respond(
                    embed=error_embed(
                        f"You're still catching your breath from the last patrol. "
                        f"Try again in {format_remaining(remaining)}."
                    )
                )
                return

            boss_bracket = None
            if at_boss_gate(user):
                if user.suit_integrity < 100:
                    gate_view = _boss_gate_view(user.boss_clears + 1, user.suit_integrity)
                    await ctx.respond(view=gate_view, files=gate_view.files)
                    return
                boss_bracket = user.boss_clears + 1
                start = await begin_boss_patrol(session, user)
            else:
                start = await begin_patrol(session, user)

            is_combat = start.outcome["key"] in ("crime_bronze", "crime_silver", "crime_gold", "boss")

            if not is_combat:
                result = await finish_noncombat_patrol(session, user, start)
                await set_cooldown(session, user.discord_id, "patrol", PATROL_COOLDOWN_SECONDS)
                suit_warning = await repair_readiness_warning(session, user)
                noncombat_view = _noncombat_view(result, suit_warning)
                await ctx.respond(view=noncombat_view, files=noncombat_view.files)
                return

            base_xp = compute_base_xp(start)
            gadget_source = list_all_owned_gadgets if boss_bracket else list_equipped_gadgets
            equipped = await gadget_source(session, user.discord_id)
            available_gadgets = []
            for inv_item in equipped:
                item_def = await inv_item.awaitable_attrs.item
                available_gadgets.append((inv_item.item_key, item_def.name))

            state = start_battle(
                outcome_key=start.outcome["key"],
                difficulty=start.difficulty,
                starting_suit_integrity=user.suit_integrity,
                base_xp=base_xp,
                base_cash=0,
                available_gadgets=available_gadgets,
                enemy_name=boss_name(boss_bracket) if boss_bracket else None,
            )

            # Lock /patrol for this fight's actual worst-case window (see
            # BATTLE_LOCK_MARGIN_SECONDS above) so a second call can't start an
            # overlapping fight.
            lock_seconds = round(BATTLE_ROUND_TIMEOUT * state.max_rounds) + BATTLE_LOCK_MARGIN_SECONDS
            await set_cooldown(session, user.discord_id, "patrol", lock_seconds)

        fluid_note = (
            ""
            if start.web_fluid_used
            else f" (out of {item_label('web_fluid_vial', 'Web-Fluid')} — cost you ${start.web_fluid_tax:,})"
        )
        view = PatrolBattleView(state, ctx.author.id, intro_banner=f"{start.flavor}{fluid_note}")
        await ctx.respond(view=view, files=view.files)
        view.message = await ctx.interaction.original_response()


def setup(bot: discord.Bot):
    bot.add_cog(PatrolCog(bot))
