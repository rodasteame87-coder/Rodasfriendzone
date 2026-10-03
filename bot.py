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
    MenuButtonCommands,
    Message,
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
DRAW_INTERVAL_SECONDS = 3
RESULT_SECONDS = 10

BET_AMOUNT = 10

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
# BINGO HELPERS
# =========================================================

def get_bingo_pattern(card, marked_numbers):

    marked = set(marked_numbers)

    # FREE center is always marked.
    marked.add(0)

    # -----------------------------------------------------
    # ROWS
    # -----------------------------------------------------

    for row in range(5):

        if all(
            card[row][col] == 0
            or card[row][col] in marked
            for col in range(5)
        ):
            return f"Row {row + 1}"

    # -----------------------------------------------------
    # COLUMNS
    # -----------------------------------------------------

    for col in range(5):

        if all(
            card[row][col] == 0
            or card[row][col] in marked
            for row in range(5)
        ):

            letters = [
                "B",
                "I",
                "N",
                "G",
                "O",
            ]

            return f"Column {letters[col]}"

    # -----------------------------------------------------
    # MAIN DIAGONAL
    # -----------------------------------------------------

    if all(
        card[i][i] == 0
        or card[i][i] in marked
        for i in range(5)
    ):
        return "Main Diagonal"

    # -----------------------------------------------------
    # OTHER DIAGONAL
    # -----------------------------------------------------

    if all(
        card[i][4 - i] == 0
        or card[i][4 - i] in marked
        for i in range(5)
    ):
        return "Other Diagonal"

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

        # -------------------------------------------------
        # ONE SHARED COUNTDOWN DEADLINE
        # -------------------------------------------------

        self.selection_end = 0

        self.start_end = None

        self.result_end = None

        self.next_draw_at = None

        # -------------------------------------------------
        # PLAYERS
        # -------------------------------------------------

        self.taken_cartelas = set()

        self.player_cartelas = {}

        self.proceeded_users = set()

        self.left_users = set()

        # -------------------------------------------------
        # DRAW
        # -------------------------------------------------

        self.drawn_numbers = []

        self.current_number = None

        # -------------------------------------------------
        # RESULT
        # -------------------------------------------------

        self.winner = None

        self.prize = 0

        self.blocked_users = set()

        self.wrong_claims = set()

        self.result_message = None

        self.start_selection_locked()

    # =====================================================
    # START NEW SELECTION
    # =====================================================

    def start_selection_locked(self):

        self.phase = "selection"

        self.game_number += 1

        # -------------------------------------------------
        # THE ONLY 30-SECOND COUNTDOWN
        # -------------------------------------------------

        self.selection_end = (
            time.monotonic()
            + SELECTION_SECONDS
        )

        self.start_end = None

        self.result_end = None

        self.next_draw_at = None

        # -------------------------------------------------
        # RESET PLAYERS
        # -------------------------------------------------

        self.taken_cartelas.clear()

        self.player_cartelas.clear()

        self.proceeded_users.clear()

        self.left_users.clear()

        # -------------------------------------------------
        # RESET DRAW
        # -------------------------------------------------

        self.drawn_numbers.clear()

        self.current_number = None

        # -------------------------------------------------
        # RESET RESULT
        # -------------------------------------------------

        self.winner = None

        self.prize = 0

        self.blocked_users.clear()

        self.wrong_claims.clear()

        self.result_message = None

        logger.info(
            "Game %s: selection started.",
            self.game_number,
        )

    # =====================================================
    # SELECTION REMAINING
    # =====================================================

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

    # =====================================================
    # START REMAINING
    # =====================================================

    def start_remaining(self):

        # -------------------------------------------------
        # IMPORTANT:
        #
        # If a player already pressed PROCEED while the
        # game is still in selection, their "starting"
        # countdown uses THE SAME selection deadline.
        # -------------------------------------------------

        if self.start_end is not None:

            deadline = self.start_end

        else:

            deadline = self.selection_end

        return max(
            0,
            int(
                deadline
                - time.monotonic()
            )
        )

    # =====================================================
    # RESULT REMAINING
    # =====================================================

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

    # =====================================================
    # ENSURE SHARED START TIMER
    # =====================================================

    def ensure_start_timer_locked(self):

        # -------------------------------------------------
        # DO NOT CREATE ANOTHER 30 SECOND TIMER.
        #
        # Start deadline = selection deadline.
        # -------------------------------------------------

        self.start_end = self.selection_end

        logger.info(
            "Game %s: shared countdown deadline set.",
            self.game_number,
        )

        return self.start_end

    # =====================================================
    # VIEW PHASE FOR PLAYER
    # =====================================================

    def get_view_phase(self, user_id):

        # -------------------------------------------------
        # PLAYER LEFT
        # -------------------------------------------------

        if user_id in self.left_users:

            return "selection"

        # -------------------------------------------------
        # GLOBAL SELECTION
        # -------------------------------------------------

        if self.phase == "selection":

            # Player pressed PROCEED.
            #
            # Their frontend can show the Bingo starting
            # screen while the SAME countdown continues.
            if user_id in self.proceeded_users:

                return "starting"

            return "selection"

        return self.phase

    # =====================================================
    # PRIZE
    # =====================================================

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

        # 75% goes to the winner.
        return int(
            total * 0.75
        )

    # =====================================================
    # CREATOR AMOUNT
    # =====================================================

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

    # =====================================================
    # START PLAYING
    # =====================================================

    def start_playing_locked(self):

        self.phase = "playing"

        self.prize = (
            self.prize_for_players()
        )

        self.next_draw_at = (
            time.monotonic()
        )

        logger.info(
            "Game %s started with %s player(s). Prize=%s",
            self.game_number,
            len(self.player_cartelas),
            self.prize,
        )

    # =====================================================
    # DRAW NUMBER
    # =====================================================

    def draw_number_locked(self):

        if self.phase != "playing":
            return None

        remaining = [
            number
            for number in range(1, 76)
            if number not in self.drawn_numbers
        ]

        # -------------------------------------------------
        # ALL NUMBERS USED
        # -------------------------------------------------

        if not remaining:

            self.finish_locked(
                winner=None,
                reason=(
                    "No winner — all 75 numbers "
                    "were called."
                ),
            )

            return None

        # -------------------------------------------------
        # RANDOM DRAW
        # -------------------------------------------------

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

        # -------------------------------------------------
        # 75 NUMBERS COMPLETED
        # -------------------------------------------------

        if len(
            self.drawn_numbers
        ) >= 75:

            self.finish_locked(
                winner=None,
                reason=(
                    "No winner — all 75 numbers "
                    "were called."
                ),
            )

        return number

    # =====================================================
    # FINISH
    # =====================================================

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

    # =====================================================
    # STATE FOR USER
    # =====================================================

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

            # -------------------------------------------------
            # GAME PHASE
            # -------------------------------------------------

            "phase": self.phase,

            "view_phase": view_phase,

            "game": self.game_number,

            "game_number": self.game_number,

            # -------------------------------------------------
            # COUNTDOWNS
            # -------------------------------------------------

            "selection_remaining": (
                self.selection_remaining()
            ),

            "start_remaining": (
                self.start_remaining()
            ),

            "result_remaining": (
                self.result_remaining()
            ),

            # -------------------------------------------------
            # PLAYERS / MONEY
            # -------------------------------------------------

            "players": len(
                self.player_cartelas
            ),

            "bet": BET_AMOUNT,

            "total_pool": (
                len(
                    self.player_cartelas
                )
                * BET_AMOUNT
            ),

            "prize": self.prize,

            "creator_amount": (
                self.creator_amount()
            ),

            "wallet": get_balance(
                user_id
            ),

            # -------------------------------------------------
            # CARTELAS
            # -------------------------------------------------

            "taken": sorted(
                self.taken_cartelas
            ),

            "taken_cartelas": sorted(
                self.taken_cartelas
            ),

            "selected": selected,

            "my_cartela": selected,

            "cartela": selected,

            "card": selected_card,

            # -------------------------------------------------
            # DRAW
            # -------------------------------------------------

            "called": list(
                self.drawn_numbers
            ),

            "drawn_numbers": list(
                self.drawn_numbers
            ),

            "current": self.current_number,

            "current_number": (
                self.current_number
            ),

            # -------------------------------------------------
            # WINNER
            # -------------------------------------------------

            "winner": winner_data,

            # -------------------------------------------------
            # PLAYER STATUS
            # -------------------------------------------------

            "blocked": (
                user_id
                in self.blocked_users
            ),

            "wrong_claim": (
                user_id
                in self.wrong_claims
            ),

            "proceeded": (
                user_id
                in self.proceeded_users
            ),

            "left": (
                user_id
                in self.left_users
            ),

            "result_message": (
                self.result_message
            ),
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
# /START
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


# =========================================================
# /PLAY
# =========================================================

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


# =========================================================
# /DEPOSIT
# =========================================================

@dp.message(Command("deposit"))
async def deposit_command(
    message: Message
):

    await message.answer(
        "💰 <b>Deposit</b>\n\n"
        "Deposit service is being prepared.\n"
        "Your balance is currently virtual for testing.",
        parse_mode="HTML",
    )


# =========================================================
# /BALANCE
# =========================================================

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

    await message.answer(
        f"💰 <b>Your Balance</b>\n\n"
        f"Balance: <b>{balance} birr</b>",
        parse_mode="HTML",
    )


# =========================================================
# /WITHDRAW
# =========================================================

@dp.message(Command("withdraw"))
async def withdraw_command(
    message: Message
):

    await message.answer(
        "💸 <b>Withdraw</b>\n\n"
        "Withdrawal service is being prepared.",
        parse_mode="HTML",
    )


# =========================================================
# /TRANSFER
# =========================================================

@dp.message(Command("transfer"))
async def transfer_command(
    message: Message
):

    await message.answer(
        "🔄 <b>Transfer</b>\n\n"
        "The transfer service is being prepared.",
        parse_mode="HTML",
    )


# =========================================================
# /INSTRUCTION
# =========================================================

@dp.message(Command("instruction"))
async def instruction_command(
    message: Message
):

    await message.answer(
        "📖 <b>How to Play Rodas Friend Zone Bingo</b>\n\n"
        "1️⃣ Choose a Cartela from 1–100.\n"
        "2️⃣ Tap <b>PROCEED</b>.\n"
        "3️⃣ The same countdown continues until the game starts.\n"
        "4️⃣ Numbers from 1–75 will be called.\n"
        "5️⃣ Complete a row, column, or diagonal.\n"
        "6️⃣ In Auto mode, called numbers are marked automatically.\n"
        "7️⃣ In Manual mode, mark the called numbers yourself.\n"
        "8️⃣ Tap <b>BINGO WIN</b> when you have a valid Bingo.\n\n"
        "💵 Each player enters with a 10 birr bet.",
        parse_mode="HTML",
    )


# =========================================================
# /INVITE
# =========================================================

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


# =========================================================
# /SUPPORT
# =========================================================

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
# API USER ID
# =========================================================

def get_api_user_id(request):

    return (
        request.headers.get(
            "X-User-ID"
        )
        or "demo-user"
    )


# =========================================================
# STATE API
# =========================================================

async def api_state(request):

    user_id = get_api_user_id(
        request
    )

    async with game.lock:

        state = game.state_for_user(
            user_id
        )

    return web.json_response(
        state
    )


# =========================================================
# SELECT CARTELA API
# =========================================================

async def api_select(request):

    user_id = get_api_user_id(
        request
    )

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
                "message": "Invalid Cartela.",
            },
            status=400,
        )

    async with game.lock:

        # -------------------------------------------------
        # GAME MUST BE IN SELECTION
        # -------------------------------------------------

        if game.phase != "selection":

            return web.json_response(
                {
                    "success": False,
                    "message": (
                        "Cartela selection is closed."
                    ),
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # TIME EXPIRED
        # -------------------------------------------------

        if game.selection_remaining() <= 0:

            return web.json_response(
                {
                    "success": False,
                    "message": (
                        "Selection time has ended."
                    ),
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # PLAYER ALREADY PROCEEDED
        # -------------------------------------------------

        if user_id in game.proceeded_users:

            return web.json_response(
                {
                    "success": False,
                    "message": (
                        "You already pressed PROCEED."
                    ),
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # VALID RANGE
        # -------------------------------------------------

        if (
            cartela < 1
            or cartela > 100
        ):

            return web.json_response(
                {
                    "success": False,
                    "message": (
                        "Cartela must be between 1 and 100."
                    ),
                },
                status=400,
            )

        # -------------------------------------------------
        # CARTELA ALREADY TAKEN
        # -------------------------------------------------

        if (
            cartela
            in game.taken_cartelas
        ):

            return web.json_response(
                {
                    "success": False,
                    "message": (
                        "That Cartela is already taken."
                    ),
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # REPLACE OLD CARTELA
        # -------------------------------------------------

        old_cartela = (
            game.player_cartelas.get(
                user_id
            )
        )

        if old_cartela:

            game.taken_cartelas.discard(
                old_cartela
            )

        game.player_cartelas[
            user_id
        ] = cartela

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
                "message": (
                    f"Cartela {cartela} selected."
                ),
                **game.state_for_user(
                    user_id
                ),
            }
        )


# =========================================================
# PROCEED API
# =========================================================

async def api_proceed(request):

    user_id = get_api_user_id(
        request
    )

    async with game.lock:

        # -------------------------------------------------
        # MUST STILL BE SELECTION
        # -------------------------------------------------

        if game.phase != "selection":

            return web.json_response(
                {
                    "success": False,
                    "message": "Selection is closed.",
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # CARTELA REQUIRED
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
                    "message": (
                        "Choose a Cartela first."
                    ),
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # PLAYER IS BACK IN GAME
        # -------------------------------------------------

        game.left_users.discard(
            user_id
        )

        game.proceeded_users.add(
            user_id
        )

        # -------------------------------------------------
        # IMPORTANT:
        #
        # SAME DEADLINE.
        # NO NEW 30 SECOND TIMER.
        # -------------------------------------------------

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
                "message": (
                    "You entered the game."
                ),
                **game.state_for_user(
                    user_id
                ),
            }
        )


# =========================================================
# LEAVE API
# =========================================================

async def api_leave(request):

    user_id = get_api_user_id(
        request
    )

    async with game.lock:

        # -------------------------------------------------
        # FINISHED
        # -------------------------------------------------

        if game.phase == "finished":

            return web.json_response(
                {
                    "success": False,
                    "message": (
                        "This game has already finished."
                    ),
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # ALREADY LEFT
        # -------------------------------------------------

        if user_id in game.left_users:

            return web.json_response(
                {
                    "success": True,
                    "message": (
                        "You already left this game."
                    ),
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
            "User %s left Game %s during phase %s.",
            user_id,
            game.game_number,
            game.phase,
        )

        return web.json_response(
            {
                "success": True,
                "message": (
                    "You left the game. "
                    "The game will continue for other players."
                ),
                **game.state_for_user(
                    user_id
                ),
            }
        )


# =========================================================
# CLAIM BINGO API
# =========================================================

async def api_claim_bingo(request):

    user_id = get_api_user_id(
        request
    )

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
                    "message": (
                        "You already left this game."
                    ),
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
                    "message": (
                        "Wrong Cartela — You lose. "
                        "Better luck next time!"
                    ),
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # GAME MUST BE PLAYING
        # -------------------------------------------------

        if game.phase != "playing":

            return web.json_response(
                {
                    "success": False,
                    "message": (
                        "The game is not currently "
                        "accepting Bingo claims."
                    ),
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # WINNER EXISTS
        # -------------------------------------------------

        if game.winner:

            return web.json_response(
                {
                    "success": False,
                    "message": (
                        "A winner has already been declared."
                    ),
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
                    "message": (
                        "You do not have a Cartela."
                    ),
                }
            )

        card = CARTELA_CARDS.get(
            cartela
        )

        if not card:

            return web.json_response(
                {
                    "success": False,
                    "message": (
                        "Cartela not found."
                    ),
                }
            )

        # -------------------------------------------------
        # FREE CENTER
        # -------------------------------------------------

        marked.add(0)

        # -------------------------------------------------
        # VALID CARD NUMBERS
        # -------------------------------------------------

        valid_card_numbers = set(
            card_numbers(card)
        )

        # -------------------------------------------------
        # INVALID MARKS
        # -------------------------------------------------

        invalid_marks = {
            number
            for number in marked
            if (
                number != 0
                and (
                    number
                    not in valid_card_numbers
                    or number
                    not in game.drawn_numbers
                )
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
                    "message": (
                        "Wrong Cartela — You lose. "
                        "Better luck next time!"
                    ),
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # CHECK WINNING PATTERN
        # -------------------------------------------------

        pattern = get_bingo_pattern(
            card,
            marked
        )

        if not pattern:

            game.blocked_users.add(
                user_id
            )

            game.wrong_claims.add(
                user_id
            )

            logger.info(
                "User %s pressed Bingo "
                "without a valid Bingo.",
                user_id,
            )

            return web.json_response(
                {
                    "success": False,
                    "wrong": True,
                    "message": (
                        "Wrong Cartela — You lose. "
                        "Better luck next time!"
                    ),
                    **game.state_for_user(
                        user_id
                    ),
                }
            )

        # -------------------------------------------------
        # WINNER
        # -------------------------------------------------

        winner_amount = game.prize

        game.winner = {
            "user_id": user_id,
            "cartela": cartela,
            "number": game.current_number,
            "pattern": pattern,
            "amount": winner_amount,
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
            "WINNER: user=%s cartela=%s pattern=%s prize=%s",
            user_id,
            cartela,
            pattern,
            winner_amount,
        )

        return web.json_response(
            {
                "success": True,
                "winner": True,
                "message": (
                    "🎉 BINGO! You won!"
                ),
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
                "Rodas Friend Zone Bingo server "
                "is running, but web/index.html "
                "was not found."
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

    return web.json_response(
        {
            "status": "ok",
            "service": (
                "Rodas Friend Zone Bingo"
            ),
            "game": game.game_number,
            "phase": game.phase,
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
            "Rejected Telegram webhook request: "
            "bad secret."
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
# TELEGRAM COMMANDS
# =========================================================

def telegram_commands():

    return [

        BotCommand(
            command="start",
            description=(
                "Start Rodas Friend Zone"
            ),
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


# =========================================================
# TELEGRAM MENU
# =========================================================

async def setup_commands():

    commands = telegram_commands()

    await bot.set_my_commands(
        commands
    )

    await bot.set_chat_menu_button(
        menu_button=MenuButtonCommands()
    )

    logger.info(
        "Telegram menu commands registered."
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

            if (
                webhook_info.url
                != webhook_url
            ):

                logger.warning(
                    "Telegram webhook URL does not "
                    "match expected URL."
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

            if (
                attempt
                < max_attempts
            ):

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

            async with game.lock:

                now = time.monotonic()

                # =========================================
                # SELECTION
                # =========================================
                #
                # Players have ONE shared 30-second timer.
                #
                # If a player presses PROCEED at 18 seconds,
                # that player's starting countdown is also
                # 18 seconds.
                #
                # There is NO second 30-second timer.
                # =========================================

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
                                "Game %s: no players. "
                                "Restarting selection.",
                                game.game_number,
                            )

                            game.start_selection_locked()

                        else:

                            # ---------------------------------
                            # THE SHARED COUNTDOWN HAS ENDED.
                            # START THE GAME IMMEDIATELY.
                            # ---------------------------------

                            game.ensure_start_timer_locked()

                            game.start_playing_locked()

                            logger.info(
                                "Game %s: shared countdown "
                                "finished. Bingo game started.",
                                game.game_number,
                            )

                # =========================================
                # PLAYING
                # =========================================

                elif game.phase == "playing":

                    if (
                        game.next_draw_at
                        is not None
                        and now
                        >= game.next_draw_at
                    ):

                        game.draw_number_locked()

                        # ---------------------------------
                        # NEXT NUMBER AFTER 3 SECONDS
                        # ---------------------------------

                        if (
                            game.phase
                            == "playing"
                        ):

                            game.next_draw_at = (
                                now
                                + DRAW_INTERVAL_SECONDS
                            )

                # =========================================
                # FINISHED
                # =========================================

                elif game.phase == "finished":

                    if (
                        game.result_end
                        is not None
                        and now
                        >= game.result_end
                    ):

                        logger.info(
                            "Game %s result finished. "
                            "Starting next game.",
                            game.game_number,
                        )

                        game.start_selection_locked()

            await asyncio.sleep(
                0.2
            )

        except asyncio.CancelledError:

            logger.info(
                "Game loop cancelled."
            )

            raise

        except Exception:

            logger.exception(
                "Error inside game loop."
            )

            await asyncio.sleep(
                1
            )


# =========================================================
# APPLICATION STARTUP
# =========================================================

async def on_startup(app):

    logger.info(
        "Starting Rodas Friend Zone Bingo..."
    )

    telegram_ok = (
        await setup_telegram()
    )

    if telegram_ok:

        logger.info(
            "Telegram setup successful."
        )

    else:

        logger.warning(
            "Telegram setup did not complete successfully."
        )

    # -----------------------------------------------------
    # START GAME LOOP
    # -----------------------------------------------------

    app["game_loop_task"] = (
        asyncio.create_task(
            game_loop()
        )
    )

    logger.info(
        "Bingo game loop started."
    )


# =========================================================
# APPLICATION SHUTDOWN
# =========================================================

async def on_shutdown(app):

    logger.info(
        "Shutting down Rodas Friend Zone Bingo..."
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

    # -----------------------------------------------------
    # CLOSE TELEGRAM BOT
    # -----------------------------------------------------

    try:

        await bot.session.close()

    except Exception:

        logger.exception(
            "Error closing Telegram bot session."
        )


# =========================================================
# CREATE WEB APPLICATION
# =========================================================

app = web.Application()


# =========================================================
# ROUTES
# =========================================================

# Main Mini App
app.router.add_get(
    "/",
    handle_index,
)

# Health check
app.router.add_get(
    "/health",
    health,
)

# Telegram webhook
app.router.add_post(
    WEBHOOK_PATH,
    telegram_webhook,
)

# Bingo API
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
    "/api/claim",
    api_claim_bingo,
)


# =========================================================
# STARTUP / SHUTDOWN HOOKS
# =========================================================

app.on_startup.append(
    on_startup
)

app.on_cleanup.append(
    on_shutdown
)


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    logger.info(
        "=================================================="
    )

    logger.info(
        "Rodas Friend Zone Bingo"
    )

    logger.info(
        "Starting web server on port %s",
        PORT,
    )

    logger.info(
        "Render URL: %s",
        RENDER_EXTERNAL_URL,
    )

    logger.info(
        "Webhook: %s",
        WEBHOOK_PATH,
    )

    logger.info(
        "=================================================="
    )

    web.run_app(
        app,
        host="0.0.0.0",
        port=PORT,
    )
