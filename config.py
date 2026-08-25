import os
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent

# Precedence: real process environment > local.env > .env.
#
# Features get tested on a separate bot application, so which token is on disk decides
# which bot you connect as — the deployed host carries only .env (the live bot), a dev
# machine adds local.env (the tester bot). Nobody has to edit .env and remember to put it
# back, which is the version of this that eventually connects a work-in-progress build as
# the live bot.
#
# The ordering is the whole mechanism, and it's the reverse of what it looks like:
# load_dotenv does *not* overwrite a name that's already set, so whichever file is loaded
# FIRST wins. Hence local.env first. Don't "fix" this by reordering them or by reaching
# for override=True — override=True would also stomp variables set in the real
# environment, and the scratch check scripts rely on exporting SPIDEY_DB_URL to a throwaway
# sqlite file before importing anything. Under override=True a local.env with its own
# SPIDEY_DB_URL would silently redirect those checks at the real database.
#
# Missing files are a no-op, so this is safe on a host that has neither and injects its
# environment directly.
load_dotenv(_ROOT / "local.env")
load_dotenv(_ROOT / ".env")

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

_dev_guild_id = os.environ.get("DEV_GUILD_ID", "").strip()
DEV_GUILD_ID = int(_dev_guild_id) if _dev_guild_id else None

_admin_ids = os.environ.get("ADMIN_DISCORD_IDS", "").strip()
# Comma-separated list of Discord user IDs allowed to run /admin commands. An empty
# set denies everyone, not everyone-but-unchecked — see cogs/admin_cog.py's _is_admin.
ADMIN_DISCORD_IDS = {int(uid) for uid in _admin_ids.split(",") if uid.strip()}

DB_URL = os.environ.get("SPIDEY_DB_URL", "sqlite+aiosqlite:///./spidey.db")

# Optional: an UptimeRobot Heartbeat monitor's push URL. Left unset, the heartbeat
# cog just doesn't start — no error, no behavior change. (Requires a paid UptimeRobot
# plan — Heartbeat monitors aren't on the free tier.)
UPTIME_ROBOT_PUSH_URL = os.environ.get("UPTIME_ROBOT_PUSH_URL", "").strip() or None

# Port for the tiny /health HTTP endpoint (see cogs.health_cog), for UptimeRobot's
# free HTTP monitor to poll. Must match the port Pterodactyl allocated in the
# Network tab — set SPIDEY_HEALTH_PORT explicitly in .env to that value.
HEALTH_PORT = int(os.environ.get("SPIDEY_HEALTH_PORT", "8080"))

# Optional: an ngrok account's authtoken + claimed free static domain (see
# cogs/tunnel_cog.py). Both must be set for the tunnel to start — gives the bot a
# permanent HTTPS URL (e.g. for Patreon's OAuth redirect_uri) without owning a
# domain or setting up TLS on the host.
NGROK_AUTHTOKEN = os.environ.get("NGROK_AUTHTOKEN", "").strip() or None
NGROK_STATIC_DOMAIN = os.environ.get("NGROK_STATIC_DOMAIN", "").strip() or None

# Patreon OAuth client (see services/patreon_service.py, cogs/patreon_cog.py).
# PATREON_REDIRECT_URI must exactly match a redirect URI registered on that client.
PATREON_CLIENT_ID = os.environ.get("PATREON_CLIENT_ID", "").strip() or None
PATREON_CLIENT_SECRET = os.environ.get("PATREON_CLIENT_SECRET", "").strip() or None
PATREON_REDIRECT_URI = os.environ.get("PATREON_REDIRECT_URI", "").strip() or None

# Exact tier titles as configured on the actual Patreon page — PatreonLink.tier
# stores whatever Patreon's API returns for a patron's tier title verbatim, so
# these have to match character-for-character. Kept in .env (not hardcoded) so a
# tier rename on Patreon's side doesn't require a code change. See
# services/patreon_service.py's tier_rank() for how these get used.
PATREON_ARACHNID_TIER_NAME = os.environ.get("PATREON_ARACHNID_TIER_NAME", "").strip() or None
PATREON_SYMBIOTE_TIER_NAME = os.environ.get("PATREON_SYMBIOTE_TIER_NAME", "").strip() or None
