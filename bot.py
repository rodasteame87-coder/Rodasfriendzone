import asyncio, hashlib, hmac, json, os, random
from pathlib import Path
from urllib.parse import parse_qsl
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (Message, InlineKeyboardMarkup,
                           InlineKeyboardButton, WebAppInfo)

TOKEN = os.environ["BOT_TOKEN"]        # from BotFather
WEBAPP_URL = os.environ["WEBAPP_URL"]  # https://rodasfriendzone.onrender.com
PORT = int(os.getenv("PORT", 8080))
BET = 10
CALL_EVERY = 4        # seconds between calls
LOBBY_SECONDS = 30    # wait time before a round starts

INDEX_FILE = Path(__file__).parent / "web" / "index.html"

game = {"called": [], "players": {}, "phase": "lobby", "winner": None}


# ---------- helpers ----------
def verify(init_data: str):
    """Check Telegram's signature; return the user dict or None."""
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    got = data.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got) or "user" not in data:
        return None
    return json.loads(data["user"])


def make_card(no: int):
    """Same card number always gives the same card. 0 = FREE."""
    r = random.Random(no)
    cols = [r.sample(range(c * 15 + 1, c * 15 + 16), 5) for c in range(5)]
    cols[2][2] = 0
    return [[cols[c][row] for c in range(5)] for row in range(5)]


def has_bingo(card, called):
    hit = lambda r, c: card[r][c] == 0 or card[r][c] in called
    lines = []
    for i in range(5):
        lines.append([(i, c) for c in range(5)])
        lines.append([(r, i) for r in range(5)])
    lines.append([(i, i) for i in range(5)])
    lines.append([(i, 4 - i) for i in range(5)])
    return any(all(hit(r, c) for r, c in line) for line in lines)


# ---------- game loop ----------
async def game_loop():
    while True:
        game.update(called=[], phase="lobby", winner=None)
        await asyncio.sleep(LOBBY_SECONDS)
        if not game["players"]:
            continue
        game["phase"] = "playing"
        for n in random.sample(range(1, 76), 75):
            if game["phase"] != "playing":
                break
            game["called"].append(n)
            await asyncio.sleep(CALL_EVERY)
        await asyncio.sleep(5)
        game["players"].clear()


# ---------- web pages / API ----------
async def index(request):
    if not INDEX_FILE.exists():
        return web.Response(text=f"File not found: {INDEX_FILE}", status=404)
    return web.FileResponse(INDEX_FILE)


async def health(request):
    return web.Response(text="ok")


async def api_state(req):
    user = verify(req.query.get("initData", ""))
    if not user:
        return web.json_response({"error": "auth"}, status=401)
    try:
        card_no = max(1, min(400, int(req.query.get("card", 1))))
    except ValueError:
        card_no = 1
    if game["phase"] == "lobby":
        game["players"][user["id"]] = card_no
    n = len(game["players"])
    return web.json_response({
        "players": n,
        "derash": int(n * BET * 0.8),
        "bet": BET,
        "card": make_card(card_no),
        "cardNo": card_no,
        "called": game["called"],
    })


async def api_bingo(req):
    body = await req.json()
    user = verify(body.get("initData", ""))
    if not user or game["phase"] != "playing":
        return web.json_response({"ok": False})
    card_no = game["players"].get(user["id"])
    if not card_no:
        return web.json_response({"ok": False})
    if has_bingo(make_card(card_no), set(game["called"])):
        game["phase"] = "finished"
        game["winner"] = user["id"]
        return web.json_response({"ok": True})
    return web.json_response({"ok": False})


async def api_leave(req):
    body = await req.json()
    user = verify(body.get("initData", ""))
    if user:
        game["players"].pop(user["id"], None)
    return web.json_response({"ok": True})


# ---------- main ----------
async def main():
    bot = Bot(TOKEN)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(m: Message):
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎱 Play Bingo",
                                 web_app=WebAppInfo(url=WEBAPP_URL))]])
        await m.answer("Welcome to Rodas Friend Zone Bingo!", reply_markup=kb)

    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/health", health),
        web.get("/api/state", api_state),
        web.post("/api/bingo", api_bingo),
        web.post("/api/leave", api_leave),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    asyncio.create_task(game_loop())
    await dp.start_polling(bot)


asyncio.run(main())
