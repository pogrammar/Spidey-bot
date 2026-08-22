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

# Plain-English perk summaries for /patreon perks — kept separate from the welcome
# DM copy above since this needs to read as a checklist, not prose.
ARACHNID_PERK_LINES = [
    "Organic Webbing — patrols never touch web-fluid vials or the no-fluid cash tax.",
    "Enhanced Strength — +30% Attack damage on crime-tier patrols.",
    "Combat-Ready Patrols — better odds of landing a crime encounter.",
    "Drawback: ally happiness decays 30% faster.",
]
SYMBIOTE_PERK_LINES = [
    "Venom Blast — the hit that would end a boss fight is absorbed and countered instead (once per fight).",
    "Biomorphic Webbing — extra chance at bonus cash, a bonus component, and a bonus photo.",
    "Stealth Mode — full /shakedown immunity while you've been inactive 20+ minutes.",
    "Drawback: Sonic Dampener — +30% incoming damage against the Shocker.",
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

# Sent as the post-link DM — same "Bond with the [Tier]" voice as the tier
# descriptions, but doing a real job: telling a brand-new subscriber what
# actually changed and what to do next, not just confirming a connection.
ARACHNID_WELCOME = (
    "🕷️ **Bond with the Arachnid Spider — and it's already working.**\n\n"
    "Organic Webbing, Enhanced Strength, Electric Webbing, Spider Bots, and more combat-favoring "
    "patrols are all live for you right now — nothing to set up.\n\n"
    "One real thing to know: the bond means your allies keep a closer eye on you now — happiness "
    "decays a bit faster, so you'll want to visit a little more often than before.\n\n"
    "Run `/patreon status` whenever you want to double-check what's active."
)
SYMBIOTE_WELCOME = (
    "🕷️ **Bond with the Symbiote Spider — you're all the way in.**\n\n"
    "Everything Arachnid gets, plus Venom Blast, Stealth Mode, and Biomorphic Webbing — all live for "
    "you right now. One thing worth knowing: the bond's watchful, but not invincible. It doesn't like "
    "the Shocker much.\n\n"
    "Run `/patreon status` whenever you want to double-check what's active."
)
NO_PLEDGE_WELCOME = (
    "✅ Patreon connected. You don't have an active pledge right now, so no perks are active yet — "
    "they'll kick in automatically the moment that changes, no need to re-link."
)


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
        try:
            url = build_authorize_url(ctx.author.id)
        except PatreonLinkError as exc:
            await ctx.respond(str(exc), ephemeral=True)
            return

        message = "Click below to connect your Patreon account. This link is single-use and expires in 10 minutes."
        view = LinkButtonView(url)

        if ctx.guild is None:
            # Already in DMs — just answer directly, no need to DM on top of a DM.
            await ctx.respond(message, view=view, ephemeral=True)
            return

        try:
            await ctx.author.send(message, view=view, ephemeral=True)
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
            stmt = select(InventoryItem.item_key).where(
                InventoryItem.user_id == ctx.author.id,
                InventoryItem.item_key.in_(ARACHNID_GATED_ITEM_KEYS),
            )
            owned_gated_items = set((await session.execute(stmt)).scalars())

        e = emoji("arachnid") or ""
        field_groups = []

        if tier_rank == TIER_RANK_NONE:
            field_groups.append((
                None,
                [("No active perks", "Subscribe at Arachnid or Symbiote and run /patreon link to connect.")],
            ))
        else:
            field_groups.append((
                f"{e} Arachnid".strip(),
                [("", "\n".join(f"• {line}" for line in ARACHNID_PERK_LINES))],
            ))
            if tier_rank >= TIER_RANK_SYMBIOTE:
                field_groups.append((
                    f"{e} Symbiote".strip(),
                    [("", "\n".join(f"• {line}" for line in SYMBIOTE_PERK_LINES))],
                ))

            gadget_lines = [
                f"• {item_label(key, label)} — {'Owned' if key in owned_gated_items else 'Not owned (see /shop browse)'}"
                for key, label in GATED_ITEM_LABELS.items()
            ]
            field_groups.append((f"{e} Arachnid+ Exclusive Gear".strip(), [("", "\n".join(gadget_lines))]))

        view = StaticView("Your Active Perks", field_groups=field_groups)
        await ctx.respond(view=view, files=view.files, ephemeral=True)

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
        if tier_rank == TIER_RANK_SYMBIOTE:
            welcome = SYMBIOTE_WELCOME
        elif tier_rank == TIER_RANK_ARACHNID:
            welcome = ARACHNID_WELCOME
        else:
            welcome = NO_PLEDGE_WELCOME

        try:
            user = await self.bot.fetch_user(discord_id)
            await user.send(welcome)
        except discord.HTTPException:
            pass  # DMs closed or similar — the web page confirmation is enough either way

        return web.Response(text=CALLBACK_SUCCESS_HTML, content_type="text/html")


def setup(bot: discord.Bot):
    bot.add_cog(PatreonCog(bot))
