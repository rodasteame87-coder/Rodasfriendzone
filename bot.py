import asyncio
import logging
import os
import random
import time
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MenuButtonCommands,
    Update,
    WebAppInfo,
)

from config import BOT_TOKEN


# =========================================================
# CONFIGURATION
# =========================================================

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
)

WEBHOOK_URL = f"{RENDER_EXTERNAL_URL.rstrip('/')}{WEBHOOK_PATH}"


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("rodas-friend-zone-bingo")


# =========================================================
# GLOBALS
# =========================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

games = {}

game_lock = asyncio.Lock()

next_game_number = 1


# =========================================================
# HELPERS
# =========================================================

def now():
    return time.time()


def get_user_id(request):
    return str(
        request.headers.get("X-User-ID")
        or request.headers.get("X-Telegram-User-ID")
        or "guest"
    )


def safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def generate_game_number():
    global next_game_number

    number = next_game_number
    next_game_number += 1

    return number


def bingo_column(number):
    if 1 <= number <= 15:
        return "B"

    if 16 <= number <= 30:
        return "I"

    if 31 <= number <= 45:
        return "N"

    if 46 <= number <= 60:
        return "G"

    if 61 <= number <= 75:
        return "O"

    return ""


def generate_cartela():
    """
    Standard 5x5 Bingo card.

    B: 1-15
    I: 16-30
    N: 31-45
    G: 46-60
    O: 61-75

    Center is FREE.
    """

    columns = [
        random.sample(range(1, 16), 5),
        random.sample(range(16, 31), 5),
        random.sample(range(31, 46), 5),
        random.sample(range(46, 61), 5),
        random.sample(range(61, 76), 5),
    ]

    board = []

    for row in range(5):
        current_row = []

        for column in range(5):
            if row == 2 and column == 2:
                current_row.append(0)
            else:
                current_row.append(columns[column][row])

        board.append(current_row)

    return board


def flatten_board(board):
    return [
        value
        for row in board
        for value in row
        if value != 0
    ]


def check_pattern(board, called_numbers, pattern):
    called = set(called_numbers)

    if pattern == "row_1":
        positions = [(0, c) for c in range(5)]

    elif pattern == "row_2":
        positions = [(1, c) for c in range(5)]

    elif pattern == "row_3":
        positions = [(2, c) for c in range(5)]

    elif pattern == "row_4":
        positions = [(3, c) for c in range(5)]

    elif pattern == "row_5":
        positions = [(4, c) for c in range(5)]

    elif pattern == "column_b":
        positions = [(r, 0) for r in range(5)]

    elif pattern == "column_i":
        positions = [(r, 1) for r in range(5)]

    elif pattern == "column_n":
        positions = [(r, 2) for r in range(5)]

    elif pattern == "column_g":
        positions = [(r, 3) for r in range(5)]

    elif pattern == "column_o":
        positions = [(r, 4) for r in range(5)]

    elif pattern == "diagonal_down":
        positions = [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4)]

    elif pattern == "diagonal_up":
        positions = [(4, 0), (3, 1), (2, 2), (1, 3), (0, 4)]

    else:
        return False

    for row, column in positions:
        value = board[row][column]

        if value == 0:
            continue

        if value not in called:
            return False

    return True


def winning_patterns_for_board(board, called_numbers):
    patterns = [
        "row_1",
        "row_2",
        "row_3",
        "row_4",
        "row_5",
        "column_b",
        "column_i",
        "column_n",
        "column_g",
        "column_o",
        "diagonal_down",
        "diagonal_up",
    ]

    return [
        pattern
        for pattern in patterns
        if check_pattern(board, called_numbers, pattern)
    ]


# =========================================================
# BINGO GAME
# =========================================================

class BingoGame:
    def __init__(self, game_number):
        self.game_number = game_number

        self.phase = "selection"

        self.created_at = now()

        # -------------------------------------------------
        # Shared 30-second timer
        # -------------------------------------------------

        self.selection_end = now() + SELECTION_SECONDS
        self.start_end = None

        # -------------------------------------------------
        # Players
        # -------------------------------------------------

        self.player_cartelas = {}
        self.player_names = {}

        self.left_users = set()
        self.proceeded_users = set()

        # -------------------------------------------------
        # Cartela boards
        # -------------------------------------------------

        self.cartela_boards = {}

        # -------------------------------------------------
        # Bingo drawing
        # -------------------------------------------------

        self.call_order = []
        self.called_numbers = []

        self.current_call = None
        self.last_call = None

        self.draw_task = None

        # -------------------------------------------------
        # Winner
        # -------------------------------------------------

        self.winner_user_id = None
        self.winner_name = None
        self.winning_pattern = None

        self.result_end = None

        self.finished_at = None

        # -------------------------------------------------
        # Prize
        # -------------------------------------------------

        self.total_bets = 0
        self.prize = 0

    # =====================================================
    # PLAYER COUNT
    # =====================================================

    @property
    def player_count(self):
        return len(self.player_cartelas)

    # =====================================================
    # CARTELA COUNT
    # =====================================================

    @property
    def taken_cartelas(self):
        return set(self.player_cartelas.values())

    # =====================================================
    # TIMER
    # =====================================================

    def selection_remaining(self):
        if self.phase != "selection":
            return 0

        return max(
            0,
            int(self.selection_end - now())
        )

    def start_remaining(self):
        if self.phase != "starting":
            return 0

        if self.start_end is None:
            return 0

        return max(
            0,
            int(self.start_end - now())
        )

    # =====================================================
    # START TIMER
    # =====================================================

    def ensure_start_timer_locked(self):
        self.start_end = self.selection_end

        logger.info(
            "Game %s: shared countdown deadline set. "
            "Selection and start timers are identical.",
            self.game_number,
        )

        return self.start_end

    # =====================================================
    # START SELECTION AGAIN
    # =====================================================

    def start_selection_locked(self):
        self.phase = "selection"

        self.selection_end = now() + SELECTION_SECONDS
        self.start_end = None

        self.player_cartelas.clear()
        self.player_names.clear()
        self.proceeded_users.clear()

        self.call_order.clear()
        self.called_numbers.clear()

        self.current_call = None
        self.last_call = None

        self.winner_user_id = None
        self.winner_name = None
        self.winning_pattern = None

        self.result_end = None
        self.finished_at = None

        self.total_bets = 0
        self.prize = 0

        logger.info(
            "Game %s: selection restarted.",
            self.game_number,
        )

    # =====================================================
    # START PLAYING
    # =====================================================

    def start_playing_locked(self):
        if len(self.player_cartelas) == 0:
            self.start_selection_locked()
            return False

        self.phase = "playing"

        self.start_end = None

        self.call_order = list(range(1, 76))
        random.shuffle(self.call_order)

        self.called_numbers.clear()

        self.current_call = None
        self.last_call = None

        self.total_bets = len(self.player_cartelas) * BET_AMOUNT

        self.prize = round(
            self.total_bets * PRIZE_PERCENT,
            2,
        )

        logger.info(
            "Game %s started with %s players. "
            "Total bets=%s prize=%s",
            self.game_number,
            len(self.player_cartelas),
            self.total_bets,
            self.prize,
        )

        return True

    # =====================================================
    # ADD PLAYER
    # =====================================================

    def add_player(self, user_id, name="Player"):
        user_id = str(user_id)

        self.left_users.discard(user_id)

        self.player_names[user_id] = name

    # =====================================================
    # SELECT CARTELA
    # =====================================================

    def select_cartela(self, user_id, cartela):
        user_id = str(user_id)

        if self.phase != "selection":
            return False, "Cartela selection is closed."

        if self.selection_remaining() <= 0:
            return False, "Cartela selection time has ended."

        cartela = safe_int(cartela)

        if cartela is None:
            return False, "Invalid Cartela."

        if not (
            CARTELA_MIN <= cartela <= CARTELA_MAX
        ):
            return False, "Invalid Cartela."

        current = self.player_cartelas.get(user_id)

        if current == cartela:
            return False, "Cartela already selected."

        owner = None

        for player_id, selected in self.player_cartelas.items():
            if selected == cartela:
                owner = player_id
                break

        if owner is not None and owner != user_id:
            return False, "This Cartela is already taken."

        if current is not None:
            self.player_cartelas.pop(user_id, None)

        self.player_cartelas[user_id] = cartela

        if cartela not in self.cartela_boards:
            self.cartela_boards[cartela] = generate_cartela()

        self.add_player(
            user_id,
            self.player_names.get(user_id, "Player"),
        )

        return True, "Cartela selected."

    # =====================================================
    # DESELECT CARTELA
    # =====================================================

    def deselect_cartela(self, user_id):
        user_id = str(user_id)

        if self.phase != "selection":
            return False, "Cartela selection is closed."

        if self.selection_remaining() <= 0:
            return False, "Cartela selection time has ended."

        if user_id not in self.player_cartelas:
            self.proceeded_users.discard(user_id)

            return True, "No Cartela selected."

        old_cartela = self.player_cartelas.pop(
            user_id,
            None,
        )

        self.proceeded_users.discard(user_id)

        logger.info(
            "Game %s: user %s deselected Cartela %s.",
            self.game_number,
            user_id,
            old_cartela,
        )

        return True, "Cartela deselected."

    # =====================================================
    # PROCEED
    # =====================================================

    def proceed(self, user_id):
        user_id = str(user_id)

        if self.phase != "selection":
            return False, "You cannot proceed now."

        if self.selection_remaining() <= 0:
            return False, "Selection time has ended."

        if user_id not in self.player_cartelas:
            return False, "Please choose a Cartela first."

        self.proceeded_users.add(user_id)

        return True, "Proceed accepted."

    # =====================================================
    # LEAVE
    # =====================================================

    def leave(self, user_id):
        user_id = str(user_id)

        if self.phase == "result":
            return False, "The game has already finished."

        if self.phase == "finished":
            return False, "The game has already finished."

        old_cartela = self.player_cartelas.pop(
            user_id,
            None,
        )

        self.proceeded_users.discard(user_id)

        self.left_users.add(user_id)

        if old_cartela is not None:
            logger.info(
                "Game %s: user %s left. "
                "Cartela %s is now free.",
                self.game_number,
                user_id,
                old_cartela,
            )

        if (
            self.phase in ("selection", "starting")
            and len(self.player_cartelas) == 0
        ):
            self.start_selection_locked()

        return True, "You left the game."

    # =====================================================
    # DRAW NUMBER
    # =====================================================

    def draw_number_locked(self):
        if self.phase != "playing":
            return None

        if not self.call_order:
            self.phase = "result"
            self.result_end = now() + RESULT_SECONDS
            self.finished_at = now()

            return None

        number = self.call_order.pop(0)

        self.last_call = self.current_call
        self.current_call = number

        self.called_numbers.append(number)

        logger.info(
            "Game %s: called number %s.",
            self.game_number,
            number,
        )

        return number

    # =====================================================
    # CHECK WINNER
    # =====================================================

    def check_winner_locked(self):
        if self.phase != "playing":
            return None

        for user_id, cartela in list(
            self.player_cartelas.items()
        ):
            board = self.cartela_boards.get(cartela)

            if not board:
                continue

            patterns = winning_patterns_for_board(
                board,
                self.called_numbers,
            )

            if patterns:
                self.winner_user_id = user_id

                self.winner_name = self.player_names.get(
                    user_id,
                    "Player",
                )

                self.winning_pattern = patterns[0]

                self.phase = "result"

                self.result_end = now() + RESULT_SECONDS

                self.finished_at = now()

                logger.info(
                    "Game %s: winner=%s cartela=%s pattern=%s",
                    self.game_number,
                    user_id,
                    cartela,
                    self.winning_pattern,
                )

                return user_id

        return None

    # =====================================================
    # SERIALIZE STATE
    # =====================================================

    def state_for(self, user_id):
        user_id = str(user_id)

        selected = self.player_cartelas.get(user_id)

        if self.phase == "selection":
            remaining = self.selection_remaining()

        elif self.phase == "starting":
            remaining = self.start_remaining()

        else:
            remaining = 0

        selected_board = None

        if selected is not None:
            selected_board = self.cartela_boards.get(
                selected
            )

        taken = sorted(
            self.taken_cartelas
        )

        winner_cartela = None

        if self.winner_user_id is not None:
            winner_cartela = self.player_cartelas.get(
                self.winner_user_id
            )

        return {
            "success": True,

            "game_number": self.game_number,

            "phase": self.phase,

            "bet": BET_AMOUNT,
            "bet_amount": BET_AMOUNT,

            "players": len(self.player_cartelas),
            "player_count": len(self.player_cartelas),

            "total_bets": self.total_bets,

            "prize": self.prize,
            "derash": self.prize,

            "selection_remaining": remaining,

            "start_remaining": (
                self.start_remaining()
                if self.phase == "starting"
                else 0
            ),

            "remaining": remaining,

            "selected": selected,
            "my_cartela": selected,
            "cartela": selected,

            "taken_cartelas": taken,
            "taken": taken,

            "cartelas": taken,

            "available_cartelas": [
                number
                for number in range(
                    CARTELA_MIN,
                    CARTELA_MAX + 1,
                )
                if number not in self.taken_cartelas
                or number == selected
            ],

            "proceeded": user_id in self.proceeded_users,

            "current_call": self.current_call,

            "last_call": self.last_call,

            "called_numbers": list(
                self.called_numbers
            ),

            "call_count": len(
                self.called_numbers
            ),

            "board": selected_board,

            "winner_user_id": self.winner_user_id,

            "winner_name": self.winner_name,

            "winner_cartela": winner_cartela,

            "winning_pattern": self.winning_pattern,

            "result_remaining": (
                max(
                    0,
                    int(
                        self.result_end - now()
                    ),
                )
                if self.phase == "result"
                and self.result_end is not None
                else 0
            ),
        }


# =========================================================
# GET CURRENT GAME
# =========================================================

def get_current_game():
    if not games:
        game = BingoGame(
            generate_game_number()
        )

        games[game.game_number] = game

        return game

    active = [
        game
        for game in games.values()
        if game.phase
        in (
            "selection",
            "starting",
            "playing",
            "result",
        )
    ]

    if active:
        return sorted(
            active,
            key=lambda item: item.game_number,
        )[-1]

    game = BingoGame(
        generate_game_number()
    )

    games[game.game_number] = game

    return game


# =========================================================
# GAME LOOP
# =========================================================

async def game_loop():
    while True:
        try:
            async with game_lock:
                active_games = list(
                    games.values()
                )

                current_time = now()

                for game in active_games:

                    # -------------------------------------
                    # SELECTION
                    # -------------------------------------

                    if game.phase == "selection":

                        if current_time >= game.selection_end:

                            if (
                                len(game.player_cartelas)
                                == 0
                            ):
                                game.start_selection_locked()

                            else:
                                game.ensure_start_timer_locked()

                                game.phase = "starting"

                                logger.info(
                                    "Game %s entered starting phase.",
                                    game.game_number,
                                )

                    # -------------------------------------
                    # STARTING
                    # -------------------------------------

                    elif game.phase == "starting":

                        if (
                            game.start_end is not None
                            and current_time
                            >= game.start_end
                        ):

                            if (
                                len(game.player_cartelas)
                                == 0
                            ):
                                game.start_selection_locked()

                            else:
                                game.start_playing_locked()

                    # -------------------------------------
                    # PLAYING
                    # -------------------------------------

                    elif game.phase == "playing":

                        last_draw = getattr(
                            game,
                            "_last_draw_time",
                            0,
                        )

                        if (
                            current_time - last_draw
                            >= DRAW_INTERVAL
                        ):

                            game._last_draw_time = (
                                current_time
                            )

                            game.draw_number_locked()

                            game.check_winner_locked()

                    # -------------------------------------
                    # RESULT
                    # -------------------------------------

                    elif game.phase == "result":

                        if (
                            game.result_end is not None
                            and current_time
                            >= game.result_end
                        ):
                            game.phase = "finished"

                            logger.info(
                                "Game %s finished.",
                                game.game_number,
                            )

                    # -------------------------------------
                    # FINISHED
                    # -------------------------------------

                    elif game.phase == "finished":
                        pass

                # -----------------------------------------
                # Make sure there is always an active game.
                # -----------------------------------------

                active_games = [
                    game
                    for game in games.values()
                    if game.phase
                    in (
                        "selection",
                        "starting",
                        "playing",
                        "result",
                    )
                ]

                if not active_games:
                    game = BingoGame(
                        generate_game_number()
                    )

                    games[game.game_number] = game

                    logger.info(
                        "Created new Bingo game %s.",
                        game.game_number,
                    )

        except Exception:
            logger.exception(
                "Error inside Bingo game loop."
            )

        await asyncio.sleep(0.25)


# =========================================================
# HTTP ROUTES
# =========================================================

async def index(request):
    index_file = WEB_DIR / "index.html"

    if not index_file.exists():
        return web.Response(
            text="index.html not found.",
            status=404,
        )

    return web.FileResponse(
        index_file
    )


async def health(request):
    return web.json_response(
        {
            "status": "ok",
            "game_count": len(games),
        }
    )


# =========================================================
# STATE
# =========================================================

async def api_state(request):
    user_id = get_user_id(request)

    async with game_lock:
        game = get_current_game()

        state = game.state_for(user_id)

    return web.json_response(state)


# =========================================================
# SELECT CARTELA
# =========================================================

async def api_select(request):
    user_id = get_user_id(request)

    try:
        data = await request.json()
    except Exception:
        return web.json_response(
            {
                "success": False,
                "message": "Invalid request.",
            },
            status=400,
        )

    if "cartela" not in data:
        return web.json_response(
            {
                "success": False,
                "message": "Cartela is required.",
            },
            status=400,
        )

    requested_cartela = data.get("cartela")

    async with game_lock:
        game = get_current_game()

        # -------------------------------------------------
        # DESELECT
        # -------------------------------------------------

        if requested_cartela is None:

            success, message = (
                game.deselect_cartela(
                    user_id
                )
            )

            state = game.state_for(
                user_id
            )

            state["success"] = success
            state["message"] = message

            return web.json_response(
                state
            )

        # -------------------------------------------------
        # SELECT
        # -------------------------------------------------

        cartela = safe_int(
            requested_cartela
        )

        if cartela is None:
            state = game.state_for(
                user_id
            )

            state["success"] = False
            state["message"] = (
                "Invalid Cartela."
            )

            return web.json_response(
                state,
                status=400,
            )

        success, message = (
            game.select_cartela(
                user_id,
                cartela,
            )
        )

        state = game.state_for(
            user_id
        )

        state["success"] = success
        state["message"] = message

        if not success:
            return web.json_response(
                state,
                status=400,
            )

        return web.json_response(
            state
        )


# =========================================================
# PROCEED
# =========================================================

async def api_proceed(request):
    user_id = get_user_id(request)

    async with game_lock:
        game = get_current_game()

        success, message = game.proceed(
            user_id
        )

        if (
            success
            and game.phase == "selection"
            and game.selection_remaining() <= 0
        ):
            if len(game.player_cartelas) > 0:
                game.ensure_start_timer_locked()

                game.phase = "starting"

        state = game.state_for(
            user_id
        )

        state["success"] = success
        state["message"] = message

    if not success:
        return web.json_response(
            state,
            status=400,
        )

    return web.json_response(
        state
    )


# =========================================================
# LEAVE
# =========================================================

async def api_leave(request):
    user_id = get_user_id(request)

    async with game_lock:
        game = get_current_game()

        success, message = game.leave(
            user_id
        )

        state = game.state_for(
            user_id
        )

        state["success"] = success
        state["message"] = message

    if not success:
        return web.json_response(
            state,
            status=400,
        )

    return web.json_response(
        state
    )


# =========================================================
# CLAIM BINGO
# =========================================================

async def api_claim_bingo(request):
    user_id = get_user_id(request)

    async with game_lock:
        game = get_current_game()

        if game.phase != "playing":
            state = game.state_for(
                user_id
            )

            state["success"] = False
            state["message"] = (
                "Bingo cannot be claimed now."
            )

            return web.json_response(
                state,
                status=400,
            )

        cartela = game.player_cartelas.get(
            user_id
        )

        if cartela is None:
            state = game.state_for(
                user_id
            )

            state["success"] = False
            state["message"] = (
                "You do not have a Cartela."
            )

            return web.json_response(
                state,
                status=400,
            )

        board = game.cartela_boards.get(
            cartela
        )

        if board is None:
            state = game.state_for(
                user_id
            )

            state["success"] = False
            state["message"] = (
                "Cartela board not found."
            )

            return web.json_response(
                state,
                status=400,
            )

        patterns = winning_patterns_for_board(
            board,
            game.called_numbers,
        )

        if not patterns:
            state = game.state_for(
                user_id
            )

            state["success"] = False
            state["message"] = (
                "BINGO is not valid yet."
            )

            return web.json_response(
                state,
                status=400,
            )

        # -------------------------------------------------
        # Valid winner
        # -------------------------------------------------

        game.winner_user_id = user_id

        game.winner_name = game.player_names.get(
            user_id,
            "Player",
        )

        game.winning_pattern = patterns[0]

        game.phase = "result"

        game.result_end = (
            now() + RESULT_SECONDS
        )

        game.finished_at = now()

        state = game.state_for(
            user_id
        )

        state["success"] = True
        state["message"] = (
            "BINGO! You won the game."
        )

        return web.json_response(
            state
        )


# =========================================================
# TELEGRAM COMMAND KEYBOARD
# =========================================================

def open_bingo_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎮 OPEN BINGO",
                    web_app=WebAppInfo(
                        url=RENDER_EXTERNAL_URL
                    ),
                )
            ]
        ]
    )


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

@dp.message(CommandStart())
async def command_start(message: Message):
    user_id = str(
        message.from_user.id
    )

    name = (
        message.from_user.first_name
        or "Player"
    )

    async with game_lock:
        game = get_current_game()

        game.add_player(
            user_id,
            name,
        )

    keyboard = open_bingo_keyboard()

    await message.answer(
        (
            "🎉 Welcome to Rodas Friend Zone Bingo!\n\n"
            "💰 Bet: 10 BIRR\n"
            "🎫 Choose your Cartela from 1–100\n"
            "⏱ Selection time: 30 seconds\n\n"
            "Good luck! 🍀"
        ),
        reply_markup=keyboard,
    )


@dp.message(Command("play"))
async def command_play(message: Message):
    await message.answer(
        "Open Rodas Friend Zone Bingo:",
        reply_markup=open_bingo_keyboard(),
    )


@dp.message(Command("bingo"))
async def command_bingo(message: Message):
    await message.answer(
        "Open Rodas Friend Zone Bingo:",
        reply_markup=open_bingo_keyboard(),
    )


@dp.message(Command("deposit"))
async def command_deposit(message: Message):
    await message.answer(
        "💰 Deposit\n\n"
        "Use the Deposit option to add BIRR to your balance."
    )


@dp.message(Command("balance"))
async def command_balance(message: Message):
    await message.answer(
        "💰 Balance\n\n"
        "Your balance information is available in the Rodas Friend Zone menu."
    )


@dp.message(Command("withdraw"))
async def command_withdraw(message: Message):
    await message.answer(
        "💸 Withdraw\n\n"
        "Use the Withdraw option to request a withdrawal."
    )


@dp.message(Command("transfer"))
async def command_transfer(message: Message):
    await message.answer(
        "🔄 Transfer\n\n"
        "Use the Transfer option to send BIRR."
    )


@dp.message(Command("instruction"))
async def command_instruction(message: Message):
    await message.answer(
        "📖 How to play\n\n"
        "1. Open Bingo.\n"
        "2. Choose one Cartela from 1–100.\n"
        "3. Press PROCEED.\n"
        "4. Wait for the Bingo numbers to be called.\n"
        "5. Complete a winning pattern and press BINGO WIN."
    )


@dp.message(Command("invite"))
async def command_invite(message: Message):
    await message.answer(
        "👥 Invite Friends\n\n"
        "Share Rodas Friend Zone Bingo with your friends."
    )


@dp.message(Command("support"))
async def command_support(message: Message):
    await message.answer(
        "🆘 Support\n\n"
        "Please contact the Rodas Friend Zone support team for assistance."
    )


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

async def telegram_webhook(request):
    try:
        data = await request.json()

        update = Update.model_validate(
            data
        )

        await dp.feed_update(
            bot,
            update,
        )

        return web.json_response(
            {
                "ok": True,
            }
        )

    except Exception:
        logger.exception(
            "Telegram webhook error."
        )

        return web.json_response(
            {
                "ok": False,
            },
            status=500,
        )


# =========================================================
# APPLICATION SETUP
# =========================================================

async def on_startup(app):
    logger.info(
        "Starting Rodas Friend Zone Bingo..."
    )

    try:
        # -------------------------------------------------
        # RESTORE THE TELEGRAM BOT MENU
        # -------------------------------------------------

        await bot.set_my_commands(
            [
                BotCommand(
                    command="start",
                    description="Open Rodas Friend Zone Bingo",
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
        )

        # -------------------------------------------------
        # KEEP TELEGRAM MENU AS COMMANDS
        # -------------------------------------------------

        await bot.set_chat_menu_button(
            menu_button=MenuButtonCommands()
        )

        # -------------------------------------------------
        # WEBHOOK
        # -------------------------------------------------

        await bot.set_webhook(
            WEBHOOK_URL
        )

        logger.info(
            "Telegram webhook set to %s",
            WEBHOOK_URL,
        )

    except Exception:
        logger.exception(
            "Unable to configure Telegram."
        )

    game = BingoGame(
        generate_game_number()
    )

    games[game.game_number] = game

    logger.info(
        "Initial Bingo game created: %s",
        game.game_number,
    )

    app["game_loop_task"] = asyncio.create_task(
        game_loop()
    )


async def on_cleanup(app):
    logger.info(
        "Stopping Rodas Friend Zone Bingo..."
    )

    task = app.get(
        "game_loop_task"
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
            "Unable to delete Telegram webhook."
        )

    try:
        await bot.session.close()
    except Exception:
        logger.exception(
            "Unable to close Telegram bot session."
        )


# =========================================================
# CREATE APP
# =========================================================

def create_app():
    app = web.Application()

    # -----------------------------------------------------
    # Web frontend
    # -----------------------------------------------------

    app.router.add_get(
        "/",
        index,
    )

    # -----------------------------------------------------
    # Health
    # -----------------------------------------------------

    app.router.add_get(
        "/health",
        health,
    )

    # -----------------------------------------------------
    # Bingo API
    # -----------------------------------------------------

    app.router.add_get(
        "/api/state",
        api_state,
    )

    app.router.add_post(
        "/api/select",
        api_select,
    )

    app.router.add_post(
        "/api/proceed",
        api_proceed,
    )

    app.router.add_post(
        "/api/leave",
        api_leave,
    )

    app.router.add_post(
        "/api/claim-bingo",
        api_claim_bingo,
    )

    # -----------------------------------------------------
    # Telegram
    # -----------------------------------------------------

    app.router.add_post(
        WEBHOOK_PATH,
        telegram_webhook,
    )

    # -----------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------

    app.on_startup.append(
        on_startup
    )

    app.on_cleanup.append(
        on_cleanup
    )

    return app


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    application = create_app()

    web.run_app(
        application,
        host="0.0.0.0",
        port=PORT,
    )
