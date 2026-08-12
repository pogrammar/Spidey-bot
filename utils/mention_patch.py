"""Patches ApplicationContext.respond to prepend the invoking user's @mention to
every slash command response. Applied once, globally (see apply(), called from
bot.py before the bot connects), so every cog's existing `ctx.respond(embed=...)`
call picks this up automatically — no per-command changes needed.

Skipped for ephemeral responses: mentioning someone in a message only they can see
is pointless, and self-mentions don't trigger a notification anyway.

Also skipped for Components V2 views (see cogs.v2_demo_cog): Discord rejects any
`content` at all alongside a V2 component tree — it must be fully self-contained."""

import discord

_original_respond = discord.ApplicationContext.respond


def _is_components_v2(kwargs: dict, args: tuple) -> bool:
    for value in (*args, *kwargs.values()):
        check = getattr(value, "is_components_v2", None)
        if callable(check) and check():
            return True
    return False


async def _respond_with_mention(self: discord.ApplicationContext, *args, **kwargs):
    if kwargs.get("ephemeral") or _is_components_v2(kwargs, args):
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
