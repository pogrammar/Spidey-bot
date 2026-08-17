import discord
from aiohttp import web
from discord.ext import commands

from utils import webapp


async def _health(request: web.Request) -> web.Response:
    bot: discord.Bot = request.app["bot"]
    if bot.is_ready():
        return web.Response(text="OK")
    return web.Response(text="NOT READY", status=503)


class HealthCog(commands.Cog):
    """A single /health route on the shared web app (see utils/webapp.py) — this
    exists purely so UptimeRobot's free HTTP monitor has something to poll. Returns
    503 if the process is up but not yet connected to Discord, so a monitor can tell
    "alive" apart from "actually working"."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        webapp.app["bot"] = bot
        webapp.app.router.add_get("/health", _health)


def setup(bot: discord.Bot):
    bot.add_cog(HealthCog(bot))
