import os
from dotenv import load_dotenv

# Load variables from .env when running locally
load_dotenv()

# Telegram Bot Token
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing. Add BOT_TOKEN in Render Environment Variables."
    )

# Render provides these automatically
PORT = int(os.getenv("PORT", "10000"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

# Telegram webhook settings
WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    "RodasFriendZone_927461_secret"
)

# Bingo settings
SELECTION_SECONDS = 30
DRAW_INTERVAL_SECONDS = 5

# Website folder
WEB_FOLDER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "web"
)

# Support both "web" and "Web" folder names
if not os.path.isdir(WEB_FOLDER):
    WEB_FOLDER = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "Web"
    )
