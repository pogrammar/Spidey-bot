import os

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

_dev_guild_id = os.environ.get("DEV_GUILD_ID", "").strip()
DEV_GUILD_ID = int(_dev_guild_id) if _dev_guild_id else None

_owner_id = os.environ.get("OWNER_DISCORD_ID", "").strip()
OWNER_DISCORD_ID = int(_owner_id) if _owner_id else None

DB_URL = os.environ.get("SPIDEY_DB_URL", "sqlite+aiosqlite:///./spidey.db")
