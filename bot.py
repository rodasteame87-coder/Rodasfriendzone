import asyncio, hashlib, hmac, json, os, random, time
from pathlib import Path
from urllib.parse import parse_qsl
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (Message, InlineKeyboardMarkup,
                           InlineKeyboardButton, WebAppInfo)

TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
PORT = int(os.getenv("PORT", 8080))
BET = 10               # price of 1 cartela
CALL_EVERY = 4         # seconds between calls
LOBBY_SECONDS = 30     # countdown after the FIRST player picks a cartela
START_BALANCE = 1000   # free test money
CARD_COUNT = 100       # numbers shown in the picker
MIN_PLAYERS = 1        # change to 2 or more for real games

INDEX_FILE = Path(__file__).parent / "web" / "index.html"

# deadline = None means: waiting for the first player (no countdown yet)
game = {"phase": "lobby", "called": [], "players": {}, "winner": None,
        "prize": 0, "round": 0, "deadline": None}
wallets, names = {}, {}


# ---------- helpers ----------
def verify(init_data: str):
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    got = data.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got) or "user" not in data:
        return None
    return json.loads(data["user"])


def touch(user):
    uid = user["id"]
    wallets.setdefault(uid, START_BALANCE)
    names[uid] = user.get("first_name", "Player")
    return uid


def make_card(no: int):
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
        game.update(called=[], phase="lobby", winner=None, prize=0,
                    round=game["round"] + 1, deadline=None)

        # wait until the first player picks a cartela, then count down
        while game["deadline"] is None or time.time() < game["deadline"]:
            await asyncio.sleep(1)

        if len(game["players"]) < MIN_PLAYERS:
            game["deadline"] = time.time() + LOBBY_SECONDS   # wait longer
            while time.time() < game["deadline"]:
                await asyncio.sleep(1)
            if len(game["players"]) < MIN_PLAYERS:
                for uid in game["players"]:                  # refund everyone
                    wallets[uid] = wallets.get(uid, 0) + BET
                game["players"].clear()
                continue

        game["phase"] = "playing"
        game["prize"] = int(len(game["players"]) * BET * 0.8)
        for n in random.sample(range(1, 76), 75):
            if game["phase"] != "playing" or not game["players"]:
                break
            game["called"].append(n)
            await asyncio.sleep(CALL_EVERY)
        game["phase"] = "finished"
        await asyncio.sleep(8)            # let everyone see the result
        game["players"].clear()


# ---------- pages / API ----------
async def index(request):
    if not INDEX_FILE.exists():
        return web.Response(text=f"File not found: {INDEX_FILE}", status=404)
    return web.FileResponse(INDEX_FILE)


async def api_state(req):
    user = verify(req.query.get("initData", ""))
    if not user:
        return web.json_response({"error": "auth"})
    uid = touch(user)
    my = game["players"].get(uid)
    if game["phase"] == "lobby":
        if game["deadline"] is None:
            left = LOBBY_SECONDS                 # not counting yet
        else:
            left = max(0, int(game["deadline"] - time.time()))
    else:
        left = 0
    n = len(game["players"])
    return web.json_response({
        "phase": game["phase"], "time": left, "round": game["round"],
        "players": n, "bet": BET, "derash": int(n * BET * 0.8),
        "wallet": wallets[uid], "name": names[uid],
        "count": CARD_COUNT, "taken": list(game["players"].values()),
        "myCard": my, "card": make_card(my) if my else None,
        "called": game["called"],
        "winner": names.get(game["winner"]), "prize": game["prize"],
    })


async def api_card(req):
    try:
        no = max(1, min(CARD_COUNT, int(req.query.get("card", 1))))
    except ValueError:
        no = 1
    return web.json_response({"card": make_card(no)})


async def api_join(req):
    body = await req.json()
    user = verify(body.get("initData", ""))
    if not user:
        return web.json_response({"ok": False, "error": "Open from Telegram"})
    uid = touch(user)
    if game["phase"] != "lobby":
        return web.json_response({"ok": False, "error": "Round already started"})
    if uid in game["players"]:
        return web.json_response({"ok": False,
                                  "error": "You already have a cartela this game"})
    try:
        card = int(body.get("card", 0))
    except (TypeError, ValueError):
        card = 0
    if not 1 <= card <= CARD_COUNT:
        return web.json_response({"ok": False, "error": "Bad card number"})
    if card in game["players"].values():
        return web.json_response({"ok": False, "error": "Card already taken"})
    if wallets[uid] < BET:
        return web.json_response({"ok": False, "error": "Not enough balance"})

    wallets[uid] -= BET
    game["players"][uid] = card
    if game["deadline"] is None:                 # first player starts the clock
        game["deadline"] = time.time() + LOBBY_SECONDS
    return web.json_response({"ok": True})


async def api_bingo(req):
    body = await req.json()
    user = verify(body.get("initData", ""))
    if not user or game["phase"] != "playing":
        return web.json_response({"ok": False})
    uid = user["id"]
    card_no = game["players"].get(uid)
    if not card_no:
        return web.json_response({"ok": False})
    if has_bingo(make_card(card_no), set(game["called"])):
        game["phase"] = "finished"
        game["winner"] = uid
        wallets[uid] = wallets.get(uid, 0) + game["prize"]
        return web.json_response({"ok": True})
    return web.json_response({"ok": False})


async def api_leave(req):
    body = await req.json()
    user = verify(body.get("initData", ""))
    if user:
        uid = user["id"]
        if uid in game["players"]:
            if game["phase"] == "lobby":
                wallets[uid] = wallets.get(uid, 0) + BET   # refund
            game["players"].pop(uid, None)
            if game["phase"] == "lobby" and not game["players"]:
                game["deadline"] = None                    # stop countdown
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
        web.get("/api/state", api_state),
        web.get("/api/card", api_card),
        web.post("/api/join", api_join),
        web.post("/api/bingo", api_bingo),
        web.post("/api/leave", api_leave),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    asyncio.create_task(game_loop())
    await dp.start_polling(bot)


asyncio.run(main())
