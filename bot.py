import asyncio
import logging

import discord

import config
from db.base import async_session, init_db
from db.seed import seed_items

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("spidey")

intents = discord.Intents.default()
bot = discord.Bot(intents=intents, debug_guilds=[config.DEV_GUILD_ID] if config.DEV_GUILD_ID else None)

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
    "cogs.scheduler_cog",
    "cogs.help_cog",
    "cogs.admin_cog",
    "cogs.status_cog",
]


@bot.event
async def on_ready():
    log.info("Logged in as %s (id=%s). [deploy test v1]", bot.user, bot.user.id)


async def main():
    # pycord has no setup_hook (that's a discord.py-2.0-only addition), so the only
    # race-free way to guarantee the DB exists before any cog's background task can
    # touch it (see cogs.scheduler_cog) is to finish this *before* the bot connects
    # at all — not inside on_ready, which fires around the same time other
    # READY-triggered code (like a task loop's wait_until_ready) does.
    await init_db()
    async with async_session() as session:
        await seed_items(session)
    log.info("Database ready.")

    for extension in EXTENSIONS:
        bot.load_extension(extension)

    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
