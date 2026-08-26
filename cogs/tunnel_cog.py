import asyncio
import io
import logging
import os
import platform
import shutil
import stat
import tarfile
import zipfile
from collections import deque
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands

import config

log = logging.getLogger("spidey")

IS_WINDOWS = platform.system() == "Windows"

# Downloaded once and kept here — this directory persists across restarts (same
# volume as spidey.db on the real deployment; on a local Windows dev machine it's
# just whatever's checked out), so this only happens on a genuinely fresh install.
NGROK_DIR = Path(__file__).resolve().parent.parent / "bin"
NGROK_PATH = NGROK_DIR / ("ngrok.exe" if IS_WINDOWS else "ngrok")

# Same release ID on equinox.io across platforms — only the platform/ext segment
# changes. macOS isn't handled since neither the real deployment (Linux) nor local
# dev (this project's Windows machines) need it.
NGROK_DOWNLOAD_URL = (
    "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip"
    if IS_WINDOWS
    else "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz"
)

# How long to let ngrok run before believing it. A rejected session — bad authtoken,
# or ERR_NGROK_108 because another agent on the same account already holds the one
# static domain — exits within a second or two; an accepted one stays up for good.
# Waiting is the only way to tell the two apart, since both look identical at the
# moment create_subprocess_exec returns.
STARTUP_GRACE_SECONDS = 5

# Retried, not one-shot. The failure that actually happened in practice was
# transient and external: a dev machine running the bot held the account's single
# static domain, so the deployed agent was rejected at boot. One attempt meant the
# outage outlived its cause by however long it took someone to notice and restart —
# the domain came free and this process never looked again. Backs off to
# RETRY_DELAY_MAX_SECONDS so a genuinely broken config doesn't spin.
RETRY_DELAY_START_SECONDS = 15
RETRY_DELAY_MAX_SECONDS = 300

# Enough of ngrok's own output to explain an exit, and no more — this is drained for
# the whole life of a healthy tunnel, so it has to stay bounded.
OUTPUT_TAIL_LINES = 5


class TunnelCog(commands.Cog):
    """Launches an ngrok tunnel pointed at the /health server's port, using a free
    ngrok account's one permanent static domain — unlike a Cloudflare "quick tunnel"
    (random URL, changes every restart), this gives a fixed HTTPS URL that never
    changes, which is what a Patreon OAuth redirect_uri actually needs long-term.
    Needs NGROK_AUTHTOKEN and NGROK_STATIC_DOMAIN set — silently does nothing if
    either is missing, same "off means off, never an error" contract as the other
    optional integrations (UptimeRobot, etc.). Works on both the real Linux
    deployment and local Windows dev — same static domain either way, since it's
    tied to the ngrok account, not the machine."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.process: asyncio.subprocess.Process | None = None
        self._closing = False
        # Same "grab the loop that's actually running, not the stale one from Bot()
        # construction" trick health_cog.py uses — see its comment for why.
        self._task = asyncio.get_event_loop().create_task(self._start_tunnel())

    async def _ensure_binary(self) -> bool:
        global NGROK_PATH
        if NGROK_PATH.is_file():
            return True

        if IS_WINDOWS:
            # Windows Smart App Control blocks a freshly-downloaded, unsigned exe
            # from ever running at all (confirmed: WinError 4556, "malicious binary
            # reputation") — there's no per-file exception for it once enabled, so
            # auto-downloading is a dead end here. A system install via a channel
            # Windows already trusts (winget/Microsoft Store) doesn't trip this —
            # detect and reuse that instead of fighting the policy.
            system_ngrok = shutil.which("ngrok")
            if system_ngrok is not None:
                NGROK_PATH = Path(system_ngrok)
                log.info("Tunnel: using system-installed ngrok at %s", NGROK_PATH)
                return True
            log.warning(
                "Tunnel: no downloaded or system ngrok found, and Windows Smart App "
                "Control blocks running a freshly-downloaded copy. Run `winget "
                "install ngrok.ngrok` once, then restart the bot."
            )
            return False

        NGROK_DIR.mkdir(parents=True, exist_ok=True)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(NGROK_DOWNLOAD_URL) as resp:
                    resp.raise_for_status()
                    archive_bytes = await resp.read()
            # Archive extraction is blocking — off the event loop so a slow
            # extract never stalls command handling elsewhere while this
            # (one-time) setup runs.
            await asyncio.to_thread(self._extract, archive_bytes)
            if not IS_WINDOWS:
                mode = NGROK_PATH.stat().st_mode
                NGROK_PATH.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            log.info("Tunnel: downloaded ngrok to %s", NGROK_PATH)
            return True
        except (aiohttp.ClientError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            # A tunnel failing to come up should never take the bot's actual Discord
            # connection down with it — same philosophy as health_cog.py's bind failure.
            log.warning("Tunnel: failed to download/extract ngrok: %s", exc)
            return False

    @staticmethod
    def _extract(archive_bytes: bytes) -> None:
        if IS_WINDOWS:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
                zf.extract(NGROK_PATH.name, path=NGROK_DIR)
        else:
            with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as tar:
                tar.extract("ngrok", path=NGROK_DIR)

    @staticmethod
    def _write_config(authtoken: str) -> None:
        """Writes ngrok's config file directly instead of shelling out to `ngrok
        config add-authtoken` — that command was observed hanging indefinitely after
        already completing its work on the Linux deployment (almost certainly a
        background update-check call that never returns on a network-restricted
        host), which stalled the entire bot's startup behind it. This is a plain,
        static v3 config format (see ngrok.com/docs/agent/config/v3), so writing it
        directly sidesteps the subprocess and its hang risk entirely — on both
        platforms, not just the one where the hang was actually observed."""
        if IS_WINDOWS:
            # %LOCALAPPDATA%\ngrok\ngrok.yml — a different default than Linux's
            # ~/.config/ngrok/ngrok.yml, not an XDG-style path on Windows.
            config_dir = Path(os.environ["LOCALAPPDATA"]) / "ngrok"
        else:
            config_dir = Path.home() / ".config" / "ngrok"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "ngrok.yml").write_text(f"version: 3\nagent:\n  authtoken: {authtoken}\n")

    async def _start_tunnel(self) -> None:
        if not config.NGROK_AUTHTOKEN or not config.NGROK_STATIC_DOMAIN:
            log.info("Tunnel: NGROK_AUTHTOKEN/NGROK_STATIC_DOMAIN not set — skipping.")
            return
        if not await self._ensure_binary():
            return

        try:
            await asyncio.to_thread(self._write_config, config.NGROK_AUTHTOKEN)
        except OSError as exc:
            log.warning("Tunnel: failed to write ngrok's config file: %s", exc)
            return

        delay = RETRY_DELAY_START_SECONDS
        last_reason: str | None = None
        while not self._closing:
            reason = await self._run_ngrok()
            if self._closing:
                return
            # The first occurrence carries the information; repeating an unchanged
            # reason every few minutes for the life of the process just buries the
            # rest of the log. A *changed* reason is worth surfacing again, since
            # it usually means the failure moved (e.g. 108 conflict -> bad token).
            if reason != last_reason:
                log.warning("Tunnel: ngrok exited — %s. Retrying in %ss.", reason, delay)
                last_reason = reason
            else:
                log.debug("Tunnel: ngrok still failing — %s.", reason)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_DELAY_MAX_SECONDS)

    async def _run_ngrok(self) -> str:
        """Runs ngrok to completion, returning why it stopped. Never raises — a tunnel
        failing must not take the bot's Discord connection down with it, same
        philosophy as health_cog.py's bind failure."""
        try:
            self.process = await asyncio.create_subprocess_exec(
                str(NGROK_PATH),
                "http",
                str(config.HEALTH_PORT),
                f"--url={config.NGROK_STATIC_DOMAIN}",
                # Without a TTY ngrok already logs here rather than drawing its
                # interactive display, but say so explicitly so a future ngrok
                # deciding otherwise can't silently blind this again.
                "--log=stdout",
                stdout=asyncio.subprocess.PIPE,
                # Merged rather than a second pipe: which stream ngrok picks for a
                # fatal error varies, and one stream means one thing to drain.
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            return f"failed to launch {NGROK_PATH}: {exc}"

        process = self.process
        recent: deque[str] = deque(maxlen=OUTPUT_TAIL_LINES)

        async def drain() -> None:
            # Read continuously instead of collecting it at the end: an undrained
            # pipe fills its buffer and blocks the child indefinitely, which would
            # hang a tunnel that was otherwise working perfectly.
            assert process.stdout is not None
            async for raw in process.stdout:
                line = raw.decode(errors="replace").strip()
                if line:
                    recent.append(line)

        drainer = asyncio.create_task(drain())
        try:
            # Still alive after the grace period means the session was accepted, so
            # this is the earliest point at which claiming "live" is actually true.
            # The old code logged it unconditionally one line after spawning, which
            # reported success just as loudly when ngrok had already exited.
            await asyncio.sleep(STARTUP_GRACE_SECONDS)
            if process.returncode is None:
                log.info(
                    "Tunnel: live at %s — register %s/patreon/callback as the Patreon redirect URI (only once — this URL doesn't change on restart).",
                    config.NGROK_STATIC_DOMAIN,
                    config.NGROK_STATIC_DOMAIN,
                )
            await process.wait()
        finally:
            await drainer

        # ngrok's own words beat the exit code every time — "ERR_NGROK_108: account
        # limited to N simultaneous sessions" is the whole diagnosis, where "exit
        # code 1" starts another round of guessing.
        for line in reversed(recent):
            if "ERR_NGROK" in line or "lvl=eror" in line or "lvl=crit" in line:
                return line
        if recent:
            return f"exit code {process.returncode}: {recent[-1]}"
        return f"exit code {process.returncode} (no output)"

    def cog_unload(self):
        # Set before cancelling so the retry loop can't relaunch what we're about to
        # terminate — otherwise unloading the cog would leave an orphan ngrok holding
        # the account's static domain, which is the exact conflict this cog trips on.
        self._closing = True
        self._task.cancel()
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()


def setup(bot: discord.Bot):
    bot.add_cog(TunnelCog(bot))
