import random

import discord
from discord.ext import commands

from db.base import async_session
from services.battle_service import (
    VENOM_BLAST_TRIGGER_INTEGRITY,
    BattleReport,
    BattleState,
    finalize_battle,
    resolve_attack,
    resolve_evade,
    resolve_gadget,
    resolve_venom_blast,
    start_battle,
    venom_blast_ready,
)
from services.busy import get_busy
from services.cooldowns import format_remaining, get_remaining_seconds, set_cooldown
from services.economy import at_boss_gate, get_or_create_user
from services.gadget_service import list_usable_gadgets
from services.patreon_service import (
    TIER_RANK_ARACHNID,
    TIER_RANK_SYMBIOTE,
    accent_for_rank,
    get_tier_rank,
    tier_badge,
)
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
from utils.v2_embeds import StaticView, add_field_groups, make_container

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


def _biomorphic_cash_line(amount: int) -> str:
    """Subtext naming Biomorphic Webbing's share of a Cash value (Symbiote+ perk).
    Same shape as _crime_line — an ambient readout under the number rather than its
    own field, since it isn't separate income, just part of that number the player
    would otherwise have no way to attribute.

    Attribution follows the two-glyph rule (GAME_DESIGN.md §9): the perk's own emoji
    leads (which perk fired) and the tier badge trails (whose subscription paid for
    it). Until 2026-08-24 the tier badge did both jobs, which meant every Symbiote
    perk announced itself with the same glyph and none of them said which one it was.
    The tier's name is still never spelled out. No tier_rank parameter because this
    line only exists on a report where report.biomorphic_cash is set, and that field
    can only be set at Symbiote."""
    glyph = emoji("biomorphic_webbing")
    lead = f"{glyph} " if glyph else ""
    badge = tier_badge(TIER_RANK_SYMBIOTE)
    trail = f" {badge}" if badge else ""
    return f"\n-# {lead}The webbing shook an extra ${amount:,} loose.{trail}"


def _webbing_note(biomorphic: bool) -> str:
    """Credits the vial-free patrol to whichever grade of the webbing perk is actually
    running. Organic and Biomorphic aren't two perks that stack — Biomorphic is what
    Organic grows into, so a Symbiote subscriber has no Organic Webbing to speak of
    any more and shouldn't be told a lower tier's perk is what saved them the vial.

    Perk glyph leads, tier badge trails, tier name never spelled out (GAME_DESIGN.md
    §9). This one does spell out the *perk's* name, deliberately: it's the only place a
    player learns which grade of webbing they're running, and the two grades are the
    whole point of the line."""
    glyph = emoji("biomorphic_webbing" if biomorphic else "organic_webbing")
    lead = f"{glyph} " if glyph else ""
    name = "Biomorphic Webbing" if biomorphic else "Organic Webbing"
    badge = tier_badge(TIER_RANK_SYMBIOTE if biomorphic else TIER_RANK_ARACHNID)
    trail = f" {badge}" if badge else ""
    return f"{lead}{name} — no vial needed{trail}"


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
        state = self.battle_view.state
        tier_rank = self.battle_view.tier_rank
        line = resolve_attack(state, tier_rank)
        await self.battle_view._advance(interaction, line)


class EvadeButton(discord.ui.Button):
    def __init__(self, battle_view: "PatrolBattleView", *, disabled: bool):
        super().__init__(
            label="Evade", emoji=emoji("evade") or "🛡️", style=discord.ButtonStyle.primary, disabled=disabled
        )
        self.battle_view = battle_view

    async def callback(self, interaction: discord.Interaction):
        state = self.battle_view.state
        tier_rank = self.battle_view.tier_rank
        line = resolve_evade(state, tier_rank)
        await self.battle_view._advance(interaction, line)


class VenomBlastButton(discord.ui.Button):
    """Symbiote's once-per-boss-fight charge, deployed by hand.

    Only added to the row when the player is Symbiote and the fight is a boss — a
    permanently-dead button would be advertising mid-fight, and /patreon perks is where
    that belongs. But for someone who *does* have it, the button is present from round 1
    in its greyed-out "Not Charged" state, because a button that only appears once the
    condition is met can't teach anyone the condition exists. The subtext line under the
    row spells out what charges it (see PatrolBattleView._render).

    Three labels, all rendered greyed except the middle one:
      "Venom Blast — Not Charged"  integrity still above the bar
      "Venom Blast"                armed; the only live state
      "Venom Blast — Spent"        already used this fight

    Kept in place after it's spent rather than dropped, so the row never reflows under a
    player mid-click and turns their Evade into something else.

    style is secondary because it's the only style left — Attack owns danger, Evade owns
    primary, gadgets own success — and it happens to be the right one anyway: grey reads
    as inert until it isn't, which is exactly this button's life cycle.
    """

    def __init__(self, battle_view: "PatrolBattleView"):
        state = battle_view.state
        ready = venom_blast_ready(state, battle_view.tier_rank)
        if state.venom_blast_used:
            label = "Venom Blast — Spent"
        elif ready:
            label = "Venom Blast"
        else:
            label = "Venom Blast — Not Charged"
        super().__init__(
            label=label,
            emoji=emoji("venom_blast") or "🕷️",
            style=discord.ButtonStyle.secondary,
            disabled=not ready,
        )
        self.battle_view = battle_view

    async def callback(self, interaction: discord.Interaction):
        state = self.battle_view.state
        tier_rank = self.battle_view.tier_rank
        line = resolve_venom_blast(state, tier_rank)
        if not line:
            # Discord won't deliver a press for a button it rendered disabled, but it can
            # still deliver one for a button that *was* enabled on the copy of the message
            # in front of the player — two fast clicks, or a click landing while the
            # previous round's edit is still in flight. Refuse rather than advance a round
            # on an empty line, and never let the charge get spent twice.
            await interaction.response.send_message(
                "The bond isn't charged for that.", ephemeral=True
            )
            return
        await self.battle_view._advance(interaction, line)


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
        state = self.battle_view.state
        tier_rank = self.battle_view.tier_rank
        async with async_session() as session:
            line = await resolve_gadget(session, self.battle_view.author_id, state, self.gadget_key, tier_rank)
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
        state = self.battle_view.state
        tier_rank = self.battle_view.tier_rank
        async with async_session() as session:
            line = await resolve_gadget(session, self.battle_view.author_id, state, gadget_key, tier_rank)
        await self.battle_view._advance(interaction, line)


class PatrolBattleView(discord.ui.DesignerView):
    def __init__(self, state: BattleState, author_id: int, tier_rank: int, intro_banner: str):
        super().__init__(timeout=BATTLE_ROUND_TIMEOUT)
        self.state = state
        self.author_id = author_id
        self.tier_rank = tier_rank
        # Derived from the rank this view is already handed, rather than read from the
        # ambient per-command context — same answer, one less thing to resolve. Stored
        # because _render and _render_final both run from button/select callbacks and
        # from on_timeout, none of which carry that context.
        self.accent = accent_for_rank(tier_rank)
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

        container = make_container(self.accent)
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
            # Venom Blast rides in the same row as the two core actions rather than a row
            # of its own — it's a third choice for the round, not a mode. Symbiote only, so
            # the row is two buttons wide for everyone else and nothing shifted for them.
            actions: list[discord.ui.Button] = [
                AttackButton(self, disabled=False),
                EvadeButton(self, disabled=False),
            ]
            if self.tier_rank >= TIER_RANK_SYMBIOTE:
                actions.append(VenomBlastButton(self))
            container.add_row(*actions)
            if self.tier_rank >= TIER_RANK_SYMBIOTE and not self.state.venom_blast_used:
                # Only while the charge is unspent, and only ever an explanation of the bar
                # — never a nudge to press it. The perk glyph leads and the tier badge
                # trails, per GAME_DESIGN.md §9.
                blast_glyph = emoji("venom_blast")
                lead = f"{blast_glyph} " if blast_glyph else ""
                badge = tier_badge(self.tier_rank)
                trail = f" {badge}" if badge else ""
                if venom_blast_ready(self.state, self.tier_rank):
                    note = "The bond is charged. One shot, this fight only."
                else:
                    note = (
                        "The bond charges at "
                        f"{VENOM_BLAST_TRIGGER_INTEGRITY}% suit integrity or lower."
                    )
                container.add_separator(divider=False)
                container.add_text(f"-# {lead}{note}{trail}")
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

        container = make_container(self.accent)
        accessory, file = self._header_accessory(result_icon_key, result_label, badge_style)
        container.add_section(discord.ui.TextDisplay(f"# Patrol Battle — {tier} — Over\n{headline}"), accessory=accessory)
        self.files = [file] if file else []

        crime_line = _crime_line(report.crime_level, report.crime_level_delta)
        outcome_fields = [(f"{emoji('reputation') or ''} Reputation XP".strip(), f"+{report.xp_gained}{crime_line}")]
        if report.cash_gained:
            cash_value = f"+${report.cash_gained:,}"
            if report.biomorphic_cash:
                cash_value += _biomorphic_cash_line(report.biomorphic_cash)
            outcome_fields.append(("Cash", cash_value))
        outcome_fields.append((f"{emoji('suit_damage') or ''} Suit Damage".strip(), f"-{report.suit_damage}%"))
        field_groups = [("Outcome", outcome_fields)]

        if report.photo_banked:
            # The camera that actually took the shot, not a hardcoded "camera" — a
            # subscriber shooting on a $3,000 Gold body was being shown the beat-up 35mm
            # everywhere a photo came up. finalize_battle resolves it (through the live
            # tier check), so a lapsed pledge correctly falls back to the stock body here
            # as well as in the stats.
            caught = (
                f"{report.camera_label} broke mid-shot!"
                if report.camera_broke
                else "Photo saved for the Bugle."
            )
            camera_emoji = emoji(report.camera_item_key)
            quality = report.photo_quality.title()
            heading = f"{camera_emoji} {quality} Photo Op" if camera_emoji else f"{quality} Photo Op"
            # One row (just `caught`) keeps the old bold-heading layout untouched; the
            # perk rows below turn the group into a headed list, which is the point —
            # a premium perk firing should visibly restructure the card, not hide in it.
            photo_rows = []
            if report.photo_quality_bumped:
                # The paid camera's headline perk. It rolls invisibly and, before
                # this, changed nothing the player could see — the photo was just
                # quietly worth more at /bugle sell time. Now it gets a labelled row
                # with the before -> after tiers spelled out and the tier badge per
                # GAME_DESIGN.md §9. before_bump is only ever set on a real tier
                # change, so this can't print "Gold -> Gold".
                #
                # Badge and lens both follow the player: this used to hardcode Arachnid
                # and "the silver lens", which told a Symbiote subscriber on a Gold body
                # that somebody else's tier and somebody else's camera had done the work.
                badge = tier_badge(self.tier_rank)
                photo_rows.append((
                    f"{badge} Photo Upgraded".strip(),
                    f"{report.photo_quality_before_bump.title()} → **{quality}** — {report.camera_label} "
                    f"caught detail the stock body would have thrown away.",
                ))
            photo_rows.append(("", caught))
            if report.biomorphic_photo:
                # Perk glyph leads the heading, tier badge trails it — same two-glyph rule
                # as _biomorphic_cash_line. This used to lead with the tier badge, which
                # made it indistinguishable from the Photo Upgraded row directly above.
                glyph = emoji("biomorphic_webbing") or ""
                badge = tier_badge(self.tier_rank)
                photo_rows.append((
                    f"{glyph} Second Shot {badge}".strip(),
                    "The webbing kept shooting after you'd stopped — a second photo banked too.",
                ))
            field_groups.append((heading, photo_rows))

        if report.unprotected_penalty:
            value = (
                f"{random.choice(UNPROTECTED_FLAVOR)} No plating, no reinforcement — "
                f"it cost you: -${report.unprotected_penalty:,}"
            )
            field_groups.append(("🧵 Homemade Suit", [("", value)]))

        if report.item_found:
            found_label = item_label(report.item_found, report.item_found.replace("_", " ").title())
            if report.biomorphic_component:
                # Byte-identical to an ordinary drop before this — the whole perk was
                # a component appearing that otherwise wouldn't have, with nothing
                # saying so. Only fires when the base scavenge roll missed.
                # Perk glyph leads, tier badge trails (GAME_DESIGN.md §9).
                glyph = emoji("biomorphic_webbing")
                lead = f"{glyph} " if glyph else ""
                badge = tier_badge(self.tier_rank)
                trail = f" {badge}" if badge else ""
                found_label += (
                    f"\n-# {lead}You'd have walked right past it — the webbing caught it.{trail}"
                )
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
            report = await finalize_battle(session, user, self.state, tier_rank=self.tier_rank)
            await set_cooldown(session, self.author_id, "patrol", PATROL_COOLDOWN_SECONDS)
            suit_warning = await repair_readiness_warning(session, user, self.tier_rank)

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
            report = await finalize_battle(session, user, self.state, tier_rank=self.tier_rank)
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
    if result.organic_webbing_active:
        fields.append((fluid_field_name, _webbing_note(result.biomorphic_webbing_active)))
    elif result.web_fluid_used:
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
        cash_value = f"+${result.cash_gained:,}"
        if result.biomorphic_cash:
            cash_value += _biomorphic_cash_line(result.biomorphic_cash)
        fields.append(("Cash", cash_value))

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
            tier_rank = await get_tier_rank(session, ctx.author.id)

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
                start = await begin_boss_patrol(session, user, tier_rank)
            else:
                start = await begin_patrol(session, user, tier_rank)

            is_combat = start.outcome["key"] in ("crime_bronze", "crime_silver", "crime_gold", "boss")

            if not is_combat:
                result = await finish_noncombat_patrol(session, user, start, tier_rank)
                await set_cooldown(session, user.discord_id, "patrol", PATROL_COOLDOWN_SECONDS)
                suit_warning = await repair_readiness_warning(session, user, tier_rank)
                noncombat_view = _noncombat_view(result, suit_warning)
                await ctx.respond(view=noncombat_view, files=noncombat_view.files)
                return

            base_xp = compute_base_xp(start)
            # Usable, not merely equipped: a gadget the player's lapsed pledge has switched
            # off must not get a button. It would roll through roll_gadget_effect, get
            # filtered there, and fall through to the fumble branch every single time —
            # which reads as the gadget being broken rather than the perk being revoked.
            equipped = await list_usable_gadgets(session, user.discord_id, all_owned=bool(boss_bracket))
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

        if start.organic_webbing_active:
            fluid_note = f" ({_webbing_note(start.biomorphic_webbing_active)})"
        elif start.web_fluid_used:
            fluid_note = ""
        else:
            fluid_note = f" (out of {item_label('web_fluid_vial', 'Web-Fluid')} — cost you ${start.web_fluid_tax:,})"
        view = PatrolBattleView(state, ctx.author.id, tier_rank, intro_banner=f"{start.flavor}{fluid_note}")
        await ctx.respond(view=view, files=view.files)
        view.message = await ctx.interaction.original_response()


def setup(bot: discord.Bot):
    bot.add_cog(PatrolCog(bot))
