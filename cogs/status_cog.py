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

# MUST be online. A Streaming activity only gets the purple "live" treatment on a
# presence whose status is online — set idle or dnd and Discord's client renders the
# ordinary idle/dnd dot and drops the streaming badge, so the activity is still
# there in the payload and still invisible where anyone would look for it. This was
# discord.Status.idle until 2026-08-25, which is most of why the bot read as a plain
# non-streaming presence. Don't "soften" it back to idle.
PRESENCE_STATUS = discord.Status.online

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


def initial_activity() -> discord.Streaming:
    """The presence handed to discord.Bot(...) in bot.py, so IDENTIFY itself declares a
    streaming presence.

    The rotation loop below cannot cover this on its own, for two separate reasons:

    * Until its first tick the bot is whatever Discord defaults an IDENTIFY with no
      presence to, which is plain online.
    * Discord resets a bot's presence to the IDENTIFY payload every time the gateway
      session is *invalidated* rather than resumed (which happens on its own schedule,
      not just on network trouble). Nothing raises when that happens, so the loop has
      no way to notice it needs to re-assert — it just keeps sleeping, and the bot sits
      plain-online until the next tick.

    So both paths set the same presence: this one at connect time, the loop from then on.
    """
    return discord.Streaming(name=random.choice(STATUS_LINES), url=STREAM_URL)


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
        #
        # Exception, not discord.HTTPException. change_presence never touches the REST API
        # at all — it goes Client.change_presence -> self.ws.change_presence ->
        # socket.send_str — so HTTPException is one of the few things it *cannot* raise,
        # and this handler caught nothing whatsoever for as long as it named that class.
        # The gateway errors it can actually raise are all OSError/ConnectionClosed
        # subclasses, which tasks.loop's own reconnect list already absorbs, so this is a
        # backstop for the genuinely unexpected rather than a fix for a known throw —
        # same reasoning as SchedulerCog.patreon_tick's (see GAME_DESIGN §9.1.1). The
        # point is that the set of things that can end this loop permanently is now empty,
        # instead of being "everything, because the one clause here matches nothing."
        try:
            await self.bot.change_presence(
                status=PRESENCE_STATUS,
                activity=discord.Streaming(name=next(self._cycle), url=STREAM_URL),
            )
        except Exception:
            log.warning("Status rotation failed this cycle", exc_info=True)

    @rotate_status.before_loop
    async def before_rotate_status(self):
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot):
    bot.add_cog(StatusCog(bot))
