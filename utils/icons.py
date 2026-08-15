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


# Application emoji, uploaded once via the Discord Developer Portal — unlike the
# icon_file() system above, these aren't read off disk at all; Discord hosts them
# once uploaded, and the bot just needs the `<:name:id>` reference string to use
# them inline in button emoji= fields or embedded directly in text. Keys match the
# same icon-key convention as icon_file() (item keys from items.json, or the UI
# action names from assets/icons/README.md) so both systems can be looked up the
# same way even though they serve different purposes.
EMOJI: dict[str, str] = {
    "web_shooters": "<:web_shooters:1538072524284231741>",
    "web_grabber": "<:web_grabber:1538072522422231050>",
    "web_fluid_vial": "<:web_fluid_vial:1538072520047988858>",
    "wallet": "<:wallet:1538072517883727872>",
    "victory": "<:Victory:1538072515488784434>",
    "upshot": "<:upshot:1538072513127522375>",
    "unstable_web_fluid": "<:unstable_web_fluid:1538072509918879825>",
    "timeout": "<:timeout:1538072507645558825>",
    "suit_warning": "<:suit_warning:1538072505401606185>",
    "suit_integrity": "<:suit_integrity:1538072503140876338>",
    "suit_damage": "<:suit_damage:1538072500620099614>",
    "streak_bar_filled": "<:streak_bar_filled:1538072498346791064>",
    "streak_bar_empty": "<:streak_bar_empty:1538072495519957053>",
    "streak": "<:streak:1538072492722098225>",
    "store": "<:store:1538072490373423154>",
    "spandex_fabric": "<:spandex_fabric:1538072488024481802>",
    "tier_silver": "<:silver_tier:1538072485701099560>",
    "ricochet_web": "<:ricochet_web:1538072483431845929>",
    "retreat": "<:retreat:1538072481288691864>",
    "reputation": "<:reputation_point:1538072479099133992>",
    "ready": "<:ready:1538072475609473124>",
    "rare_badge": "<:Rarebadge:1538072471888994474>",
    "pvp": "<:PVP:1538072469066227802>",
    "money": "<:money:1538072464431513630>",
    "micro_electronics": "<:micro_electronics:1538072462225317958>",
    "market": "<:market:1538072459838881894>",
    "locked": "<:locked:1538072456814923856>",
    "lab": "<:lab:1538072454654730301>",
    "tier_gold": "<:gold_tier:1538072450896764998>",
    "gifts_category": "<:gifts_category:1538072448749150248>",
    "gift_jewelry": "<:gift_jewelry:1538072446584885259>",
    "gift_flowers": "<:gift_flowers:1538072444395462746>",
    "gift_dinner": "<:gift_dinner:1538072442059227226>",
    "gift": "<:gift:1538072439798366300>",
    "gear_category": "<:gear_category:1538072437705539676>",
    "gadgets_category": "<:gadgets_category:1538072434429792337>",
    "gadget_use": "<:gadget_use:1538072431078412329>",
    "evade": "<:evade:1538072428725411870>",
    "concussion_burst": "<:concussion_burst:1538072424942276690>",
    "camera": "<:camera:1538072422883008542>",
    "tier_bronze": "<:bronze_tier:1538072420508901467>",
    "bank": "<:bank:1538072418260615230>",
    "attack": "<:attack:1538072415991500800>",
    "admin_badge": "<:admin_badge:1538072414095671316>",
}


def emoji(key: str) -> str | None:
    """The `<:name:id>` reference for a given icon key, or None if it hasn't been
    uploaded as an application emoji yet — same 'miss means render without it,
    never an error' contract as icon_file()."""
    return EMOJI.get(key)
