import asyncio
import logging
from pathlib import Path

import discord
from alembic import command
from alembic.config import Config as AlembicConfig

import config
from db.base import async_session
from db.seed import seed_items
from utils.first_run import announce_if_first_time
from utils.mention_patch import apply as apply_mention_patch

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("spidey")

intents = discord.Intents.default()
bot = discord.Bot(intents=intents, debug_guilds=[config.DEV_GUILD_ID] if config.DEV_GUILD_ID else None)
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
    "cogs.status_cog",
    "cogs.heartbeat_cog",
    "cogs.health_cog",
    "cogs.v2_demo_cog",  # TEMPORARY — remove once done comparing Components V2
]


@bot.event
async def on_ready():
    log.info("Logged in as %s (id=%s). [deploy test v1]", bot.user, bot.user.id)


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

    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    run_migrations()
    asyncio.run(main())
