import discord

SPIDEY_RED = discord.Colour.from_rgb(220, 20, 30)
SPIDEY_BLUE = discord.Colour.from_rgb(30, 80, 200)
SPIDEY_GREEN = discord.Colour.from_rgb(40, 170, 80)


def base_embed(title: str, description: str = "", colour: discord.Colour = SPIDEY_RED) -> discord.Embed:
    return discord.Embed(title=title, description=description, colour=colour)


def error_embed(description: str) -> discord.Embed:
    return discord.Embed(title="Parker Luck.", description=description, colour=discord.Colour.dark_grey())


def link_button_view(label: str, url: str) -> discord.ui.View:
    """One link button on a classic-embed response — the counterpart to
    `v2_embeds.StaticView(link_button=...)`, for the surfaces that are still embeds and
    should stay that way.

    `/lab`'s refusals are the reason this exists (2026-08-28). They needed a shop button
    without becoming Components V2 cards: `error_embed`'s "Parker Luck." grey is the error
    identity every cog in the project shares, and a V2 conversion would both break that
    and paint a *refusal* in the caller's Patreon accent (`make_container` applies it
    automatically). A classic embed plus a classic view keeps the refusal looking like
    every other refusal in the bot.

    timeout=None for the same reason StaticView uses it: a link button has no callback, so
    there is nothing for a timeout to protect. Clicking it opens a URL client-side and
    never comes back through the bot, which is also why the button keeps working
    indefinitely — there is no interaction that could expire.
    """
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url))
    return view
