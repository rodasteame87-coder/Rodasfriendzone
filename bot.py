import os
import asyncio
import logging
import random
import time
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
    Update,
    BotCommand,
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
# WEB FOLDER
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

WEB_FOLDER = BASE_DIR / "web"

if not WEB_FOLDER.is_dir():
    WEB_FOLDER = BASE_DIR / "Web"


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# =========================================================
# BOT / DISPATCHER
# =========================================================

bot = Bot(token=BOT_TOKEN)

dp = Dispatcher()


# =========================================================
# BINGO CARDS
# =========================================================

def generate_bingo_card():
    """
    Generate a standard 75-ball Bingo card.

    B: 1-15
    I: 16-30
    N: 31-45
    G: 46-60
    O: 61-75

    Center position is FREE.
    """

    columns = []

    ranges = [
        range(1, 16),
        range(16, 31),
        range(31, 46),
        range(46, 61),
        range(61, 76),
    ]

    for number_range in ranges:
        columns.append(
            random.sample(list(number_range), 5)
        )

    card = []

    for row in range(5):
        current_row = []

        for column in range(5):
            if row == 2 and column == 2:
                current_row.append("FREE")
            else:
                current_row.append(columns[column][row])

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
    """
    Check rows, columns and both diagonals.
    """

    drawn = set(drawn_numbers)

    # Rows
    for row in range(5):
        valid = True

        for column in range(5):
            value = card[row][column]

            if value == "FREE":
                continue

            if value not in drawn:
                valid = False
                break

        if valid:
            return True

    # Columns
    for column in range(5):
        valid = True

        for row in range(5):
            value = card[row][column]

            if value == "FREE":
                continue

            if value not in drawn:
                valid = False
                break

        if valid:
            return True

    # Main diagonal
    valid = True

    for i in range(5):
        value = card[i][i]

        if value == "FREE":
            continue

        if value not in drawn:
            valid = False
            break

    if valid:
        return True

    # Opposite diagonal
    valid = True

    for i in range(5):
        value = card[i][4 - i]

        if value == "FREE":
            continue

        if value not in drawn:
            valid = False
            break

    if valid:
        return True

    return False


# =========================================================
# GAME
# =========================================================

class BingoGame:

    def __init__(self):
        self.phase = "selection"

        self.game_number = 0

        self.selection_end = 0

        self.taken_cartelas = set()

        self.player_cartelas = {}

        self.players = set()

        self.drawn_numbers = []

        self.current_number = None

        self.winner = None

        self.prize = 0

        self.lock = asyncio.Lock()


game = BingoGame()


# =========================================================
# VIRTUAL BALANCES
# =========================================================

balances = {}


def get_balance(user_id):
    return balances.setdefault(user_id, 1000)


# =========================================================
# USER ID
# =========================================================

def get_user_id(request):
    """
    Temporary user identification.

    The frontend currently needs to send X-User-ID.

    If it doesn't, demo-user is used.
    """

    return request.headers.get(
        "X-User-ID"
    ) or "demo-user"


# =========================================================
# SELECTION TIMER
# =========================================================

def selection_remaining():
    if game.phase != "selection":
        return 0

    remaining = int(
        game.selection_end - time.time()
    )

    return max(0, remaining)


# =========================================================
# GAME STATE
# =========================================================

def get_state(user_id):

    selected = game.player_cartelas.get(user_id)

    card = None

    if selected is not None:
        card = CARTELA_CARDS.get(selected)

    return {
        "phase": game.phase,

        "game": game.game_number,

        "players": len(game.players),

        "wallet": get_balance(user_id),

        "timer": selection_remaining(),

        "selection_remaining": selection_remaining(),

        "taken": list(game.taken_cartelas),

        "taken_cartelas": list(game.taken_cartelas),

        "selected": selected,

        "my_cartela": selected,

        "cartela": selected,

        "card": card,

        "called": list(game.drawn_numbers),

        "drawn_numbers": list(game.drawn_numbers),

        "current": game.current_number,

        "current_number": game.current_number,

        "winner": game.winner,

        "prize": game.prize,
    }


# =========================================================
# START SELECTION
# =========================================================

async def start_selection():

    async with game.lock:

        game.phase = "selection"

        game.game_number += 1

        game.selection_end = (
            time.time() + SELECTION_SECONDS
        )

        game.taken_cartelas.clear()

        game.player_cartelas.clear()

        game.players.clear()

        game.drawn_numbers.clear()

        game.current_number = None

        game.winner = None

        game.prize = 0

        logger.info(
            "New game selection started: %s",
            game.game_number
        )


# =========================================================
# START PLAYING
# =========================================================

async def start_playing_locked():

    if game.phase != "selection":
        return

    if not game.player_cartelas:
        logger.info(
            "No players selected a Cartela."
        )

        game.phase = "selection"

        game.selection_end = (
            time.time() + SELECTION_SECONDS
        )

        return

    game.phase = "playing"

    game.players = set(
        game.player_cartelas.keys()
    )

    game.prize = len(game.players) * 100

    game.drawn_numbers.clear()

    game.current_number = None

    game.winner = None

    logger.info(
        "Game %s started with %s players",
        game.game_number,
        len(game.players)
    )


# =========================================================
# PRIZE
# =========================================================

def calculate_prize():

    return len(game.players) * 100


# =========================================================
# DRAW NUMBER
# =========================================================

async def draw_number():

    if game.phase != "playing":
        return

    available = [
        number
        for number in range(1, 76)
        if number not in game.drawn_numbers
    ]

    if not available:

        logger.info(
            "All 75 numbers have been called."
        )

        game.phase = "finished"

        return

    number = random.choice(available)

    game.drawn_numbers.append(number)

    game.current_number = number

    logger.info(
        "Game %s called number %s",
        game.game_number,
        number
    )

    # -----------------------------------------------------
    # IMPORTANT:
    # Automatic Bingo winner is kept compatible with
    # the current backend for now.
    #
    # The manual BINGO WIN / AUTO mode system will be
    # changed in the next game-logic update.
    # -----------------------------------------------------

    for user_id, cartela in list(
        game.player_cartelas.items()
    ):

        card = CARTELA_CARDS.get(cartela)

        if not card:
            continue

        if has_bingo(
            card,
            game.drawn_numbers
        ):

            game.winner = {
                "user_id": user_id,
                "cartela": cartela,
                "number": number,
            }

            prize = calculate_prize()

            balances[user_id] = (
                get_balance(user_id) + prize
            )

            game.prize = prize

            game.phase = "finished"

            logger.info(
                "Bingo detected for user %s, Cartela %s",
                user_id,
                cartela
            )

            break


# =========================================================
# GAME LOOP
# =========================================================

async def game_loop():

    await start_selection()

    while True:

        try:

            # ---------------------------------------------
            # SELECTION
            # ---------------------------------------------

            if game.phase == "selection":

                remaining = selection_remaining()

                if remaining <= 0:

                    async with game.lock:

                        await start_playing_locked()

                    continue

                await asyncio.sleep(0.5)

                continue

            # ---------------------------------------------
            # PLAYING
            # ---------------------------------------------

            if game.phase == "playing":

                await draw_number()

                await asyncio.sleep(
                    DRAW_INTERVAL_SECONDS
                )

                continue

            # ---------------------------------------------
            # FINISHED
            # ---------------------------------------------

            if game.phase == "finished":

                await asyncio.sleep(
                    GAME_OVER_SECONDS
                )

                await start_selection()

                continue

            await asyncio.sleep(1)

        except asyncio.CancelledError:

            raise

        except Exception:

            logger.exception(
                "Error inside game loop"
            )

            await asyncio.sleep(2)


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
                "error": "Invalid JSON"
            },
            status=400
        )

    cartela = data.get("cartela")

    try:
        cartela = int(cartela)

    except Exception:

        return web.json_response(
            {
                "ok": False,
                "error": "Invalid Cartela"
            },
            status=400
        )

    async with game.lock:

        if game.phase != "selection":

            return web.json_response(
                {
                    "ok": False,
                    "error": "Cartela selection is closed."
                },
                status=400
            )

        if selection_remaining() <= 0:

            return web.json_response(
                {
                    "ok": False,
                    "error": "Selection time has ended."
                },
                status=400
            )

        if cartela < 1 or cartela > 100:

            return web.json_response(
                {
                    "ok": False,
                    "error": "Cartela must be between 1 and 100."
                },
                status=400
            )

        # User already selected a Cartela
        if user_id in game.player_cartelas:

            old_cartela = game.player_cartelas[user_id]

            if old_cartela == cartela:

                return web.json_response(
                    {
                        "ok": True,
                        "selected": cartela,
                        "state": get_state(user_id)
                    }
                )

            return web.json_response(
                {
                    "ok": False,
                    "error": "You already selected a Cartela."
                },
                status=400
            )

        # Cartela already taken
        if cartela in game.taken_cartelas:

            return web.json_response(
                {
                    "ok": False,
                    "error": "This Cartela is already taken."
                },
                status=400
            )

        game.taken_cartelas.add(cartela)

        game.player_cartelas[user_id] = cartela

        game.players.add(user_id)

        logger.info(
            "User %s selected Cartela %s",
            user_id,
            cartela
        )

        # If every Cartela is taken, start immediately.
        if len(game.taken_cartelas) >= 100:

            await start_playing_locked()

        return web.json_response(
            {
                "ok": True,
                "selected": cartela,
                "state": get_state(user_id)
            }
        )


# =========================================================
# API: LEAVE
# =========================================================

async def api_leave(request):

    user_id = get_user_id(request)

    async with game.lock:

        if game.phase != "selection":

            return web.json_response(
                {
                    "ok": False,
                    "error": "You cannot leave after the game has started."
                },
                status=400
            )

        cartela = game.player_cartelas.pop(
            user_id,
            None
        )

        game.players.discard(user_id)

        if cartela is not None:

            game.taken_cartelas.discard(
                cartela
            )

            logger.info(
                "User %s left Cartela %s",
                user_id,
                cartela
            )

        return web.json_response(
            {
                "ok": True,
                "state": get_state(user_id)
            }
        )


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
        "🎱 Welcome to Rodas Friend Zone Bingo!\n\n"
        "Choose PLAY BINGO to open the game.",
        reply_markup=keyboard
    )


# =========================================================
# TELEGRAM /PLAY
# =========================================================

@dp.message(Command("play"))
async def play(message: Message):

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
        "🎯 Tap below to play Rodas Friend Zone Bingo.",
        reply_markup=keyboard
    )


# =========================================================
# TELEGRAM /BALANCE
# =========================================================

@dp.message(Command("balance"))
async def balance(message: Message):

    user_id = str(message.from_user.id)

    amount = get_balance(user_id)

    await message.answer(
        f"💰 Your balance: {amount}"
    )


# =========================================================
# TELEGRAM /DEPOSIT
# =========================================================

@dp.message(Command("deposit"))
async def deposit(message: Message):

    await message.answer(
        "💳 Deposit\n\n"
        "Deposit system is being prepared."
    )


# =========================================================
# TELEGRAM /WITHDRAW
# =========================================================

@dp.message(Command("withdraw"))
async def withdraw(message: Message):

    await message.answer(
        "💸 Withdraw\n\n"
        "Withdraw system is being prepared."
    )


# =========================================================
# TELEGRAM /TRANSFER
# =========================================================

@dp.message(Command("transfer"))
async def transfer(message: Message):

    await message.answer(
        "🔄 Transfer\n\n"
        "Transfer system is being prepared."
    )


# =========================================================
# TELEGRAM /INSTRUCTION
# =========================================================

@dp.message(Command("instruction"))
async def instruction(message: Message):

    await message.answer(
        "📖 How to Play\n\n"
        "1. Choose an available Cartela.\n"
        "2. Tap Proceed to enter the game.\n"
        "3. Wait for the game countdown.\n"
        "4. Numbers will be called automatically.\n"
        "5. Complete a valid Bingo pattern to win.\n\n"
        "More game options will be added."
    )


# =========================================================
# TELEGRAM /INVITE
# =========================================================

@dp.message(Command("invite"))
async def invite(message: Message):

    await message.answer(
        "👥 Invite your friends to Rodas Friend Zone Bingo!"
    )


# =========================================================
# TELEGRAM /SUPPORT
# =========================================================

@dp.message(Command("support"))
async def support(message: Message):

    await message.answer(
        "🛟 Support\n\n"
        "Please contact the Rodas Friend Zone support team."
    )


# =========================================================
# TELEGRAM COMMAND MENU
# =========================================================

async def set_bot_commands():

    commands = [

        BotCommand(
            command="start",
            description="Start Rodas Friend Zone"
        ),

        BotCommand(
            command="play",
            description="Play Bingo"
        ),

        BotCommand(
            command="deposit",
            description="Deposit"
        ),

        BotCommand(
            command="balance",
            description="Check balance"
        ),

        BotCommand(
            command="withdraw",
            description="Withdraw"
        ),

        BotCommand(
            command="transfer",
            description="Transfer"
        ),

        BotCommand(
            command="instruction",
            description="How to play"
        ),

        BotCommand(
            command="invite",
            description="Invite friends"
        ),

        BotCommand(
            command="support",
            description="Support"
        ),
    ]

    await bot.set_my_commands(
        commands
    )

    logger.info(
        "Telegram bot commands registered."
    )


# =========================================================
# HEALTH
# =========================================================

async def health(request):

    return web.json_response(
        {
            "status": "ok",
            "service": "RodasFriendZone",
            "game_phase": game.phase,
            "game": game.game_number,
        }
    )


# =========================================================
# ROOT
# =========================================================

async def root(request):

    index_file = WEB_FOLDER / "index.html"

    if not index_file.exists():

        return web.Response(
            text="Rodas Friend Zone web app is missing.",
            status=500
        )

    return web.FileResponse(
        index_file
    )


# =========================================================
# WEBHOOK
# =========================================================

async def telegram_webhook(request):

    secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    if secret != WEBHOOK_SECRET:

        logger.warning(
            "Invalid Telegram webhook secret."
        )

        return web.Response(
            text="Forbidden",
            status=403
        )

    try:

        data = await request.json()

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

    except Exception:

        logger.exception(
            "Telegram webhook error."
        )

        return web.Response(
            text="Bad Request",
            status=400
        )


# =========================================================
# APPLICATION STARTUP
# =========================================================

async def on_startup(app):

    logger.info(
        "Starting Rodas Friend Zone..."
    )

    # Register Telegram commands
    await set_bot_commands()

    # Remove any old webhook first
    try:

        await bot.delete_webhook(
            drop_pending_updates=True
        )

        logger.info(
            "Old Telegram webhook removed."
        )

    except Exception:

        logger.exception(
            "Could not remove old webhook."
        )

    webhook_url = (
        RENDER_EXTERNAL_URL
        + WEBHOOK_PATH
    )

    try:

        await bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True
        )

        logger.info(
            "Telegram webhook set: %s",
            webhook_url
        )

    except Exception:

        logger.exception(
            "Could not set Telegram webhook."
        )

    # Start Bingo game loop
    app["game_task"] = asyncio.create_task(
        game_loop()
    )

    logger.info(
        "Rodas Friend Zone Bingo game loop started."
    )


# =========================================================
# APPLICATION CLEANUP
# =========================================================

async def on_cleanup(app):

    logger.info(
        "Stopping Rodas Friend Zone..."
    )

    task = app.get(
        "game_task"
    )

    if task:

        task.cancel()

        try:
            await task

        except asyncio.CancelledError:
            pass

    try:

        await bot.delete_webhook(
            drop_pending_updates=False
        )

        logger.info(
            "Telegram webhook removed."
        )

    except Exception:

        logger.exception(
            "Could not remove Telegram webhook."
        )

    await bot.session.close()


# =========================================================
# CREATE WEB APP
# =========================================================

app = web.Application()

app.router.add_get(
    "/",
    root
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

app.on_startup.append(
    on_startup
)

app.on_cleanup.append(
    on_cleanup
)


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    logger.info(
        "Rodas Friend Zone running on port %s",
        PORT
    )

    web.run_app(
        app,
        host="0.0.0.0",
        port=PORT
    )
