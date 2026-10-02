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
        list(range(1, 16)),     # B
        list(range(16, 31)),    # I
        list(range(31, 46)),    # N
        list(range(46, 61)),    # G
        list(range(61, 76)),    # O
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
    marked.add(0)

    # Rows
    for row in range(5):
        if all(
            card[row][col] == 0
            or card[row][col] in marked
            for col in range(5)
        ):
            return f"Row {row + 1}"

    # Columns
    for col in range(5):
        if all(
            card[row][col] == 0
            or card[row][col] in marked
            for row in range(5)
        ):
            letters = ["B", "I", "N", "G", "O"]
            return f"Column {letters[col]}"

    # Main diagonal
    if all(
        card[i][i] == 0
        or card[i][i] in marked
        for i in range(5)
    ):
        return "Main Diagonal"

    # Other diagonal
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

        self.selection_end = 0
        self.start_end = None
        self.result_end = None
        self.next_draw_at = None

        self.taken_cartelas = set()
        self.player_cartelas = {}
        self.proceeded_users = set()

        self.drawn_numbers = []
        self.current_number = None

        self.winner = None
        self.prize = 0

        self.blocked_users = set()
        self.wrong_claims = set()

        self.start_selection_locked()

    # -----------------------------------------------------

    def start_selection_locked(self):
        self.phase = "selection"

        self.game_number += 1

        self.selection_end = time.monotonic() + SELECTION_SECONDS

        self.start_end = None
        self.result_end = None
        self.next_draw_at = None

        self.taken_cartelas.clear()
        self.player_cartelas.clear()
        self.proceeded_users.clear()

        self.drawn_numbers.clear()
        self.current_number = None

        self.winner = None
        self.prize = 0

        self.blocked_users.clear()
        self.wrong_claims.clear()

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
            int(self.selection_end - time.monotonic())
        )

    # -----------------------------------------------------

    def start_remaining(self):
        if self.start_end is None:
            return 0

        return max(
            0,
            int(self.start_end - time.monotonic())
        )

    # -----------------------------------------------------

    def result_remaining(self):
        if self.result_end is None:
            return 0

        return max(
            0,
            int(self.result_end - time.monotonic())
        )

    # -----------------------------------------------------

    def ensure_start_timer_locked(self):
        now = time.monotonic()

        if self.start_end is None:
            self.start_end = max(
                self.selection_end,
                now + START_SECONDS,
            )

        return self.start_end

    # -----------------------------------------------------

    def get_view_phase(self, user_id):
        if self.phase == "selection":

            if user_id in self.proceeded_users:
                return "starting"

            return "selection"

        return self.phase

    # -----------------------------------------------------

    def prize_for_players(self):
        players = len(self.player_cartelas)

        if players <= 0:
            return 0

        total = players * BET_AMOUNT

        # 75% goes to the winner
        # 25% goes to the creator
        return int(total * 0.75)

    # -----------------------------------------------------

    def creator_amount(self):
        players = len(self.player_cartelas)

        if players <= 0:
            return 0

        total = players * BET_AMOUNT

        return total - self.prize_for_players()

    # -----------------------------------------------------

    def start_playing_locked(self):
        self.phase = "playing"

        self.prize = self.prize_for_players()

        self.next_draw_at = time.monotonic()

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
                reason="No winner — all 75 numbers were called."
            )
            return None

        number = random.choice(remaining)

        self.drawn_numbers.append(number)
        self.current_number = number

        logger.info(
            "Game %s called number %s",
            self.game_number,
            number,
        )

        if len(self.drawn_numbers) >= 75:
            self.finish_locked(
                winner=None,
                reason="No winner — all 75 numbers were called."
            )

        return number

    # -----------------------------------------------------

    def finish_locked(self, winner=None, reason=None):
        self.phase = "finished"

        self.winner = winner

        self.result_end = (
            time.monotonic() + RESULT_SECONDS
        )

        self.next_draw_at = None

        self.result_message = reason

    # -----------------------------------------------------

    def state_for_user(self, user_id):

        selected = self.player_cartelas.get(user_id)

        selected_card = (
            CARTELA_CARDS.get(selected)
            if selected
            else None
        )

        view_phase = self.get_view_phase(user_id)

        winner_data = None

        if self.winner:
            winner_data = dict(self.winner)

        return {
            "phase": self.phase,
            "view_phase": view_phase,

            "game": self.game_number,
            "game_number": self.game_number,

            "selection_remaining": self.selection_remaining(),
            "start_remaining": self.start_remaining(),
            "result_remaining": self.result_remaining(),

            "players": len(self.player_cartelas),

            "bet": BET_AMOUNT,

            "total_pool": (
                len(self.player_cartelas) * BET_AMOUNT
            ),

            "prize": self.prize,

            "creator_amount": self.creator_amount(),

            "wallet": get_balance(user_id),

            "taken": sorted(self.taken_cartelas),
            "taken_cartelas": sorted(self.taken_cartelas),

            "selected": selected,
            "my_cartela": selected,
            "cartela": selected,

            "card": selected_card,

            "called": list(self.drawn_numbers),
            "drawn_numbers": list(self.drawn_numbers),

            "current": self.current_number,
            "current_number": self.current_number,

            "winner": winner_data,

            "blocked": user_id in self.blocked_users,
            "wrong_claim": user_id in self.wrong_claims,

            "proceeded": user_id in self.proceeded_users,

            "result_message": getattr(
                self,
                "result_message",
                None
            ),
        }


game = BingoGame()


# =========================================================
# TELEGRAM USER ID
# =========================================================

def telegram_user_id(message: Message):
    if not message.from_user:
        return "unknown"

    return str(message.from_user.id)


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
                        url=RENDER_EXTERNAL_URL + "/"
                    )
                )
            ]
        ]
    )


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

@dp.message(CommandStart())
async def start_command(message: Message):

    user_id = telegram_user_id(message)

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


# ---------------------------------------------------------

@dp.message(Command("play"))
async def play_command(message: Message):

    user_id = telegram_user_id(message)

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


# ---------------------------------------------------------

@dp.message(Command("deposit"))
async def deposit_command(message: Message):

    user_id = telegram_user_id(message)

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


# ---------------------------------------------------------

@dp.message(Command("balance"))
async def balance_command(message: Message):

    user_id = telegram_user_id(message)

    balance = get_balance(user_id)

    logger.info(
        "/balance received from user %s",
        user_id,
    )

    await message.answer(
        f"💰 <b>Your Balance</b>\n\n"
        f"Balance: <b>{balance} birr</b>",
        parse_mode="HTML",
    )


# ---------------------------------------------------------

@dp.message(Command("withdraw"))
async def withdraw_command(message: Message):

    user_id = telegram_user_id(message)

    logger.info(
        "/withdraw received from user %s",
        user_id,
    )

    await message.answer(
        "💸 <b>Withdraw</b>\n\n"
        "Withdrawal service is being prepared.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------

@dp.message(Command("transfer"))
async def transfer_command(message: Message):

    user_id = telegram_user_id(message)

    logger.info(
        "/transfer received from user %s",
        user_id,
    )

    await message.answer(
        "🔄 <b>Transfer</b>\n\n"
        "The transfer service is being prepared.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------

@dp.message(Command("instruction"))
async def instruction_command(message: Message):

    await message.answer(
        "📖 <b>How to Play Rodas Friend Zone Bingo</b>\n\n"
        "1️⃣ Choose a Cartela from 1–100.\n"
        "2️⃣ Tap <b>PROCEED</b>.\n"
        "3️⃣ Wait for the game countdown.\n"
        "4️⃣ Numbers from 1–75 will be called.\n"
        "5️⃣ Complete a row, column, or diagonal.\n"
        "6️⃣ In Auto mode, called numbers are marked automatically.\n"
        "7️⃣ In Manual mode, mark the called numbers yourself.\n"
        "8️⃣ Tap <b>BINGO WIN</b> when you have a valid Bingo.\n\n"
        "💵 Each player enters with a 10 birr bet.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------

@dp.message(Command("invite"))
async def invite_command(message: Message):

    await message.answer(
        "👥 <b>Invite Friends</b>\n\n"
        "Invite your friends to join Rodas Friend Zone "
        "and play Bingo together.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------

@dp.message(Command("support"))
async def support_command(message: Message):

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
        request.headers.get("X-User-ID")
        or "demo-user"
    )


# ---------------------------------------------------------

async def api_state(request):

    user_id = get_api_user_id(request)

    async with game.lock:
        state = game.state_for_user(user_id)

    return web.json_response(state)


# ---------------------------------------------------------

async def api_select(request):

    user_id = get_api_user_id(request)

    try:
        data = await request.json()
    except Exception:
        return web.json_response(
            {
                "success": False,
                "message": "Invalid request."
            },
            status=400,
        )

    cartela = data.get("cartela")

    try:
        cartela = int(cartela)
    except Exception:
        return web.json_response(
            {
                "success": False,
                "message": "Invalid Cartela."
            },
            status=400,
        )

    async with game.lock:

        if game.phase != "selection":
            return web.json_response(
                {
                    "success": False,
                    "message": "Cartela selection is closed.",
                    **game.state_for_user(user_id),
                }
            )

        if game.selection_remaining() <= 0:
            return web.json_response(
                {
                    "success": False,
                    "message": "Selection time has ended.",
                    **game.state_for_user(user_id),
                }
            )

        if user_id in game.proceeded_users:
            return web.json_response(
                {
                    "success": False,
                    "message": "You already pressed PROCEED.",
                    **game.state_for_user(user_id),
                }
            )

        if cartela < 1 or cartela > 100:
            return web.json_response(
                {
                    "success": False,
                    "message": "Cartela must be between 1 and 100."
                },
                status=400,
            )

        if cartela in game.taken_cartelas:
            return web.json_response(
                {
                    "success": False,
                    "message": "That Cartela is already taken.",
                    **game.state_for_user(user_id),
                }
            )

        old_cartela = game.player_cartelas.get(user_id)

        if old_cartela:
            game.taken_cartelas.discard(old_cartela)

        game.player_cartelas[user_id] = cartela
        game.taken_cartelas.add(cartela)

        logger.info(
            "User %s selected Cartela %s",
            user_id,
            cartela,
        )

        return web.json_response(
            {
                "success": True,
                "message": f"Cartela {cartela} selected.",
                **game.state_for_user(user_id),
            }
        )


# ---------------------------------------------------------

async def api_proceed(request):

    user_id = get_api_user_id(request)

    async with game.lock:

        if game.phase != "selection":
            return web.json_response(
                {
                    "success": False,
                    "message": "Selection is closed.",
                    **game.state_for_user(user_id),
                }
            )

        cartela = game.player_cartelas.get(user_id)

        if not cartela:
            return web.json_response(
                {
                    "success": False,
                    "message": "Choose a Cartela first.",
                    **game.state_for_user(user_id),
                }
            )

        game.proceeded_users.add(user_id)

        game.ensure_start_timer_locked()

        logger.info(
            "User %s pressed PROCEED with Cartela %s",
            user_id,
            cartela,
        )

        return web.json_response(
            {
                "success": True,
                "message": "You entered the game.",
                **game.state_for_user(user_id),
            }
        )


# ---------------------------------------------------------

async def api_leave(request):

    user_id = get_api_user_id(request)

    async with game.lock:

        if game.phase != "selection":

            return web.json_response(
                {
                    "success": False,
                    "message": "You cannot leave after the game starts.",
                    **game.state_for_user(user_id),
                }
            )

        cartela = game.player_cartelas.pop(
            user_id,
            None
        )

        if cartela:
            game.taken_cartelas.discard(cartela)

        game.proceeded_users.discard(user_id)

        return web.json_response(
            {
                "success": True,
                "message": "You left the game.",
                **game.state_for_user(user_id),
            }
        )


# ---------------------------------------------------------

async def api_claim_bingo(request):

    user_id = get_api_user_id(request)

    try:
        data = await request.json()
    except Exception:
        return web.json_response(
            {
                "success": False,
                "message": "Invalid request."
            },
            status=400,
        )

    marked = data.get("marked", [])

    if not isinstance(marked, list):
        marked = []

    try:
        marked = {
            int(number)
            for number in marked
        }
    except Exception:
        marked = set()

    async with game.lock:

        if user_id in game.blocked_users:

            return web.json_response(
                {
                    "success": False,
                    "wrong": True,
                    "message": "Wrong Cartela — You lose. Better luck next time!",
                    **game.state_for_user(user_id),
                }
            )

        if game.phase != "playing":

            return web.json_response(
                {
                    "success": False,
                    "message": "The game is not currently accepting Bingo claims.",
                    **game.state_for_user(user_id),
                }
            )

        if game.winner:

            return web.json_response(
                {
                    "success": False,
                    "message": "A winner has already been declared.",
                    **game.state_for_user(user_id),
                }
            )

        cartela = game.player_cartelas.get(user_id)

        if not cartela:

            return web.json_response(
                {
                    "success": False,
                    "message": "You do not have a Cartela."
                }
            )

        card = CARTELA_CARDS.get(cartela)

        if not card:

            return web.json_response(
                {
                    "success": False,
                    "message": "Cartela not found."
                }
            )

        # FREE center is always marked
        marked.add(0)

        # Only numbers actually on the player's card
        valid_card_numbers = set(
            card_numbers(card)
        )

        # Player cannot claim using a number
        # that has not been called.
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

            game.blocked_users.add(user_id)
            game.wrong_claims.add(user_id)

            logger.info(
                "User %s made invalid Bingo claim.",
                user_id,
            )

            return web.json_response(
                {
                    "success": False,
                    "wrong": True,
                    "message": "Wrong Cartela — You lose. Better luck next time!",
                    **game.state_for_user(user_id),
                }
            )

        pattern = get_bingo_pattern(
            card,
            marked
        )

        # A winner is declared only when the player
        # submits a valid Bingo claim.
        if not pattern:

            game.blocked_users.add(user_id)
            game.wrong_claims.add(user_id)

            logger.info(
                "User %s pressed Bingo without a valid Bingo.",
                user_id,
            )

            return web.json_response(
                {
                    "success": False,
                    "wrong": True,
                    "message": "Wrong Cartela — You lose. Better luck next time!",
                    **game.state_for_user(user_id),
                }
            )

        # VALID WINNER
        winner_amount = game.prize

        game.winner = {
            "user_id": user_id,
            "cartela": cartela,
            "number": game.current_number,
            "pattern": pattern,
            "amount": winner_amount,
        }

        get_balance(user_id)

        balances[user_id] += winner_amount

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
                "message": "🎉 BINGO! You won!",
                **game.state_for_user(user_id),
            }
        )


# =========================================================
# INDEX PAGE
# =========================================================

async def handle_index(request):

    index_file = WEB_FOLDER / "index.html"

    if not index_file.exists():

        return web.Response(
            text=(
                "Rodas Friend Zone Bingo server is running, "
                "but web/index.html was not found."
            ),
            content_type="text/plain",
            status=404,
        )

    return web.FileResponse(index_file)


# =========================================================
# HEALTH CHECK
# =========================================================

async def health(request):

    return web.json_response(
        {
            "status": "ok",
            "service": "Rodas Friend Zone Bingo",
            "game": game.game_number,
            "phase": game.phase,
        }
    )


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

async def telegram_webhook(request):

    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
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

        update = Update.model_validate(data)

        await dp.feed_update(
            bot,
            update,
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

async def setup_commands():

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

    await bot.set_my_commands(commands)

    logger.info(
        "Telegram menu commands registered."
    )


# =========================================================
# GAME LOOP
# =========================================================

async def game_loop():

    while True:

        try:

            async with game.lock:

                now = time.monotonic()

                # -----------------------------------------
                # SELECTION
                # -----------------------------------------

                if game.phase == "selection":

                    if now >= game.selection_end:

                        # IMPORTANT:
                        # DO NOT START THE GAME IF NO PLAYER
                        # HAS SELECTED A CARTELA.
                        if len(game.player_cartelas) == 0:

                            logger.info(
                                "Game %s: no players selected a Cartela. "
                                "Keeping selection open for another %s seconds.",
                                game.game_number,
                                SELECTION_SECONDS,
                            )

                            # Restart the selection timer.
                            # The game remains in "selection".
                            game.selection_end = (
                                now + SELECTION_SECONDS
                            )

                        else:

                            # At least one player selected a Cartela.
                            # Now the starting countdown can begin.
                            game.ensure_start_timer_locked()

                            game.phase = "starting"

                            logger.info(
                                "Game %s: starting countdown with %s player(s).",
                                game.game_number,
                                len(game.player_cartelas),
                            )

                # -----------------------------------------
                # STARTING
                # -----------------------------------------

                elif game.phase == "starting":

                    if (
                        game.start_end is not None
                        and now >= game.start_end
                    ):

                        # Extra safety check:
                        # Never start a game if all players
                        # somehow left before the game started.
                        if len(game.player_cartelas) == 0:

                            logger.info(
                                "Game %s: all players left before start. "
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
                        game.next_draw_at is not None
                        and now >= game.next_draw_at
                    ):

                        game.draw_number_locked()

                        if game.phase == "playing":

                            game.next_draw_at = (
                                now
                                + DRAW_INTERVAL_SECONDS
                            )

                # -----------------------------------------
                # FINISHED
                # -----------------------------------------

                elif game.phase == "finished":

                    if (
                        game.result_end is not None
                        and now >= game.result_end
                    ):

                        game.start_selection_locked()

        except Exception:

            logger.exception(
                "Game loop error."
            )

        await asyncio.sleep(0.25)


# =========================================================
# APP STARTUP
# =========================================================

async def on_startup(app):

    logger.info(
        "Starting Rodas Friend Zone..."
    )

    await setup_commands()

    webhook_url = (
        RENDER_EXTERNAL_URL
        + WEBHOOK_PATH
    )

    logger.info(
        "Setting Telegram webhook: %s",
        webhook_url,
    )

    # Remove any previous webhook first.
    await bot.delete_webhook(
        drop_pending_updates=False
    )

    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=False,
    )

    logger.info(
        "Telegram webhook configured successfully."
    )

    app["game_task"] = asyncio.create_task(
        game_loop()
    )


# =========================================================
# APP SHUTDOWN
# =========================================================

async def on_shutdown(app):

    logger.info(
        "Shutting down Rodas Friend Zone..."
    )

    task = app.get("game_task")

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
            "Could not remove webhook during shutdown."
        )

    await bot.session.close()


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
