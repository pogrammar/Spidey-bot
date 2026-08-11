import itertools

import discord
from discord.ext import commands, tasks

STATUS_ROTATION_SECONDS = 60

# (activity, ) tuples — mixing types the way Dank Memer does (its own status sits on
# Streaming) so the presence doesn't read as one flat "Playing X" line the whole time.
STATUSES = [
    discord.Streaming(name="/help — Peter", url="https://www.twitch.tv/spidey"),
    discord.Activity(type=discord.ActivityType.watching, name="the police scanner"),
    discord.Activity(type=discord.ActivityType.playing, name="/patrol"),
    discord.Activity(type=discord.ActivityType.listening, name="J. Jonah Jameson yell"),
    discord.Activity(type=discord.ActivityType.competing, name="the Bugle's front page"),
    discord.Activity(type=discord.ActivityType.watching, name="your suit integrity drop"),
]


class StatusCog(commands.Cog):
    """Rotates the bot's presence through a themed status list, Dank Memer-style,
    instead of sitting on one static 'Playing' line forever."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._cycle = itertools.cycle(STATUSES)
        self.rotate_status.start()

    def cog_unload(self):
        self.rotate_status.cancel()

    @tasks.loop(seconds=STATUS_ROTATION_SECONDS)
    async def rotate_status(self):
        await self.bot.change_presence(activity=next(self._cycle))

    @rotate_status.before_loop
    async def before_rotate_status(self):
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot):
    bot.add_cog(StatusCog(bot))
