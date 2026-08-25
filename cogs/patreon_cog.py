import logging

import discord
from aiohttp import web
from discord.ext import commands
from sqlalchemy import select

from db.base import async_session
from db.models import InventoryItem, PatreonLink
from services.battle_service import VENOM_BLAST_TRIGGER_INTEGRITY
from services.patreon_service import (
    GATED_ITEM_KEYS,
    GATED_ITEM_MIN_RANK,
    TIER_RANK_ARACHNID,
    TIER_RANK_LABELS,
    TIER_RANK_NONE,
    TIER_RANK_SYMBIOTE,
    PatreonLinkError,
    build_authorize_url,
    get_tier_rank,
    handle_callback,
    tier_badge,
    tier_rank_from_name,
    unlink_account,
)
from services.shakedown_service import (
    STEALTH_MODE_INACTIVITY_THRESHOLD_SECONDS,
    count_stealth_protections,
)
from utils import webapp
from utils.icons import emoji, item_label
from utils.v2_embeds import StaticView

# NOTE: Accelerated Growth (Reputation XP boost / Supportive Allies) is
# deliberately NOT wired to Patreon tiers — that mechanic belongs to the
# separate server-boost-exclusive perk track (discord.gg/spider-man Nitro
# boosting), which was built once, fully reverted, and hasn't been rebuilt.
# The underlying code (services/patreon_service.py's get_growth_choice/
# set_growth_choice, the hooks in economy.py/ally_service.py) is left intact
# and dormant on purpose — don't remove it, don't wire a /patreon command to
# it, it's for the other track whenever that gets rebuilt.

# TIER_RANK_LABELS is imported from patreon_service rather than defined here — /shop's
# purchase refusals need to name a tier too, and a service can't import a cog for it.

# Plain-English perk summaries, shared by /patreon perks AND the welcome DM. These
# used to be duplicated as prose in the welcome strings, which is exactly how the
# ally-decay drawback went stale (it read "30% faster" for a while after the number
# became 50%). One list, both surfaces — if a perk changes, it changes here once.
#
# Perks and drawbacks are separate lists rather than one list with "Drawback:"
# prefixes so each surface can frame them itself: the welcome gives the cost its own
# section instead of burying it as the last bullet of the good news.


def _glyph(key: str, line: str) -> str:
    """Prefix a perk line with that perk's own custom emoji, if it's been uploaded.

    Deliberately NOT tier attribution — the tier badge lives on the section header
    (see _perk_sections) and GAME_DESIGN §9 requires it to appear exactly once. This
    glyph says *which perk*, the badge says *whose subscription*. Same "a miss renders
    without it, never an error" contract as everything else in utils.icons, so an
    un-uploaded emoji degrades to the bare line.

    Defined up here rather than beside _bullets because the perk lists below are built
    at import time and would NameError on a forward reference.
    """
    e = emoji(key)
    return f"{e} {line}" if e else line


# The webbing perk is ONE perk with two grades, not two perks. Biomorphic Webbing is
# what Organic Webbing grows into — it does everything Organic does (no vials, no
# no-fluid cash tax) and adds bonus rolls on top, and Organic is its prerequisite,
# which the rank ladder already enforces for free since Symbiote > Arachnid. So
# exactly one of these two lines ever renders. Listing both for a Symbiote subscriber
# (which is what this did until 2026-08-22) advertises the same vial-free patrol
# twice under two different names and reads like Biomorphic is a sidegrade sitting
# next to Organic rather than the thing Organic became.
#
# The three bonus rolls do NOT all fire everywhere, so the line says where each one
# applies. Cash (BIOMORPHIC_WEBBING_CASH_CHANCE, 0.25) is rolled on both patrol paths —
# patrol_service for a quiet patrol, battle_service after a fight — so it's promised
# unqualified. The component and photo rolls (0.20 each) exist only in battle_service,
# so they're only ever on the table for a combat patrol, and the photo additionally
# needs a camera equipped. Promising all three flatly (which this did until 2026-08-22)
# tells a subscriber who mostly runs quiet patrols to expect two rolls that can never
# happen for them.
#
# As of 2026-08-24 there's a fourth roll, and it's the one that finally makes this perk
# distinguishable from plain Organic Webbing while you're NOT patrolling: the ambient
# scavenge (biomorphic_service.AMBIENT_SCAVENGE_CHANCE, raised to 0.30 on 2026-08-25) on
# /tutoring, /ally visit and /bugle submit. Named as "wherever else you go" rather than
# listing the three commands because the list would go stale the moment a fourth activity is
# wired up, and the flavor line on the pickup itself always says where it happened.
#
# NOTE FOR THE NEXT RATE CHANGE: this line quotes no percentages at all, deliberately, so
# none of the four numbers above can go stale in player-facing copy the way Venom Blast's
# threshold did. The rates in the comments here are documentation and DO need updating —
# the 0.20 → 0.30 move only touched this block. Keep it that way: if a rate ever earns a
# mention in the copy, interpolate the constant rather than typing the number.
ORGANIC_WEBBING_LINE = _glyph(
    "organic_webbing",
    "Organic Webbing — patrols never touch web-fluid vials or the no-fluid cash tax.",
)
BIOMORPHIC_WEBBING_LINE = _glyph(
    "biomorphic_webbing",
    "Biomorphic Webbing — Organic Webbing evolved. Everything it did (no vials, no "
    "no-fluid cash tax) and the suit takes more than you told it to: an extra chance at "
    "bonus cash on every patrol, a bonus component and a bonus photo on combat patrols, "
    "and it keeps helping itself wherever else you go — tutoring, visiting, selling "
    "photos — turning up components you never picked up.",
)

ARACHNID_PERKS = [
    "Enhanced Strength — +30% Attack damage on crime-tier patrols.",
    "Combat-Ready Patrols — better odds of landing a crime encounter.",
]
ARACHNID_DRAWBACKS = [
    "The people who know Peter Parker are holding onto him harder — happiness decays "
    "50% faster, so a full meter runs dry in 16 hours instead of 24. Keep showing up "
    "as Peter, or there won't be much Peter left to come back to.",
]
# Venom Blast became a button the player presses on 2026-08-24, and this line has to say so:
# a perk you have to deploy is worth nothing to a subscriber who doesn't know it's there. The
# old wording ("the suit swallows the hit whole") described the automatic version, which
# negated the incoming hit outright. A button can't do that — it's pressed before the round
# resolves, not in the middle of it — so what's promised now is guaranteed damage and a round
# nothing gets through, on the player's timing. The threshold is interpolated from
# battle_service rather than spelled out, because this exact line already went stale once when
# that number moved, and the in-fight button subtext quotes the same constant.
SYMBIOTE_PERKS_STATIC = [
    _glyph("venom_blast",
           f"Venom Blast — in a boss fight, once the suit is down to "
           f"{VENOM_BLAST_TRIGGER_INTEGRITY}% integrity or lower, a Venom Blast button unlocks "
           f"beside Evade. Spend it when you decide to: it hits twice as hard as an ordinary "
           f"attack, and nothing gets through to answer it. Once per fight."),
]


def _stealth_mode_line(protections: int) -> str:
    """Stealth Mode's perk line, with a running count of what it's actually done.

    A function rather than a constant in SYMBIOTE_PERKS_STATIC because the count is
    per-subscriber, and it belongs on this line rather than in a section of its own: the
    number is meaningless without the sentence explaining what was blocked.

    The count exists because this is the only perk in the tier that fires where its owner
    can't see it. A protected /shakedown answers the *thief* and tells the target nothing,
    and the gate only opens once the target has been idle 20+ minutes — so "it worked" and
    "you weren't watching" are the same condition. Every other perk shows itself the moment
    it fires. Zero renders as no clause at all, not "blocked 0 attempts": a brand-new
    subscriber reading the welcome DM shouldn't be handed a nil stat about a perk they
    haven't had time to benefit from.

    The threshold is interpolated, not typed. Hardcoding it is what went stale on the Venom
    Blast line above when its constant moved, and the same trap was already sprung once on
    the ally-decay drawback.
    """
    minutes = STEALTH_MODE_INACTIVITY_THRESHOLD_SECONDS // 60
    line = f"Stealth Mode — full /shakedown immunity while you've been inactive {minutes}+ minutes."
    if protections:
        line += (
            f" It's turned away {protections} attempt{'' if protections == 1 else 's'} so far — "
            f"you weren't there to see any of them."
        )
    return _glyph("stealth_mode", line)


def _symbiote_perks(stealth_protections: int) -> list[str]:
    return [*SYMBIOTE_PERKS_STATIC, _stealth_mode_line(stealth_protections)]


SYMBIOTE_DRAWBACKS = [
    "The suit overrides you when you try to hold back — in patrols and boss fights "
    "alike, a Dodge sometimes comes out as an attack, and sometimes it won't let you "
    "reach for a gadget at all. A gadget it talks you out of isn't spent, so you lose "
    "the round and not the gear. It never overrides an attack. It doesn't need to.",
]
# Display names for the gated purchasables. Which items are gated, and at which tier,
# is patreon_service.GATED_ITEM_MIN_RANK's business — this only supplies the wording, and
# the checklist below iterates the rank map so a newly gated item shows up here on its
# own rather than being silently left off the list. Buying is what the tier unlocks;
# none of these are granted for free.
GATED_ITEM_LABELS = {
    "spider_bots": "Spider Bots",
    "electric_webbing": "Electric Webbing",
    "camera_silver": "Silver-Grade Camera",
    "camera_gold": "Gold-Grade Camera",
}

log = logging.getLogger("spidey")

CALLBACK_SUCCESS_HTML = """<!doctype html><html><body style="font-family:sans-serif;text-align:center;padding:4rem;">
<h2>You're linked!</h2><p>Head back to Discord — you're all set.</p></body></html>"""

CALLBACK_ERROR_HTML = """<!doctype html><html><body style="font-family:sans-serif;text-align:center;padding:4rem;">
<h2>Something went wrong</h2><p>{message}</p></body></html>"""

# The post-link DM. Written in the past tense on purpose — by the time this arrives
# the bond has already happened, so it reports rather than pitches. It's the only
# place a subscriber is *told* what they bought without having to go looking, so it
# carries the full picture: what's on, what it costs, what still has to be bought,
# and every command for checking later.
ARACHNID_INTRO = (
    "You've **bonded with the Arachnid Spider**. The bite's taken — everything below "
    "is already running on your patrols, with nothing to switch on."
)
SYMBIOTE_INTRO = (
    "You've **bonded with the Symbiote Spider**. The bond runs deeper than the "
    "Arachnid's — you keep every bit of that, and the suit answers back now. "
    "It's on your side. That's not the same as it being safe."
)
NO_PLEDGE_INTRO = (
    "Your Patreon account is connected. There's no active pledge on it right now, so "
    "nothing below is live yet — but the moment one starts, your perks switch "
    "themselves on. You won't need to link again."
)

# Listed in full even for a brand-new subscriber who hasn't used any of them yet —
# this DM is the one message they're guaranteed to see, so it doubles as the reference.
COMMAND_LINES = [
    "`/patreon perks` — everything that's live for you, and what gear you still don't own.",
    "`/patreon status` — the raw tier the bot currently reads off Patreon.",
    "`/patreon link` — re-run any time to re-check your tier or switch Patreon accounts.",
    "`/patreon unlink` — disconnect. Perks stop immediately.",
    "`/shop browse` — where your exclusive gear lives.",
]


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"• {line}" for line in lines)


def _perk_sections(
    tier_rank: int, owned_gated_items: set[str], stealth_protections: int = 0
) -> list[tuple[str | None, list[tuple[str, str]]]]:
    """The perk breakdown shared by /patreon perks and the welcome DM, so the two can
    never drift. Symbiote is a strict superset of Arachnid, so its sections stack on
    top rather than replacing — including the drawbacks, which a Symbiote subscriber
    inherits and therefore has to be told about too. The one exception is the webbing
    perk, which upgrades in place instead of stacking (see ORGANIC_WEBBING_LINE).

    stealth_protections defaults to 0 so a caller with no reason to count (and the scratch
    checks) can omit it — 0 renders the Stealth Mode line exactly as it read before the
    count existed. Only read when the viewer is Symbiote, since that's the only tier the
    line renders for at all."""
    if tier_rank == TIER_RANK_NONE:
        return [(
            None,
            [("No active perks", "Subscribe at Arachnid or Symbiote and run /patreon link to connect.")],
        )]

    is_symbiote = tier_rank >= TIER_RANK_SYMBIOTE

    # Webbing leads the list at both tiers — it's the perk that changes every single
    # patrol, so it's the one that should land first.
    perks = [BIOMORPHIC_WEBBING_LINE if is_symbiote else ORGANIC_WEBBING_LINE, *ARACHNID_PERKS]
    drawbacks = list(ARACHNID_DRAWBACKS)
    if is_symbiote:
        perks += _symbiote_perks(stealth_protections)
        drawbacks += SYMBIOTE_DRAWBACKS

    # Emoji-only attribution, per the house rule — the tier's emoji carries the
    # attribution, never the tier name as text. This is the viewer's own tier, so a
    # Symbiote subscriber sees the Symbiote emoji on the Arachnid perks they inherited:
    # the badge means "your subscription", not "the tier this originated at".
    tier_e = tier_badge(tier_rank)
    sections: list[tuple[str | None, list[tuple[str, str]]]] = [
        (f"{tier_e} Always On".strip(), [("", _bullets(perks))]),
        (f"{tier_e} What It Costs You".strip(), [("", _bullets(drawbacks))]),
    ]

    # Deliberately its own section with ownership state: the tier unlocks the *right to
    # buy* these, it doesn't hand them over. The old welcome copy claimed Spider Bots
    # and Electric Webbing were "live right now", which sent new subscribers hunting
    # for gear they didn't have.
    #
    # Driven off the rank map, filtered to what this tier can actually buy — an
    # Arachnid subscriber shouldn't be shown a Symbiote-only item on a list headed
    # "Yours to Buy", and a new gated item lands here without a second edit.
    gear = [
        f"• {item_label(key, GATED_ITEM_LABELS.get(key, key))} — "
        f"{'Owned' if key in owned_gated_items else 'Not owned yet — `/shop browse`'}"
        for key, min_rank in GATED_ITEM_MIN_RANK.items()
        if tier_rank >= min_rank
    ]
    if gear:
        sections.append((f"{tier_e} Yours to Buy".strip(), [("", "\n".join(gear))]))
    return sections


def build_welcome_view(
    tier_rank: int, owned_gated_items: set[str], stealth_protections: int = 0
) -> StaticView:
    """The welcome DM as a Components V2 view. Note a V2 message can't also carry
    `content` or an `embed`, so this has to be entirely self-contained — and its
    `files` must be passed along as `files=view.files` or the thumbnail won't
    resolve."""
    if tier_rank >= TIER_RANK_SYMBIOTE:
        title, intro, icon_key = "Bonded — Symbiote Spider", SYMBIOTE_INTRO, "symbiote"
    elif tier_rank >= TIER_RANK_ARACHNID:
        title, intro, icon_key = "Bonded — Arachnid Spider", ARACHNID_INTRO, "arachnid"
    else:
        title, intro, icon_key = "Patreon Connected", NO_PLEDGE_INTRO, "arachnid"

    field_groups = _perk_sections(tier_rank, owned_gated_items, stealth_protections)
    field_groups.append(("Your Commands", [("", _bullets(COMMAND_LINES))]))

    return StaticView(title, description=intro, field_groups=field_groups, icon_key=icon_key)


async def _stealth_protections(tier_rank: int, discord_id: int) -> int:
    """The Stealth Mode block count, or 0 for anyone the line won't render for anyway.

    The tier check is here rather than at the two call sites so neither can forget it and
    spend a query on a card that has no Stealth Mode line to put the answer on."""
    if tier_rank < TIER_RANK_SYMBIOTE:
        return 0
    return await count_stealth_protections(discord_id)


async def _owned_gated_items(session, discord_id: int) -> set[str]:
    """Which of the Patreon-gated purchasables this user actually owns — the tier
    unlocks buying them, so ownership is a separate question from tier. Queries every
    gated key regardless of tier; the render filters down to what the caller's tier can
    buy, so a lapsed subscriber's owned gear is still recognised as owned."""
    stmt = select(InventoryItem.item_key).where(
        InventoryItem.user_id == discord_id,
        InventoryItem.item_key.in_(GATED_ITEM_KEYS),
    )
    return set((await session.execute(stmt)).scalars())


class LinkButtonView(discord.ui.View):
    """A single-use, non-interactive view — just a link button, no callback needed
    since clicking it sends the user straight to Patreon, not back through the bot."""

    def __init__(self, url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Link Patreon", style=discord.ButtonStyle.link, url=url))


class PatreonCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        webapp.app.router.add_get("/patreon/callback", self._callback)

    patreon = discord.SlashCommandGroup("patreon", "Link your Patreon account for perks.")

    @patreon.command(name="link", description="Connect your Patreon account to unlock your perks.")
    async def link(self, ctx: discord.ApplicationContext):
        # Re-running this on an existing link re-sends the welcome against whatever tier
        # the user currently resolves to. The background re-check now handles the common
        # case on its own (on_patreon_tier_upgraded, below), so this is the manual
        # fallback: the DM bounced, or the pledge landed outside the window that job
        # watches. The authorize button still goes out alongside it, so a genuine re-link
        # (switching Patreon accounts, re-checking a tier that changed on Patreon's side)
        # is unaffected.
        async with async_session() as session:
            already_linked = await session.get(PatreonLink, ctx.author.id) is not None

        try:
            url = build_authorize_url(ctx.author.id)
        except PatreonLinkError as exc:
            await ctx.respond(str(exc), ephemeral=True)
            return

        if already_linked:
            message = (
                "You're already linked — I've re-sent your welcome message with everything "
                "that's currently active for you.\n\nIf you're switching Patreon accounts or "
                "your tier changed, use the button below to reconnect."
            )
        else:
            message = "Click below to connect your Patreon account. This link is single-use and expires in 10 minutes."
        view = LinkButtonView(url)

        if already_linked:
            # Sent as its own message rather than folded into the reply: the welcome is
            # a Components V2 view, which can't share a message with content or a button.
            welcomed = await self._send_welcome(ctx.author.id)
            if not welcomed:
                message = (
                    "You're already linked, but I couldn't DM you your welcome message — your DMs "
                    "are closed. Run `/patreon perks` to see everything that's active.\n\nSwitching "
                    "accounts or tier changed? Use the button below."
                )

        if ctx.guild is None:
            # Already in DMs — just answer directly, no need to DM on top of a DM.
            await ctx.respond(message, view=view, ephemeral=True)
            return

        try:
            await ctx.author.send(message, view=view)
            await ctx.respond("Check your DMs — I've sent you a link to connect Patreon.", ephemeral=True)
        except discord.HTTPException:
            # DMs closed — fall back to answering right where the command was run.
            await ctx.respond(message, view=view, ephemeral=True)

    @patreon.command(name="unlink", description="Disconnect your Patreon account from this bot.")
    async def unlink(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            ok, message = await unlink_account(session, ctx.author.id)
        await ctx.respond(message, ephemeral=True)

    @patreon.command(name="status", description="Check what tier the bot currently sees you as.")
    async def status(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            link = await session.get(PatreonLink, ctx.author.id)
            tier_rank = await get_tier_rank(session, ctx.author.id)

        if link is None:
            await ctx.respond("Not linked yet — run /patreon link to connect your Patreon account.", ephemeral=True)
            return

        lines = [
            f"**Patreon tier reported:** {link.tier or '*(linked, no active pledge)*'}",
            f"**Perk tier:** {TIER_RANK_LABELS[tier_rank]}",
        ]
        await ctx.respond("\n".join(lines), ephemeral=True)

    @patreon.command(name="perks", description="See exactly which perks are active for you right now.")
    async def perks(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            tier_rank = await get_tier_rank(session, ctx.author.id)
            owned_gated_items = await _owned_gated_items(session, ctx.author.id)

        # Outside the block above on purpose — count_stealth_protections opens its own
        # session (see its docstring), and it's skipped entirely below Symbiote since that's
        # the only tier whose card carries the line.
        stealth_protections = await _stealth_protections(tier_rank, ctx.author.id)

        view = StaticView(
            "Your Active Perks",
            field_groups=_perk_sections(tier_rank, owned_gated_items, stealth_protections),
        )
        await ctx.respond(view=view, files=view.files, ephemeral=True)

    async def _send_welcome(self, discord_id: int) -> bool:
        """DMs the welcome card for whatever tier the user currently resolves to.
        Returns False if the DM couldn't be delivered (DMs closed), so callers that
        have somewhere else to put it can fall back instead of silently dropping it."""
        async with async_session() as session:
            tier_rank = await get_tier_rank(session, discord_id)
            owned_gated_items = await _owned_gated_items(session, discord_id)

        # A first-time subscriber has none, and 0 renders no clause — but /patreon link
        # re-sends this card on demand, so a long-standing subscriber re-running it should
        # see the same count /patreon perks would give them.
        stealth_protections = await _stealth_protections(tier_rank, discord_id)

        view = build_welcome_view(tier_rank, owned_gated_items, stealth_protections)
        try:
            user = await self.bot.fetch_user(discord_id)
            await user.send(view=view, files=view.files)
            return True
        except discord.HTTPException:
            return False

    @commands.Cog.listener()
    async def on_patreon_tier_upgraded(self, discord_id: int):
        """A background re-check found a higher tier than we had stored — dispatched by
        cogs/scheduler_cog.py's patreon_tick. This is the link-then-subscribe case: at
        link time there was no pledge, so the welcome that went out was the "Patreon
        Connected, nothing active yet" card, and the pledge itself arrives with no
        interaction for the bot to respond to.

        No fallback when the DM bounces, unlike /patreon link's copy of this — there's no
        channel to answer in, because nothing the user did triggered this. Their perks are
        live either way; the log line is so a "why did I never hear anything" question has
        an answer."""
        if not await self._send_welcome(discord_id):
            log.info(
                "Patreon upgrade welcome undelivered (DMs closed): discord_id=%s — perks are "
                "active regardless, /patreon perks shows them",
                discord_id,
            )

    async def _callback(self, request: web.Request) -> web.Response:
        code = request.query.get("code")
        state = request.query.get("state")
        if not code or not state:
            log.warning("Patreon callback: missing code/state in query params: %s", dict(request.query))
            return web.Response(text=CALLBACK_ERROR_HTML.format(message="Missing code or state."), content_type="text/html", status=400)

        try:
            async with async_session() as session:
                discord_id, tier = await handle_callback(session, code, state)
        except PatreonLinkError as exc:
            log.warning("Patreon callback failed: %s", exc)
            return web.Response(text=CALLBACK_ERROR_HTML.format(message=str(exc)), content_type="text/html", status=400)
        except Exception:
            log.exception("Patreon callback: unexpected error")
            return web.Response(
                text=CALLBACK_ERROR_HTML.format(message="Unexpected error — check the bot's logs."),
                content_type="text/html",
                status=500,
            )

        tier_rank = tier_rank_from_name(tier)
        log.info("Patreon linked: discord_id=%s tier=%r rank=%s", discord_id, tier, tier_rank)
        await self._send_welcome(discord_id)

        return web.Response(text=CALLBACK_SUCCESS_HTML, content_type="text/html")


def setup(bot: discord.Bot):
    bot.add_cog(PatreonCog(bot))
