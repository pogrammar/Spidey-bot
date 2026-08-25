"""/invite — the "add this bot to your server" card.

Its own cog rather than a topic in help_cog, because help_cog answers questions for
people who already have the bot and this is the one command aimed at a server that
doesn't. Nothing here touches the database or the player's state: the card is identical
for everyone, so there's nothing to look up.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from utils.links import BOT_INVITE_URL
from utils.v2_embeds import static_container

INVITE_INTRO = (
    "The whole game — daily cash, patrols that turn into real fights, and rent that "
    "never stops coming — wherever you want it."
)

# One line each. Same rule as /patreon subscribe: this is read by somebody deciding
# whether to click, and a paragraph is a thing they bounce off.
INVITE_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Setup", [("", "None. Add it, run `/start`, and you're Peter Parker.")]),
    (
        "Your Save Travels",
        # True by construction, not marketing: every table in db/models.py is keyed by
        # Discord user ID and not one of them carries a guild ID, so a profile genuinely
        # is global. If a guild-scoped table ever lands, re-read this line before shipping
        # it — it would stop being true.
        [("", "One profile per person, not per server. Cash, gear and streak follow you.")],
    ),
]

INVITE_FOOTER_LINES = [
    "Asks for no special permissions — it uses whatever the channel already allows.",
    "`/help` has the full rundown once it's in.",
]


class InviteView(discord.ui.DesignerView):
    """The invite card with the link button inside the container.

    Bespoke rather than a StaticView for the same reason patreon_cog's SubscribeView is:
    StaticView is explicitly the no-buttons case, and a button added outside the container
    renders detached from the card it belongs to instead of as its call to action.

    timeout=None — a link button has no callback for a timeout to protect, and this
    message is never edited afterwards.
    """

    def __init__(self):
        super().__init__(timeout=None)
        container, file = static_container(
            "Bring Him to Your Server",
            description=INVITE_INTRO,
            field_groups=INVITE_SECTIONS,
            icon_key="web_shooters",
        )
        container.add_separator()
        container.add_text("\n".join(f"-# {line}" for line in INVITE_FOOTER_LINES))
        container.add_separator()
        container.add_row(
            discord.ui.Button(
                label="Add to Your Server", style=discord.ButtonStyle.link, url=BOT_INVITE_URL
            )
        )
        self.add_item(container)
        self.files: list[discord.File] = [file] if file else []


class InviteCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(
        name="invite",
        description="Add this bot to your own server.",
        # No `contexts=` on purpose. pycord's default already serialises to
        # [BOT_DM, PRIVATE_CHANNEL, GUILD], so declaring {guild, bot_dm} bought nothing
        # and only dropped PRIVATE_CHANNEL — while making this the one command in the
        # project sending a non-default value. Omitting it matches every other cog.
        #
        # Worth being precise about what this did NOT cause: guild is 0 and the old
        # payload was [1, 0], so guilds were always included. When this first shipped it
        # appeared in DMs only, and the explicit contexts looked like the culprit — it
        # wasn't. A newly-registered *global* command is live in DMs immediately while
        # each guild's command list catches up separately, so DM-first is the expected
        # shape of a fresh global sync, not a misdeclaration. /start carries the same
        # redundant kwarg and is equally unaffected, which is why it's left alone.
    )
    async def invite(self, ctx: discord.ApplicationContext):
        # Public, not ephemeral — matching /patreon subscribe. One person asking is how
        # everyone else in the channel finds out the link exists, and the card contains
        # nothing private. The tier accent comes from the ambient context for free.
        view = InviteView()
        await ctx.respond(view=view, files=view.files)


def setup(bot: discord.Bot):
    bot.add_cog(InviteCog(bot))
