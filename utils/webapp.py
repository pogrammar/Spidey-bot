from __future__ import annotations

import logging

from aiohttp import web

import config

log = logging.getLogger("spidey")

# One shared aiohttp app for every HTTP route the bot needs (currently /health and
# /patreon/callback) — they all have to share config.HEALTH_PORT, since only one
# server can bind a given port. Cogs register their routes onto this app in their
# own __init__ (synchronous, safe before the server's actually listening); bot.py
# calls start() once after every extension has loaded, so route registration order
# across cogs never matters.
app = web.Application()
_runner: web.AppRunner | None = None


async def start() -> None:
    global _runner
    if _runner is not None:
        return
    _runner = web.AppRunner(app)
    try:
        await _runner.setup()
        site = web.TCPSite(_runner, "0.0.0.0", config.HEALTH_PORT)
        await site.start()
        log.info("Web server listening on 0.0.0.0:%s", config.HEALTH_PORT)
    except OSError as exc:
        # A shared web server failing to bind should never take the bot's actual
        # Discord connection down with it — same philosophy as the old per-cog server.
        log.warning("Web server failed to start on port %s: %s", config.HEALTH_PORT, exc)


async def stop() -> None:
    global _runner
    if _runner is not None:
        await _runner.cleanup()
        _runner = None
