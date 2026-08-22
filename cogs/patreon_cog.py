import logging

import discord
from aiohttp import web
from discord.ext import commands
from sqlalchemy import select

from db.base import async_session
from db.models import InventoryItem, PatreonLink
from services.patreon_service import (
    TIER_RANK_ARACHNID,
    TIER_RANK_NONE,
    TIER_RANK_SYMBIOTE,
    PatreonLinkError,
    build_authorize_url,
    get_tier_rank,
    handle_callback,
    tier_rank_from_name,
    unlink_account,
)
from services.shop_service import ARACHNID_GATED_ITEM_KEYS
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

TIER_RANK_LABELS = {
    TIER_RANK_NONE: "None",
    TIER_RANK_ARACHNID: "Arachnid",
    TIER_RANK_SYMBIOTE: "Symbiote",
}

# Plain-English perk summaries, shared by /patreon perks AND the welcome DM. These
# used to be duplicated as prose in the welcome strings, which is exactly how the
# ally-decay drawback went stale (it read "30% faster" for a while after the number
# became 50%). One list, both surfaces — if a perk changes, it changes here once.
#
# Perks and drawbacks are separate lists rather than one list with "Drawback:"
# prefixes so each surface can frame them itself: the welcome gives the cost its own
# section instead of burying it as the last bullet of the good news.
ARACHNID_PERKS = [
    "Organic Webbing — patrols never touch web-fluid vials or the no-fluid cash tax.",
    "Enhanced Strength — +30% Attack damage on crime-tier patrols.",
    "Combat-Ready Patrols — better odds of landing a crime encounter.",
]
ARACHNID_DRAWBACKS = [
    "The people who know Peter Parker are holding onto him harder — happiness decays "
    "50% faster, so a full meter runs dry in 16 hours instead of 24. Keep showing up "
    "as Peter, or there won't be much Peter left to come back to.",
]
SYMBIOTE_PERKS = [
    "Venom Blast — the hit that would end a boss fight is absorbed and countered instead (once per fight).",
    "Biomorphic Webbing — extra chance at bonus cash, a bonus component, and a bonus photo.",
    "Stealth Mode — full /shakedown immunity while you've been inactive 20+ minutes.",
]
SYMBIOTE_DRAWBACKS = [
    "Sonic Dampener — the Shocker's frequency cuts through the bond. +30% incoming "
    "damage from him specifically.",
]
# Arachnid+-gated items you have to actually buy (see shop_service.ARACHNID_GATED_ITEM_KEYS)
# — being Arachnid+ only unlocks the ability to buy these, it doesn't grant them for free.
GATED_ITEM_LABELS = {
    "spider_bots": "Spider Bots",
    "electric_webbing": "Electric Webbing",
    "camera_silver": "Silver-Grade Camera",
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


def _perk_sections(tier_rank: int, owned_gated_items: set[str]) -> list[tuple[str | None, list[tuple[str, str]]]]:
    """The perk breakdown shared by /patreon perks and the welcome DM, so the two can
    never drift. Symbiote is a strict superset of Arachnid, so its sections stack on
    top rather than replacing — including the drawbacks, which a Symbiote subscriber
    inherits and therefore has to be told about too."""
    if tier_rank == TIER_RANK_NONE:
        return [(
            None,
            [("No active perks", "Subscribe at Arachnid or Symbiote and run /patreon link to connect.")],
        )]

    perks = list(ARACHNID_PERKS)
    drawbacks = list(ARACHNID_DRAWBACKS)
    if tier_rank >= TIER_RANK_SYMBIOTE:
        perks += SYMBIOTE_PERKS
        drawbacks += SYMBIOTE_DRAWBACKS

    # Emoji-only attribution, per the house rule — the tier's emoji carries the
    # attribution, never the tier name as text. Symbiote gets its own emoji rather
    # than reusing Arachnid's.
    tier_e = emoji("symbiote" if tier_rank >= TIER_RANK_SYMBIOTE else "arachnid") or ""
    sections: list[tuple[str | None, list[tuple[str, str]]]] = [
        (f"{tier_e} Always On".strip(), [("", _bullets(perks))]),
        (f"{tier_e} What It Costs You".strip(), [("", _bullets(drawbacks))]),
    ]

    # Deliberately its own section with ownership state: being Arachnid+ unlocks the
    # *right to buy* these, it doesn't hand them over. The old welcome copy claimed
    # Spider Bots and Electric Webbing were "live right now", which sent new
    # subscribers hunting for gear they didn't have.
    gear = [
        f"• {item_label(key, label)} — {'Owned' if key in owned_gated_items else 'Not owned yet — `/shop browse`'}"
        for key, label in GATED_ITEM_LABELS.items()
    ]
    sections.append((f"{tier_e} Yours to Buy".strip(), [("", "\n".join(gear))]))
    return sections


def build_welcome_view(tier_rank: int, owned_gated_items: set[str]) -> StaticView:
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

    field_groups = _perk_sections(tier_rank, owned_gated_items)
    field_groups.append(("Your Commands", [("", _bullets(COMMAND_LINES))]))

    return StaticView(title, description=intro, field_groups=field_groups, icon_key=icon_key)


async def _owned_gated_items(session, discord_id: int) -> set[str]:
    """Which of the Arachnid+-gated purchasables this user actually owns — the tier
    unlocks buying them, so ownership is a separate question from tier."""
    stmt = select(InventoryItem.item_key).where(
        InventoryItem.user_id == discord_id,
        InventoryItem.item_key.in_(ARACHNID_GATED_ITEM_KEYS),
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
        # Someone who subscribed before ever linking — or who subscribed, linked, and
        # then upgraded — has no other way to get their welcome. Re-running /patreon
        # link re-sends it against their current tier. The authorize button still goes
        # out alongside it, so a genuine re-link (switching Patreon accounts, or
        # re-checking a tier that changed on Patreon's side) is unaffected.
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

        view = StaticView("Your Active Perks", field_groups=_perk_sections(tier_rank, owned_gated_items))
        await ctx.respond(view=view, files=view.files, ephemeral=True)

    async def _send_welcome(self, discord_id: int) -> bool:
        """DMs the welcome card for whatever tier the user currently resolves to.
        Returns False if the DM couldn't be delivered (DMs closed), so callers that
        have somewhere else to put it can fall back instead of silently dropping it."""
        async with async_session() as session:
            tier_rank = await get_tier_rank(session, discord_id)
            owned_gated_items = await _owned_gated_items(session, discord_id)

        view = build_welcome_view(tier_rank, owned_gated_items)
        try:
            user = await self.bot.fetch_user(discord_id)
            await user.send(view=view, files=view.files)
            return True
        except discord.HTTPException:
            return False

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
