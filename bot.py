import os
import asyncio
import logging
import random
import time

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
    Update,
    BotCommand,
)

from config import BOT_TOKEN


# =========================
# SETTINGS
# =========================

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
GAME_START_SECONDS = 30
DRAW_INTERVAL_SECONDS = 5
GAME_OVER_SECONDS = 5

BET_AMOUNT = 100


# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("RodasFriendZone")


# =========================
# WEB FOLDER
# =========================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

WEB_FOLDER = os.path.join(BASE_DIR, "web")

if not os.path.isdir(WEB_FOLDER):
    WEB_FOLDER = os.path.join(BASE_DIR, "Web")


# =========================
# BOT
# =========================

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# =========================
# BALANCES
# =========================

balances = {}


def get_balance(user_id):
    return balances.setdefault(user_id, 1000)


# =========================
# BINGO CARDS
# =========================

def generate_card():
    columns = []

    ranges = [
        range(1, 16),    # B
        range(16, 31),   # I
        range(31, 46),   # N
        range(46, 61),   # G
        range(61, 76),   # O
    ]

    for r in ranges:
        columns.append(random.sample(list(r), 5))

    card = []

    for row in range(5):
        card.append([
            columns[col][row]
            for col in range(5)
        ])

    card[2][2] = "FREE"

    return card


CARTELA_CARDS = {
    number: generate_card()
    for number in range(1, 101)
}


# =========================
# BINGO CHECK
# =========================

def has_bingo(card, marked_numbers):

    marked = set(marked_numbers)

    # FREE is always marked
    marked.add("FREE")

    # Rows
    for row in card:
        if all(
            cell == "FREE" or cell in marked
            for cell in row
        ):
            return True

    # Columns
    for col in range(5):
        if all(
            card[row][col] == "FREE"
            or card[row][col] in marked
            for row in range(5)
        ):
            return True

    # Main diagonal
    if all(
        card[i][i] == "FREE"
        or card[i][i] in marked
        for i in range(5)
    ):
        return True

    # Opposite diagonal
    if all(
        card[i][4 - i] == "FREE"
        or card[i][4 - i] in marked
        for i in range(5)
    ):
        return True

    return False


# =========================
# GAME
# =========================

class BingoGame:

    def __init__(self):
        self.lock = asyncio.Lock()

        self.game_number = 0

        # selection timer
        self.selection_end = None

        # second 30 second timer
        self.game_start_end = None

        # True after numbers begin
        self.playing = False

        # True after winner
        self.finished = False

        # user_id -> cartela
        self.players = {}

        # users who pressed Proceed
        self.ready_users = set()

        # user_id -> set of manually marked numbers
        self.manual_marks = {}

        # user_id -> auto on/off
        self.auto_marking = {}

        # user_id -> numbers automatically marked
        self.auto_marks = {}

        self.drawn_numbers = []

        self.current_number = None

        self.winner = None

        self.prize = 0


game = BingoGame()


# =========================
# USER ID
# =========================

def get_user_id(request):

    return (
        request.headers.get("X-User-ID")
        or "demo-user"
    )


# =========================
# TIME HELPERS
# =========================

def selection_remaining():

    if game.selection_end is None:
        return 0

    return max(
        0,
        int(game.selection_end - time.time())
    )


def game_start_remaining():

    if game.game_start_end is None:
        return 0

    return max(
        0,
        int(game.game_start_end - time.time())
    )


# =========================
# CURRENT USER MARKS
# =========================

def get_user_marks(user_id):

    if game.auto_marking.get(user_id, True):

        return set(game.auto_marks.get(user_id, set()))

    return set(game.manual_marks.get(user_id, set()))


# =========================
# STATE
# =========================

def get_state(user_id):

    if game.finished:

        phase = "finished"

    elif game.playing:

        phase = "playing"

    elif game.game_start_end is not None:

        # This user has entered the game
        # while the second 30 sec countdown runs.
        if user_id in game.ready_users:
            phase = "starting"
        else:
            phase = "selection"

    else:

        phase = "selection"

    cartela = game.players.get(user_id)

    card = CARTELA_CARDS.get(cartela)

    auto = game.auto_marking.get(user_id, True)

    marks = get_user_marks(user_id)

    return {
        "phase": phase,

        "game": game.game_number,

        "players": len(game.players),

        "wallet": get_balance(user_id),

        "bet": BET_AMOUNT,

        "prize": game.prize,

        "selection_remaining": selection_remaining(),

        "timer": selection_remaining(),

        "game_start_remaining": game_start_remaining(),

        "start_timer": game_start_remaining(),

        "taken": list(game.players.values()),

        "taken_cartelas": list(game.players.values()),

        "selected": cartela,

        "my_cartela": cartela,

        "cartela": cartela,

        "card": card,

        "called": list(game.drawn_numbers),

        "drawn_numbers": list(game.drawn_numbers),

        "current": game.current_number,

        "current_number": game.current_number,

        "call_count": len(game.drawn_numbers),

        "winner": game.winner,

        "auto_marking": auto,

        "auto": auto,

        "marked": list(marks),

        "ready": user_id in game.ready_users,
    }


# =========================
# NEW SELECTION
# =========================

async def start_selection():

    async with game.lock:

        game.game_number += 1

        game.selection_end = (
            time.time() + SELECTION_SECONDS
        )

        game.game_start_end = None

        game.playing = False
        game.finished = False

        game.players.clear()
        game.ready_users.clear()

        game.manual_marks.clear()
        game.auto_marks.clear()
        game.auto_marking.clear()

        game.drawn_numbers.clear()

        game.current_number = None

        game.winner = None

        game.prize = 0

        logger.info(
            "Game %s selection started",
            game.game_number
        )


# =========================
# START GAME
# =========================

async def begin_game():

    async with game.lock:

        if game.playing:
            return

        if not game.players:
            return

        game.playing = True
        game.finished = False

        game.prize = (
            len(game.players) * BET_AMOUNT
        )

        logger.info(
            "Game %s STARTED with %s players",
            game.game_number,
            len(game.players)
        )


# =========================
# DRAW NUMBER
# =========================

async def draw_number():

    async with game.lock:

        if not game.playing:
            return

        if game.finished:
            return

        available = [
            n for n in range(1, 76)
            if n not in game.drawn_numbers
        ]

        if not available:
            return

        number = random.choice(available)

        game.drawn_numbers.append(number)

        game.current_number = number

        # AUTO MARKING
        for user_id, cartela in game.players.items():

            if game.auto_marking.get(
                user_id,
                True
            ):

                game.auto_marks.setdefault(
                    user_id,
                    set()
                ).add(number)

                card = CARTELA_CARDS[cartela]

                marks = game.auto_marks[user_id]

                # Automatic Bingo
                if has_bingo(
                    card,
                    marks
                ):

                    game.winner = {
                        "user_id": user_id,
                        "cartela": cartela,
                        "number": number,
                        "automatic": True,
                    }

                    balances[user_id] = (
                        get_balance(user_id)
                        + game.prize
                    )

                    game.finished = True
                    game.playing = False

                    logger.info(
                        "AUTO BINGO: %s",
                        user_id
                    )

                    return

        logger.info(
            "Called number: %s",
            number
        )


# =========================
# GAME LOOP
# =========================

async def game_loop():

    await start_selection()

    while True:

        try:

            # -------------------------
            # SELECTION
            # -------------------------

            while selection_remaining() > 0:

                await asyncio.sleep(0.5)

            # -------------------------
            # SELECTION FINISHED
            # -------------------------

            async with game.lock:

                if game.game_start_end is None:

                    if game.players:

                        game.game_start_end = (
                            time.time()
                            + GAME_START_SECONDS
                        )

                        # Everyone who selected
                        # gets into the game.
                        game.ready_users.update(
                            game.players.keys()
                        )

                    else:

                        game.selection_end = (
                            time.time()
                            + SELECTION_SECONDS
                        )

                        continue

            # -------------------------
            # GAME START COUNTDOWN
            # -------------------------

            while game_start_remaining() > 0:

                await asyncio.sleep(0.5)

            # -------------------------
            # START CALLING
            # -------------------------

            await begin_game()

            # -------------------------
            # DRAWING
            # -------------------------

            while game.playing:

                await draw_number()

                if not game.playing:
                    break

                await asyncio.sleep(
                    DRAW_INTERVAL_SECONDS
                )

            # -------------------------
            # GAME OVER
            # -------------------------

            await asyncio.sleep(
                GAME_OVER_SECONDS
            )

            await start_selection()

        except asyncio.CancelledError:

            raise

        except Exception:

            logger.exception(
                "Game loop error"
            )

            await asyncio.sleep(2)


# =========================
# API STATE
# =========================

async def api_state(request):

    user_id = get_user_id(request)

    return web.json_response(
        get_state(user_id)
    )


# =========================
# SELECT CARTELA
# =========================

async def api_select(request):

    user_id = get_user_id(request)

    try:
        data = await request.json()

        cartela = int(
            data.get("cartela")
        )

    except Exception:

        return web.json_response(
            {
                "ok": False,
                "error": "Invalid Cartela"
            },
            status=400
        )

    async with game.lock:

        if game.playing or game.finished:

            return web.json_response(
                {
                    "ok": False,
                    "error": "Game already started"
                },
                status=400
            )

        if selection_remaining() <= 0:

            return web.json_response(
                {
                    "ok": False,
                    "error": "Selection time finished"
                },
                status=400
            )

        if cartela < 1 or cartela > 100:

            return web.json_response(
                {
                    "ok": False,
                    "error": "Cartela must be 1-100"
                },
                status=400
            )

        # Already selected
        if user_id in game.players:

            return web.json_response(
                {
                    "ok": False,
                    "error": "You already selected a Cartela"
                },
                status=400
            )

        # Taken by someone else
        if cartela in game.players.values():

            return web.json_response(
                {
                    "ok": False,
                    "error": "Cartela already taken"
                },
                status=400
            )

        game.players[user_id] = cartela

        game.manual_marks[user_id] = set()

        game.auto_marks[user_id] = set()

        game.auto_marking[user_id] = True

        return web.json_response(
            {
                "ok": True,
                "cartela": cartela
            }
        )


# =========================
# PROCEED
# =========================

async def api_proceed(request):

    user_id = get_user_id(request)

    async with game.lock:

        if user_id not in game.players:

            return web.json_response(
                {
                    "ok": False,
                    "error": "Choose a Cartela first"
                },
                status=400
            )

        game.ready_users.add(user_id)

        # Start the second 30-second countdown
        # the first time somebody proceeds.
        if game.game_start_end is None:

            game.game_start_end = (
                time.time()
                + GAME_START_SECONDS
            )

            logger.info(
                "Game-start countdown started"
            )

        return web.json_response(
            {
                "ok": True,
                "start_timer":
                    game_start_remaining()
            }
        )


# =========================
# AUTO TOGGLE
# =========================

async def api_toggle_auto(request):

    user_id = get_user_id(request)

    try:

        data = await request.json()

        enabled = bool(
            data.get("enabled")
        )

    except Exception:

        return web.json_response(
            {
                "ok": False,
                "error": "Invalid setting"
            },
            status=400
        )

    async with game.lock:

        if user_id not in game.players:

            return web.json_response(
                {
                    "ok": False,
                    "error": "Choose a Cartela first"
                },
                status=400
            )

        game.auto_marking[user_id] = enabled

        return web.json_response(
            {
                "ok": True,
                "auto_marking": enabled
            }
        )


# =========================
# MANUAL MARK
# =========================

async def api_mark(request):

    user_id = get_user_id(request)

    try:

        data = await request.json()

        number = int(
            data.get("number")
        )

    except Exception:

        return web.json_response(
            {
                "ok": False,
                "error": "Invalid number"
            },
            status=400
        )

    async with game.lock:

        if game.auto_marking.get(
            user_id,
            True
        ):

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        "Turn Auto Marking off first"
                },
                status=400
            )

        if number not in game.drawn_numbers:

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        "That number has not been called"
                },
                status=400
            )

        cartela = game.players.get(user_id)

        if not cartela:

            return web.json_response(
                {
                    "ok": False,
                    "error": "No Cartela selected"
                },
                status=400
            )

        card = CARTELA_CARDS[cartela]

        # Make sure number is actually on card
        exists = any(
            number in row
            for row in card
        )

        if not exists:

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        "Number is not on your card"
                },
                status=400
            )

        game.manual_marks.setdefault(
            user_id,
            set()
        ).add(number)

        return web.json_response(
            {
                "ok": True,
                "marked":
                    list(game.manual_marks[user_id])
            }
        )


# =========================
# BINGO WIN
# =========================

async def api_claim_bingo(request):

    user_id = get_user_id(request)

    async with game.lock:

        if not game.playing:

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        "The game is not currently running"
                },
                status=400
            )

        if game.winner:

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        "Someone already won"
                },
                status=400
            )

        cartela = game.players.get(user_id)

        if not cartela:

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        "You do not have a Cartela"
                },
                status=400
            )

        card = CARTELA_CARDS[cartela]

        marks = get_user_marks(user_id)

        if not has_bingo(
            card,
            marks
        ):

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        "BINGO is not valid yet"
                },
                status=400
            )

        game.winner = {
            "user_id": user_id,
            "cartela": cartela,
            "number": game.current_number,
            "automatic": False,
        }

        balances[user_id] = (
            get_balance(user_id)
            + game.prize
        )

        game.finished = True
        game.playing = False

        logger.info(
            "MANUAL BINGO: %s",
            user_id
        )

        return web.json_response(
            {
                "ok": True,
                "winner": game.winner,
                "prize": game.prize
            }
        )


# =========================
# LEAVE
# =========================

async def api_leave(request):

    user_id = get_user_id(request)

    async with game.lock:

        if game.playing:

            return web.json_response(
                {
                    "ok": False,
                    "error":
                        "You cannot leave during the game"
                },
                status=400
            )

        game.players.pop(
            user_id,
            None
        )

        game.ready_users.discard(
            user_id
        )

        game.manual_marks.pop(
            user_id,
            None
        )

        game.auto_marks.pop(
            user_id,
            None
        )

        game.auto_marking.pop(
            user_id,
            None
        )

        return web.json_response(
            {
                "ok": True
            }
        )


# =========================
# TELEGRAM COMMANDS
# =========================

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


async def play_command(message: Message):

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
        "🎯 Open Rodas Friend Zone Bingo:",
        reply_markup=keyboard
    )


async def balance_command(message: Message):

    user_id = str(message.from_user.id)

    await message.answer(
        f"💰 Balance: {get_balance(user_id)}"
    )


async def instruction_command(message: Message):

    await message.answer(
        "🎱 HOW TO PLAY\n\n"
        "1. Choose one Cartela from 1-100.\n"
        "2. Press Proceed.\n"
        "3. Wait for the 30-second game countdown.\n"
        "4. Numbers will then be called automatically.\n"
        "5. With Auto Marking ON, your card marks automatically.\n"
        "6. With Auto Marking OFF, touch called numbers yourself.\n"
        "7. Press BINGO WIN when you have a valid Bingo."
    )


async def support_command(message: Message):

    await message.answer(
        "🆘 Rodas Friend Zone Support"
    )


# =========================
# COMMAND REGISTRATION
# =========================

async def setup_commands():

    await bot.set_my_commands(
        [
            BotCommand(
                command="start",
                description="Start"
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
                description="Balance"
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
                description="Invite"
            ),
            BotCommand(
                command="support",
                description="Support"
            ),
        ]
    )


# =========================
# WEBHOOK
# =========================

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

    except Exception:

        logger.exception(
            "Telegram webhook error"
        )

        return web.Response(
            status=500,
            text="ERROR"
        )


# =========================
# HEALTH
# =========================

async def health(request):

    return web.json_response(
        {
            "ok": True,
            "service":
                "RodasFriendZone"
        }
    )


# =========================
# APP
# =========================

async def main():

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    await setup_commands()

    await start_selection()

    game_task = asyncio.create_task(
        game_loop()
    )

    app = web.Application()

    app.router.add_get(
        "/",
        lambda request:
            web.FileResponse(
                os.path.join(
                    WEB_FOLDER,
                    "index.html"
                )
            )
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
        "/api/proceed",
        api_proceed
    )

    app.router.add_post(
        "/api/toggle-auto",
        api_toggle_auto
    )

    app.router.add_post(
        "/api/mark",
        api_mark
    )

    app.router.add_post(
        "/api/claim-bingo",
        api_claim_bingo
    )

    app.router.add_post(
        "/api/leave",
        api_leave
    )

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

    webhook_url = (
        RENDER_EXTERNAL_URL
        + WEBHOOK_PATH
    )

    await bot.set_webhook(
        webhook_url,
        secret_token=WEBHOOK_SECRET
    )

    logger.info(
        "Rodas Friend Zone running on port %s",
        PORT
    )

    logger.info(
        "Webhook: %s",
        webhook_url
    )

    try:

        while True:

            await asyncio.sleep(3600)

    finally:

        game_task.cancel()

        await bot.delete_webhook()

        await bot.session.close()


if __name__ == "__main__":

    asyncio.run(main())
