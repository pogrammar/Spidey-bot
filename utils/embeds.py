import discord

SPIDEY_RED = discord.Colour.from_rgb(220, 20, 30)
SPIDEY_BLUE = discord.Colour.from_rgb(30, 80, 200)
SPIDEY_GREEN = discord.Colour.from_rgb(40, 170, 80)


def base_embed(title: str, description: str = "", colour: discord.Colour = SPIDEY_RED) -> discord.Embed:
    return discord.Embed(title=title, description=description, colour=colour)


def error_embed(description: str) -> discord.Embed:
    return discord.Embed(title="Parker Luck.", description=description, colour=discord.Colour.dark_grey())
