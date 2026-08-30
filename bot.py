import asyncio
import logging
from pathlib import Path
from itertools import cycle

import discord
from discord.ext import commands, tasks
from alembic import command
from alembic.config import Config as AlembicConfig

import config
from db.base import async_session
from db.seed import seed_items
from utils import webapp
from utils.first_run import announce_if_first_time
from utils.mention_patch import apply as apply_mention_patch

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("spidey")

intents = discord.Intents.default()
bot = discord.Bot(
    intents=intents,
    debug_guilds=[config.DEV_GUILD_ID] if config.DEV_GUILD_ID else None,
)
apply_mention_patch()
bot.before_invoke(announce_if_first_time)

EXTENSIONS = [
    "cogs.economy_cog",
    "cogs.daily_cog",
    "cogs.patrol_cog",
    "cogs.bugle_cog",
    "cogs.tutoring_cog",
    "cogs.apartment_cog",
    "cogs.suit_cog",
    "cogs.pvp_cog",
    "cogs.shop_cog",
    "cogs.gadget_cog",
    "cogs.market_cog",
    "cogs.ally_cog",
    "cogs.lab_cog",
    "cogs.leaderboard_cog",
    "cogs.scheduler_cog",
    "cogs.help_cog",
    "cogs.admin_cog",
    "cogs.patreon_cog",
    "cogs.perks_cog",
    "cogs.heartbeat_cog",
    "cogs.health_cog",
    "cogs.tunnel_cog",
    "cogs.ads_cog",
    "cogs.invite_cog",
]

STREAM_URL = "https://www.twitch.tv/betchespy"

# MUST be online. A Streaming activity only gets the purple "live" treatment on a
# presence whose status is online — set idle or dnd and Discord's client renders the
# ordinary idle/dnd dot and drops the streaming badge, so the activity is still
# there in the payload and still invisible where anyone would look for it. This was
# discord.Status.idle until 2026-08-25, which is most of why the bot read as a plain
# non-streaming presence. Don't "soften" it back to idle.
PRESENCE_STATUS = discord.Status.online

STATUS_LINES = cycle([
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
])

@tasks.loop(seconds=60) # Change status every 60 seconds
async def change_status():
    current_stream = next(STATUS_LINES)
    activity = discord.Streaming(name=current_stream, url=STREAM_URL)
    await bot.change_presence(activity=activity)

@bot.event
async def on_ready():
    log.info("Logged in as %s (id=%s). [deploy test v1]", bot.user, bot.user.id)
    change_status.start()


def run_migrations() -> None:
    """Brings the DB schema up to the latest Alembic revision. Runs synchronously,
    before the event loop starts — Alembic's async template does its own
    asyncio.run() internally, which can't nest inside one we're already in."""
    cfg = AlembicConfig(str(Path(__file__).resolve().parent / "alembic.ini"))
    command.upgrade(cfg, "head")


async def main():
    # pycord has no setup_hook (that's a discord.py-2.0-only addition), so the only
    # race-free way to guarantee the DB exists before any cog's background task can
    # touch it (see cogs.scheduler_cog) is to finish this *before* the bot connects
    # at all — not inside on_ready, which fires around the same time other
    # READY-triggered code (like a task loop's wait_until_ready) does.
    async with async_session() as session:
        await seed_items(session)
    log.info("Database ready.")

    for extension in EXTENSIONS:
        bot.load_extension(extension)

    # After every extension has loaded (so every cog's routes are already
    # registered onto it — see utils/webapp.py) but before the bot actually
    # connects, so /health can answer "not ready yet" instead of 404ing.
    await webapp.start()

    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    run_migrations()
    asyncio.run(main())
