"""Patches ApplicationContext.respond to prepend the invoking user's @mention to
every slash command response. Applied once, globally (see apply(), called from
bot.py before the bot connects), so every cog's existing `ctx.respond(embed=...)`
call picks this up automatically — no per-command changes needed.

Skipped for ephemeral responses: mentioning someone in a message only they can see
is pointless, and self-mentions don't trigger a notification anyway."""

import discord

_original_respond = discord.ApplicationContext.respond


async def _respond_with_mention(self: discord.ApplicationContext, *args, **kwargs):
    if kwargs.get("ephemeral"):
        return await _original_respond(self, *args, **kwargs)

    mention = self.author.mention
    if args:
        content = args[0]
        args = (f"{mention} {content}" if content else mention,) + args[1:]
    else:
        existing = kwargs.get("content")
        kwargs["content"] = f"{mention} {existing}" if existing else mention

    return await _original_respond(self, *args, **kwargs)


def apply() -> None:
    discord.ApplicationContext.respond = _respond_with_mention
