# Items that get a visual flex badge wherever they're shown — pure collector's items,
# no gameplay function, just meant to stand out when you own one.
from utils.icons import emoji

RARE_COLLECTIBLE_KEYS = {"unstable_web_fluid"}

# Fallback only, for a bot whose application emoji haven't been uploaded — same "a miss
# renders without it, never an error" contract as everything in utils.icons. The real badge
# is the project's own rare_badge art; the sparkles are what shipped before it existed.
_FALLBACK_BADGE = "✨"


def badge(item_key: str) -> str:
    if item_key not in RARE_COLLECTIBLE_KEYS:
        return ""
    return f"{emoji('rare_badge') or _FALLBACK_BADGE} "
