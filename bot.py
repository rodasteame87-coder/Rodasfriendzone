import asyncio, hashlib, hmac, json, os, random, time
from pathlib import Path
from urllib.parse import parse_qsl
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (Message, CallbackQuery, BotCommand,
                           InlineKeyboardMarkup, InlineKeyboardButton,
                           WebAppInfo)

TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))            # your Telegram ID
PAY_INFO = os.getenv("PAY_INFO", "Ask support for payment details")
SUPPORT = os.getenv("SUPPORT_USERNAME", "@your_support")
PORT = int(os.getenv("PORT", 8080))

BET = 10               # price of 1 cartela
CALL_EVERY = 4         # seconds between calls
LOBBY_SECONDS = 30     # countdown after the FIRST player picks a cartela
START_BALANCE = 1000   # free test money (set 0 for real money)
CARD_COUNT = 100
MIN_PLAYERS = 1        # change to 2 or more for real games
IDLE_KICK = 60
MIN_DEPOSIT = 10
MIN_WITHDRAW = 50

INDEX_FILE = Path(__file__).parent / "web" / "index.html"

game = {"phase": "lobby", "called": [], "players": {}, "winner": None,
        "prize": 0, "round": 0, "deadline": None}
wallets, names, seen = {}, {}, {}
pending, req_counter = {}, [0]          # deposit / withdraw requests

dp = Dispatcher()


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
    seen[uid] = time.time()
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


def remove_player(uid):
    if uid not in game["players"]:
        return
    if game["phase"] == "lobby":
        wallets[uid] = wallets.get(uid, 0) + BET
    game["players"].pop(uid, None)
    if game["phase"] == "lobby" and not game["players"]:
        game["deadline"] = None


# ---------- background jobs ----------
async def reaper():
    while True:
        await asyncio.sleep(5)
        now = time.time()
        for uid in list(game["players"]):
            if now - seen.get(uid, 0) > IDLE_KICK:
                remove_player(uid)


async def run_round():
    game.update(called=[], phase="lobby", winner=None, prize=0,
                round=game["round"] + 1, deadline=None)
    while game["deadline"] is None or time.time() < game["deadline"]:
        await asyncio.sleep(1)

    if len(game["players"]) < MIN_PLAYERS:
        game["deadline"] = time.time() + LOBBY_SECONDS
        while game["deadline"] is not None and time.time() < game["deadline"]:
            await asyncio.sleep(1)
        if len(game["players"]) < MIN_PLAYERS:
            for uid in list(game["players"]):
                wallets[uid] = wallets.get(uid, 0) + BET
            game["players"].clear()
            return

    game["phase"] = "playing"
    game["prize"] = int(len(game["players"]) * BET * 0.8)
    for n in random.sample(range(1, 76), 75):
        if game["phase"] != "playing" or not game["players"]:
            break
        game["called"].append(n)
        await asyncio.sleep(CALL_EVERY)

    game["phase"] = "finished"
    await asyncio.sleep(8)
    game["players"].clear()


async def game_loop():
    while True:
        try:
            await run_round()
        except Exception as e:
            print("game loop error:", repr(e))
            game["players"].clear()
            game["phase"] = "lobby"
            game["deadline"] = None
            await asyncio.sleep(2)


# ---------- web pages / API ----------
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
        left = LOBBY_SECONDS if game["deadline"] is None \
            else max(0, int(game["deadline"] - time.time()))
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
    if game["deadline"] is None:
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
        remove_player(user["id"])
    return web.json_response({"ok": True})


# ---------- bot commands ----------
def ensure(u):
    """Create the wallet the first time a user talks to the bot."""
    wallets.setdefault(u.id, START_BALANCE)
    names[u.id] = u.first_name or "Player"
    return u.id


def play_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎱 Play Bingo",
                             web_app=WebAppInfo(url=WEBAPP_URL))]])


def to_amount(text):
    try:
        v = int(text)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


@dp.message(CommandStart())
async def cmd_start(m: Message):
    ensure(m.from_user)
    await m.answer("🎯 Rodas Friend Zone Bingo\n\n"
                   "Tap the button below to open the Bingo game.",
                   reply_markup=play_kb())


@dp.message(Command("play"))
async def cmd_play(m: Message):
    ensure(m.from_user)
    await m.answer("Tap the button below to play 👇", reply_markup=play_kb())


@dp.message(Command("balance"))
async def cmd_balance(m: Message):
    uid = ensure(m.from_user)
    await m.answer(f"💰 Balance: {wallets[uid]:.2f} birr\n"
                   f"🆔 Your ID: {uid}")


@dp.message(Command("deposit"))
async def cmd_deposit(m: Message, command: CommandObject):
    uid = ensure(m.from_user)
    args = (command.args or "").split(maxsplit=1)
    amount = to_amount(args[0]) if args else None
    if not amount:
        await m.answer(
            "💰 Deposit\n\n"
            f"1) Send your money to:\n{PAY_INFO}\n\n"
            "2) Then send this message:\n"
            "/deposit <amount> <transaction number>\n\n"
            "Example:\n/deposit 100 FT24123ABC")
        return
    if amount < MIN_DEPOSIT:
        await m.answer(f"Minimum deposit is {MIN_DEPOSIT} birr.")
        return
    if not ADMIN_ID:
        await m.answer(f"Deposits are handled by support: {SUPPORT}")
        return
    ref = args[1] if len(args) > 1 else "(none)"
    req_counter[0] += 1
    rid = req_counter[0]
    pending[rid] = {"type": "deposit", "uid": uid, "amount": amount}
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"ok:{rid}"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"no:{rid}")]])
    await m.bot.send_message(
        ADMIN_ID,
        f"💰 Deposit request #{rid}\nUser: {names[uid]} ({uid})\n"
        f"Amount: {amount} birr\nReference: {ref}", reply_markup=kb)
    await m.answer(f"✅ Request #{rid} sent. You will get a message when it is approved.")


@dp.message(Command("withdraw"))
async def cmd_withdraw(m: Message, command: CommandObject):
    uid = ensure(m.from_user)
    args = (command.args or "").split(maxsplit=1)
    amount = to_amount(args[0]) if args else None
    if not amount or len(args) < 2:
        await m.answer(
            "🏧 Withdraw\n\n"
            "Send:\n/withdraw <amount> <your account or phone>\n\n"
            f"Example:\n/withdraw 200 0912345678\n\nMinimum: {MIN_WITHDRAW} birr")
        return
    if amount < MIN_WITHDRAW:
        await m.answer(f"Minimum withdraw is {MIN_WITHDRAW} birr.")
        return
    if wallets[uid] < amount:
        await m.answer("Not enough balance.")
        return
    if not ADMIN_ID:
        await m.answer(f"Withdrawals are handled by support: {SUPPORT}")
        return
    wallets[uid] -= amount                       # hold the money
    req_counter[0] += 1
    rid = req_counter[0]
    pending[rid] = {"type": "withdraw", "uid": uid, "amount": amount}
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Paid", callback_data=f"ok:{rid}"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"no:{rid}")]])
    await m.bot.send_message(
        ADMIN_ID,
        f"🏧 Withdraw request #{rid}\nUser: {names[uid]} ({uid})\n"
        f"Amount: {amount} birr\nSend to: {args[1]}", reply_markup=kb)
    await m.answer(f"✅ Request #{rid} sent. {amount} birr is on hold until it is processed.")


@dp.message(Command("transfer"))
async def cmd_transfer(m: Message, command: CommandObject):
    uid = ensure(m.from_user)
    args = (command.args or "").split()
    if len(args) != 2 or not args[0].isdigit() or not to_amount(args[1]):
        await m.answer(
            "🔁 Transfer\n\n"
            "Send:\n/transfer <user ID> <amount>\n\n"
            "Example:\n/transfer 123456789 50\n\n"
            "Ask your friend to send /balance to see their ID.")
        return
    to, amount = int(args[0]), int(args[1])
    if to == uid:
        await m.answer("You can't transfer to yourself.")
    elif to not in wallets:
        await m.answer("That user hasn't started the bot yet.")
    elif wallets[uid] < amount:
        await m.answer("Not enough balance.")
    else:
        wallets[uid] -= amount
        wallets[to] += amount
        await m.answer(f"✅ Sent {amount} birr to {names.get(to, to)}.\n"
                       f"New balance: {wallets[uid]:.2f}")
        try:
            await m.bot.send_message(
                to, f"💸 You received {amount} birr from {names[uid]}.")
        except Exception:
            pass


@dp.message(Command("instruction"))
async def cmd_instruction(m: Message):
    await m.answer(
        "📖 How to play\n\n"
        "1) Tap /play and press JOIN.\n"
        f"2) Choose a cartela number. It costs {BET} birr.\n"
        "3) When the countdown ends, numbers are called one by one.\n"
        "4) Keep Auto on to mark numbers automatically.\n"
        "5) Complete a row, column or diagonal, then press BINGO WIN.\n\n"
        "The first player to press BINGO WIN with a real bingo wins the prize.")


@dp.message(Command("invite"))
async def cmd_invite(m: Message):
    uid = ensure(m.from_user)
    me = await m.bot.get_me()
    await m.answer("👥 Invite your friends with this link:\n"
                   f"https://t.me/{me.username}?start=ref_{uid}")


@dp.message(Command("support"))
async def cmd_support(m: Message):
    await m.answer(f"🛟 Need help? Contact {SUPPORT}")


@dp.callback_query()
async def admin_buttons(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Not allowed", show_alert=True)
        return
    action, _, rid = (cb.data or "").partition(":")
    req = pending.pop(int(rid), None) if rid.isdigit() else None
    if not req:
        await cb.answer("Already handled")
        return
    uid, amount = req["uid"], req["amount"]
    approved = action == "ok"
    if req["type"] == "deposit":
        if approved:
            wallets[uid] = wallets.get(uid, 0) + amount
            note = f"✅ Your deposit of {amount} birr was approved."
        else:
            note = f"❌ Your deposit of {amount} birr was rejected. Contact {SUPPORT}."
    else:
        if approved:
            note = f"✅ Your withdrawal of {amount} birr was paid."
        else:
            wallets[uid] = wallets.get(uid, 0) + amount        # give it back
            note = f"❌ Your withdrawal was rejected. {amount} birr returned to your balance."
    try:
        await cb.bot.send_message(uid, note)
    except Exception:
        pass
    await cb.message.edit_text(
        cb.message.text + ("\n\n✅ DONE" if approved else "\n\n❌ REJECTED"))
    await cb.answer("Done")


# ---------- main ----------
async def main():
    bot = Bot(TOKEN)

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
    asyncio.create_task(reaper())

    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands([
        BotCommand(command="start", description="Start Rodas Friend Zone"),
        BotCommand(command="play", description="Play Bingo"),
        BotCommand(command="deposit", description="Deposit"),
        BotCommand(command="balance", description="Check balance"),
        BotCommand(command="withdraw", description="Withdraw"),
        BotCommand(command="transfer", description="Transfer"),
        BotCommand(command="instruction", description="How to play"),
        BotCommand(command="invite", description="Invite friends"),
        BotCommand(command="support", description="Support"),
    ])
    await dp.start_polling(bot)


asyncio.run(main())
