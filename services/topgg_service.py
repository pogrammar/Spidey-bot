from __future__ import annotations

import hmac
import logging

from sqlalchemy.ext.asyncio import AsyncSession

import config
from services import cooldowns
from services.economy import add_wallet, get_or_create_user

log = logging.getLogger("spidey")

# The public listing — where someone goes to cast the vote. Voting is a top.gg-side
# action; nothing here calls this URL, it's just the destination /vote points at.
TOPGG_PAGE_URL = "https://top.gg/bot/1536438986913095751"
TOPGG_VOTE_URL = f"{TOPGG_PAGE_URL}/vote"

VOTE_REWARD = 1000

# top.gg's own rule is one vote per 12 hours, and it enforces that server-side. This
# cooldown is NOT a second copy of that rule for the player's benefit — it's replay
# protection for us. The webhook is an unauthenticated-by-default HTTP endpoint whose
# only secret is a shared token, and a replayed POST is otherwise indistinguishable
# from a real vote, so without this a captured request could be resent for unbounded
# cash. Keep it at (or above) top.gg's interval: shorter, and a legitimate re-vote is
# the thing it starts rejecting.
VOTE_COOLDOWN_SECONDS = 12 * 60 * 60

COOLDOWN_KEY = "topgg_vote"
TRANSACTION_REASON = "topgg:vote"


class VoteRejected(Exception):
    """A well-formed vote we're declining to pay. Carries the HTTP status the webhook
    should answer with, because top.gg's retry behaviour depends on it: 4xx means "stop,
    this request is broken", 2xx means "received, don't retry". A duplicate vote is the
    latter — the request was fine, we simply already paid it, and answering 4xx there
    would make top.gg retry a request that will never succeed."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def is_configured() -> bool:
    """Whether the webhook can actually run. False means /vote should send people to the
    listing but promise nothing, because a vote can't be credited."""
    return bool(config.TOPGG_WEBHOOK_SECRET)


def check_auth(header_value: str | None) -> bool:
    """Constant-time comparison against the configured secret.

    An unset secret returns False — it must never fall through to "accept anything".
    That's the difference between an unconfigured feature and an open endpoint that
    hands out money to whoever finds it, and the failure is silent either way, so this
    is the one place the distinction can be enforced. The caller answers 503 for that
    case rather than 401, since the problem is our configuration, not their request.
    """
    if not config.TOPGG_WEBHOOK_SECRET:
        return False
    if not header_value:
        return False
    return hmac.compare_digest(header_value, config.TOPGG_WEBHOOK_SECRET)


def parse_user_id(payload: dict) -> int:
    """Pull the voter's Discord ID out of a top.gg webhook body.

    top.gg sends IDs as strings ("snowflakes"), which is why this coerces rather than
    trusting the type. Raises VoteRejected with 400 on anything unusable, because a body
    we can't read is a broken request and retrying it won't help.
    """
    raw = payload.get("user")
    if raw is None:
        raise VoteRejected("payload has no 'user' field", 400)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise VoteRejected(f"unusable 'user' value: {raw!r}", 400) from None


def is_test_payload(payload: dict) -> bool:
    """top.gg's dashboard sends type="test" when you click "Test" on the webhook config.
    It carries a real-looking body, so it has to be recognised and NOT paid, or testing
    the wiring quietly mints cash."""
    return payload.get("type") == "test"


async def credit_vote(session: AsyncSession, user_id: int) -> int:
    """Pay a verified vote. Returns the voter's new wallet balance.

    Order matters and is deliberate: the cooldown is written and committed BEFORE the
    money moves. If the process dies between the two commits, the voter is out one
    payout and can say so — whereas the reverse order would leave the vote replayable
    after a crash that happened *because* of the payout, which is the failure that costs
    unbounded cash rather than $1000.
    """
    remaining = await cooldowns.get_remaining_seconds(session, user_id, COOLDOWN_KEY)
    if remaining > 0:
        raise VoteRejected(
            f"already credited a vote for {user_id}, "
            f"{cooldowns.format_remaining(remaining)} left",
            200,
        )

    user = await get_or_create_user(session, user_id)
    await cooldowns.set_cooldown(session, user_id, COOLDOWN_KEY, VOTE_COOLDOWN_SECONDS)
    balance = await add_wallet(session, user, VOTE_REWARD, TRANSACTION_REASON)
    log.info("Credited top.gg vote: user=%s amount=%s balance=%s", user_id, VOTE_REWARD, balance)
    return balance
