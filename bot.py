import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import random
import time
from urllib.parse import parse_qsl

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

import config
from config import (
    BOT_TOKEN,
    DRAW_INTERVAL_SECONDS,
    GAME_OVER_SECONDS,
    PORT,
    SELECTION_SECONDS,
    WEB_FOLDER,
    WEBHOOK_PATH,
    WEBHOOK_SECRET,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("rodas-friend-zone-bingo")

# ============================================================
# SETTINGS
# ============================================================

BET_AMOUNT = 10
PRIZE_PERCENT = 0.75          # winner(s) share 75% of all bets
CARTELA_MIN = 1
CARTELA_MAX = 100

APP_URL = (
    config.RENDER_EXTERNAL_URL or "https://rodasfriendzone.onrender.com"
).rstrip("/")
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# Set ALLOW_GUEST=1 in Render ONLY for testing outside Telegram.
# With it off, every game request must carry valid Telegram login data.
ALLOW_GUEST = os.getenv("ALLOW_GUEST", "0") == "1"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def now():
    return time.time()


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ============================================================
# CARTELAS (1-100, always the same card for the same number)
# ============================================================

FIXED_CARTELAS = {
    35: [
        [9, 28, 36, 47, 61],
        [6, 27, 41, 50, 65],
        [13, 25, "FREE", 48, 73],
        [3, 17, 38, 55, 70],
        [14, 21, 31, 60, 68],
    ],
}


def build_cartela(number):
    if number in FIXED_CARTELAS:
        return FIXED_CARTELAS[number]

    rng = random.Random(50000 + number)
    columns = []
    for c in range(5):
        values = list(range(c * 15 + 1, c * 15 + 16))
        rng.shuffle(values)
        columns.append(values[:5])

    board = [[columns[c][r] for c in range(5)] for r in range(5)]
    board[2][2] = "FREE"
    return board


CARTELAS = {n: build_cartela(n) for n in range(CARTELA_MIN, CARTELA_MAX + 1)}


# ============================================================
# WIN PATTERNS
# ============================================================

PATTERNS = {}

for i in range(5):
    PATTERNS[f"horizontal-{i + 1}"] = [(i, c) for c in range(5)]
    PATTERNS[f"vertical-{i + 1}"] = [(r, i) for r in range(5)]

PATTERNS["diagonal-main"] = [(i, i) for i in range(5)]
PATTERNS["diagonal-other"] = [(i, 4 - i) for i in range(5)]
PATTERNS["four-corners"] = [(0, 0), (0, 4), (4, 0), (4, 4)]
PATTERNS["center-four"] = [(1, 1), (1, 3), (3, 1), (3, 3)]
PATTERNS["t-corners"] = (
    [(0, c) for c in range(5)]
    + [(r, 0) for r in range(1, 5)]
    + [(r, 4) for r in range(1, 5)]
)
PATTERNS["center-t"] = [(2, c) for c in range(5)] + [(r, 2) for r in (0, 1, 3, 4)]

PATTERN_NAMES = {
    "four-corners": "Four Corners",
    "center-four": "Center Four",
    "t-corners": "T Corners",
    "center-t": "Center T",
    "diagonal-main": "Diagonal",
    "diagonal-other": "Diagonal",
}


def pattern_label(pattern):
    if pattern in PATTERN_NAMES:
        return PATTERN_NAMES[pattern]
    if pattern.startswith("horizontal-"):
        return "Horizontal Line"
    if pattern.startswith("vertical-"):
        return "Vertical Line"
    return pattern


def winning_patterns(board, called):
    result = []
    for name, cells in PATTERNS.items():
        if all(board[r][c] == "FREE" or board[r][c] in called for r, c in cells):
            result.append(name)
    return result


# ============================================================
# GAME
# ============================================================

class Game:

    def __init__(self, number):
        self.number = number
        self.phase = "selection"          # selection -> playing -> result

        self.timer_started_at = None      # set when the FIRST cartela is picked

        self.players = {}                 # user_id -> cartela number or None
        self.names = {}
        self.boards = {}
        self.auto = {}
        self.proceeded = set()
        self.disqualified = set()
        self.bet_users = set()            # who paid, fixed when play starts

        self.pool = random.sample(range(1, 76), 75)
        self.called = []
        self.called_set = set()
        self.last_draw_at = None

        self.winner_ids = []
        self.winner_name = None
        self.winner_cartela = None
        self.winner_board = None
        self.winner_patterns = []
        self.prize = 0
        self.result_at = None

    # ---------------- counts ----------------

    def selected(self):
        return [u for u, c in self.players.items() if c is not None]

    def active(self):
        return [
            u for u in self.selected()
            if u not in self.disqualified and u in self.boards
        ]

    def total_bets(self):
        count = len(self.bet_users) if self.bet_users else len(self.selected())
        return count * BET_AMOUNT

    def prize_pool(self):
        return round(self.total_bets() * PRIZE_PERCENT, 2)

    # ---------------- timers ----------------

    def start_timer(self):
        if self.timer_started_at is None and self.selected():
            self.timer_started_at = now()
            logger.info("Game %s: countdown started.", self.number)

    def countdown(self):
        if self.timer_started_at is None:
            return 0
        return max(0, math.ceil(SELECTION_SECONDS - (now() - self.timer_started_at)))

    def result_left(self):
        if self.result_at is None:
            return GAME_OVER_SECONDS
        return max(0, math.ceil(GAME_OVER_SECONDS - (now() - self.result_at)))

    # ---------------- player actions ----------------

    def join(self, user_id, name=None):
        if user_id in self.disqualified:
            return False
        self.players.setdefault(user_id, None)
        self.auto.setdefault(user_id, True)
        if name:
            self.names[user_id] = name
        return True

    def select(self, user_id, number, name=None):
        if self.phase != "selection":
            return False, "Cartela selection is closed."
        if not (CARTELA_MIN <= number <= CARTELA_MAX):
            return False, "Invalid cartela number."
        if not self.join(user_id, name):
            return False, "You cannot join this round."

        # Tapping your own cartela again removes it
        if self.players[user_id] == number:
            self.deselect(user_id)
            return True, "Cartela removed."

        if number in self.players.values():
            return False, "That cartela is already taken."

        self.players[user_id] = number
        self.boards[user_id] = [row[:] for row in CARTELAS[number]]
        self.proceeded.discard(user_id)
        self.start_timer()
        return True, "Cartela selected."

    def deselect(self, user_id):
        if user_id in self.players:
            self.players[user_id] = None
        self.boards.pop(user_id, None)
        self.proceeded.discard(user_id)
        if not self.selected():
            self.timer_started_at = None

    def proceed(self, user_id):
        if self.phase != "selection":
            return False, "Selection is already closed."
        if self.players.get(user_id) is None:
            return False, "Pick a cartela first."
        self.proceeded.add(user_id)
        return True, "Good luck!"

    def leave(self, user_id):
        # Only possible before the game starts (nobody has paid yet)
        if self.phase != "selection":
            return False
        for store in (self.players, self.names, self.boards, self.auto):
            store.pop(user_id, None)
        self.proceeded.discard(user_id)
        if not self.selected():
            self.timer_started_at = None
        return True

    def set_auto(self, user_id, enabled):
        if user_id not in self.players:
            return False
        self.auto[user_id] = bool(enabled)
        return True

    # ---------------- play ----------------

    def start_playing(self):
        self.bet_users = set(self.selected())
        self.phase = "playing"
        self.last_draw_at = now()
        logger.info("Game %s started with %s player(s).", self.number, len(self.bet_users))

    def draw(self):
        if not self.pool:
            return None
        number = self.pool.pop()
        self.called.append(number)
        self.called_set.add(number)
        self.last_draw_at = now()
        return number

    def auto_winners(self):
        winners = []
        for user_id in self.active():
            if not self.auto.get(user_id, True):
                continue
            patterns = winning_patterns(self.boards[user_id], self.called_set)
            if patterns:
                winners.append((user_id, patterns))
        return winners

    def claim(self, user_id):
        if self.phase != "playing":
            return False, "The game is not running."
        if user_id in self.disqualified:
            return False, "You are disqualified from this round."
        board = self.boards.get(user_id)
        if not board:
            return False, "You have no cartela in this game."

        patterns = winning_patterns(board, self.called_set)
        if patterns:
            self.finish([(user_id, patterns)])
            return True, "BINGO! You won!"

        self.disqualified.add(user_id)
        self.boards.pop(user_id, None)
        return False, "Wrong BINGO. You are out of this round."

    def finish(self, winners):
        """winners = [(user_id, [patterns])]. Empty list = nobody won."""
        self.phase = "result"
        self.result_at = now()

        if not winners:
            logger.info("Game %s ended with no winner.", self.number)
            return

        first_id, first_patterns = winners[0]
        self.winner_ids = [u for u, _ in winners]
        self.winner_name = " & ".join(self.names.get(u, "Player") for u in self.winner_ids)
        self.winner_cartela = self.players.get(first_id)
        self.winner_board = [row[:] for row in self.boards[first_id]]
        self.winner_patterns = first_patterns
        self.prize = round(self.prize_pool() / len(winners), 2)
        logger.info(
            "Game %s winner(s): %s | prize each: %s",
            self.number, self.winner_name, self.prize,
        )

    # ---------------- state for the Mini App ----------------

    def serialize(self, user_id):
        mine = self.players.get(user_id)
        is_dq = user_id in self.disqualified
        in_result = self.phase == "result"

        winning_cells = []
        for pattern in self.winner_patterns:
            winning_cells.extend([list(cell) for cell in PATTERNS[pattern]])

        return {
            "game_number": self.number,
            "phase": self.phase,
            "selection_remaining": self.countdown() if self.phase == "selection" else 0,
            "result_remaining": self.result_left() if in_result else 0,

            "players": len(self.bet_users) if self.bet_users else len(self.selected()),
            "joined": user_id in self.players,
            "my_cartela": mine,
            "proceeded": user_id in self.proceeded,
            "taken_cartelas": [c for c in self.players.values() if c is not None],
            "my_board": None if is_dq else self.boards.get(user_id),
            "auto": self.auto.get(user_id, True),
            "disqualified": is_dq,

            "called": self.called,
            "last_called": self.called[-1] if self.called else None,

            "winner": self.winner_name,
            "winner_user_ids": self.winner_ids,
            "winner_cartela": self.winner_cartela,
            "winner_pattern": pattern_label(self.winner_patterns[0]) if self.winner_patterns else None,
            "winner_board": self.winner_board,
            "winning_cells": winning_cells,

            "bet": BET_AMOUNT,
            "total_bets": self.total_bets(),
            "prize": self.prize if in_result else self.prize_pool(),
        }


# ============================================================
# GLOBAL STATE + GAME LOOP
# ============================================================

lock = asyncio.Lock()
game_counter = 1
game = Game(game_counter)
loop_task = None


def new_game():
    global game, game_counter
    game_counter += 1
    game = Game(game_counter)
    logger.info("Created game %s.", game_counter)


def tick():
    g = game

    if g.phase == "selection":
        if not g.selected():
            g.timer_started_at = None
        else:
            g.start_timer()
            if g.countdown() <= 0:
                g.start_playing()

    elif g.phase == "playing":
        if not g.active():
            g.finish([])                        # everybody is out
            return
        if now() - g.last_draw_at >= DRAW_INTERVAL_SECONDS:
            number = g.draw()
            if number is None:
                g.finish([])                    # all 75 called, no winner
                return
            winners = g.auto_winners()
            if winners:
                g.finish(winners)

    elif g.phase == "result":
        if g.result_left() <= 0:
            new_game()


async def game_loop():
    logger.info("Game loop started.")
    while True:
        try:
            async with lock:
                tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error in game loop.")
        await asyncio.sleep(0.25)


# ============================================================
# TELEGRAM LOGIN CHECK (so nobody can pretend to be someone else)
# ============================================================

def verify_init_data(init_data, max_age=86400):
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", None)
        if not received:
            return None

        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None
        if now() - int(pairs.get("auth_date", "0")) > max_age:
            return None

        user = json.loads(pairs["user"])
        return user if "id" in user else None
    except Exception:
        return None


def get_user(request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if init_data:
        user = verify_init_data(init_data)
        if user:
            return str(user["id"]), user.get("first_name") or "Player"
        return None

    if ALLOW_GUEST:
        return (
            request.query.get("user_id") or "guest",
            request.query.get("user_name") or "Player",
        )
    return None


@web.middleware
async def auth_middleware(request, handler):
    if request.path.startswith("/api/") and request.path != "/api/cartelas":
        user = get_user(request)
        if not user:
            return web.json_response(
                {"success": False, "ok": False, "message": "Open this game from Telegram."},
                status=401,
            )
        request["uid"], request["name"] = user
    return await handler(request)


# ============================================================
# WEB ROUTES
# ============================================================

def reply(user_id, ok=True, message=None, status=200):
    payload = game.serialize(user_id)
    payload["success"] = ok
    payload["ok"] = ok
    if message:
        payload["message"] = message
    return web.json_response(payload, status=status)


async def read_json(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def index_handler(request):
    path = os.path.join(WEB_FOLDER, "index.html")
    if not os.path.exists(path):
        return web.Response(text="<h1>Rodas Friend Zone</h1><p>index.html not found.</p>",
                            content_type="text/html")
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def health_handler(request):
    return web.json_response({"status": "ok", "game": game.number, "phase": game.phase})


async def cartelas_handler(request):
    return web.json_response({str(n): board for n, board in CARTELAS.items()})


async def state_handler(request):
    async with lock:
        return reply(request["uid"])


async def join_handler(request):
    uid, name = request["uid"], request["name"]
    async with lock:
        if game.phase != "selection":
            return reply(uid, False, "A game is in progress. Join the next round.", 400)
        ok = game.join(uid, name)
        return reply(uid, ok, "Joined." if ok else "You cannot join this round.", 200 if ok else 400)


async def select_handler(request):
    uid, name = request["uid"], request["name"]
    data = await read_json(request)
    value = data.get("cartela", data.get("number"))

    async with lock:
        if value in (None, ""):
            if game.phase == "selection":
                game.deselect(uid)
            return reply(uid, True, "Cartela removed.")

        ok, message = game.select(uid, safe_int(value), name)
        return reply(uid, ok, message, 200 if ok else 400)


async def proceed_handler(request):
    uid = request["uid"]
    async with lock:
        ok, message = game.proceed(uid)
        return reply(uid, ok, message, 200 if ok else 400)


async def leave_handler(request):
    uid = request["uid"]
    async with lock:
        ok = game.leave(uid)
        return reply(uid, ok, None if ok else "You can't leave once the game has started.",
                     200 if ok else 400)


async def auto_handler(request):
    uid = request["uid"]
    data = await read_json(request)
    enabled = data.get("auto", data.get("enabled", True))
    if isinstance(enabled, str):
        enabled = enabled.lower() in ("true", "1", "yes", "on")

    async with lock:
        ok = game.set_auto(uid, enabled)
        return reply(uid, ok, ("Auto ON." if enabled else "Auto OFF.") if ok else "Join first.",
                     200 if ok else 400)


async def claim_handler(request):
    uid = request["uid"]
    async with lock:
        ok, message = game.claim(uid)
        return reply(uid, ok, message, 200 if ok else 400)


# ============================================================
# TELEGRAM BOT
# ============================================================

def play_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🎯 PLAY BINGO", web_app=WebAppInfo(url=APP_URL))
        ]]
    )


async def configure_menu():
    commands = [
        BotCommand(command="start", description="Start Rodas Friend Zone"),
        BotCommand(command="play", description="Play Bingo"),
        BotCommand(command="deposit", description="Deposit"),
        BotCommand(command="balance", description="Check balance"),
        BotCommand(command="withdraw", description="Withdraw"),
        BotCommand(command="transfer", description="Transfer"),
        BotCommand(command="instruction", description="How to play"),
        BotCommand(command="invite", description="Invite friends"),
        BotCommand(command="support", description="Support"),
    ]
    await bot.set_my_commands(commands=commands, scope=BotCommandScopeAllPrivateChats())
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


@dp.message(Command("start"))
async def cmd_start(message: Message):
    name = message.from_user.first_name if message.from_user else "Player"
    await message.answer(
        f"👋 Welcome, {name}!\n\n🎯 <b>Rodas Friend Zone Bingo</b>\n\n"
        "Tap the button below to open the game.",
        reply_markup=play_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("play"))
@dp.message(Command("bingo"))
async def cmd_play(message: Message):
    await message.answer(
        "🎯 <b>Rodas Friend Zone Bingo</b>\n\nTap the button below to open the game.",
        reply_markup=play_keyboard(),
        parse_mode=ParseMode.HTML,
    )


def simple_reply(text):
    async def handler(message: Message):
        await message.answer(text, parse_mode=ParseMode.HTML)
    return handler


dp.message.register(simple_reply("💰 <b>Deposit</b>\n\nDeposits will be available here."), Command("deposit"))
dp.message.register(simple_reply("💳 <b>Balance</b>\n\nYour balance will be shown here."), Command("balance"))
dp.message.register(simple_reply("💸 <b>Withdraw</b>\n\nWithdrawals will be available here."), Command("withdraw"))
dp.message.register(simple_reply("🔄 <b>Transfer</b>\n\nTransfers will be available here."), Command("transfer"))
dp.message.register(
    simple_reply("👥 <b>Invite Friends</b>\n\nInvite your friends to Rodas Friend Zone and play Bingo together."),
    Command("invite"),
)
dp.message.register(
    simple_reply("🆘 <b>Support</b>\n\nContact the Rodas Friend Zone administrator."),
    Command("support"),
)
dp.message.register(
    simple_reply(
        "📖 <b>How to Play</b>\n\n"
        "1️⃣ Tap PLAY BINGO, then Join.\n"
        "2️⃣ Choose one cartela from 1–100. Tap it again to remove it.\n"
        "3️⃣ The countdown starts when the first player picks a cartela.\n"
        "4️⃣ Numbers from 1–75 are called one by one.\n"
        "5️⃣ Auto ON marks and checks your card for you. With Auto OFF, "
        "mark it yourself and press BINGO!\n"
        "6️⃣ A wrong BINGO takes you out of the round.\n\n"
        "🏆 Winning patterns: horizontal line, vertical line, diagonal, "
        "four corners, center four, T corners, center T."
    ),
    Command("instruction"),
)


# ============================================================
# WEBHOOK + STARTUP
# ============================================================

async def telegram_webhook(request):
    if WEBHOOK_SECRET:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return web.json_response({"ok": False}, status=403)

    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
        return web.json_response({"ok": True})
    except Exception:
        logger.exception("Webhook update failed.")
        return web.json_response({"ok": False}, status=500)


async def on_startup(app):
    global loop_task

    # These two must never stop the web server from starting.
    try:
        await configure_menu()
    except Exception:
        logger.exception("Could not configure the Telegram menu.")

    try:
        await bot.set_webhook(
            url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=False,
        )
        info = await bot.get_webhook_info()
        logger.info("Webhook: url=%s pending=%s last_error=%s",
                    info.url, info.pending_update_count, info.last_error_message)
    except Exception:
        logger.exception("Could not set the Telegram webhook.")

    loop_task = asyncio.create_task(game_loop())
    logger.info("Rodas Friend Zone Bingo is ready on %s", APP_URL)


async def on_cleanup(app):
    global loop_task
    if loop_task:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
    await bot.session.close()


def create_app():
    app = web.Application(middlewares=[auth_middleware])

    app.router.add_get("/", index_handler)
    app.router.add_get("/health", health_handler)

    app.router.add_get("/api/cartelas", cartelas_handler)
    app.router.add_get("/api/state", state_handler)
    app.router.add_post("/api/join", join_handler)
    app.router.add_post("/api/select", select_handler)
    app.router.add_post("/api/proceed", proceed_handler)
    app.router.add_post("/api/leave", leave_handler)
    app.router.add_post("/api/auto", auto_handler)
    app.router.add_post("/api/claim-bingo", claim_handler)

    app.router.add_post(WEBHOOK_PATH, telegram_webhook)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    logger.info("Starting on port %s", PORT)
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
