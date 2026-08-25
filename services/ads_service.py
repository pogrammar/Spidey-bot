"""House promos — the occasional extra card that rides along with a command's own
response, pointing at the Discord server or the Patreon page.

Not paid advertising and nothing external: two destinations we own, in rotation.

Equal coverage is the requirement, so this rotates rather than rolls. A 50/50
random pick is only equal in the limit — over the handful of promos any one player
actually sees it happily deals five server cards and zero Patreon ones. Instead
User.ad_impressions counts up and its parity picks the destination, which makes the
two strictly alternate for every user. Variant choice inside a destination is
rotated for the same reason: random.choice repeats itself, and "various funny
iterations" means the second Patreon card someone sees shouldn't be the first one
again.

Both rotations are phase-shifted by the user's own Discord ID so the fleet doesn't
move in lockstep — otherwise every player's first-ever promo would be the same
destination with the same wording. Snowflake IDs end in a per-process increment
counter, so the low bits are effectively arbitrary, which is all the shift needs.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User
from services.cooldowns import get_remaining_seconds, set_cooldown
from services.patreon_service import PATREON_PAGE_URL

SERVER_INVITE_URL = "https://discord.gg/spider-man"

# Chance that any single successful command is even considered for a promo, before the
# two throttles below get a say.
AD_CHANCE = 0.25

# Hard floor between two promos for the same user, whatever the roll says. Keyed into
# the existing cooldowns table (which has no FK to users, so a throttle can be written
# for anyone) rather than a column, since that's exactly what it is.
AD_COOLDOWN_SECONDS = 15 * 60
COOLDOWN_KEY = "ad_impression"

# Hard floor between two promos in the same *channel* — 15 minutes, so a channel sees at
# most 4 an hour no matter how many people are talking in it. This is the throttle that
# actually decides whether this feature reads as spam.
#
# The per-user one above bounds any one player's experience and nothing else: every
# user carries an independent 15-minute budget, so a channel's promo rate scales
# linearly with how many people are in it. Simulated at 0.25 with only the per-user
# throttle, one channel with 100 active users sees ~240 promos an hour — one every 15
# seconds — while each individual user is politely within their limit. The room is the
# thing being spammed, not the person, so the room needs its own limit.
#
# Deliberately a separate constant from AD_COOLDOWN_SECONDS even though both currently
# sit at 15 minutes: they answer different questions (don't nag a person / don't flood a
# room) and want to be tunable apart, so don't collapse them into one name because the
# numbers happen to match today.
AD_CHANNEL_COOLDOWN_SECONDS = 15 * 60

# In-memory, per-process, keyed by channel ID — deliberately not in the database.
#
# It isn't game state (same reasoning as cooldowns._BYPASS_USER_IDS) and it doesn't need
# to survive a restart: the worst a restart can do is allow one extra promo per channel,
# whereas persisting it would mean a database write on a code path whose whole job is to
# stay cheap. monotonic() rather than wall time so a system clock adjustment can't park a
# channel in the future.
_channel_next_ok: dict[int, float] = {}

# Only relevant if the bot ends up in enough channels for the dict to be worth pruning
# at all; entries are two machine words, so this is housekeeping, not a real concern.
_PRUNE_THRESHOLD = 1000

# /start is already a wall of onboarding, and the /patreon group is the pitch itself —
# a "subscribe to Patreon" card under /patreon subscribe reads like the bot isn't
# paying attention. Group names cover their subcommands (qualified_name is
# "patreon subscribe"), so listing the group skips all of them at once.
#
# /invite is here for the same reason as /patreon: it's already an ask, with its own link
# button. Following it with a second card asking for something else turns one request into
# two in a row, which is the exact impression this feature has to avoid.
SKIP_COMMANDS = {"start", "invite"}
SKIP_GROUPS = {"patreon"}

DEST_SERVER = "server"
DEST_PATREON = "patreon"
# Order is load-bearing: index 0 is what an even impression count resolves to.
DESTINATIONS = (DEST_SERVER, DEST_PATREON)


@dataclass(frozen=True)
class Promo:
    """One card's worth of copy. `body` is deliberately a single line — the owner's
    standing note on /patreon subscribe applies here with more force, since this one
    shows up uninvited next to something the player actually asked for."""

    headline: str
    body: str


# Keep these two lists the same length. Rotation guarantees the destinations get equal
# airtime regardless, but unequal pools would mean one of them cycles its jokes twice as
# fast as the other — scratch/check_ads.py fails if they drift apart.
SERVER_PROMOS = (
    Promo(
        "Your Spider-Sense Is Tingling",
        "That's not danger, that's the Discord arguing about which Spider-Man is best. Go settle it.",
    ),
    Promo(
        "Swinging Solo?",
        "Vigilante work is significantly less lonely with a group chat.",
    ),
    Promo(
        "J. Jonah Jameson Wants A Word",
        "He says you've been fighting crime in an empty city. Get somewhere people can see it.",
    ),
    Promo(
        "With Great Power Comes A Great Server",
        "Uncle Ben handled the first half. He'd have wanted you to click the button for the rest.",
    ),
    Promo(
        "Aunt May Asked If You've Made Friends",
        "You said yes. Make that retroactively true.",
    ),
    Promo(
        "Web Of Connections",
        "Yours is currently attached to exactly one wall. Branch out.",
    ),
    Promo(
        "The Rogues Are Organised. You Aren't.",
        "Everyone in the server is working the same crime wave. Compare notes.",
    ),
    Promo(
        "Patrol Buddy Wanted",
        "No experience necessary. Must tolerate puns.",
    ),
)

PATREON_PROMOS = (
    Promo(
        "Bills Don't Web Themselves",
        "Rent's due, the suit's torn, and the Bugle pays in exposure. Perks help with two of those.",
    ),
    Promo(
        "Symbiote Curious?",
        "The suit whispers to you. Mostly about shorter cooldowns.",
    ),
    Promo(
        "Doc Ock Has Six Arms",
        "You have two. Perks are how you even that up.",
    ),
    Promo(
        "The Suit Won't Repair Itself",
        "At Symbiote tier it more or less does, though.",
    ),
    Promo(
        "Fund The Friendly Neighborhood",
        "Keeps the bot swinging, gets you gear the free suit only ever reads about.",
    ),
    Promo(
        "Parker Luck Is Optional",
        "Perks are the closest thing to not being cursed by narrative causality.",
    ),
    Promo(
        "Upgrade Your Arachnid Status",
        "Same you, better numbers, slightly smug colour bar on every card.",
    ),
    Promo(
        "Your Web Fluid Budget Is A Choice",
        "And right now it's the cheap stuff.",
    ),
)

_POOLS = {DEST_SERVER: SERVER_PROMOS, DEST_PATREON: PATREON_PROMOS}

_BUTTONS = {
    DEST_SERVER: ("Join the Discord", SERVER_INVITE_URL),
    DEST_PATREON: ("See the Perks", PATREON_PAGE_URL),
}

_ICONS = {DEST_SERVER: "web_shooters", DEST_PATREON: "arachnid"}


def should_consider(command_name: str) -> bool:
    """The free half of the gate — no database, no I/O, so the ~75% of commands that
    aren't getting a promo cost nothing beyond a coin flip. `command_name` is
    ctx.command.qualified_name, e.g. "patreon subscribe"."""
    if command_name in SKIP_COMMANDS or command_name.split(" ")[0] in SKIP_GROUPS:
        return False
    return random.random() < AD_CHANCE


def destination_for(user_id: int, impressions: int) -> str:
    return DESTINATIONS[(impressions + user_id) % len(DESTINATIONS)]


def promo_for(user_id: int, impressions: int, destination: str) -> Promo:
    """Which variant of `destination`'s copy this impression shows.

    Divides by the number of destinations first: a given destination only comes up
    every other impression, so the raw count would step the variant twice per
    appearance and show half the pool."""
    pool = _POOLS[destination]
    return pool[(impressions // len(DESTINATIONS) + user_id) % len(pool)]


def button_for(destination: str) -> tuple[str, str]:
    """(label, url) — the call to action stays fixed per destination while the copy
    above it rotates. It's the functional part of the card, not the joke."""
    return _BUTTONS[destination]


def icon_key_for(destination: str) -> str:
    """An emoji key (utils.icons.EMOJI), used inline in the headline rather than as a
    Thumbnail accessory — a promo card is small and uninvited, and a thumbnail would
    mean attaching a PNG to a message the player didn't ask for."""
    return _ICONS[destination]


def channel_ready(channel_id: int) -> bool:
    """Whether this channel is outside its own promo window.

    Checked before the database work in claim_promo, both because it's free and because
    a channel-blocked promo must not consume the user's place in the rotation — they
    should get that card in the next quiet moment, not lose it to a busy room."""
    next_ok = _channel_next_ok.get(channel_id)
    return next_ok is None or time.monotonic() >= next_ok


def mark_channel(channel_id: int) -> None:
    """Opens this channel's window. Called at claim time rather than after a successful
    send, matching the user throttle: a channel the bot can't post in would otherwise
    re-attempt on every single command."""
    now = time.monotonic()
    if len(_channel_next_ok) > _PRUNE_THRESHOLD:
        for cid in [cid for cid, t in _channel_next_ok.items() if t <= now]:
            del _channel_next_ok[cid]
    _channel_next_ok[channel_id] = now + AD_CHANNEL_COOLDOWN_SECONDS


async def claim_promo(session: AsyncSession, user_id: int) -> tuple[str, Promo] | None:
    """Reserves this user's next promo slot, or None if they're still inside the
    throttle window or have no profile row yet.

    Commits the impression bump and the throttle together, before anything tries to
    send: a failed send costing someone one promo is invisible, whereas not
    committing would let a channel the bot can't post in re-roll on every command
    until it succeeds.

    No profile row means the command that just ran never touched player state, which
    also means there's nowhere to keep this user's place in the rotation. Skip rather
    than create one — an ad hook has no business minting economy rows, and the next
    real command will make one anyway."""
    if await get_remaining_seconds(session, user_id, COOLDOWN_KEY) > 0:
        return None

    user = await session.get(User, user_id)
    if user is None:
        return None

    impressions = user.ad_impressions or 0
    destination = destination_for(user_id, impressions)
    promo = promo_for(user_id, impressions, destination)

    user.ad_impressions = impressions + 1
    await set_cooldown(session, user_id, COOLDOWN_KEY, AD_COOLDOWN_SECONDS)
    return destination, promo
