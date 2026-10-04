import asyncio, hashlib, hmac, json, os, random, secrets, string, time
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

BETS = [10, 20, 50, 100]   # room prices (birr per cartela)
CALL_EVERY = 4             # seconds between calls
LOBBY_SECONDS = 30         # countdown after the FIRST player picks a cartela
START_BALANCE = 1000       # free test money (set 0 for real money)
CARD_COUNT = 100
MIN_PLAYERS = 1            # change to 2 or more for real games
IDLE_KICK = 60             # seconds without contact before a player is removed
MIN_DEPOSIT = 10
MIN_WITHDRAW = 50

INDEX_FILE = Path(__file__).parent / "web" / "index.html"

rooms = {b: {"bet": b, "phase": "lobby", "called": [], "players": {},
             "winner": None, "prize": 0, "round": 0, "deadline": None,
             "seq": [], "secret": "", "hash": ""}
         for b in BETS}
wallets, names, seen = {}, {}, {}
joined, usernames, wins, won = {}, {}, {}, {}      # profile data
history, winlog = {}, {}                           # transactions / wins list
games, game_counter = [], [0]                      # finished games (History page)
pending, req_counter = {}, [0]          # deposit / withdraw requests

dp = Dispatcher()


# ---------- winning patterns ----------
PATTERNS = (
    [[(r, c) for c in range(5)] for r in range(5)]            # horizontal lines
    + [[(r, c) for r in range(5)] for c in range(5)]          # vertical lines
    + [[(i, i) for i in range(5)], [(i, 4 - i) for i in range(5)]]   # diagonals
    + [[(0, 0), (0, 4), (4, 0), (4, 4)],                      # four corners
       [(1, 1), (1, 3), (3, 1), (3, 3)],                      # center four
       [(0, 0), (0, 4), (1, 2), (2, 2), (3, 2)],              # T corners
       [(1, 1), (1, 2), (1, 3), (2, 2), (3, 2)]]              # center T
)


def has_bingo(card, called):
    hit = lambda r, c: card[r][c] == 0 or card[r][c] in called
    return any(all(hit(r, c) for r, c in p) for p in PATTERNS)


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


def log(uid, kind, amount, note=""):
    """Save a transaction for the profile page (keeps the last 50)."""
    h = history.setdefault(uid, [])
    h.insert(0, {"t": int(time.time()), "k": kind, "a": amount, "n": note})
    del h[50:]


def touch(user):
    uid = user["id"]
    wallets.setdefault(uid, START_BALANCE)
    joined.setdefault(uid, int(time.time()))
    names[uid] = user.get("first_name", "Player")
    usernames[uid] = user.get("username", "") or ""
    seen[uid] = time.time()
    return uid


def make_card(no: int):
    r = random.Random(no)
    cols = [r.sample(range(c * 15 + 1, c * 15 + 16), 5) for c in range(5)]
    cols[2][2] = 0
    return [[cols[c][row] for c in range(5)] for row in range(5)]


def room_of(uid):
    """The room this player is currently in (or None)."""
    for g in rooms.values():
        if uid in g["players"]:
            return g
    return None


def remove_player(uid):
    g = room_of(uid)
    if not g:
        return
    if g["phase"] == "lobby":
        wallets[uid] = wallets.get(uid, 0) + g["bet"]     # refund
        log(uid, "refund", g["bet"], f"Room {g['bet']} refund")
    g["players"].pop(uid, None)
    if g["phase"] == "lobby" and not g["players"]:
        g["deadline"] = None                              # stop countdown


def lobby_time(g):
    if g["phase"] != "lobby":
        return 0
    if g["deadline"] is None:
        return LOBBY_SECONDS
    return max(0, int(g["deadline"] - time.time()))


def record_game(g):
    """Save a finished game so it shows on the History page."""
    if not g["players"]:
        return
    game_counter[0] += 1
    wid = g["winner"]
    ref = "".join(random.choices(string.ascii_uppercase, k=2)) + \
          "".join(random.choices(string.digits, k=4))
    games.insert(0, {
        "id": game_counter[0], "bet": g["bet"], "t": int(time.time()),
        "winner": names.get(wid, "Player") if wid else "",
        "wid": wid, "board": g["players"].get(wid) if wid else None,
        "ref": ref, "calls": len(g["called"]),
        "prize": g["prize"] if wid else 0,
        "called": list(g["called"]), "seq": list(g["seq"]),
        "secret": g["secret"], "hash": g["hash"],
        "pl": list(g["players"].keys()),
    })
    del games[300:]


def summ(r, uid):
    return {"id": r["id"], "bet": r["bet"], "t": r["t"],
            "winner": r["winner"], "board": r["board"], "ref": r["ref"],
            "calls": r["calls"], "prize": r["prize"],
            "won": r["wid"] == uid, "mine": uid in r["pl"]}


# ---------- background jobs ----------
async def reaper():
    """Remove players who closed the app without pressing LEAVE."""
    while True:
        await asyncio.sleep(5)
        now = time.time()
        for g in rooms.values():
            for uid in list(g["players"]):
                if now - seen.get(uid, 0) > IDLE_KICK:
                    remove_player(uid)


async def run_round(g):
    g.update(called=[], phase="lobby", winner=None, prize=0,
             round=g["round"] + 1, deadline=None,
             seq=[], secret="", hash="")

    # wait for the first player, then count down
    while g["deadline"] is None or time.time() < g["deadline"]:
        await asyncio.sleep(1)

    if len(g["players"]) < MIN_PLAYERS:
        g["deadline"] = time.time() + LOBBY_SECONDS
        while g["deadline"] is not None and time.time() < g["deadline"]:
            await asyncio.sleep(1)
        if len(g["players"]) < MIN_PLAYERS:
            for uid in list(g["players"]):
                wallets[uid] = wallets.get(uid, 0) + g["bet"]
                log(uid, "refund", g["bet"], f"Room {g['bet']} refund")
            g["players"].clear()
            return

    # provably fair: pick all numbers + secret first, publish the hash
    g["seq"] = random.sample(range(1, 76), 75)
    g["secret"] = "".join(secrets.choice(string.ascii_letters) for _ in range(16))
    g["hash"] = hashlib.sha256(
        f"{g['secret']}:{','.join(map(str, g['seq']))}".encode()).hexdigest()

    g["phase"] = "playing"
    g["prize"] = int(len(g["players"]) * g["bet"] * 0.8)
    for n in g["seq"]:
        if g["phase"] != "playing" or not g["players"]:
            break
        g["called"].append(n)
        await asyncio.sleep(CALL_EVERY)

    g["phase"] = "finished"
    record_game(g)
    await asyncio.sleep(8)
    g["players"].clear()


async def room_loop(g):
    while True:
        try:
            await run_round(g)
        except Exception as e:                 # never let a room die
            print("room loop error:", repr(e))
            g["players"].clear()
            g["phase"] = "lobby"
            g["deadline"] = None
            await asyncio.sleep(2)


async def game_loop():
    await asyncio.gather(*[room_loop(g) for g in rooms.values()])


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
    mine = room_of(uid)
    try:
        asked = rooms.get(int(req.query.get("room", 0)))
    except (TypeError, ValueError):
        asked = None
    g = mine or asked or rooms[BETS[0]]
    my = g["players"].get(uid)
    n = len(g["players"])
    return web.json_response({
        "phase": g["phase"], "time": lobby_time(g), "round": g["round"],
        "players": n, "bet": g["bet"], "derash": int(n * g["bet"] * 0.8),
        "wallet": wallets[uid], "name": names[uid],
        "count": CARD_COUNT, "taken": list(g["players"].values()),
        "myCard": my, "card": make_card(my) if my else None,
        "called": g["called"],
        "winner": names.get(g["winner"]), "prize": g["prize"],
        "room": g["bet"], "inRoom": mine["bet"] if mine else None,
        "rooms": [{"bet": r["bet"], "phase": r["phase"],
                   "players": len(r["players"]),
                   "win": int(len(r["players"]) * r["bet"] * 0.8),
                   "time": lobby_time(r)} for r in rooms.values()],
    })


async def api_profile(req):
    user = verify(req.query.get("initData", ""))
    if not user:
        return web.json_response({"error": "auth"})
    uid = touch(user)
    return web.json_response({
        "id": uid, "name": names[uid], "username": usernames.get(uid, ""),
        "balance": wallets[uid], "joined": joined[uid],
        "wins": wins.get(uid, 0), "won": won.get(uid, 0),
        "tx": history.get(uid, [])[:30],
        "winlist": winlog.get(uid, [])[:30],
    })


async def api_history(req):
    user = verify(req.query.get("initData", ""))
    if not user:
        return web.json_response({"error": "auth"})
    uid = touch(user)
    scope = req.query.get("scope", "recent")
    try:
        bet = int(req.query.get("bet", 0))
    except ValueError:
        bet = 0
    q = req.query.get("q", "").strip().lstrip("#")
    out = []
    for r in games:
        if bet and r["bet"] != bet:
            continue
        if scope == "mine" and uid not in r["pl"]:
            continue
        if q and q not in str(r["id"]):
            continue
        out.append(summ(r, uid))
        if len(out) >= 50:
            break
    return web.json_response({"list": out})


async def api_game(req):
    user = verify(req.query.get("initData", ""))
    if not user:
        return web.json_response({"error": "auth"})
    uid = touch(user)
    try:
        gid = int(req.query.get("id", 0))
    except ValueError:
        gid = 0
    for r in games:
        if r["id"] == gid:
            d = summ(r, uid)
            d.update(called=r["called"], seq=r["seq"],
                     secret=r["secret"], hash=r["hash"])
            return web.json_response(d)
    return web.json_response({"error": "not found"})


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
    try:
        g = rooms.get(int(body.get("room")))
    except (TypeError, ValueError):
        g = None
    if not g:
        return web.json_response({"ok": False, "error": "Bad room"})
    if g["phase"] != "lobby":
        return web.json_response({"ok": False, "error": "Round already started"})
    if room_of(uid):
        return web.json_response({"ok": False,
                                  "error": "You already have a cartela this game"})
    try:
        card = int(body.get("card", 0))
    except (TypeError, ValueError):
        card = 0
    if not 1 <= card <= CARD_COUNT:
        return web.json_response({"ok": False, "error": "Bad card number"})
    if card in g["players"].values():
        return web.json_response({"ok": False, "error": "Card already taken"})
    if wallets[uid] < g["bet"]:
        return web.json_response({"ok": False, "error": "Not enough balance"})

    wallets[uid] -= g["bet"]
    log(uid, "bet", -g["bet"], f"Room {g['bet']} · cartela {card}")
    g["players"][uid] = card
    if g["deadline"] is None:                  # first player starts the clock
        g["deadline"] = time.time() + LOBBY_SECONDS
    return web.json_response({"ok": True})


async def api_bingo(req):
    body = await req.json()
    user = verify(body.get("initData", ""))
    if not user:
        return web.json_response({"ok": False})
    uid = user["id"]
    g = room_of(uid)
    if not g or g["phase"] != "playing":
        return web.json_response({"ok": False})
    if has_bingo(make_card(g["players"][uid]), set(g["called"])):
        g["phase"] = "finished"
        g["winner"] = uid
        wallets[uid] = wallets.get(uid, 0) + g["prize"]
        wins[uid] = wins.get(uid, 0) + 1
        won[uid] = won.get(uid, 0) + g["prize"]
        wl = winlog.setdefault(uid, [])
        wl.insert(0, {"t": int(time.time()), "bet": g["bet"], "prize": g["prize"]})
        del wl[50:]
        log(uid, "win", g["prize"], f"Won in room {g['bet']}")
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
    wallets.setdefault(u.id, START_BALANCE)
    joined.setdefault(u.id, int(time.time()))
    names[u.id] = u.first_name or "Player"
    usernames[u.id] = u.username or ""
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
    log(uid, "withdraw", -amount, "Withdraw request")
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
        log(uid, "transfer", -amount, f"Transfer to {names.get(to, to)}")
        log(to, "transfer", amount, f"Transfer from {names[uid]}")
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
        "1) Tap /play and choose a room (10, 20, 50 or 100 birr).\n"
        "2) Choose a cartela number. It costs the room price.\n"
        "3) When the countdown ends, numbers are called one by one.\n"
        "4) Keep Auto on to mark numbers automatically.\n"
        "5) Complete any winning pattern, then press BINGO WIN.\n\n"
        "🏆 Winning Patterns:\n"
        "• Horizontal line\n• Vertical line\n• Diagonal\n• Four corners\n"
        "• Center four\n• T corners\n• Center T\n\n"
        "Complete any pattern to win the round prize!\n\n"
        "The first player to press BINGO WIN with a real pattern wins the prize.")


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
            log(uid, "deposit", amount, "Deposit approved")
            note = f"✅ Your deposit of {amount} birr was approved."
        else:
            note = f"❌ Your deposit of {amount} birr was rejected. Contact {SUPPORT}."
    else:
        if approved:
            note = f"✅ Your withdrawal of {amount} birr was paid."
        else:
            wallets[uid] = wallets.get(uid, 0) + amount        # give it back
            log(uid, "refund", amount, "Withdraw rejected")
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
        web.get("/api/profile", api_profile),
        web.get("/api/history", api_history),
        web.get("/api/game", api_game),
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
