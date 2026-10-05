import asyncio, hashlib, hmac, html, json, math, os, random, re, secrets, string, time
from pathlib import Path
from urllib.parse import parse_qsl, quote
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (Message, CallbackQuery, BotCommand, BotCommandScopeChat,
                           InlineKeyboardMarkup, InlineKeyboardButton,
                           ReplyKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardRemove, WebAppInfo)

TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
DATABASE_URL = os.environ["DATABASE_URL"]             # Render Postgres "Internal Database URL"
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))            # your Telegram ID
SMS_SECRET = os.getenv("SMS_SECRET", "")              # secret key for the SMS forwarder
PAY_INFO = os.getenv("PAY_INFO", "Ask support for payment details")
SUPPORT = os.getenv("SUPPORT_USERNAME", "@your_support")
PORT = int(os.getenv("PORT", 8080))

# ---------- PAYMENT DETAILS (set these ONLY in environment variables, never in code) ----------
CBE_ACCOUNT = os.getenv("CBE_ACCOUNT", "")
CBE_NAME = os.getenv("CBE_NAME", "")
TELEBIRR_PHONE = os.getenv("TELEBIRR_PHONE", "")
TELEBIRR_NAME = os.getenv("TELEBIRR_NAME", "")
CBEBIRR_PHONE = os.getenv("CBEBIRR_PHONE", "")
CBEBIRR_NAME = os.getenv("CBEBIRR_NAME", "")
DEPOSIT_SUPPORT = os.getenv("DEPOSIT_SUPPORT", "@Rodasfriendzonesupport")
REF_BONUS_PERCENT = 10     # invite bonus: % of the invitee's FIRST deposit (0 = off)

BETS = [10, 20, 50, 100]   # room prices (birr per cartela)
CALL_EVERY = 4             # seconds between calls
LOBBY_SECONDS = 30         # countdown after the FIRST player picks a cartela
START_BALANCE = 0          # new players start with 0: balance comes only from deposits
CARD_COUNT = 100
MIN_PLAYERS = 2            # a round needs at least 2 players
IDLE_KICK = 60             # seconds without contact before a player is removed
MIN_DEPOSIT = 10
MIN_WITHDRAW = 100
WIN_SCREEN_SECONDS = 10    # how long the winner / loser card stays
AUTH_MAX_AGE = 86400       # Telegram initData older than this (seconds) is rejected

INDEX_FILE = Path(__file__).parent / "web" / "index.html"

rng = random.SystemRandom()      # unpredictable randomness for the called numbers

rooms = {b: {"bet": b, "phase": "lobby", "called": [], "players": {},
             "winner": None, "prize": 0, "round": 0, "deadline": None,
             "seq": [], "secret": "", "hash": "",
             "wcells": [], "wname": "", "finish_at": 0,
             "winners": [], "wlist": [], "share": 0}
         for b in BETS}
wallets, names, seen = {}, {}, {}
joined, usernames, wins, won = {}, {}, {}, {}      # profile data
deposited, wagered = {}, {}                        # turnover rule: total deposited / total played
history, winlog = {}, {}                           # transactions / wins list
games, game_counter = [], [0]                      # finished games (History page)
pending, req_counter = {}, [0]          # deposit / withdraw requests
used_sms = set()                        # SMS already sent + used payment numbers ("tok:...")
referrer, invited = {}, {}              # player -> who invited him / invite count
ref_paid, ref_earn = set(), {}          # first-deposit bonus already paid / total earned
wd_state = {}                           # players in the middle of a withdraw
phones = {}                             # player -> verified phone number
bank_sms, bank_by_h = [], set()         # SMS forwarded from YOUR phone (newest first)
banned = set()                          # blocked players
bc_pending = {}                         # admin's broadcast waiting for confirmation
bg_tasks = set()                        # keeps background tasks alive

dp = Dispatcher()


class BanMiddleware(BaseMiddleware):
    """Blocked players cannot use the bot (the admin is never blocked)."""
    async def __call__(self, handler, event, data):
        u = getattr(event, "from_user", None)
        if u and u.id in banned and u.id != ADMIN_ID:
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer("Your account is blocked.", show_alert=True)
                else:
                    await event.answer(f"🚫 Your account is blocked. Contact {SUPPORT}")
            except Exception:
                pass
            return
        return await handler(event, data)


dp.message.outer_middleware(BanMiddleware())
dp.callback_query.outer_middleware(BanMiddleware())


# ---------- DATABASE (Postgres) ----------
# Memory stays the fast working copy. Every change is also put in a queue and
# written to Postgres in ONE transaction (changes made together are saved
# together). When the bot starts, everything is loaded back from Postgres.
pool = None
lock_conn = None
ready = False
BOT = None         # the Bot object, set in main()
_q = None          # asyncio.Queue, created in main()


def _enqueue(sql, *args):
    _q.put_nowait((sql, args))


def save_user(uid):
    snap = {"bal": wallets.get(uid, 0), "name": names.get(uid, "Player"),
            "username": usernames.get(uid, ""), "joined": joined.get(uid, 0),
            "wins": wins.get(uid, 0), "won": won.get(uid, 0),
            "hist": history.get(uid, []), "winlog": winlog.get(uid, []),
            "phone": phones.get(uid), "ref": referrer.get(uid),
            "inv": invited.get(uid, 0), "earn": ref_earn.get(uid, 0),
            "paid": uid in ref_paid, "ban": uid in banned,
            "dep": deposited.get(uid, 0), "wag": wagered.get(uid, 0)}
    _enqueue("INSERT INTO users(uid, data) VALUES($1, $2::jsonb) "
             "ON CONFLICT (uid) DO UPDATE SET data = EXCLUDED.data",
             uid, json.dumps(snap))


def _save_counter(key, value):
    _enqueue("INSERT INTO kv(k, v) VALUES($1, $2) "
             "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", key, value)


def save_pending(rid):
    """Save a deposit / withdraw request (or delete it if it is finished)."""
    r = pending.get(rid)
    if r:
        _enqueue("INSERT INTO pending(rid, data) VALUES($1, $2::jsonb) "
                 "ON CONFLICT (rid) DO UPDATE SET data = EXCLUDED.data",
                 rid, json.dumps(r))
    else:
        _enqueue("DELETE FROM pending WHERE rid = $1", rid)
    _save_counter("req_counter", req_counter[0])


def save_game(r):
    _enqueue("INSERT INTO games(id, data) VALUES($1, $2::jsonb) "
             "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
             r["id"], json.dumps(r))
    _enqueue("DELETE FROM games WHERE id <= $1", r["id"] - 300)
    _save_counter("game_counter", game_counter[0])


def save_sms(key):
    _enqueue("INSERT INTO used_sms(h) VALUES($1) ON CONFLICT DO NOTHING", key)


def save_bank(rec):
    """Save an SMS that was forwarded from your phone."""
    _enqueue("INSERT INTO bank_sms(h, t, data) VALUES($1, $2, $3::jsonb) "
             "ON CONFLICT (h) DO UPDATE SET data = EXCLUDED.data",
             rec["h"], rec["t"], json.dumps(rec))
    _enqueue("DELETE FROM bank_sms WHERE t < $1", int(time.time()) - 30 * 86400)


def save_room_join(bet, uid, card):
    """Remember a paid cartela, so the stake can be refunded after a restart."""
    _enqueue("INSERT INTO room_players(uid, bet, card) VALUES($1, $2, $3) "
             "ON CONFLICT (uid) DO UPDATE SET bet = EXCLUDED.bet, card = EXCLUDED.card",
             uid, bet, card)


def save_room_leave(uid):
    _enqueue("DELETE FROM room_players WHERE uid = $1", uid)


def save_room_clear(bet):
    _enqueue("DELETE FROM room_players WHERE bet = $1", bet)


async def db_writer():
    while True:
        batch = [await _q.get()]
        while not _q.empty():
            batch.append(_q.get_nowait())
        while True:                                   # never drop a write
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        for sql, args in batch:
                            await conn.execute(sql, *args)
                break
            except Exception as e:
                print("db write failed, retrying:", repr(e))
                await asyncio.sleep(3)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(uid BIGINT PRIMARY KEY, data JSONB NOT NULL);
CREATE TABLE IF NOT EXISTS pending(rid BIGINT PRIMARY KEY, data JSONB NOT NULL);
CREATE TABLE IF NOT EXISTS games(id BIGINT PRIMARY KEY, data JSONB NOT NULL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS used_sms(h TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS room_players(uid BIGINT PRIMARY KEY, bet INT NOT NULL, card INT NOT NULL);
CREATE TABLE IF NOT EXISTS bank_sms(h TEXT PRIMARY KEY, t BIGINT NOT NULL, data JSONB NOT NULL);
"""


async def load_state():
    async with pool.acquire() as c:
        for r in await c.fetch("SELECT uid, data FROM users"):
            uid, d = r["uid"], json.loads(r["data"])
            wallets[uid] = d.get("bal", 0)
            names[uid] = d.get("name", "Player")
            usernames[uid] = d.get("username", "")
            joined[uid] = d.get("joined") or int(time.time())
            wins[uid] = d.get("wins", 0)
            won[uid] = d.get("won", 0)
            history[uid] = d.get("hist", [])
            winlog[uid] = d.get("winlog", [])
            deposited[uid] = d.get("dep", 0)
            wagered[uid] = d.get("wag", 0)
            if d.get("phone"):
                phones[uid] = d["phone"]
            if d.get("ref"):
                referrer[uid] = d["ref"]
            invited[uid] = d.get("inv", 0)
            ref_earn[uid] = d.get("earn", 0)
            if d.get("paid"):
                ref_paid.add(uid)
            if d.get("ban"):
                banned.add(uid)
        for r in await c.fetch("SELECT rid, data FROM pending"):
            pending[r["rid"]] = json.loads(r["data"])
        for r in await c.fetch("SELECT id, data FROM games ORDER BY id DESC LIMIT 300"):
            games.append(json.loads(r["data"]))
        for r in await c.fetch("SELECT h FROM used_sms"):
            used_sms.add(r["h"])
        for r in await c.fetch("SELECT data FROM bank_sms ORDER BY t DESC LIMIT 500"):
            rec = json.loads(r["data"])
            bank_sms.append(rec)
            bank_by_h.add(rec["h"])
        for r in await c.fetch("SELECT k, v FROM kv"):
            if r["k"] == "req_counter":
                req_counter[0] = r["v"]
            elif r["k"] == "game_counter":
                game_counter[0] = r["v"]
        # players who had paid for a cartela when the bot stopped: give it back
        stuck = await c.fetch("SELECT uid, bet FROM room_players")
    for r in stuck:
        refund_stake(r["uid"], r["bet"], f"Room {r['bet']} refund (bot restarted)")
    if stuck:
        _enqueue("DELETE FROM room_players")
    print(f"loaded {len(wallets)} users, {len(pending)} pending, "
          f"{len(games)} games, refunded {len(stuck)} cartelas")


@web.middleware
async def wait_ready(request, handler):
    if not ready and request.path.startswith("/api/"):
        return web.json_response({"error": "starting"}, status=503)
    return await handler(request)


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
    # reject old / replayed initData
    try:
        auth_date = int(data.get("auth_date", 0))
    except ValueError:
        return None
    if time.time() - auth_date > AUTH_MAX_AGE:
        return None
    try:
        return json.loads(data["user"])
    except ValueError:
        return None


def log(uid, kind, amount, note=""):
    """Save a transaction for the profile page (keeps the last 50)."""
    h = history.setdefault(uid, [])
    h.insert(0, {"t": int(time.time()), "k": kind, "a": amount, "n": note})
    del h[50:]
    save_user(uid)                 # wallet + history saved to the database


def refund_stake(uid, bet, note):
    """Give a cartela price back and undo its turnover."""
    wallets[uid] = wallets.get(uid, 0) + bet
    wagered[uid] = max(0, wagered.get(uid, 0) - bet)
    log(uid, "refund", bet, note)


def turnover_left(uid):
    """Birr the player must still play before he can withdraw (deposit must be played once)."""
    return max(0, deposited.get(uid, 0) - wagered.get(uid, 0))


def touch(user):
    uid = user["id"]
    new = uid not in wallets
    wallets.setdefault(uid, START_BALANCE)
    joined.setdefault(uid, int(time.time()))
    nm = user.get("first_name", "Player")
    un = user.get("username", "") or ""
    changed = new or names.get(uid) != nm or usernames.get(uid) != un
    names[uid] = nm
    usernames[uid] = un
    seen[uid] = time.time()
    if changed:
        save_user(uid)
    return uid


def make_card(no: int):
    r = random.Random(no)          # must stay seeded: a cartela number = the same card always
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
        refund_stake(uid, g["bet"], f"Room {g['bet']} refund")
    g["players"].pop(uid, None)
    save_room_leave(uid)
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
    save_game(games[0])


def summ(r, uid):
    return {"id": r["id"], "bet": r["bet"], "t": r["t"],
            "winner": r["winner"], "board": r["board"], "ref": r["ref"],
            "calls": r["calls"], "prize": r["prize"],
            "won": uid in r["wids"], "mine": uid in r["pl"]}


# ---------- automatic deposit checking (SMS forwarded from YOUR phone) ----------
AMOUNT_RE = re.compile(r"(?:ETB|Birr|BIRR|birr|ብር)\s*([\d,]+(?:\.\d+)?)"
                       r"|([\d,]+(?:\.\d+)?)\s*(?:ETB|Birr|BIRR|birr|ብር)")
CREDIT_WORDS = ("credited", "received", "ተቀብለዋል")
DEBIT_WORDS = ("debited", "transferred", "you have sent", "sent to", "paid to")


def parse_amount(text):
    mt = AMOUNT_RE.search(text)
    if not mt:
        return None
    try:
        return int(float((mt.group(1) or mt.group(2)).replace(",", "")))
    except ValueError:
        return None


def sms_tokens(text):
    """Payment / reference numbers in an SMS: words that mix letters and digits."""
    out = set()
    for t in re.findall(r"[A-Za-z0-9]{8,24}", text):
        t = t.upper()
        if re.search(r"\d", t) and re.search(r"[A-Z]", t):
            out.add(t)
    return out


def is_credit(text):
    """True only for 'money came IN' messages (never for money you sent out)."""
    low = text.lower()
    if any(w in low for w in DEBIT_WORDS):
        return False
    return any(w in low for w in CREDIT_WORDS)


def mark_token(t):
    key = "tok:" + t
    if key not in used_sms:
        used_sms.add(key)
        save_sms(key)


def claim_bank(rec):
    rec["claimed"] = True
    save_bank(rec)
    for t in rec["tokens"]:
        mark_token(t)


def find_bank_sms(amount, tokens):
    if not tokens:
        return None
    for r in bank_sms:                                  # newest first
        if (r["credit"] and not r["claimed"] and r["amount"] == amount
                and tokens & set(r["tokens"])):
            return r
    return None


async def approve_deposit(bot, uid, amount, tokens=()):
    """Add the money to the player, pay the invite bonus, tell the player."""
    wallets[uid] = wallets.get(uid, 0) + amount
    deposited[uid] = deposited.get(uid, 0) + amount        # turnover rule counts real deposits
    log(uid, "deposit", amount, "Deposit approved")
    for t in tokens:
        mark_token(t)
    inv = referrer.get(uid)
    if REF_BONUS_PERCENT and inv and uid not in ref_paid:
        ref_paid.add(uid)                              # first deposit only
        save_user(uid)
        bonus = amount * REF_BONUS_PERCENT // 100
        if bonus > 0:
            wallets[inv] = wallets.get(inv, 0) + bonus
            ref_earn[inv] = ref_earn.get(inv, 0) + bonus
            deposited[inv] = deposited.get(inv, 0) + bonus   # bonus must be played before withdrawing
            log(inv, "bonus", bonus, f"Invite bonus · {names.get(uid, uid)}")
            try:
                await bot.send_message(
                    inv, f"🎁 ጋብዘውት የነበረው {names.get(uid, 'ተጫዋች')} ገንዘብ አስገብቷል። "
                         f"{bonus} ብር ቦነስ አግኝተዋል!")
            except Exception:
                pass
    try:
        await bot.send_message(uid, f"✅ Your deposit of {amount} birr was approved.")
    except Exception:
        pass


async def on_bank_sms(rec):
    """A new SMS came from your phone: check if a player is already waiting for it."""
    if not rec["credit"] or rec["claimed"] or not rec["tokens"]:
        return
    for rid, req in list(pending.items()):
        if req["type"] != "deposit" or req["amount"] != rec["amount"]:
            continue
        if not set(req.get("tokens", [])) & set(rec["tokens"]):
            continue
        pending.pop(rid, None)
        save_pending(rid)
        claim_bank(rec)
        await approve_deposit(BOT, req["uid"], rec["amount"])
        if ADMIN_ID:
            try:
                await BOT.send_message(
                    ADMIN_ID, f"✅ Deposit #{rid} checked automatically.\n"
                              f"User: {names.get(req['uid'], '')} ({req['uid']})\n"
                              f"Amount: {rec['amount']} birr")
            except Exception:
                pass
        return


async def api_sms_hook(req):
    """Your phone's SMS-forwarder app sends every SMS here.
    The secret can be sent in the header  X-Key  (recommended, not saved in logs)
    or in the URL as ?key=...  (works, but URLs may appear in server logs)."""
    given = req.headers.get("X-Key", "") or req.query.get("key", "")
    if not SMS_SECRET or not hmac.compare_digest(given, SMS_SECRET):
        return web.json_response({"ok": False}, status=403)
    data = {}
    try:
        if req.content_type == "application/json":
            data = await req.json()
        elif req.can_read_body:
            if req.content_type in ("application/x-www-form-urlencoded",
                                    "multipart/form-data"):
                data = dict(await req.post())
            else:
                data = {"text": await req.text()}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {"text": str(data)}
    merged = {**dict(req.query), **data}
    text = ""
    for k in ("text", "message", "body", "msg", "sms", "content"):
        if merged.get(k):
            text = str(merged[k])
            break
    text = text.strip()
    if not text:
        return web.json_response({"ok": False, "error": "no text"}, status=400)
    sender = str(merged.get("from") or merged.get("sender") or merged.get("number") or "")
    h = hashlib.sha256(re.sub(r"\s+", " ", text).encode()).hexdigest()
    if h in bank_by_h:                                  # forwarder sent it twice
        return web.json_response({"ok": True, "dup": True})
    amount = parse_amount(text) or 0
    rec = {"h": h, "t": int(time.time()), "amount": amount,
           "tokens": sorted(sms_tokens(text)),
           "credit": bool(amount) and is_credit(text), "claimed": False,
           "text": text[:300], "from": sender[:30]}
    bank_sms.insert(0, rec)
    bank_by_h.add(h)
    del bank_sms[500:]
    save_bank(rec)
    await on_bank_sms(rec)
    return web.json_response({"ok": True})


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
                refund_stake(uid, g["bet"], f"Room {g['bet']} refund (not enough players)")
            g["players"].clear()
            save_room_clear(g["bet"])
            return

    # provably fair: pick all numbers + secret first, publish the hash
    g["seq"] = rng.sample(range(1, 76), 75)
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
    save_room_clear(g["bet"])              # round is over: stakes are settled
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
            # if the round was cut short before anybody was paid, give stakes back
            if g["phase"] in ("lobby", "playing") and not g["winners"]:
                for uid in list(g["players"]):
                    refund_stake(uid, g["bet"], f"Room {g['bet']} refund")
            g["players"].clear()
            save_room_clear(g["bet"])
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
    if uid in banned:
        return web.json_response({"ok": False,
                                  "error": "Your account is blocked. Contact support"})
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
    wagered[uid] = wagered.get(uid, 0) + g["bet"]          # counts toward the turnover rule
    log(uid, "bet", -g["bet"], f"Room {g['bet']} · cartela {card}")
    g["players"][uid] = card
    save_room_join(g["bet"], uid, card)
    if g["deadline"] is None:                  # first player starts the clock
        g["deadline"] = time.time() + LOBBY_SECONDS
    return web.json_response({"ok": True})


async def api_bingo(req):
    body = await req.json()
    user = verify(body.get("initData", ""))
    if not user:
        return web.json_response({"ok": False})
    uid = user["id"]
    if uid in banned:
        return web.json_response({"ok": False})
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
        save_room_clear(g["bet"])              # prize paid: stakes are settled
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
    new = u.id not in wallets
    wallets.setdefault(u.id, START_BALANCE)
    joined.setdefault(u.id, int(time.time()))
    nm = u.first_name or "Player"
    un = u.username or ""
    changed = new or names.get(u.id) != nm or usernames.get(u.id) != un
    names[u.id] = nm
    usernames[u.id] = un
    if changed:
        save_user(u.id)
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
    not_set = f"Payment details are not available right now. Contact {DEPOSIT_SUPPORT}"
    if kind == "cbe":
        if not CBE_ACCOUNT:
            return html.escape(not_set)
        return (
            "የ CBE አካውንት\n"
            f"<code>{html.escape(CBE_ACCOUNT)}</code> _{html.escape(CBE_NAME)}\n\n"
            + deposit_steps("CBE አካውንት ቁጥር", "CBE"))
    if kind == "telebirr":
        if not TELEBIRR_PHONE:
            return html.escape(not_set)
        return (
            "የቴሌብር (Telebirr) ቁጥር\n"
            f"<code>{html.escape(TELEBIRR_PHONE)}</code> _{html.escape(TELEBIRR_NAME)}\n\n"
            + deposit_steps("ቴሌብር ስልክ ቁጥር", "Telebirr"))
    if kind == "cbebirr":
        if not CBEBIRR_PHONE:
            return html.escape(not_set)
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
    save_user(uid)
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
            save_user(uid)
            save_user(inviter)
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
    left = turnover_left(uid)
    extra = (f"\n🎯 Play {left} more birr before you can withdraw." if left else "")
    await m.answer(
        "Your wallet's detail currently is:\n\n"
        "<blockquote>"
        f"Name:  {html.escape(names[uid])}\n"
        f"Phone Number:  {phones.get(uid, '-')}\n"
        f"Withdrawable Balance:  {bal:.2f} ETB\n"
        f"Non-withdrawable Bal:  0.00 ETB\n"
        "----------------------------------------\n"
        f"<b>Total Balance: {bal:.2f} ETB</b>\n"
        "----------------------------------------"
        "</blockquote>" + extra,
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
    save_pending(rid)
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


def turnover_msg(left):
    return (f"⚠️ ገንዘብ ከማውጣትዎ በፊት ያስገቡትን ገንዘብ መጫወት አለብዎት። "
            f"ቀሪ: {left} ብር\n"
            f"You must play {left} more birr before you can withdraw.")


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
    left = turnover_left(uid)
    if left > 0:
        await m.answer(turnover_msg(left))
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
    save_pending(rid)                            # saved in the same transaction as the hold
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
    left = turnover_left(uid)
    if left > 0:
        await m.answer(turnover_msg(left))
        return
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
            left = turnover_left(uid)
            if left > 0:
                await cb.message.edit_text(turnover_msg(left))
                await cb.answer()
                return
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


def find_by_phone(text):
    """Find a player by phone number. 0912345678 and +251912345678 both work."""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) < 9:
        return None
    last9 = digits[-9:]
    for uid, ph in phones.items():
        if ph.endswith(last9):
            return uid
    return None


@dp.message(Command("transfer"))
async def cmd_transfer(m: Message, command: CommandObject):
    uid = ensure(m.from_user)
    if await need_phone(m):
        return
    args = (command.args or "").split()
    if len(args) != 2 or not to_amount(args[1]):
        await m.answer(
            "🔁 Transfer\n\n"
            "Send:\n/transfer <phone number> <amount>\n\n"
            "Example:\n/transfer 0912345678 50")
        return
    to, amount = find_by_phone(args[0]), int(args[1])
    if not to:
        await m.answer("No player found with that phone number.\n"
                       "They must start the bot and share their number first.")
    elif to == uid:
        await m.answer("You can't transfer to yourself.")
    elif wallets[uid] < amount:
        await m.answer("Not enough balance.")
    else:
        wallets[uid] -= amount
        wallets[to] += amount
        # the receiver must play transferred money before withdrawing it
        # (stops "deposit -> transfer to 2nd account -> withdraw" tricks)
        deposited[to] = deposited.get(to, 0) + amount
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
        "The first player to press BINGO WIN with a real pattern wins the prize.\n\n"
        "ℹ️ A round needs at least 2 players. If nobody joins you, your stake is refunded.\n"
        "ℹ️ To withdraw, you must first play the amount you deposited.")


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


# ---------- ADMIN COMMANDS (only your ADMIN_ID account can use these) ----------
def is_admin(m: Message):
    return bool(ADMIN_ID) and m.from_user.id == ADMIN_ID


@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m):
        return
    await m.answer(
        "🛠 Admin commands\n\n"
        "/stats\nPlayers, money, waiting requests, last 24h\n\n"
        "/addbalance <phone> <amount>\nAdd money to a player. Use a minus to remove: -50\n\n"
        "/ban <phone>\nBlock a player\n\n"
        "/unban <phone>\nUnblock a player\n\n"
        "/broadcast <message>\nSend a message to all players (you confirm first)\n\n"
        "/lastsms\nLast SMS forwarded from your phone")


@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    if not is_admin(m):
        return
    now = time.time()
    in_rooms = sum(len(g["players"]) for g in rooms.values())
    deps = [r for r in pending.values() if r["type"] == "deposit"]
    wds = [r for r in pending.values() if r["type"] == "withdraw"]
    recent = [r for r in games if r["t"] >= now - 86400]
    stake = sum(r["bet"] * len(r["pl"]) for r in recent)
    paid = sum(r["prize"] * len(r["wids"]) for r in recent)
    await m.answer(
        "📊 <b>Stats</b>\n\n"
        f"Players: {len(wallets)} ({len(phones)} with phone)\n"
        f"Blocked: {len(banned)}\n"
        f"Playing now: {in_rooms}\n"
        f"Money in player wallets: {sum(wallets.values()):,} ETB\n\n"
        f"Waiting deposits: {len(deps)} ({sum(r['amount'] for r in deps):,} ETB)\n"
        f"Waiting withdrawals: {len(wds)} ({sum(r['amount'] for r in wds):,} ETB)\n\n"
        f"<b>Last 24 hours</b> (from the last 300 games)\n"
        f"Games: {len(recent)}\n"
        f"Stakes played: {stake:,} ETB\n"
        f"Paid to winners: {paid:,} ETB\n"
        f"House earned (approx): {stake - paid:,} ETB",
        parse_mode="HTML")


@dp.message(Command("addbalance"))
async def cmd_addbalance(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    args = (command.args or "").split()
    if len(args) != 2 or not re.fullmatch(r"-?\d+", args[1]) or int(args[1]) == 0:
        await m.answer("Send:\n/addbalance <phone> <amount>\n\n"
                       "Add 100 birr:\n/addbalance 0912345678 100\n"
                       "Remove 50 birr:\n/addbalance 0912345678 -50")
        return
    uid, amount = find_by_phone(args[0]), int(args[1])
    if not uid:
        await m.answer("No player found with that phone number.")
        return
    if wallets.get(uid, 0) + amount < 0:
        await m.answer(f"{names.get(uid, 'Player')} only has {wallets.get(uid, 0)} birr.")
        return
    wallets[uid] = wallets.get(uid, 0) + amount
    if amount > 0:
        deposited[uid] = deposited.get(uid, 0) + amount     # admin-added money must be played too
        log(uid, "deposit", amount, "Added by admin")
        note = f"💰 {amount} birr was added to your balance."
    else:
        log(uid, "withdraw", amount, "Removed by admin")
        note = f"💸 {-amount} birr was removed from your balance."
    try:
        await m.bot.send_message(uid, note)
    except Exception:
        pass
    await m.answer(f"✅ Done.\nPlayer: {names.get(uid, 'Player')} ({phones.get(uid)})\n"
                   f"Change: {amount:+d} birr\nNew balance: {wallets[uid]} birr")


@dp.message(Command("ban"))
async def cmd_ban(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    uid = find_by_phone((command.args or "").strip())
    if not uid:
        await m.answer("Send:\n/ban <phone>\n\nExample:\n/ban 0912345678\n\n"
                       "No player found with that phone number.")
        return
    if uid == ADMIN_ID:
        await m.answer("You can't block yourself.")
        return
    banned.add(uid)
    save_user(uid)
    await m.answer(f"🚫 {names.get(uid, 'Player')} ({phones.get(uid)}) is blocked.\n"
                   "They can no longer use the bot or join games.")


@dp.message(Command("unban"))
async def cmd_unban(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    uid = find_by_phone((command.args or "").strip())
    if not uid:
        await m.answer("Send:\n/unban <phone>\n\nNo player found with that phone number.")
        return
    banned.discard(uid)
    save_user(uid)
    await m.answer(f"✅ {names.get(uid, 'Player')} ({phones.get(uid)}) is unblocked.")


@dp.message(Command("broadcast"))
async def cmd_broadcast(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    text = (command.args or "").strip()
    if not text:
        await m.answer("Send:\n/broadcast <your message>")
        return
    bc_pending[m.from_user.id] = text
    n = len([u for u in wallets if u not in banned])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Send to {n} players", callback_data="bc:send"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="bc:cancel")]])
    await m.answer(f"📢 This is what players will see:\n\n{text}", reply_markup=kb)


async def run_broadcast(bot, text, admin_id):
    ok = fail = 0
    for uid in list(wallets):
        if uid in banned:
            continue
        try:
            await bot.send_message(uid, text)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.06)              # stay under Telegram's speed limit
    try:
        await bot.send_message(admin_id, f"📢 Broadcast finished.\n✅ Sent: {ok}\n❌ Failed: {fail}")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("bc:"))
async def broadcast_buttons(cb: CallbackQuery):
    if not ADMIN_ID or cb.from_user.id != ADMIN_ID:
        await cb.answer("Not allowed", show_alert=True)
        return
    action = (cb.data or "").split(":", 1)[1]
    text = bc_pending.pop(cb.from_user.id, None)
    if action != "send" or not text:
        try:
            await cb.message.edit_text("❌ Cancelled." if action != "send"
                                       else "Nothing to send.")
        except Exception:
            pass
        await cb.answer()
        return
    try:
        await cb.message.edit_text("📤 Sending…")
    except Exception:
        pass
    await cb.answer()
    task = asyncio.create_task(run_broadcast(cb.bot, text, cb.from_user.id))
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)


@dp.message(Command("lastsms"))
async def cmd_lastsms(m: Message):
    """Admin only: shows the last SMS your phone forwarded, and how the bot read them."""
    if not is_admin(m):
        return
    if not bank_sms:
        await m.answer("No SMS received from your phone yet.")
        return
    parts = []
    for r in bank_sms[:5]:
        kind = "✅ money IN" if r["credit"] else "➖ not a deposit"
        used = "used" if r["claimed"] else "not used"
        parts.append(f"{kind} · {r['amount']} birr · {used}\n"
                     f"{html.escape(r['text'][:150])}\n"
                     f"Numbers found: {html.escape(', '.join(r['tokens']) or '-')}")
    await m.answer("\n\n".join(parts), parse_mode="HTML")


@dp.message(F.text & ~F.text.startswith("/"))
async def sms_deposit(m: Message):
    """Player pastes the payment SMS (CBE / Telebirr / CBE Birr)."""
    uid = ensure(m.from_user)
    if await need_phone(m):
        return
    text = m.text.strip()
    amount = parse_amount(text)
    if not amount:
        await m.answer("የገንዘቡን መጠን ማግኘት አልቻልንም። እባክዎ የደረሰዎትን SMS ሙሉ ይላኩ ወይም ይህንን ይጠቀሙ:\n"
                       "/deposit <amount> <transaction number>")
        return
    if amount < MIN_DEPOSIT:
        await m.answer(f"Minimum deposit is {MIN_DEPOSIT} birr.")
        return
    tokens = sms_tokens(text)
    if any(("tok:" + t) in used_sms for t in tokens):
        await m.answer("ይህ ክፍያ ቀደም ብሎ ተመዝግቧል። / This payment was already credited.")
        return
    key = hashlib.sha256(re.sub(r"\s+", " ", text).encode()).hexdigest()
    if key in used_sms:
        await m.answer("ይህ መልዕክት ቀደም ብሎ ተልኳል። / This message was already sent.")
        return

    # 1) Automatic: the same payment SMS already arrived from YOUR phone
    rec = find_bank_sms(amount, tokens)
    if rec:
        used_sms.add(key)
        save_sms(key)
        claim_bank(rec)
        await approve_deposit(m.bot, uid, rec["amount"])
        if ADMIN_ID:
            try:
                await m.bot.send_message(
                    ADMIN_ID, f"✅ Deposit checked automatically.\n"
                              f"User: {names[uid]} ({uid})\nAmount: {rec['amount']} birr")
            except Exception:
                pass
        return

    # 2) Not found (yet): send to the admin. If your phone's SMS arrives
    #    later, it is approved automatically.
    if not ADMIN_ID:
        await m.answer(f"Deposits are handled by support: {SUPPORT}")
        return
    used_sms.add(key)
    save_sms(key)
    req_counter[0] += 1
    rid = req_counter[0]
    pending[rid] = {"type": "deposit", "uid": uid, "amount": amount,
                    "tokens": sorted(tokens)}
    save_pending(rid)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"ok:{rid}"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"no:{rid}")]])
    await m.bot.send_message(
        ADMIN_ID,
        f"💰 Deposit request #{rid}\nUser: {names[uid]} ({uid})\n"
        f"Amount: {amount} birr\n"
        f"⚠️ Not found in your phone's SMS yet. Check your account before approving.\n\n"
        f"SMS:\n{text[:800]}", reply_markup=kb)
    await m.answer(f"🔎 ጥያቄዎ #{rid} እየተረጋገጠ ነው። ሲጸድቅ መልዕክት ይደርስዎታል።\n"
                   f"Your payment #{rid} is being checked. You will get a message soon.")


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
    save_pending(int(rid))                     # request is finished: delete it
    uid, amount = req["uid"], req["amount"]
    approved = action == "ok"
    note = None
    if req["type"] == "deposit":
        if approved:
            await approve_deposit(cb.bot, uid, amount, req.get("tokens", []))
        else:
            note = f"❌ Your deposit of {amount} birr was rejected. Contact {SUPPORT}."
    else:
        if approved:
            note = f"✅ Your withdrawal of {amount} birr was paid."
        else:
            wallets[uid] = wallets.get(uid, 0) + amount        # give it back
            log(uid, "refund", amount, "Withdraw rejected")
            note = f"❌ Your withdrawal was rejected. {amount} birr returned to your balance."
    if note:
        try:
            await cb.bot.send_message(uid, note)
        except Exception:
            pass
    await cb.message.edit_text(
        cb.message.text + ("\n\n✅ DONE" if approved else "\n\n❌ REJECTED"))
    await cb.answer("Done")


# ---------- main ----------
async def main():
    global pool, lock_conn, ready, _q, BOT
    bot = Bot(TOKEN)
    BOT = bot

    app = web.Application(middlewares=[wait_ready])
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
        web.post("/api/sms-hook", api_sms_hook),
        web.get("/api/sms-hook", api_sms_hook),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()   # port open: Render sees us as live

    # database: wait until an older copy of the bot (during a redeploy) has stopped,
    # so two copies never write to the database at the same time
    lock_conn = await asyncpg.connect(DATABASE_URL)
    await lock_conn.execute("SELECT pg_advisory_lock(727001)")
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
    _q = asyncio.Queue()
    asyncio.create_task(db_writer())
    await load_state()
    ready = True

    asyncio.create_task(game_loop())
    asyncio.create_task(reaper())

    await bot.delete_webhook(drop_pending_updates=True)
    public_commands = [
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
    await bot.set_my_commands(public_commands)
    if ADMIN_ID:                                  # extra menu only you can see
        try:
            await bot.set_my_commands(
                public_commands + [
                    BotCommand(command="admin", description="Admin commands"),
                    BotCommand(command="stats", description="Stats"),
                    BotCommand(command="addbalance", description="Add / remove balance"),
                    BotCommand(command="ban", description="Block a player"),
                    BotCommand(command="unban", description="Unblock a player"),
                    BotCommand(command="broadcast", description="Message all players"),
                    BotCommand(command="lastsms", description="Last forwarded SMS"),
                ],
                scope=BotCommandScopeChat(chat_id=ADMIN_ID))
        except Exception as e:
            print("could not set admin menu:", repr(e))
    await dp.start_polling(bot)


asyncio.run(main())
