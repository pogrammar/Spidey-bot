import logging

import discord
from aiohttp import web
from discord.ext import commands

from db.base import async_session
from services.patreon_service import PatreonLinkError, build_authorize_url, handle_callback
from utils import webapp

log = logging.getLogger("spidey")

CALLBACK_SUCCESS_HTML = """<!doctype html><html><body style="font-family:sans-serif;text-align:center;padding:4rem;">
<h2>You're linked!</h2><p>Head back to Discord — you're all set.</p></body></html>"""

CALLBACK_ERROR_HTML = """<!doctype html><html><body style="font-family:sans-serif;text-align:center;padding:4rem;">
<h2>Something went wrong</h2><p>{message}</p></body></html>"""


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

        await ctx.respond(
            "Click below to connect your Patreon account. This link is single-use and expires in 10 minutes.",
            view=LinkButtonView(url),
            ephemeral=True,
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

        try:
            user = await self.bot.fetch_user(discord_id)
            tier_line = f"You're linked as a **{tier}** supporter." if tier else "You're linked, but don't have an active pledge right now."
            await user.send(f"✅ Patreon connected. {tier_line}")
        except discord.HTTPException:
            pass  # DMs closed or similar — the web page confirmation is enough either way

        return web.Response(text=CALLBACK_SUCCESS_HTML, content_type="text/html")


def setup(bot: discord.Bot):
    bot.add_cog(PatreonCog(bot))
