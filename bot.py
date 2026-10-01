import os
import asyncio
import logging
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
# GAME STATE
# =========================

class BingoRoom:

    def __init__(self):

        # waiting = before first game
        # selection = 30 second Cartela selection
        # playing = Bingo game running
        self.state = "waiting"

        self.game_number = 0

        # Cartela -> Telegram user ID
        self.taken_cartelas = {}

        # Telegram user ID -> Cartela
        self.player_cartelas = {}

        # Players participating in current game
        self.players = set()

        # When 30 second selection ends
        self.selection_end = None


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
# SELECTION MESSAGE
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

        remaining = 30

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
# START
# =========================

@dp.message(CommandStart())
async def start(message: Message):

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

    await message.answer(
        "🎱 RODAS FRIEND ZONE\n\n"
        "Welcome!\n\n"
        "The next Cartela selection will begin "
        "automatically when a Bingo game ends.",
        reply_markup=main_menu()
    )


# =========================
# PLAY BUTTON
# =========================

@dp.callback_query(F.data == "play")
async def play(callback: CallbackQuery):

    user_id = callback.from_user.id

    # GAME IS CURRENTLY PLAYING
    if room.state == "playing":

        await callback.answer(
            "🔴 GAME ALREADY IN PLAY\n"
            "Please wait until this game ends.",
            show_alert=True
        )

        return

    # SELECTION IS OPEN
    if room.state == "selection":

        # Already selected
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

    # BEFORE FIRST GAME
    await callback.answer(
        "⏳ Please wait for the next Cartela selection.",
        show_alert=True
    )


# =========================
# CHOOSE CARTELA
# =========================

@dp.callback_query(F.data.startswith("cartela:"))
async def choose_cartela(callback: CallbackQuery):

    user_id = callback.from_user.id

    # Game started while user was looking at old message
    if room.state != "selection":

        await callback.answer(
            "🔴 GAME ALREADY IN PLAY\n"
            "Please wait for the next game.",
            show_alert=True
        )

        return

    # User already owns a Cartela
    if user_id in room.player_cartelas:

        current = room.player_cartelas[user_id]

        await callback.answer(
            f"You already have Cartela {current}.",
            show_alert=True
        )

        return

    cartela = int(
        callback.data.split(":")[1]
    )

    # Someone else already took it
    if cartela in room.taken_cartelas:

        await callback.answer(
            f"Cartela {cartela} is already taken.\n"
            "Choose another one.",
            show_alert=True
        )

        return

    # Take Cartela
    room.taken_cartelas[cartela] = user_id

    room.player_cartelas[user_id] = cartela

    await callback.message.edit_text(
        selection_text()
        + f"\n\n✅ You selected Cartela {cartela}.",
        reply_markup=cartela_board()
    )

    await callback.answer(
        f"Cartela {cartela} selected!"
    )

    # If all 100 are taken, start immediately
    if len(room.taken_cartelas) >= 100:

        await finish_selection()


# =========================
# REFRESH CARTELA BOARD
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
# LEAVE CARTELA
# =========================

def leave_button():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚪 LEAVE CARTELA",
                    callback_data="leave_cartela"
                )
            ]
        ]
    )


@dp.callback_query(F.data == "leave_cartela")
async def leave_cartela(callback: CallbackQuery):

    user_id = callback.from_user.id

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

    # Release Cartela
    del room.player_cartelas[user_id]

    if cartela in room.taken_cartelas:

        del room.taken_cartelas[cartela]

    await callback.message.edit_text(
        "🚪 You left the Cartela.\n\n"
        "You can choose another available Cartela "
        "while the selection period is still open.",
        reply_markup=cartela_board()
    )

    await callback.answer(
        f"Cartela {cartela} released."
    )


# =========================
# FINISH 30 SECOND SELECTION
# =========================

async def finish_selection():

    if room.state != "selection":

        return

    room.state = "playing"

    room.game_number += 1

    room.selection_end = None

    # Save current players
    room.players = set(
        room.player_cartelas.keys()
    )

    logger.info(
        f"Game {room.game_number} started "
        f"with {len(room.players)} players."
    )

    # Notify players
    for user_id in room.players:

        cartela = room.player_cartelas[user_id]

        try:

            await bot.send_message(
                user_id,
                "🎱 BINGO GAME STARTED!\n\n"
                f"🎮 Game #{room.game_number}\n"
                f"📋 Your Cartela: {cartela}\n\n"
                "Your Cartela is now locked into this game.\n\n"
                "🎯 The Bingo drawing system will be added next."
            )

        except Exception:

            logger.exception(
                f"Could not message user {user_id}"
            )

    # DEMO GAME
    # This is temporary.
    # Later this will be replaced by
    # the real 1-75 Bingo drawing engine.

    await asyncio.sleep(20)

    await end_game()


# =========================
# END GAME
# =========================

async def end_game():

    if room.state != "playing":

        return

    logger.info(
        f"Game {room.game_number} ended."
    )

    # Notify players
    for user_id in room.players:

        try:

            await bot.send_message(
                user_id,
                "🏁 BINGO GAME ENDED!\n\n"
                "The next Cartela selection will "
                "start automatically."
            )

        except Exception:

            logger.exception(
                f"Could not notify user {user_id}"
            )

    # Clear old Cartelas
    room.taken_cartelas.clear()

    room.player_cartelas.clear()

    room.players.clear()

    # START NEXT 30 SECOND SELECTION
    await start_selection()


# =========================
# START 30 SECOND SELECTION
# =========================

async def start_selection():

    room.state = "selection"

    room.selection_end = (
        datetime.now()
        + timedelta(seconds=30)
    )

    logger.info(
        "30 second Cartela selection started."
    )

    # Countdown
    while room.state == "selection":

        remaining = int(
            (
                room.selection_end
                - datetime.now()
            ).total_seconds()
        )

        if remaining <= 0:

            break

        await asyncio.sleep(1)

    # Timer finished
    if room.state == "selection":

        await finish_selection()


# =========================
# ADMIN/DEMO START
# =========================

async def start_first_selection():

    # Start the very first 30 second period
    # because there is no previous game yet.

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

    # Start the first Cartela selection
    asyncio.create_task(
        start_first_selection()
    )

    while True:

        await asyncio.sleep(3600)


# =========================
# RUN
# =========================

if __name__ == "__main__":

    asyncio.run(main())
