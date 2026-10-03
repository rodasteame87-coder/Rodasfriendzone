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

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://rodasfriendzone.onrender.com"
).rstrip("/")

WEBHOOK_PATH = "/telegram/webhook"

WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    "RodasFriendZone_927461_secret"
)

SELECTION_SECONDS = 30
START_SECONDS = 30
DRAW_INTERVAL_SECONDS = 3
RESULT_SECONDS = 10

BET_AMOUNT = 10


# ---------------------------------------------------------
# ONLINE STATUS
# ---------------------------------------------------------

ONLINE_TIMEOUT_SECONDS = 12

WEB_FOLDER = Path(__file__).resolve().parent / "web"


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("rodasfriendzone")


# =========================================================
# BOT / DISPATCHER
# =========================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# =========================================================
# VIRTUAL BALANCES
# =========================================================

balances = {}


def get_balance(user_id):
    return balances.setdefault(user_id, 1000)


# =========================================================
# SERVER-SIDE ONLINE USERS
# =========================================================
#
# user_id -> last heartbeat time
#
# This is intentionally stored in memory for now.
# It works while the Render instance is running.
#
# A future database can make this persistent across restarts
# and multiple server instances.
# =========================================================

user_last_seen = {}


def mark_user_online(user_id):
    """
    Record that this user is currently active.
    """
    if not user_id:
        return

    user_last_seen[user_id] = time.monotonic()


def is_user_online(user_id):
    """
    Return True if the user's last heartbeat is recent.
    """
    if not user_id:
        return False

    last_seen = user_last_seen.get(user_id)

    if last_seen is None:
        return False

    return (
        time.monotonic() - last_seen
        <= ONLINE_TIMEOUT_SECONDS
    )


def online_user_count():
    """
    Return the number of users currently considered online.
    """
    now = time.monotonic()

    return sum(
        1
        for last_seen in user_last_seen.values()
        if now - last_seen <= ONLINE_TIMEOUT_SECONDS
    )


def cleanup_offline_users():
    """
    Remove users whose heartbeat has expired.
    """
    now = time.monotonic()

    offline_users = [
        user_id
        for user_id, last_seen in user_last_seen.items()
        if now - last_seen > ONLINE_TIMEOUT_SECONDS
    ]

    for user_id in offline_users:
        user_last_seen.pop(user_id, None)


def game_player_online_status():
    """
    Return server-side online/offline status for players
    currently participating in the Bingo game.

    Example:
        {
            "12345": True,
            "67890": False
        }
    """
    return {
        user_id: is_user_online(user_id)
        for user_id in game.player_cartelas.keys()
    }


# =========================================================
# BINGO CARDS
# =========================================================

def generate_card(number):
    rng = random.Random(number)

    columns = [
        list(range(1, 16)),
        list(range(16, 31)),
        list(range(31, 46)),
        list(range(46, 61)),
        list(range(61, 76)),
    ]

    card = []

    for row in range(5):
        current_row = []

        for col in range(5):

            if col == 2 and row == 2:
                current_row.append(0)

            else:
                current_row.append(None)

        card.append(current_row)

    for col in range(5):

        values = columns[col][:]
        rng.shuffle(values)

        for row in range(5):

            if row == 2 and col == 2:
                continue

            card[row][col] = values.pop()

    return card


CARTELA_CARDS = {
    number: generate_card(number)
    for number in range(1, 101)
}


# =========================================================
# WINNING PATTERNS
# =========================================================
#
# Coordinates use:
#
# row 0 = top
# row 4 = bottom
# col 0 = left
# col 4 = right
#
# The FREE center square is (2, 2).
#
# The following 8 patterns exactly match the patterns
# shown in the user's winning-pattern screenshot.
# =========================================================

WINNING_PATTERNS = {

    # -----------------------------------------------------
    # 1. HORIZONTAL LINE
    # -----------------------------------------------------
    #
    # Any complete row.
    #
    "Horizontal line": [
        [(row, col) for col in range(5)]
        for row in range(5)
    ],

    # -----------------------------------------------------
    # 2. VERTICAL LINE
    # -----------------------------------------------------
    #
    # Any complete column.
    #
    "Vertical line": [
        [(row, col) for row in range(5)]
        for col in range(5)
    ],

    # -----------------------------------------------------
    # 3. DIAGONAL
    # -----------------------------------------------------
    #
    # Top-left -> bottom-right.
    #
    "Diagonal": [
        [
            (0, 0),
            (1, 1),
            (2, 2),
            (3, 3),
            (4, 4),
        ]
    ],

    # -----------------------------------------------------
    # 4. ANTI-DIAGONAL
    # -----------------------------------------------------
    #
    # Top-right -> bottom-left.
    #
    "Anti-diagonal": [
        [
            (0, 4),
            (1, 3),
            (2, 2),
            (3, 1),
            (4, 0),
        ]
    ],

    # -----------------------------------------------------
    # 5. FOUR CORNERS
    # -----------------------------------------------------
    #
    # Four outside corners.
    #
    "Four corners": [
        [
            (0, 0),
            (0, 4),
            (4, 0),
            (4, 4),
        ]
    ],

    # -----------------------------------------------------
    # 6. CENTER FOUR
    # -----------------------------------------------------
    #
    # The four cells surrounding the FREE center.
    #
    # . . . . .
    # . X . X .
    # . . . . .
    # . X . X .
    # . . . . .
    #
    "Center four": [
        [
            (1, 1),
            (1, 3),
            (3, 1),
            (3, 3),
        ]
    ],

    # -----------------------------------------------------
    # 7. T CORNERS
    # -----------------------------------------------------
    #
    # Top-center
    # Left-center
    # Right-center
    # Bottom-center
    #
    # . . X . .
    # . . . . .
    # X . . . X
    # . . . . .
    # . . X . .
    #
    "T corners": [
        [
            (0, 2),
            (2, 0),
            (2, 4),
            (4, 2),
        ]
    ],

    # -----------------------------------------------------
    # 8. CENTER T
    # -----------------------------------------------------
    #
    # The four cells immediately around the FREE center.
    #
    # . . . . .
    # . . X . .
    # . X . X .
    # . . X . .
    # . . . . .
    #
    "Center T": [
        [
            (1, 2),
            (2, 1),
            (2, 3),
            (3, 2),
        ]
    ],
}


# =========================================================
# BINGO HELPERS
# =========================================================

def get_bingo_pattern(card, marked_numbers):
    """
    Check whether the player's marked numbers complete
    ANY of the allowed winning patterns.

    Returns the exact pattern name if successful.

    Returns None when no valid pattern is complete.
    """

    marked = set(marked_numbers)

    # -----------------------------------------------------
    # FREE CENTER
    # -----------------------------------------------------
    #
    # The center square is always considered marked.
    #
    marked.add(0)

    # -----------------------------------------------------
    # CHECK ALL WINNING PATTERNS
    # -----------------------------------------------------

    for pattern_name, pattern_variants in WINNING_PATTERNS.items():

        for pattern in pattern_variants:

            pattern_complete = True

            for row, col in pattern:

                value = card[row][col]

                # FREE center is automatically complete.
                if value == 0:
                    continue

                # Every other required square must have its
                # number marked by the player.
                if value not in marked:
                    pattern_complete = False
                    break

            if pattern_complete:
                return pattern_name

    return None


def card_numbers(card):
    result = []

    for row in card:

        for number in row:

            if number != 0:
                result.append(number)

    return result


# =========================================================
# GAME
# =========================================================

class BingoGame:

    def __init__(self):

        self.lock = asyncio.Lock()

        self.phase = "selection"

        self.game_number = 0

        self.selection_end = 0
        self.start_end = None
        self.result_end = None
        self.next_draw_at = None

        self.taken_cartelas = set()
        self.player_cartelas = {}
        self.proceeded_users = set()

        # Players who have left the current game.
        self.left_users = set()

        self.drawn_numbers = []
        self.current_number = None

        self.winner = None
        self.prize = 0

        self.blocked_users = set()
        self.wrong_claims = set()

        self.result_message = None

        self.start_selection_locked()

    # -----------------------------------------------------

    def start_selection_locked(self):

        self.phase = "selection"

        self.game_number += 1

        # THIS IS THE ONE AND ONLY COUNTDOWN DEADLINE
        self.selection_end = (
            time.monotonic() + SELECTION_SECONDS
        )

        # Starting countdown will use this SAME deadline.
        self.start_end = None

        self.result_end = None
        self.next_draw_at = None

        self.taken_cartelas.clear()
        self.player_cartelas.clear()
        self.proceeded_users.clear()

        self.left_users.clear()

        self.drawn_numbers.clear()
        self.current_number = None

        self.winner = None
        self.prize = 0

        self.blocked_users.clear()
        self.wrong_claims.clear()

        self.result_message = None

        logger.info(
            "Game %s: selection started",
            self.game_number,
        )

    # -----------------------------------------------------

    def selection_remaining(self):

        if self.phase != "selection":
            return 0

        return max(
            0,
            int(
                self.selection_end
                - time.monotonic()
            )
        )

    # -----------------------------------------------------

    def start_remaining(self):

        if self.start_end is None:
            return 0

        return max(
            0,
            int(
                self.start_end
                - time.monotonic()
            )
        )

    # -----------------------------------------------------

    def result_remaining(self):

        if self.result_end is None:
            return 0

        return max(
            0,
            int(
                self.result_end
                - time.monotonic()
            )
        )

    # -----------------------------------------------------
    # IMPORTANT:
    # THE START COUNTDOWN IS THE SAME COUNTDOWN AS
    # THE CARTELA SELECTION COUNTDOWN.
    #
    # We DO NOT create another 30-second timer.
    # -----------------------------------------------------

    def ensure_start_timer_locked(self):

        # Use the exact same deadline as Cartela selection.
        self.start_end = self.selection_end

        logger.info(
            "Game %s: shared countdown deadline set. "
            "Selection and start timers are identical.",
            self.game_number,
        )

        return self.start_end

    # -----------------------------------------------------

    def get_view_phase(self, user_id):

        if user_id in self.left_users:
            return self.phase

        if self.phase == "selection":

            if user_id in self.proceeded_users:
                return "starting"

            return "selection"

        return self.phase

    # -----------------------------------------------------

    def prize_for_players(self):

        players = len(
            self.player_cartelas
        )

        if players <= 0:
            return 0

        total = (
            players
            * BET_AMOUNT
        )

        return int(
            total * 0.75
        )

    # -----------------------------------------------------

    def creator_amount(self):

        players = len(
            self.player_cartelas
        )

        if players <= 0:
            return 0

        total = (
            players
            * BET_AMOUNT
        )

        return (
            total
            - self.prize_for_players()
        )

    # -----------------------------------------------------

    def start_playing_locked(self):

        self.phase = "playing"

        self.prize = (
            self.prize_for_players()
        )

        self.next_draw_at = (
            time.monotonic()
        )

        logger.info(
            "Game %s started with %s players. Prize=%s",
            self.game_number,
            len(self.player_cartelas),
            self.prize,
        )

    # -----------------------------------------------------

    def draw_number_locked(self):

        if self.phase != "playing":
            return None

        remaining = [
            number
            for number in range(1, 76)
            if number not in self.drawn_numbers
        ]

        if not remaining:

            self.finish_locked(
                winner=None,
                reason=(
                    "No winner — all 75 numbers were called."
                ),
            )

            return None

        number = random.choice(
            remaining
        )

        self.drawn_numbers.append(
            number
        )

        self.current_number = number

        logger.info(
            "Game %s called number %s",
            self.game_number,
            number,
        )

        if len(
            self.drawn_numbers
        ) >= 75:

            self.finish_locked(
                winner=None,
                reason=(
                    "No winner — all 75 numbers were called."
                ),
            )

        return number

    # -----------------------------------------------------

    def finish_locked(
        self,
        winner=None,
        reason=None,
    ):

        self.phase = "finished"

        self.winner = winner

        self.result_end = (
            time.monotonic()
            + RESULT_SECONDS
        )

        self.next_draw_at = None

        self.result_message = reason

    # -----------------------------------------------------

    def state_for_user(self, user_id):

        selected = (
            self.player_cartelas.get(
                user_id
            )
        )

        selected_card = (
            CARTELA_CARDS.get(
                selected
            )
            if selected
            else None
        )

        view_phase = (
            self.get_view_phase(
                user_id
            )
        )

        winner_data = None

        if self.winner:
            winner_data = dict(
                self.winner
            )

        return {
            "phase": self.phase,
            "view_phase": view_phase,

            "game": self.game_number,
            "game_number": self.game_number,

            "selection_remaining":
                self.selection_remaining(),

            "start_remaining":
                self.start_remaining(),

            "result_remaining":
                self.result_remaining(),

            "players":
                len(self.player_cartelas),

            "bet":
                BET_AMOUNT,

            "total_pool": (
                len(self.player_cartelas)
                * BET_AMOUNT
            ),

            "prize":
                self.prize,

            "creator_amount":
                self.creator_amount(),

            # -------------------------------------------------
            # CURRENT USER BALANCE
            # -------------------------------------------------

            "wallet":
                get_balance(user_id),

            # -------------------------------------------------
            # SERVER-SIDE ONLINE STATUS
            # -------------------------------------------------

            "online":
                is_user_online(user_id),

            "online_count":
                online_user_count(),

            "player_online_status":
                game_player_online_status(),

            # -------------------------------------------------
            # WINNING PATTERNS
            # -------------------------------------------------
            #
            # Sent to the frontend so the same pattern names
            # can be displayed there.
            #
            "winning_patterns": [
                "Horizontal line",
                "Vertical line",
                "Diagonal",
                "Anti-diagonal",
                "Four corners",
                "Center four",
                "T corners",
                "Center T",
            ],

            "taken":
                sorted(
                    self.taken_cartelas
                ),

            "taken_cartelas":
                sorted(
                    self.taken_cartelas
                ),

            "selected":
                selected,

            "my_cartela":
                selected,

            "cartela":
                selected,

            "card":
                selected_card,

            "called":
                list(
                    self.drawn_numbers
                ),

            "drawn_numbers":
                list(
                    self.drawn_numbers
                ),

            "current":
                self.current_number,

            "current_number":
                self.current_number,

            "winner":
                winner_data,

            "blocked":
                user_id in self.blocked_users,

            "wrong_claim":
                user_id in self.wrong_claims,

            "proceeded":
                user_id in self.proceeded_users,

            "left":
                user_id in self.left_users,

            "result_message":
                self.result_message,
        }


game = BingoGame()


# =========================================================
# TELEGRAM USER ID
# =========================================================

def telegram_user_id(message: Message):

    if not message.from_user:
        return "unknown"

    return str(
        message.from_user.id
    )


# =========================================================
# PLAY BUTTON
# =========================================================

def play_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎯 PLAY BINGO",
                    web_app=WebAppInfo(
                        url=(
                            RENDER_EXTERNAL_URL
                            + "/"
                        )
                    ),
                )
            ]
        ]
    )


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

@dp.message(CommandStart())
async def start_command(
    message: Message
):

    user_id = telegram_user_id(
        message
    )

    logger.info(
        "/start received from user %s",
        user_id,
    )

    await message.answer(
        "🎱 <b>Welcome to Rodas Friend Zone Bingo!</b>\n\n"
        "Choose an option from the Menu below "
        "or press <b>PLAY BINGO</b> to enter the game.",
        reply_markup=play_keyboard(),
        parse_mode="HTML",
    )


@dp.message(Command("play"))
async def play_command(
    message: Message
):

    user_id = telegram_user_id(
        message
    )

    logger.info(
        "/play received from user %s",
        user_id,
    )

    await message.answer(
        "🎯 <b>Rodas Friend Zone Bingo</b>\n\n"
        "Tap the button below to open the Bingo game.",
        reply_markup=play_keyboard(),
        parse_mode="HTML",
    )


@dp.message(Command("deposit"))
async def deposit_command(
    message: Message
):

    user_id = telegram_user_id(
        message
    )

    logger.info(
        "/deposit received from user %s",
        user_id,
    )

    await message.answer(
        "💰 <b>Deposit</b>\n\n"
        "Deposit service is being prepared.\n"
        "Your balance is currently virtual for testing.",
        parse_mode="HTML",
    )


@dp.message(Command("balance"))
async def balance_command(
    message: Message
):

    user_id = telegram_user_id(
        message
    )

    balance = get_balance(
        user_id
    )

    logger.info(
        "/balance received from user %s",
        user_id,
    )

    await message.answer(
        f"💰 <b>Your Balance</b>\n\n"
        f"Balance: <b>{balance} birr</b>",
        parse_mode="HTML",
    )


@dp.message(Command("withdraw"))
async def withdraw_command(
    message: Message
):

    user_id = telegram_user_id(
        message
    )

    logger.info(
        "/withdraw received from user %s",
        user_id,
    )

    await message.answer(
        "💸 <b>Withdraw</b>\n\n"
        "Withdrawal service is being prepared.",
        parse_mode="HTML",
    )


@dp.message(Command("transfer"))
async def transfer_command(
    message: Message
):

    user_id = telegram_user_id(
        message
    )

    logger.info(
        "/transfer received from user %s",
        user_id
    )

    await message.answer(
        "🔄 <b>Transfer</b>\n\n"
        "The transfer service is being prepared.",
        parse_mode="HTML",
    )


@dp.message(Command("instruction"))
async def instruction_command(
    message: Message
):

    await message.answer(
        "📖 <b>How to Play Rodas Friend Zone Bingo</b>\n\n"
        "1️⃣ Choose a Cartela from 1–100.\n"
        "2️⃣ Tap <b>PROCEED</b>.\n"
        "3️⃣ Wait for the game countdown.\n"
        "4️⃣ Numbers from 1–75 will be called.\n"
        "5️⃣ Complete any valid winning pattern.\n"
        "6️⃣ In Auto mode, called numbers are marked automatically.\n"
        "7️⃣ In Manual mode, mark the called numbers yourself.\n"
        "8️⃣ Tap <b>BINGO WIN</b> when you have a valid Bingo.\n\n"
        "🏆 Winning patterns:\n"
        "• Horizontal line\n"
        "• Vertical line\n"
        "• Diagonal\n"
        "• Anti-diagonal\n"
        "• Four corners\n"
        "• Center four\n"
        "• T corners\n"
        "• Center T\n\n"
        "💵 Each player enters with a 10 birr bet.",
        parse_mode="HTML",
    )


@dp.message(Command("invite"))
async def invite_command(
    message: Message
):

    await message.answer(
        "👥 <b>Invite Friends</b>\n\n"
        "Invite your friends to join Rodas Friend Zone "
        "and play Bingo together.",
        parse_mode="HTML",
    )


@dp.message(Command("support"))
async def support_command(
    message: Message
):

    await message.answer(
        "🆘 <b>Support</b>\n\n"
        "If you have a problem with the game, "
        "please contact the Rodas Friend Zone administrator.",
        parse_mode="HTML",
    )


# =========================================================
# BINGO API
# =========================================================

def get_api_user_id(request):

    return (
        request.headers.get(
            "X-User-ID"
        )
        or "demo-user"
    )


# =========================================================
# HEARTBEAT API
# =========================================================

async def api_heartbeat(request):

    user_id = get_api_user_id(
        request
    )

    mark_user_online(
        user_id
    )

    cleanup_offline_users()

    async with game.lock:

        state = game.state_for_user(
            user_id
        )

    return web.json_response(
        {
            "success": True,
            "online": True,
            "online_count":
                online_user_count(),
            **state,
        }
    )


# =========================================================
# STATE
# =========================================================

async def api_state(request):

    user_id = get_api_user_id(
        request
    )

    # Reading state also confirms that
    # the client is active.
    mark_user_online(
        user_id
    )

    cleanup_offline_users()

    async with game.lock:

        state = game.state_for_user(
            user_id
        )

    return web.json_response(
        state
    )


# =========================================================
# SELECT CARTELA
# =========================================================

async def api_select(request):

    user_id = get_api_user_id(
        request
    )

    mark_user_online(
        user_id
    )

    try:

        data = await request.json()

    except Exception:

        return web.json_response(
            {
                "success": False,
                "message":
                    "Invalid request.",
            },
            status=400,
        )

    cartela = data.get(
        "cartela"
    )

    try:

        cartela = int(
            cartela
        )

    except Exception:

        return web.json_response(
            {
                "success": False,
                "message":
                    "Invalid Cartela.",
            },
            status=400,
        )

    async with game.lock:

        if game.phase != "selection":

            return web.json_response(
                {
                    "success": False,
                    "message":
                        "Cartela selection is closed.",
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        if game.selection_remaining() <= 0:

            return web.json_response(
                {
                    "success": False,
                    "message":
                        "Selection time has ended.",
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        if user_id in game.proceeded_users:

            return web.json_response(
                {
                    "success": False,
                    "message":
                        "You already pressed PROCEED.",
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        if cartela < 1 or cartela > 100:

            return web.json_response(
                {
                    "success": False,
                    "message":
                        "Cartela must be between 1 and 100.",
                },
                status=400,
            )

        if cartela in game.taken_cartelas:

            return web.json_response(
                {
                    "success": False,
                    "message":
                        "That Cartela is already taken.",
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        old_cartela = (
            game.player_cartelas.get(
                user_id
            )
        )

        if old_cartela:

            game.taken_cartelas.discard(
                old_cartela
            )

        game.player_cartelas[user_id] = (
            cartela
        )

        game.taken_cartelas.add(
            cartela
        )

        game.left_users.discard(
            user_id
        )

        logger.info(
            "User %s selected Cartela %s",
            user_id,
            cartela,
        )

        return web.json_response(
            {
                "success": True,
                "message":
                    f"Cartela {cartela} selected.",
                **game.state_for_user(
                    user_id
                ),
            }
        )


# =========================================================
# PROCEED
# =========================================================

async def api_proceed(request):

    user_id = get_api_user_id(
        request
    )

    mark_user_online(
        user_id
    )

    async with game.lock:

        if game.phase != "selection":

            return web.json_response(
                {
                    "success": False,
                    "message":
                        "Selection is closed.",
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        cartela = (
            game.player_cartelas.get(
                user_id
            )
        )

        if not cartela:

            return web.json_response(
                {
                    "success": False,
                    "message":
                        "Choose a Cartela first.",
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        game.left_users.discard(
            user_id
        )

        game.proceeded_users.add(
            user_id
        )

        # IMPORTANT:
        # This does NOT create a new 30-second timer.
        # It uses the exact same deadline as Cartela selection.
        game.ensure_start_timer_locked()

        logger.info(
            "User %s pressed PROCEED with Cartela %s. "
            "Shared countdown remaining=%ss",
            user_id,
            cartela,
            game.start_remaining(),
        )

        return web.json_response(
            {
                "success": True,
                "message":
                    "You entered the game.",
                **game.state_for_user(
                    user_id
                ),
            }
        )


# =========================================================
# LEAVE GAME
# =========================================================

async def api_leave(request):

    user_id = get_api_user_id(
        request
    )

    mark_user_online(
        user_id
    )

    async with game.lock:

        # -------------------------------------------------
        # GAME IS FINISHED
        # -------------------------------------------------

        if game.phase == "finished":

            return web.json_response(
                {
                    "success": False,
                    "message":
                        "This game has already finished.",
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # USER IS ALREADY OUT
        # -------------------------------------------------

        if user_id in game.left_users:

            return web.json_response(
                {
                    "success": True,
                    "message":
                        "You already left this game.",
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # REMOVE PLAYER
        # -------------------------------------------------

        cartela = (
            game.player_cartelas.pop(
                user_id,
                None
            )
        )

        if cartela:

            game.taken_cartelas.discard(
                cartela
            )

        game.proceeded_users.discard(
            user_id
        )

        game.left_users.add(
            user_id
        )

        game.blocked_users.discard(
            user_id
        )

        game.wrong_claims.discard(
            user_id
        )

        logger.info(
            "User %s left Game %s during phase %s. "
            "Game continues.",
            user_id,
            game.game_number,
            game.phase,
        )

        return web.json_response(
            {
                "success": True,
                "message":
                    "You left the game. "
                    "The game will continue for other players.",
                **game.state_for_user(
                    user_id
                ),
            }
        )


# =========================================================
# CLAIM BINGO
# =========================================================

async def api_claim_bingo(request):

    user_id = get_api_user_id(
        request
    )

    mark_user_online(
        user_id
    )

    try:

        data = await request.json()

    except Exception:

        return web.json_response(
            {
                "success": False,
                "message":
                    "Invalid request.",
            },
            status=400,
        )

    marked = data.get(
        "marked",
        []
    )

    if not isinstance(
        marked,
        list
    ):

        marked = []

    try:

        marked = {
            int(number)
            for number in marked
        }

    except Exception:

        marked = set()

    async with game.lock:

        # -------------------------------------------------
        # PLAYER LEFT
        # -------------------------------------------------

        if user_id in game.left_users:

            return web.json_response(
                {
                    "success": False,
                    "message":
                        "You already left this game.",
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # BLOCKED PLAYER
        # -------------------------------------------------

        if user_id in game.blocked_users:

            return web.json_response(
                {
                    "success": False,
                    "wrong": True,
                    "message":
                        "Wrong Cartela — You lose. "
                        "Better luck next time!",
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # GAME NOT PLAYING
        # -------------------------------------------------

        if game.phase != "playing":

            return web.json_response(
                {
                    "success": False,
                    "message":
                        "The game is not currently "
                        "accepting Bingo claims.",
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # WINNER ALREADY EXISTS
        # -------------------------------------------------

        if game.winner:

            return web.json_response(
                {
                    "success": False,
                    "message":
                        "A winner has already been declared.",
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # PLAYER CARTELA
        # -------------------------------------------------

        cartela = (
            game.player_cartelas.get(
                user_id
            )
        )

        if not cartela:

            return web.json_response(
                {
                    "success": False,
                    "message":
                        "You do not have a Cartela."
                }
            )

        card = CARTELA_CARDS.get(
            cartela
        )

        if not card:

            return web.json_response(
                {
                    "success": False,
                    "message":
                        "Cartela not found."
                }
            )

        # -------------------------------------------------
        # FREE CENTER
        # -------------------------------------------------
        #
        # The center square is always considered marked.
        # -------------------------------------------------

        marked.add(0)

        valid_card_numbers = set(
            card_numbers(card)
        )

        # -------------------------------------------------
        # INVALID MARKS
        # -------------------------------------------------
        #
        # A player cannot claim using:
        #
        # - a number not on their Cartela
        # - a number that has not been called
        #
        # The FREE center (0) is allowed.
        # -------------------------------------------------

        invalid_marks = {
            number
            for number in marked
            if number != 0
            and (
                number not in valid_card_numbers
                or number not in game.drawn_numbers
            )
        }

        if invalid_marks:

            game.blocked_users.add(
                user_id
            )

            game.wrong_claims.add(
                user_id
            )

            logger.info(
                "User %s made invalid Bingo claim.",
                user_id,
            )

            return web.json_response(
                {
                    "success": False,
                    "wrong": True,
                    "message":
                        "Wrong Cartela — You lose. "
                        "Better luck next time!",
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # CHECK ALL 8 WINNING PATTERNS
        # -------------------------------------------------

        pattern = get_bingo_pattern(
            card,
            marked
        )

        # -------------------------------------------------
        # NO VALID PATTERN
        # -------------------------------------------------

        if not pattern:

            game.blocked_users.add(
                user_id
            )

            game.wrong_claims.add(
                user_id
            )

            logger.info(
                "User %s pressed Bingo without "
                "a valid Bingo pattern.",
                user_id,
            )

            return web.json_response(
                {
                    "success": False,
                    "wrong": True,
                    "message":
                        "Wrong Cartela — You lose. "
                        "Better luck next time!",
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # VALID WINNER
        # -------------------------------------------------

        winner_amount = game.prize

        game.winner = {
            "user_id":
                user_id,

            "cartela":
                cartela,

            "number":
                game.current_number,

            "pattern":
                pattern,

            "amount":
                winner_amount,
        }

        get_balance(
            user_id
        )

        balances[user_id] += (
            winner_amount
        )

        game.finish_locked(
            winner=game.winner
        )

        logger.info(
            "WINNER: user=%s cartela=%s "
            "pattern=%s prize=%s",
            user_id,
            cartela,
            pattern,
            winner_amount,
        )

        return web.json_response(
            {
                "success": True,
                "winner": True,
                "message":
                    "🎉 BINGO! You won!",
                **game.state_for_user(
                    user_id
                ),
            }
        )


# =========================================================
# INDEX PAGE
# =========================================================

async def handle_index(request):

    index_file = (
        WEB_FOLDER
        / "index.html"
    )

    if not index_file.exists():

        return web.Response(
            text=(
                "Rodas Friend Zone Bingo server is running, "
                "but web/index.html was not found."
            ),
            content_type="text/plain",
            status=404,
        )

    return web.FileResponse(
        index_file
    )


# =========================================================
# HEALTH CHECK
# =========================================================

async def health(request):

    cleanup_offline_users()

    return web.json_response(
        {
            "status":
                "ok",

            "service":
                "Rodas Friend Zone Bingo",

            "game":
                game.game_number,

            "phase":
                game.phase,

            "online_count":
                online_user_count(),
        }
    )


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

async def telegram_webhook(request):

    received_secret = (
        request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        )
    )

    if received_secret != WEBHOOK_SECRET:

        logger.warning(
            "Rejected Telegram webhook request: bad secret."
        )

        return web.Response(
            text="Forbidden",
            status=403,
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

        return web.json_response(
            {
                "ok": True
            }
        )

    except Exception:

        logger.exception(
            "Error processing Telegram webhook."
        )

        return web.json_response(
            {
                "ok": False
            },
            status=500,
        )


# =========================================================
# TELEGRAM MENU
# =========================================================

def telegram_commands():

    return [

        BotCommand(
            command="start",
            description=
                "Start Rodas Friend Zone",
        ),

        BotCommand(
            command="play",
            description=
                "Play Bingo",
        ),

        BotCommand(
            command="deposit",
            description=
                "Deposit",
        ),

        BotCommand(
            command="balance",
            description=
                "Check balance",
        ),

        BotCommand(
            command="withdraw",
            description=
                "Withdraw",
        ),

        BotCommand(
            command="transfer",
            description=
                "Transfer",
        ),

        BotCommand(
            command="instruction",
            description=
                "How to play",
        ),

        BotCommand(
            command="invite",
            description=
                "Invite friends",
        ),

        BotCommand(
            command="support",
            description=
                "Support",
        ),
    ]


async def setup_commands():

    commands = telegram_commands()

    await bot.set_my_commands(
        commands
    )

    await bot.set_chat_menu_button(
        menu_button=MenuButtonCommands()
    )

    logger.info(
        "Telegram menu commands and Menu button registered."
    )


# =========================================================
# TELEGRAM STARTUP SETUP
# =========================================================

async def setup_telegram():

    webhook_url = (
        RENDER_EXTERNAL_URL
        + WEBHOOK_PATH
    )

    max_attempts = 5

    for attempt in range(
        1,
        max_attempts + 1
    ):

        try:

            logger.info(
                "Telegram setup attempt %s/%s",
                attempt,
                max_attempts,
            )

            await setup_commands()

            logger.info(
                "Setting Telegram webhook: %s",
                webhook_url,
            )

            await bot.set_webhook(
                url=webhook_url,
                secret_token=WEBHOOK_SECRET,
                drop_pending_updates=False,
            )

            webhook_info = (
                await bot.get_webhook_info()
            )

            logger.info(
                "Telegram webhook active: %s",
                webhook_info.url,
            )

            if webhook_info.url != webhook_url:

                logger.warning(
                    "Telegram webhook URL does not match expected URL."
                )

            else:

                logger.info(
                    "Telegram webhook verified successfully."
                )

            logger.info(
                "Telegram setup completed successfully."
            )

            return True

        except Exception:

            logger.exception(
                "Telegram setup attempt %s failed.",
                attempt,
            )

            if attempt < max_attempts:

                wait_seconds = (
                    attempt * 3
                )

                logger.info(
                    "Retrying Telegram setup in %s seconds...",
                    wait_seconds,
                )

                await asyncio.sleep(
                    wait_seconds
                )

    logger.error(
        "Telegram setup failed after %s attempts.",
        max_attempts,
    )

    return False


# =========================================================
# GAME LOOP
# =========================================================

async def game_loop():

    while True:

        try:

            # Clean online status regularly.
            cleanup_offline_users()

            async with game.lock:

                now = time.monotonic()

                # -----------------------------------------
                # SELECTION
                # -----------------------------------------

                if game.phase == "selection":

                    if (
                        now
                        >= game.selection_end
                    ):

                        if (
                            len(
                                game.player_cartelas
                            )
                            == 0
                        ):

                            logger.info(
                                "Game %s: no players selected "
                                "a Cartela. Restarting selection.",
                                game.game_number,
                            )

                            game.start_selection_locked()

                        else:

                            # IMPORTANT:
                            # start_end is the SAME deadline
                            # that selection used.
                            game.ensure_start_timer_locked()

                            game.phase = "starting"

                            logger.info(
                                "Game %s: starting countdown "
                                "with %s player(s).",
                                game.game_number,
                                len(
                                    game.player_cartelas
                                ),
                            )

                # -----------------------------------------
                # STARTING
                # -----------------------------------------

                elif game.phase == "starting":

                    if (
                        game.start_end
                        is not None
                        and now
                        >= game.start_end
                    ):

                        if (
                            len(
                                game.player_cartelas
                            )
                            == 0
                        ):

                            logger.info(
                                "Game %s: all players left "
                                "before start. "
                                "Returning to selection.",
                                game.game_number,
                            )

                            game.start_selection_locked()

                        else:

                            game.start_playing_locked()

                # -----------------------------------------
                # PLAYING
                # -----------------------------------------

                elif game.phase == "playing":

                    if (
                        game.next_draw_at
                        is not None
                        and now
                        >= game.next_draw_at
                    ):

                        game.draw_number_locked()

                        if (
                            game.phase
                            == "playing"
                        ):

                            game.next_draw_at = (
                                now
                                + DRAW_INTERVAL_SECONDS
                            )

                # -----------------------------------------
                # FINISHED
                # -----------------------------------------

                elif game.phase == "finished":

                    if (
                        game.result_end
                        is not None
                        and now
                        >= game.result_end
                    ):

                        game.start_selection_locked()

        except Exception:

            logger.exception(
                "Game loop error."
            )

        await asyncio.sleep(
            0.25
        )


# =========================================================
# APP STARTUP
# =========================================================

async def on_startup(app):

    logger.info(
        "Starting Rodas Friend Zone..."
    )

    app["game_task"] = (
        asyncio.create_task(
            game_loop()
        )
    )

    telegram_ready = (
        await setup_telegram()
    )

    if not telegram_ready:

        logger.error(
            "Telegram setup did not complete successfully. "
            "The server will remain running."
        )


# =========================================================
# APP SHUTDOWN
# =========================================================

async def on_shutdown(app):

    logger.info(
        "Shutting down Rodas Friend Zone..."
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

        await bot.session.close()

    except Exception:

        logger.exception(
            "Could not close Telegram bot session cleanly."
        )


# =========================================================
# WEB APP
# =========================================================

app = web.Application()


app.router.add_get(
    "/",
    handle_index,
)

app.router.add_get(
    "/health",
    health,
)

app.router.add_post(
    WEBHOOK_PATH,
    telegram_webhook,
)

app.router.add_get(
    "/api/state",
    api_state,
)

app.router.add_post(
    "/api/heartbeat",
    api_heartbeat,
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


app.on_startup.append(
    on_startup
)

app.on_shutdown.append(
    on_shutdown
)


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    logger.info(
        "Rodas Friend Zone running on port %s",
        PORT,
    )

    web.run_app(
        app,
        host="0.0.0.0",
        port=PORT,
    )
