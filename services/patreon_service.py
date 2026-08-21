from __future__ import annotations

import datetime
import secrets
import time
from urllib.parse import urlencode

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

import config
from db.models import PatreonLink

AUTHORIZE_URL = "https://www.patreon.com/oauth2/authorize"
TOKEN_URL = "https://www.patreon.com/api/oauth2/token"
IDENTITY_URL = "https://www.patreon.com/api/oauth2/v2/identity"
SCOPES = "identity identity.memberships"

# Pending link requests, keyed by a random state token — this is what ties a
# Patreon callback (which only carries the state back) to the Discord user who
# actually ran /patreon link. In-memory is a deliberate tradeoff: a bot restart
# mid-link just means the user re-runs the command, which is a fine failure mode
# for something that takes seconds, and avoids a throwaway DB table for data that's
# only ever relevant for a few minutes.
STATE_TTL_SECONDS = 600
_pending: dict[str, tuple[int, float]] = {}


class PatreonLinkError(Exception):
    """Raised with a message that's safe to show the user directly."""


# Symbiote includes every Arachnid perk plus its own — a rank comparison
# (`>= TIER_RANK_ARACHNID`) is how an Arachnid-tier perk check should read a
# Symbiote subscriber too, not an equality check against a single tier name.
TIER_RANK_NONE = 0
TIER_RANK_ARACHNID = 1
TIER_RANK_SYMBIOTE = 2


def tier_rank_from_name(tier: str | None) -> int:
    """Pure name -> rank mapping, no DB lookup — exposed separately from
    get_tier_rank() so callers already holding a tier string (e.g. the OAuth
    callback, right after handle_callback returns one) don't need a redundant
    query just to pick the right welcome message."""
    if tier is None:
        return TIER_RANK_NONE
    if config.PATREON_SYMBIOTE_TIER_NAME and tier == config.PATREON_SYMBIOTE_TIER_NAME:
        return TIER_RANK_SYMBIOTE
    if config.PATREON_ARACHNID_TIER_NAME and tier == config.PATREON_ARACHNID_TIER_NAME:
        return TIER_RANK_ARACHNID
    return TIER_RANK_NONE


async def get_tier_rank(session: AsyncSession, discord_id: int) -> int:
    """The single chokepoint every perk check should go through. Reads whatever
    tier title Patreon's API last reported for this user (see handle_callback) and
    maps it to a rank via the exact tier-name strings in config.py — returns
    TIER_RANK_NONE if unlinked, no active pledge, or the tier name doesn't match
    either configured tier (e.g. PATREON_*_TIER_NAME not set yet)."""
    link = await session.get(PatreonLink, discord_id)
    if link is None:
        return TIER_RANK_NONE
    return tier_rank_from_name(link.tier)


# Accelerated Growth (Arachnid+ perk) — Reputation XP boost and Supportive Allies
# decay reduction are deliberately mutually exclusive (see the design notes: stacked,
# they'd compound past either perk's intended standalone rate). One field, one value.
GROWTH_CHOICE_XP = "xp"
GROWTH_CHOICE_ALLIES = "allies"


async def get_growth_choice(session: AsyncSession, discord_id: int) -> str | None:
    """None if never chosen, or if chosen but the subscription has since lapsed
    below Arachnid — a stored choice from an old subscription should never keep
    granting a perk on its own, same "live tier is the only source of truth"
    contract every other perk check uses."""
    tier_rank = await get_tier_rank(session, discord_id)
    if tier_rank < TIER_RANK_ARACHNID:
        return None
    link = await session.get(PatreonLink, discord_id)
    return link.growth_perk_choice if link is not None else None


async def set_growth_choice(session: AsyncSession, discord_id: int, choice: str) -> tuple[bool, str]:
    tier_rank = await get_tier_rank(session, discord_id)
    if tier_rank < TIER_RANK_ARACHNID:
        return False, "This is an Arachnid+ perk — link a subscribed Patreon account first with /patreon link."

    link = await session.get(PatreonLink, discord_id)
    link.growth_perk_choice = choice
    await session.commit()
    label = "Reputation XP boost" if choice == GROWTH_CHOICE_XP else "Supportive Allies"
    return True, f"Locked in — you're now getting the {label}. Switch anytime with /patreon choose."


def build_authorize_url(discord_id: int) -> str:
    if not config.PATREON_CLIENT_ID or not config.PATREON_REDIRECT_URI:
        raise PatreonLinkError("Patreon linking isn't configured yet — try again later.")

    state = secrets.token_urlsafe(24)
    _pending[state] = (discord_id, time.monotonic() + STATE_TTL_SECONDS)

    params = {
        "response_type": "code",
        "client_id": config.PATREON_CLIENT_ID,
        "redirect_uri": config.PATREON_REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _pop_pending(state: str) -> int | None:
    entry = _pending.pop(state, None)
    if entry is None:
        return None
    discord_id, expires_at = entry
    if time.monotonic() > expires_at:
        return None
    return discord_id


async def unlink_account(session: AsyncSession, discord_id: int) -> tuple[bool, str]:
    """Deletes the stored connection entirely — matches what PRIVACY_POLICY.md
    already promises (deletes the Patreon account ID, revokes stored tokens).
    Doesn't touch the actual Patreon pledge itself, only this bot's record of it."""
    link = await session.get(PatreonLink, discord_id)
    if link is None:
        return False, "You're not linked."
    await session.delete(link)
    await session.commit()
    return True, "Unlinked. This doesn't cancel your actual Patreon pledge — that's managed on Patreon's own platform."


def _extract_tier(identity: dict) -> str | None:
    """First currently-entitled tier from the identity response's `included` array,
    or None if the account has no active pledge (still a valid, linked state)."""
    for item in identity.get("included", []):
        if item.get("type") == "tier":
            return item.get("attributes", {}).get("title")
    return None


async def handle_callback(session: AsyncSession, code: str, state: str) -> tuple[int, str | None]:
    """Exchanges the OAuth code, reads the linking user's current tier, and upserts
    their PatreonLink row. Returns (discord_id, tier) on success. Raises
    PatreonLinkError with a message safe to render directly in the callback page."""
    discord_id = _pop_pending(state)
    if discord_id is None:
        raise PatreonLinkError("This link expired or was already used — run /patreon link again.")

    async with aiohttp.ClientSession() as http:
        async with http.post(
            TOKEN_URL,
            data={
                "code": code,
                "grant_type": "authorization_code",
                "client_id": config.PATREON_CLIENT_ID,
                "client_secret": config.PATREON_CLIENT_SECRET,
                "redirect_uri": config.PATREON_REDIRECT_URI,
            },
        ) as resp:
            if resp.status != 200:
                raise PatreonLinkError("Patreon didn't accept that request — run /patreon link again.")
            tokens = await resp.json()

        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]
        expires_in = tokens.get("expires_in", 2678400)  # Patreon's default token lifetime, ~31 days

        async with http.get(
            IDENTITY_URL,
            params={
                "include": "memberships.currently_entitled_tiers",
                # API v2 strips a resource down to just type+id unless its fields
                # are explicitly requested — without this, the tier relationship
                # was present but its title attribute came back empty, which
                # _extract_tier() couldn't tell apart from "no tier at all".
                "fields[tier]": "title",
            },
            headers={"Authorization": f"Bearer {access_token}"},
        ) as resp:
            if resp.status != 200:
                raise PatreonLinkError("Linked, but couldn't read your membership tier — try again shortly.")
            identity = await resp.json()

    patreon_user_id = identity["data"]["id"]
    tier = _extract_tier(identity)
    now = datetime.datetime.utcnow()

    link = await session.get(PatreonLink, discord_id)
    if link is None:
        link = PatreonLink(discord_id=discord_id, linked_at=now)
        session.add(link)
    link.patreon_user_id = patreon_user_id
    link.tier = tier
    link.access_token = access_token
    link.refresh_token = refresh_token
    link.token_expires_at = now + datetime.timedelta(seconds=expires_in)
    link.last_checked_at = now
    await session.commit()

    return discord_id, tier
