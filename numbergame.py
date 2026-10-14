"""
Rodas Friend Zone NUMBER GAME  -  STAGE 1 + STAGE 2
Stage 1: database tables, number purchase, wallet charge, 5-number limit.
Stage 2: freeze, secure draw (hash published first), 60-second countdown,
         timed reveal, prize settlement (paid exactly once), results, new round.

Stage 3 (server side): a game number that counts separately inside each lobby
         ("50 birr Game 7"), the anonymous start poll, the admin START button,
         the "game starts in 60 seconds" message, and saved past games that can
         be read back from the database by their game number.
         Poll rules: the FIRST tap of a player is final; after voting the player
         sees the Yes / No percentages, which refresh live; the admin buttons are
         tied to one specific game so an old button can never start a newer one.

Stage 4 (rooms): four rooms with fixed prizes (see ROOMS). The two 50-number rooms start
         ONLY when all 50 numbers are sold. The two 100-number rooms may also be started early
         by the admin (MIN_EARLY_START numbers or more). Players get only the
         "game starts in 60 seconds" Telegram message; winners see results in the app.
Keep NUMBER_GAME_ENABLED off (default) until the legal check is finished.

How a round ends (all times come from saved timestamps, not from a browser):
  open -> FREEZE (100 sold, or admin early start with 50+ sold)
       -> the 3 winners are drawn NOW with a secure random generator and saved;
          their hash is published; no more purchases are possible
       -> countdown 60 s -> animation window (winners are revealed one by one)
       -> SETTLE: the 3 prizes are credited once, results saved, new round opens

Design rules (why money is safe):
  * buy_number() is a normal function with NO "await" inside. In asyncio, code
    without await cannot be interrupted, so "check the number is free, check the
    balance, take the money, save the owner" happens as ONE step. Two players
    can never get the same number, and nobody is charged twice.
  * The wallet change and the number row are put in the database queue in the
    same step, so they are saved together in one transaction.
  * The database also protects itself: PRIMARY KEY (round_id, num) = one owner
    per number, UNIQUE (round_id, uid, slot) with slot 1..5 = at most 5 numbers
    per player per round, UNIQUE (idem) = the same purchase request is never
    saved twice.
  * This file imports nothing from bot.py. bot.py gives it what it needs
    through init(). The Telegram / web libraries are imported only where used.
"""
import asyncio
import hashlib
import json
import os
import random
import re
import secrets
import string
import time
import uuid
from collections import OrderedDict

ENABLED = os.getenv("NUMBER_GAME_ENABLED", "0") == "1"   # off = only the admin can use it

NUMBERS = 100            # the biggest room has 100 numbers (the database allows 1..100)
MAX_PER_PLAYER = 5       # per player, per room, per round
# A room key is what the code, the database and the web screen call "lobby".
# 50 and 100 are the original 100-number rooms (their saved games keep working).
#   price   = birr per number          size = how many numbers the room has
#   early   = the admin may start it before it is full
#   prizes  = gold, silver, bronze when ALL numbers are sold (an early start scales them down)
ROOMS = {
    50:    {"price": 50,  "size": 100, "early": True,  "prizes": (3000, 1000, 500)},
    100:   {"price": 100, "size": 100, "early": True,  "prizes": (7000, 1000, 500)},
    5050:  {"price": 50,  "size": 50,  "early": False, "prizes": (1500, 500)},   # gold + silver only (no bronze)
    10050: {"price": 100, "size": 50,  "early": False, "prizes": (3000, 700, 300)},
}
PRICES = tuple(ROOMS)    # the list of room keys
NAME_MAX = 20            # longest name a player can give a number
IDEM_KEEP = 20000        # how many recent purchase keys are remembered in memory

# ---- draw, timing and prizes (stage 2) ----
MIN_EARLY_START = 3      # TESTING value. An early start needs at least this many numbers sold
                         # (3 is the lowest possible). Change to 50 later in this one place.
MIN_DRAW = 3             # three different winners are needed
COUNTDOWN_SECONDS = 60   # visible countdown after the round is frozen
DRAW_INTRO = 8           # seconds of "machine mixing" before the first ball
REVEAL_GAP = 12          # seconds between the three reveals
REVEAL_ORDER = (0, 1, 2) # 0 gold, 1 silver, 2 bronze. (2, 1, 0) would reveal bronze first
PLACES = ("gold", "silver", "bronze")
# Prizes are the fixed amounts in ROOMS when the room is full. With fewer numbers sold
# (early start) every prize shrinks in the same proportion, rounded down to whole birr.
HISTORY_KEEP = 50
RESULTS_PAGE = 20        # past games sent to the screen at once (max 50)
POLL_REFRESH = 5         # seconds between live updates of the players' poll messages

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS ng_rounds(
    id BIGINT PRIMARY KEY,
    lobby INT NOT NULL,
    status TEXT NOT NULL,
    created BIGINT NOT NULL,
    full_at BIGINT,
    close_at BIGINT,
    settled_at BIGINT,
    early INT NOT NULL DEFAULT 0,
    test INT NOT NULL DEFAULT 0,
    game_no INT,
    poll_at BIGINT,
    data JSONB NOT NULL DEFAULT '{{}}'::jsonb);
ALTER TABLE ng_rounds ADD COLUMN IF NOT EXISTS close_at BIGINT;
ALTER TABLE ng_rounds ADD COLUMN IF NOT EXISTS settled_at BIGINT;
ALTER TABLE ng_rounds ADD COLUMN IF NOT EXISTS early INT NOT NULL DEFAULT 0;
ALTER TABLE ng_rounds ADD COLUMN IF NOT EXISTS test INT NOT NULL DEFAULT 0;
ALTER TABLE ng_rounds ADD COLUMN IF NOT EXISTS game_no INT;
ALTER TABLE ng_rounds ADD COLUMN IF NOT EXISTS poll_at BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS ng_rounds_game ON ng_rounds(lobby, game_no) WHERE game_no IS NOT NULL;
CREATE TABLE IF NOT EXISTS ng_numbers(
    round_id BIGINT NOT NULL,
    lobby INT NOT NULL,
    num INT NOT NULL CHECK (num BETWEEN 1 AND {NUMBERS}),
    uid BIGINT NOT NULL,
    name TEXT NOT NULL,
    price INT NOT NULL,
    used INT NOT NULL DEFAULT 0,
    slot INT NOT NULL CHECK (slot BETWEEN 1 AND {MAX_PER_PLAYER}),
    idem TEXT NOT NULL,
    t BIGINT NOT NULL,
    PRIMARY KEY (round_id, num),
    UNIQUE (round_id, uid, slot),
    UNIQUE (idem));
CREATE INDEX IF NOT EXISTS ng_numbers_uid ON ng_numbers(uid);
CREATE TABLE IF NOT EXISTS ng_prizes(
    round_id BIGINT NOT NULL,
    place INT NOT NULL CHECK (place BETWEEN 1 AND 3),
    num INT NOT NULL,
    uid BIGINT NOT NULL,
    amount INT NOT NULL,
    status TEXT NOT NULL,
    notified INT NOT NULL DEFAULT 0,
    t BIGINT NOT NULL,
    PRIMARY KEY (round_id, place));
CREATE TABLE IF NOT EXISTS ng_votes(
    round_id BIGINT NOT NULL,
    uid BIGINT NOT NULL,
    vote INT NOT NULL CHECK (vote IN (0, 1)),
    t BIGINT NOT NULL,
    PRIMARY KEY (round_id, uid));
CREATE TABLE IF NOT EXISTS ng_audit(
    id BIGSERIAL PRIMARY KEY,
    t BIGINT NOT NULL,
    kind TEXT NOT NULL,
    uid BIGINT,
    lobby INT,
    round_id BIGINT,
    data JSONB NOT NULL DEFAULT '{{}}'::jsonb);
"""

# ---- things bot.py gives us (see init) ----
D = {}

# ---- working copy in memory (the database is the permanent copy) ----
rounds = {}                    # lobby price -> current round
history = {p: [] for p in PRICES}   # lobby price -> finished rounds, newest first
to_save = []                   # settled rounds waiting until their database writes are done
_rng = random.SystemRandom()   # secure randomness (operating system)
idem_seen = OrderedDict()      # "uid:key" -> {"lobby", "num", "result"}
ng_counter = [0]               # last round id used
game_counter = {p: 0 for p in PRICES}   # lobby price -> last REAL game number used (test rounds never use one)
start_notices = []             # frozen rounds whose players still get "game starts in 60 seconds"
_tasks = set()                 # background tasks (keeps them alive until they finish)
_refreshing = [False]          # True while the poll messages are being updated


def price_of(lobby):
    return ROOMS[lobby]["price"]


def size_of(lobby):
    return ROOMS[lobby]["size"]


def early_ok(lobby):
    return bool(ROOMS[lobby]["early"])


def room_name(lobby):
    """'በ 50ብር / 100 ቁጥሮች' : price and how many numbers the room has."""
    return f"በ {price_of(lobby)}ብር / {size_of(lobby)} ቁጥሮች"


def init(**deps):
    """bot.py calls this once. Needed: wallets, wagered, turnover_left, log,
    enqueue, save_counter, verify, touch, phones, banned, admin_id, ensure."""
    D.update(deps)


# ---------- small helpers ----------
def _int(v):
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        return None
    try:
        return int(v)
    except ValueError:
        return None


def clean_name(raw):
    """The name a player gives a number: 1-20 characters, no control characters.
    HTML characters are allowed here; every screen must escape them when showing."""
    if not isinstance(raw, str):
        return None
    s = re.sub(r"\s+", " ", raw)
    s = "".join(ch for ch in s if ch.isprintable()).strip()
    return s if 1 <= len(s) <= NAME_MAX else None


def allowed(uid):
    """Who may use the Number Game right now."""
    return bool(ENABLED or (D.get("admin_id") and uid == D["admin_id"]))


def audit(kind, uid, lobby, rid, data):
    D["enqueue"](
        "INSERT INTO ng_audit(t, kind, uid, lobby, round_id, data) "
        "VALUES($1, $2, $3, $4, $5, $6::jsonb)",
        int(time.time()), kind, uid, lobby, rid, json.dumps(data))


# ---------- rounds ----------
def _new_round(lobby, rid, created, status="open", full_at=None):
    return {"id": rid, "lobby": lobby, "status": status, "created": created,
            "full_at": full_at,
            "close_at": None,  # when the countdown ends (saved timestamp)
            "early": False,    # True = started early by the admin
            "test": False,     # True = admin test round with fake players, no real money
            "draw": None,      # the saved draw (winners, hash, prizes) once frozen
            "paid": set(),     # prize places already credited (0 gold, 1 silver, 2 bronze)
            "settled_at": None, "saved": False, "save_mark": 0,
            "game_no": None,   # "Game 7" of this lobby (None for test rounds)
            "poll_at": None,   # when the admin opened the start poll (None = no poll)
            "votes": {},       # uid -> 1 (yes) / 0 (no). Only the totals are ever shown.
            "poll_msgs": {},   # uid -> Telegram message id of that player's poll message (memory only)
            "poll_dirty": False, "poll_synced": 0.0,   # live-update bookkeeping (memory only)
            "owners": {},      # number -> {"uid", "name", "t", "slot", "used"}
            "by_uid": {}}      # uid -> [numbers]


def start_round(lobby):
    ng_counter[0] += 1
    r = _new_round(lobby, ng_counter[0], int(time.time()))
    r["game_no"] = game_counter[lobby] + 1      # shown to players; becomes final when the round freezes
    rounds[lobby] = r
    D["enqueue"]("INSERT INTO ng_rounds(id, lobby, status, created, game_no) VALUES($1, $2, $3, $4, $5)",
                 r["id"], lobby, "open", r["created"], r["game_no"])
    D["save_counter"]("ng_round_counter", ng_counter[0])
    audit("round_open", None, lobby, r["id"], {})
    return r


def ensure_rounds():
    for p in PRICES:
        if p not in rounds:
            start_round(p)


# ---------- the purchase ----------
def buy_number(uid, lobby, num, name, idem):
    """Buy one number. Returns {"ok": True, ...} or {"ok": False, "code", "error"}.
    IMPORTANT: no "await" may ever be added inside this function (see top of file)."""
    r = rounds.get(lobby) if isinstance(lobby, int) and not isinstance(lobby, bool) else None
    if r is None:
        return {"ok": False, "code": "lobby", "error": "Unknown lobby"}

    def fail(code, msg):
        audit("buy_fail", uid, lobby, r["id"], {"code": code, "num": str(num)[:20]})
        return {"ok": False, "code": code, "error": msg}

    n = _int(num)
    if n is None or not 1 <= n <= size_of(lobby):
        return fail("num", f"Choose a number from 1 to {size_of(lobby)}")
    nm = clean_name(name)
    if nm is None:
        return fail("name", f"Give the number a name (1 to {NAME_MAX} characters)")
    if not isinstance(idem, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", idem):
        return fail("idem", "Bad request key")

    # the same request sent again (retry / double tap): same answer, no new charge
    key = f"{uid}:{idem}"
    prev = idem_seen.get(key)
    if prev is not None:
        if prev["lobby"] == lobby and prev["num"] == n:
            res = dict(prev["result"])
            res["replay"] = True
            res["balance"] = D["wallets"].get(uid, 0)
            return res
        return fail("idem", "This request key was already used for another purchase")

    if r["status"] != "open":
        return fail("closed", "This round is closed")
    if r["test"]:
        return fail("test", "This round is an admin test round")
    if n in r["owners"]:
        return fail("taken", "Sorry, this number was just taken")
    mine = r["by_uid"].get(uid, [])
    if len(mine) >= MAX_PER_PLAYER:
        return fail("limit", f"You can own at most {MAX_PER_PLAYER} numbers in this lobby")
    price = price_of(lobby)
    wallets = D["wallets"]
    if wallets.get(uid, 0) < price:
        return fail("funds", "Not enough balance")

    # ---- everything is checked: take the money and save the owner (one step) ----
    taken_slots = {r["owners"][x]["slot"] for x in mine}
    slot = min(s for s in range(1, MAX_PER_PLAYER + 1) if s not in taken_slots)
    used = min(price, D["turnover_left"](uid))      # counts toward the play-through rule
    wallets[uid] = wallets.get(uid, 0) - price
    D["wagered"][uid] = D["wagered"].get(uid, 0) + used
    now = int(time.time())
    D["log"](uid, "bet", -price, f"Number Game {room_name(lobby)} · #{n} ({nm})")   # saves the wallet
    r["owners"][n] = {"uid": uid, "name": nm, "t": now, "slot": slot, "used": used}
    r["by_uid"].setdefault(uid, []).append(n)
    D["enqueue"](
        "INSERT INTO ng_numbers(round_id, lobby, num, uid, name, price, used, slot, idem, t) "
        "VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        r["id"], lobby, n, uid, nm, price, used, slot, key, now)
    full = len(r["owners"]) >= size_of(lobby)
    if full:                                       # 100 sold: freeze + draw in the SAME step
        r["full_at"] = now
        audit("round_full", None, lobby, r["id"], {"sold": len(r["owners"])})
        freeze_round(lobby)
    result = {"ok": True, "num": n, "name": nm, "price": price,
              "balance": wallets[uid], "mine": len(r["by_uid"][uid]),
              "round": r["id"], "full": full}
    idem_seen[key] = {"lobby": lobby, "num": n, "result": result}
    while len(idem_seen) > IDEM_KEEP:
        idem_seen.popitem(last=False)
    return result


# ---------- stage 2: draw, timing, prizes ----------
def prizes_for(lobby, sold):
    """Gold, silver, bronze in whole birr. A full room pays exactly the amounts in ROOMS;
    fewer numbers sold shrink them in proportion (rounded down)."""
    size = size_of(lobby)
    return [p * sold // size for p in ROOMS[lobby]["prizes"]]


def draw_winners(sold_numbers, k=3):
    """k different winners (one per prize place) from the sold numbers, secure randomness,
    no replacement. Who owns a number, its name and the buying time play NO part in this."""
    return _rng.sample(sorted(sold_numbers), k)


def places_of(d):
    """How many prize places a saved draw has (3 normally, 2 in the 50-number 50 birr room)."""
    return len(d["prizes"])


def prize_line(lobby, sold):
    """Prize text for the Telegram messages: only the places this room really pays."""
    return " · ".join(f"{('🥇', '🥈', '🥉')[i]} {p}" for i, p in enumerate(prizes_for(lobby, sold))) + " ETB"


def _verify_text(secret, rid, lobby, sold, winners):
    return f"{secret}:{rid}:{lobby}:{','.join(map(str, sold))}:{','.join(map(str, winners))}"


def reveal_time(r, idx):
    """When place idx (0 gold, 1 silver, 2 bronze) becomes visible to everybody."""
    return r["close_at"] + DRAW_INTRO + REVEAL_ORDER.index(idx) * REVEAL_GAP


def settle_time(r):
    n = places_of(r["draw"]) if r.get("draw") else 3
    return r["close_at"] + DRAW_INTRO + n * REVEAL_GAP


def freeze_round(lobby, early=False, min_sold=None):
    """Stop sales, draw the 3 winners, save them, publish the hash, start the countdown.
    No await inside: it is one uninterruptible step. A frozen round can never be drawn again."""
    r = rounds.get(lobby)
    if r is None or r["status"] != "open":
        return {"ok": False, "error": "This round is not open"}
    sold = sorted(r["owners"])
    n = len(sold)
    size = size_of(lobby)
    if early:
        if not early_ok(lobby):
            return {"ok": False, "error": f"This room starts only when all {size} numbers are sold ({n}/{size})"}
        need = max(MIN_EARLY_START if min_sold is None else min_sold, MIN_DRAW)
        if n < need:
            return {"ok": False, "error": f"At least {need} numbers must be sold (now {n})"}
    elif n < size:
        return {"ok": False, "error": f"The round is not full ({n}/{size})"}
    now = int(time.time())
    if not r["test"]:                      # a real game: its number is final from now on
        if r["game_no"] is None:
            r["game_no"] = game_counter[lobby] + 1
        game_counter[lobby] = max(game_counter[lobby], r["game_no"])
    winners = draw_winners(sold, len(ROOMS[lobby]["prizes"]))
    secret = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
    verify = _verify_text(secret, r["id"], lobby, sold, winners)
    prizes = prizes_for(lobby, n)
    r["draw"] = {
        "secret": secret, "verify": verify,
        "hash": hashlib.sha256(verify.encode()).hexdigest(),
        "winners": winners, "sold": n, "prizes": prizes, "frozen_at": now,
        "winners_info": [{"num": w, "name": r["owners"][w]["name"], "uid": r["owners"][w]["uid"]}
                         for w in winners],
        "poll": poll_tally(r) if r["poll_at"] is not None else None}
    r["status"] = "countdown"
    r["early"] = bool(early)
    r["close_at"] = now + COUNTDOWN_SECONDS
    D["enqueue"]("UPDATE ng_rounds SET status = 'countdown', close_at = $1, early = $2, "
                 "data = $3::jsonb, game_no = $4 WHERE id = $5",
                 r["close_at"], 1 if early else 0, json.dumps(r["draw"]), r["game_no"], r["id"])
    audit("round_frozen", None, lobby, r["id"],
          {"sold": n, "early": bool(early), "hash": r["draw"]["hash"], "prizes": prizes,
           "game": r["game_no"]})
    if not r["test"]:
        start_notices.append(r)            # the players are told "starts in 60 seconds" (see ticker)
    return {"ok": True, "round": r["id"], "sold": n, "close_at": r["close_at"]}


def settle_round(lobby, now=None):
    """After the animation window: credit the 3 prizes ONCE, save, open a new round.
    Safe to call again and again: places already paid are skipped. No await inside."""
    r = rounds.get(lobby)
    if not r or r["status"] != "countdown" or not r["draw"]:
        return None
    now = time.time() if now is None else now
    if now < settle_time(r):
        return None
    d = r["draw"]
    t = int(now)
    for idx in range(places_of(d)):
        if idx in r["paid"]:
            continue
        info = d["winners_info"][idx]
        uid, amount = info["uid"], d["prizes"][idx]
        real = uid > 0 and not r["test"]
        if real:
            D["wallets"][uid] = D["wallets"].get(uid, 0) + amount
            D["log"](uid, "win", amount, f"Number Game {room_name(lobby)} · {PLACES[idx]} #{info['num']}")
        D["enqueue"](
            "INSERT INTO ng_prizes(round_id, place, num, uid, amount, status, notified, t) "
            "VALUES($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT (round_id, place) DO NOTHING",
            r["id"], idx + 1, info["num"], uid, amount, "paid" if real else "test",
            1, t)             # 1 = no Telegram message is sent for prizes (the app shows them)
        r["paid"].add(idx)
    r["status"] = "settled"
    r["settled_at"] = t
    D["enqueue"]("UPDATE ng_rounds SET status = 'settled', settled_at = $1 WHERE id = $2",
                 t, r["id"])
    audit("round_settled", None, lobby, r["id"], {"prizes": d["prizes"], "winners": d["winners"]})
    history[lobby].insert(0, r)
    del history[lobby][HISTORY_KEEP:]
    r["save_mark"] = D["db_mark"]()          # "saved" once the database has written up to here
    to_save.append(r)
    start_round(lobby)                       # the lobby immediately gets a fresh round
    return r


def _pname(uid):
    if uid < 0:
        return "Test player"
    return str(D.get("names", {}).get(uid) or "Player")


def result_view(r, now=None):
    """What players may see about a frozen or finished round. Winners appear one by
    one at their reveal time, the secret only after the round is settled."""
    now = time.time() if now is None else now
    d = r["draw"]
    price = price_of(r["lobby"])
    settled = r["status"] == "settled"
    shown = [i for i in range(places_of(d)) if settled or now >= reveal_time(r, i)]
    v = {
        "round": r["id"], "game": r["game_no"], "lobby": r["lobby"], "price": price,
        "size": size_of(r["lobby"]), "name": room_name(r["lobby"]), "status": r["status"],
        "test": bool(r["test"]), "poll": d.get("poll"),
        "early": bool(r["early"]), "sold": d["sold"], "total_sales": d["sold"] * price,
        "prize_total": sum(d["prizes"]), "prizes": d["prizes"],
        "frozen_at": d["frozen_at"], "draw_at": r["close_at"], "settled_at": r["settled_at"],
        "settlement": ("paid" if r["saved"] else "settling") if settled else "pending",
        "hash": d["hash"], "server_now": int(now),
        "reveal_at": [reveal_time(r, i) for i in range(places_of(d))], "settle_at": settle_time(r),
        "winners": [{"place": PLACES[i], "num": d["winners_info"][i]["num"],
                     "name": d["winners_info"][i]["name"],
                     "prize": d["prizes"][i]}
                    for i in shown],
    }
    if now < r["close_at"]:
        v["phase"], v["seconds_left"] = "countdown", int(r["close_at"] - now + 0.999)
    elif not settled and now < settle_time(r):
        v["phase"], v["seconds_left"] = "drawing", 0
    else:
        v["phase"], v["seconds_left"] = "done", 0
    if settled:
        v["secret"], v["verify"] = d["secret"], d["verify"]
    return v


def last_result(lobby, include_test=False):
    for r in history.get(lobby, []):
        if include_test or not r["test"]:
            return r
    return None


def game_label(lobby, game_no, rid=None):
    """'በ 50ብር / 100 ቁጥሮች · Game 7'. A round without a game number (a test round) shows its round id."""
    return f"{room_name(lobby)} · Game {game_no}" if game_no else f"{room_name(lobby)} · round #{rid}"


def summary_text(r):
    d = r["draw"]
    lines = [f"✅ Number Game {game_label(r['lobby'], r['game_no'], r['id'])} settled"
             + (" (TEST, no money moved)" if r["test"] else "")
             + (" · early start" if r["early"] else ""),
             f"Sold {d['sold']}/{size_of(r['lobby'])} · sales {d['sold'] * price_of(r['lobby'])} ETB"]
    for i in range(places_of(d)):
        w = d["winners_info"][i]
        lines.append(f"{('🥇', '🥈', '🥉')[i]} #{w['num']} {w['name']} · {_pname(w['uid'])} · {d['prizes'][i]} ETB")
    lines.append(f"Hash: {d['hash']}")
    return "\n".join(lines)


# ---------- stage 2: background work ----------
async def _send(uid, text, markup=None):
    """Send a Telegram message. Returns its message id (or None if it could not be sent)."""
    if not uid:
        return None
    try:
        if markup is None:
            m = await D["send"](uid, text)
        else:
            m = await D["send"](uid, text, reply_markup=markup)
        return getattr(m, "message_id", None)
    except Exception as e:                      # blocked bot, network...: never stop the loop
        print("number game message failed:", uid, repr(e))
        return None


async def _edit(uid, mid, text, rows=None):
    """Change a message we sent earlier. Errors (message not modified, blocked...) are ignored."""
    if D.get("edit") is None or not mid:
        return
    try:
        if rows:
            await D["edit"](uid, mid, text, reply_markup=_kb(rows))
        else:
            await D["edit"](uid, mid, text)
    except Exception:
        pass


async def after_settle():
    """Mark a settled round as 'saved' once the database has really written it.
    No Telegram message is sent for results or prizes: players see them in the app."""
    done = D["db_done"]()
    for r in list(to_save):
        if done >= r["save_mark"]:
            r["saved"] = True
            to_save.remove(r)


def tick(now=None):
    """Settle every round whose animation window is over."""
    return [settle_round(p, now) for p in PRICES]


async def ticker():
    """bot.py starts this once: asyncio.create_task(ng.ticker())"""
    while True:
        await asyncio.sleep(1)
        try:
            tick()
            await after_settle()
            if start_notices:
                _spawn(send_start_notices())
            if any(r["poll_msgs"] for r in rounds.values()) and not _refreshing[0]:
                _spawn(poll_refresh())
        except Exception as e:
            print("number game tick error:", repr(e))


# ---------- stage 2: admin TEST mode (only while the game is OFF for players) ----------
def test_fill(lobby, count):
    """Fill a round with FAKE players (negative ids, no wallets, no money)."""
    r = rounds.get(lobby)
    if ENABLED:
        return {"ok": False, "error": "Test mode works only while NUMBER_GAME_ENABLED is off"}
    if r is None or r["status"] != "open":
        return {"ok": False, "error": "This round is not open"}
    if any(o["uid"] > 0 for o in r["owners"].values()):
        return {"ok": False, "error": "This round already has real purchases"}
    if not r["test"]:
        r["test"], r["game_no"] = True, None          # test rounds never take a game number
        D["enqueue"]("UPDATE ng_rounds SET test = 1, game_no = NULL WHERE id = $1", r["id"])
    now = int(time.time())
    added = 0
    for n in range(1, size_of(lobby) + 1):
        if added >= count:
            break
        if n in r["owners"]:
            continue
        uid = -n
        r["owners"][n] = {"uid": uid, "name": f"Test {n}", "t": now, "slot": 1, "used": 0}
        r["by_uid"].setdefault(uid, []).append(n)
        D["enqueue"](
            "INSERT INTO ng_numbers(round_id, lobby, num, uid, name, price, used, slot, idem, t) "
            "VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
            r["id"], lobby, n, uid, f"Test {n}", price_of(lobby), 0, 1, f"{uid}:test-{r['id']}-{n}", now)
        added += 1
    out = {"ok": True, "added": added, "sold": len(r["owners"])}
    if len(r["owners"]) >= size_of(lobby):
        freeze_round(lobby)
        out["frozen"] = True
    return out


def test_freeze(lobby):
    r = rounds.get(lobby)
    if ENABLED:
        return {"ok": False, "error": "Test mode works only while NUMBER_GAME_ENABLED is off"}
    if r is None or not r["test"]:
        return {"ok": False, "error": "Use /ngtestfill first (test rounds only)"}
    return freeze_round(lobby, early=True)


# ---------- stage 3: start poll, admin START, "starts in 60 seconds" ----------
def _spawn(coro):
    """Run something in the background and keep it alive until it is finished."""
    t = asyncio.ensure_future(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return t


def _kb(rows):
    """rows = [[(text, callback_data), ...], ...]  ->  Telegram inline keyboard."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def real_players(r):
    """Telegram ids of everybody who owns a number in this round (fake test players excluded)."""
    return sorted(u for u, nums in r["by_uid"].items() if nums and u > 0)


def poll_tally(r):
    """Only totals. Who voted what is never shown to anybody."""
    yes = sum(1 for v in r["votes"].values() if v == 1)
    no = sum(1 for v in r["votes"].values() if v == 0)
    players = len(real_players(r))
    voted = yes + no
    yes_pct = int(yes * 100 / voted + 0.5) if voted else 0
    return {"yes": yes, "no": no, "players": players, "not_voted": max(0, players - voted),
            "voted": voted, "yes_pct": yes_pct, "no_pct": (100 - yes_pct) if voted else 0}


def vote_line(r):
    """'Yes 28 (70%) / No 12 (30%)': totals and percentages only, never who voted what."""
    t = poll_tally(r)
    return (f"✅ Yes {t['yes']} ({t['yes_pct']}%) · ❌ No {t['no']} ({t['no_pct']}%)\n"
            f"{t['voted']} of {t['players']} players voted · anonymous")


def open_poll(lobby):
    """Admin: ask the players 'start the game now?'. Needs MIN_EARLY_START numbers sold.
    Opening it again does nothing new (returns already=True). No await inside."""
    r = rounds.get(lobby)
    if r is None or r["status"] != "open":
        return {"ok": False, "error": "This round is not open"}
    if r["test"]:
        return {"ok": False, "error": "This is a test round (fake players). Use /ngtestfreeze"}
    if not early_ok(lobby):
        return {"ok": False, "error": f"This room starts only when all {size_of(lobby)} numbers are sold"}
    sold = len(r["owners"])
    if sold < MIN_EARLY_START:
        return {"ok": False, "error": f"At least {MIN_EARLY_START} numbers must be sold (now {sold})"}
    if r["poll_at"] is not None:
        return {"ok": True, "already": True, "round": r}
    r["poll_at"] = int(time.time())
    D["enqueue"]("UPDATE ng_rounds SET poll_at = $1 WHERE id = $2", r["poll_at"], r["id"])
    audit("poll_open", None, lobby, r["id"], {"sold": sold})
    return {"ok": True, "already": False, "round": r}


def cast_vote(uid, rid, yes):
    """A player votes yes or no. The FIRST tap is final: a second tap changes nothing.
    Only owners of a number in this round may vote, one vote each. No await inside."""
    r = next((x for x in rounds.values() if x["id"] == rid), None)
    if r is None:
        return {"ok": False, "error": "This vote is over / ድምጽ መስጫው አብቅቷል"}
    if r["status"] != "open":
        return {"ok": False, "error": "The game has already started / ጨዋታው ተጀምሯል"}
    if r["poll_at"] is None:
        return {"ok": False, "error": "No vote is open"}
    if uid in D["banned"]:
        return {"ok": False, "error": "Your account is blocked"}
    if not r["by_uid"].get(uid):
        return {"ok": False, "error": "Only players who own a number can vote"}
    if uid in r["votes"]:                           # already voted: the first vote stays
        return {"ok": False, "code": "voted", "vote": r["votes"][uid],
                "error": "You already voted. Your vote is final / ድምጽዎ ተቀምጧል፣ መቀየር አይቻልም"}
    vote = 1 if yes else 0
    r["votes"][uid] = vote
    r["poll_dirty"] = True
    D["enqueue"]("INSERT INTO ng_votes(round_id, uid, vote, t) VALUES($1, $2, $3, $4) "
                 "ON CONFLICT (round_id, uid) DO NOTHING",
                 r["id"], uid, vote, int(time.time()))
    return {"ok": True, "vote": vote}


def admin_start(lobby, force=False):
    """Admin pressed START: freeze now, draw the winners, begin the 60-second countdown.
    force=True: the admin may start with as few as MIN_DRAW (3) numbers sold, because three
    different winners are needed. Without force the normal early-start minimum applies."""
    r = rounds.get(lobby)
    if r is None or r["status"] != "open":
        return {"ok": False, "error": "This round is not open"}
    if r["test"]:
        return {"ok": False, "error": "This is a test round (fake players). Use /ngtestfreeze"}
    res = freeze_round(lobby, early=True, min_sold=MIN_DRAW if force else None)
    if res["ok"]:
        audit("admin_start", D.get("admin_id"), lobby, r["id"], {"sold": res["sold"], "force": bool(force)})
    return res


def find_uid_by_phone(text):
    """Find a player by phone number (0912345678 and +251912345678 both work)."""
    digits = "".join(ch for ch in str(text) if ch.isdigit())
    if len(digits) < 9:
        return None
    last9 = digits[-9:]
    for uid, ph in D["phones"].items():
        if str(ph).endswith(last9):
            return uid
    return None


def refund_number(uid, lobby, num):
    """ADMIN: take ONE number away from ONE player and give him his money back, so he can
    choose another number. Only while the round is still open (before the draw).
    IMPORTANT: no "await" may ever be added inside this function (see top of file):
    the number, the wallet, the play-through and the database delete all change in one step."""
    r = rounds.get(lobby)
    if r is None:
        return {"ok": False, "error": "Unknown room"}
    if r["status"] != "open":
        return {"ok": False, "error": "This round has already started (winners are drawn). "
                                      "A number can no longer be taken back."}
    if r["test"]:
        return {"ok": False, "error": "This is a test round (fake players)"}
    n = _int(num)
    o = r["owners"].get(n) if n is not None else None
    if o is None or o["uid"] != uid:
        return {"ok": False, "error": f"#{num} is not owned by this player in {room_name(lobby)}"}

    price = price_of(lobby)
    # 1) the number is free again, and the player's slot too
    del r["owners"][n]
    r["by_uid"][uid].remove(n)
    if not r["by_uid"][uid]:
        del r["by_uid"][uid]
    # 2) the money goes back (cash) and the play-through this number added is undone
    D["wallets"][uid] = D["wallets"].get(uid, 0) + price
    D["wagered"][uid] = max(0, D["wagered"].get(uid, 0) - o["used"])
    D["log"](uid, "refund", price, f"Number Game {room_name(lobby)} · #{n} cancelled by admin")  # saves the wallet
    # 3) the database forgets the number (this also frees its purchase key)
    D["enqueue"]("DELETE FROM ng_numbers WHERE round_id = $1 AND num = $2 AND uid = $3",
                 r["id"], n, uid)
    # 4) a retry of the old purchase request must not confirm the cancelled number again
    for k in [k for k, v in idem_seen.items()
              if k.startswith(f"{uid}:") and v["lobby"] == lobby and v["num"] == n]:
        del idem_seen[k]
    # 5) no number left = he is no longer a player of this round: his poll vote is removed too
    if uid not in r["by_uid"] and uid in r["votes"]:
        del r["votes"][uid]
        r["poll_dirty"] = True
        D["enqueue"]("DELETE FROM ng_votes WHERE round_id = $1 AND uid = $2", r["id"], uid)
    audit("admin_refund", D.get("admin_id"), lobby, r["id"],
          {"uid": uid, "num": n, "name": o["name"], "amount": price, "used": o["used"]})
    return {"ok": True, "num": n, "name": o["name"], "amount": price,
            "balance": D["wallets"][uid], "left": len(r["by_uid"].get(uid, [])), "round": r["id"]}


def poll_player_text(r):
    sold = len(r["owners"])
    return (f"🗳 Number Game {game_label(r['lobby'], r['game_no'], r['id'])}\n\n"
            f"ጨዋታውን አሁን እንጀምር? ድምጽዎ ሚስጥራዊ ነው።\n"
            f"Start the game now? Your vote is anonymous.\n\n"
            f"Sold now: {sold}/{size_of(r['lobby'])}. If we start now the prizes are:\n"
            f"{prize_line(r['lobby'], sold)}\n"
            f"(If more numbers are sold first, the prizes grow.)")


def panel_text(r):
    t = poll_tally(r)
    sold = len(r["owners"])
    return (f"🗳 Poll · {game_label(r['lobby'], r['game_no'], r['id'])}\n"
            f"Sold {sold}/{size_of(r['lobby'])} · players {t['players']}\n"
            f"✅ Yes {t['yes']} ({t['yes_pct']}%) · ❌ No {t['no']} ({t['no_pct']}%) · "
            f"not voted {t['not_voted']}\n"
            f"If you start now: {prize_line(r['lobby'], sold)}\n\n"
            f"Votes are anonymous and only advise you. Nothing starts until you press Start.")


def panel_rows(r, confirm=False):
    """Buttons are tied to THIS game (its round id), so an old button can never start a newer game."""
    p = r["id"]
    if confirm:
        return [[("✅ Yes, start now", f"ngq:{p}:y"), ("↩️ Cancel", f"ngq:{p}:n")]]
    return [[("🔄 Refresh", f"ngq:{p}:r"), ("▶️ Start game now", f"ngq:{p}:s")]]


def start_text(r):
    return (f"⏱ ጨዋታው ከ60 ሰከንድ በኋላ ይጀምራል። ለሁላችሁም መልካም ዕድል! 🍀\n"
            f"The game will start in {COUNTDOWN_SECONDS} seconds. Good luck to you all! 🍀\n\n"
            f"Number Game {game_label(r['lobby'], r['game_no'], r['id'])}")


def vote_rows(r):
    return [[("✅ አዎ · Yes", f"ngv:{r['id']}:1"), ("❌ አይ · No", f"ngv:{r['id']}:0")]]


def player_poll_view(r, uid):
    """(text, button rows) of one player's poll message.
    Before voting: the question and two buttons. After voting: his vote (final) and
    the live percentages, no buttons."""
    v = r["votes"].get(uid)
    if v is None:
        return poll_player_text(r), vote_rows(r)
    mine = "✅ አዎ · Yes" if v == 1 else "❌ አይ · No"
    return (f"{poll_player_text(r)}\n\n{vote_line(r)}\n"
            f"✔ Your vote: {mine} (final / የመጨረሻ)"), None


def closed_poll_text(r):
    return (f"🔒 ድምጽ መስጫው ተዘግቷል · ጨዋታው ተጀምሯል።\n"
            f"Vote closed: the game has started.\n\n"
            f"Number Game {game_label(r['lobby'], r['game_no'], r['id'])}\n{vote_line(r)}")


async def send_poll(r):
    """Send the vote to every player who has not got it yet (so running it again only
    reaches players who bought a number after the poll was opened)."""
    for uid in real_players(r):
        if uid in r["poll_msgs"] or uid in r["votes"]:
            continue
        text, rows = player_poll_view(r, uid)
        mid = await _send(uid, text, _kb(rows) if rows else None)
        if mid:
            r["poll_msgs"][uid] = mid
        await asyncio.sleep(0.06)


async def poll_refresh():
    """Live percentages: update the players' poll messages (at most every POLL_REFRESH
    seconds), and when the game starts close them for everybody."""
    if D.get("edit") is None or _refreshing[0]:
        return
    _refreshing[0] = True
    try:
        now = time.time()
        for r in list(rounds.values()):
            if not r["poll_msgs"]:
                continue
            closed = r["status"] != "open"
            if not closed and not (r["poll_dirty"] and now - r["poll_synced"] >= POLL_REFRESH):
                continue
            r["poll_dirty"], r["poll_synced"] = False, now
            msgs = dict(r["poll_msgs"])
            if closed:
                r["poll_msgs"] = {}                 # the final edit is made only once
            for uid, mid in msgs.items():
                if closed:
                    await _edit(uid, mid, closed_poll_text(r))
                else:
                    text, rows = player_poll_view(r, uid)
                    await _edit(uid, mid, text, rows)
                await asyncio.sleep(0.05)
    finally:
        _refreshing[0] = False


async def send_start_notices():
    """Tell every player of a frozen round that the game starts in 60 seconds."""
    batch = list(start_notices)
    start_notices.clear()
    for r in batch:
        text = start_text(r)
        for uid in real_players(r):
            await _send(uid, text)
            await asyncio.sleep(0.06)


# ---------- load everything back after a restart ----------
def _loads(v):
    if isinstance(v, str):
        v = json.loads(v)
    return v or None


ROUND_COLS = ("id, lobby, status, created, full_at, close_at, settled_at, early, test, "
              "game_no, poll_at, data")


def _round_from_row(row, status=None):
    r = _new_round(row["lobby"], row["id"], row["created"], status or row["status"], row["full_at"])
    r["close_at"], r["early"] = row["close_at"], bool(row["early"])
    r["test"], r["draw"] = bool(row["test"]), _loads(row["data"])
    r["game_no"], r["poll_at"] = row.get("game_no"), row.get("poll_at")
    r["settled_at"] = row["settled_at"]
    return r


async def load_state(c):
    """c = an asyncpg connection. Rebuilds open / counting-down rounds, their numbers,
    votes, paid prize places, recent history and game numbers."""
    rows = await c.fetch(
        f"SELECT {ROUND_COLS} "
        "FROM ng_rounds WHERE status IN ('open', 'full', 'countdown') ORDER BY id")
    top = await c.fetchval("SELECT COALESCE(MAX(id), 0) FROM ng_rounds") or 0
    saved = await c.fetchval("SELECT v FROM kv WHERE k = 'ng_round_counter'") or 0
    ng_counter[0] = max(top, saved)

    # game numbers: the last real game of every lobby ...
    for p in PRICES:
        game_counter[p] = 0
    for row in await c.fetch(
            "SELECT lobby, MAX(game_no) AS g FROM ng_rounds WHERE test = 0 AND game_no IS NOT NULL "
            "AND status IN ('countdown', 'settled') GROUP BY lobby"):
        if row["lobby"] in game_counter:
            game_counter[row["lobby"]] = row["g"] or 0
    # ... and old real rounds from before game numbers existed get one now, oldest first
    backfilled = {}
    for row in await c.fetch(
            "SELECT id, lobby FROM ng_rounds WHERE test = 0 AND game_no IS NULL "
            "AND status IN ('countdown', 'settled') ORDER BY id"):
        if row["lobby"] in game_counter:
            game_counter[row["lobby"]] += 1
            backfilled[row["id"]] = game_counter[row["lobby"]]
            D["enqueue"]("UPDATE ng_rounds SET game_no = $1 WHERE id = $2",
                         game_counter[row["lobby"]], row["id"])

    for row in rows:
        if row["lobby"] not in PRICES:
            continue
        r = _round_from_row(row)
        if r["id"] in backfilled:
            r["game_no"] = backfilled[r["id"]]
        rounds[row["lobby"]] = r
    for lobby, r in rounds.items():                 # an open round gets the next free game number
        if r["status"] in ("open", "full") and not r["test"] \
                and (r["game_no"] is None or r["game_no"] <= game_counter[lobby]):
            r["game_no"] = game_counter[lobby] + 1
            D["enqueue"]("UPDATE ng_rounds SET game_no = $1 WHERE id = $2", r["game_no"], r["id"])
    ids = [r["id"] for r in rounds.values()]
    by_id = {r["id"]: r for r in rounds.values()}
    if ids:
        for row in await c.fetch(
                "SELECT round_id, lobby, num, uid, name, price, used, slot, idem, t "
                "FROM ng_numbers WHERE round_id = ANY($1::bigint[])", ids):
            r = by_id[row["round_id"]]
            r["owners"][row["num"]] = {"uid": row["uid"], "name": row["name"], "t": row["t"],
                                       "slot": row["slot"], "used": row["used"]}
            r["by_uid"].setdefault(row["uid"], []).append(row["num"])
            idem_seen[row["idem"]] = {
                "lobby": row["lobby"], "num": row["num"],
                "result": {"ok": True, "num": row["num"], "name": row["name"],
                           "price": row["price"], "round": row["round_id"],
                           "full": False, "restored": True}}
        for row in await c.fetch("SELECT round_id, place FROM ng_prizes "
                                 "WHERE round_id = ANY($1::bigint[])", ids):
            by_id[row["round_id"]]["paid"].add(row["place"] - 1)   # never pay a place twice
        for row in await c.fetch("SELECT round_id, uid, vote FROM ng_votes "
                                 "WHERE round_id = ANY($1::bigint[])", ids):
            by_id[row["round_id"]]["votes"][row["uid"]] = row["vote"]
    for row in await c.fetch(
            f"SELECT {ROUND_COLS} "
            "FROM ng_rounds WHERE status = 'settled' ORDER BY id DESC LIMIT 100"):
        if row["lobby"] in PRICES and len(history[row["lobby"]]) < HISTORY_KEEP:
            r = _round_from_row(row, "settled")
            if r["id"] in backfilled:
                r["game_no"] = backfilled[r["id"]]
            r["saved"] = True
            if r["draw"]:
                history[row["lobby"]].append(r)
    for lobby, r in list(rounds.items()):          # repair anything left half way
        if r["status"] in ("open", "full"):
            r["status"] = "open"
            if len(r["owners"]) >= size_of(lobby):
                freeze_round(lobby)
        elif r["status"] == "countdown" and not r["draw"]:
            r["status"] = "open"                   # cannot happen normally; never lose the round
    ensure_rounds()


# ---------- stage 3: saved past games, read back from the database ----------
async def list_games(c, lobby, limit=RESULTS_PAGE, before=None):
    """Finished REAL games of one lobby, newest first. `before` = a game number:
    only older games are returned (use it to page backwards)."""
    limit = min(max(int(limit or RESULTS_PAGE), 1), 50)
    out = []
    for row in await c.fetch(
            f"SELECT {ROUND_COLS} FROM ng_rounds WHERE lobby = $1 AND status = 'settled' "
            "AND test = 0 AND game_no IS NOT NULL AND ($3::int IS NULL OR game_no < $3) "
            "ORDER BY game_no DESC LIMIT $2", lobby, limit, before):
        r = _round_from_row(row, "settled")
        r["saved"] = True
        if r["draw"]:
            out.append(r)
    return out


async def load_game(c, lobby, game_no):
    """One finished game by its number, with its board (the numbers that were sold)
    and a list of (num, uid, name) for the caller to pick out a player's own numbers."""
    rows = await c.fetch(
        f"SELECT {ROUND_COLS} FROM ng_rounds WHERE lobby = $1 AND game_no = $2 "
        "AND status = 'settled' AND test = 0", lobby, game_no)
    if not rows:
        return None, []
    r = _round_from_row(rows[0], "settled")
    r["saved"] = True
    if not r["draw"]:
        return None, []
    sold = await c.fetch("SELECT num, uid, name FROM ng_numbers WHERE round_id = $1 ORDER BY num",
                         r["id"])
    return r, [(x["num"], x["uid"], x["name"]) for x in sold]


def game_view(r, sold_rows, uid):
    """Everything a player needs to read (or replay) a finished game."""
    v = result_view(r)
    v["board"] = [n for n, _, _ in sold_rows]
    v["mine"] = [{"num": n, "name": nm} for n, u, nm in sold_rows if u == uid]
    v["replay"] = {"intro": DRAW_INTRO, "gap": REVEAL_GAP, "order": [i for i in REVEAL_ORDER if i < places_of(r["draw"])]}
    return v


# ---------- what the screen asks for (no web library needed, easy to test) ----------
def state_for(uid, lobby):
    r = rounds[lobby]
    mine = [{"num": n, "name": r["owners"][n]["name"]}
            for n in sorted(r["by_uid"].get(uid, []))]
    return {
        "ok": True, "lobby": lobby, "round": r["id"], "game": r["game_no"],
        "status": r["status"], "count": size_of(lobby), "max": MAX_PER_PLAYER, "sold": len(r["owners"]),
        "price": price_of(lobby), "size": size_of(lobby), "name": room_name(lobby),
        "early_ok": early_ok(lobby),
        "taken": sorted(r["owners"]),            # other players' names are not sent
        "mine": mine, "cash": D["wallets"].get(uid, 0),
        "prize_full": list(ROOMS[lobby]["prizes"]),
        "prize_now": prizes_for(lobby, len(r["owners"])), "min_early": MIN_EARLY_START,
        "admin": bool(D.get("admin_id") and uid == D["admin_id"]), "min_force": MIN_DRAW,
        "draw": result_view(r) if r["status"] == "countdown" else None,
        "last": (result_view(last_result(lobby)) if last_result(lobby) else None),
        "lobbies": [{"key": p, "price": price_of(p), "size": size_of(p), "name": room_name(p),
                     "round": rounds[p]["id"], "game": rounds[p]["game_no"],
                     "status": rounds[p]["status"],
                     "sold": len(rounds[p]["owners"]),
                     "mine": len(rounds[p]["by_uid"].get(uid, []))} for p in PRICES],
    }


async def handle_state(query):
    user = D["verify"](query.get("initData", ""))
    if not user:
        return {"ok": False, "error": "auth"}
    uid = D["touch"](user)
    if uid in D["banned"]:
        return {"ok": False, "error": "blocked"}
    if not allowed(uid):
        return {"ok": False, "error": "closed", "msg": "Number Game is not open yet"}
    lobby = _int(query.get("lobby"))
    if lobby not in rounds:
        lobby = PRICES[0]
    return state_for(uid, lobby)


async def handle_buy(body):
    user = D["verify"](body.get("initData", ""))
    if not user:
        return {"ok": False, "error": "auth"}
    uid = D["touch"](user)
    if uid in D["banned"]:
        return {"ok": False, "error": "Your account is blocked. Contact support"}
    if not allowed(uid):
        return {"ok": False, "error": "Number Game is not open yet"}
    if uid not in D["phones"]:
        return {"ok": False, "needPhone": True,
                "error": "Share your phone number first / መጀመሪያ ስልክ ቁጥርዎን ያጋሩ"}
    return buy_number(uid, _int(body.get("lobby")), body.get("num"),
                      body.get("name"), body.get("idem"))


async def _gate(query):
    """Shared checks of the read endpoints. Returns (uid, None) or (None, error answer)."""
    user = D["verify"](query.get("initData", ""))
    if not user:
        return None, {"ok": False, "error": "auth"}
    uid = D["touch"](user)
    if uid in D["banned"]:
        return None, {"ok": False, "error": "blocked"}
    if not allowed(uid):
        return None, {"ok": False, "error": "closed", "msg": "Number Game is not open yet"}
    return uid, None


async def handle_start(body):
    """ADMIN ONLY. The Start button on the number screen. Names the game (round id) it was
    pressed for, so an old screen can never start a newer game."""
    user = D["verify"](body.get("initData", ""))
    if not user:
        return {"ok": False, "error": "auth"}
    uid = D["touch"](user)
    if not (D.get("admin_id") and uid == D["admin_id"]):
        return {"ok": False, "error": "Admin only"}
    lobby, rid = _int(body.get("lobby")), _int(body.get("round"))
    r = rounds.get(lobby)
    if r is None:
        return {"ok": False, "error": "Unknown lobby"}
    if rid is None or rid != r["id"]:
        return {"ok": False, "error": "This game has changed. Refresh the screen"}
    return admin_start(lobby, force=True)


async def handle_results(query):
    """Past games of one lobby, newest first. ?lobby=50&before=7 pages backwards.
    Reads the database (all games ever), or the in-memory history when there is no database."""
    uid, err = await _gate(query)
    if err:
        return err
    lobby = _int(query.get("lobby"))
    if lobby not in rounds:
        lobby = PRICES[0]
    limit = min(max(_int(query.get("limit")) or RESULTS_PAGE, 1), 50)
    before = _int(query.get("before"))
    pool = D.get("pool")
    if pool is None:
        found = [r for r in history[lobby] if not r["test"] and r["game_no"]
                 and (before is None or r["game_no"] < before)][:limit]
    else:
        try:
            async with pool.acquire() as c:
                found = await list_games(c, lobby, limit, before)
        except Exception as e:
            print("number game results failed:", repr(e))
            return {"ok": False, "error": "busy"}
    return {"ok": True, "lobby": lobby, "results": [result_view(r) for r in found],
            "more": len(found) == limit,
            "next_before": found[-1]["game_no"] if found else None}


async def handle_game(query):
    """One finished game by its number: ?lobby=50&game=7. Includes the board, the player's
    own numbers and the data for the replay animation."""
    uid, err = await _gate(query)
    if err:
        return err
    lobby, game = _int(query.get("lobby")), _int(query.get("game"))
    if lobby not in PRICES or game is None or game < 1:
        return {"ok": False, "error": "notfound"}
    pool = D.get("pool")
    if pool is None:
        r = next((x for x in history[lobby] if x["game_no"] == game and not x["test"]), None)
        sold = [(n, o["uid"], o["name"]) for n, o in sorted(r["owners"].items())] if r else []
    else:
        try:
            async with pool.acquire() as c:
                r, sold = await load_game(c, lobby, game)
        except Exception as e:
            print("number game load failed:", repr(e))
            return {"ok": False, "error": "busy"}
    if r is None or r["status"] != "settled":
        return {"ok": False, "error": "notfound"}
    return {"ok": True, "game": game_view(r, sold, uid)}


def routes():
    """Web routes for bot.py:  app.add_routes([... *ng.routes()])"""
    from aiohttp import web

    async def api_state(req):
        return web.json_response(await handle_state(dict(req.query)))

    async def api_buy(req):
        try:
            body = await req.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        return web.json_response(await handle_buy(body))

    async def api_start(req):
        try:
            body = await req.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        return web.json_response(await handle_start(body))

    async def api_results(req):
        return web.json_response(await handle_results(dict(req.query)))

    async def api_game(req):
        return web.json_response(await handle_game(dict(req.query)))

    return [web.get("/api/ng/state", api_state), web.post("/api/ng/buy", api_buy),
            web.get("/api/ng/results", api_results), web.get("/api/ng/game", api_game),
            web.post("/api/ng/start", api_start)]


# ---------- admin test commands (only the ADMIN_ID account) ----------
def phase_text(r, now=None):
    now = time.time() if now is None else now
    if r["status"] != "countdown":
        return r["status"]
    if now < r["close_at"]:
        return f"countdown {int(r['close_at'] - now)}s"
    if now < settle_time(r):
        return "drawing"
    return "settling"


def status_text():
    lines = ["🎟 Number Game: " + ("ON for everyone" if ENABLED else "OFF (admin test only)")]
    for p in PRICES:
        r = rounds.get(p)
        if not r:
            continue
        sold = len(r["owners"])
        players = sum(1 for v in r["by_uid"].values() if v)
        poll = ""
        if r["poll_at"] is not None:
            t = poll_tally(r)
            poll = f"\nPoll: ✅ {t['yes']} · ❌ {t['no']} · not voted {t['not_voted']}"
        lines.append(f"\n{game_label(p, r['game_no'], r['id'])} · round #{r['id']} · {phase_text(r)}"
                     + (" · TEST" if r["test"] else "") + "\n"
                     f"Sold {sold}/{size_of(p)} · players {players}\n"
                     f"Sales {sold * price_of(p)} ETB" + poll)
    return "\n".join(lines)


def result_text(r):
    d = r["draw"]
    lines = [f"{game_label(r['lobby'], r['game_no'], r['id'])} · round #{r['id']}" + (" · TEST" if r["test"] else "")
             + (" · early start" if r["early"] else ""),
             f"Sold {d['sold']} · sales {d['sold'] * price_of(r['lobby'])} ETB · prizes {sum(d['prizes'])} ETB"]
    for i in range(places_of(d)):
        w = d["winners_info"][i]
        lines.append(f"{PLACES[i].title()}: #{w['num']} {w['name']} ({_pname(w['uid'])}) {d['prizes'][i]} ETB")
    lines.append(f"Hash: {d['hash']}")
    if r["status"] == "settled":
        lines.append(f"Check: SHA-256 of this text must equal the hash:\n{d['verify']}")
    else:
        lines.append("(secret is revealed after the round is settled)")
    return "\n".join(lines)


def register(dp):
    """bot.py calls ng.register(dp) once."""
    from aiogram import F
    from aiogram.filters import Command, CommandObject
    from aiogram.types import CallbackQuery, Message

    def is_admin(m):
        return bool(D.get("admin_id")) and m.from_user.id == D["admin_id"]

    @dp.message(Command("ngstatus"))
    async def cmd_ngstatus(m: Message):
        if is_admin(m):
            await m.answer(status_text())

    @dp.message(Command("ngbuy"))
    async def cmd_ngbuy(m: Message, command: CommandObject):
        """/ngbuy 50 17 Abebe : test purchase with YOUR admin wallet (real cash)."""
        if not is_admin(m):
            return
        parts = (command.args or "").split(maxsplit=2)
        lobby = _int(parts[0]) if parts else None
        if len(parts) < 3 or lobby not in PRICES:
            await m.answer("Send:\n/ngbuy <room 50, 100, 5050 or 10050> <number 1-100> <name>\n\n"
                           "Example:\n/ngbuy 50 17 Abebe\n\n"
                           "⚠️ This spends real cash from YOUR wallet (test).")
            return
        uid = D["ensure"](m.from_user)
        res = buy_number(uid, lobby, parts[1], parts[2], "adm-" + uuid.uuid4().hex)
        if res["ok"]:
            await m.answer(f"✅ Bought #{res['num']} ({res['name']}) for {res['price']} ETB.\n"
                           f"Balance: {res['balance']} · you own {res['mine']}/{MAX_PER_PLAYER} "
                           f"· round #{res['round']}" + ("\n🔒 Round is now FULL" if res["full"] else ""))
        else:
            await m.answer("❌ " + res["error"])

    def _lobby_arg(command):
        parts = (command.args or "").split()
        lobby = _int(parts[0]) if parts else None
        return lobby if lobby in PRICES else None

    @dp.message(Command("ngpoll"))
    async def cmd_ngpoll(m: Message, command: CommandObject):
        """/ngpoll 50 : ask the players of that lobby "start the game now?" (anonymous)."""
        if not is_admin(m):
            return
        lobby = _lobby_arg(command)
        if lobby is None:
            await m.answer("Send: /ngpoll <room 50, 100, 5050 or 10050>\n\nAsks the players of that room "
                           f"(needs {MIN_EARLY_START}+ numbers sold) whether to start now. "
                           "You see only the totals, then you decide with the Start button.")
            return
        res = open_poll(lobby)
        if not res["ok"]:
            await m.answer("❌ " + res["error"])
            return
        r = res["round"]
        await m.answer(panel_text(r), reply_markup=_kb(panel_rows(r)))
        todo = [u for u in real_players(r) if u not in r["poll_msgs"] and u not in r["votes"]]
        if todo:                       # first time: everybody. Again later: only new buyers
            await m.answer(f"📨 Sending the vote to {len(todo)} players…")
            _spawn(send_poll(r))

    @dp.message(Command("ngstart"))
    async def cmd_ngstart(m: Message, command: CommandObject):
        """/ngstart 50 : start the game NOW (same as the Start button, no confirmation)."""
        if not is_admin(m):
            return
        lobby = _lobby_arg(command)
        if lobby is None:
            await m.answer("Send: /ngstart <room 50, 100, 5050 or 10050>\n\nStarts the game now "
                           f"(needs {MIN_EARLY_START}+ numbers sold). Players get the 60-second message.")
            return
        res = admin_start(lobby)
        if not res["ok"]:
            await m.answer("❌ " + res["error"])
            return
        r = rounds[lobby]
        await m.answer(f"▶️ {game_label(lobby, r['game_no'], r['id'])} started with {res['sold']} numbers. "
                       f"Countdown {COUNTDOWN_SECONDS}s. Players are being told.\n"
                       f"Hash: {r['draw']['hash']}")

    @dp.message(Command("ngforce"))
    async def cmd_ngforce(m: Message, command: CommandObject):
        """/ngforce 50 : admin starts the game NOW with as few as 3 numbers sold."""
        if not is_admin(m):
            return
        lobby = _lobby_arg(command)
        if lobby is None:
            await m.answer("Send: /ngforce <room 50, 100, 5050 or 10050>\n\nStarts the game now with any "
                           f"number of sold numbers from {MIN_DRAW} up. Prizes shrink in proportion.")
            return
        res = admin_start(lobby, force=True)
        if not res["ok"]:
            await m.answer("❌ " + res["error"])
            return
        r = rounds[lobby]
        await m.answer(f"▶️ {game_label(lobby, r['game_no'], r['id'])} started with {res['sold']} numbers. "
                       f"Countdown {COUNTDOWN_SECONDS}s.\nHash: {r['draw']['hash']}")

    @dp.message(Command("ngrefund"))
    async def cmd_ngrefund(m: Message, command: CommandObject):
        """/ngrefund 0912345678 50 17 : cancel that player's number #17 in room 50 and refund him.
        /ngrefund 0912345678 50       : show the numbers he owns in room 50."""
        if not is_admin(m):
            return
        parts = (command.args or "").split()
        uid = find_uid_by_phone(parts[0]) if parts else None
        lobby = _int(parts[1]) if len(parts) >= 2 else None
        if len(parts) < 2 or lobby not in PRICES:
            await m.answer("Send:\n/ngrefund <phone> <room 50, 100, 5050 or 10050> <number>\n\n"
                           "Cancels that player's number and returns his money, so he can choose "
                           "another one. Works only before the game has started.\n\n"
                           "Example:\n/ngrefund 0912345678 50 17\n\n"
                           "Without the number it shows his numbers in that room:\n"
                           "/ngrefund 0912345678 50")
            return
        if uid is None:
            await m.answer("No player found with that phone number.")
            return
        r = rounds[lobby]
        if len(parts) == 2:
            nums = sorted(r["by_uid"].get(uid, []))
            if not nums:
                await m.answer(f"{_pname(uid)} owns no numbers in {game_label(lobby, r['game_no'], r['id'])}.")
                return
            lst = ", ".join(f"#{x} ({r['owners'][x]['name']})" for x in nums)
            await m.answer(f"{_pname(uid)} owns in {game_label(lobby, r['game_no'], r['id'])}:\n{lst}\n\n"
                           f"To cancel one: /ngrefund {parts[0]} {lobby} <number>")
            return
        res = refund_number(uid, lobby, parts[2])
        if not res["ok"]:
            await m.answer("❌ " + res["error"])
            return
        await m.answer(f"✅ Number #{res['num']} ({res['name']}) cancelled.\n"
                       f"Player: {_pname(uid)}\n"
                       f"Refunded: {res['amount']} ETB · new balance {res['balance']} ETB\n"
                       f"He still owns {res['left']}/{MAX_PER_PLAYER} numbers in this room "
                       f"and can choose another one.")
        await _send(uid, f"↩️ ቁጥር #{res['num']} ተሰርዟል፤ {res['amount']} ብር ወደ ሂሳብዎ ተመልሷል። "
                         f"ሌላ ቁጥር መምረጥ ይችላሉ።\n"
                         f"Number #{res['num']} was cancelled and {res['amount']} birr was returned "
                         f"to your balance. You can choose another number.")

    @dp.callback_query(F.data.startswith("ngv:"))
    async def cb_vote(cb: CallbackQuery):
        """A player pressed Yes / No under the poll message. The first tap is final."""
        parts = (cb.data or "").split(":")
        rid = _int(parts[1]) if len(parts) == 3 else None
        if rid is None or parts[2] not in ("0", "1"):
            await cb.answer()
            return
        res = cast_vote(cb.from_user.id, rid, parts[2] == "1")
        r = next((x for x in rounds.values() if x["id"] == rid), None)
        if not res["ok"]:
            await cb.answer(res["error"], show_alert=True)
            if res.get("code") == "voted" and r is not None:    # take the buttons away
                try:
                    text, _ = player_poll_view(r, cb.from_user.id)
                    await cb.message.edit_text(text)
                except Exception:
                    pass
            return
        mine = "✅ አዎ · Yes" if res["vote"] else "❌ አይ · No"
        await cb.answer("Saved (final): " + mine)
        try:                                    # show his vote and the live percentages
            text, _ = player_poll_view(r, cb.from_user.id)
            await cb.message.edit_text(text)
        except Exception:
            pass

    @dp.callback_query(F.data.startswith("ngp:"))
    async def cb_panel_old(cb: CallbackQuery):
        """Buttons of an older panel (tied to a lobby, not to a game): never act on them."""
        if D.get("admin_id") and cb.from_user.id == D["admin_id"]:
            await cb.answer("This button is out of date. Send /ngpoll again.", show_alert=True)
        else:
            await cb.answer()

    @dp.callback_query(F.data.startswith("ngq:"))
    async def cb_panel(cb: CallbackQuery):
        """Admin buttons under the poll panel: refresh / start (with a confirm step).
        Every button names ONE game (round id): it can only ever act on that game."""
        if not (D.get("admin_id") and cb.from_user.id == D["admin_id"]):
            await cb.answer()
            return
        parts = (cb.data or "").split(":")
        rid = _int(parts[1]) if len(parts) == 3 else None
        act = parts[2] if len(parts) == 3 else ""
        r = next((x for x in rounds.values() if x["id"] == rid), None) if rid is not None else None
        if r is None or r["poll_at"] is None:
            await cb.answer("This panel is out of date (that game has started or is over)", show_alert=True)
            return
        lobby = r["lobby"]
        if act == "y":                                          # confirmed: start THIS game now
            res = admin_start(lobby)
            if not res["ok"]:
                await cb.answer(res["error"], show_alert=True)
                return
            await cb.answer("Started")
            done = f"▶️ Started with {res['sold']} numbers. Countdown {COUNTDOWN_SECONDS}s."
            try:
                await cb.message.edit_text(f"{done}\nPlayers are being told.\nHash: {r['draw']['hash']}")
            except Exception:
                pass
            return
        if r["status"] != "open":
            await cb.answer("This poll is over (the game has started)", show_alert=True)
            return
        await cb.answer()
        try:
            if act == "s":
                await cb.message.edit_text(
                    panel_text(r) + f"\n\n⚠️ Start {game_label(lobby, r['game_no'], r['id'])} now with "
                    f"{len(r['owners'])} numbers sold? This cannot be undone.",
                    reply_markup=_kb(panel_rows(r, confirm=True)))
            else:                                               # refresh / cancel: back to the panel
                await cb.message.edit_text(panel_text(r), reply_markup=_kb(panel_rows(r)))
        except Exception:
            pass                                                # "message is not modified"

    @dp.message(Command("ngresult"))
    async def cmd_ngresult(m: Message, command: CommandObject):
        """/ngresult 50 : the latest finished round of that lobby (test rounds included)."""
        if not is_admin(m):
            return
        lobby = _int((command.args or "50").split()[0])
        r = last_result(lobby, include_test=True) if lobby in PRICES else None
        await m.answer(result_text(r) if r else "No finished round yet for that lobby.")

    @dp.message(Command("ngtestfill"))
    async def cmd_ngtestfill(m: Message, command: CommandObject):
        """/ngtestfill 50 60 : TEST ONLY. Fill the lobby with 60 fake players (no money)."""
        if not is_admin(m):
            return
        parts = (command.args or "").split()
        lobby, count = (_int(parts[0]), _int(parts[1])) if len(parts) == 2 else (None, None)
        if lobby not in PRICES or count is None or count < 1:
            await m.answer("Send:\n/ngtestfill <room 50, 100, 5050 or 10050> <how many numbers>\n\n"
                           "Test only: fake players, no money. Works only while "
                           "NUMBER_GAME_ENABLED is off. 100 numbers starts the countdown.")
            return
        res = test_fill(lobby, count)
        if not res["ok"]:
            await m.answer("❌ " + res["error"])
            return
        await m.answer(f"✅ Added {res['added']} fake numbers. Sold {res['sold']}/{size_of(lobby)}."
                       + ("\n⏱ Round is full: countdown started." if res.get("frozen") else
                          "\nFor an early start send /ngtestfreeze " + str(lobby)))

    @dp.message(Command("ngtestfreeze"))
    async def cmd_ngtestfreeze(m: Message, command: CommandObject):
        """/ngtestfreeze 50 : TEST ONLY early start of a fake round with 50+ numbers."""
        if not is_admin(m):
            return
        lobby = _int((command.args or "").split()[0]) if (command.args or "").split() else None
        if lobby not in PRICES:
            await m.answer("Send: /ngtestfreeze <room 50, 100, 5050 or 10050>")
            return
        res = test_freeze(lobby)
        await m.answer(f"⏱ Countdown started ({COUNTDOWN_SECONDS}s)." if res["ok"] else "❌ " + res["error"])
