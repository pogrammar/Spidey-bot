"""Sends the occasional house promo alongside a command the player actually ran —
see services/ads_service.py for the copy and the rotation that keeps the Discord
server and the Patreon page exactly balanced.

Why on_application_command_completion rather than a bot.after_invoke hook, which is
the obvious place for "after every command":

- after_invoke fires from inside a `finally:` block (discord/commands/core.py's
  call_after_hooks), so it runs when the command *raised* too. A promo card stapled
  under an error message is the worst possible placement for one.
- pycord has exactly one after_invoke slot, the same constraint that has the
  last-active stamp and the tier accent sharing before_invoke (see utils/first_run.py).
  Spending it on ads would make the next global hook someone needs a refactor.

The completion event is dispatched only from the `else:` branch of that try/except
(discord/bot.py's invoke_application_command), so it means "the command succeeded",
and being an event it can have any number of listeners.

Two throttles gate the send, and they answer different questions — see
services/ads_service.py. The per-user one keeps any one player from being nagged; the
per-channel one keeps the channel itself readable, which is the one that matters as the
bot grows, since without it a room's promo rate rises with its headcount.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from db.base import async_session
from services import ads_service
from utils import icons
from utils.v2_embeds import make_container


class PromoView(discord.ui.DesignerView):
    """One promo card: headline, one-liner, and the link button inside the container
    so it reads as part of the card rather than floating under it (same reason
    patreon_cog's SubscribeView is bespoke).

    Built by hand instead of via StaticView because StaticView is explicitly the
    no-buttons case. The container still comes from make_container(), so a
    subscriber's accent bar shows up here like it does everywhere else.

    timeout=None: a link button has no callback, so there's nothing for a timeout to
    protect — and this message is never edited afterwards.
    """

    def __init__(self, destination: str, promo: ads_service.Promo):
        super().__init__(timeout=None)
        label, url = ads_service.button_for(destination)

        emoji = icons.emoji(ads_service.icon_key_for(destination))
        headline = f"{emoji} {promo.headline}" if emoji else promo.headline

        container = make_container()
        container.add_text(f"**{headline}**\n{promo.body}")
        container.add_separator()
        container.add_row(discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url))
        self.add_item(container)


class AdsCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_application_command_completion(self, ctx: discord.ApplicationContext) -> None:
        if ctx.command is None or not ads_service.should_consider(ctx.command.qualified_name):
            return
        # An interaction whose channel didn't resolve has nowhere to put this. Checked
        # before the throttle is claimed so an unsendable channel doesn't quietly eat
        # someone's promo slot, and separately from the HTTPException below because
        # AttributeError on None wouldn't be caught by it.
        if ctx.channel is None:
            return
        # Before the database work: it's free, and a channel that's inside its own window
        # shouldn't spend the user's place in the rotation. In a busy channel this is
        # where the overwhelming majority of commands stop.
        if not ads_service.channel_ready(ctx.channel.id):
            return

        async with async_session() as session:
            claimed = await ads_service.claim_promo(session, ctx.author.id)
        if claimed is None:
            return
        ads_service.mark_channel(ctx.channel.id)

        destination, promo = claimed
        # A separate channel message, not a follow-up on the interaction: the command's
        # own response is already using that, and a Components V2 message can't carry
        # extra content or an embed alongside the view anyway (utils/mention_patch.py).
        #
        # Swallowing HTTPException covers the ordinary case of the bot lacking Send
        # Messages in the channel it was just used in. The player's command has already
        # succeeded and been answered by this point, so a promo that can't be delivered
        # must not surface as an error against it.
        try:
            await ctx.channel.send(view=PromoView(destination, promo))
        except discord.HTTPException:
            pass


def setup(bot: discord.Bot):
    bot.add_cog(AdsCog(bot))
