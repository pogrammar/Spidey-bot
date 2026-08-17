import asyncio
import io
import logging
import platform
import stat
import tarfile
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands

import config

log = logging.getLogger("spidey")

# Downloaded once and kept here — this directory persists across restarts (same
# volume as spidey.db), so this only happens on a genuinely fresh install.
NGROK_DIR = Path(__file__).resolve().parent.parent / "bin"
NGROK_PATH = NGROK_DIR / "ngrok"
NGROK_DOWNLOAD_URL = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz"


class TunnelCog(commands.Cog):
    """Launches an ngrok tunnel pointed at the /health server's port, using a free
    ngrok account's one permanent static domain — unlike a Cloudflare "quick tunnel"
    (random URL, changes every restart), this gives a fixed HTTPS URL that never
    changes, which is what a Patreon OAuth redirect_uri actually needs long-term.
    Needs NGROK_AUTHTOKEN and NGROK_STATIC_DOMAIN set — silently does nothing if
    either is missing, same "off means off, never an error" contract as the other
    optional integrations (UptimeRobot, etc.)."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.process: asyncio.subprocess.Process | None = None
        # Same "grab the loop that's actually running, not the stale one from Bot()
        # construction" trick health_cog.py uses — see its comment for why.
        asyncio.get_event_loop().create_task(self._start_tunnel())

    async def _ensure_binary(self) -> bool:
        if NGROK_PATH.is_file():
            return True
        if platform.system() != "Linux":
            log.warning("Tunnel: ngrok auto-download only supports Linux hosts; skipping.")
            return False

        NGROK_DIR.mkdir(parents=True, exist_ok=True)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(NGROK_DOWNLOAD_URL) as resp:
                    resp.raise_for_status()
                    archive_bytes = await resp.read()
            # tarfile is blocking — off the event loop so a slow extract never stalls
            # command handling elsewhere while this (one-time) setup runs.
            await asyncio.to_thread(self._extract, archive_bytes)
            mode = NGROK_PATH.stat().st_mode
            NGROK_PATH.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            log.info("Tunnel: downloaded ngrok to %s", NGROK_PATH)
            return True
        except (aiohttp.ClientError, OSError, tarfile.TarError) as exc:
            # A tunnel failing to come up should never take the bot's actual Discord
            # connection down with it — same philosophy as health_cog.py's bind failure.
            log.warning("Tunnel: failed to download/extract ngrok: %s", exc)
            return False

    @staticmethod
    def _extract(archive_bytes: bytes) -> None:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as tar:
            tar.extract("ngrok", path=NGROK_DIR)

    async def _start_tunnel(self) -> None:
        if not config.NGROK_AUTHTOKEN or not config.NGROK_STATIC_DOMAIN:
            log.info("Tunnel: NGROK_AUTHTOKEN/NGROK_STATIC_DOMAIN not set — skipping.")
            return
        if not await self._ensure_binary():
            return

        try:
            configure = await asyncio.create_subprocess_exec(
                str(NGROK_PATH), "config", "add-authtoken", config.NGROK_AUTHTOKEN
            )
            await configure.wait()

            self.process = await asyncio.create_subprocess_exec(
                str(NGROK_PATH),
                "http",
                str(config.HEALTH_PORT),
                f"--url={config.NGROK_STATIC_DOMAIN}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("Tunnel: failed to launch ngrok: %s", exc)
            return

        # The domain's already known (it's the one you claimed on ngrok's dashboard),
        # so there's no output to scrape for it the way the old cloudflared version
        # needed — it's fixed, that's the whole point of using a static domain.
        log.info(
            "Tunnel: live at %s — register %s/patreon/callback as the Patreon redirect URI (only once — this URL doesn't change on restart).",
            config.NGROK_STATIC_DOMAIN,
            config.NGROK_STATIC_DOMAIN,
        )

    def cog_unload(self):
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()


def setup(bot: discord.Bot):
    bot.add_cog(TunnelCog(bot))
