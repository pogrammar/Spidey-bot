from __future__ import annotations

from pathlib import Path

import discord

# Local folder the owner drops PNGs into. Nothing needs to be told about a new file
# landing here — icon_file() reads straight off disk on every call, so a file added
# or replaced takes effect the next time the command runs, no redeploy needed.
ICONS_DIR = Path(__file__).resolve().parent.parent / "assets" / "icons"


def icon_path(key: str) -> Path:
    return ICONS_DIR / f"{key}.png"


def icon_file(key: str) -> discord.File | None:
    """A fresh discord.File for the given icon key, or None if that PNG hasn't been
    dropped into assets/icons/ yet. A miss should always mean 'render without the
    icon', never an error — icons land incrementally as the owner makes them.

    `key` is either an item's `key` from items.json (icons for shop/inventory items
    reuse that directly, e.g. icon_file("web_shooters")) or one of the UI action
    names listed in assets/icons/README.md (e.g. icon_file("streak"))."""
    path = icon_path(key)
    if not path.is_file():
        return None
    return discord.File(path, filename=path.name)


def set_thumbnail(embed: discord.Embed, file: discord.File | None) -> discord.Embed:
    """Points the embed's thumbnail at the attachment if one was found; no-ops
    otherwise so call sites don't need an if-check of their own."""
    if file is not None:
        embed.set_thumbnail(url=f"attachment://{file.filename}")
    return embed
