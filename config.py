import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing. Add BOT_TOKEN in Render Environment Variables."
    )

PORT = int(os.getenv("PORT", "10000"))

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    ""
).rstrip("/")

WEBHOOK_PATH = "/telegram/webhook"

WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    "RodasFriendZone_927461_secret"
)

# Game settings
SELECTION_SECONDS = 30

# Bingo number calling interval
# A new number is called every 3 seconds.
DRAW_INTERVAL_SECONDS = 3

# How long the game-over screen remains before the next
# Cartela selection round starts.
GAME_OVER_SECONDS = 5

# Web folder
WEB_FOLDER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "web"
)

# Support capitalized Web folder too
if not os.path.isdir(WEB_FOLDER):
    WEB_FOLDER = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "Web"
    )
