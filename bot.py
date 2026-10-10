import asyncio, hashlib, hmac, html, io, json, math, os, random, re, secrets, string, time, zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote
import asyncpg
import edge_tts
from aiohttp import web
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (Message, CallbackQuery, BotCommand, BotCommandScopeChat,
                           BotCommandScopeDefault, BotCommandScopeAllPrivateChats,
                           InlineKeyboardMarkup, InlineKeyboardButton,
                           ReplyKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardRemove, WebAppInfo, FSInputFile, BufferedInputFile)

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
REG_BONUS = 10               # play-only bonus (birr) given once when a new player registers. 0 = no registration bonus.
REG_BONUS_TEXT = ("🎁 እንኳን ወደ Rodas Friend Zone Bingo በደህና መጡ! {amount} ብር ቦነስ ወደ ሂሳብዎ ተጨምሯል።\n"
                  "መልካም ዕድል 🍀!\n\n"
                  "🎁 Welcome to Rodas friend zone bingo! {amount} ETB bonus added to your wallet. "
                  "Good luck 🍀!")
BONUS_ADD_NOTIFY = True      # True = player gets a message when bonus is ADDED (/addbonus, /bonusmany). False = silent.
BONUS_REMOVE_NOTIFY = False  # False = player gets NO message when bonus is REMOVED. True = send a message.
REF_BONUS_PERCENT = 10     # invite bonus: % of the invitee's FIRST deposit (0 = off). Play-only money.

BRAND = "© 2026 Rodas Friend Zone Bingo"     # footer / branding line (change the text here)

# shown in the empty chat before a player presses Start (max 512 characters)
BOT_DESCRIPTION = (
    "🎯 Rodas Friend Zone Bingo\n"
    "🎉 Fair & exciting bingo games\n"
    "💰 Clear prizes & rules\n"
    "🔒 Secure & simple\n"
    "🤝 Support available\n"
    "🔞 18+ Play responsibly")
# the "Bio" on the bot's profile page (max 120 characters)
BOT_SHORT = "© 2026 Rodas Friend Zone Bingo • Play with Friends 🤝"

# ---------- WAGERING (turnover) RULE ----------
FIRST_DEPOSIT_TURNOVER = 1.0   # a player's FIRST deposit must be played 1x before withdrawing
NEXT_DEPOSIT_TURNOVER = 0.5    # every deposit after the first must be played 0.5x

BETS = [10, 20, 50, 100]   # room prices (birr per cartela)
CALL_EVERY = 4             # seconds between calls
CLAIM_GRACE = 2.0           # after the first claim, other manual players have this many seconds to press BINGO too
LOBBY_SECONDS = 30         # countdown after the FIRST player picks a cartela
START_BALANCE = 0          # new players start with 0: balance comes only from deposits
CARD_COUNT = 100
MIN_PLAYERS = 5            # a round needs at least 5 players, otherwise stakes are refunded
IDLE_KICK = 60             # seconds without contact: removed from the LOBBY (refunded);
                           # during a round the player stays and a win is claimed for them
MIN_DEPOSIT = 50           # smallest deposit (50 is allowed, below 50 is not)
MIN_WITHDRAW = 100
MAX_PENDING_DEPOSITS = 3   # most deposit requests one player can have waiting for the admin
WIN_SCREEN_SECONDS = 10    # how long the winner / loser card stays
AUTH_MAX_AGE = 86400       # Telegram initData older than this (seconds) is rejected

INDEX_FILE = Path(__file__).parent / "web" / "index.html"
PATTERNS_IMG = Path(__file__).parent / "patterns.png"     # winning patterns picture

rng = random.SystemRandom()      # unpredictable randomness for the called numbers

rooms = {b: {"bet": b, "phase": "lobby", "called": [], "players": {},
             "winner": None, "prize": 0, "round": 0, "deadline": None,
             "seq": [], "secret": "", "hash": "",
             "wcells": [], "wname": "", "finish_at": 0,
             "winners": [], "wlist": [], "share": 0, "dq": set(), "left": set(), "claims": set(), "claim_t": 0}
         for b in BETS}
wallets, names, seen = {}, {}, {}                  # wallets = real, withdrawable-type money
bonus = {}                                         # PLAY-ONLY bonus money (never withdrawn / transferred)
joined, usernames, wins, won = {}, {}, {}, {}      # profile data
deposited, wagered = {}, {}                        # total money added / total played
required = {}                                      # total turnover the player has to play (1x first deposit, 0.5x later)
dep_count = {}                                     # how many real deposits were approved (0 = next one is the FIRST)
stake_used = {}                                    # uid -> how much of the CURRENT cartela counted as turnover
stake_bonus = {}                                   # uid -> how much of the CURRENT cartela was paid with bonus
history, winlog = {}, {}                           # transactions / wins list
games, game_counter = [], [0]                      # finished games (History page)
pending, req_counter = {}, [0]          # deposit / withdraw requests
used_sms = set()                        # SMS already sent + used payment numbers ("tok:...")
referrer, invited = {}, {}              # player -> who invited him / invite count
ref_paid, ref_earn = set(), {}          # first-deposit bonus already paid / total earned
ref_phones = set()                      # phone numbers (last 9 digits) that already gave an invite bonus
wd_state = {}                           # players in the middle of a withdraw
phones = {}                             # player -> verified phone number
bank_sms, bank_by_h = [], set()         # SMS forwarded from YOUR phone (newest first)
banned = set()                          # blocked players
bc_pending = {}                         # admin's broadcast waiting for confirmation
rf_pending = {}                         # admin's /resetfree list waiting for confirmation
bg_tasks = set()                        # keeps background tasks alive
auto_pref = {}                          # uid -> True if the player has "Auto" switched ON in the web app

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
    snap = {"bal": wallets.get(uid, 0), "bonus": bonus.get(uid, 0),
            "name": names.get(uid, "Player"),
            "username": usernames.get(uid, ""), "joined": joined.get(uid, 0),
            "wins": wins.get(uid, 0), "won": won.get(uid, 0),
            "hist": history.get(uid, []), "winlog": winlog.get(uid, []),
            "phone": phones.get(uid), "ref": referrer.get(uid),
            "inv": invited.get(uid, 0), "earn": ref_earn.get(uid, 0),
            "paid": uid in ref_paid, "ban": uid in banned,
            "dep": deposited.get(uid, 0), "wag": wagered.get(uid, 0),
            "req": required.get(uid, 0), "dc": dep_count.get(uid, 0)}
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


def free_sms(key):
    """Make an SMS usable again (used when the admin rejects a deposit,
    so the player can send the same SMS again)."""
    used_sms.discard(key)
    _enqueue("DELETE FROM used_sms WHERE h = $1", key)


def save_bank(rec):
    """Save an SMS that was forwarded from your phone."""
    _enqueue("INSERT INTO bank_sms(h, t, data) VALUES($1, $2, $3::jsonb) "
             "ON CONFLICT (h) DO UPDATE SET data = EXCLUDED.data",
             rec["h"], rec["t"], json.dumps(rec))
    _enqueue("DELETE FROM bank_sms WHERE t < $1", int(time.time()) - 30 * 86400)


def save_room_join(bet, uid, card, used, bonus_used=0):
    """Remember a paid cartela (how much of it counted as turnover and how much
    was paid with bonus), so the stake can be refunded correctly after a restart."""
    _enqueue("INSERT INTO room_players(uid, bet, card, used, bonus_used) "
             "VALUES($1, $2, $3, $4, $5) "
             "ON CONFLICT (uid) DO UPDATE SET bet = EXCLUDED.bet, card = EXCLUDED.card, "
             "used = EXCLUDED.used, bonus_used = EXCLUDED.bonus_used",
             uid, bet, card, used, bonus_used)


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
CREATE TABLE IF NOT EXISTS room_players(uid BIGINT PRIMARY KEY, bet INT NOT NULL, card INT NOT NULL, used INT NOT NULL DEFAULT -1);
ALTER TABLE room_players ADD COLUMN IF NOT EXISTS used INT NOT NULL DEFAULT -1;
ALTER TABLE room_players ADD COLUMN IF NOT EXISTS bonus_used INT NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS bank_sms(h TEXT PRIMARY KEY, t BIGINT NOT NULL, data JSONB NOT NULL);
"""


async def load_state():
    async with pool.acquire() as c:
        for r in await c.fetch("SELECT uid, data FROM users"):
            uid, d = r["uid"], json.loads(r["data"])
            wallets[uid] = d.get("bal", 0)
            bonus[uid] = d.get("bonus", 0)
            names[uid] = d.get("name", "Player")
            usernames[uid] = d.get("username", "")
            joined[uid] = d.get("joined") or int(time.time())
            wins[uid] = d.get("wins", 0)
            won[uid] = d.get("won", 0)
            history[uid] = d.get("hist", [])
            winlog[uid] = d.get("winlog", [])
            deposited[uid] = d.get("dep", 0)
            # players saved before this update owe what they owed before (= deposited)
            required[uid] = d.get("req", deposited[uid])
            # players who already deposited before count as "not first deposit" from now on
            dep_count[uid] = d.get("dc", 1 if deposited[uid] > 0 else 0)
            # extra play above what is required must not carry over to future deposits
            wagered[uid] = min(d.get("wag", 0), required[uid])
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
            elif r["k"].startswith("refphone:"):
                ref_phones.add(r["k"][9:])
        # players who had paid for a cartela when the bot stopped: give it back
        stuck = await c.fetch("SELECT uid, bet, used, bonus_used FROM room_players")
    for r in stuck:
        used = r["used"] if r["used"] >= 0 else r["bet"]
        refund_stake(r["uid"], r["bet"], f"Room {r['bet']} refund (bot restarted)",
                     used, r["bonus_used"])
    if stuck:
        _enqueue("DELETE FROM room_players")
    print(f"loaded {len(wallets)} users, {len(pending)} pending, "
          f"{len(games)} games, refunded {len(stuck)} cartelas")


@web.middleware
async def wait_ready(request, handler):
    if not ready and request.path.startswith("/api/"):
        return web.json_response({"error": "starting"}, status=503)
    return await handler(request)


# ---------- winning patterns (same 8 as the picture) ----------
PATTERNS = (
    [[(r, c) for c in range(5)] for r in range(5)]            # horizontal lines
    + [[(r, c) for r in range(5)] for c in range(5)]          # vertical lines
    + [[(i, i) for i in range(5)], [(i, 4 - i) for i in range(5)]]   # diagonal, anti-diagonal
    + [[(0, 0), (0, 4), (4, 0), (4, 4)],                      # four corners
       [(1, 1), (1, 3), (3, 1), (3, 3)],                      # center four
       [(0, 2), (2, 0), (2, 4), (4, 2)],                      # T corners (like the picture)
       [(1, 2), (2, 1), (2, 3), (3, 2)]]                      # center T (like the picture)
)
PATTERN_NAMES = (["Horizontal line"] * 5 + ["Vertical line"] * 5
                 + ["Diagonal", "Anti-diagonal"]
                 + ["Four corners", "Center four", "T corners", "Center T"])


def has_bingo(card, called):
    """Any complete pattern at all (old or new)."""
    hit = lambda r, c: card[r][c] == 0 or card[r][c] in called
    return any(all(hit(r, c) for r, c in p) for p in PATTERNS)


def fresh_pattern(card, called_list):
    """A pattern completed by the LATEST called number: (name, cells) or None.
    A pattern that was already complete before the latest call is 'passed'
    and can no longer win; only patterns that include the latest number count."""
    if not called_list:
        return None
    called, last = set(called_list), called_list[-1]
    hit = lambda r, c: card[r][c] == 0 or card[r][c] in called
    for p, nm in zip(PATTERNS, PATTERN_NAMES):
        if any(card[r][c] == last for r, c in p) and all(hit(r, c) for r, c in p):
            return nm, [list(x) for x in p]
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


def log(uid, kind, amount, note="", hidden=False):
    """Save a transaction for the profile page (keeps the last 50).
    hidden=True: admin-only record. It is saved but NOT shown to the player."""
    h = history.setdefault(uid, [])
    e = {"t": int(time.time()), "k": kind, "a": amount, "n": note}
    if hidden:
        e["adm"] = 1
    h.insert(0, e)
    del h[50:]
    save_user(uid)                 # wallet + bonus + history saved to the database


def refund_stake(uid, bet, note, used=None, bonus_used=None):
    """Give a cartela price back and undo the turnover that this cartela added.
    The part that was paid with bonus goes back to bonus, the rest back to cash."""
    if used is None:
        used = stake_used.pop(uid, bet)
    else:
        stake_used.pop(uid, None)
    if bonus_used is None:
        bonus_used = stake_bonus.pop(uid, 0)
    else:
        stake_bonus.pop(uid, None)
    bonus_used = max(0, min(bet, bonus_used))
    wallets[uid] = wallets.get(uid, 0) + (bet - bonus_used)
    bonus[uid] = bonus.get(uid, 0) + bonus_used
    wagered[uid] = max(0, wagered.get(uid, 0) - used)
    log(uid, "refund", bet, note)


def turnover_left(uid):
    """Birr the player must still play before his deposit is fully unlocked."""
    return max(0, required.get(uid, 0) - wagered.get(uid, 0))


def withdrawable_amount(uid):
    """Cash that can be withdrawn now: wallet minus the deposit still to be played.
    Bonus money is never withdrawable, so it is not counted here."""
    return max(0, wallets.get(uid, 0) - turnover_left(uid))


def turnover_info(uid):
    """Played / required / left, for messages and for the web app."""
    req = required.get(uid, 0)
    wag = min(wagered.get(uid, 0), req)
    return {"played": wag, "required": req, "left": max(0, req - wag)}


def deposit_multiplier(uid):
    """1x for the player's first deposit, 0.5x for every deposit after that."""
    return FIRST_DEPOSIT_TURNOVER if dep_count.get(uid, 0) == 0 else NEXT_DEPOSIT_TURNOVER


def fmt_x(v):
    """1.0 -> '1x', 0.5 -> '0.5x'"""
    return (f"{v:g}") + "x"


def rule_text(uid):
    """The wagering rule that applies to this player's NEXT deposit (shown before paying)."""
    if dep_count.get(uid, 0) == 0:
        return ("ℹ️ ማሳሰቢያ: ለመጀመሪያ ጊዜ ያስገቡትን ገንዘብ ከማውጣትዎ በፊት "
                f"አንድ ጊዜ ({fmt_x(FIRST_DEPOSIT_TURNOVER)}) መጫወት አለብዎት። "
                f"ከዚያ በኋላ ለሚያስገቡት ግማሹን ({fmt_x(NEXT_DEPOSIT_TURNOVER)}) ብቻ።\n"
                "Note: your first deposit must be played "
                f"{fmt_x(FIRST_DEPOSIT_TURNOVER)} before you can withdraw it. "
                f"Later deposits only need {fmt_x(NEXT_DEPOSIT_TURNOVER)}. "
                "Your winnings are yours to withdraw.")
    return ("ℹ️ ማሳሰቢያ: ለሚያስገቡት ገንዘብ ግማሹን "
            f"({fmt_x(NEXT_DEPOSIT_TURNOVER)}) ከማውጣትዎ በፊት መጫወት አለብዎት።\n"
            "Note: this deposit must be played "
            f"{fmt_x(NEXT_DEPOSIT_TURNOVER)} (half the amount) before you can withdraw it. "
            "Your winnings are yours to withdraw.")


def progress_text(uid):
    """Short 'Played 150 / 200 birr' line (empty if the player has nothing to play)."""
    t = turnover_info(uid)
    if t["required"] <= 0:
        return ""
    if t["left"] > 0:
        return (f"🎯 ተጫውተዋል / Played: {t['played']} / {t['required']} birr\n"
                f"⏳ ከተቀማጭዎ {t['left']} ብር መጫወት ይቀራል\n"
                f"Play {t['left']} more birr to unlock your deposit.")
    return ""                  # requirement finished: show nothing


def turnover_msg(uid):
    """Shown when a player tries to withdraw more than he can right now."""
    t = turnover_info(uid)
    w = withdrawable_amount(uid)
    return (f"⚠️ አሁን ማውጣት የሚችሉት: {w} ብር\n"
            f"🎯 ተጫውተዋል: {t['played']} / {t['required']} ብር · ቀሪ: {t['left']} ብር\n"
            f"ዝቅተኛው የማውጫ መጠን {MIN_WITHDRAW} ብር ነው።\n\n"
            f"You can withdraw up to {w} birr now. "
            f"{t['left']} birr of your deposit still has to be played.\n"
            f"Played {t['played']} / {t['required']} birr. "
            f"Minimum withdraw is {MIN_WITHDRAW} birr.")


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
    else:
        stake_used.pop(uid, None)                         # round already started: stake is played
        stake_bonus.pop(uid, None)
    g["players"].pop(uid, None)
    save_room_leave(uid)
    if g["phase"] == "lobby" and len(g["players"]) < MIN_PLAYERS:
        g["deadline"] = None                              # not enough players: stop countdown


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


def award_winners(g, claimants=None):
    """Finish the round and pay the winner(s).
    Rule: only a pattern completed by the LATEST called number can win.
    Only players who CLAIMED it share the prize equally: manual players who
    pressed BINGO in time, Auto players, and players who are away / left.
    A manual player who did not press gets nothing. A pattern completed on an
    earlier call has 'passed' and can no longer win.
    Prizes always go to the cash wallet (withdrawable), even if the stake was bonus."""
    hits = {}
    for u, no in g["players"].items():
        if u in g["dq"]:
            continue
        if claimants is not None and u not in claimants:
            continue
        fp = fresh_pattern(make_card(no), g["called"])
        if fp:
            hits[u] = fp
    if not hits:
        return []
    winners = list(hits)
    share = g["prize"] // len(winners)
    shared = len(winners) > 1
    g["phase"] = "finished"
    g["winner"] = winners[0]
    g["winners"] = winners
    g["share"] = share
    g["wlist"] = []
    for u in winners:
        no = g["players"][u]
        nm, cells = hits[u]
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
    return winners


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
    """Add the money to the player, set the wagering requirement
    (1x for the first deposit, 0.5x after that), pay the invite bonus, tell the player."""
    mult = deposit_multiplier(uid)
    added = math.ceil(amount * mult)                       # whole birr, rounded up
    wallets[uid] = wallets.get(uid, 0) + amount
    deposited[uid] = deposited.get(uid, 0) + amount
    required[uid] = required.get(uid, 0) + added
    dep_count[uid] = dep_count.get(uid, 0) + 1
    log(uid, "deposit", amount, "Deposit approved")
    for t in tokens:
        mark_token(t)
    inv = referrer.get(uid)
    pk = phones.get(uid, "")[-9:]
    if REF_BONUS_PERCENT and inv and pk and pk not in ref_phones and uid not in ref_paid:
        ref_phones.add(pk)
        _save_counter("refphone:" + pk, 1)
        ref_paid.add(uid)                              # first deposit only
        save_user(uid)
        bonus_amt = amount * REF_BONUS_PERCENT // 100
        if bonus_amt > 0:
            # invite bonus is PLAY-ONLY: it goes to the bonus balance, not the wallet,
            # so it can never be withdrawn or transferred (only played)
            bonus[inv] = bonus.get(inv, 0) + bonus_amt
            ref_earn[inv] = ref_earn.get(inv, 0) + bonus_amt
            log(inv, "bonus", bonus_amt, f"Invite bonus · {names.get(uid, uid)}")
            try:
                await bot.send_message(
                    inv, f"🎁 ጋብዘውት የነበረው {names.get(uid, 'ተጫዋች')} ገንዘብ አስገብቷል። "
                         f"{bonus_amt} ብር ቦነስ አግኝተዋል! (ለመጫወት ብቻ)\n"
                         f"You got a {bonus_amt} birr bonus (play only). "
                         "Winnings from it are withdrawable.")
            except Exception:
                pass
    try:
        await bot.send_message(
            uid,
            "✅ <b>Deposit successful!</b>\n"
            "<blockquote>"
            f"Your wallet has been credited with {amount:.2f} ETB\n"
            f"Your wallet balance: {wallets[uid]:.2f}"
            "</blockquote>\n"
            "🙏 Thank you for choosing us! ❤️\n\n"
            f"ℹ️ ገንዘብ ከማውጣትዎ በፊት {added} ብር ({fmt_x(mult)}) መጫወት አለብዎት።\n"
            f"Play {added} birr ({fmt_x(mult)}) before withdrawing.\n\n"
            + html.escape(progress_text(uid)),
            parse_mode="HTML")
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
    """Your phone's SMS-forwarder app sends every SMS here (POST only).
    The secret must be sent in the header  X-Key  (it is never put in the URL,
    so it can not appear in server logs)."""
    given = req.headers.get("X-Key", "")
    if not SMS_SECRET or not hmac.compare_digest(given.encode(), SMS_SECRET.encode()):
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


# ---------- AMHARIC NUMBER SOUND (played inside the web app only, never sent to the chat) ----------
AUDIO_DIR = Path(__file__).parent / "audio"
VOICE = "am-ET-AmehaNeural"
ONES = ["", "አንድ", "ሁለት", "ሦስት", "አራት", "አምስት", "ስድስት", "ሰባት", "ስምንት", "ዘጠኝ"]
TENS = {2: "ሃያ", 3: "ሠላሳ", 4: "አርባ", 5: "ሃምሳ", 6: "ስድሳ", 7: "ሰባ"}
LETTER_SOUND = {"B": "ቢ", "I": "አይ", "N": "ኤን", "G": "ጂ", "O": "ኦ"}


def amharic(n):
    if n == 10: return "አስር"
    if n < 10: return ONES[n]
    if n < 20: return "አስራ " + ONES[n - 10]
    t, o = divmod(n, 10)
    return (TENS[t] + " " + ONES[o]).strip()


def call_letter(n):
    return "BINGO"[(n - 1) // 15]


async def make_audio(n):
    AUDIO_DIR.mkdir(exist_ok=True)
    path = AUDIO_DIR / f"{n}.mp3"
    if not path.exists():
        tmp = AUDIO_DIR / f"{n}.{secrets.token_hex(4)}.tmp"
        text = f"{LETTER_SOUND[call_letter(n)]}፣ {amharic(n)}"
        await edge_tts.Communicate(text, VOICE).save(str(tmp))
        tmp.replace(path)
    return path


async def api_audio(req):
    try:
        n = int(req.match_info["n"])
    except ValueError:
        return web.Response(status=404)
    if not 1 <= n <= 75:
        return web.Response(status=404)
    try:
        path = await make_audio(n)
    except Exception as e:
        print("audio failed:", repr(e))
        return web.Response(status=503)
    return web.FileResponse(
        path, headers={"Cache-Control": "public, max-age=604800, immutable"})   # files are versioned with ?v= in the page


async def pregen_audio():
    for n in range(1, 76):
        try:
            await make_audio(n)
        except Exception as e:
            print("pregen failed:", repr(e))
            await asyncio.sleep(5)


# ---------- background jobs ----------
async def reaper():
    """Remove players who closed the app without pressing LEAVE.
    Only in the LOBBY (they get their stake back). Once a round has started the
    player stays in the game: their stake is already in the prize pool, and if
    their card wins while they are away, the win is claimed for them."""
    while True:
        await asyncio.sleep(5)
        now = time.time()
        for g in rooms.values():
            if g["phase"] != "lobby":
                continue
            for uid in list(g["players"]):
                if now - seen.get(uid, 0) > IDLE_KICK:
                    remove_player(uid)


async def run_round(g):
    g.update(called=[], phase="lobby", winner=None, prize=0,
             round=g["round"] + 1, deadline=None,
             seq=[], secret="", hash="",
             wcells=[], wname="", finish_at=0,
             winners=[], wlist=[], share=0, dq=set(), left=set(),
             claims=set(), claim_t=0)

    # wait until MIN_PLAYERS have chosen a cartela (that starts the countdown),
    # then count down. If players leave and fewer than MIN_PLAYERS remain,
    # the countdown stops and starts again when enough players are back.
    while True:
        if (g["deadline"] is not None and time.time() >= g["deadline"]
                and len(g["players"]) >= MIN_PLAYERS):
            break
        await asyncio.sleep(1)

    # provably fair: pick all numbers + secret first, publish the hash
    g["seq"] = rng.sample(range(1, 76), 75)
    g["secret"] = "".join(secrets.choice(string.ascii_letters) for _ in range(16))
    g["hash"] = hashlib.sha256(
        f"{g['secret']}:{','.join(map(str, g['seq']))}".encode()).hexdigest()

    g["phase"] = "playing"
    g["prize"] = int(len(g["players"]) * g["bet"] * 0.8)
    for n in g["seq"]:
        if (g["phase"] != "playing" or not g["players"]
                or all(u in g["dq"] for u in g["players"])):
            break
        g["called"].append(n)
        g["claims"] = set()                # manual players who press BINGO for THIS call
        g["claim_t"] = 0
        call_t = time.time()

        # The server claims BINGO right on the call for two kinds of players:
        #   - players with Auto ON in the web app (they never miss a call)
        #   - players who are AWAY (app closed)
        # Manual players must press BINGO themselves before the next number is called.
        now = time.time()
        away = {u for u in g["players"]
                if now - seen.get(u, 0) > IDLE_KICK or u in g["left"]}
        claimers = [u for u, no in g["players"].items()
                    if u not in g["dq"]
                    and (u in away or auto_pref.get(u))
                    and fresh_pattern(make_card(no), g["called"])]
        # Wait for this call's claims. The first claim (an Auto / away player right
        # now, or the first manual press) starts a short grace time so other manual
        # players can still press BINGO. Then everybody who claimed shares the prize.
        first = call_t if claimers else 0
        while True:
            now2 = time.time()
            if not first and g["claim_t"]:
                first = g["claim_t"]
            if first and now2 >= first + CLAIM_GRACE:
                break
            if not first and now2 >= call_t + CALL_EVERY:
                break
            await asyncio.sleep(0.2)
        if first:
            award_winners(g, set(claimers) | set(g["claims"]))
            if g["winners"]:
                break

    g["phase"] = "finished"
    if not g["winners"]:                   # nobody won: give bets back, except cheaters
        for uid in list(g["players"]):
            if uid in g["dq"]:             # banned for false / late bingo: no refund
                continue
            refund_stake(uid, g["bet"], f"Room {g['bet']} refund (no winner)")
            try:
                await BOT.send_message(
                    uid, f"↩️ በዚህ ዙር ማንም አላሸነፈም። {g['bet']} ብር ተመልሶልዎታል።\n"
                         f"No winner this round. Your {g['bet']} birr was returned.")
            except Exception:
                pass
    save_room_clear(g["bet"])              # round is over: stakes are settled
    for uid in list(g["players"]):
        stake_used.pop(uid, None)          # this round's stake is final, no refund possible
        stake_bonus.pop(uid, None)
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


def play_balance(uid):
    """Everything the player can bet with: cash wallet + play-only bonus."""
    return wallets.get(uid, 0) + bonus.get(uid, 0)


async def api_state(req):
    user = verify(req.query.get("initData", ""))
    if not user:
        return web.json_response({"error": "auth"})
    uid = touch(user)
    if "auto" in req.query:                      # the web app tells us if Auto is ON
        auto_pref[uid] = req.query.get("auto") == "1"
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
        "players": n, "minPlayers": MIN_PLAYERS,
        "bet": g["bet"], "derash": int(n * g["bet"] * 0.8),
        "wallet": play_balance(uid), "bonus": bonus.get(uid, 0),
        "cash": wallets[uid], "name": names[uid],
        "withdrawable": withdrawable_amount(uid),
        "turnover": turnover_info(uid),
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
        "dq": uid in g["dq"],
        # provably fair: the hash is published as soon as the round starts
        # (before the first number is called); the secret is revealed after it ends
        "hash": g["hash"] if g["phase"] != "lobby" else "",
        "secret": g["secret"] if g["phase"] == "finished" else "",
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
        "balance": play_balance(uid), "bonus": bonus.get(uid, 0),
        "cash": wallets[uid], "withdrawable": withdrawable_amount(uid),
        "joined": joined[uid],
        "wins": wins.get(uid, 0), "won": won.get(uid, 0),
        "turnover": turnover_info(uid),
        "tx": [t for t in history.get(uid, []) if not t.get("adm")][:30],   # admin-only records are not shown to players
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
    if play_balance(uid) < g["bet"]:
        return web.json_response({"ok": False, "error": "Not enough balance"})

    # the stake is paid with BONUS first, then with cash.
    # Only the CASH part counts toward the deposit turnover (bonus play never
    # unlocks withdrawals), and only the part that is still OWED counts, so extra
    # play can never be saved up to skip the rule on a later deposit.
    bonus_used = min(bonus.get(uid, 0), g["bet"])
    cash_used = g["bet"] - bonus_used
    used = min(cash_used, turnover_left(uid))
    bonus[uid] = bonus.get(uid, 0) - bonus_used
    wallets[uid] -= cash_used
    wagered[uid] = wagered.get(uid, 0) + used
    stake_used[uid] = used
    stake_bonus[uid] = bonus_used
    log(uid, "bet", -g["bet"], f"Room {g['bet']} · cartela {card}"
        + (f" (bonus {bonus_used})" if bonus_used else ""))
    g["players"][uid] = card
    save_room_join(g["bet"], uid, card, used, bonus_used)
    if g["deadline"] is None and len(g["players"]) >= MIN_PLAYERS:
        g["deadline"] = time.time() + LOBBY_SECONDS     # clock starts when enough players joined
    return web.json_response({"ok": True})


async def notify_false_bingo(uid, card_no, bet, late=False):
    try:
        if late:
            msg = ("🚫 ቢንጎ ዘግይተው ነው የነኩት! ቁጥሩ ቀድሞ አልፏል። ካርታዎ #" + str(card_no) +
                   " ከዚህ ጨዋታ ታግዷል።\n"
                   "You pressed BINGO too late: that number had already passed. "
                   f"Your cartela #{card_no} is banned from this game. "
                   "The game continues without you.")
        else:
            msg = ("🚫 ቢንጎ አላሸነፉም! ካርታዎ #" + str(card_no) + " ከዚህ ጨዋታ ታግዷል።\n"
                   "You pressed BINGO but you did not win. "
                   f"Your cartela #{card_no} is banned from this game. "
                   "The game continues without you.")
        await BOT.send_message(uid, msg)
    except Exception:
        pass


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
    if uid in g["dq"]:                         # already banned in this round
        return web.json_response({"ok": False, "disq": True})
    card = make_card(g["players"][uid])
    # VALID press: a pattern completed by the latest called number.
    # Everybody else whose card completed on this same call shares the prize.
    if fresh_pattern(card, g["called"]):
        g["claims"].add(uid)                   # counted when the claim time ends (see run_round)
        if not g["claim_t"]:
            g["claim_t"] = time.time()
        return web.json_response({"ok": True})
    # INVALID press = ban for the rest of this round:
    #   late  = the card had a real pattern, but it was completed by an earlier call
    #   false = the card has no complete pattern at all
    # The stake stays in the prize pool and the game continues without him.
    late = has_bingo(card, set(g["called"]))
    g["dq"].add(uid)
    t = asyncio.create_task(
        notify_false_bingo(uid, g["players"][uid], g["bet"], late))
    bg_tasks.add(t)
    t.add_done_callback(bg_tasks.discard)
    return web.json_response({"ok": False, "disq": True,
                              "reason": "late" if late else "false"})


async def api_leave(req):
    body = await req.json()
    user = verify(body.get("initData", ""))
    if user:
        g = room_of(user["id"])
        if g and g["phase"] == "lobby":
            remove_player(user["id"])              # lobby: stake is refunded
        elif g and g["phase"] == "playing":
            g["left"].add(user["id"])              # round running: bet stays in the game,
                                                   # the server claims a win for this player
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


def pending_deposits(uid):
    """How many deposit requests of this player are waiting for the admin."""
    return sum(1 for r in pending.values()
               if r["type"] == "deposit" and r["uid"] == uid)


# ---------- deposit menu (CBE / Telebirr / CBE Birr) ----------
def deposit_menu_text(uid):
    return ("እባክዎ የሚፈልጉትን የመክፈያ አማራጭ ይምረጡ 👇\n\n"
            "Please select the top-up option you wish to use:\n\n"
            f"💵 ዝቅተኛው ማስገቢያ {MIN_DEPOSIT} ብር ነው። / Minimum deposit: {MIN_DEPOSIT} birr.\n\n"
            + rule_text(uid))


def deposit_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="CBE", callback_data="dep:cbe")],
        [InlineKeyboardButton(text="Telebirr", callback_data="dep:telebirr"),
         InlineKeyboardButton(text="CBE Birr", callback_data="dep:cbebirr")],
    ])


def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ ተመለስ / Back", callback_data="dep:menu")]])


def deposit_steps(target, sender, uid):
    return (
        "<b>መመሪያ</b>\n"
        f"1) ከላይ በተቀመጠው የ {target} ገንዘቡን ያስገቡ።\n"
        f"2) የከፈላችሁበትን (transaction) ደረሰኝ መረጃ የያዘ አጭር የጽሁፍ መልዕክት(SMS) ከ {sender} እስኪደርሳችሁ ትጠብቃላችሁ።\n"
        "3) የደረሳችሁን አጭር የጽሁፍ መልዕክት(SMS) መጀመሪያ ኮፒ(copy) ከዚያም ከታች ባለው የቴሌግራም የጽሁፍ ማስገቢያ ላይ ፔስት(paste) በማድረግ ይላኩት።\n\n"
        "<b>ማሳሰቢያ</b>\n\n"
        "በክፍያ ወቅት ያጋጠማችሁ ችግር ካለ\n"
        f"{DEPOSIT_SUPPORT}\n"
        "ማውራት ትችላላችሁ።\n"
        "እናመሰግናለን!\n\n"
        + html.escape(rule_text(uid)))


def deposit_details(kind, uid):
    not_set = f"Payment details are not available right now. Contact {DEPOSIT_SUPPORT}"
    if kind == "cbe":
        if not CBE_ACCOUNT:
            return html.escape(not_set)
        return (
            "የ CBE አካውንት\n"
            f"<code>{html.escape(CBE_ACCOUNT)}</code> _{html.escape(CBE_NAME)}\n\n"
            + deposit_steps("CBE አካውንት ቁጥር", "CBE", uid))
    if kind == "telebirr":
        if not TELEBIRR_PHONE:
            return html.escape(not_set)
        return (
            "የቴሌብር (Telebirr) ቁጥር\n"
            f"<code>{html.escape(TELEBIRR_PHONE)}</code> _{html.escape(TELEBIRR_NAME)}\n\n"
            + deposit_steps("ቴሌብር ስልክ ቁጥር", "Telebirr", uid))
    if kind == "cbebirr":
        if not CBEBIRR_PHONE:
            return html.escape(not_set)
        return (
            "የ CBE Birr ቁጥር\n"
            f"<code>{html.escape(CBEBIRR_PHONE)}</code> _{html.escape(CBEBIRR_NAME)}\n\n"
            + deposit_steps("CBE Birr ስልክ ቁጥር", "CBE Birr", uid))
    return None


@dp.callback_query(F.data.startswith("dep:"))
async def deposit_buttons(cb: CallbackQuery):
    uid = ensure(cb.from_user)
    kind = (cb.data or "").split(":", 1)[1]
    try:
        if kind == "menu":
            await cb.message.edit_text(deposit_menu_text(uid), reply_markup=deposit_kb())
        else:
            text = deposit_details(kind, uid)
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
              "To continue, tap the button below to share your phone number.\n\n"
              "ቁልፉ ካልታየ ከመልእክት መጻፊያው አጠገብ ያለውን ▦ አዶ ይንኩ።\n"
              "Can't see the button? Tap the ▦ icon next to the message box.")
WELCOME_TEXT = ("👋 እንኳን በደህና መጡ! ቢንጎ ለመጫወት ዝግጁ ነዎት?\n\n"
                "Welcome! Ready to play Bingo?\n\n"
                f"{BRAND}")


def start_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Start", callback_data="start:go")]])


def phone_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 ስልክ ቁጥር ያጋሩ / Share phone number",
                                  request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=False,
        input_field_placeholder="📱 Tap: Share phone number")


async def need_phone(m: Message):
    """True (and shows the Start button) if the player has not shared a phone yet."""
    if m.from_user.id in phones:
        return False
    await m.answer(START_TEXT, reply_markup=start_kb())
    return True


@dp.callback_query(F.data == "start:go")
async def start_go(cb: CallbackQuery):
    try:
        await cb.answer()
    except Exception:
        pass
    if cb.from_user.id in phones:
        await cb.message.answer(WELCOME_TEXT, reply_markup=menu_kb(cb.from_user.id))
    else:
        await cb.message.answer(PHONE_TEXT, reply_markup=phone_kb())


@dp.message(F.contact)
async def got_contact(m: Message):
    uid = ensure(m.from_user)
    c = m.contact
    if c.user_id != uid:                        # must be the player's OWN number
        await m.answer("እባክዎ የራስዎን ስልክ ቁጥር ለማጋራት ከታች ያለውን ቁልፍ ይጠቀሙ።\n"
                       "Please use the button to share your own number.",
                       reply_markup=phone_kb())
        return
    first_time = uid not in phones                  # registration bonus only the first time
    digits = "".join(ch for ch in c.phone_number if ch.isdigit())
    phones[uid] = digits
    save_user(uid)
    await m.answer("✅ ስልክ ቁጥርዎ ተመዝግቧል። እናመሰግናለን!\nPhone number saved. Thank you!",
                   reply_markup=ReplyKeyboardRemove())
    used_by_other = any(p == digits for u, p in phones.items() if u != uid)
    if REG_BONUS > 0 and first_time and uid not in banned and not used_by_other:
        bonus[uid] = bonus.get(uid, 0) + REG_BONUS
        log(uid, "bonus", REG_BONUS, "Welcome bonus")      # also saves the player
        await m.answer(REG_BONUS_TEXT.format(amount=REG_BONUS))
    await m.answer(WELCOME_TEXT, reply_markup=menu_kb(uid))


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
        await m.answer(WELCOME_TEXT, reply_markup=menu_kb(uid))
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
    cash = wallets[uid]
    bon = bonus.get(uid, 0)
    w = withdrawable_amount(uid)          # cash minus the deposit still to be played
    locked = (cash - w) + bon             # cash still waiting for turnover + play-only bonus
    total = cash + bon
    prog = progress_text(uid)
    extra = ("\n\n" + prog) if prog else ""
    await m.answer(
        "Your wallet's detail currently is:\n\n"
        "<blockquote>"
        f"Name:  {html.escape(names[uid])}\n"
        f"Phone Number:  {phones.get(uid, '-')}\n"
        f"Withdrawable Balance:  {w:.2f} ETB\n"
        f"Non-withdrawable Bal:  {locked:.2f} ETB\n"
        "----------------------------------------\n"
        f"<b>Total Balance: {total:.2f} ETB</b>\n"
        "----------------------------------------"
        "</blockquote>" + html.escape(extra + "\n\n" + BRAND),
        parse_mode="HTML")


@dp.message(Command("deposit"))
async def cmd_deposit(m: Message, command: CommandObject):
    uid = ensure(m.from_user)
    if await need_phone(m):
        return
    args = (command.args or "").split(maxsplit=1)
    amount = to_amount(args[0]) if args else None
    if not amount:
        await m.answer(deposit_menu_text(uid), reply_markup=deposit_kb())
        return
    if amount < MIN_DEPOSIT:
        await m.answer(f"ዝቅተኛው የማስገቢያ መጠን {MIN_DEPOSIT} ብር ነው።\nMinimum deposit is {MIN_DEPOSIT} birr.")
        return
    if pending_deposits(uid) >= MAX_PENDING_DEPOSITS:
        await m.answer(f"አስቀድመው {MAX_PENDING_DEPOSITS} ጥያቄዎች በመጠባበቅ ላይ ናቸው።\n"
                       f"You already have {MAX_PENDING_DEPOSITS} deposit requests waiting. Please wait.")
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
# Only CASH can be withdrawn: wallet minus the deposit that still has to be played.
# Bonus money is play-only and is never counted.
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
    if amount > withdrawable_amount(uid):
        await m.answer(turnover_msg(uid))
        return
    if not ADMIN_ID:
        await m.answer(f"Withdrawals are handled by support: {SUPPORT}")
        return
    wallets[uid] -= amount                       # hold the money
    log(uid, "withdraw", -amount, f"Withdraw request · {method}")
    req_counter[0] += 1
    rid = req_counter[0]
    # method + account are saved so the player gets a detailed message when you press Paid
    pending[rid] = {"type": "withdraw", "uid": uid, "amount": amount,
                    "method": method, "account": account}
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
    if withdrawable_amount(uid) < MIN_WITHDRAW:
        await m.answer(turnover_msg(uid))
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
            if withdrawable_amount(uid) < MIN_WITHDRAW:
                await cb.message.edit_text(turnover_msg(uid))
                await cb.answer()
                return
            wd_state[uid] = {"m": kind, "step": "amount", "t": time.time()}
            await cb.message.edit_text(
                f"💸 {WD_NAMES[kind]} ገንዘብ ማውጣት\n\n"
                f"💰 ማውጣት የሚችሉት: {withdrawable_amount(uid):.2f} ብር\n\n"
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
        if amount > withdrawable_amount(uid):
            await m.answer(f"ማውጣት የሚችሉት {withdrawable_amount(uid)} ብር ብቻ ነው።\n"
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


# (The /transfer command has been removed completely.)


# /instruction and /instructions both work (the menu uses /instructions)
@dp.message(Command("instruction", "instructions"))
async def cmd_instruction(m: Message):
    await m.answer(
        "📖 How to play\n\n"
        "1) Tap /play and choose a room (10, 20, 50 or 100 birr).\n"
        "2) Choose a cartela number. It costs the room price.\n"
        "3) When the countdown ends, numbers are called one by one.\n"
        "4) Keep Auto on to mark numbers automatically and claim BINGO for you.\n"
        "5) With Auto off, mark numbers yourself and press BINGO WIN.\n\n"
        "🏆 Winning Patterns:\n"
        "• Horizontal line\n• Vertical line\n• Diagonal\n• Anti-diagonal\n• Four corners\n"
        "• Center four\n• T corners\n• Center T\n\n"
        "Complete any pattern to win the round prize!\n\n"
        "⚠️ A pattern wins only on the call that completes it. If you press BINGO "
        "after the next number was already called, or without a real pattern, "
        "your cartela is banned for that round. Players who complete a pattern on "
        "the same call share the prize.\n\n"
        "ℹ️ If a round cannot start, your stake is refunded.\n"
        "ℹ️ If you close the app during a round, your cartela stays in the game and "
        "a win is added to your balance automatically.\n\n"
        "💸 Withdrawing: your first deposit must be played "
        f"{fmt_x(FIRST_DEPOSIT_TURNOVER)} before you can withdraw it. Later deposits only need "
        f"{fmt_x(NEXT_DEPOSIT_TURNOVER)}. Every bet counts, win or lose, and your winnings "
        "are yours to withdraw. Use /balance to see your progress.\n"
        "የመጀመሪያ ገንዘብዎን አንድ ጊዜ፣ ቀጣይ ገንዘብዎን ግማሽ ጊዜ ይጫወቱ። ያሸነፉት ገንዘብ የእርስዎ ነው።\n\n"
        "🎁 Bonus money is for playing only and cannot be withdrawn. "
        "Anything you win with it is yours to withdraw.\n"
        "ቦነስ ለመጫወት ብቻ ነው፤ ማውጣት አይቻልም። በቦነስ ያሸነፉት ግን የእርስዎ ነው።\n\n"
        + BRAND)


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
                 f"ከተቀማጩ ገንዘብ {REF_BONUS_PERCENT}% ቦነስ ያገኛሉ! (ለመጫወት ብቻ)\n")
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


# ---------- start screen: button menu ----------
def menu_kb(uid=None):
    b = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [b(text="🎮 ጨዋታ ተጫወት", web_app=WebAppInfo(url=WEBAPP_URL)),
         b(text="🏆 የማሸነፊያ ስርዓቶች", callback_data="menu:patterns")],
        [b(text="📝 የጨዋታ መመሪያ", callback_data="menu:instr"),
         b(text="💰 የእኔ ቀሪ ሂሳብ", callback_data="menu:balance")],
        [b(text="💵 ገንዘብ አስገባ", callback_data="menu:deposit"),
         b(text="💸 ገንዘብ አውጣ", callback_data="menu:withdraw")],
        [b(text="📜 የእኔ ግብይቶች", callback_data="menu:tx"),
         b(text="👥 ጓደኞችን ጋብዝ", callback_data="menu:invite")],
        [b(text="📞 ድጋፍ ያግኙ", callback_data="menu:support")],
    ])


WIN_PATTERNS_TEXT = (
    "🏆 የማሸነፊያ ስርዓቶች / Winning Patterns\n\n"
    "• አግድም መስመር (Horizontal line)\n"
    "• ቁም መስመር (Vertical line)\n"
    "• ዲያጎናል / ሰያፍ (Diagonal)\n"
    "• ተቃራኒ ዲያጎናል (Anti-diagonal)\n"
    "• አራቱም ማዕዘኖች (Four corners)\n"
    "• የመካከል አራት (Center four)\n"
    "• የT ማዕዘኖች (T corners)\n"
    "• የመካከል T (Center T)\n\n"
    "ማንኛውንም አንዱን ሲያሟሉ ያሸንፋሉ!\n"
    "Complete any pattern to win the round prize!")

_patterns_file_id = [None]       # Telegram remembers the picture after the first send


async def send_patterns(m: Message):
    """Send the winning patterns picture (text only if the picture file is missing)."""
    try:
        if _patterns_file_id[0]:
            await m.answer_photo(_patterns_file_id[0], caption=WIN_PATTERNS_TEXT)
            return
        if PATTERNS_IMG.exists():
            sent = await m.answer_photo(FSInputFile(PATTERNS_IMG),
                                        caption=WIN_PATTERNS_TEXT)
            _patterns_file_id[0] = sent.photo[-1].file_id
            return
    except Exception as e:
        print("patterns picture failed:", repr(e))
    await m.answer(WIN_PATTERNS_TEXT)


# the /winning_patterns command (shows in the menu list)
@dp.message(Command("winning_patterns"))
async def cmd_winning_patterns(m: Message):
    ensure(m.from_user)
    await send_patterns(m)


TX_ICONS = {"deposit": "💵", "withdraw": "💸", "bet": "🎯", "win": "🏆",
            "refund": "↩️", "bonus": "🎁", "transfer": "🔁"}


@dp.message(Command("transactions"))
async def cmd_transactions(m: Message):
    uid = ensure(m.from_user)
    if await need_phone(m):
        return
    # only deposits and withdrawals are shown here
    rows = [t for t in history.get(uid, [])
            if t.get("k") in ("deposit", "withdraw")][:10]
    if not rows:
        await m.answer("📜 ምንም ግብይት የለም።\nNo deposits or withdrawals yet.")
        return
    lines = []
    for t in rows:
        when = time.strftime("%d %b %H:%M", time.gmtime(t["t"] + 3 * 3600))
        sign = "+" if t["a"] > 0 else ""
        lines.append(f"{TX_ICONS.get(t['k'], '•')} {sign}{t['a']} ETB · "
                     f"{html.escape(t.get('n') or t['k'])}\n🕒 {when}")
    await m.answer("📜 <b>My Transactions</b> · Deposits & Withdrawals (last 10)\n\n" + "\n\n".join(lines),
                   parse_mode="HTML")


@dp.callback_query(F.data.startswith("menu:"))
async def menu_buttons(cb: CallbackQuery):
    action = (cb.data or "").split(":", 1)[1]

    await cb.answer()
    # same message, but "from" is the player who pressed the button,
    # so the normal command handlers work unchanged
    m = cb.message.model_copy(update={"from_user": cb.from_user})
    if action == "patterns":
        await send_patterns(m)
    elif action == "instr":
        await cmd_instruction(m)
    elif action == "balance":
        await cmd_balance(m)
    elif action == "deposit":
        await cmd_deposit(m, CommandObject(command="deposit", args=None))
    elif action == "withdraw":
        await cmd_withdraw(m, CommandObject(command="withdraw", args=None))
    elif action == "tx":
        await cmd_transactions(m)
    elif action == "invite":
        await cmd_invite(m)
    elif action == "support":
        await cmd_support(m)


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
        "/players\nAll players and balances (or /players <phone> for one player)\n\n"
        "/online\nHow many players are online / checked in (now, 5 min, 1 hour, 24 hours)\n\n"
        "/backup\nDownload a backup of all data (zip file) to your chat\n\n"
        "/nophone\nAsk players who have no phone number to share it (Amharic + English)\n\n"
        "/post\nSend a picture or text with a ተጫወት button to all players (you see a preview first)\n\n"
        "/addbalance <phone> <amount>\nAdd real money to a player. Use a minus to remove: -50\n\n"
        "/addbonus <phone> <amount>\nAdd PLAY-ONLY bonus (cannot be withdrawn). Minus removes: -20\n\n"
        "/bonusmany <amount> all  or  <amount> <phone> <phone>...\nGive PLAY-ONLY bonus to many players (you confirm first)\n\n"
        "/resetfree\nRemove free balance from players who never deposited\n\n"
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
        f"Cash in player wallets: {sum(wallets.values()):,} ETB\n"
        f"Bonus in player wallets (play only): {sum(bonus.values()):,} ETB\n\n"
        f"Waiting deposits: {len(deps)} ({sum(r['amount'] for r in deps):,} ETB)\n"
        f"Waiting withdrawals: {len(wds)} ({sum(r['amount'] for r in wds):,} ETB)\n\n"
        f"<b>Last 24 hours</b> (from the last 300 games)\n"
        f"Games: {len(recent)}\n"
        f"Stakes played: {stake:,} ETB\n"
        f"Paid to winners: {paid:,} ETB\n"
        f"House earned (approx): {stake - paid:,} ETB",
        parse_mode="HTML")


# ---------- /online: how many players are in the app (admin only) ----------
ONLINE_NOW = 15            # the web app asks the server every second, so 15 s = "online right now"


@dp.message(Command("online"))
async def cmd_online(m: Message):
    if not is_admin(m):
        return
    now = time.time()
    count = lambda sec: sum(1 for t in seen.values() if now - t <= sec)
    new24 = sum(1 for t in joined.values() if t >= now - 86400)
    room_lines = []
    for b, g in rooms.items():
        room_lines.append(f"  {b} birr: {len(g['players'])} player(s) · {g['phase']}")
    on_now = sorted((u for u, t in seen.items() if now - t <= ONLINE_NOW),
                    key=lambda u: -seen[u])
    names_txt = ", ".join(html.escape(names.get(u, "Player")) for u in on_now[:25])
    if len(on_now) > 25:
        names_txt += f" … +{len(on_now) - 25} more"
    await m.answer(
        "🟢 <b>Players online</b>\n\n"
        f"Online now: <b>{count(ONLINE_NOW)}</b>\n"
        f"Last 5 minutes: {count(300)}\n"
        f"Last 1 hour: {count(3600)}\n"
        f"Last 24 hours: {count(86400)}\n\n"
        f"New players (24h): {new24}\n"
        f"Total players: {len(wallets)}\n\n"
        "<b>In rooms</b>\n" + "\n".join(room_lines)
        + (f"\n\n<b>Online now:</b> {names_txt}" if names_txt else "")
        + "\n\n<i>Counts people who opened the web app. They reset when the bot restarts.</i>",
        parse_mode="HTML")


# ---------- /backup: send a backup file of all data (admin only) ----------
BACKUP_TABLES = ("users", "pending", "games", "kv", "used_sms", "room_players", "bank_sms")


def build_backup_zip(dump):
    raw = json.dumps(dump, ensure_ascii=False, indent=1).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("backup.json", raw)
    return buf.getvalue()


@dp.message(Command("backup"))
async def cmd_backup(m: Message):
    if not is_admin(m):
        return
    note = await m.answer("⏳ Making backup…")
    try:
        for _ in range(20):                       # let pending database writes finish first
            if _q is None or _q.empty():
                break
            await asyncio.sleep(0.25)
        await asyncio.sleep(0.5)
        dump = {"created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "tables": {}}
        async with pool.acquire() as c:
            # one consistent snapshot of all tables
            async with c.transaction(isolation="repeatable_read", readonly=True):
                for t in BACKUP_TABLES:
                    rows = []
                    for r in await c.fetch(f"SELECT * FROM {t}"):
                        row = {}
                        for k, v in dict(r).items():
                            if isinstance(v, str) and k == "data":
                                try:
                                    v = json.loads(v)       # keep the JSON readable
                                except ValueError:
                                    pass
                            row[k] = v
                        rows.append(row)
                    dump["tables"][t] = rows
        data = await asyncio.to_thread(build_backup_zip, dump)
        if len(data) > 49 * 1024 * 1024:
            await note.edit_text("❌ Backup is bigger than Telegram's 50 MB file limit.")
            return
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")
        counts = ", ".join(f"{t}: {len(dump['tables'][t])}" for t in BACKUP_TABLES)
        await m.answer_document(
            BufferedInputFile(data, filename=f"bingo-backup-{stamp}-UTC.zip"),
            caption=f"✅ Backup ({len(data) // 1024} KB)\n{counts}\n\n"
                    "Keep it private: it has phone numbers and balances.")
        await note.delete()
    except Exception as e:
        print("backup failed:", repr(e))
        try:
            await note.edit_text("❌ Backup failed. Check the server log.")
        except Exception:
            pass


# ---------- /post: send a picture/text + "ተጫወት" button to all players (admin only) ----------
POST_BUTTON_TEXT = "🎮 ተጫወት"          # the button under the post; it opens the game lobby
post_wait = set()                      # admin pressed /post and the next message is the post
post_pending = {}                      # admin -> (chat_id, message_id, player ids) waiting for Confirm
post_sending = False                   # only one send at a time


def play_button_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=POST_BUTTON_TEXT, web_app=WebAppInfo(url=WEBAPP_URL))]])


@dp.message(Command("post"))
async def cmd_post(m: Message):
    if not is_admin(m):
        return
    post_wait.add(m.from_user.id)
    post_pending.pop(m.from_user.id, None)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Cancel", callback_data="pp:no")]])
    await m.answer("📣 Send me the post now.\n\n"
                   "• One picture with your caption under it (best), or just text.\n"
                   f"• The button “{POST_BUTTON_TEXT}” is added under it automatically and opens the game.\n"
                   "• You will see a preview first. Nothing is sent to players until you confirm.",
                   reply_markup=kb)


def post_waiting(m: Message):
    if not ADMIN_ID or not m.from_user or m.from_user.id != ADMIN_ID:
        return False
    if ADMIN_ID not in post_wait:
        return False
    return not (m.text and m.text.startswith("/"))      # commands still work


@dp.message(post_waiting)
async def post_received(m: Message):
    post_wait.discard(m.from_user.id)
    uids = [u for u in wallets if u not in banned and u in phones]
    if not uids:
        await m.answer("No players found.")
        return
    try:                                                # preview = exactly what players will get
        await m.bot.copy_message(m.chat.id, m.chat.id, m.message_id, reply_markup=play_button_kb())
    except Exception as e:
        print("post preview failed:", repr(e))
        await m.answer("❌ I can't send this kind of message as a post. Send a picture with a caption, or text.")
        return
    post_pending[m.from_user.id] = (m.chat.id, m.message_id, uids)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Send to {len(uids)} players", callback_data="pp:go"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="pp:no")]])
    await m.answer("👆 This is the preview players will get.\n"
                   "Do not delete it until sending is finished.\n\nSend it?", reply_markup=kb)


async def run_post_send(bot, uids, chat_id, msg_id, admin_id):
    global post_sending
    post_sending = True
    ok = fail = 0
    try:
        kb = play_button_kb()
        for u in uids:
            for attempt in (1, 2):
                try:
                    await bot.copy_message(u, chat_id, msg_id, reply_markup=kb)
                    ok += 1
                    break
                except Exception as e:
                    if attempt == 1 and e.__class__.__name__ == "TelegramRetryAfter":
                        await asyncio.sleep(getattr(e, "retry_after", 5) + 1)
                        continue
                    fail += 1                              # blocked the bot, deleted account, etc.
                    break
            await asyncio.sleep(0.06)                      # stay under Telegram's speed limit
    finally:
        post_sending = False
    try:
        await bot.send_message(admin_id, f"📣 Post finished.\n✅ Sent: {ok}\n❌ Failed: {fail} (blocked the bot or left)")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("pp:"))
async def post_buttons(cb: CallbackQuery):
    if not ADMIN_ID or cb.from_user.id != ADMIN_ID:
        await cb.answer("Not allowed", show_alert=True)
        return
    action = (cb.data or "").split(":", 1)[1]
    post_wait.discard(cb.from_user.id)
    data = post_pending.pop(cb.from_user.id, None)
    if action != "go" or not data:
        try:
            await cb.message.edit_text("❌ Cancelled." if action != "go" else "Nothing to send.")
        except Exception:
            pass
        await cb.answer()
        return
    if post_sending:
        post_pending[cb.from_user.id] = data
        await cb.answer("Another post is still being sent. Wait for it to finish.", show_alert=True)
        return
    chat_id, msg_id, uids = data
    try:
        await cb.message.edit_text(f"📤 Sending to {len(uids)} players… I will tell you when it is done.")
    except Exception:
        pass
    await cb.answer()
    t = asyncio.create_task(run_post_send(cb.bot, uids, chat_id, msg_id, cb.from_user.id))
    bg_tasks.add(t)
    t.add_done_callback(bg_tasks.discard)


# ---------- /nophone: ask players who have no phone number to share it (admin only) ----------
NOPHONE_TEXT = ("🎯 Rodas Friend Zone Bingo\n\n"
                "📱 ስልክ ቁጥርዎን ገና አላጋሩም።\n"
                "You have not shared your phone number yet.\n\n") + PHONE_TEXT
nophone_sending = False


def nophone_uids():
    return [u for u in wallets if u not in phones and u not in banned and u != ADMIN_ID]


@dp.message(Command("nophone"))
async def cmd_nophone(m: Message):
    if not is_admin(m):
        return
    uids = nophone_uids()
    if not uids:
        await m.answer("✅ Every player has shared a phone number.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"📤 Send to {len(uids)} players", callback_data="np:go"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="np:no")]])
    await m.answer(f"📱 Players without a phone number: <b>{len(uids)}</b>\n\n"
                   "I will send each of them a message (Amharic + English) with a "
                   "“Share phone number” button.\n\n"
                   "<i>Players who never pressed Start, or who blocked the bot, cannot receive it "
                   "and will show as failed.</i>",
                   parse_mode="HTML", reply_markup=kb)


async def run_nophone_send(bot, uids, admin_id):
    global nophone_sending
    nophone_sending = True
    ok = fail = 0
    try:
        for u in uids:
            if u in phones:                                # shared in the meantime
                continue
            for attempt in (1, 2):
                try:
                    await bot.send_message(u, NOPHONE_TEXT, reply_markup=phone_kb())
                    ok += 1
                    break
                except Exception as e:
                    if attempt == 1 and e.__class__.__name__ == "TelegramRetryAfter":
                        await asyncio.sleep(getattr(e, "retry_after", 5) + 1)
                        continue
                    fail += 1
                    break
            await asyncio.sleep(0.06)                      # stay under Telegram's speed limit
    finally:
        nophone_sending = False
    try:
        await bot.send_message(admin_id, f"📱 Phone requests finished.\n✅ Sent: {ok}\n"
                                         f"❌ Failed: {fail} (never started the bot or blocked it)")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("np:"))
async def nophone_buttons(cb: CallbackQuery):
    if not ADMIN_ID or cb.from_user.id != ADMIN_ID:
        await cb.answer("Not allowed", show_alert=True)
        return
    action = (cb.data or "").split(":", 1)[1]
    if action != "go":
        try:
            await cb.message.edit_text("❌ Cancelled.")
        except Exception:
            pass
        await cb.answer()
        return
    if nophone_sending:
        await cb.answer("Still sending. Wait for it to finish.", show_alert=True)
        return
    uids = nophone_uids()
    try:
        await cb.message.edit_text(f"📤 Sending to {len(uids)} players… I will tell you when it is done.")
    except Exception:
        pass
    await cb.answer()
    t = asyncio.create_task(run_nophone_send(cb.bot, uids, cb.from_user.id))
    bg_tasks.add(t)
    t.add_done_callback(bg_tasks.discard)


# ---------- /players: see all players and their balances (admin only) ----------
PLAYERS_PER_PAGE = 20


def players_page(page):
    uids = sorted(wallets, key=lambda u: wallets.get(u, 0) + bonus.get(u, 0),
                  reverse=True)                      # richest first
    pages = max(1, math.ceil(len(uids) / PLAYERS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    start = page * PLAYERS_PER_PAGE
    lines = []
    for i, u in enumerate(uids[start:start + PLAYERS_PER_PAGE], start + 1):
        lines.append(f"{i}. {html.escape(names.get(u, 'Player'))} · {phones.get(u, '-')}\n"
                     f"     💵 {wallets.get(u, 0)} · 🎁 {bonus.get(u, 0)}")
    text = (f"👥 <b>Players</b> ({len(uids)}) · page {page + 1}/{pages}\n"
            f"Cash total: {sum(wallets.values()):,} · "
            f"Bonus total: {sum(bonus.values()):,}\n\n" + "\n".join(lines))
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"pl:{page - 1}"))
    if page < pages - 1:
        row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"pl:{page + 1}"))
    return text, InlineKeyboardMarkup(inline_keyboard=[row] if row else [])


def admin_records_text(uid):
    """Admin-only records (e.g. bonus removed) that the player cannot see. Last 5."""
    rows = [t for t in history.get(uid, []) if t.get("adm")][:5]
    if not rows:
        return ""
    lines = []
    for t in rows:
        when = time.strftime("%d %b %H:%M", time.gmtime(t["t"] + 3 * 3600))
        lines.append(f"• {t['a']:+d} birr · {html.escape(t.get('n') or t['k'])} · {when}")
    return "\n\n🔒 Admin-only records (player can't see):\n" + "\n".join(lines)


@dp.message(Command("players"))
async def cmd_players(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    arg = (command.args or "").strip()
    if arg:                                           # /players 0912345678 = one player
        uid = find_by_phone(arg)
        if not uid:
            await m.answer("No player found with that phone number.")
            return
        t = turnover_info(uid)
        await m.answer(
            f"👤 {names.get(uid, 'Player')} ({phones.get(uid, '-')})\n"
            f"Cash: {wallets.get(uid, 0)} birr\n"
            f"Bonus: {bonus.get(uid, 0)} birr\n"
            f"Withdrawable: {withdrawable_amount(uid)} birr\n"
            f"Played: {t['played']} / {t['required']} birr\n"
            f"Deposited: {deposited.get(uid, 0)} · Won: {won.get(uid, 0)}"
            + ("\n🚫 Blocked" if uid in banned else "")
            + admin_records_text(uid))
        return
    text, kb = players_page(0)
    await m.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("pl:"))
async def players_buttons(cb: CallbackQuery):
    if not ADMIN_ID or cb.from_user.id != ADMIN_ID:
        await cb.answer("Not allowed", show_alert=True)
        return
    try:
        page = int((cb.data or "").split(":", 1)[1])
    except ValueError:
        page = 0
    text, kb = players_page(page)
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await cb.answer()


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
        deposited[uid] = deposited.get(uid, 0) + amount
        required[uid] = required.get(uid, 0) + amount       # admin-added money must be played 1x too
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


@dp.message(Command("addbonus"))
async def cmd_addbonus(m: Message, command: CommandObject):
    """Admin: give (or remove) PLAY-ONLY bonus. It can be bet but never withdrawn."""
    if not is_admin(m):
        return
    args = (command.args or "").split()
    if len(args) != 2 or not re.fullmatch(r"-?\d+", args[1]) or int(args[1]) == 0:
        await m.answer("Send:\n/addbonus <phone> <amount>\n\n"
                       "Give 20 birr bonus:\n/addbonus 0912345678 20\n"
                       "Remove 10 birr bonus:\n/addbonus 0912345678 -10")
        return
    uid, amount = find_by_phone(args[0]), int(args[1])
    if not uid:
        await m.answer("No player found with that phone number.")
        return
    if bonus.get(uid, 0) + amount < 0:
        await m.answer(f"{names.get(uid, 'Player')} only has {bonus.get(uid, 0)} birr bonus.")
        return
    bonus[uid] = bonus.get(uid, 0) + amount
    if amount > 0:
        log(uid, "bonus", amount, "Bonus added by admin")
        note = (f"🎁 {amount} ብር ቦነስ ተጨምሯል (ለመጫወት ብቻ)።\n"
                f"{amount} birr bonus was added (play only). "
                "Anything you win with it is withdrawable.")
    else:
        log(uid, "bonus", amount, "Bonus removed by admin", hidden=not BONUS_REMOVE_NOTIFY)
        note = f"🎁 {-amount} birr bonus was removed from your account."
    notify = BONUS_ADD_NOTIFY if amount > 0 else BONUS_REMOVE_NOTIFY
    if notify:
        try:
            await m.bot.send_message(uid, note)
        except Exception:
            pass
    await m.answer(f"✅ Done.\nPlayer: {names.get(uid, 'Player')} ({phones.get(uid)})\n"
                   f"Change: {amount:+d} birr bonus\nBonus now: {bonus[uid]} birr"
                   + ("" if notify else "\n🔕 Player was not notified."))


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


# ---------- /resetfree: remove free test money from players who never deposited ----------
def free_money_players():
    """Players who have a balance but never made a real deposit (free test money)."""
    out = []
    for uid, bal in wallets.items():
        if uid == ADMIN_ID or bal <= 0:
            continue
        if deposited.get(uid, 0) > 0:
            continue                      # deposited after the update: keep
        if any(t.get("k") == "deposit" for t in history.get(uid, [])):
            continue                      # has a deposit in history: keep
        out.append(uid)
    return out


@dp.message(Command("resetfree"))
async def cmd_resetfree(m: Message):
    if not is_admin(m):
        return
    uids = free_money_players()
    if not uids:
        await m.answer("No players with free balance found.")
        return
    rf_pending[m.from_user.id] = uids
    total = sum(wallets[u] for u in uids)
    lines = [f"{names.get(u, 'Player')} ({phones.get(u, '-')}): {wallets[u]}"
             for u in uids[:25]]
    more = f"\n…and {len(uids) - 25} more" if len(uids) > 25 else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Reset {len(uids)} players", callback_data="rf:go"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="rf:cancel")]])
    await m.answer(f"⚠️ These players have balance but NO deposit in their history.\n"
                   f"Total: {total} birr\n\n" + "\n".join(lines) + more +
                   "\n\nCheck the list, then confirm.", reply_markup=kb)


@dp.callback_query(F.data.startswith("rf:"))
async def resetfree_buttons(cb: CallbackQuery):
    if not ADMIN_ID or cb.from_user.id != ADMIN_ID:
        await cb.answer("Not allowed", show_alert=True)
        return
    action = (cb.data or "").split(":", 1)[1]
    uids = rf_pending.pop(cb.from_user.id, None)
    if action != "go" or not uids:
        try:
            await cb.message.edit_text("❌ Cancelled.")
        except Exception:
            pass
        await cb.answer()
        return
    n = total = 0
    for u in uids:
        amt = wallets.get(u, 0)
        if amt <= 0:
            continue
        wallets[u] = 0
        log(u, "withdraw", -amt, "Free test balance removed")
        n += 1
        total += amt
    try:
        await cb.message.edit_text(f"✅ Done. Reset {n} players ({total} birr removed).")
    except Exception:
        pass
    await cb.answer()


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
        await m.answer(f"ዝቅተኛው የማስገቢያ መጠን {MIN_DEPOSIT} ብር ነው።\nMinimum deposit is {MIN_DEPOSIT} birr.")
        return
    tokens = sms_tokens(text)
    if any(("tok:" + t) in used_sms for t in tokens):
        await m.answer("ይህ ክፍያ ቀደም ብሎ ተመዝግቧል። / This payment was already credited.")
        return
    key = hashlib.sha256(re.sub(r"\s+", " ", text).encode()).hexdigest()
    if key in used_sms:
        await m.answer("ይህ መልዕክት ቀደም ብሎ ተልኳል። / This message was already sent.")
        return

    # tell the player right away that the request was received
    await m.answer("Deposit request received. Your top-up will be done in a minute.\n"
                   "ጥያቄዎ ደርሶናል። በአንድ ደቂቃ ውስጥ ገንዘቡ ይገባል።")

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
    if pending_deposits(uid) >= MAX_PENDING_DEPOSITS:
        await m.answer(f"አስቀድመው {MAX_PENDING_DEPOSITS} ጥያቄዎች በመጠባበቅ ላይ ናቸው።\n"
                       f"You already have {MAX_PENDING_DEPOSITS} deposit requests waiting. Please wait.")
        return
    used_sms.add(key)
    save_sms(key)
    req_counter[0] += 1
    rid = req_counter[0]
    pending[rid] = {"type": "deposit", "uid": uid, "amount": amount,
                    "tokens": sorted(tokens), "key": key}
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


# ---------- /bonusmany: give play-only bonus to many players at once ----------
# CHANGE THE WORDS HERE.
# {amount} = bonus given, {balance} = the player's balance after the bonus
BONUS_TEXT = (
    "💖 ውድ ደንበኞቻችን፣\n\n"
    "🌼 Rodas Friend Zone Bingo {amount} ብር ቦነስ ሰጥቶዎታል (ለመጫወት ብቻ)።\n\n"
    "🍀 መልካም ዕድል እንመኝልዎታለን! 🌼\n\n"
    "💖 Dear valued customer,\n\n"
    "🌼 Rodas Friend Zone Bingo has gifted you a {amount} birr bonus (play only).\n\n"
    "🍀 We wish you the best of luck! 🌼\n\n"
    "💰 Your wallet balance is now: {balance:.2f}")

bn_pending = {}                 # admin's bonus list waiting for confirmation


@dp.message(Command("bonusmany"))
async def cmd_bonusmany(m: Message, command: CommandObject):
    """/bonusmany 20 all            -> everybody
       /bonusmany 20 0912.. 0911..  -> only these phone numbers"""
    if not is_admin(m):
        return
    args = (command.args or "").split()
    if len(args) < 2 or not args[0].isdigit() or int(args[0]) <= 0:
        await m.answer("Send:\n/bonusmany <amount> all\n"
                       "or\n/bonusmany <amount> <phone> <phone> ...\n\n"
                       "Examples:\n/bonusmany 20 all\n"
                       "/bonusmany 20 0912345678 0911111111")
        return
    amount = int(args[0])
    uids, missing = [], []
    if args[1].lower() == "all":
        uids = [u for u in wallets if u not in banned and u in phones]
    else:
        for a in args[1:]:
            u = find_by_phone(a)
            if not u:
                missing.append(a)
            elif u not in uids:
                uids.append(u)
    if not uids:
        await m.answer("No players found.")
        return
    bn_pending[m.from_user.id] = (amount, uids)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Give to {len(uids)} players", callback_data="bn:go"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="bn:cancel")]])
    miss = ("\n\n⚠️ Not found: " + ", ".join(missing)) if missing else ""
    await m.answer(f"🎁 Bonus: {amount} birr each\n"
                   f"Players: {len(uids)}\n"
                   f"Total: {amount * len(uids):,} birr{miss}\n\n"
                   "Press the green button to send.", reply_markup=kb)


async def run_bonus_notify(bot, uids, amount, admin_id):
    ok = fail = 0
    for u in uids:
        try:
            await bot.send_message(
                u, BONUS_TEXT.format(amount=amount, balance=play_balance(u)))
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.06)              # stay under Telegram's speed limit
    try:
        await bot.send_message(admin_id, f"🎁 Bonus messages finished.\n✅ Sent: {ok}\n❌ Failed: {fail}")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("bn:"))
async def bonusmany_buttons(cb: CallbackQuery):
    if not ADMIN_ID or cb.from_user.id != ADMIN_ID:
        await cb.answer("Not allowed", show_alert=True)
        return
    action = (cb.data or "").split(":", 1)[1]
    data = bn_pending.pop(cb.from_user.id, None)
    if action != "go" or not data:
        try:
            await cb.message.edit_text("❌ Cancelled." if action != "go"
                                       else "Nothing to send.")
        except Exception:
            pass
        await cb.answer()
        return
    amount, uids = data
    for u in uids:                             # money first, then the messages
        bonus[u] = bonus.get(u, 0) + amount
        log(u, "bonus", amount, "Bonus from admin")
    try:
        await cb.message.edit_text(f"✅ Gave {amount} birr bonus to {len(uids)} players.\n"
                                   + ("📤 Sending messages…" if BONUS_ADD_NOTIFY
                                      else "🔕 Players were not notified."))
    except Exception:
        pass
    await cb.answer()
    if BONUS_ADD_NOTIFY:
        t = asyncio.create_task(run_bonus_notify(cb.bot, uids, amount, cb.from_user.id))
        bg_tasks.add(t)
        t.add_done_callback(bg_tasks.discard)


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
            if req.get("key"):
                free_sms(req["key"])           # the player may send the same SMS again
            note = f"❌ Your deposit of {amount} birr was rejected. Contact {SUPPORT}."
    else:
        if approved:
            method = req.get("method") or "—"
            acct = str(req.get("account") or "—")
            ben = names.get(uid, "Player")
            if method == "CBE":                    # CBE: "number Full Name"
                parts = acct.split(maxsplit=1)
                acct = parts[0]
                if len(parts) > 1:
                    ben = parts[1]
            note = ("✅ Withdrawal Approved\n"
                    f"Request ID: #{rid}\n\n"
                    f"Amount: {amount:.2f}\n"
                    f"Provider: {method.upper()}\n"
                    f"Account: {acct}\n"
                    f"Beneficiary: {ben}\n"
                    "Status: Approved\n\n"
                    "ገንዘቡ ተልኳል። / Your money has been sent.")
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


# ---------- menu cleanup helpers ----------
async def set_player_menus(bot, commands):
    """Set the full player menu directly on every known player's chat.
    A chat-level list beats every older default / language list, so players
    always see the current menu. Runs in the background on every start."""
    ok = fail = 0
    for uid in list(wallets):
        if uid == ADMIN_ID:
            continue
        try:
            await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=uid))
            ok += 1
        except Exception:
            fail += 1                      # e.g. player never opened the chat / blocked the bot
        await asyncio.sleep(0.05)
    print(f"player menus set: {ok} ok, {fail} failed")


# ---------- main ----------
async def main():
    global pool, lock_conn, ready, _q, BOT
    bot = Bot(TOKEN)
    BOT = bot

    # POST only for the SMS hook, and bodies are limited to 64 KB
    app = web.Application(middlewares=[wait_ready], client_max_size=64 * 1024)
    app.add_routes([
        web.get("/", index),
        web.get("/api/state", api_state),
        web.get("/api/profile", api_profile),
        web.get("/api/history", api_history),
        web.get("/api/game", api_game),
        web.get("/api/card", api_card),
        web.get("/audio/{n}.mp3", api_audio),
        web.post("/api/join", api_join),
        web.post("/api/bingo", api_bingo),
        web.post("/api/leave", api_leave),
        web.post("/api/sms-hook", api_sms_hook),
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
    asyncio.create_task(pregen_audio())

    await bot.delete_webhook(drop_pending_updates=True)

    # branding on the bot's profile page
    try:
        await bot.set_my_description(BOT_DESCRIPTION)
        await bot.set_my_short_description(BOT_SHORT)
    except Exception as e:
        print("could not set bot description:", repr(e))

    # note: /sound is no longer a command; the Sound ON/OFF button is in the start menu
    public_commands = [
        BotCommand(command="start", description="Start the bot"),
        BotCommand(command="play", description="🎮 Play Bingo"),
        BotCommand(command="winning_patterns", description="🏆 Winning Patterns"),
        BotCommand(command="instructions", description="📝 Game Instructions"),
        BotCommand(command="balance", description="💰 Check Balance"),
        BotCommand(command="deposit", description="💵 Deposit"),
        BotCommand(command="withdraw", description="💸 Withdraw"),
        BotCommand(command="transactions", description="📜 My Transactions"),
        BotCommand(command="invite", description="👥 Invite Friends"),
        BotCommand(command="support", description="📞 Support"),
    ]

    # Telegram keeps SEPARATE command lists per scope and per language, and a more
    # specific old list wins over the default. Clear all of them, then set the new one.
    for scope in (BotCommandScopeDefault(), BotCommandScopeAllPrivateChats()):
        for lang in (None, "en", "am", "om", "ti"):
            try:
                await bot.delete_my_commands(scope=scope, language_code=lang)
            except Exception:
                pass
    try:
        await bot.set_my_commands(public_commands, scope=BotCommandScopeDefault())
        await bot.set_my_commands(public_commands, scope=BotCommandScopeAllPrivateChats())
    except Exception as e:
        print("could not set player menu:", repr(e))

    t = asyncio.create_task(set_player_menus(bot, public_commands))
    bg_tasks.add(t)
    t.add_done_callback(bg_tasks.discard)
    try:                                          # log what Telegram really stored
        stored = await bot.get_my_commands(scope=BotCommandScopeDefault())
        print("default menu has", len(stored), "commands:", [c.command for c in stored])
    except Exception as e:
        print("could not read menu:", repr(e))

    if ADMIN_ID:                                  # extra menu only you can see
        try:
            await bot.set_my_commands(
                public_commands + [
                    BotCommand(command="admin", description="🛠 Admin commands"),
                    BotCommand(command="stats", description="📊 Stats"),
                    BotCommand(command="players", description="👥 All player balances"),
                    BotCommand(command="online", description="🟢 Players online"),
                    BotCommand(command="backup", description="💾 Download backup"),
                    BotCommand(command="post", description="📣 Send post to players"),
                    BotCommand(command="nophone", description="📱 Ask for phone numbers"),
                    BotCommand(command="addbalance", description="➕ Add / remove balance"),
                    BotCommand(command="addbonus", description="🎁 Add / remove play-only bonus"),
                    BotCommand(command="bonusmany", description="🎁 Bonus for many players"),
                    BotCommand(command="resetfree", description="🧹 Remove free balances"),
                    BotCommand(command="ban", description="🚫 Block a player"),
                    BotCommand(command="unban", description="✅ Unblock a player"),
                    BotCommand(command="broadcast", description="📢 Message all players"),
                    BotCommand(command="lastsms", description="📩 Last forwarded SMS"),
                ],
                scope=BotCommandScopeChat(chat_id=ADMIN_ID))
        except Exception as e:
            print("could not set admin menu:", repr(e))
    await dp.start_polling(bot)


asyncio.run(main())
