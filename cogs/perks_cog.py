"""`/perks` — what the community server is currently giving the person who ran it, plus the
one decision it lets them make.

Two subcommands, and the split is the whole shape of the file:

`/perks status` reports. Every perk here switches on and off with a Discord role, which
means a player can gain or lose one without any command ever telling them, and §9
attribution says a perk the player can't attribute reads as a bug. This is the only surface
that can say what's live and why it changed.

`/perks choose` is the one thing on this track that is a choice. Higher Reputation and
Supportive Allies are one slot with two occupants (services/server_perks.ServerPerks._pair
has the arithmetic for why they can never both be live), and at level 10 both are unlocked.
Until 2026-08-30 the slot resolved itself and this file was read-only by construction — the
owner reversed that so a level 10 member can move between them freely. Everything else here
is still pure report: the Bronze camera is handed over automatically by utils/first_run.py,
boosting and the level ladder are role reads, and none of them has a claim step.

The cost of the choice being a subcommand is that bare `/perks` no longer exists — Discord
won't let a command with subcommands also be invoked on its own.
"""

import random

import discord
from discord import Option, OptionChoice
from discord.ext import commands

from db.base import async_session
from services.ads_service import SERVER_INVITE_URL
from services.ally_service import (
    FULL_DECAY_HOURS,
    SUPPORTIVE_ALLIES_DECAY_MULTIPLIER,
    THRIVING_HAPPINESS_THRESHOLD,
)
from services.battle_service import HIGHER_INTEGRITY_DAMAGE_REDUCTION
from services.brewing_service import BREW_DURATION, QUICKER_BREW_DURATION
from services.economy import ACCELERATED_GROWTH_XP_MULTIPLIER, get_or_create_user
from services.patrol_service import CAMERA_BRONZE_ITEM_KEY, CAMERA_TIER_STATS
from services.server_perks import (
    LOWER_COOLDOWN_MULTIPLIER,
    PAIR_CHOICE_ALLIES,
    PAIR_CHOICE_XP,
    PERK_RANK_LEVEL_5,
    ServerPerks,
    in_perks_guild,
    resolve_perks,
    set_pair_choice,
)
from utils.icons import emoji, item_label
from utils.v2_embeds import StaticView

PERKS_FOOTERS = [
    "Boost the server, climb the levels, and the city gets a little easier.",
    "None of this costs money. It costs showing up.",
    "The suit doesn't care where you got the help.",
]

# A dark row still renders, with the requirement on it. Hiding what someone doesn't have
# turns the panel into a list of things they already knew and removes the only reason to
# run it twice.
#
# The lit box is the project's own `ready` glyph rather than a green unicode tick, with the
# tick kept as the fallback — same "a miss renders without it, never an error" contract as
# everything else in utils.icons, so this is still correct on a bot with no emoji uploaded.
ON = emoji("ready") or "✅"
OFF = "▫️"

# Names, glyphs and slash-command labels for the two occupants of the exclusive slot, in one
# place so the panel, the confirmation and the option list can't drift apart. OptionChoice
# labels are plain strings that can't render custom emoji markup at all, which is why they
# take the bare name — see item_label's note.
PAIR_NAMES = {PAIR_CHOICE_XP: "Higher Reputation", PAIR_CHOICE_ALLIES: "Supportive Allies"}
PAIR_GLYPHS = {PAIR_CHOICE_XP: "reputation", PAIR_CHOICE_ALLIES: "gifts_category"}
PAIR_OPTION_CHOICES = [
    OptionChoice(name="Higher Reputation — more reputation XP", value=PAIR_CHOICE_XP),
    OptionChoice(name="Supportive Allies — slower happiness drain", value=PAIR_CHOICE_ALLIES),
]


def _pct(fraction: float) -> str:
    """A multiplier or reduction as a whole-percent string.

    Everything player-facing in this file goes through here or an f-string on the same
    constant, per §9.3 — no perk percentage is ever typed as a literal, because that is
    exactly how the Patreon ally-decay line went stale ("30% faster" for weeks after the
    number became 50%)."""
    return f"{round(abs(fraction) * 100)}%"


def _minutes(delta) -> str:
    return f"{round(delta.total_seconds() / 60)} min"


def _row(live: bool, glyph_key: str, name: str, detail: str, requirement: str) -> str:
    """One perk line: state box, perk glyph, name, and either what it does or what it
    would take. The perk's own emoji says *which perk*; nothing here names the source in
    words, per §9 — the section heading carries that.

    An empty tail drops the dash rather than rendering "**Name** — " with nothing after
    it. That isn't defensive padding: the exclusive pair below has a state for each of its
    two rows where the other row's note is carrying the explanation, and a trailing dash
    there reads as copy that failed to load."""
    mark = ON if live else OFF
    label = item_label(glyph_key, name) if glyph_key else name
    tail = detail if live else requirement
    return f"{mark} **{label}** — {tail}" if tail else f"{mark} **{label}**"

def _xp_detail() -> str:
    return f"reputation XP {ACCELERATED_GROWTH_XP_MULTIPLIER}x from patrols and tutoring"


def _allies_detail() -> str:
    # The full-drain hours are computed, not quoted: the perk is a multiplier on a rate, so
    # the only honest way to state the outcome is to derive it the way _decayed_happiness
    # does.
    stretched = FULL_DECAY_HOURS / SUPPORTIVE_ALLIES_DECAY_MULTIPLIER
    return (
        f"Aunt May and MJ lose happiness {_pct(1 - SUPPORTIVE_ALLIES_DECAY_MULTIPLIER)} slower "
        f"— a full meter lasts about {round(stretched)}h instead of {round(FULL_DECAY_HOURS)}h, "
        f"so staying above {THRIVING_HAPPINESS_THRESHOLD} is far less work"
    )


# Functions rather than strings so every number is read at render time, which is what makes
# the §9.3 interpolation check able to move a constant and see the copy follow.
PAIR_DETAILS = {PAIR_CHOICE_XP: _xp_detail, PAIR_CHOICE_ALLIES: _allies_detail}


def _pair_line(perks: ServerPerks) -> str:
    """The one line that can't be a plain on/off row.

    Higher Reputation and Supportive Allies are one slot with two occupants, so the panel
    renders the *resolved* answer and then a note explaining how it got there. Two of the
    states it explains are surprising and both are correct, which is why the note isn't
    optional: reaching level 10 puts a plain member on the ally perk, so their XP rate drops
    the moment they level up; and a subscriber's untouched default is the XP boost, so a
    pledge lapse moves them across. Each is a change the player didn't ask for at the moment
    it happens, so each has to be readable at the moment they come looking."""
    if perks.rank < PERK_RANK_LEVEL_5:
        return (
            f"{OFF} **{PAIR_NAMES[PAIR_CHOICE_XP]} / {PAIR_NAMES[PAIR_CHOICE_ALLIES]}** — one "
            f"slot, two perks. Level 5 fills it with the XP boost; at level 10 the second one "
            f"unlocks and you pick which is live."
        )

    lines = [
        _row(
            perks.higher_reputation,
            PAIR_GLYPHS[PAIR_CHOICE_XP],
            PAIR_NAMES[PAIR_CHOICE_XP],
            _xp_detail(),
            "the ally perk is holding this slot",
        ),
        _row(
            perks.supportive_allies,
            PAIR_GLYPHS[PAIR_CHOICE_ALLIES],
            PAIR_NAMES[PAIR_CHOICE_ALLIES],
            _allies_detail(),
            # Two different reasons this row can be dark, and they need different words: at
            # level 5 the slot has only one occupant, at level 10 both are yours and the
            # other one is sitting in it. "Reach level 10" shown to a level 10 member would
            # read as the panel not knowing what level they are.
            "reach level 10" if not perks.can_choose_pair else "the XP boost is holding this slot",
        ),
    ]

    if not perks.can_choose_pair:
        note = "-# At level 10 the second one unlocks and you choose between them. Never both."
    elif perks.choice is not None:
        note = (
            "-# You picked this one. Switch back whenever you like with `/perks choose` — the "
            "two never run together."
        )
    elif perks.pair_default == PAIR_CHOICE_XP:
        # Only reachable through a live pledge, which moves the default rather than removing
        # the choice. Says the slot started somewhere unusual without naming the source in
        # words, per §9.
        note = (
            "-# Level 10 normally starts you on the ally perk. Yours starts on the XP boost "
            "instead. Move it either way with `/perks choose`."
        )
    else:
        note = (
            "-# Level 10 starts you here rather than on the XP boost — it's a trade, not a "
            "loss. Swap them any time with `/perks choose`."
        )
    return "\n".join([*lines, note])


def _heading(glyph_key: str, text: str) -> str:
    e = emoji(glyph_key)
    return f"{e} {text}" if e else text


def _sections(perks: ServerPerks) -> list[tuple[str | None, list[tuple[str, str]]]]:
    booster_rows = [
        _row(
            perks.organic_webbing,
            "organic_webbing",
            "Organic Webbing",
            "patrols never touch web-fluid vials or the no-fluid cash tax",
            "boost the server",
        ),
    ]

    bronze_stats = CAMERA_TIER_STATS[CAMERA_BRONZE_ITEM_KEY]
    level_rows = [
        _row(
            perks.lower_cooldown,
            "timeout",
            "Lower Cooldown",
            f"patrol, Bugle and shakedown come back {_pct(1 - LOWER_COOLDOWN_MULTIPLIER)} sooner",
            "reach level 5",
        ),
        _row(
            perks.higher_integrity,
            "suit_integrity",
            "Higher Integrity",
            f"the suit takes {_pct(HIGHER_INTEGRITY_DAMAGE_REDUCTION)} less damage on crime "
            f"patrols (boss fights unchanged)",
            "reach level 5",
        ),
        _row(
            perks.quicker_brewing,
            "lab",
            "Quicker Lab Brewing",
            f"a batch is ready in {_minutes(QUICKER_BREW_DURATION)} instead of "
            f"{_minutes(BREW_DURATION)}",
            "reach level 10",
        ),
        _row(
            perks.bronze_camera,
            CAMERA_BRONZE_ITEM_KEY,
            "Bronze-Grade Camera",
            f"yours already — {_pct(bronze_stats['break_chance_reduction'])} less likely to "
            f"break, and a {_pct(bronze_stats['quality_bump_chance'])} shot at bumping a photo "
            f"up a tier",
            "reach level 10",
        ),
    ]

    return [
        (_heading("booster", "From Boosting"), [("", "\n".join(booster_rows))]),
        (_heading("reputation", "From Your Level"), [("", "\n".join(level_rows))]),
        (None, [("", _pair_line(perks))]),
    ]

# The two empty states. They used to be one panel, on the reasoning that "the feature is off
# here" and "you were somewhere else" both mean no roles were readable — but that told a
# member standing in the server that they weren't in it. Only one of these two situations is
# fixed by joining, so only one of them gets the invite.
NOT_IN_SERVER_BODY = (
    "These come off the community server's roles, so they only apply to commands you run "
    "inside it — that's the whole deal, and it's why they don't follow you into DMs. Come "
    "hang out, pick up a level or two, and run this again."
)
NO_ROLES_BODY = (
    "You haven't unlocked these yet. They come from boosting the server and from the level "
    "roles you pick up just by being around in it — nothing to buy and nothing to claim. "
    "Reach level 5 and this panel starts filling in."
)
JOIN_BUTTON = ("Join the Discord", SERVER_INVITE_URL)


def _empty_view(body: str, *, invite: bool) -> StaticView:
    return StaticView(
        "Server Perks",
        body,
        footer_lines=[random.choice(PERKS_FOOTERS)],
        icon_key="locked",
        link_button=JOIN_BUTTON if invite else None,
    )


def _too_early_body(perks: ServerPerks) -> str:
    """Why /perks choose has nothing to offer yet, in the two ways that can be true."""
    if perks.rank < PERK_RANK_LEVEL_5:
        return (
            "There's nothing to pick yet. The slot this command switches shows up at level 5 "
            "with the XP boost already in it, and the second perk unlocks at level 10 — that's "
            "when you get a say in which one is live. `/perks status` shows where you are."
        )
    return (
        f"The slot is yours, it just has one occupant until level 10: right now it's holding "
        f"**{PAIR_NAMES[PAIR_CHOICE_XP]}**. Reach level 10 and "
        f"**{PAIR_NAMES[PAIR_CHOICE_ALLIES]}** unlocks alongside it — then this command starts "
        f"moving between them."
    )

class PerksCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    perks = discord.SlashCommandGroup(
        "perks", "Community-server perks — what's live for you, and the one choice you get."
    )

    @perks.command(
        name="status", description="See which community-server perks are live for you."
    )
    async def status(self, ctx: discord.ApplicationContext):
        # The guild check comes before the database read: outside the server there is no role
        # list in the payload, so there is nothing to resolve and nothing to look up. It also
        # covers the feature being switched off on this bot (PERKS_GUILD_ID unset), which
        # renders as the invite rather than as "you've unlocked nothing" — an unconfigured
        # bot has no business telling anyone what they've earned.
        if not in_perks_guild(ctx):
            view = _empty_view(NOT_IN_SERVER_BODY, invite=True)
            await ctx.respond(view=view, files=view.files)
            return

        async with async_session() as session:
            perks = await resolve_perks(session, ctx)

        if not perks.any_perk:
            view = _empty_view(NO_ROLES_BODY, invite=False)
            await ctx.respond(view=view, files=view.files)
            return

        # No description: the panel is a list of perks, and a sentence about how role reads
        # work is a note to whoever wrote this file, not to the person running it.
        view = StaticView(
            "Server Perks",
            field_groups=_sections(perks),
            footer_lines=[random.choice(PERKS_FOOTERS)],
            icon_key="reputation",
        )
        await ctx.respond(view=view, files=view.files)

    @perks.command(
        name="choose", description="Pick which half of the level 10 perk slot is live."
    )
    async def choose(
        self,
        ctx: discord.ApplicationContext,
        perk: Option(str, "Which one do you want live?", choices=PAIR_OPTION_CHOICES),
    ):
        if not in_perks_guild(ctx):
            view = _empty_view(NOT_IN_SERVER_BODY, invite=True)
            await ctx.respond(view=view, files=view.files)
            return

        async with async_session() as session:
            perks = await resolve_perks(session, ctx)
            if not perks.can_choose_pair:
                view = StaticView(
                    "Nothing to Choose Yet",
                    _too_early_body(perks),
                    footer_lines=[random.choice(PERKS_FOOTERS)],
                    icon_key="locked",
                )
                await ctx.respond(view=view, files=view.files)
                return

            # Written even when it matches what's already live, which is not a wasted
            # UPDATE: an unset choice resolves to a default that moves with the player's
            # pledge, so somebody who deliberately confirms the perk they're on is asking for
            # it to stop moving. Storing it is the difference between "this is where I landed"
            # and "this is what I want".
            user = await get_or_create_user(session, ctx.author.id)
            await set_pair_choice(session, user, perk)

        other = PAIR_CHOICE_ALLIES if perk == PAIR_CHOICE_XP else PAIR_CHOICE_XP
        view = StaticView(
            "Locked In",
            f"You're on **{PAIR_NAMES[perk]}** from here on. Switch back whenever you feel like "
            f"it with `/perks choose`.",
            fields=[
                ("", _row(True, PAIR_GLYPHS[perk], PAIR_NAMES[perk], PAIR_DETAILS[perk](), "")),
                (
                    "",
                    _row(
                        False,
                        PAIR_GLYPHS[other],
                        PAIR_NAMES[other],
                        "",
                        "switched off — the slot only ever holds one",
                    ),
                ),
            ],
            footer_lines=[random.choice(PERKS_FOOTERS)],
            icon_key=PAIR_GLYPHS[perk],
        )
        await ctx.respond(view=view, files=view.files)


def setup(bot: discord.Bot):
    bot.add_cog(PerksCog(bot))
