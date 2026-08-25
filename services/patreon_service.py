from __future__ import annotations

import asyncio
import datetime
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import aiohttp
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

import config
from db.models import PatreonLink
from utils.icons import emoji

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

# Lives here rather than in patreon_cog so services can name a tier in player-facing
# copy without importing a cog (wrong direction, and a circular import besides).
TIER_RANK_LABELS = {
    TIER_RANK_NONE: "None",
    TIER_RANK_ARACHNID: "Arachnid",
    TIER_RANK_SYMBIOTE: "Symbiote",
}


def tier_requirement_label(min_rank: int) -> str:
    """How to name a tier *gate* in player-facing copy. Anything below the top rank
    gets a "+" because higher tiers satisfy it too (Symbiote can buy Arachnid-gated
    items); the top rank doesn't, since there's nothing above it to include and
    "Symbiote+" would imply a tier that doesn't exist."""
    label = TIER_RANK_LABELS[min_rank]
    return f"{label}+" if min_rank < TIER_RANK_SYMBIOTE else label


# Two different questions get asked about tier emoji, and they have different answers —
# hence two helpers rather than one:
#
#   tier_badge(rank)              -> "which tier is this player?"   (live attribution)
#   tier_requirement_badges(rank) -> "which tiers clear this gate?" (static catalog)
#
# Live attribution is for the moment a perk fires, or a drawback is explained to the
# person carrying it. A Symbiote subscriber reading about the ally-decay cost they
# inherited from Arachnid should see the *Symbiote* badge, because the badge is saying
# "this is your subscription talking" — per GAME_DESIGN.md §9 the tier's *name* never
# appears in that copy, so the emoji is the entire signal, and showing Arachnid's there
# attributes their cost to a tier they aren't on.
#
# A catalog entry has no player to attribute to, so it answers the other question and
# wears every badge that qualifies. A lone Arachnid badge on a shop listing reads as
# "Arachnid only" to a Symbiote subscriber who does in fact have it — this is the visual
# form of the "+" in tier_requirement_label.
def tier_badge(tier_rank: int) -> str:
    """The emoji for the tier a player actually has. "" below Arachnid, and "" if that
    emoji hasn't been uploaded yet — same "a miss means render without it, never an
    error" contract as emoji() itself, so call sites can interpolate it unguarded."""
    if tier_rank >= TIER_RANK_SYMBIOTE:
        return emoji("symbiote") or ""
    if tier_rank >= TIER_RANK_ARACHNID:
        return emoji("arachnid") or ""
    return ""


def tier_requirement_badges(min_rank: int) -> str:
    """Every tier badge that satisfies a gate at `min_rank`, lowest rank first."""
    badges = [
        tier_badge(rank)
        for rank in (TIER_RANK_ARACHNID, TIER_RANK_SYMBIOTE)
        if rank >= min_rank
    ]
    return " ".join(badge for badge in badges if badge)


# The accent bar on every Components V2 container a subscriber sees — the one perk that's
# purely visual. utils/tier_accent.py carries the resolved value through a command;
# utils.v2_embeds.make_container() is where it lands.
#
# Keyed on the *exact* rank, which is deliberate and is the one place in this file that
# isn't a rank comparison. A gate asks "does this rank clear the bar" — that's why
# GATED_ITEM_MIN_RANK is compared with `<` and a Symbiote subscriber satisfies an
# Arachnid-gated item. An accent asks "which tier IS this player", and that has exactly
# one answer. Walking this with `>=` the way tier_requirement_badges does would paint a
# Symbiote subscriber Arachnid red, which is precisely the misattribution tier_badge
# exists to prevent. Do not "consistency-fix" this into a rank comparison.
ACCENT_BY_RANK: dict[int, int] = {
    TIER_RANK_ARACHNID: 0xA91D3A,
    TIER_RANK_SYMBIOTE: 0x707C8F,
}


def accent_for_rank(tier_rank: int) -> int | None:
    """The container accent for the tier a player actually has, or None below Arachnid.
    None means "no accent bar at all" rather than some neutral colour — discord.ui.Container
    treats `colour=None` as identical to omitting it entirely, so callers can pass this
    straight through without an if-check."""
    return ACCENT_BY_RANK.get(tier_rank)


# Patreon-gated purchasables, each mapped to the MINIMUM tier rank allowed to buy it.
# Everyone still *sees* these in the shop (same as any reputation-locked gadget) — only
# the purchase is blocked. list_shop_items applies no tier filter at all.
#
# A rank map rather than one set per tier: buy_item needs exactly one lookup no matter
# how many tiers exist, the refusal message names the tier that actually applies instead
# of hardcoding "Arachnid+", and GATED_ITEM_KEYS below derives from it so a new gated
# item can't be added here and silently omitted from /shop's branding or /patreon perks'
# ownership checklist. Compared with `<`, never `==` — Symbiote is a strict superset of
# Arachnid and must satisfy an Arachnid gate.
#
# Lives here rather than in shop_service (where it was until 2026-08-23) because buying
# is no longer the only thing it governs: the same gate is now re-checked at *use* time
# so a lapsed pledge actually loses these (patrol_service.get_effective_camera,
# gadget_service.list_usable_gadgets). patrol_service can't import shop_service — that's
# a cycle, since shop_service reads CAMERA_FAMILY_KEYS from it — and a tier gate is a
# Patreon concept regardless of which surface consults it.
GATED_ITEM_MIN_RANK: dict[str, int] = {
    "spider_bots": TIER_RANK_ARACHNID,
    "electric_webbing": TIER_RANK_ARACHNID,
    "camera_silver": TIER_RANK_ARACHNID,
    "camera_gold": TIER_RANK_SYMBIOTE,
}

# For the callers that only ask "is this item gated at all?" without caring which tier —
# /shop's branding note, /patreon perks' ownership query. Deliberately NOT named
# ARACHNID_GATED_ITEM_KEYS any more (it was until 2026-08-22): the moment a
# Symbiote-gated item joins the map that name is a lie.
GATED_ITEM_KEYS = frozenset(GATED_ITEM_MIN_RANK)


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


async def locked_item_keys(session: AsyncSession, discord_id: int, item_keys) -> frozenset[str]:
    """Which of `item_keys` this user owns but can no longer use, because the tier that
    unlocked them isn't active any more.

    The read-only counterpart to the enforcement in gadget_service.list_usable_gadgets and
    patrol_service.get_effective_camera: same live-rank rule, but for surfaces that only
    need to *label* the situation. A revoked item that renders identically to a working one
    reads as a bug rather than a lapsed pledge, so every list that shows owned gear should
    be able to ask this cheaply.

    Returns empty without touching the database when nothing in `item_keys` is gated at
    all, which is the normal case."""
    gated = GATED_ITEM_KEYS.intersection(item_keys)
    if not gated:
        return frozenset()
    tier_rank = await get_tier_rank(session, discord_id)
    return frozenset(key for key in gated if tier_rank < GATED_ITEM_MIN_RANK[key])


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


# --------------------------------------------------------------------------- refresh loop
#
# Without this, a tier was written once at link time and never re-read, so cancelling a
# pledge kept every perk forever — the PatreonLink docstring claimed a periodic re-check
# that did not exist. This is that re-check.
#
# THE ONE RULE: only a successful read of Patreon's identity endpoint may change a stored
# tier. Every other path leaves it exactly as it was. A Patreon outage, a DNS blip, a 503
# — none of them may take perks away from someone who is paying. Erring in this direction
# means a lapsed pledge keeps its perks for up to one extra interval, which is the correct
# side to be wrong on.
REFRESH_STALE_AFTER = datetime.timedelta(hours=6)
# A link with no stored tier has nothing to revoke — the only change it can produce is a
# *grant*. That's the link-then-subscribe order (link while still deciding, pledge
# afterwards), and answering it with up to six hours of nothing happening gets the
# asymmetry backwards: being slow to revoke protects someone who is paying, while being
# slow to grant just strands them. Sits just above PATREON_TICK_INTERVAL_MINUTES below, so
# a pending link comes up on roughly every other tick — call it 20-35 minutes from pledging
# to hearing about it. Dropping it under the tick interval wouldn't make that faster, only
# more expensive: the queue can't drain more often than it runs.
REFRESH_PENDING_STALE_AFTER = datetime.timedelta(minutes=20)
# ...but only while the link is still new. refresh_link also nulls the tier on a dead
# grant (_DeadLinkError), and a tier-less row that's been sitting for days is far likelier
# to be one of those — or someone who linked and never pledged — than a pledge about to
# land, and neither is worth re-reading three times an hour forever. Past this window a
# row falls back to the normal cadence above; /patreon link still re-sends the welcome on
# demand for anyone who subscribes later than that.
REFRESH_PENDING_WINDOW = datetime.timedelta(hours=48)
# Cap per tick so one hot loop can't hammer Patreon, and so a persistently failing link
# can't starve the rest of the queue (rows are taken oldest-checked-first).
REFRESH_BATCH_SIZE = 25
# How often the scheduler drains that queue. 15min x 25 = 2,400 checks a day, which
# re-reads every subscriber several times over at any subscriber count this bot will
# realistically see, while staying far under Patreon's rate limits. Deliberately much
# shorter than REFRESH_STALE_AFTER: the tick is how fast the queue drains, the staleness
# window is how often any one link is actually re-read.
PATREON_TICK_INTERVAL_MINUTES = 15


class _TransientPatreonError(Exception):
    """Couldn't reach Patreon, or Patreon couldn't answer right now. Fail open — the
    stored tier is left untouched and the link is simply re-checked next cycle."""


class _DeadLinkError(Exception):
    """Patreon authoritatively refused our credentials *and* refused to refresh them.
    Retrying can never help — only the user re-running /patreon link can. Distinct from
    transient precisely because the fail-open rule must NOT apply: a credential we can
    never verify again is not an outage, and going on granting paid perks off one would
    reopen the exact hole this loop exists to close."""


class _AuthExpiredError(Exception):
    """Internal only: the access token was rejected, so a refresh is worth trying."""


@dataclass
class RefreshOutcome:
    """What one refresh attempt established. `reached_patreon` is the important field —
    it's the difference between "they have no pledge" and "we couldn't tell", which is
    the whole fail-open contract and is exactly what a bare `tier: str | None` return
    could not express."""

    reached_patreon: bool
    tier: str | None = None
    previous_tier: str | None = None
    dead_link: bool = False
    error: str | None = None

    @property
    def changed(self) -> bool:
        return self.reached_patreon and self.tier != self.previous_tier

    @property
    def rank_delta(self) -> int:
        """Negative when the refresh cost the user perks — the case worth logging loudly.
        Positive is the link-then-subscribe grant, which the scheduler turns into a welcome
        DM (cogs/scheduler_cog.py). Only meaningful alongside `changed`, i.e. when we
        actually reached Patreon: an unreachable check reports tier=None and would read as
        a downgrade on its own."""
        return tier_rank_from_name(self.tier) - tier_rank_from_name(self.previous_tier)


async def _get_identity(http: aiohttp.ClientSession, access_token: str) -> dict:
    try:
        async with http.get(
            IDENTITY_URL,
            # Same explicit fields[tier] as handle_callback — without it the tier
            # relationship comes back stripped to type+id and _extract_tier can't tell
            # "no title" from "no tier", which here would read as a cancelled pledge.
            params={"include": "memberships.currently_entitled_tiers", "fields[tier]": "title"},
            headers={"Authorization": f"Bearer {access_token}"},
        ) as resp:
            if resp.status in (401, 403):
                raise _AuthExpiredError()
            if resp.status != 200:
                raise _TransientPatreonError(f"identity returned HTTP {resp.status}")
            return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise _TransientPatreonError(f"identity request failed: {exc!r}") from exc


async def _refresh_tokens(http: aiohttp.ClientSession, link: PatreonLink) -> tuple[str, str, int]:
    """Trades the stored refresh token for a fresh pair. A 4xx here is authoritative:
    Patreon is saying this grant is gone (revoked, or already rotated away), which no
    amount of retrying fixes."""
    try:
        async with http.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": link.refresh_token,
                "client_id": config.PATREON_CLIENT_ID,
                "client_secret": config.PATREON_CLIENT_SECRET,
            },
        ) as resp:
            if 400 <= resp.status < 500:
                raise _DeadLinkError(f"refresh grant rejected with HTTP {resp.status}")
            if resp.status != 200:
                raise _TransientPatreonError(f"token refresh returned HTTP {resp.status}")
            tokens = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise _TransientPatreonError(f"token refresh failed: {exc!r}") from exc

    access = tokens.get("access_token")
    if not access:
        raise _TransientPatreonError("token refresh returned no access_token")
    # Patreon hands back a new refresh token on use; keep the old one if it doesn't, so a
    # response without one can't blank the field and turn a live link into a dead one.
    return access, tokens.get("refresh_token") or link.refresh_token, tokens.get("expires_in", 2678400)


async def refresh_link(session: AsyncSession, link: PatreonLink) -> RefreshOutcome:
    """Re-reads one link's current tier from Patreon and writes it back.

    Commits on every path that changes a row, including the fail-open ones — a failed
    check still stamps last_checked_at so the link rejoins the back of the queue instead
    of being retried every single tick forever. The tier itself is only ever written from
    a successful identity read.
    """
    previous = link.tier
    now = datetime.datetime.utcnow()

    if not config.PATREON_CLIENT_ID or not config.PATREON_CLIENT_SECRET:
        # Nothing to check against. Deliberately does NOT stamp last_checked_at: this is
        # a local misconfiguration, not an attempt, and once the credentials are set the
        # queue should still be in its true order.
        return RefreshOutcome(reached_patreon=False, previous_tier=previous,
                              error="Patreon credentials aren't configured")

    try:
        async with aiohttp.ClientSession() as http:
            try:
                identity = await _get_identity(http, link.access_token)
            except _AuthExpiredError:
                access, refresh, expires_in = await _refresh_tokens(http, link)
                # Committed immediately, before the identity call: Patreon rotates
                # refresh tokens on use, so if the process died between here and the end
                # of this function the stored token would already be spent and the next
                # cycle would read invalid_grant and declare a paying subscriber dead.
                link.access_token = access
                link.refresh_token = refresh
                link.token_expires_at = now + datetime.timedelta(seconds=expires_in)
                await session.commit()
                try:
                    identity = await _get_identity(http, access)
                except _AuthExpiredError as exc:
                    # Rejected a token Patreon minted seconds ago — that's Patreon's
                    # problem, not a dead grant, so it stays fail-open.
                    raise _TransientPatreonError("identity rejected a freshly issued token") from exc
    except _DeadLinkError as exc:
        link.tier = None
        link.last_checked_at = now
        await session.commit()
        return RefreshOutcome(reached_patreon=True, tier=None, previous_tier=previous,
                              dead_link=True, error=str(exc))
    except _TransientPatreonError as exc:
        link.last_checked_at = now
        await session.commit()
        return RefreshOutcome(reached_patreon=False, previous_tier=previous, error=str(exc))

    tier = _extract_tier(identity)
    link.tier = tier
    link.last_checked_at = now
    # patreon_user_id is deliberately not rewritten here. If it ever differs the row is
    # for a different Patreon account than it was, which /patreon link handles properly;
    # silently reassigning it in a background job would hide that.
    await session.commit()
    return RefreshOutcome(reached_patreon=True, tier=tier, previous_tier=previous)


async def refresh_stale_links(session: AsyncSession) -> list[tuple[int, RefreshOutcome]]:
    """One tick's worth of re-checks: the oldest-checked links past their staleness
    window, capped at REFRESH_BATCH_SIZE. Returns (discord_id, outcome) pairs for the
    caller to log — deliberately returns rather than logs, so the service stays free of
    the cog's logging conventions and is testable without capturing output.

    Two windows rather than one (see REFRESH_PENDING_STALE_AFTER). The fast lane is an
    extra way in, never a replacement: the six-hour rule still applies to every row
    regardless of tier, so a tier-less link that ages out of the pending window keeps
    being re-checked at the normal cadence instead of dropping off the queue for good.

    Ordering stays oldest-checked-first across both, which is what keeps the fast lane
    from starving revocation without needing a reserved share of the batch: a row that
    only qualifies via the fast lane is at most twenty minutes stale, so everything
    eligible under the six-hour rule sorts ahead of it."""
    now = datetime.datetime.utcnow()
    stmt = (
        select(PatreonLink)
        .where(
            or_(
                PatreonLink.last_checked_at <= now - REFRESH_STALE_AFTER,
                and_(
                    PatreonLink.tier.is_(None),
                    PatreonLink.linked_at >= now - REFRESH_PENDING_WINDOW,
                    PatreonLink.last_checked_at <= now - REFRESH_PENDING_STALE_AFTER,
                ),
            )
        )
        .order_by(PatreonLink.last_checked_at)
        .limit(REFRESH_BATCH_SIZE)
    )
    links = list((await session.execute(stmt)).scalars())

    results = []
    for link in links:
        # Sequential, not gathered: this is a background job with no deadline, and a
        # burst of concurrent requests is exactly what gets an API client rate-limited.
        #
        # refresh_link commits per link, which is only safe here because async_session is
        # built with expire_on_commit=False (db/base.py). With expiry on, that commit would
        # expire every other object still in `links`, and reading the next link's
        # access_token would need implicit IO — which raises MissingGreenlet under asyncio
        # rather than lazy-loading. If that setting ever changes, load these as plain rows
        # or re-fetch per iteration.
        results.append((link.discord_id, await refresh_link(session, link)))
    return results
