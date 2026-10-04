import asyncio, hashlib, hmac, html, json, math, os, random, re, secrets, string, time
from pathlib import Path
from urllib.parse import parse_qsl, quote
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (Message, CallbackQuery, BotCommand,
                           InlineKeyboardMarkup, InlineKeyboardButton,
                           ReplyKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardRemove, WebAppInfo)

TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))            # your Telegram ID
PAY_INFO = os.getenv("PAY_INFO", "Ask support for payment details")
SUPPORT = os.getenv("SUPPORT_USERNAME", "@your_support")
PORT = int(os.getenv("PORT", 8080))

# ---------- PAYMENT DETAILS ----------
CBE_ACCOUNT = os.getenv("CBE_ACCOUNT", "1000613735775")
CBE_NAME = os.getenv("CBE_NAME", "Rodas Teame")
TELEBIRR_PHONE = os.getenv("TELEBIRR_PHONE", "0978856625")
TELEBIRR_NAME = os.getenv("TELEBIRR_NAME", "Rodas Teame")
CBEBIRR_PHONE = os.getenv("CBEBIRR_PHONE", "0978856625")
CBEBIRR_NAME = os.getenv("CBEBIRR_NAME", "Rodas Teame")
DEPOSIT_SUPPORT = "@Rodasfriendzonesupport"
REF_BONUS_PERCENT = 10     # invite bonus: % of the invitee's FIRST deposit (0 = off)

BETS = [10, 20, 50, 100]   # room prices (birr per cartela)
CALL_EVERY = 4             # seconds between calls
LOBBY_SECONDS = 30         # countdown after the FIRST player picks a cartela
START_BALANCE = 1000       # free test money (set 0 for real money)
CARD_COUNT = 100
MIN_PLAYERS = 1            # change to 2 or more for real games
IDLE_KICK = 60             # seconds without contact before a player is removed
MIN_DEPOSIT = 10
MIN_WITHDRAW = 100
WIN_SCREEN_SECONDS = 10    # how long the winner / loser card stays

INDEX_FILE = Path(__file__).parent / "web" / "index.html"

rooms = {b: {"bet": b, "phase": "lobby", "called": [], "players": {},
             "winner": None, "prize": 0, "round": 0, "deadline": None,
             "seq": [], "secret": "", "hash": "",
             "wcells": [], "wname": "", "finish_at": 0,
             "winners": [], "wlist": [], "share": 0}
         for b in BETS}
wallets, names, seen = {}, {}, {}
joined, usernames, wins, won = {}, {}, {}, {}      # profile data
history, winlog = {}, {}                           # transactions / wins list
games, game_counter = [], [0]                      # finished games (History page)
pending, req_counter = {}, [0]          # deposit / withdraw requests
used_sms = set()                        # SMS already sent (no double use)
referrer, invited = {}, {}              # player -> who invited him / invite count
ref_paid, ref_earn = set(), {}          # first-deposit bonus already paid / total earned
wd_state = {}                           # players in the middle of a withdraw
phones = {}                             # player -> verified phone number

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
PATTERN_NAMES = (["Horizontal line"] * 5 + ["Vertical line"] * 5
                 + ["Diagonal"] * 2
                 + ["Four corners", "Center four", "T corners", "Center T"])


def has_bingo(card, called):
    hit = lambda r, c: card[r][c] == 0 or card[r][c] in called
    return any(all(hit(r, c) for r, c in p) for p in PATTERNS)


def winning_cells(card, called):
    """Name + cells of the first completed pattern (winner screen)."""
    hit = lambda r, c: card[r][c] == 0 or card[r][c] in called
    for p, nm in zip(PATTERNS, PATTERN_NAMES):
        if all(hit(r, c) for r, c in p):
            return nm, [list(x) for x in p]
    return "", []


def first_bingo_call(card, called_list):
    """Index of the call where this card first got a bingo (None if never)."""
    seen_nums = set()
    for i, n in enumerate(called_list):
        seen_nums.add(n)
        if has_bingo(card, seen_nums):
            return i
    return None


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


def finish_left(g):
    """Seconds left on the winner / loser card."""
    if g["phase"] != "finished" or not g["finish_at"]:
        return 0
    return max(0, math.ceil(g["finish_at"] - time.time()))


def record_game(g):
    """Save a finished game so it shows on the History page."""
    if not g["players"]:
        return
    game_counter[0] += 1
    wl = g.get("wlist", [])
    ref = "".join(random.choices(string.ascii_uppercase, k=2)) + \
          "".join(random.choices(string.digits, k=4))
    games.insert(0, {
        "id": game_counter[0], "bet": g["bet"], "t": int(time.time()),
        "winner": ", ".join(w["name"] for w in wl),
        "wids": [w["uid"] for w in wl],
        "board": ", ".join(str(w["board"]) for w in wl),
        "ref": ref, "calls": len(g["called"]),
        "prize": g["share"] if wl else 0,
        "called": list(g["called"]), "seq": list(g["seq"]),
        "secret": g["secret"], "hash": g["hash"],
        "pl": list(g["players"].keys()),
    })
    del games[300:]


def summ(r, uid):
    return {"id": r["id"], "bet": r["bet"], "t": r["t"],
            "winner": r["winner"], "board": r["board"], "ref": r["ref"],
            "calls": r["calls"], "prize": r["prize"],
            "won": uid in r["wids"], "mine": uid in r["pl"]}


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
             seq=[], secret="", hash="",
             wcells=[], wname="", finish_at=0,
             winners=[], wlist=[], share=0)

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
    if not g["finish_at"]:
        g["finish_at"] = time.time() + WIN_SCREEN_SECONDS
    record_game(g)
    await asyncio.sleep(max(0, g["finish_at"] - time.time()))
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
        "share": g.get("share", 0),
        "winners": [{"name": w["name"], "board": w["board"],
                     "card": make_card(w["board"]),
                     "pat": w["pat"], "cells": w["cells"],
                     "me": w["uid"] == uid} for w in g.get("wlist", [])],
        "wBoard": g["players"].get(g["winner"]) if g["winner"] else None,
        "wCard": make_card(g["players"][g["winner"]])
                 if g["winner"] in g["players"] else None,
        "wCells": g.get("wcells", []),
        "wName": g.get("wname", ""),
        "left": finish_left(g),
        "isWinner": uid in g.get("winners", []),
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
    if uid not in phones:
        return web.json_response({"ok": False,
                                  "error": "Press /start in the bot and share your phone number first"})
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
    if not g:
        return web.json_response({"ok": False})
    # already counted as a winner of this finished round (shared win)
    if g["phase"] == "finished" and uid in g.get("winners", []):
        return web.json_response({"ok": True})
    if g["phase"] != "playing":
        return web.json_response({"ok": False})
    called = set(g["called"])
    card = make_card(g["players"][uid])
    if has_bingo(card, called):
        # the earliest call that gave anybody a bingo decides the winners:
        # only players who finished on that SAME call share the prize,
        # players whose bingo came earlier or later do not share
        firsts = {u: first_bingo_call(make_card(no), g["called"])
                  for u, no in g["players"].items()}
        firsts = {u: i for u, i in firsts.items() if i is not None}
        best = min(firsts.values())
        winners = [u for u, i in firsts.items() if i == best]
        wcalled = set(g["called"][:best + 1])
        share = g["prize"] // len(winners)
        shared = len(winners) > 1
        g["phase"] = "finished"
        g["winner"] = winners[0]
        g["winners"] = winners
        g["share"] = share
        g["wlist"] = []
        for u in winners:
            no = g["players"][u]
            nm, cells = winning_cells(make_card(no), wcalled)
            g["wlist"].append({"uid": u, "name": names.get(u, "Player"),
                               "board": no, "pat": nm, "cells": cells})
            wallets[u] = wallets.get(u, 0) + share
            wins[u] = wins.get(u, 0) + 1
            won[u] = won.get(u, 0) + share
            wl = winlog.setdefault(u, [])
            wl.insert(0, {"t": int(time.time()), "bet": g["bet"], "prize": share})
            del wl[50:]
            log(u, "win", share,
                f"Won in room {g['bet']}" + (" (shared)" if shared else ""))
        g["wname"], g["wcells"] = g["wlist"][0]["pat"], g["wlist"][0]["cells"]
        g["finish_at"] = time.time() + WIN_SCREEN_SECONDS
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


# ---------- deposit menu (CBE / Telebirr / CBE Birr) ----------
DEPOSIT_MENU_TEXT = "እባክዎ የሚፈልጉትን የመክፈያ አማራጭ ይምረጡ 👇\n\nPlease select the top-up option you wish to use:"


def deposit_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="CBE", callback_data="dep:cbe")],
        [InlineKeyboardButton(text="Telebirr", callback_data="dep:telebirr"),
         InlineKeyboardButton(text="CBE Birr", callback_data="dep:cbebirr")],
    ])


def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ ተመለስ / Back", callback_data="dep:menu")]])


def deposit_steps(target, sender):
    return (
        "<b>መመሪያ</b>\n"
        f"1) ከላይ በተቀመጠው የ {target} ገንዘቡን ያስገቡ።\n"
        f"2) የከፈላችሁበትን (transaction) ደረሰኝ መረጃ የያዘ አጭር የጽሁፍ መልዕክት(SMS) ከ {sender} እስኪደርሳችሁ ትጠብቃላችሁ።\n"
        "3) የደረሳችሁን አጭር የጽሁፍ መልዕክት(SMS) መጀመሪያ ኮፒ(copy) ከዚያም ከታች ባለው የቴሌግራም የጽሁፍ ማስገቢያ ላይ ፔስት(paste) በማድረግ ይላኩት።\n\n"
        "<b>ማሳሰቢያ</b>\n\n"
        "በክፍያ ወቅት ያጋጠማችሁ ችግር ካለ\n"
        f"{DEPOSIT_SUPPORT}\n"
        "ማውራት ትችላላችሁ።\n"
        "እናመሰግናለን!")


def deposit_details(kind):
    if kind == "cbe":
        return (
            "የ CBE አካውንት\n"
            f"<code>{html.escape(CBE_ACCOUNT)}</code> _{html.escape(CBE_NAME)}\n\n"
            + deposit_steps("CBE አካውንት ቁጥር", "CBE"))
    if kind == "telebirr":
        return (
            "የቴሌብር (Telebirr) ቁጥር\n"
            f"<code>{html.escape(TELEBIRR_PHONE)}</code> _{html.escape(TELEBIRR_NAME)}\n\n"
            + deposit_steps("ቴሌብር ስልክ ቁጥር", "Telebirr"))
    if kind == "cbebirr":
        return (
            "የ CBE Birr ቁጥር\n"
            f"<code>{html.escape(CBEBIRR_PHONE)}</code> _{html.escape(CBEBIRR_NAME)}\n\n"
            + deposit_steps("CBE Birr ስልክ ቁጥር", "CBE Birr"))
    return None


@dp.callback_query(F.data.startswith("dep:"))
async def deposit_buttons(cb: CallbackQuery):
    kind = (cb.data or "").split(":", 1)[1]
    try:
        if kind == "menu":
            await cb.message.edit_text(DEPOSIT_MENU_TEXT, reply_markup=deposit_kb())
        else:
            text = deposit_details(kind)
            if text:
                await cb.message.edit_text(text, parse_mode="HTML",
                                           reply_markup=back_kb())
    except Exception:
        pass
    await cb.answer()


# ---------- first start: Start button -> share phone number ----------
START_TEXT = ("🎯 Rodas Friend Zone Bingo\n\n"
              "እንኳን በደህና መጡ! ለመጀመር «Start» ቁልፍን ይንኩ።\n\n"
              "Welcome! Tap Start to begin.")
PHONE_TEXT = ("📱 ለመቀጠል ስልክ ቁጥርዎን ያጋሩ\n\n"
              "ከታች ያለውን «ስልክ ቁጥር ያጋሩ» ቁልፍ ይንኩ።\n\n"
              "To continue, tap the button below to share your phone number.")
WELCOME_TEXT = ("🎯 Rodas Friend Zone Bingo\n\n"
                "Tap the button below to open the Bingo game.")


def start_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Start", callback_data="start:go")]])


def phone_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 ስልክ ቁጥር ያጋሩ / Share phone number",
                                  request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True)


async def need_phone(m: Message):
    """True (and shows the Start button) if the player has not shared a phone yet."""
    if m.from_user.id in phones:
        return False
    await m.answer(START_TEXT, reply_markup=start_kb())
    return True


@dp.callback_query(F.data == "start:go")
async def start_go(cb: CallbackQuery):
    if cb.from_user.id in phones:
        await cb.message.answer(WELCOME_TEXT, reply_markup=play_kb())
    else:
        await cb.message.answer(PHONE_TEXT, reply_markup=phone_kb())
    await cb.answer()


@dp.message(F.contact)
async def got_contact(m: Message):
    uid = ensure(m.from_user)
    c = m.contact
    if c.user_id != uid:                        # must be the player's OWN number
        await m.answer("እባክዎ የራስዎን ስልክ ቁጥር ለማጋራት ከታች ያለውን ቁልፍ ይጠቀሙ።\n"
                       "Please use the button to share your own number.",
                       reply_markup=phone_kb())
        return
    phones[uid] = "".join(ch for ch in c.phone_number if ch.isdigit())
    await m.answer("✅ ስልክ ቁጥርዎ ተመዝግቧል። እናመሰግናለን!\nPhone number saved. Thank you!",
                   reply_markup=ReplyKeyboardRemove())
    await m.answer(WELCOME_TEXT, reply_markup=play_kb())


@dp.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    is_new = m.from_user.id not in wallets
    uid = ensure(m.from_user)
    arg = command.args or ""
    if is_new and arg.startswith("ref_") and arg[4:].isdigit():
        inviter = int(arg[4:])
        if inviter != uid and inviter in wallets:
            referrer[uid] = inviter
            invited[inviter] = invited.get(inviter, 0) + 1
            try:
                await m.bot.send_message(
                    inviter, f"🎉 {names[uid]} በእርስዎ ሊንክ ተቀላቅሏል!")
            except Exception:
                pass
    if uid in phones:
        await m.answer(WELCOME_TEXT, reply_markup=play_kb())
    else:
        await m.answer(START_TEXT, reply_markup=start_kb())


@dp.message(Command("play"))
async def cmd_play(m: Message):
    ensure(m.from_user)
    if await need_phone(m):
        return
    await m.answer("Tap the button below to play 👇", reply_markup=play_kb())


@dp.message(Command("balance"))
async def cmd_balance(m: Message):
    uid = ensure(m.from_user)
    if await need_phone(m):
        return
    bal = wallets[uid]
    await m.answer(
        "Your wallet's detail currently is:\n\n"
        "<blockquote>"
        f"Name:  {html.escape(names[uid])}\n"
        f"Phone Number:  {phones.get(uid, '-')}\n"
        f"Telegram ID:  {uid}\n"
        f"Withdrawable Balance:  {bal:.2f} ETB\n"
        f"Non-withdrawable Bal:  0.00 ETB\n"
        "----------------------------------------\n"
        f"<b>Total Balance: {bal:.2f} ETB</b>\n"
        "----------------------------------------"
        "</blockquote>",
        parse_mode="HTML")


@dp.message(Command("deposit"))
async def cmd_deposit(m: Message, command: CommandObject):
    uid = ensure(m.from_user)
    if await need_phone(m):
        return
    args = (command.args or "").split(maxsplit=1)
    amount = to_amount(args[0]) if args else None
    if not amount:
        await m.answer(DEPOSIT_MENU_TEXT, reply_markup=deposit_kb())
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


# ---------- withdraw (CBE / Telebirr / CBE Birr) ----------
WD_NAMES = {"cbe": "CBE", "telebirr": "Telebirr", "cbebirr": "CBE Birr"}
WD_MENU_TEXT = ("💸 ገንዘብ ማውጣት\n\n"
                "እባክዎ ገንዘብዎን ለመቀበል የሚፈልጉትን አማራጭ ይምረጡ 👇\n\n"
                "Select how you want to receive your money:")


def wd_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="CBE", callback_data="wd:cbe")],
        [InlineKeyboardButton(text="Telebirr", callback_data="wd:telebirr"),
         InlineKeyboardButton(text="CBE Birr", callback_data="wd:cbebirr")],
    ])


def wd_cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ ሰርዝ / Cancel", callback_data="wd:cancel")]])


def wd_account_prompt(kind):
    if kind == "cbe":
        return ("የ CBE አካውንት ቁጥርዎንና ሙሉ ስምዎን ይላኩ።\n\n"
                "ምሳሌ:\n1000123456789 Abebe Kebede")
    if kind == "telebirr":
        return "የቴሌብር ስልክ ቁጥርዎን ይላኩ።\n\nምሳሌ:\n0912345678"
    return "የ CBE Birr ስልክ ቁጥርዎን ይላኩ።\n\nምሳሌ:\n0912345678"


async def submit_withdraw(m: Message, uid, amount, account, method):
    if amount < MIN_WITHDRAW:
        await m.answer(f"ዝቅተኛው የማውጫ መጠን {MIN_WITHDRAW} ብር ነው።\n"
                       f"Minimum withdraw is {MIN_WITHDRAW} birr.")
        return
    if wallets.get(uid, 0) < amount:
        await m.answer("ቀሪ ሂሳብዎ በቂ አይደለም። / Not enough balance.")
        return
    if not ADMIN_ID:
        await m.answer(f"Withdrawals are handled by support: {SUPPORT}")
        return
    wallets[uid] -= amount                       # hold the money
    log(uid, "withdraw", -amount, f"Withdraw request · {method}")
    req_counter[0] += 1
    rid = req_counter[0]
    pending[rid] = {"type": "withdraw", "uid": uid, "amount": amount}
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Paid", callback_data=f"ok:{rid}"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"no:{rid}")]])
    await m.bot.send_message(
        ADMIN_ID,
        f"🏧 Withdraw request #{rid}\nUser: {names[uid]} ({uid})\n"
        f"Method: {method}\nAmount: {amount} birr\nSend to: {account}",
        reply_markup=kb)
    await m.answer(f"💸 Withdrawal initiated\n\n"
                   f"🧾 ጥያቄ #{rid}\n💰 {amount} ብር\n\n"
                   f"ገንዘቡ እስኪላክልዎ ድረስ ከሂሳብዎ ላይ ተይዟል። ሲጠናቀቅ መልዕክት ይደርስዎታል።")


@dp.message(Command("withdraw"))
async def cmd_withdraw(m: Message, command: CommandObject):
    uid = ensure(m.from_user)
    if await need_phone(m):
        return
    wd_state.pop(uid, None)
    args = (command.args or "").split(maxsplit=1)
    amount = to_amount(args[0]) if args else None
    if not amount or len(args) < 2:
        await m.answer(WD_MENU_TEXT, reply_markup=wd_menu_kb())
        return
    await submit_withdraw(m, uid, amount, args[1], "—")


@dp.callback_query(F.data.startswith("wd:"))
async def withdraw_buttons(cb: CallbackQuery):
    uid = ensure(cb.from_user)
    kind = (cb.data or "").split(":", 1)[1]
    try:
        if kind == "cancel":
            wd_state.pop(uid, None)
            await cb.message.edit_text("❌ ተሰርዟል። / Cancelled")
        elif kind in WD_NAMES:
            wd_state[uid] = {"m": kind, "step": "amount", "t": time.time()}
            await cb.message.edit_text(
                f"💸 {WD_NAMES[kind]} ገንዘብ ማውጣት\n\n"
                f"💰 ቀሪ ሂሳብ: {wallets.get(uid, 0):.2f} ብር\n\n"
                "ማውጣት የሚፈልጉትን መጠን ይላኩ።\n"
                f"⚠️ ዝቅተኛው {MIN_WITHDRAW} ብር ነው።",
                reply_markup=wd_cancel_kb())
    except Exception:
        pass
    await cb.answer()


def wd_active(m: Message):
    s = wd_state.get(m.from_user.id)
    return bool(s) and time.time() - s["t"] < 600


@dp.message(F.text & ~F.text.startswith("/"), wd_active)
async def withdraw_steps(m: Message):
    uid = ensure(m.from_user)
    s = wd_state[uid]
    text = m.text.strip()
    if s["step"] == "amount":
        amount = to_amount(text)
        if not amount:
            await m.answer("እባክዎ የገንዘቡን መጠን በቁጥር ብቻ ይላኩ። (ምሳሌ: 100)")
            return
        if amount < MIN_WITHDRAW:
            await m.answer(f"ዝቅተኛው የማውጫ መጠን {MIN_WITHDRAW} ብር ነው። እባክዎ እንደገና ይላኩ።")
            return
        if amount > wallets.get(uid, 0):
            await m.answer(f"ቀሪ ሂሳብዎ በቂ አይደለም። (ቀሪ: {wallets.get(uid, 0):.2f} ብር)\n"
                           "ያነሰ መጠን ይላኩ።")
            return
        s.update(step="account", amount=amount, t=time.time())
        await m.answer(wd_account_prompt(s["m"]), reply_markup=wd_cancel_kb())
    else:
        wd_state.pop(uid, None)
        await submit_withdraw(m, uid, s["amount"], text[:100], WD_NAMES[s["m"]])


@dp.message(Command("transfer"))
async def cmd_transfer(m: Message, command: CommandObject):
    uid = ensure(m.from_user)
    if await need_phone(m):
        return
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
    if await need_phone(m):
        return
    me = await m.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{uid}"
    text = ("👥 ጓደኞችዎን ይጋብዙ!\n\n"
            "ይህን ሊንክ ለጓደኞችዎና ለቤተሰብዎ ያጋሩ።\n")
    if REF_BONUS_PERCENT:
        text += (f"💵 የጋበዙት ሰው ለመጀመሪያ ጊዜ ገንዘብ ሲያስገባ "
                 f"ከተቀማጩ ገንዘብ {REF_BONUS_PERCENT}% ቦነስ ያገኛሉ!\n")
    text += (f"\n🔗 የእርስዎ ሊንክ:\n{link}\n\n"
             f"👤 የጋበዟቸው ሰዎች: {invited.get(uid, 0)}\n"
             f"💰 ያገኙት ቦነስ: {ref_earn.get(uid, 0)} ብር")
    share = ("https://t.me/share/url?url=" + quote(link) +
             "&text=" + quote("🎯 Rodas Friend Zone Bingo ተጫወቱ!"))
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📤 Share / አጋራ", url=share)]])
    await m.answer(text, reply_markup=kb)


@dp.message(Command("support"))
async def cmd_support(m: Message):
    await m.answer(f"🛟 Need help? Contact {SUPPORT}")


@dp.message(F.text & ~F.text.startswith("/"))
async def sms_deposit(m: Message):
    """Player pastes the payment SMS (CBE / Telebirr / CBE Birr)."""
    uid = ensure(m.from_user)
    if await need_phone(m):
        return
    text = m.text.strip()
    mt = re.search(r"(?:ETB|Birr|BIRR|birr|ብር)\s*([\d,]+(?:\.\d+)?)"
                   r"|([\d,]+(?:\.\d+)?)\s*(?:ETB|Birr|BIRR|birr|ብር)", text)
    amount = None
    if mt:
        try:
            amount = int(float((mt.group(1) or mt.group(2)).replace(",", "")))
        except ValueError:
            amount = None
    if not amount:
        await m.answer("የገንዘቡን መጠን ማግኘት አልቻልንም። እባክዎ የደረሰዎትን SMS ሙሉ ይላኩ ወይም ይህንን ይጠቀሙ:\n"
                       "/deposit <amount> <transaction number>")
        return
    if amount < MIN_DEPOSIT:
        await m.answer(f"Minimum deposit is {MIN_DEPOSIT} birr.")
        return
    key = hashlib.sha256(re.sub(r"\s+", " ", text).encode()).hexdigest()
    if key in used_sms:
        await m.answer("ይህ መልዕክት ቀደም ብሎ ተልኳል። / This message was already sent.")
        return
    if not ADMIN_ID:
        await m.answer(f"Deposits are handled by support: {SUPPORT}")
        return
    used_sms.add(key)
    req_counter[0] += 1
    rid = req_counter[0]
    pending[rid] = {"type": "deposit", "uid": uid, "amount": amount}
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"ok:{rid}"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"no:{rid}")]])
    await m.bot.send_message(
        ADMIN_ID,
        f"💰 Deposit request #{rid}\nUser: {names[uid]} ({uid})\n"
        f"Amount: {amount} birr\n\nSMS:\n{text[:800]}", reply_markup=kb)
    await m.answer(f"✅ ጥያቄዎ #{rid} ተልኳል። ሲጸድቅ መልዕክት ይደርስዎታል።")


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
            inv = referrer.get(uid)
            if REF_BONUS_PERCENT and inv and uid not in ref_paid:
                ref_paid.add(uid)                      # first deposit only
                bonus = amount * REF_BONUS_PERCENT // 100
                if bonus > 0:
                    wallets[inv] = wallets.get(inv, 0) + bonus
                    ref_earn[inv] = ref_earn.get(inv, 0) + bonus
                    log(inv, "bonus", bonus, f"Invite bonus · {names.get(uid, uid)}")
                    try:
                        await cb.bot.send_message(
                            inv, f"🎁 ጋብዘውት የነበረው {names.get(uid, 'ተጫዋች')} ገንዘብ አስገብቷል። "
                                 f"{bonus} ብር ቦነስ አግኝተዋል!")
                    except Exception:
                        pass
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
