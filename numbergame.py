"""
Rodas Friend Zone NUMBER GAME  -  STAGE 1
(database tables, number purchase, wallet charge, 5-number limit)

NOT in this stage yet: the 60-second countdown, the draw, prizes, the poll,
the lottery animation and the player screen. A round that sells all 100
numbers is marked "full" and then waits (stage 2 will draw it).
Because of that, keep NUMBER_GAME_ENABLED off (default) for real players.

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
import json
import os
import re
import time
import uuid
from collections import OrderedDict

ENABLED = os.getenv("NUMBER_GAME_ENABLED", "0") == "1"   # off = only the admin can use it

NUMBERS = 100            # numbers 1..100 in every lobby
MAX_PER_PLAYER = 5       # per player, per lobby, per round
PRICES = (50, 100)       # the lobby name is its price in birr
NAME_MAX = 20            # longest name a player can give a number
IDEM_KEEP = 20000        # how many recent purchase keys are remembered in memory

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS ng_rounds(
    id BIGINT PRIMARY KEY,
    lobby INT NOT NULL,
    status TEXT NOT NULL,
    created BIGINT NOT NULL,
    full_at BIGINT,
    data JSONB NOT NULL DEFAULT '{{}}'::jsonb);
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
idem_seen = OrderedDict()      # "uid:key" -> {"lobby", "num", "result"}
ng_counter = [0]               # last round id used


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
            "owners": {},      # number -> {"uid", "name", "t", "slot", "used"}
            "by_uid": {}}      # uid -> [numbers]


def start_round(lobby):
    ng_counter[0] += 1
    r = _new_round(lobby, ng_counter[0], int(time.time()))
    rounds[lobby] = r
    D["enqueue"]("INSERT INTO ng_rounds(id, lobby, status, created) VALUES($1, $2, $3, $4)",
                 r["id"], lobby, "open", r["created"])
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
    if n is None or not 1 <= n <= NUMBERS:
        return fail("num", f"Choose a number from 1 to {NUMBERS}")
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
    if n in r["owners"]:
        return fail("taken", "Sorry, this number was just taken")
    mine = r["by_uid"].get(uid, [])
    if len(mine) >= MAX_PER_PLAYER:
        return fail("limit", f"You can own at most {MAX_PER_PLAYER} numbers in this lobby")
    price = lobby
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
    D["log"](uid, "bet", -price, f"Number Game {price} · #{n} ({nm})")   # saves the wallet
    r["owners"][n] = {"uid": uid, "name": nm, "t": now, "slot": slot, "used": used}
    r["by_uid"].setdefault(uid, []).append(n)
    D["enqueue"](
        "INSERT INTO ng_numbers(round_id, lobby, num, uid, name, price, used, slot, idem, t) "
        "VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        r["id"], lobby, n, uid, nm, price, used, slot, key, now)
    full = len(r["owners"]) >= NUMBERS
    if full:
        r["status"] = "full"
        r["full_at"] = now
        D["enqueue"]("UPDATE ng_rounds SET status = 'full', full_at = $1 WHERE id = $2",
                     now, r["id"])
        audit("round_full", None, lobby, r["id"], {"sold": len(r["owners"])})
    result = {"ok": True, "num": n, "name": nm, "price": price,
              "balance": wallets[uid], "mine": len(r["by_uid"][uid]),
              "round": r["id"], "full": full}
    idem_seen[key] = {"lobby": lobby, "num": n, "result": result}
    while len(idem_seen) > IDEM_KEEP:
        idem_seen.popitem(last=False)
    return result


# ---------- load everything back after a restart ----------
async def load_state(c):
    """c = an asyncpg connection. Rebuilds the open rounds and their numbers."""
    rows = await c.fetch("SELECT id, lobby, status, created, full_at FROM ng_rounds "
                         "WHERE status IN ('open', 'full') ORDER BY id")
    top = await c.fetchval("SELECT COALESCE(MAX(id), 0) FROM ng_rounds") or 0
    saved = await c.fetchval("SELECT v FROM kv WHERE k = 'ng_round_counter'") or 0
    ng_counter[0] = max(top, saved)
    for row in rows:
        if row["lobby"] in PRICES:
            rounds[row["lobby"]] = _new_round(row["lobby"], row["id"], row["created"],
                                              row["status"], row["full_at"])
    ids = [r["id"] for r in rounds.values()]
    if ids:
        for row in await c.fetch(
                "SELECT round_id, lobby, num, uid, name, price, used, slot, idem, t "
                "FROM ng_numbers WHERE round_id = ANY($1::bigint[])", ids):
            r = next(x for x in rounds.values() if x["id"] == row["round_id"])
            r["owners"][row["num"]] = {"uid": row["uid"], "name": row["name"], "t": row["t"],
                                       "slot": row["slot"], "used": row["used"]}
            r["by_uid"].setdefault(row["uid"], []).append(row["num"])
            idem_seen[row["idem"]] = {
                "lobby": row["lobby"], "num": row["num"],
                "result": {"ok": True, "num": row["num"], "name": row["name"],
                           "price": row["price"], "round": row["round_id"],
                           "full": False, "restored": True}}
    for r in rounds.values():                      # safety: 100 sold must be "full"
        if r["status"] == "open" and len(r["owners"]) >= NUMBERS:
            r["status"] = "full"
            r["full_at"] = int(time.time())
            D["enqueue"]("UPDATE ng_rounds SET status = 'full', full_at = $1 WHERE id = $2",
                         r["full_at"], r["id"])
    ensure_rounds()


# ---------- what the screen asks for (no web library needed, easy to test) ----------
def state_for(uid, lobby):
    r = rounds[lobby]
    mine = [{"num": n, "name": r["owners"][n]["name"]}
            for n in sorted(r["by_uid"].get(uid, []))]
    return {
        "ok": True, "lobby": lobby, "round": r["id"], "status": r["status"],
        "count": NUMBERS, "max": MAX_PER_PLAYER, "sold": len(r["owners"]),
        "taken": sorted(r["owners"]),            # other players' names are not sent
        "mine": mine, "cash": D["wallets"].get(uid, 0),
        "lobbies": [{"price": p, "round": rounds[p]["id"], "status": rounds[p]["status"],
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

    return [web.get("/api/ng/state", api_state), web.post("/api/ng/buy", api_buy)]


# ---------- admin test commands (only the ADMIN_ID account) ----------
def status_text():
    lines = ["🎟 Number Game: " + ("ON for everyone" if ENABLED else "OFF (admin test only)")]
    for p in PRICES:
        r = rounds.get(p)
        if not r:
            continue
        sold = len(r["owners"])
        players = sum(1 for v in r["by_uid"].values() if v)
        lines.append(f"\n{p} ETB · round #{r['id']} · {r['status']}\n"
                     f"Sold {sold}/{NUMBERS} · players {players}\n"
                     f"Sales {sold * p} ETB")
    return "\n".join(lines)


def register(dp):
    """bot.py calls ng.register(dp) once."""
    from aiogram.filters import Command, CommandObject
    from aiogram.types import Message

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
            await m.answer("Send:\n/ngbuy <lobby 50 or 100> <number 1-100> <name>\n\n"
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
