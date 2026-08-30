"""Fires a short welcome nudge the first time a user ever runs any command —
registered once as a global bot.before_invoke hook (see bot.py), so no individual
cog needs to know about this. Never blocks or replaces the command the user
actually ran; it's a bonus message sent alongside it.

pycord only supports one before_invoke slot, so this is also where every other
"run on every command" need rides along rather than adding a second hook:

- the global "last active" stamp (see User.last_active_at) that Stealth Mode's
  inactivity window reads from;
- resolving the invoker's Patreon tier accent for this command (see
  utils/tier_accent.py) so every Components V2 container can pick it up without
  being passed a colour by hand;
- handing over the community server's Level 10 Bronze camera (see
  services/shop_service.grant_bronze_camera). It lives here because the owner ruled out
  any command that makes the player claim or choose a perk, so the grant has to happen
  wherever they already are."""

import datetime

import discord

from db.base import async_session
from db.models import User
from services.patreon_service import accent_for_rank, get_tier_rank
from services.server_perks import perks_from
from services.shop_service import grant_bronze_camera
from utils.embeds import SPIDEY_BLUE, base_embed
from utils.tier_accent import set_current_accent

# /start already gives the full welcome experience on its own — firing this too
# would just be a redundant second welcome message for the same interaction.
FIRST_RUN_SKIP_COMMANDS = {"start"}

# How stale the last-active stamp has to be before it's worth a write.
#
# This hook runs on EVERY command, so without this it issued one UPDATE + COMMIT per
# command per user — the bot's single largest source of write traffic, and the reason
# ordinary play generated a constant stream of writers competing for SQLite's one write
# lock. With it, a user issuing commands faster than once a minute costs a read and
# nothing else.
#
# 60 seconds is safe because there is exactly one consumer of this column:
# shakedown_service.target_idle_seconds, feeding Stealth Mode's
# STEALTH_MODE_INACTIVITY_THRESHOLD_SECONDS window of 20 minutes (§9.1.2). Granularity of
# a minute against a threshold of twenty is at most a 5% error on the one comparison that
# reads it, and it errs toward reporting a player as MORE idle than they are — i.e.
# toward firing the perk the subscriber paid for. If another consumer ever needs a
# precise stamp, give it its own column rather than lowering this.
LAST_ACTIVE_WRITE_INTERVAL = datetime.timedelta(seconds=60)


async def announce_if_first_time(ctx: discord.ApplicationContext) -> None:
    grant_note: str | None = None

    async with async_session() as session:
        # Before any early return below, and on the session that was being opened
        # anyway: the accent has to be set for /start (which skips the rest of this
        # function) and for a brand-new user's very first command alike, or those are
        # the two places a subscriber's colour would be missing.
        set_current_accent(accent_for_rank(await get_tier_rank(session, ctx.author.id)))

        if ctx.command.qualified_name in FIRST_RUN_SKIP_COMMANDS:
            return

        existing = await session.get(User, ctx.author.id)
        # No profile row yet means there's nothing to hang an inventory row off, so the
        # Bronze grant is skipped rather than attempted — get_or_create_user runs inside
        # the command that's about to execute, and a brand-new Level 10 member picks the
        # camera up on their next command instead. That branch falls straight through to
        # the welcome message, which is what it's actually for.
        if existing is None:
            embed = base_embed(
                "Friendly Neighborhood Welcome",
                "First time swinging out? Run **/start** for a quick intro, or **/help** "
                "for the full rundown.",
                colour=SPIDEY_BLUE,
            )
            try:
                await ctx.channel.send(content=ctx.author.mention, embed=embed)
            except discord.HTTPException:
                pass
            return

        now = datetime.datetime.utcnow()
        if (
            existing.last_active_at is None
            or now - existing.last_active_at >= LAST_ACTIVE_WRITE_INTERVAL
        ):
            existing.last_active_at = now
            # Deliberately inside the stale branch, sharing the stamp's throttle rather
            # than running on every single command. perks_from is a pure read of the
            # interaction payload, but grant_bronze_camera has to query the equipped
            # camera to know it has nothing to do — and for the players this applies to
            # (max level in the home server, i.e. the most active ones) that would be an
            # extra SELECT per command forever. The grant is idempotent and nothing about
            # it is time-critical, so "at most once a minute" is indistinguishable from
            # "immediately".
            grant_note = await grant_bronze_camera(session, ctx.author.id, perks_from(ctx))
            await session.commit()

    # Outside the session: this is a Discord round-trip, and holding SQLite's write lock
    # across one is what the last-active throttle above exists to avoid.
    if grant_note is not None:
        try:
            await ctx.channel.send(content=f"{ctx.author.mention} {grant_note}")
        except discord.HTTPException:
            pass
