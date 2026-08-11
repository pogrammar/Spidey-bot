import logging

import aiohttp
import discord
from discord.ext import commands, tasks

import config

log = logging.getLogger("spidey")

# Comfortably shorter than any reasonable UptimeRobot heartbeat "Period" setting,
# so a single slow tick never falsely trips the monitor.
HEARTBEAT_INTERVAL_SECONDS = 60


class HeartbeatCog(commands.Cog):
    """Pings an UptimeRobot heartbeat monitor on an interval so it can tell the bot
    process is alive. Entirely inert if UPTIME_ROBOT_PUSH_URL isn't configured."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None
        if config.UPTIME_ROBOT_PUSH_URL:
            self.send_heartbeat.start()

    def cog_unload(self):
        self.send_heartbeat.cancel()

    @tasks.loop(seconds=HEARTBEAT_INTERVAL_SECONDS)
    async def send_heartbeat(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.get(
                config.UPTIME_ROBOT_PUSH_URL, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    log.warning("Heartbeat ping returned status %s", resp.status)
        except aiohttp.ClientError as exc:
            log.warning("Heartbeat ping failed: %s", exc)

    @send_heartbeat.before_loop
    async def before_send_heartbeat(self):
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot):
    bot.add_cog(HeartbeatCog(bot))
