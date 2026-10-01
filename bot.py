import os
import asyncio
import logging
import random
import time
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
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


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger("RodasFriendZone")


# =========================================================
# FILES
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

# Works with either "web" or "Web"
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
# TELEGRAM BOT
# =========================================================

bot = Bot(token=BOT_TOKEN)

dp = Dispatcher()


# =========================================================
# BINGO CARD
# =========================================================

def generate_bingo_card():

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

                current_row.append("FREE")

            else:

                current_row.append(
                    columns[column][row]
                )

        card.append(current_row)

    return card


CARTELA_CARDS = {

    cartela: generate_bingo_card()

    for cartela in range(1, 101)

}


# =========================================================
# BINGO CHECK
# =========================================================

def has_bingo(card, drawn_numbers):

    marked = set(drawn_numbers)

    marked.add("FREE")

    # Rows
    for row in range(5):

        if all(
            card[row][column] in marked
            for column in range(5)
        ):

            return True

    # Columns
    for column in range(5):

        if all(
            card[row][column] in marked
            for row in range(5)
        ):

            return True

    # Diagonal
    if all(
        card[i][i] in marked
        for i in range(5)
    ):

        return True

    # Other diagonal
    if all(
        card[i][4 - i] in marked
        for i in range(5)
    ):

        return True

    return False


# =========================================================
# GAME
# =========================================================

class BingoGame:

    def __init__(self):

        self.phase = "selection"

        self.game_number = 0

        self.selection_end = (
            time.time() + SELECTION_SECONDS
        )

        # cartela -> user_id
        self.taken_cartelas = {}

        # user_id -> cartela
        self.player_cartelas = {}

        # user_id -> card
        self.players = {}

        self.drawn_numbers = []

        self.current_number = None

        self.winner = None

        self.lock = asyncio.Lock()


game = BingoGame()


# =========================================================
# VIRTUAL DEMO BALANCE
# =========================================================

balances = {}


def get_balance(user_id):

    if user_id not in balances:

        balances[user_id] = 1000

    return balances[user_id]


# =========================================================
# GET USER ID
# =========================================================

def get_user_id(request):

    user_id = request.headers.get(
        "X-User-ID"
    )

    if user_id:

        return str(user_id)

    return "demo-user"


# =========================================================
# SELECTION TIMER
# =========================================================

def selection_remaining():

    if game.phase != "selection":

        return 0

    return max(
        0,
        int(game.selection_end - time.time())
    )


# =========================================================
# API STATE
# =========================================================

def get_state(user_id):

    cartela = game.player_cartelas.get(
        user_id
    )

    card = None

    if cartela:

        card = CARTELA_CARDS.get(
            cartela
        )

    return {

        "ok": True,

        "phase": game.phase,

        "game": game.game_number,

        "players": len(
            game.player_cartelas
        ),

        "wallet": get_balance(user_id),

        "selection_remaining":
            selection_remaining(),

        "taken_cartelas":
            list(game.taken_cartelas.keys()),

        "my_cartela":
            cartela,

        "card":
            card,

        "drawn_numbers":
            game.drawn_numbers,

        "current_number":
            game.current_number,

        "winner":
            game.winner,

    }


# =========================================================
# MINI APP HOME
# =========================================================

async def index(request):

    logger.info(
        "MINI APP REQUEST: %s",
        request.path
    )

    logger.info(
        "Looking for index at: %s",
        INDEX_FILE
    )

    if not INDEX_FILE.exists():

        return web.Response(

            status=500,

            content_type="text/plain",

            text=(
                "RODASFRIENDZONE MINI APP ERROR\n\n"
                "index.html was not found.\n\n"
                f"Expected:\n{INDEX_FILE}\n\n"
                f"WEB DIR:\n{WEB_DIR}"
            )
        )

    return web.FileResponse(
        INDEX_FILE
    )


# =========================================================
# HEALTH
# =========================================================

async def health(request):

    return web.json_response({

        "status": "ok",

        "application":
            "RodasFriendZone",

        "mini_app":
            INDEX_FILE.exists(),

        "index_file":
            str(INDEX_FILE),

        "phase":
            game.phase,

        "game":
            game.game_number,

    })


# =========================================================
# API STATE
# =========================================================

async def api_state(request):

    user_id = get_user_id(
        request
    )

    return web.json_response(
        get_state(user_id)
    )


# =========================================================
# API SELECT CARTELA
# =========================================================

async def api_select(request):

    user_id = get_user_id(
        request
    )

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

        cartela = int(
            data.get("cartela")
        )

    except Exception:

        return web.json_response(

            {
                "ok": False,
                "error": "Invalid Cartela."
            },

            status=400
        )

    async with game.lock:

        if game.phase != "selection":

            return web.json_response(

                {
                    "ok": False,
                    "error":
                        "Cartela selection is closed."
                },

                status=400
            )

        if selection_remaining() <= 0:

            return web.json_response(

                {
                    "ok": False,
                    "error":
                        "Selection time has ended."
                },

                status=400
            )

        if cartela < 1 or cartela > 100:

            return web.json_response(

                {
                    "ok": False,
                    "error":
                        "Cartela must be between 1 and 100."
                },

                status=400
            )

        if user_id in game.player_cartelas:

            return web.json_response(

                {
                    "ok": False,
                    "error":
                        "You already selected a Cartela."
                },

                status=400
            )

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

        # Reserve Cartela
        game.taken_cartelas[
            cartela
        ] = user_id

        game.player_cartelas[
            user_id
        ] = cartela

        logger.info(
            "USER %s SELECTED CARTELA %s",
            user_id,
            cartela
        )

        # If all 100 are taken,
        # start immediately.
        if len(
            game.taken_cartelas
        ) >= 100:

            game.phase = "playing"

    return web.json_response(
        get_state(user_id)
    )


# =========================================================
# API LEAVE
# =========================================================

async def api_leave(request):

    user_id = get_user_id(
        request
    )

    async with game.lock:

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

        if cartela:

            game.taken_cartelas.pop(
                cartela,
                None
            )

            logger.info(
                "USER %s LEFT CARTELA %s",
                user_id,
                cartela
            )

    return web.json_response(
        get_state(user_id)
    )


# =========================================================
# START SELECTION
# =========================================================

async def start_selection():

    async with game.lock:

        game.phase = "selection"

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

        game.game_number += 1

        logger.info(
            "30-SECOND CARTELA SELECTION STARTED"
        )


# =========================================================
# GAME LOOP
# =========================================================

async def game_loop():

    await start_selection()

    while True:

        await asyncio.sleep(1)

        if game.phase == "selection":

            if selection_remaining() <= 0:

                async with game.lock:

                    if game.player_cartelas:

                        game.phase = "playing"

                        game.players = {

                            user_id:
                                CARTELA_CARDS[cartela]

                            for user_id, cartela
                            in game.player_cartelas.items()

                        }

                        game.drawn_numbers = []

                        logger.info(
                            "GAME %s STARTED",
                            game.game_number
                        )

                    else:

                        # Nobody selected.
                        # Keep selection cycle going.
                        game.selection_end = (
                            time.time()
                            + SELECTION_SECONDS
                        )

        elif game.phase == "playing":

            await asyncio.sleep(
                DRAW_INTERVAL_SECONDS
            )

            await draw_number()


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

        if not available:

            game.phase = "finished"

            return

        number = random.choice(
            available
        )

        game.drawn_numbers.append(
            number
        )

        game.current_number = number

        logger.info(
            "NUMBER DRAWN: %s",
            number
        )

        # Check players
        for user_id, card in game.players.items():

            if has_bingo(
                card,
                game.drawn_numbers
            ):

                game.winner = {

                    "user_id":
                        user_id,

                    "cartela":
                        game.player_cartelas[
                            user_id
                        ],

                    "number":
                        number

                }

                game.phase = "finished"

                logger.info(
                    "WINNER: USER %s",
                    user_id
                )

                break

    if game.phase == "finished":

        await asyncio.sleep(5)

        await start_selection()


# =========================================================
# TELEGRAM START
# =========================================================

@dp.message(CommandStart())
async def start(message: Message):

    keyboard = InlineKeyboardMarkup(

        inline_keyboard=[

            [

                InlineKeyboardButton(

                    text="🎯 OPEN RODAS BINGO",

                    web_app=WebAppInfo(

                        url=
                            RENDER_EXTERNAL_URL + "/"

                    )
                )
            ]
        ]
    )

    await message.answer(

        "🎱 Welcome to RodasFriendZone Bingo!\n\n"
        "Tap below to open the Bingo game.",

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

        from aiogram.types import Update

        update = Update.model_validate(
            data
        )

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
        "================================="
    )

    logger.info(
        "RODASFRIENDZONE STARTING"
    )

    logger.info(
        "PORT: %s",
        PORT
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
        "================================="
    )

    # Telegram webhook
    webhook_url = (
        RENDER_EXTERNAL_URL
        + WEBHOOK_PATH
    )

    await bot.set_webhook(

        url=webhook_url,

        secret_token=WEBHOOK_SECRET,

        drop_pending_updates=True

    )

    # Web server
    app = web.Application()

    # THIS IS THE IMPORTANT PART:
    # "/" now serves index.html.
    app.router.add_get(
        "/",
        index
    )

    app.router.add_get(
        "/health",
        health
    )

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

    app.router.add_post(
        WEBHOOK_PATH,
        telegram_webhook
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    site = web.TCPSite(

        runner,

        "0.0.0.0",

        PORT

    )

    await site.start()

    logger.info(
        "SERVER LIVE"
    )

    # Start game cycle
    asyncio.create_task(
        game_loop()
    )

    while True:

        await asyncio.sleep(3600)


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    asyncio.run(
        main()
    )
