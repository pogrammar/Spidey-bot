# Items that get a visual flex badge wherever they're shown — pure collector's items,
# no gameplay function, just meant to stand out when you own one.
RARE_COLLECTIBLE_KEYS = {"unstable_web_fluid"}


def badge(item_key: str) -> str:
    return "✨ " if item_key in RARE_COLLECTIBLE_KEYS else ""
