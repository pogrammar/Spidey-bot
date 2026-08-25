"""Where the invoking user's Patreon tier accent lives for the duration of one command.

Set once per command by the global before_invoke hook (utils/first_run.py) and read by
utils.v2_embeds.make_container(), so no command and no view has to pass a colour down by
hand. The alternative was an `accent=` argument threaded through 36 StaticView call sites,
each one a chance to silently drop a paid perk — and a dropped accent is invisible, because
it renders exactly like a free account.

A ContextVar rather than a module-level global: a global would be shared by every
concurrently-running command. pycord dispatches each interaction in its own task and
asyncio.create_task copies the current context at creation, so a set() here is visible to
that one command's code and to nothing else — no chance of one user's colour leaking onto
another user's panel.

IMPORTANT: this is only live for the duration of the command body. Component callbacks
(button presses, select changes) and on_timeout run later, in their own tasks with a fresh
context, so current_accent() returns None inside them. Anything that rebuilds a container
after the initial response must capture the accent into the view at construction time and
re-render from that field — see v2_embeds.PaginatedView and the bespoke panel views.
"""

from __future__ import annotations

import contextvars

_current_accent: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "spidey_tier_accent", default=None
)


def set_current_accent(accent: int | None) -> None:
    _current_accent.set(accent)


def current_accent() -> int | None:
    """None means no subscription, and therefore no accent bar — which is also the right
    answer for anything building a container outside a command context at all."""
    return _current_accent.get()
