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
RESULT_SECONDS = 10


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
# BOT
# =========================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# =========================================================
# BINGO CARDS
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
    number: generate_bingo_card()
    for number in range(1, 101)
}


# =========================================================
# BINGO VALIDATION
# =========================================================

def winning_patterns(card, numbers):
    """
    Return all winning patterns currently completed.
    """

    called = set(numbers)
    patterns = []

    def complete(values):
        for value in values:
            if value == "FREE":
                continue

            if value not in called:
                return False

        return True

    # Rows
    for row in range(5):
        if complete(card[row]):
            patterns.append(
                f"Row {row + 1}"
            )

    # Columns
    letters = ["B", "I", "N", "G", "O"]

    for column in range(5):

        values = [
            card[row][column]
            for row in range(5)
        ]

        if complete(values):
            patterns.append(
                f"Column {letters[column]}"
            )

    # Main diagonal
    diagonal_1 = [
        card[i][i]
        for i in range(5)
    ]

    if complete(diagonal_1):
        patterns.append(
            "Main Diagonal"
        )

    # Other diagonal
    diagonal_2 = [
        card[i][4 - i]
        for i in range(5)
    ]

    if complete(diagonal_2):
        patterns.append(
            "Diagonal"
        )

    return patterns


def has_bingo(card, numbers):
    return bool(
        winning_patterns(
            card,
            numbers
        )
    )


# =========================================================
# GAME CLASS
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

        self.result_end = 0

        self.blocked_users = set()

        self.result_message = ""

        self.winning_pattern = ""

        self.winning_call = None

        self.lock = asyncio.Lock()


game = BingoGame()


# =========================================================
# BALANCES
# =========================================================

balances = {}


def get_balance(user_id):
    return balances.setdefault(
        user_id,
        1000
    )


# =========================================================
# USER ID
# =========================================================

def get_user_id(request):

    return (
        request.headers.get("X-User-ID")
        or "demo-user"
    )


# =========================================================
# SELECTION TIMER
# =========================================================

def selection_remaining():

    if game.phase != "selection":
        return 0

    return max(
        0,
        int(
            game.selection_end -
            time.time()
        )
    )


# =========================================================
# RESULT TIMER
# =========================================================

def result_remaining():

    if game.phase != "finished":
        return 0

    return max(
        0,
        int(
            game.result_end -
            time.time()
        )
    )


# =========================================================
# STATE
# =========================================================

def get_state(user_id):

    selected = game.player_cartelas.get(
        user_id
    )

    card = None

    if selected is not None:
        card = CARTELA_CARDS.get(
            selected
        )

    return {

        "phase": game.phase,

        "game": game.game_number,

        "players": len(game.players),

        "wallet": get_balance(user_id),

        "timer": selection_remaining(),

        "selection_remaining":
            selection_remaining(),

        "result_remaining":
            result_remaining(),

        "taken":
            list(game.taken_cartelas),

        "taken_cartelas":
            list(game.taken_cartelas),

        "selected": selected,

        "my_cartela": selected,

        "cartela": selected,

        "card": card,

        "called":
            list(game.drawn_numbers),

        "drawn_numbers":
            list(game.drawn_numbers),

        "current":
            game.current_number,

        "current_number":
            game.current_number,

        "winner":
            game.winner,

        "prize":
            game.prize,

        "winning_pattern":
            game.winning_pattern,

        "winning_call":
            game.winning_call,

        "result_message":
            game.result_message,

        "blocked":
            user_id in game.blocked_users,

        "blocked_cartela":
            (
                selected
                if user_id in game.blocked_users
                else None
            ),

        "phase_timer":
            (
                selection_remaining()
                if game.phase == "selection"
                else result_remaining()
            ),
    }


# =========================================================
# START NEW SELECTION
# =========================================================

async def start_selection():

    async with game.lock:

        game.phase = "selection"

        game.game_number += 1

        game.selection_end = (
            time.time() +
            SELECTION_SECONDS
        )

        game.taken_cartelas.clear()

        game.player_cartelas.clear()

        game.players.clear()

        game.drawn_numbers.clear()

        game.current_number = None

        game.winner = None

        game.prize = 0

        game.result_end = 0

        game.blocked_users.clear()

        game.result_message = ""

        game.winning_pattern = ""

        game.winning_call = None

        logger.info(
            "Game %s: Cartela selection started.",
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
            "No players joined game %s.",
            game.game_number
        )

        await start_selection()

        return

    game.phase = "playing"

    game.players = set(
        game.player_cartelas.keys()
    )

    game.prize = (
        len(game.players) * 100
    )

    game.drawn_numbers.clear()

    game.current_number = None

    game.winner = None

    game.result_end = 0

    logger.info(
        "Game %s started with %s players.",
        game.game_number,
        len(game.players)
    )


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

        game.result_message = (
            "No winner. All numbers were called."
        )

        game.phase = "finished"

        game.result_end = (
            time.time() +
            RESULT_SECONDS
        )

        return

    number = random.choice(
        available
    )

    game.drawn_numbers.append(
        number
    )

    game.current_number = number

    logger.info(
        "Game %s called %s.",
        game.game_number,
        number
    )

    # IMPORTANT:
    # There is NO automatic winner here.
    #
    # Even if a card now has Bingo,
    # Auto OFF requires the player to
    # press Bingo.
    #
    # Auto ON is handled by the frontend
    # calling /api/claim-bingo, and the
    # SERVER validates that claim.


# =========================================================
# API STATE
# =========================================================

async def api_state(request):

    user_id = get_user_id(request)

    return web.json_response(
        get_state(user_id)
    )


# =========================================================
# API SELECT
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

        if not 1 <= cartela <= 100:

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        "Cartela must be 1-100."
                },
                status=400
            )

        if user_id in game.player_cartelas:

            old = game.player_cartelas[
                user_id
            ]

            if old == cartela:

                return web.json_response(
                    {
                        "ok": True,
                        "selected": cartela,
                        "state":
                            get_state(user_id)
                    }
                )

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
                        "This Cartela is already taken."
                },
                status=400
            )

        game.taken_cartelas.add(
            cartela
        )

        game.player_cartelas[
            user_id
        ] = cartela

        game.players.add(
            user_id
        )

        logger.info(
            "User %s selected Cartela %s.",
            user_id,
            cartela
        )

        # Keep the 30-second selection timer
        # as the real game-start timer.
        #
        # If all 100 cards are taken,
        # the game starts immediately.
        if len(game.taken_cartelas) >= 100:

            await start_playing_locked()

        return web.json_response(
            {
                "ok": True,
                "selected": cartela,
                "state":
                    get_state(user_id)
            }
        )


# =========================================================
# API LEAVE
# =========================================================

async def api_leave(request):

    user_id = get_user_id(request)

    async with game.lock:

        if game.phase != "selection":

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        "You cannot leave after the game starts."
                },
                status=400
            )

        cartela = game.player_cartelas.pop(
            user_id,
            None
        )

        game.players.discard(
            user_id
        )

        if cartela is not None:

            game.taken_cartelas.discard(
                cartela
            )

        return web.json_response(
            {
                "ok": True,
                "state":
                    get_state(user_id)
            }
        )


# =========================================================
# API CLAIM BINGO
# =========================================================

async def api_claim_bingo(request):

    user_id = get_user_id(request)

    async with game.lock:

        # -------------------------------------------------
        # GAME ALREADY FINISHED
        # -------------------------------------------------

        if game.phase == "finished":

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        "The game has already finished.",
                    "state":
                        get_state(user_id)
                },
                status=400
            )

        # -------------------------------------------------
        # GAME NOT STARTED
        # -------------------------------------------------

        if game.phase != "playing":

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        "The game has not started yet.",
                    "state":
                        get_state(user_id)
                },
                status=400
            )

        # -------------------------------------------------
        # BLOCKED PLAYER
        # -------------------------------------------------

        if user_id in game.blocked_users:

            return web.json_response(
                {
                    "ok": False,
                    "blocked": True,
                    "wrong_cartela": True,
                    "message":
                        "WRONG CARTELA — You lose. Better luck next time!",
                    "state":
                        get_state(user_id)
                },
                status=400
            )

        # -------------------------------------------------
        # PLAYER MUST HAVE CARTELA
        # -------------------------------------------------

        cartela = game.player_cartelas.get(
            user_id
        )

        if cartela is None:

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        "You do not have a Cartela."
                },
                status=400
            )

        card = CARTELA_CARDS.get(
            cartela
        )

        if not card:

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        "Cartela card not found."
                },
                status=400
            )

        # -------------------------------------------------
        # READ MANUAL MARKS
        # -------------------------------------------------

        try:

            data = await request.json()

        except Exception:

            data = {}

        marked = data.get(
            "marked",
            []
        )

        if not isinstance(
            marked,
            list
        ):

            marked = []

        clean_marked = set()

        for value in marked:

            try:
                clean_marked.add(
                    int(value)
                )
            except Exception:
                pass

        called = set(
            game.drawn_numbers
        )

        # -------------------------------------------------
        # MARKED NUMBERS MUST HAVE BEEN CALLED
        # -------------------------------------------------

        if not clean_marked.issubset(
            called
        ):

            game.blocked_users.add(
                user_id
            )

            return web.json_response(
                {
                    "ok": False,
                    "blocked": True,
                    "wrong_cartela": True,
                    "message":
                        "WRONG CARTELA — You lose. Better luck next time!",
                    "state":
                        get_state(user_id)
                },
                status=400
            )

        # -------------------------------------------------
        # CHECK REAL BINGO FROM SERVER CARD
        # -------------------------------------------------

        patterns = winning_patterns(
            card,
            game.drawn_numbers
        )

        if not patterns:

            game.blocked_users.add(
                user_id
            )

            return web.json_response(
                {
                    "ok": False,
                    "blocked": True,
                    "wrong_cartela": True,
                    "message":
                        "WRONG CARTELA — You lose. Better luck next time!",
                    "state":
                        get_state(user_id)
                },
                status=400
            )

        # -------------------------------------------------
        # MANUAL MARK CHECK
        #
        # When Auto is OFF, the player must have
        # actually marked every non-FREE number
        # in at least one winning pattern.
        # -------------------------------------------------

        winning_pattern = patterns[0]

        required = []

        if winning_pattern.startswith("Row"):

            row = int(
                winning_pattern.split()[1]
            ) - 1

            required = [
                card[row][c]
                for c in range(5)
                if card[row][c] != "FREE"
            ]

        elif winning_pattern.startswith("Column"):

            letter = winning_pattern.split()[-1]

            column = {
                "B": 0,
                "I": 1,
                "N": 2,
                "G": 3,
                "O": 4
            }[letter]

            required = [
                card[r][column]
                for r in range(5)
                if card[r][column] != "FREE"
            ]

        elif winning_pattern == "Main Diagonal":

            required = [
                card[i][i]
                for i in range(5)
                if card[i][i] != "FREE"
            ]

        elif winning_pattern == "Diagonal":

            required = [
                card[i][4 - i]
                for i in range(5)
                if card[i][4 - i] != "FREE"
            ]

        # If marked list was supplied, it must contain
        # the winning numbers.
        #
        # Auto mode sends all called numbers, so it
        # passes this check too.

        if not set(required).issubset(
            clean_marked
        ):

            game.blocked_users.add(
                user_id
            )

            return web.json_response(
                {
                    "ok": False,
                    "blocked": True,
                    "wrong_cartela": True,
                    "message":
                        "WRONG CARTELA — You lose. Better luck next time!",
                    "state":
                        get_state(user_id)
                },
                status=400
            )

        # -------------------------------------------------
        # WINNER
        # -------------------------------------------------

        game.winner = {
            "user_id": user_id,
            "cartela": cartela,
        }

        game.winning_pattern = (
            winning_pattern
        )

        game.winning_call = (
            game.current_number
        )

        prize = (
            len(game.players) * 100
        )

        game.prize = prize

        balances[user_id] = (
            get_balance(user_id) +
            prize
        )

        game.result_message = (
            "🏆 WINNER! "
            "Congratulations, you won!"
        )

        game.phase = "finished"

        game.result_end = (
            time.time() +
            RESULT_SECONDS
        )

        logger.info(
            "WINNER: user=%s cartela=%s pattern=%s prize=%s",
            user_id,
            cartela,
            winning_pattern,
            prize
        )

        return web.json_response(
            {
                "ok": True,
                "winner": True,
                "cartela": cartela,
                "pattern":
                    winning_pattern,
                "prize": prize,
                "message":
                    "🏆 Congratulations! You won!",
                "state":
                    get_state(user_id)
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
                        url=
                        RENDER_EXTERNAL_URL + "/"
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
                        url=
                        RENDER_EXTERNAL_URL + "/"
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
# BALANCE
# =========================================================

@dp.message(Command("balance"))
async def balance(message: Message):

    user_id = str(
        message.from_user.id
    )

    await message.answer(
        f"💰 Your balance: "
        f"{get_balance(user_id)}"
    )


# =========================================================
# DEPOSIT
# =========================================================

@dp.message(Command("deposit"))
async def deposit(message: Message):

    await message.answer(
        "💳 Deposit\n\n"
        "Deposit system is being prepared."
    )


# =========================================================
# WITHDRAW
# =========================================================

@dp.message(Command("withdraw"))
async def withdraw(message: Message):

    await message.answer(
        "💸 Withdraw\n\n"
        "Withdraw system is being prepared."
    )


# =========================================================
# TRANSFER
# =========================================================

@dp.message(Command("transfer"))
async def transfer(message: Message):

    await message.answer(
        "🔄 Transfer\n\n"
        "Transfer system is being prepared."
    )


# =========================================================
# INSTRUCTION
# =========================================================

@dp.message(Command("instruction"))
async def instruction(message: Message):

    await message.answer(
        "📖 HOW TO PLAY\n\n"
        "1. Choose an available Cartela.\n"
        "2. Press Proceed.\n"
        "3. The same countdown continues.\n"
        "4. Numbers are called automatically.\n"
        "5. Auto ON marks your called numbers automatically.\n"
        "6. Auto OFF lets you mark the numbers yourself.\n"
        "7. Press BINGO when you have a valid Bingo.\n\n"
        "A wrong Bingo claim blocks your Cartela."
    )


# =========================================================
# INVITE
# =========================================================

@dp.message(Command("invite"))
async def invite(message: Message):

    await message.answer(
        "👥 Invite your friends to "
        "Rodas Friend Zone Bingo!"
    )


# =========================================================
# SUPPORT
# =========================================================

@dp.message(Command("support"))
async def support(message: Message):

    await message.answer(
        "🛟 Support\n\n"
        "Please contact Rodas Friend Zone support."
    )


# =========================================================
# BOT COMMANDS
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
        "Telegram command menu updated."
    )


# =========================================================
# HEALTH
# =========================================================

async def health(request):

    return web.json_response(
        {
            "status": "ok",
            "service":
                "RodasFriendZone",
            "phase":
                game.phase,
            "game":
                game.game_number,
        }
    )


# =========================================================
# ROOT
# =========================================================

async def root(request):

    index_file = (
        WEB_FOLDER /
        "index.html"
    )

    if not index_file.exists():

        return web.Response(
            text=
                "index.html not found.",
            status=500
        )

    return web.FileResponse(
        index_file
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
# STARTUP
# =========================================================

async def on_startup(app):

    logger.info(
        "Starting Rodas Friend Zone..."
    )

    await set_bot_commands()

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
        RENDER_EXTERNAL_URL +
        WEBHOOK_PATH
    )

    try:

        await bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True
        )

        logger.info(
            "Webhook set: %s",
            webhook_url
        )

    except Exception:

        logger.exception(
            "Could not set webhook."
        )

    app["game_task"] = (
        asyncio.create_task(
            game_loop()
        )
    )


# =========================================================
# CLEANUP
# =========================================================

async def on_cleanup(app):

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

    except Exception:

        logger.exception(
            "Could not remove webhook."
        )

    await bot.session.close()


# =========================================================
# GAME LOOP
# =========================================================

async def game_loop():

    await start_selection()

    while True:

        try:

            # -----------------------------
            # CARTELA SELECTION
            # -----------------------------

            if game.phase == "selection":

                if selection_remaining() <= 0:

                    async with game.lock:

                        await start_playing_locked()

                    continue

                await asyncio.sleep(
                    0.25
                )

                continue

            # -----------------------------
            # GAME PLAYING
            # -----------------------------

            if game.phase == "playing":

                await draw_number()

                if game.phase == "playing":

                    await asyncio.sleep(
                        DRAW_INTERVAL_SECONDS
                    )

                continue

            # -----------------------------
            # RESULT
            # -----------------------------

            if game.phase == "finished":

                if result_remaining() <= 0:

                    await start_selection()

                    continue

                await asyncio.sleep(
                    0.25
                )

                continue

            await asyncio.sleep(1)

        except asyncio.CancelledError:

            raise

        except Exception:

            logger.exception(
                "Game loop error."
            )

            await asyncio.sleep(2)


# =========================================================
# ROUTES
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
    "/api/claim-bingo",
    api_claim_bingo
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
# RUN
# =========================================================

if __name__ == "__main__":

    logger.info(
        "Rodas Friend Zone running "
        "on port %s",
        PORT
    )

    web.run_app(
        app,
        host="0.0.0.0",
        port=PORT
    )
