import os

from dotenv import load_dotenv

load_dotenv()

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
