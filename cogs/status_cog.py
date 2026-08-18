import itertools
import logging
import random

import discord
from discord.ext import commands, tasks

log = logging.getLogger("spidey")

STATUS_ROTATION_SECONDS = 60

# Streaming, not Watching/Listening/Competing — confirmed by live testing that those
# newer Rich Presence types only render for bots inside the expanded profile view,
# not inline in the member list. Streaming (like Playing) is one of the original
# activity types and gets the purple "live" badge, which reads as a lot less classic
# than a plain online dot. Needs a syntactically valid stream URL or Discord won't
# apply the streaming treatment, even though nothing's actually being streamed.
STREAM_URL = "https://www.twitch.tv/betchespy"

STATUS_LINES = [
    "hooky from Oscorp",
    "tag with the NYPD",
    "hide and seek with the Sinister Six",
    "chicken with a glider",
    "catch with a city bus",
    "20 questions with J. Jonah Jameson",
    "keep-away with Doc Ock's arms",
    "Jenga with the Chrysler Building",
    "peekaboo with the Vulture",
    "dodgeball with pumpkin bombs",
    "tug-of-war with a web line",
    "Uno with Deadpool (he's cheating)",
    "hopscotch on bridge cables",
    "whack-a-mole with Kingpin's goons",
    "connect four with Electro",
    "freeze tag with Mysterio's illusions",
    "hide and seek (Kraven is it)",
    "darts with Shocker (badly)",
    "the quiet game with Venom",
    "arm wrestling with Rhino (losing)",
    "leapfrog over the Daily Bugle",
    "chicken with rent day",
    "budget hero on a sidekick's salary",
    "landlord roulette",
    "hooky from chemistry class",
    "guess the villain from the headline",
    "web fluid roulette (usually fine)",
    "dress-up in a homemade suit",
    "hide the black eye from Aunt May",
    "phone tag with MJ",
    "tourist photographer, technically",
    "damage control, mostly on myself",
    "extremely unpaid overtime",
    "hooky from my responsibilities",
    "chicken with a deadline and a villain",
    "catch-up on 4 hours of sleep",
    "keep the mask on straight",
    "find the exact rent amount",
    "good cop bad cop, badly, alone",
    "tag, you're mugged",
    "the world's worst internship",
    "wall crawler, occasionally falling",
    "hero for a city that reads the Bugle",
    "dodge the wanted poster",
    "who left this pumpkin bomb here",
    "spot the difference, villain edition",
    "web-slinger's cardio, unwillingly",
]


class StatusCog(commands.Cog):
    """Rotates the bot's presence through a big themed status list. Shuffled once at
    startup so restarts don't always begin on the same line."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        shuffled = STATUS_LINES.copy()
        random.shuffle(shuffled)
        self._cycle = itertools.cycle(shuffled)
        self.rotate_status.start()

    def cog_unload(self):
        self.rotate_status.cancel()

    @tasks.loop(seconds=STATUS_ROTATION_SECONDS)
    async def rotate_status(self):
        # tasks.loop kills itself for good after any single uncaught exception —
        # confirmed live (status worked, then silently died and never came back).
        # One bad change_presence call should cost this cycle's update, not every
        # future one, so failures get logged and swallowed instead of propagating.
        try:
            await self.bot.change_presence(
                status=discord.Status.idle,
                activity=discord.Streaming(name=next(self._cycle), url=STREAM_URL),
            )
        except discord.HTTPException:
            log.warning("Status rotation failed this cycle", exc_info=True)

    @rotate_status.before_loop
    async def before_rotate_status(self):
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot):
    bot.add_cog(StatusCog(bot))
