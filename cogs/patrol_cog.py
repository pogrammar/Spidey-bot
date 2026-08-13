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
from services.economy import get_or_create_user
from services.gadget_service import list_equipped_gadgets
from services.patrol_service import (
    PATROL_COOLDOWN_SECONDS,
    PatrolResult,
    begin_patrol,
    compute_base_xp,
    finish_noncombat_patrol,
)
from services.suit_service import repair_readiness_warning
from utils.embeds import error_embed
from utils.v2_embeds import StaticView, add_field_groups

# Round decisions get a full 30 seconds — this is a choice, not a reflex test. Outcomes
# depend on which action you pick + a dice roll behind it, never on how fast you click,
# specifically so server/network lag can never be the reason someone loses a fight.
BATTLE_ROUND_TIMEOUT = 30.0

# Covers the worst case (max(ROUND_RANGE)=7 rounds x 30s + margin) so a second
# /patrol can't sneak in and start an overlapping fight. Reset to the normal 30s the
# moment the battle ends.
BATTLE_LOCK_SECONDS = 250

# The advanced suit is still trashed — you went out in the old homemade one instead.
UNPROTECTED_FLAVOR = [
    "The advanced suit's still in pieces, so you went out in the old homemade one.",
    "Still no advanced suit, so it's the beat-up first-generation one tonight.",
    "The good suit's in the shop — you're stuck with the sewing-kit special.",
]


def _bar(current: int, maximum: int, filled_emoji: str, segments: int = 10) -> str:
    if maximum <= 0:
        return "⬜" * segments
    filled = max(0, min(segments, round((current / maximum) * segments)))
    return filled_emoji * filled + "⬜" * (segments - filled)


def _cap(name: str) -> str:
    """Capitalizes just the first letter — str.capitalize() also lowercases the rest,
    which mangles names with their own internal capitals (e.g. "a Sable mercenary")."""
    return name[0].upper() + name[1:] if name else name


class AttackButton(discord.ui.Button):
    def __init__(self, battle_view: "PatrolBattleView", *, disabled: bool):
        super().__init__(label="Attack", emoji="⚡", style=discord.ButtonStyle.danger, disabled=disabled)
        self.battle_view = battle_view

    async def callback(self, interaction: discord.Interaction):
        await self.battle_view._advance(interaction, resolve_attack(self.battle_view.state))


class EvadeButton(discord.ui.Button):
    def __init__(self, battle_view: "PatrolBattleView", *, disabled: bool):
        super().__init__(label="Evade", emoji="🛡️", style=discord.ButtonStyle.primary, disabled=disabled)
        self.battle_view = battle_view

    async def callback(self, interaction: discord.Interaction):
        await self.battle_view._advance(interaction, resolve_evade(self.battle_view.state))


class GadgetActionButton(discord.ui.Button):
    """One of these gets added per equipped gadget (up to 2) — a real loadout choice
    instead of a single fixed button, since which gadget to spend matters."""

    def __init__(self, gadget_key: str, gadget_name: str, battle_view: "PatrolBattleView", *, disabled: bool):
        super().__init__(label=gadget_name, emoji="🔧", style=discord.ButtonStyle.success, disabled=disabled)
        self.gadget_key = gadget_key
        self.battle_view = battle_view

    async def callback(self, interaction: discord.Interaction):
        async with async_session() as session:
            line = await resolve_gadget(session, self.battle_view.author_id, self.battle_view.state, self.gadget_key)
        await self.battle_view._advance(interaction, line)


class PatrolBattleView(discord.ui.DesignerView):
    def __init__(self, state: BattleState, author_id: int, intro_banner: str):
        super().__init__(timeout=BATTLE_ROUND_TIMEOUT)
        self.state = state
        self.author_id = author_id
        self.message: discord.Message | None = None
        self._render(banner=intro_banner)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your fight.", ephemeral=True)
            return False
        return True

    def _tier(self) -> tuple[str, str]:
        return ("Gold", "🥇") if self.state.outcome_key == "crime_gold" else ("Bronze", "🥉")

    def _render(self, banner: str | None = None) -> None:
        """Round-in-progress card: live HP/suit meters, the last few log lines under a
        large-spacing divider (visually splitting 'what's true right now' from 'what
        just happened'), and a real ActionRow — no embed involved at all."""
        self.clear_items()
        tier, tier_emoji = self._tier()
        round_num = min(self.state.round_number, self.state.max_rounds)

        container = discord.ui.Container()
        header_text = f"# Patrol Battle — Round {round_num}/{self.state.max_rounds}"
        if banner:
            header_text += f"\n{banner}"
        container.add_section(
            discord.ui.TextDisplay(header_text),
            accessory=discord.ui.Button(
                label=f"{tier_emoji} {tier}",
                style=discord.ButtonStyle.success if tier == "Gold" else discord.ButtonStyle.secondary,
                disabled=True,
            ),
        )
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
            container.add_text("⚡ **Combo Ready** — next Attack is a guaranteed hit for bonus damage.")

        if self.state.log:
            container.add_separator(spacing=discord.SeparatorSpacingSize.large)
            log_lines = "\n".join(f"• {line.strip()}" for line in self.state.log[-3:])
            container.add_text(f"-# BATTLE LOG\n{log_lines}")

        container.add_separator()
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
            result_label = "⏱️ Timed Out"
            headline = "You hesitate too long and the moment passes."
            badge_style = discord.ButtonStyle.secondary
        elif report.won_clean:
            result_label = "🏆 Victory"
            headline = f"{_cap(self.state.enemy_name)} goes down clean."
            badge_style = discord.ButtonStyle.success
        else:
            result_label = "🏃 Retreated"
            headline = f"{_cap(self.state.enemy_name)} is still standing — you disengage."
            badge_style = discord.ButtonStyle.danger

        container = discord.ui.Container()
        container.add_section(
            discord.ui.TextDisplay(f"# Patrol Battle — {tier} — Over\n{headline}"),
            accessory=discord.ui.Button(label=result_label, style=badge_style, disabled=True),
        )

        outcome_fields = [("Reputation XP", f"+{report.xp_gained}")]
        if report.cash_gained:
            outcome_fields.append(("Cash", f"+${report.cash_gained:,}"))
        outcome_fields.append(("Suit Damage", f"-{report.suit_damage}%"))
        field_groups = [("Outcome", outcome_fields)]

        if report.photo_banked:
            caught = "Camera broke mid-shot!" if report.camera_broke else "Photo saved for the Bugle."
            field_groups.append((f"{report.photo_quality.title()} Photo Op", [("", caught)]))

        if report.unprotected_penalty:
            value = (
                f"{random.choice(UNPROTECTED_FLAVOR)} No plating, no reinforcement — "
                f"it cost you: -${report.unprotected_penalty:,}"
            )
            field_groups.append(("🧵 Homemade Suit", [("", value)]))

        if report.item_found:
            field_groups.append(("Scavenged", [("", report.item_found.replace("_", " ").title())]))

        if report.gadgets_broken:
            value = f"{', '.join(report.gadgets_broken)} took too much punishment and gave out. Check /shop."
            field_groups.append(("Gadget Broke", [("", value)]))

        if report.donation_flavor:
            field_groups.append(("City Thanks You", [("", f"{report.donation_flavor} (+${report.donation_cash:,})")]))

        if report.hazard_flavor:
            field_groups.append(("Parker Luck", [("", f"{report.hazard_flavor} (${report.hazard_cash:,})")]))

        if suit_warning:
            field_groups.append(("⚠️ Suit Warning", [("", suit_warning)]))

        add_field_groups(container, field_groups)
        self.add_item(container)

    async def _advance(self, interaction: discord.Interaction, line: str) -> None:
        self.state.log.append(line)

        if self.state.enemy_hp <= 0:
            self.state.ended = True
            self.state.end_reason = "won"
        elif self.state.round_number >= self.state.max_rounds:
            self.state.ended = True
            self.state.end_reason = "rounds_exhausted"
        else:
            self.state.round_number += 1

        if not self.state.ended:
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
        await interaction.response.edit_message(view=self)

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
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def _noncombat_view(result: PatrolResult, suit_warning: str | None) -> StaticView:
    fields = []
    if result.web_fluid_used:
        fields.append(("Web Fluid", "-1 vial"))
    else:
        fields.append((
            "Web Fluid",
            f"Out of vials — improvised with store-bought fluid: -${result.web_fluid_tax:,}. "
            f"Brew more with /lab brew.",
        ))

    xp_note = " (thriving allies bonus)" if result.ally_xp_bonus else ""
    fields.append(("Reputation XP", f"+{result.xp_gained}{xp_note}"))

    if result.cash_gained:
        fields.append(("Cash", f"+${result.cash_gained:,}"))

    field_groups = [(None, fields)]
    if result.hazard_flavor:
        field_groups.append(("Parker Luck", [("", f"{result.hazard_flavor} (${result.hazard_cash:,})")]))
    if suit_warning:
        field_groups.append(("⚠️ Suit Warning", [("", suit_warning)]))

    return StaticView("Patrol Report", result.flavor, field_groups=field_groups)


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

            start = await begin_patrol(session, user)
            is_crime = start.outcome["key"] in ("crime_bronze", "crime_gold")

            if not is_crime:
                result = await finish_noncombat_patrol(session, user, start)
                await set_cooldown(session, user.discord_id, "patrol", PATROL_COOLDOWN_SECONDS)
                suit_warning = await repair_readiness_warning(session, user)
                await ctx.respond(view=_noncombat_view(result, suit_warning))
                return

            # Crime encounter — lock /patrol for the whole possible battle window so a
            # second call can't start an overlapping fight; reset to normal once this ends.
            await set_cooldown(session, user.discord_id, "patrol", BATTLE_LOCK_SECONDS)

            base_xp = compute_base_xp(start)
            equipped = await list_equipped_gadgets(session, user.discord_id)
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
            )

        fluid_note = "" if start.web_fluid_used else f" (out of Web-Fluid — cost you ${start.web_fluid_tax:,})"
        view = PatrolBattleView(state, ctx.author.id, intro_banner=f"{start.flavor}{fluid_note}")
        await ctx.respond(view=view)
        view.message = await ctx.interaction.original_response()


def setup(bot: discord.Bot):
    bot.add_cog(PatrolCog(bot))
