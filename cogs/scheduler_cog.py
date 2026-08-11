import logging

import discord
from discord.ext import commands, tasks

from db.base import async_session
from services.apartment_service import TICK_INTERVAL_MINUTES, process_due_rents

log = logging.getLogger("spidey")


class SchedulerCog(commands.Cog):
    """Runs background ticks — right now just weekly rent auto-debits.
    Anything time-based and not player-triggered belongs here."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.rent_tick.start()

    def cog_unload(self):
        self.rent_tick.cancel()

    @tasks.loop(minutes=TICK_INTERVAL_MINUTES)
    async def rent_tick(self):
        async with async_session() as session:
            results = await process_due_rents(session)
        if results:
            paid = sum(1 for r in results if r.paid)
            log.info("Rent tick: %d due, %d paid, %d missed", len(results), paid, len(results) - paid)

    @rent_tick.before_loop
    async def before_rent_tick(self):
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot):
    bot.add_cog(SchedulerCog(bot))
