"""TEMPORARY demo cog — shows what Discord Components V2 looks like in practice.
Not part of the real game. Safe to delete this file and its EXTENSIONS entry in
bot.py once you've seen enough."""

import random

import discord
from discord.ext import commands

DEMO_FOOTERS = [
    "Peter Parker did not build this UI. A friendly neighborhood dev did.",
    "No radioactive spiders were involved in this component tree.",
    "Even Tony Stark reads the docs sometimes.",
    "This demo will self-destruct once you've seen enough. (It won't. Just delete the file.)",
]


class TryMeButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Try me", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("Buttons work as Section accessories too.", ephemeral=True)


class V2DemoView(discord.ui.DesignerView):
    def __init__(self, author_avatar_url: str):
        super().__init__(timeout=180)

        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("# Components V2 Demo"))
        container.add_item(
            discord.ui.TextDisplay(
                "This whole message is built from layout blocks instead of one fixed "
                "embed shape — headers, dividers, and images/buttons placed wherever "
                "you want them."
            )
        )
        container.add_item(discord.ui.Separator())

        image_section = discord.ui.Section(
            discord.ui.TextDisplay(
                "**Text next to an image**\nThis is a `Section` — text with a "
                "`Thumbnail` accessory sitting beside it. This is how Dank Memer's "
                "\"2X\" graphic sits next to its text instead of being squeezed into "
                "a tiny corner thumbnail."
            ),
            accessory=discord.ui.Thumbnail(url=author_avatar_url),
        )
        container.add_item(image_section)
        container.add_item(discord.ui.Separator())

        button_section = discord.ui.Section(
            discord.ui.TextDisplay(
                "**Text next to a button**\nA `Section`'s accessory can be a button "
                "instead of an image."
            ),
            accessory=TryMeButton(),
        )
        container.add_item(button_section)
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                f"-# {random.choice(DEMO_FOOTERS)}\n-# Temporary demo command — not part of the real game.\n-# discord.gg/spider-man"
            )
        )

        self.add_item(container)


class V2DemoCog(commands.Cog):
    """TEMPORARY — delete this cog once done comparing the look and feel."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="v2demo", description="TEMP: preview what Components V2 looks like.")
    async def v2demo(self, ctx: discord.ApplicationContext):
        view = V2DemoView(ctx.author.display_avatar.url)
        await ctx.respond(view=view)


def setup(bot: discord.Bot):
    bot.add_cog(V2DemoCog(bot))
