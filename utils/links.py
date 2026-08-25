"""Outbound links the project owns, where more than one module needs them.

Not in config.py: config is env-derived, and these are the same on every deployment
(see the client_id note below for why that's the point, not an oversight). Not in a
service either — services own a *domain* and hang their URL off it, which is why
PATREON_PAGE_URL lives in patreon_service and SERVER_INVITE_URL in ads_service. The
bot's own install link has no such domain; it's just a string two cogs both need.
"""

from __future__ import annotations

# The live bot's application ID, hardcoded rather than built from bot.user.id.
#
# That's deliberate, and it's the two-bot split talking (see config.py): on a dev machine
# local.env connects as the *tester* application, and a tester bot that hands out its own
# invite link is inviting people to a work-in-progress build nobody should be playing on.
# Whichever bot renders the button, the link points at the real one.
#
# permissions=0 asks for no permission bits at all, so the bot's role lands with none and
# it inherits whatever @everyone can already do in a channel. That's the intended posture
# — an install prompt with no scary checklist — and it's also why every send in this
# project treats a refusal as normal (cogs/ads_cog.py swallows HTTPException for exactly
# this case).
BOT_INVITE_URL = (
    "https://discord.com/oauth2/authorize"
    "?client_id=1536438986913095751&scope=bot%20applications.commands&permissions=0"
)
