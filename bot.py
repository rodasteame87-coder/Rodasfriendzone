import os
import asyncio
import logging
import random
import time
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
    Update,
)

from config import BOT_TOKEN


# =========================================================
# SETTINGS
# =========================================================

PORT = int(os.getenv("PORT", "10000"))

WEBHOOK_PATH = "/telegram/webhook"

WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    "RodasFriendZone_927461_secret"
)

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://rodasfriendzone.onrender.com"
).rstrip("/")

SELECTION_SECONDS = 30
DRAW_INTERVAL_SECONDS = 5
GAME_OVER_SECONDS = 5


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("RodasFriendZone")


# =========================================================
# FILES
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

if (BASE_DIR / "web").is_dir():
    WEB_DIR = BASE_DIR / "web"
elif (BASE_DIR / "Web").is_dir():
    WEB_DIR = BASE_DIR / "Web"
else:
    WEB_DIR = BASE_DIR / "web"

INDEX_FILE = WEB_DIR / "index.html"

logger.info("BASE DIR: %s", BASE_DIR)
logger.info("WEB DIR: %s", WEB_DIR)
logger.info("INDEX FILE: %s", INDEX_FILE)
logger.info("INDEX EXISTS: %s", INDEX_FILE.exists())


# =========================================================
# TELEGRAM
# =========================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# =========================================================
# BINGO CARDS
# =========================================================

def generate_bingo_card():
    """
    Standard 75-ball Bingo card.

    B = 1-15
    I = 16-30
    N = 31-45
    G = 46-60
    O = 61-75

    Center is FREE.
    """

    columns = [
        random.sample(range(1, 16), 5),
        random.sample(range(16, 31), 5),
        random.sample(range(31, 46), 5),
        random.sample(range(46, 61), 5),
        random.sample(range(61, 76), 5),
    ]

    card = []

    for row in range(5):

        current_row = []

        for column in range(5):

            if row == 2 and column == 2:
                current_row.append(0)  # FREE
            else:
                current_row.append(
                    columns[column][row]
                )

        card.append(current_row)

    return card


# Every Cartela 1-100 gets its own card.
CARTELA_CARDS = {
    cartela: generate_bingo_card()
    for cartela in range(1, 101)
}


# =========================================================
# BINGO CHECK
# =========================================================

def has_bingo(card, drawn_numbers):
    """
    Win condition:
    - complete row
    - complete column
    - main diagonal
    - opposite diagonal

    FREE is automatically marked.
    """

    drawn = set(drawn_numbers)

    def marked(value):
        return value == 0 or value in drawn

    # Rows
    for row in range(5):

        if all(
            marked(card[row][column])
            for column in range(5)
        ):
            return True

    # Columns
    for column in range(5):

        if all(
            marked(card[row][column])
            for row in range(5)
        ):
            return True

    # Main diagonal
    if all(
        marked(card[i][i])
        for i in range(5)
    ):
        return True

    # Opposite diagonal
    if all(
        marked(card[i][4 - i])
        for i in range(5)
    ):
        return True

    return False


# =========================================================
# GAME STATE
# =========================================================

class BingoGame:

    def __init__(self):

        self.phase = "selection"

        self.game_number = 0

        # Exact end time of the 30-second selection.
        self.selection_end = 0

        # cartela -> user_id
        self.taken_cartelas = {}

        # user_id -> cartela
        self.player_cartelas = {}

        # user_id -> bingo card
        self.players = {}

        # Numbers already called: 1-75
        self.drawn_numbers = []

        # Last/current number
        self.current_number = None

        # Winner information
        self.winner = None

        # Winner's prize
        self.prize = 0

        self.lock = asyncio.Lock()


game = BingoGame()


# =========================================================
# VIRTUAL DEMO BALANCES
# =========================================================

balances = {}


def get_balance(user_id):
    if user_id not in balances:
        balances[user_id] = 1000

    return balances[user_id]


# =========================================================
# USER ID
# =========================================================

def get_user_id(request):
    user_id = request.headers.get("X-User-ID")

    if user_id:
        return str(user_id)

    return "demo-user"


# =========================================================
# SELECTION TIMER
# =========================================================

def selection_remaining():
    if game.phase != "selection":
        return 0

    remaining = game.selection_end - time.time()

    return max(0, int(remaining))


# =========================================================
# CURRENT GAME STATE
# =========================================================

def get_state(user_id):

    cartela = game.player_cartelas.get(user_id)

    card = None

    if cartela is not None:
        card = CARTELA_CARDS.get(cartela)

    remaining = selection_remaining()

    return {

        "ok": True,

        # Phase
        "phase": game.phase,

        # Game number
        "game": game.game_number,

        # Player count
        "players": len(game.player_cartelas),

        # Virtual wallet
        "wallet": get_balance(user_id),

        # IMPORTANT:
        # These names match index.html.
        "timer": remaining,

        "selection_remaining": remaining,

        # Cartelas
        "taken": list(game.taken_cartelas.keys()),
        "taken_cartelas": list(game.taken_cartelas.keys()),

        # Current user's Cartela
        "selected": cartela,
        "my_cartela": cartela,
        "cartela": cartela,

        # Bingo card
        "card": card,

        # Called numbers
        "called": list(game.drawn_numbers),
        "drawn_numbers": list(game.drawn_numbers),

        # Current number
        "current": game.current_number,
        "current_number": game.current_number,

        # Winner
        "winner": game.winner,

        # Prize
        "prize": game.prize,
    }


# =========================================================
# MINI APP HOME
# =========================================================

async def index(request):

    logger.info(
        "MINI APP REQUEST: %s",
        request.path
    )

    if not INDEX_FILE.exists():

        return web.Response(
            status=500,
            content_type="text/plain",
            text=(
                "RODASFRIENDZONE MINI APP ERROR\n\n"
                "index.html was not found.\n\n"
                f"Expected: {INDEX_FILE}\n"
            )
        )

    return web.FileResponse(INDEX_FILE)


# =========================================================
# HEALTH
# =========================================================

async def health(request):

    return web.json_response({

        "status": "ok",

        "application": "RodasFriendZone",

        "mini_app": INDEX_FILE.exists(),

        "phase": game.phase,

        "game": game.game_number,

        "players": len(game.player_cartelas),

    })


# =========================================================
# API: STATE
# =========================================================

async def api_state(request):

    user_id = get_user_id(request)

    return web.json_response(
        get_state(user_id)
    )


# =========================================================
# API: SELECT CARTELA
# =========================================================

async def api_select(request):

    user_id = get_user_id(request)

    try:
        data = await request.json()
    except Exception:
        return web.json_response(
            {
                "ok": False,
                "error": "Invalid request."
            },
            status=400
        )

    try:
        cartela = int(data.get("cartela"))
    except Exception:
        return web.json_response(
            {
                "ok": False,
                "error": "Invalid Cartela."
            },
            status=400
        )

    async with game.lock:

        # No new players during Bingo.
        if game.phase != "selection":

            return web.json_response(
                {
                    "ok": False,
                    "error": "Cartela selection is closed."
                },
                status=400
            )

        # Timer is controlled by server.
        if selection_remaining() <= 0:

            return web.json_response(
                {
                    "ok": False,
                    "error": "Selection time has ended."
                },
                status=400
            )

        if not 1 <= cartela <= 100:

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        "Cartela must be between 1 and 100."
                },
                status=400
            )

        # One Cartela per player.
        if user_id in game.player_cartelas:

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        "You already selected a Cartela."
                },
                status=400
            )

        # Cartela already taken.
        if cartela in game.taken_cartelas:

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        f"Cartela {cartela} is already taken. "
                        "Choose another one."
                },
                status=400
            )

        # Reserve Cartela.
        game.taken_cartelas[cartela] = user_id
        game.player_cartelas[user_id] = cartela

        logger.info(
            "USER %s SELECTED CARTELA %s",
            user_id,
            cartela
        )

        # If all 100 Cartelas are taken,
        # selection ends immediately.
        if len(game.taken_cartelas) >= 100:

            await start_playing_locked()

    return web.json_response(
        get_state(user_id)
    )


# =========================================================
# API: LEAVE
# =========================================================

async def api_leave(request):

    user_id = get_user_id(request)

    async with game.lock:

        # During Bingo, players cannot leave
        # in the middle of the game.
        if game.phase != "selection":

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        "You cannot leave during the game."
                },
                status=400
            )

        cartela = game.player_cartelas.pop(
            user_id,
            None
        )

        if cartela is not None:

            game.taken_cartelas.pop(
                cartela,
                None
            )

            logger.info(
                "USER %s RELEASED CARTELA %s",
                user_id,
                cartela
            )

    return web.json_response(
        get_state(user_id)
    )


# =========================================================
# START NEW SELECTION
# =========================================================

async def start_selection():

    async with game.lock:

        game.phase = "selection"

        # IMPORTANT:
        # This starts ONCE for the new game.
        # Players joining/leaving do not reset it.
        game.selection_end = (
            time.time()
            + SELECTION_SECONDS
        )

        game.taken_cartelas.clear()

        game.player_cartelas.clear()

        game.players.clear()

        game.drawn_numbers.clear()

        game.current_number = None

        game.winner = None

        game.prize = 0

        game.game_number += 1

        logger.info(
            "========================================"
        )

        logger.info(
            "GAME %s: 30-SECOND CARTELA SELECTION",
            game.game_number
        )

        logger.info(
            "========================================"
        )


# =========================================================
# START PLAYING
# =========================================================

async def start_playing_locked():

    if game.phase != "selection":
        return

    if not game.player_cartelas:
        return

    game.phase = "playing"

    game.players = {}

    for user_id, cartela in game.player_cartelas.items():

        game.players[user_id] = CARTELA_CARDS[cartela]

    game.drawn_numbers = []

    game.current_number = None

    game.winner = None

    game.prize = 0

    logger.info(
        "========================================"
    )

    logger.info(
        "GAME %s STARTED | PLAYERS: %s",
        game.game_number,
        len(game.players)
    )

    logger.info(
        "========================================"
    )


# =========================================================
# PRIZE CALCULATION
# =========================================================

def calculate_prize():

    """
    Demo prize calculation.

    For now this is virtual only.
    Each player contributes a virtual
    100-unit entry amount.
    """

    player_count = len(game.players)

    if player_count <= 0:
        return 0

    virtual_entry = 100

    return player_count * virtual_entry


# =========================================================
# GAME LOOP
# =========================================================

async def game_loop():

    await start_selection()

    while True:

        if game.phase == "selection":

            remaining = selection_remaining()

            if remaining <= 0:

                async with game.lock:

                    # The timer has reached zero.
                    # It must NOT restart because players
                    # joined or left.

                    if game.player_cartelas:

                        await start_playing_locked()

                    else:

                        # Nobody selected.
                        # Start a completely new 30-sec
                        # selection period.
                        game.selection_end = (
                            time.time()
                            + SELECTION_SECONDS
                        )

                        logger.info(
                            "NO PLAYERS - NEW SELECTION STARTED"
                        )

            else:

                await asyncio.sleep(0.2)

        elif game.phase == "playing":

            # Wait exactly 5 seconds between calls.
            await asyncio.sleep(
                DRAW_INTERVAL_SECONDS
            )

            await draw_number()

        elif game.phase == "finished":

            # Keep the winning/game-over state visible.
            await asyncio.sleep(
                GAME_OVER_SECONDS
            )

            await start_selection()

        else:

            await asyncio.sleep(0.5)


# =========================================================
# DRAW NUMBER
# =========================================================

async def draw_number():

    async with game.lock:

        if game.phase != "playing":
            return

        available = [
            number
            for number in range(1, 76)
            if number not in game.drawn_numbers
        ]

        # All 75 numbers have been called.
        if not available:

            game.phase = "finished"

            logger.info(
                "ALL 75 NUMBERS CALLED"
            )

            return

        # Pick random unused number.
        number = random.choice(available)

        game.drawn_numbers.append(number)

        game.current_number = number

        logger.info(
            "GAME %s | NUMBER CALLED: %s",
            game.game_number,
            number
        )

        # Check every player's card.
        for user_id, card in game.players.items():

            if has_bingo(
                card,
                game.drawn_numbers
            ):

                cartela = game.player_cartelas.get(
                    user_id
                )

                game.winner = {
                    "user_id": user_id,
                    "cartela": cartela,
                    "number": number
                }

                game.prize = calculate_prize()

                # Give virtual prize.
                balances[user_id] = (
                    get_balance(user_id)
                    + game.prize
                )

                game.phase = "finished"

                logger.info(
                    "========================================"
                )

                logger.info(
                    "BINGO WINNER"
                )

                logger.info(
                    "USER: %s",
                    user_id
                )

                logger.info(
                    "CARTELA: %s",
                    cartela
                )

                logger.info(
                    "NUMBER: %s",
                    number
                )

                logger.info(
                    "PRIZE: %s DEMO",
                    game.prize
                )

                logger.info(
                    "========================================"
                )

                break


# =========================================================
# TELEGRAM /START
# =========================================================

@dp.message(CommandStart())
async def start(message: Message):

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎯 PLAY BINGO",
                    web_app=WebAppInfo(
                        url=RENDER_EXTERNAL_URL + "/"
                    )
                )
            ]
        ]
    )

    await message.answer(
        "🎱 Welcome to RodasFriendZone Bingo!\n\n"
        "Choose PLAY BINGO to open the game.",
        reply_markup=keyboard
    )


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

async def telegram_webhook(request):

    secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    if secret != WEBHOOK_SECRET:

        return web.Response(
            status=403,
            text="Forbidden"
        )

    try:

        data = await request.json()

        update = Update.model_validate(data)

        await dp.feed_update(
            bot,
            update
        )

        return web.Response(
            text="OK"
        )

    except Exception as error:

        logger.exception(
            "Webhook error: %s",
            error
        )

        return web.Response(
            status=500,
            text="Webhook error"
        )


# =========================================================
# START SERVER
# =========================================================

async def main():

    logger.info(
        "========================================"
    )

    logger.info(
        "RODASFRIENDZONE STARTING"
    )

    logger.info(
        "PORT: %s",
        PORT
    )

    logger.info(
        "RENDER URL: %s",
        RENDER_EXTERNAL_URL
    )

    logger.info(
        "INDEX: %s",
        INDEX_FILE
    )

    logger.info(
        "INDEX EXISTS: %s",
        INDEX_FILE.exists()
    )

    logger.info(
        "========================================"
    )

    # -----------------------------------------------------
    # TELEGRAM WEBHOOK
    # -----------------------------------------------------

    webhook_url = (
        RENDER_EXTERNAL_URL
        + WEBHOOK_PATH
    )

    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True
    )

    logger.info(
        "Telegram webhook configured: %s",
        webhook_url
    )

    # -----------------------------------------------------
    # WEB SERVER
    # -----------------------------------------------------

    app = web.Application()

    # Mini App
    app.router.add_get(
        "/",
        index
    )

    # Health
    app.router.add_get(
        "/health",
        health
    )

    # Mini App APIs
    app.router.add_get(
        "/api/state",
        api_state
    )

    app.router.add_post(
        "/api/select",
        api_select
    )

    app.router.add_post(
        "/api/leave",
        api_leave
    )

    # Telegram
    app.router.add_post(
        WEBHOOK_PATH,
        telegram_webhook
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    logger.info(
        "SERVER LIVE ON PORT %s",
        PORT
    )

    # -----------------------------------------------------
    # BINGO ENGINE
    # -----------------------------------------------------

    asyncio.create_task(
        game_loop()
    )

    logger.info(
        "BINGO GAME ENGINE STARTED"
    )

    # Keep server alive.
    while True:

        await asyncio.sleep(3600)


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    asyncio.run(main())
