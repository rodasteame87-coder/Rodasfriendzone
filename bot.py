import asyncio
import logging
import os
import random
import time
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Message,
    Update,
    WebAppInfo,
)

from config import BOT_TOKEN, WEBHOOK_SECRET


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("rodas-friend-zone-bingo")


# ============================================================
# CONFIGURATION
# ============================================================

PORT = int(os.getenv("PORT", "10000"))

WEB_DIR = Path(__file__).resolve().parent / "web"

BET_AMOUNT = 10

SELECTION_SECONDS = 30
DRAW_INTERVAL = 3
RESULT_SECONDS = 10
PRIZE_PERCENT = 0.75

CARTELA_MIN = 1
CARTELA_MAX = 100

WEBHOOK_PATH = "/telegram/webhook"

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://rodasfriendzone.onrender.com",
).rstrip("/")

WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"


# ============================================================
# BOT / DISPATCHER
# ============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# GLOBAL GAME STATE
# ============================================================

games = {}

game_lock = asyncio.Lock()

next_game_number = 1

game_loop_task = None


# ============================================================
# HELPERS
# ============================================================

def now():
    return time.time()


def get_user_id(request):
    """
    Gets the user ID supplied by the web app.

    Telegram Web App / frontend can send:
      X-User-ID
      X-Telegram-User-ID

    If neither exists, use guest.
    """
    value = (
        request.headers.get("X-User-ID")
        or request.headers.get("X-Telegram-User-ID")
        or "guest"
    )

    return str(value)


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def generate_game_number():
    global next_game_number

    number = next_game_number
    next_game_number += 1

    return number


def bingo_column(column):
    ranges = {
        "B": range(1, 16),
        "I": range(16, 31),
        "N": range(31, 46),
        "G": range(46, 61),
        "O": range(61, 76),
    }

    values = list(ranges[column])

    return random.sample(values, 5)


def generate_cartela():
    columns = ["B", "I", "N", "G", "O"]

    board = []

    column_values = {
        column: bingo_column(column)
        for column in columns
    }

    for row in range(5):
        current_row = []

        for column in columns:
            if column == "N" and row == 2:
                current_row.append("FREE")
            else:
                current_row.append(column_values[column][row])

        board.append(current_row)

    return board


def flatten_board(board):
    result = []

    for row in board:
        for value in row:
            if value != "FREE":
                result.append(value)

    return result


def check_pattern(board, called_numbers):
    called = set(called_numbers)

    # Rows
    for row in board:
        if all(
            value == "FREE" or value in called
            for value in row
        ):
            return True

    # Columns
    for col in range(5):
        if all(
            board[row][col] == "FREE"
            or board[row][col] in called
            for row in range(5)
        ):
            return True

    # Main diagonal
    if all(
        board[i][i] == "FREE"
        or board[i][i] in called
        for i in range(5)
    ):
        return True

    # Other diagonal
    if all(
        board[i][4 - i] == "FREE"
        or board[i][4 - i] in called
        for i in range(5)
    ):
        return True

    return False


def winning_patterns_for_board(board, called_numbers):
    called = set(called_numbers)

    patterns = []

    # Rows
    for row_index, row in enumerate(board):
        if all(
            value == "FREE" or value in called
            for value in row
        ):
            patterns.append(f"row-{row_index + 1}")

    # Columns
    for col in range(5):
        if all(
            board[row][col] == "FREE"
            or board[row][col] in called
            for row in range(5)
        ):
            patterns.append(f"column-{col + 1}")

    # Main diagonal
    if all(
        board[i][i] == "FREE"
        or board[i][i] in called
        for i in range(5)
    ):
        patterns.append("diagonal-main")

    # Other diagonal
    if all(
        board[i][4 - i] == "FREE"
        or board[i][4 - i] in called
        for i in range(5)
    ):
        patterns.append("diagonal-other")

    return patterns


# ============================================================
# BINGO GAME
# ============================================================

class BingoGame:

    def __init__(self, game_number):
        self.game_number = game_number

        self.phase = "selection"

        self.selection_started_at = now()

        self.selection_seconds = SELECTION_SECONDS

        self.players = {}
        self.player_names = {}

        self.boards = {}

        self.left_users = set()
        self.proceeded_users = set()

        self.called_numbers = []

        self.number_pool = list(range(1, 76))
        random.shuffle(self.number_pool)

        self.last_draw_at = None

        self.started_at = None

        self.finished_at = None

        self.winner = None
        self.winner_user_id = None
        self.winner_cartela = None
        self.winner_pattern = None

        self.total_bets = 0
        self.prize = 0

        self.result_started_at = None

    # --------------------------------------------------------
    # TIMERS
    # --------------------------------------------------------

    def selection_remaining(self):
        elapsed = now() - self.selection_started_at

        return max(
            0,
            int(self.selection_seconds - elapsed),
        )

    def result_remaining(self):
        if not self.result_started_at:
            return RESULT_SECONDS

        elapsed = now() - self.result_started_at

        return max(
            0,
            int(RESULT_SECONDS - elapsed),
        )

    def reset_selection_timer(self):
        self.selection_started_at = now()

    # --------------------------------------------------------
    # START GAME
    # --------------------------------------------------------

    def start_playing(self):
        self.phase = "playing"

        self.started_at = now()

        self.last_draw_at = now()

        logger.info(
            "Game %s started with %s player(s).",
            self.game_number,
            len(self.players),
        )

    # --------------------------------------------------------
    # PLAYER
    # --------------------------------------------------------

    def add_player(self, user_id, name=None):
        user_id = str(user_id)

        if user_id in self.left_users:
            self.left_users.remove(user_id)

        if user_id not in self.players:
            self.players[user_id] = None
            self.total_bets += BET_AMOUNT

        if name:
            self.player_names[user_id] = name

        return True

    def select_cartela(self, user_id, cartela_number):
        user_id = str(user_id)

        cartela_number = safe_int(
            cartela_number,
            0,
        )

        if not (
            CARTELA_MIN
            <= cartela_number
            <= CARTELA_MAX
        ):
            return False, "Invalid cartela number."

        self.add_player(user_id)

        for other_user_id, selected in self.players.items():
            if (
                other_user_id != user_id
                and selected == cartela_number
            ):
                return False, "That cartela is already selected."

        self.players[user_id] = cartela_number

        self.boards[user_id] = generate_cartela()

        return True, "Cartela selected."

    def deselect_cartela(self, user_id):
        user_id = str(user_id)

        if user_id in self.players:
            self.players[user_id] = None

        self.boards.pop(user_id, None)

        return True

    def proceed(self, user_id):
        user_id = str(user_id)

        if user_id not in self.players:
            return False

        if self.players[user_id] is None:
            return False

        self.proceeded_users.add(user_id)

        return True

    def leave(self, user_id):
        user_id = str(user_id)

        self.left_users.add(user_id)

        self.players.pop(user_id, None)
        self.player_names.pop(user_id, None)
        self.boards.pop(user_id, None)
        self.proceeded_users.discard(user_id)

        return True

    # --------------------------------------------------------
    # DRAW NUMBERS
    # --------------------------------------------------------

    def draw_number(self):
        if not self.number_pool:
            return None

        number = self.number_pool.pop(0)

        self.called_numbers.append(number)

        self.last_draw_at = now()

        logger.info(
            "Game %s: number called: %s",
            self.game_number,
            number,
        )

        return number

    # --------------------------------------------------------
    # WINNER
    # --------------------------------------------------------

    def check_winner(self):
        for user_id, board in self.boards.items():

            if user_id not in self.players:
                continue

            if self.players.get(user_id) is None:
                continue

            if check_pattern(
                board,
                self.called_numbers,
            ):
                patterns = winning_patterns_for_board(
                    board,
                    self.called_numbers,
                )

                self.winner_user_id = user_id

                self.winner = (
                    self.player_names.get(
                        user_id,
                        user_id,
                    )
                )

                self.winner_cartela = self.players.get(
                    user_id
                )

                self.winner_pattern = (
                    patterns[0]
                    if patterns
                    else None
                )

                self.prize = round(
                    self.total_bets * PRIZE_PERCENT,
                    2,
                )

                return True

        return False

    # --------------------------------------------------------
    # SERIALIZE
    # --------------------------------------------------------

    def serialize(self, user_id=None):

        user_id = (
            str(user_id)
            if user_id is not None
            else None
        )

        my_cartela = None
        my_board = None

        if user_id:
            my_cartela = self.players.get(user_id)

            my_board = self.boards.get(user_id)

        return {
            "game_number": self.game_number,
            "phase": self.phase,

            "running": self.phase
            in (
                "selection",
                "starting",
                "playing",
                "result",
            ),

            "selection_remaining": (
                self.selection_remaining()
                if self.phase == "selection"
                else 0
            ),

            "result_remaining": (
                self.result_remaining()
                if self.phase == "result"
                else 0
            ),

            "players": len(self.players),

            "player_count": len(self.players),

            "my_cartela": my_cartela,

            "my_board": my_board,

            "called": self.called_numbers,

            "called_numbers": self.called_numbers,

            "last_called": (
                self.called_numbers[-1]
                if self.called_numbers
                else None
            ),

            "winner": self.winner,

            "winner_user_id": self.winner_user_id,

            "winner_cartela": self.winner_cartela,

            "winner_pattern": self.winner_pattern,

            "prize": self.prize,

            "total_bets": self.total_bets,
        }


# ============================================================
# CURRENT GAME
# ============================================================

def get_current_game():
    active = [
        game
        for game in games.values()
        if game.phase != "finished"
    ]

    if active:
        return active[-1]

    game_number = generate_game_number()

    game = BingoGame(game_number)

    games[game_number] = game

    logger.info(
        "Initial Bingo game created: %s",
        game_number,
    )

    return game


# ============================================================
# GAME LOOP
# ============================================================

async def game_loop():

    logger.info("Bingo game loop started.")

    while True:

        try:

            async with game_lock:

                game = get_current_game()

                if game.phase == "selection":

                    remaining = game.selection_remaining()

                    if remaining <= 0:

                        if game.players:

                            game.phase = "starting"

                            logger.info(
                                "Game %s: selection finished.",
                                game.game_number,
                            )

                            await asyncio.sleep(1)

                            game.start_playing()

                        else:

                            game.reset_selection_timer()

                            logger.info(
                                "Game %s: selection restarted.",
                                game.game_number,
                            )

                elif game.phase == "playing":

                    if (
                        game.last_draw_at is None
                        or now() - game.last_draw_at
                        >= DRAW_INTERVAL
                    ):

                        number = game.draw_number()

                        if number is not None:

                            if game.check_winner():

                                game.phase = "result"

                                game.result_started_at = now()

                                logger.info(
                                    "Game %s: winner is %s.",
                                    game.game_number,
                                    game.winner,
                                )

                        else:

                            game.phase = "result"

                            game.result_started_at = now()

                elif game.phase == "result":

                    if game.result_remaining() <= 0:

                        game.phase = "finished"

                        game.finished_at = now()

                        logger.info(
                            "Game %s finished.",
                            game.game_number,
                        )

                elif game.phase == "finished":

                    new_game_number = generate_game_number()

                    new_game = BingoGame(
                        new_game_number
                    )

                    games[new_game_number] = new_game

                    logger.info(
                        "New Bingo game created: %s",
                        new_game_number,
                    )

        except asyncio.CancelledError:
            logger.info("Bingo game loop cancelled.")
            raise

        except Exception:
            logger.exception(
                "Unexpected error inside Bingo game loop."
            )

        await asyncio.sleep(0.25)


# ============================================================
# WEB APP
# ============================================================

async def index_handler(request):
    index_file = WEB_DIR / "index.html"

    if not index_file.exists():
        return web.Response(
            text=(
                "<h1>Rodas Friend Zone</h1>"
                "<p>Web app is not available.</p>"
            ),
            content_type="text/html",
        )

    return web.FileResponse(index_file)


async def health_handler(request):

    return web.json_response(
        {
            "status": "ok",
            "game_count": len(games),
            "webhook_url": WEBHOOK_URL,
        }
    )


async def state_handler(request):

    user_id = get_user_id(request)

    game = get_current_game()

    return web.json_response(
        game.serialize(user_id)
    )


async def select_handler(request):

    try:
        data = await request.json()
    except Exception:
        return web.json_response(
            {
                "ok": False,
                "error": "Invalid JSON.",
            },
            status=400,
        )

    user_id = get_user_id(request)

    cartela_number = data.get(
        "cartela",
        data.get(
            "cartela_number",
            data.get("number"),
        ),
    )

    name = data.get("name")

    async with game_lock:

        game = get_current_game()

        if game.phase != "selection":
            return web.json_response(
                {
                    "ok": False,
                    "error": "Cartela selection is closed.",
                    "state": game.serialize(user_id),
                },
                status=400,
            )

        game.add_player(
            user_id,
            name,
        )

        success, message = game.select_cartela(
            user_id,
            cartela_number,
        )

        return web.json_response(
            {
                "ok": success,
                "message": message,
                "state": game.serialize(user_id),
            },
            status=200 if success else 400,
        )


async def proceed_handler(request):

    try:
        data = await request.json()
    except Exception:
        data = {}

    user_id = get_user_id(request)

    if data.get("user_id"):
        user_id = str(data["user_id"])

    async with game_lock:

        game = get_current_game()

        success = game.proceed(user_id)

        return web.json_response(
            {
                "ok": success,
                "state": game.serialize(user_id),
            },
            status=200 if success else 400,
        )


async def leave_handler(request):

    try:
        data = await request.json()
    except Exception:
        data = {}

    user_id = get_user_id(request)

    if data.get("user_id"):
        user_id = str(data["user_id"])

    async with game_lock:

        game = get_current_game()

        game.leave(user_id)

        return web.json_response(
            {
                "ok": True,
                "state": game.serialize(user_id),
            }
        )


async def claim_bingo_handler(request):

    user_id = get_user_id(request)

    async with game_lock:

        game = get_current_game()

        board = game.boards.get(user_id)

        if not board:
            return web.json_response(
                {
                    "ok": False,
                    "error": "You do not have a cartela.",
                },
                status=400,
            )

        if game.phase != "playing":
            return web.json_response(
                {
                    "ok": False,
                    "error": "The game is not currently playing.",
                    "state": game.serialize(user_id),
                },
                status=400,
            )

        if check_pattern(
            board,
            game.called_numbers,
        ):

            patterns = winning_patterns_for_board(
                board,
                game.called_numbers,
            )

            game.winner_user_id = user_id

            game.winner = game.player_names.get(
                user_id,
                user_id,
            )

            game.winner_cartela = game.players.get(
                user_id,
            )

            game.winner_pattern = (
                patterns[0]
                if patterns
                else None
            )

            game.prize = round(
                game.total_bets * PRIZE_PERCENT,
                2,
            )

            game.phase = "result"

            game.result_started_at = now()

            return web.json_response(
                {
                    "ok": True,
                    "message": "BINGO!",
                    "state": game.serialize(user_id),
                }
            )

        return web.json_response(
            {
                "ok": False,
                "message": "Your cartela does not have Bingo yet.",
                "state": game.serialize(user_id),
            },
            status=400,
        )


# ============================================================
# TELEGRAM MENU
# ============================================================

async def configure_telegram_menu():

    commands = [
        BotCommand(
            command="start",
            description="Start Rodas Friend Zone",
        ),
        BotCommand(
            command="play",
            description="Play Bingo",
        ),
        BotCommand(
            command="deposit",
            description="Deposit",
        ),
        BotCommand(
            command="balance",
            description="Check balance",
        ),
        BotCommand(
            command="withdraw",
            description="Withdraw",
        ),
        BotCommand(
            command="transfer",
            description="Transfer",
        ),
        BotCommand(
            command="instruction",
            description="How to play",
        ),
        BotCommand(
            command="invite",
            description="Invite friends",
        ),
        BotCommand(
            command="support",
            description="Support",
        ),
    ]

    await bot.set_my_commands(
        commands=commands,
        scope=BotCommandScopeAllPrivateChats(),
    )

    await bot.set_chat_menu_button(
        menu_button=MenuButtonCommands()
    )

    logger.info(
        "Telegram private-chat menu commands configured successfully."
    )


# ============================================================
# TELEGRAM KEYBOARD
# ============================================================

def open_bingo_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎯 PLAY BINGO",
                    web_app=WebAppInfo(
                        url=RENDER_EXTERNAL_URL
                    ),
                )
            ]
        ]
    )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

@dp.message(Command("start"))
async def start_command(message: Message):

    name = (
        message.from_user.first_name
        if message.from_user
        else "Player"
    )

    await message.answer(
        f"👋 Welcome, {name}!\n\n"
        "🎯 <b>Rodas Friend Zone Bingo</b>\n\n"
        "Choose an option from the menu below "
        "or open Bingo to play.",
        reply_markup=open_bingo_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("play"))
async def play_command(message: Message):

    await message.answer(
        "🎯 <b>Rodas Friend Zone Bingo</b>\n\n"
        "Tap the button below to open the Bingo game.",
        reply_markup=open_bingo_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("bingo"))
async def bingo_command(message: Message):

    await message.answer(
        "🎯 <b>Bingo is ready!</b>\n\n"
        "Tap below to enter the game.",
        reply_markup=open_bingo_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("deposit"))
async def deposit_command(message: Message):

    await message.answer(
        "💰 <b>Deposit</b>\n\n"
        "Deposit functionality will be available here.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("balance"))
async def balance_command(message: Message):

    await message.answer(
        "💳 <b>Your Balance</b>\n\n"
        "Your current balance is being prepared.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("withdraw"))
async def withdraw_command(message: Message):

    await message.answer(
        "💸 <b>Withdraw</b>\n\n"
        "Withdrawal functionality will be available here.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("transfer"))
async def transfer_command(message: Message):

    await message.answer(
        "🔄 <b>Transfer</b>\n\n"
        "Transfer functionality will be available here.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("instruction"))
async def instruction_command(message: Message):

    await message.answer(
        "📖 <b>How to Play</b>\n\n"
        "1️⃣ Open Bingo.\n"
        "2️⃣ Choose your cartela.\n"
        "3️⃣ Wait for the game to start.\n"
        "4️⃣ Watch the numbers being called.\n"
        "5️⃣ Complete a winning pattern.\n"
        "6️⃣ Press BINGO when you have a winning card.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("invite"))
async def invite_command(message: Message):

    await message.answer(
        "👥 <b>Invite Friends</b>\n\n"
        "Invite your friends to Rodas Friend Zone "
        "and play Bingo together.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("support"))
async def support_command(message: Message):

    await message.answer(
        "🆘 <b>Support</b>\n\n"
        "For support, please contact the Rodas Friend Zone administrator.",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

async def telegram_webhook(request):

    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    if WEBHOOK_SECRET:

        if received_secret != WEBHOOK_SECRET:

            logger.warning(
                "Rejected webhook request with invalid secret."
            )

            return web.json_response(
                {
                    "ok": False,
                    "error": "Unauthorized",
                },
                status=403,
            )

    try:
        data = await request.json()

    except Exception:

        logger.exception(
            "Telegram webhook received invalid JSON."
        )

        return web.json_response(
            {
                "ok": False,
                "error": "Invalid JSON",
            },
            status=400,
        )

    try:

        update = Update.model_validate(data)

        await dp.feed_update(
            bot,
            update,
        )

        logger.info(
            "Telegram update received successfully."
        )

        return web.json_response(
            {
                "ok": True,
            }
        )

    except Exception:

        logger.exception(
            "Error while processing Telegram webhook update."
        )

        return web.json_response(
            {
                "ok": False,
            },
            status=500,
        )


# ============================================================
# WEBHOOK SETUP
# ============================================================

async def setup_webhook():

    logger.info(
        "Setting Telegram webhook to %s",
        WEBHOOK_URL,
    )

    await bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=False,
    )

    info = await bot.get_webhook_info()

    logger.info(
        "Telegram webhook status: url=%s pending=%s last_error=%s",
        info.url,
        info.pending_update_count,
        info.last_error_message,
    )

    if info.url != WEBHOOK_URL:

        raise RuntimeError(
            "Telegram webhook verification failed. "
            f"Expected {WEBHOOK_URL}, got {info.url!r}"
        )

    logger.info(
        "Telegram webhook verified successfully."
    )


# ============================================================
# STARTUP
# ============================================================

async def on_startup(app):

    global game_loop_task

    logger.info(
        "Rodas Friend Zone Bingo starting..."
    )

    await configure_telegram_menu()

    await setup_webhook()

    game = get_current_game()

    logger.info(
        "Current Bingo game: %s",
        game.game_number,
    )

    game_loop_task = asyncio.create_task(
        game_loop()
    )

    logger.info(
        "Rodas Friend Zone Bingo is ready."
    )


# ============================================================
# CLEANUP
# ============================================================

async def on_cleanup(app):

    global game_loop_task

    logger.info(
        "Rodas Friend Zone Bingo shutting down..."
    )

    if game_loop_task:

        game_loop_task.cancel()

        try:
            await game_loop_task

        except asyncio.CancelledError:
            pass

        game_loop_task = None

    # IMPORTANT:
    # Do NOT delete the Telegram webhook on shutdown.
    logger.info(
        "Telegram webhook was intentionally kept configured."
    )

    await bot.session.close()

    logger.info(
        "Rodas Friend Zone Bingo shutdown complete."
    )


# ============================================================
# CREATE APPLICATION
# ============================================================

def create_app():

    application = web.Application()

    application.router.add_get(
        "/",
        index_handler,
    )

    application.router.add_get(
        "/health",
        health_handler,
    )

    application.router.add_get(
        "/api/state",
        state_handler,
    )

    application.router.add_post(
        "/api/select",
        select_handler,
    )

    application.router.add_post(
        "/api/proceed",
        proceed_handler,
    )

    application.router.add_post(
        "/api/leave",
        leave_handler,
    )

    application.router.add_post(
        "/api/claim-bingo",
        claim_bingo_handler,
    )

    application.router.add_post(
        WEBHOOK_PATH,
        telegram_webhook,
    )

    application.on_startup.append(
        on_startup
    )

    application.on_cleanup.append(
        on_cleanup
    )

    return application


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    logger.info(
        "Starting Rodas Friend Zone Bingo on port %s",
        PORT,
    )

    app = create_app()

    web.run_app(
        app,
        host="0.0.0.0",
        port=PORT,
    )
