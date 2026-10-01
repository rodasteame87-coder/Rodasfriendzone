import os
import asyncio
import logging
import random
from datetime import datetime, timedelta

from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from config import BOT_TOKEN


# =========================
# SETTINGS
# =========================

PORT = int(os.getenv("PORT", "10000"))

WEBHOOK_PATH = "/telegram/webhook"

WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    "rodasfriendzone-demo-secret"
)

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

SELECTION_SECONDS = 30

# Time between balls.
# We can change this later.
DRAW_INTERVAL_SECONDS = 5


# =========================
# LOGGING
# =========================

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)


# =========================
# BOT
# =========================

bot = Bot(token=BOT_TOKEN)

dp = Dispatcher()


# =========================
# BINGO CARD
# =========================

def generate_bingo_card():
    """
    Standard 75-ball Bingo card.

    B = 1-15
    I = 16-30
    N = 31-45
    G = 46-60
    O = 61-75

    Center N3 is FREE.
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
                current_row.append("FREE")
            else:
                current_row.append(
                    columns[column][row]
                )

        card.append(current_row)

    return card


# =========================
# CARTELA CARDS
# =========================

# Each Cartela 1-100 gets its own card.
# Cards are generated once when the server starts.
cartela_cards = {
    cartela: generate_bingo_card()
    for cartela in range(1, 101)
}


# =========================
# GAME PLAYER
# =========================

class Player:

    def __init__(self, user_id, cartela):

        self.user_id = user_id

        self.cartela = cartela

        self.card = cartela_cards[cartela]

        # FREE is already marked.
        self.marked = {
            (2, 2)
        }


# =========================
# GAME ROOM
# =========================

class BingoRoom:

    def __init__(self):

        # selection
        # playing
        self.state = "selection"

        self.game_number = 0

        # Cartela -> Telegram user ID
        self.taken_cartelas = {}

        # Telegram user ID -> Cartela
        self.player_cartelas = {}

        # Telegram user ID -> Player object
        self.players = {}

        # Numbers drawn in current game
        self.drawn_numbers = []

        # Numbers still available
        self.remaining_numbers = list(
            range(1, 76)
        )

        # Selection deadline
        self.selection_end = None

        # Lock prevents two operations changing
        # game state at exactly the same time.
        self.lock = asyncio.Lock()


room = BingoRoom()


# =========================
# CARTELA BOARD
# =========================

def cartela_board():

    rows = []

    row = []

    for number in range(1, 101):

        if number in room.taken_cartelas:

            text = f"🔴 {number}"

        else:

            text = f"🟢 {number}"

        row.append(
            InlineKeyboardButton(
                text=text,
                callback_data=f"cartela:{number}"
            )
        )

        if len(row) == 10:

            rows.append(row)

            row = []

    if row:
        rows.append(row)

    rows.append([
        InlineKeyboardButton(
            text="🔄 REFRESH",
            callback_data="cartela_refresh"
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


# =========================
# SELECTION TEXT
# =========================

def selection_text():

    if room.selection_end:

        remaining = max(
            0,
            int(
                (
                    room.selection_end
                    - datetime.now()
                ).total_seconds()
            )
        )

    else:

        remaining = SELECTION_SECONDS

    taken = len(room.taken_cartelas)

    return (
        "🎱 RODAS FRIEND ZONE\n\n"
        "🟢 CARTELA SELECTION\n\n"
        f"⏱️ Time remaining: {remaining} seconds\n"
        f"📋 Cartelas taken: {taken}/100\n\n"
        "Choose ONE Cartela.\n\n"
        "🟢 Available\n"
        "🔴 Taken"
    )


# =========================
# BINGO CARD DISPLAY
# =========================

def card_text(player):

    lines = []

    lines.append(
        f"📋 CARTELA {player.cartela}"
    )

    lines.append(
        "🅱️   🇮   🇳   🇬   🅾️"
    )

    for row in range(5):

        cells = []

        for column in range(5):

            value = player.card[row][column]

            if value == "FREE":

                cells.append("⭐")

            elif (
                row,
                column
            ) in player.marked:

                cells.append(
                    f"🟢{value}"
                )

            else:

                cells.append(
                    f"⬜{value}"
                )

        lines.append(
            "  ".join(
                f"{cell:>5}"
                for cell in cells
            )
        )

    return "\n".join(lines)


# =========================
# WIN CHECK
# =========================

def has_bingo(player):

    # Rows
    for row in range(5):

        if all(
            (row, column)
            in player.marked
            for column in range(5)
        ):

            return True

    # Columns
    for column in range(5):

        if all(
            (row, column)
            in player.marked
            for row in range(5)
        ):

            return True

    # Main diagonal
    if all(
        (i, i)
        in player.marked
        for i in range(5)
    ):

        return True

    # Other diagonal
    if all(
        (i, 4 - i)
        in player.marked
        for i in range(5)
    ):

        return True

    return False


# =========================
# MARK DRAWN NUMBER
# =========================

def mark_number(player, number):

    for row in range(5):

        for column in range(5):

            if player.card[row][column] == number:

                player.marked.add(
                    (row, column)
                )


# =========================
# MAIN MENU
# =========================

def main_menu():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎯 PLAY BINGO",
                    callback_data="play"
                )
            ]
        ]
    )


# =========================
# START
# =========================

@dp.message(CommandStart())
async def start(message: Message):

    user_id = message.from_user.id

    if room.state == "playing":

        await message.answer(
            "🔴 GAME ALREADY IN PLAY\n\n"
            "Please wait until this game ends."
        )

        return

    if room.state == "selection":

        await message.answer(
            selection_text(),
            reply_markup=cartela_board()
        )

        return


# =========================
# PLAY
# =========================

@dp.callback_query(F.data == "play")
async def play(callback: CallbackQuery):

    user_id = callback.from_user.id

    if room.state == "playing":

        await callback.answer(
            "🔴 GAME ALREADY IN PLAY\n"
            "Please wait until this game ends.",
            show_alert=True
        )

        return

    if room.state == "selection":

        if user_id in room.player_cartelas:

            cartela = room.player_cartelas[user_id]

            await callback.answer(
                f"You already selected Cartela {cartela}.",
                show_alert=True
            )

            return

        await callback.message.edit_text(
            selection_text(),
            reply_markup=cartela_board()
        )

        await callback.answer()

        return


# =========================
# CHOOSE CARTELA
# =========================

@dp.callback_query(F.data.startswith("cartela:"))
async def choose_cartela(callback: CallbackQuery):

    user_id = callback.from_user.id

    async with room.lock:

        if room.state != "selection":

            await callback.answer(
                "🔴 GAME ALREADY IN PLAY\n\n"
                "Please wait for the next game.",
                show_alert=True
            )

            return

        if user_id in room.player_cartelas:

            current = room.player_cartelas[user_id]

            await callback.answer(
                f"You already selected Cartela {current}.",
                show_alert=True
            )

            return

        cartela = int(
            callback.data.split(":")[1]
        )

        if cartela < 1 or cartela > 100:

            await callback.answer(
                "Invalid Cartela.",
                show_alert=True
            )

            return

        if cartela in room.taken_cartelas:

            await callback.answer(
                f"Cartela {cartela} is already taken.\n"
                "Choose another one.",
                show_alert=True
            )

            return

        # Reserve Cartela
        room.taken_cartelas[cartela] = user_id

        room.player_cartelas[user_id] = cartela

    await callback.message.edit_text(
        selection_text()
        + f"\n\n✅ Your Cartela: {cartela}",
        reply_markup=cartela_board()
    )

    await callback.answer(
        f"Cartela {cartela} selected!"
    )


# =========================
# REFRESH
# =========================

@dp.callback_query(F.data == "cartela_refresh")
async def refresh_cartela(callback: CallbackQuery):

    if room.state != "selection":

        await callback.answer(
            "The selection period is closed.",
            show_alert=True
        )

        return

    await callback.message.edit_text(
        selection_text(),
        reply_markup=cartela_board()
    )

    await callback.answer("Updated")


# =========================
# LEAVE
# =========================

@dp.callback_query(F.data == "leave_cartela")
async def leave_cartela(callback: CallbackQuery):

    user_id = callback.from_user.id

    async with room.lock:

        if room.state != "selection":

            await callback.answer(
                "The selection period has ended.",
                show_alert=True
            )

            return

        if user_id not in room.player_cartelas:

            await callback.answer(
                "You don't have a Cartela.",
                show_alert=True
            )

            return

        cartela = room.player_cartelas[user_id]

        del room.player_cartelas[user_id]

        room.taken_cartelas.pop(
            cartela,
            None
        )

    await callback.message.edit_text(
        "🚪 You left your Cartela.\n\n"
        "You can choose another available Cartela.",
        reply_markup=cartela_board()
    )

    await callback.answer(
        f"Cartela {cartela} is available again."
    )


# =========================
# PLAYER MENU
# =========================

def player_menu():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚪 LEAVE",
                    callback_data="leave_cartela"
                )
            ]
        ]
    )


# =========================
# START SELECTION
# =========================

async def start_selection():

    async with room.lock:

        room.state = "selection"

        room.selection_end = (
            datetime.now()
            + timedelta(
                seconds=SELECTION_SECONDS
            )
        )

        room.taken_cartelas.clear()

        room.player_cartelas.clear()

        logger.info(
            "30-second Cartela selection started."
        )

    # Countdown loop
    while True:

        await asyncio.sleep(1)

        async with room.lock:

            if room.state != "selection":

                return

            remaining = int(
                (
                    room.selection_end
                    - datetime.now()
                ).total_seconds()
            )

            if remaining <= 0:

                break

    await finish_selection()


# =========================
# FINISH SELECTION
# =========================

async def finish_selection():

    async with room.lock:

        if room.state != "selection":

            return

        # Nobody joined
        if not room.player_cartelas:

            logger.info(
                "No players selected Cartelas."
            )

            # Start another selection period
            # because there are no players.
            room.selection_end = (
                datetime.now()
                + timedelta(
                    seconds=SELECTION_SECONDS
                )
            )

            restart = True

        else:

            restart = False

            room.state = "playing"

            room.game_number += 1

            room.drawn_numbers = []

            room.remaining_numbers = list(
                range(1, 76)
            )

            room.players = {}

            for user_id, cartela in (
                room.player_cartelas.items()
            ):

                room.players[user_id] = Player(
                    user_id,
                    cartela
                )

            logger.info(
                f"Game #{room.game_number} "
                f"started with "
                f"{len(room.players)} players."
            )

    if restart:

        await start_selection()

        return

    # Tell everyone game has started
    for user_id, player in room.players.items():

        try:

            await bot.send_message(
                user_id,
                "🎱 BINGO GAME STARTED!\n\n"
                f"🎮 Game #{room.game_number}\n"
                f"📋 Cartela: {player.cartela}\n\n"
                "🏆 Win condition:\n"
                "Complete any ROW, COLUMN, or DIAGONAL.\n\n"
                + card_text(player),
            )

        except Exception:

            logger.exception(
                f"Could not send game start to {user_id}"
            )

    # Start drawing
    await draw_balls()


# =========================
# DRAW BINGO BALLS
# =========================

async def draw_balls():

    while True:

        async with room.lock:

            if room.state != "playing":

                return

            if not room.remaining_numbers:

                logger.info(
                    "All 75 Bingo balls were drawn."
                )

                winner = None

                break

            number = random.choice(
                room.remaining_numbers
            )

            room.remaining_numbers.remove(
                number
            )

            room.drawn_numbers.append(
                number
            )

            # Mark the number on every card
            for player in room.players.values():

                mark_number(
                    player,
                    number
                )

                if has_bingo(player):

                    winner = player

                    break

            else:

                winner = None

        # Announce drawn number
        letter = get_bingo_letter(number)

        for user_id in room.players:

            try:

                await bot.send_message(
                    user_id,
                    f"🎱 BALL DRAWN\n\n"
                    f"🔵 {letter} {number}\n\n"
                    f"📊 Balls drawn: "
                    f"{len(room.drawn_numbers)}/75"
                )

            except Exception:

                logger.exception(
                    f"Could not notify {user_id}"
                )

        # Someone won
        if winner:

            await announce_winner(
                winner,
                number
            )

            return

        await asyncio.sleep(
            DRAW_INTERVAL_SECONDS
        )


    # No winner after all 75 balls
    await end_game(
        winner=None
    )


# =========================
# BINGO LETTER
# =========================

def get_bingo_letter(number):

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


# =========================
# WINNER
# =========================

async def announce_winner(
    winner,
    winning_number
):

    logger.info(
        f"Winner: user={winner.user_id}, "
        f"cartela={winner.cartela}"
    )

    for user_id in room.players:

        try:

            if user_id == winner.user_id:

                message = (
                    "🏆🎉 BINGO! 🎉🏆\n\n"
                    "YOU WIN!\n\n"
                    f"📋 Cartela: {winner.cartela}\n"
                    f"🎱 Winning ball: "
                    f"{get_bingo_letter(winning_number)} "
                    f"{winning_number}\n\n"
                    "✅ Complete row, column, "
                    "or diagonal."
                )

            else:

                message = (
                    "🏁 BINGO GAME OVER\n\n"
                    f"🏆 Winner: Cartela "
                    f"{winner.cartela}\n\n"
                    f"🎱 Winning ball: "
                    f"{get_bingo_letter(winning_number)} "
                    f"{winning_number}"
                )

            await bot.send_message(
                user_id,
                message
            )

        except Exception:

            logger.exception(
                f"Could not notify {user_id}"
            )

    await end_game(
        winner=winner
    )


# =========================
# END GAME
# =========================

async def end_game(winner=None):

    async with room.lock:

        if room.state != "playing":

            return

        logger.info(
            f"Game #{room.game_number} ended."
        )

        room.state = "ending"

        current_players = list(
            room.players.values()
        )

    # Short pause after winner announcement
    await asyncio.sleep(3)

    # Reset current game
    async with room.lock:

        room.players.clear()

        room.taken_cartelas.clear()

        room.player_cartelas.clear()

        room.drawn_numbers.clear()

        room.remaining_numbers = list(
            range(1, 76)
        )

        room.selection_end = None

    # IMPORTANT:
    # Only NOW does the next 30-second
    # Cartela selection begin.
    await start_selection()


# =========================
# HEALTH CHECK
# =========================

async def health(request):

    return web.Response(
        text="RodasFriendZone is running!"
    )


# =========================
# TELEGRAM WEBHOOK
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

        from aiogram.types import Update

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
            "Webhook error"
        )

        return web.Response(
            status=500,
            text="Webhook error"
        )


# =========================
# START SERVER
# =========================

async def main():

    if not RENDER_EXTERNAL_URL:

        raise RuntimeError(
            "RENDER_EXTERNAL_URL is missing."
        )

    webhook_url = (
        RENDER_EXTERNAL_URL.rstrip("/")
        + WEBHOOK_PATH
    )

    logger.info(
        f"Setting webhook: {webhook_url}"
    )

    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True
    )

    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    app.router.add_get(
        "/health",
        health
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

    logger.info(
        f"RodasFriendZone running on port {PORT}"
    )

    # First-ever selection period.
    asyncio.create_task(
        start_selection()
    )

    while True:

        await asyncio.sleep(3600)


# =========================
# RUN
# =========================

if __name__ == "__main__":

    asyncio.run(main())
