import asyncio, hashlib, hmac, html, json, math, os, random, re, secrets, string, time
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
                           ReplyKeyboardRemove, WebAppInfo, FSInputFile)

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
LOBBY_SECONDS = 30         # countdown after the FIRST player picks a cartela
START_BALANCE = 0          # new players start with 0: balance comes only from deposits
CARD_COUNT = 100
MIN_PLAYERS = 1            # a round needs at least 1 player (testing)
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
             "winners": [], "wlist": [], "share": 0, "dq": set()}
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
            elif r["k"].startswith("snd:") and r["v"] == 1:
                sound_on.add(int(r["k"][4:]))
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


def log(uid, kind, amount, note=""):
    """Save a transaction for the profile page (keeps the last 50)."""
    h = history.setdefault(uid, [])
    h.insert(0, {"t": int(time.time()), "k": kind, "a": amount, "n": note})
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


def award_winners(g):
    """Finish the round and pay the winner(s).
    Rule: only a pattern completed by the LATEST called number can win.
    Every (not banned) player whose card completed a pattern on this same
    call shares the prize equally. A pattern completed on an earlier call
    has 'passed' and can no longer win.
    Prizes always go to the cash wallet (withdrawable), even if the stake was bonus."""
    hits = {}
    for u, no in g["players"].items():
        if u in g["dq"]:
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


# ---------- AMHARIC NUMBER SOUND (each player chooses, button in the start menu) ----------
sound_on = set()          # players who turned sound ON (saved in the database)
audio_ids = {}            # number -> Telegram file_id (fast resend)
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
        path, headers={"Cache-Control": "public, max-age=86400"})


async def pregen_audio():
    for n in range(1, 76):
        try:
            await make_audio(n)
        except Exception as e:
            print("pregen failed:", repr(e))
            await asyncio.sleep(5)


async def send_call_audio(n, uids):
    targets = [u for u in uids if u in sound_on]
    if not targets:
        return
    try:
        src = audio_ids.get(n) or FSInputFile(await make_audio(n))
    except Exception as e:
        print("audio failed:", repr(e))
        return
    for uid in targets:
        try:
            msg = await BOT.send_voice(uid, src, caption=f"{call_letter(n)}{n}")
            audio_ids[n] = msg.voice.file_id
            src = audio_ids[n]
        except Exception:
            pass


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
             winners=[], wlist=[], share=0, dq=set())

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
        if (g["phase"] != "playing" or not g["players"]
                or all(u in g["dq"] for u in g["players"])):
            break
        g["called"].append(n)
        if sound_on:                       # Amharic voice for players who turned sound ON
            t = asyncio.create_task(send_call_audio(n, list(g["players"])))
            bg_tasks.add(t)
            t.add_done_callback(bg_tasks.discard)

        # The server claims BINGO right on the call for two kinds of players:
        #   - players with Auto ON in the web app (they never miss a call)
        #   - players who are AWAY (app closed)
        # Manual players must press BINGO themselves before the next number is called.
        now = time.time()
        away = {u for u in g["players"] if now - seen.get(u, 0) > IDLE_KICK}
        claimers = [u for u, no in g["players"].items()
                    if u not in g["dq"]
                    and (u in away or auto_pref.get(u))
                    and fresh_pattern(make_card(no), g["called"])]
        if claimers:
            winners = award_winners(g)
            for u in winners:
                if u in away:
                    try:
                        await BOT.send_message(
                            u, f"🏆 ካርታዎ አሸንፏል! {g['share']} ብር ወደ ሂሳብዎ ገብቷል።\n"
                               f"Your card won while you were away! "
                               f"{g['share']} birr was added to your balance.")
                    except Exception:
                        pass
            break
        await asyncio.sleep(CALL_EVERY)

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
        "players": n, "bet": g["bet"], "derash": int(n * g["bet"] * 0.8),
        "wallet": play_balance(uid), "bonus": bonus.get(uid, 0),
        "cash": wallets[uid], "name": names[uid],
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
    if g["deadline"] is None:                  # first player starts the clock
        g["deadline"] = time.time() + LOBBY_SECONDS
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
        award_winners(g)
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
