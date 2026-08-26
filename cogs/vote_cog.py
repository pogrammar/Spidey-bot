import logging

import discord
from aiohttp import web
from discord.ext import commands

from db.base import async_session
from services import topgg_service
from utils import webapp
from utils.icons import emoji

log = logging.getLogger("spidey")


async def _topgg_vote_handler(request: web.Request) -> web.Response:
    """Receives a POST from top.gg whenever someone votes.

    top.gg retries on 5xx. It treats 4xx as "broken request, stop retrying" and 2xx as
    "received, don't retry". Those semantics are load-bearing: never return 5xx for a
    business-rule rejection (duplicate, etc.), or top.gg will replay it indefinitely.
    """
    bot: discord.Bot = request.app["bot"]

    # An unset secret is a config problem on our side, not a bad request from theirs.
    if not topgg_service.is_configured():
        log.warning("top.gg vote received but TOPGG_WEBHOOK_SECRET is not set — ignoring")
        return web.Response(status=503, text="webhook not configured")

    if not topgg_service.check_auth(request.headers.get("Authorization")):
        return web.Response(status=401, text="unauthorized")

    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid JSON")

    # top.gg's "Test" button sends a real-looking body with type="test". Acknowledge it
    # 200 so the config page shows success, but never credit it.
    if topgg_service.is_test_payload(payload):
        log.info("top.gg test webhook received (not credited)")
        return web.Response(status=200, text="test acknowledged")

    try:
        user_id = topgg_service.parse_user_id(payload)
    except topgg_service.VoteRejected as exc:
        return web.Response(status=exc.status, text=exc.message)

    try:
        async with async_session() as session:
            balance = await topgg_service.credit_vote(session, user_id)
    except topgg_service.VoteRejected as exc:
        # Log duplicates at debug — they're normal near the 12-hour window boundary.
        log.debug("top.gg vote rejected for user %s: %s", user_id, exc.message)
        return web.Response(status=exc.status, text=exc.message)
    except Exception:
        log.exception("Unexpected error crediting top.gg vote for user %s", user_id)
        return web.Response(status=500, text="internal error")

    # DM the voter — there's no interaction object here, so a direct message is the
    # only way to surface the result. Failure is logged and swallowed: the vote was
    # already credited and the webhook must answer 200.
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        vote_e = emoji("attack") or "🕷️"
        await user.send(
            f"{vote_e} Thanks for the vote! "
            f"**${topgg_service.VOTE_REWARD:,}** has landed in your wallet. "
            f"You can vote again in 12 hours."
        )
    except Exception:
        log.warning("Couldn't DM vote reward to user %s", user_id, exc_info=True)

    return web.Response(status=200, text="ok")


class VoteCog(commands.Cog):
    """Handles top.gg upvote webhooks and exposes a /vote slash command."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        # Route registered here (synchronously, before the server starts) so it's live
        # as soon as the server binds — same pattern as health_cog and patreon_cog.
        webapp.app.router.add_post("/topgg/vote", _topgg_vote_handler)

    @discord.slash_command(name="vote", description="Vote for the bot on top.gg — get $1,000 instantly.")
    async def vote(self, ctx: discord.ApplicationContext):
        vote_e = emoji("attack") or "🕷️"
        reward_line = (
            f"Vote once every 12 hours. Each vote drops "
            f"**${topgg_service.VOTE_REWARD:,}** straight into your wallet."
        )
        if topgg_service.is_configured():
            msg = (
                f"{vote_e} **Vote for the bot and get paid.**\n\n"
                f"{reward_line}\n\n"
                f"[Vote on top.gg]({topgg_service.TOPGG_VOTE_URL})"
            )
        else:
            # Webhook isn't wired yet — don't promise instant cash we can't deliver.
            msg = (
                f"{vote_e} **Vote for the bot on top.gg.**\n\n"
                f"[Vote on top.gg]({topgg_service.TOPGG_VOTE_URL})\n\n"
                f"-# Automated vote rewards aren't live yet — they're coming soon."
            )
        await ctx.respond(msg, ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(VoteCog(bot))
