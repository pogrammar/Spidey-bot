import logging

import discord
from discord.ext import commands, tasks

from db.base import async_session
from services.apartment_service import TICK_INTERVAL_MINUTES, process_due_rents
from services.patreon_service import (
    PATREON_TICK_INTERVAL_MINUTES,
    TIER_RANK_LABELS,
    refresh_stale_links,
    tier_rank_from_name,
)

log = logging.getLogger("spidey")


class SchedulerCog(commands.Cog):
    """Runs background ticks — weekly rent auto-debits, and re-checking Patreon tiers.
    Anything time-based and not player-triggered belongs here."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.rent_tick.start()
        self.patreon_tick.start()

    def cog_unload(self):
        self.rent_tick.cancel()
        self.patreon_tick.cancel()

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

    @tasks.loop(minutes=PATREON_TICK_INTERVAL_MINUTES)
    async def patreon_tick(self):
        """Re-reads a batch of Patreon tiers so a cancelled pledge actually loses its
        perks. Before this existed a tier was written once at link time and never looked
        at again, so lapsing kept everything forever.

        Wrapped in a bare except on purpose: tasks.loop kills itself permanently after a
        single uncaught exception (see the note in cogs/status_cog.py), and this loop
        going quietly dead is exactly the failure that reopens the hole it closes. The
        service already fails open per-link, so this is only here for the unexpected —
        a DB error, or something malformed getting past the service's own handling.
        """
        try:
            async with async_session() as session:
                results = await refresh_stale_links(session)
        except Exception:
            log.exception("Patreon tick failed; tiers left as they were, retrying next cycle")
            return

        if not results:
            return

        unreachable = [d for d, o in results if not o.reached_patreon]
        for discord_id, outcome in results:
            if not outcome.changed:
                continue
            # Logged per-user and at warning level when it costs someone perks: this is
            # the only record that a background job took away something a player paid
            # for, and it's the first thing worth checking if one of them asks why.
            before = TIER_RANK_LABELS[tier_rank_from_name(outcome.previous_tier)]
            after = TIER_RANK_LABELS[tier_rank_from_name(outcome.tier)]
            level = log.warning if outcome.rank_delta < 0 else log.info
            level(
                "Patreon tier changed: discord_id=%s %s -> %s (tier=%r%s)",
                discord_id, before, after, outcome.tier,
                ", link is dead — user must re-run /patreon link" if outcome.dead_link else "",
            )

        log.info(
            "Patreon tick: %d checked, %d changed, %d unreachable",
            len(results), sum(1 for _, o in results if o.changed), len(unreachable),
        )

    @patreon_tick.before_loop
    async def before_patreon_tick(self):
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot):
    bot.add_cog(SchedulerCog(bot))
