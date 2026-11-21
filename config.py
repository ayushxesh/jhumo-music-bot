import os
from dotenv import load_dotenv

load_dotenv()


def _int_set(name: str) -> set[int]:
    raw = os.getenv(name, "").replace(" ", "")
    return {int(x) for x in raw.split(",") if x.lstrip("-").isdigit()}


# --- Telegram (required) ---
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
STRING_SESSION = os.getenv("STRING_SESSION", "")

if not (API_ID and API_HASH and BOT_TOKEN and STRING_SESSION):
    raise RuntimeError(
        "API_ID, API_HASH, BOT_TOKEN aur STRING_SESSION environment variables set karo."
    )

# --- Owner (cookies upload + alerts ke liye) ---
OWNER_IDS = _int_set("OWNER_IDS")  # comma separated Telegram user IDs

# --- Limits ---
MAX_QUEUE = int(os.getenv("MAX_QUEUE", "15"))                 # per group
MAX_DURATION_MIN = int(os.getenv("MAX_DURATION_MIN", "20"))   # 0 = no limit

# --- Cache ---
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))   # seconds
CACHE_MAX = int(os.getenv("CACHE_MAX", "500"))    # entries

# --- YouTube ---
YT_PROXY = os.getenv("YT_PROXY", "").strip() or None
POT_URL = os.getenv("POT_URL", "").strip() or None