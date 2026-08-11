import logging

import discord
from aiohttp import web
from discord.ext import commands

import config

log = logging.getLogger("spidey")


async def _health(request: web.Request) -> web.Response:
    bot: discord.Bot = request.app["bot"]
    if bot.is_ready():
        return web.Response(text="OK")
    return web.Response(text="NOT READY", status=503)


class HealthCog(commands.Cog):
    """Tiny HTTP server with a single /health endpoint — this bot has no other web
    server, this exists purely so UptimeRobot's free HTTP monitor has something to
    poll. Returns 503 if the process is up but not yet connected to Discord, so a
    monitor can tell "alive" apart from "actually working"."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.runner: web.AppRunner | None = None
        bot.loop.create_task(self._start_server())

    async def _start_server(self) -> None:
        app = web.Application()
        app["bot"] = self.bot
        app.router.add_get("/health", _health)
        self.runner = web.AppRunner(app)
        try:
            await self.runner.setup()
            site = web.TCPSite(self.runner, "0.0.0.0", config.HEALTH_PORT)
            await site.start()
            log.info("Health check server listening on 0.0.0.0:%s", config.HEALTH_PORT)
        except OSError as exc:
            # A monitoring convenience failing to bind should never take the bot's
            # actual Discord connection down with it.
            log.warning("Health check server failed to start on port %s: %s", config.HEALTH_PORT, exc)

    def cog_unload(self):
        if self.runner is not None:
            self.bot.loop.create_task(self.runner.cleanup())


def setup(bot: discord.Bot):
    bot.add_cog(HealthCog(bot))
