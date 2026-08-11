import random

import discord
from discord.ext import commands

from db.base import async_session
from services.battle_service import BattleReport, BattleState, finalize_battle, resolve_attack, resolve_evade, resolve_gadget, start_battle
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
from utils.embeds import SPIDEY_GREEN, SPIDEY_RED, base_embed, error_embed

# Round decisions get a full 30 seconds — this is a choice, not a reflex test. Outcomes
# depend on which action you pick + a dice roll behind it, never on how fast you click,
# specifically so server/network lag can never be the reason someone loses a fight.
BATTLE_ROUND_TIMEOUT = 30.0

# Covers the worst case (3 rounds x 30s + margin) so a second /patrol can't sneak in
# and start an overlapping fight. Reset to the normal 30s the moment the battle ends.
BATTLE_LOCK_SECONDS = 130

# The advanced suit is still trashed — you went out in the old homemade one instead.
UNPROTECTED_FLAVOR = [
    "The advanced suit's still in pieces, so you went out in the old homemade one.",
    "Still no advanced suit, so it's the beat-up first-generation one tonight.",
    "The good suit's in the shop — you're stuck with the sewing-kit special.",
]


class GadgetActionButton(discord.ui.Button):
    """One of these gets added per equipped gadget (up to 2) — a real loadout choice
    instead of a single fixed button, since which gadget to spend matters."""

    def __init__(self, gadget_key: str, gadget_name: str, battle_view: "PatrolBattleView"):
        super().__init__(label=f"🔧 {gadget_name}", style=discord.ButtonStyle.success)
        self.gadget_key = gadget_key
        self.battle_view = battle_view

    async def callback(self, interaction: discord.Interaction):
        async with async_session() as session:
            line = await resolve_gadget(session, self.battle_view.author_id, self.battle_view.state, self.gadget_key)
        await self.battle_view._advance(interaction, line)


class PatrolBattleView(discord.ui.View):
    def __init__(self, state: BattleState, author_id: int):
        super().__init__(timeout=BATTLE_ROUND_TIMEOUT)
        self.state = state
        self.author_id = author_id

        self.gadget_buttons: list[GadgetActionButton] = []
        for key, name in state.available_gadgets:
            btn = GadgetActionButton(key, name, self)
            self.gadget_buttons.append(btn)
            self.add_item(btn)

        self._sync_buttons()

    def _sync_buttons(self) -> None:
        ended = self.state.ended
        self.attack_button.disabled = ended
        self.evade_button.disabled = ended
        for btn in self.gadget_buttons:
            btn.disabled = ended or btn.gadget_key in self.state.broken_gadget_keys

    def render(self, banner: str | None = None) -> discord.Embed:
        round_num = min(self.state.round_number, self.state.max_rounds)
        tier = "Gold" if self.state.outcome_key == "crime_gold" else "Bronze"
        embed = base_embed(
            f"Patrol Battle — {tier} — Round {round_num}/{self.state.max_rounds}",
            banner or f"You run into {self.state.enemy_name}.",
        )
        embed.add_field(name="Enemy", value=f"{self.state.enemy_hp}/{self.state.enemy_max_hp} HP")
        embed.add_field(name="Your Suit", value=f"{self.state.suit_remaining}%")
        if self.state.combo_ready:
            embed.add_field(name="⚡ Combo Ready", value="Next Attack is a guaranteed hit for bonus damage.", inline=False)
        if self.state.log:
            embed.add_field(
                name="Log", value="\n".join(f"• {line}" for line in self.state.log[-3:]), inline=False
            )
        if not self.state.available_gadgets:
            embed.set_footer(text="No gadgets equipped this fight.")
        return embed

    def _final_embed(self, report: BattleReport, timed_out: bool = False) -> discord.Embed:
        if timed_out:
            result_label = "⏱️ Timed Out"
            headline = "You hesitate too long and the moment passes."
            colour = SPIDEY_RED
        elif report.won_clean:
            result_label = "🏆 Victory"
            headline = f"{self.state.enemy_name.capitalize()} goes down clean."
            colour = SPIDEY_GREEN
        else:
            result_label = "🏃 Retreated"
            headline = f"{self.state.enemy_name.capitalize()} is still standing — you disengage."
            colour = SPIDEY_RED

        tier = "Gold" if self.state.outcome_key == "crime_gold" else "Bronze"
        embed = base_embed(f"Patrol Battle — {tier} — Over", headline, colour=colour)
        embed.add_field(name="Result", value=result_label, inline=False)
        embed.add_field(name="Reputation XP", value=f"+{report.xp_gained}")
        if report.cash_gained:
            embed.add_field(name="Cash", value=f"+${report.cash_gained:,}")
        embed.add_field(name="Suit Damage", value=f"-{report.suit_damage}%")

        if report.photo_banked:
            caught = "Camera broke mid-shot!" if report.camera_broke else "Photo saved for the Bugle."
            embed.add_field(name=f"{report.photo_quality.title()} Photo Op", value=caught)

        if report.unprotected_penalty:
            embed.add_field(
                name="🧵 Homemade Suit",
                value=(
                    f"{random.choice(UNPROTECTED_FLAVOR)} No plating, no reinforcement — "
                    f"it cost you: -${report.unprotected_penalty:,}"
                ),
                inline=False,
            )
        if report.item_found:
            embed.add_field(name="Scavenged", value=report.item_found.replace("_", " ").title())
        if report.gadgets_broken:
            embed.add_field(
                name="Gadget Broke",
                value=f"{', '.join(report.gadgets_broken)} took too much punishment and gave out. Check /shop.",
                inline=False,
            )
        if report.donation_flavor:
            embed.add_field(
                name="City Thanks You", value=f"{report.donation_flavor} (+${report.donation_cash:,})", inline=False
            )
        if report.hazard_flavor:
            embed.add_field(
                name="Parker Luck", value=f"{report.hazard_flavor} (${report.hazard_cash:,})", inline=False
            )
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your fight.", ephemeral=True)
            return False
        return True

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
            self._sync_buttons()
            await interaction.response.edit_message(embed=self.render(), view=self)
            return

        async with async_session() as session:
            user = await get_or_create_user(session, self.author_id)
            report = await finalize_battle(session, user, self.state)
            await set_cooldown(session, self.author_id, "patrol", PATROL_COOLDOWN_SECONDS)
            suit_warning = await repair_readiness_warning(session, user)

        self.stop()
        self._sync_buttons()
        embed = self._final_embed(report)
        if suit_warning:
            embed.add_field(name="⚠️ Suit Warning", value=suit_warning, inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="⚡ Attack", style=discord.ButtonStyle.danger)
    async def attack_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._advance(interaction, resolve_attack(self.state))

    @discord.ui.button(label="🛡️ Evade", style=discord.ButtonStyle.primary)
    async def evade_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._advance(interaction, resolve_evade(self.state))

    async def on_timeout(self) -> None:
        if self.state.ended:
            return
        self.state.ended = True
        self.state.end_reason = "timeout"

        async with async_session() as session:
            user = await get_or_create_user(session, self.author_id)
            report = await finalize_battle(session, user, self.state)
            await set_cooldown(session, self.author_id, "patrol", PATROL_COOLDOWN_SECONDS)

        self._sync_buttons()
        if self.message is not None:
            try:
                await self.message.edit(embed=self._final_embed(report, timed_out=True), view=self)
            except discord.HTTPException:
                pass


def _noncombat_embed(result: PatrolResult, suit_warning: str | None) -> discord.Embed:
    embed = base_embed("Patrol Report", result.flavor)

    if result.web_fluid_used:
        embed.add_field(name="Web Fluid", value="-1 vial")
    else:
        embed.add_field(
            name="Web Fluid",
            value=f"Out of vials — improvised with store-bought fluid: -${result.web_fluid_tax:,}. "
            f"Brew more with /lab brew.",
            inline=False,
        )

    xp_note = " (thriving allies bonus)" if result.ally_xp_bonus else ""
    embed.add_field(name="Reputation XP", value=f"+{result.xp_gained}{xp_note}")

    if result.cash_gained:
        embed.add_field(name="Cash", value=f"+${result.cash_gained:,}")

    if result.hazard_flavor:
        embed.add_field(
            name="Parker Luck", value=f"{result.hazard_flavor} (${result.hazard_cash:,})", inline=False
        )

    if suit_warning:
        embed.add_field(name="⚠️ Suit Warning", value=suit_warning, inline=False)

    return embed


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
                await ctx.respond(embed=_noncombat_embed(result, suit_warning))
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

        view = PatrolBattleView(state, ctx.author.id)
        fluid_note = "" if start.web_fluid_used else f" (out of Web-Fluid — cost you ${start.web_fluid_tax:,})"
        await ctx.respond(embed=view.render(f"{start.flavor}{fluid_note}"), view=view)
        view.message = await ctx.interaction.original_response()


def setup(bot: discord.Bot):
    bot.add_cog(PatrolCog(bot))
