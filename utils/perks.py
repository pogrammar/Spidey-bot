from __future__ import annotations

import discord

import config


async def is_server_booster(bot: discord.Bot, discord_id: int) -> bool:
    """True if the given user is currently boosting our official Discord server
    (config.PERK_GUILD_ID) — the only server booster-gated perks ever check,
    regardless of which server the command was actually run from.

    Prefers the gateway's member cache (populated by the Members privileged
    intent — see bot.py), which is what makes it possible to react to a boost
    starting or lapsing after the fact rather than only at the moment a command
    runs. Falls back to a direct API fetch if the cache hasn't seen this member
    yet, so a cold cache doesn't produce a false negative.

    False (never an error) if the perk guild isn't configured, the bot isn't in
    it, or the user isn't a member there."""
    if config.PERK_GUILD_ID is None:
        return False

    guild = bot.get_guild(config.PERK_GUILD_ID)
    if guild is None:
        return False

    member = guild.get_member(discord_id)
    if member is None:
        try:
            member = await guild.fetch_member(discord_id)
        except discord.HTTPException:
            return False

    return member.premium_since is not None
