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
            params={"include": "memberships.currently_entitled_tiers"},
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
