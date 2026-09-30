import os
import logging
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery

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


# Temporary demo data
games = {}


# =========================
# SIMPLE GAME
# =========================

class BingoGame:

    def __init__(self):
        self.balance = 1000
        self.selected_numbers = set()

    def select_number(self, number):

        if number in self.selected_numbers:
            self.selected_numbers.remove(number)
            return False

        self.selected_numbers.add(number)
        return True

    def clear(self):
        self.selected_numbers.clear()


def get_game(user_id):

    if user_id not in games:
        games[user_id] = BingoGame()

    return games[user_id]


# =========================
# MAIN MENU
# =========================

def main_menu():

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎯 PLAY BINGO",
                    callback_data="play"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💰 DEMO BALANCE",
                    callback_data="balance"
                )
            ]
        ]
    )


# =========================
# NUMBER BOARD 1-100
# =========================

def number_board(selected_numbers):

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    row = []

    for number in range(1, 101):

        if number in selected_numbers:
            text = f"🟢 {number}"
        else:
            text = str(number)

        row.append(
            InlineKeyboardButton(
                text=text,
                callback_data=f"number:{number}"
            )
        )

        if len(row) == 10:
            rows.append(row)
            row = []

    rows.append([
        InlineKeyboardButton(
            text="🔄 CLEAR",
            callback_data="clear"
        ),
        InlineKeyboardButton(
            text="✅ CONFIRM",
            callback_data="confirm"
        )
    ])

    rows.append([
        InlineKeyboardButton(
            text="⬅️ BACK",
            callback_data="back"
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


# =========================
# START
# =========================

@dp.message(CommandStart())
async def start(message: Message):

    user_id = message.from_user.id
    game = get_game(user_id)

    await message.answer(
        "🎱 RODAS FRIEND ZONE\n\n"
        "Welcome!\n\n"
        f"💰 Demo Balance: {game.balance} credits\n\n"
        "Choose an option:",
        reply_markup=main_menu()
    )


# =========================
# BALANCE
# =========================

@dp.callback_query(F.data == "balance")
async def balance(callback: CallbackQuery):

    game = get_game(callback.from_user.id)

    await callback.message.edit_text(
        "💰 DEMO BALANCE\n\n"
        f"Your balance: {game.balance} credits",
        reply_markup=main_menu()
    )

    await callback.answer()


# =========================
# PLAY
# =========================

@dp.callback_query(F.data == "play")
async def play(callback: CallbackQuery):

    game = get_game(callback.from_user.id)

    await callback.message.edit_text(
        "🎯 CHOOSE YOUR NUMBERS\n\n"
        "Select numbers from 1 to 100.\n\n"
        "🟢 = Selected\n\n"
        f"💰 Balance: {game.balance} credits",
        reply_markup=number_board(game.selected_numbers)
    )

    await callback.answer()


# =========================
# NUMBER SELECTION
# =========================

@dp.callback_query(F.data.startswith("number:"))
async def choose_number(callback: CallbackQuery):

    game = get_game(callback.from_user.id)

    number = int(callback.data.split(":")[1])

    selected = game.select_number(number)

    if selected:
        await callback.answer(
            f"✅ {number} selected"
        )
    else:
        await callback.answer(
            f"❌ {number} removed"
        )

    await callback.message.edit_reply_markup(
        reply_markup=number_board(
            game.selected_numbers
        )
    )


# =========================
# CLEAR
# =========================

@dp.callback_query(F.data == "clear")
async def clear(callback: CallbackQuery):

    game = get_game(callback.from_user.id)

    game.clear()

    await callback.message.edit_reply_markup(
        reply_markup=number_board(
            game.selected_numbers
        )
    )

    await callback.answer(
        "All numbers cleared"
    )


# =========================
# CONFIRM
# =========================

@dp.callback_query(F.data == "confirm")
async def confirm(callback: CallbackQuery):

    game = get_game(callback.from_user.id)

    if not game.selected_numbers:

        await callback.answer(
            "⚠️ Select at least one number.",
            show_alert=True
        )

        return

    numbers = sorted(game.selected_numbers)

    numbers_text = ", ".join(
        str(number) for number in numbers
    )

    await callback.message.edit_text(
        "🎱 RODAS FRIEND ZONE\n\n"
        "✅ NUMBERS CONFIRMED\n\n"
        f"Your numbers:\n{numbers_text}\n\n"
        "🎯 Bingo drawing will be added next."
    )

    await callback.answer(
        "Numbers confirmed!"
    )


# =========================
# BACK
# =========================

@dp.callback_query(F.data == "back")
async def back(callback: CallbackQuery):

    game = get_game(callback.from_user.id)

    await callback.message.edit_text(
        "🎱 RODAS FRIEND ZONE\n\n"
        f"💰 Demo Balance: {game.balance} credits\n\n"
        "Choose an option:",
        reply_markup=main_menu()
    )

    await callback.answer()


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

    while True:
        await __import__("asyncio").sleep(3600)


# =========================
# RUN
# =========================

if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
