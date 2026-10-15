"""
Rodas Friend Zone SOLITAIRE  -  one player, win = 2x the bet.

How it works (why it is safe):
  * The SERVER shuffles and keeps all cards. The phone only receives the cards the player may see
    (face-down cards are sent as -1) and sends back moves. Every move is checked here, so a
    changed phone/app can not cheat.
  * Taking the bet, saving the game and paying a win are plain functions with NO "await" inside,
    so they can never be interrupted halfway (same rule as numbergame.py).
  * One open game per player. Pressing PLAY twice never charges twice: the open game is shown again.
  * The game is saved in the database after every move, so a restart of the bot loses nothing.
  * A game that is not finished within TIME_LIMIT seconds is a loss (checked when the player or
    the server touches it). Quit = loss. All cards on the 4 foundations = win = bet x PAYOUT_MULT.

Keep SOLITAIRE_ENABLED off (default) until the legal check is finished: then only the admin can play.

bot.py needs only these lines (see the notes at the end of this file):
    import solitaire as sol
    sol.register(dp)                       (next to ng.register(dp))
    *sol.routes(),                         (inside app.add_routes)
    await c.execute(sol.SCHEMA)            (next to ng.SCHEMA)
    sol.init(...same arguments as ng.init...)
    await sol.load_state(c)                (next to ng.load_state)
"""
import json
import os
import re
import secrets
import time

ENABLED = os.getenv("SOLITAIRE_ENABLED", "0") == "1"      # off = only the admin can play
MIN_BET = int(os.getenv("SOLITAIRE_MIN_BET", "30"))
MAX_BET = int(os.getenv("SOLITAIRE_MAX_BET", "1000"))
PAYOUT_MULT = 2                                            # a win pays bet x 2 (stake included)
DRAW = int(os.getenv("SOLITAIRE_DRAW", "3"))               # 1 = easier, 3 = harder
TIME_LIMIT = int(os.getenv("SOLITAIRE_MINUTES", "10")) * 60

SUITS = 4                  # 0 spades, 1 clubs, 2 diamonds, 3 hearts (same order as the screen)
D = {}                     # filled by init()
games = {}                 # uid -> the open game of that player
stats = {"games": 0, "bets": 0, "paid": 0, "wins": 0}
_last_id = [0]

SCHEMA = """
CREATE TABLE IF NOT EXISTS sol_games(
    id BIGINT PRIMARY KEY,
    uid BIGINT NOT NULL,
    bet BIGINT NOT NULL,
    state TEXT NOT NULL,
    status TEXT NOT NULL,
    payout BIGINT NOT NULL DEFAULT 0,
    created BIGINT NOT NULL,
    ended BIGINT
);
CREATE INDEX IF NOT EXISTS sol_games_uid ON sol_games(uid);
"""

_SAVE = ("INSERT INTO sol_games(id, uid, bet, state, status, payout, created, ended) "
         "VALUES($1, $2, $3, $4, $5, $6, $7, $8) "
         "ON CONFLICT (id) DO UPDATE SET state = $4, status = $5, payout = $6, ended = $7")


def init(**deps):
    D.update(deps)


def allowed(uid):
    return bool(ENABLED or (D.get("admin_id") and uid == D["admin_id"]))


def _int(v):
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        return None
    try:
        return int(v)
    except ValueError:
        return None


# ---------- cards: id 0..51, suit = id // 13, rank = id % 13 (0 = Ace ... 12 = King) ----------
def suit(c):
    return c // 13


def rank(c):
    return c % 13


def red(c):
    return suit(c) >= 2


def new_deal():
    deck = list(range(52))
    secrets.SystemRandom().shuffle(deck)
    tab = []
    for i in range(7):                                   # column i gets i+1 cards, the last is face up
        col = [[deck.pop(), 0] for _ in range(i + 1)]
        col[-1][1] = 1
        tab.append(col)
    return {"stock": deck, "waste": [], "found": [0, 0, 0, 0], "tab": tab, "moves": 0}


def _can_stack(card, onto):
    return rank(onto) == rank(card) + 1 and red(onto) != red(card)


def _won(g):
    return all(n == 13 for n in g["s"]["found"])


# ---------- saving ----------
def _save(g):
    D["enqueue"](_SAVE, g["id"], g["uid"], g["bet"], json.dumps(g["s"]), g["status"],
                 g["payout"], g["created"], g["ended"])


def _finish(g, won):
    """Close the game ONCE. A win pays the player. No 'await' in here."""
    if g["status"] != "active":
        return
    g["status"] = "won" if won else "lost"
    g["ended"] = int(time.time())
    games.pop(g["uid"], None)
    if won:
        g["payout"] = g["bet"] * PAYOUT_MULT
        uid = g["uid"]
        D["wallets"][uid] = D["wallets"].get(uid, 0) + g["payout"]
        D["log"](uid, "win", g["payout"], f"Solitaire #{g['id']} won (x{PAYOUT_MULT})")   # saves the wallet
        stats["paid"] += g["payout"]
        stats["wins"] += 1
    _save(g)


def _expire(g):
    """A game that ran out of time is lost."""
    if g["status"] == "active" and time.time() - g["created"] > TIME_LIMIT:
        _finish(g, False)
        return True
    return False


# ---------- start ----------
def start_game(uid, bet):
    """Take the bet and deal. If the player already has an open game, show that one (no 2nd charge)."""
    g = games.get(uid)
    if g is not None and not _expire(g):
        return {"ok": True, "resumed": True, **view(g)}
    b = _int(bet)
    if b is None or not MIN_BET <= b <= MAX_BET:
        return {"ok": False, "error": f"Bet must be {MIN_BET} to {MAX_BET} ETB"}
    wallets = D["wallets"]
    if wallets.get(uid, 0) < b:
        return {"ok": False, "error": "Not enough balance / በቂ ሂሳብ የለዎትም", "balance": wallets.get(uid, 0)}
    wallets[uid] = wallets.get(uid, 0) - b
    used = min(b, D["turnover_left"](uid))                    # counts toward the play-through rule
    D["wagered"][uid] = D["wagered"].get(uid, 0) + used
    _last_id[0] = max(_last_id[0] + 1, int(time.time() * 1000))
    g = {"id": _last_id[0], "uid": uid, "bet": b, "s": new_deal(), "status": "active",
         "payout": 0, "created": int(time.time()), "ended": None}
    games[uid] = g
    stats["games"] += 1
    stats["bets"] += b
    D["log"](uid, "bet", -b, f"Solitaire #{g['id']}")          # saves the wallet
    _save(g)
    return {"ok": True, "resumed": False, **view(g)}


# ---------- moves ----------
def _parse_pile(txt):
    """'w' waste, 't0'..'t6' columns, 'f0'..'f3' foundations, 'f' = any foundation (target only)."""
    if not isinstance(txt, str):
        return None
    if txt == "w" or txt == "f":
        return txt, None
    m = re.fullmatch(r"([tf])(\d)", txt)
    if not m:
        return None
    i = int(m.group(2))
    if m.group(1) == "t" and i > 6 or m.group(1) == "f" and i > 3:
        return None
    return m.group(1), i


def draw(g):
    s = g["s"]
    if s["stock"]:
        for _ in range(min(DRAW, len(s["stock"]))):
            s["waste"].append(s["stock"].pop())
    elif s["waste"]:                                       # stock empty: turn the waste over again
        s["stock"] = s["waste"][::-1]
        s["waste"] = []
    else:
        return "Nothing to draw"
    return None


def move(g, src, dst, n):
    """Move n cards from src to dst. Returns an error text or None."""
    s = g["s"]
    a, b = _parse_pile(src), _parse_pile(dst)
    if a is None or b is None or a[0] == "f" and a[1] is None or b[0] == "w":
        return "Bad move"
    n = _int(n) or 1
    # 1) which cards are lifted
    if a[0] == "w":
        if not s["waste"] or n != 1:
            return "Bad move"
        cards = [s["waste"][-1]]
    elif a[0] == "f":
        if s["found"][a[1]] == 0 or n != 1:
            return "Bad move"
        cards = [a[1] * 13 + s["found"][a[1]] - 1]
    else:
        col = s["tab"][a[1]]
        if not 1 <= n <= len(col) or any(not up for _, up in col[len(col) - n:]):
            return "Bad move"
        cards = [c for c, _ in col[len(col) - n:]]
    top = cards[0]
    # 2) can they be put down there
    if b[0] == "f":
        if len(cards) != 1:
            return "Only one card goes to a foundation"
        j = suit(top) if b[1] is None else b[1]
        if suit(top) != j or rank(top) != s["found"][j]:
            return "That card can not go there"
    else:
        if a[0] == "t" and a[1] == b[1]:
            return "Bad move"
        dest = s["tab"][b[1]]
        if dest:
            if not dest[-1][1] or not _can_stack(top, dest[-1][0]):
                return "That card can not go there"
        elif rank(top) != 12:
            return "Only a King goes to an empty column"
    # 3) do it
    if a[0] == "w":
        s["waste"].pop()
    elif a[0] == "f":
        s["found"][a[1]] -= 1
    else:
        col = s["tab"][a[1]]
        del col[len(col) - n:]
        if col and not col[-1][1]:
            col[-1][1] = 1                                  # the card below turns face up
    if b[0] == "f":
        s["found"][suit(top) if b[1] is None else b[1]] += 1
    else:
        s["tab"][b[1]].extend([c, 1] for c in cards)
    return None


def act(uid, action, src=None, dst=None, n=1, seq=None):
    g = games.get(uid)
    if g is None:
        return {"ok": False, "error": "No open game", "none": True}
    if _expire(g):
        return {"ok": False, "error": "Time is over. The game was lost / ጊዜው አልቋል", "ended": True,
                "balance": D["wallets"].get(uid, 0)}
    if action == "quit":
        _finish(g, False)
        return {"ok": True, "ended": True, "status": "lost", "balance": D["wallets"].get(uid, 0)}
    if action in ("draw", "move") and seq is not None:      # retry-safe: a repeated request never acts twice
        sq, cur = _int(seq), g["s"]["moves"]
        if sq == cur:                                        # that move was already done (answer got lost)
            return {"ok": True, "dup": True, **view(g)}
        if sq != cur + 1:
            return {"ok": False, "sync": True, "error": "Board refreshed", **view(g)}
    if action == "draw":
        err = draw(g)
    elif action == "move":
        err = move(g, src, dst, n)
    else:
        err = "Bad move"
    if err:
        return {"ok": False, "error": err, **view(g)}
    g["s"]["moves"] += 1
    if _won(g):
        _finish(g, True)
    else:
        _save(g)
    return {"ok": True, **view(g)}


# ---------- what the phone may see ----------
def view(g):
    s = g["s"]
    return {
        "game": g["id"], "bet": g["bet"], "status": g["status"], "mult": PAYOUT_MULT,
        "win_amount": g["bet"] * PAYOUT_MULT, "payout": g["payout"], "draw": DRAW,
        "left": max(0, int(TIME_LIMIT - (time.time() - g["created"]))) if g["status"] == "active" else 0,
        "stock": len(s["stock"]),
        "waste": s["waste"][-3:] if DRAW == 3 else s["waste"][-1:],      # only the top cards are shown
        "waste_n": len(s["waste"]),
        "found": s["found"],
        "tab": [[[c if up else -1, up] for c, up in col] for col in s["tab"]],
        "moves": s["moves"],
        "balance": D["wallets"].get(g["uid"], 0),
    }


# ---------- web api ----------
async def _gate(data):
    user = D["verify"](data.get("initData", ""))
    if not user:
        return None, {"ok": False, "error": "auth"}
    uid = D["touch"](user)
    if uid in D["banned"]:
        return None, {"ok": False, "error": "blocked"}
    if not allowed(uid):
        return None, {"ok": False, "error": "closed", "msg": "Solitaire is not open yet"}
    return uid, None


async def handle_state(query):
    uid, err = await _gate(query)
    if err:
        return err
    base = {"min": MIN_BET, "max": MAX_BET, "mult": PAYOUT_MULT, "draw": DRAW,
            "minutes": TIME_LIMIT // 60, "balance": D["wallets"].get(uid, 0)}
    g = games.get(uid)
    if g is not None and not _expire(g):
        return {"ok": True, "active": True, **base, **view(g)}
    return {"ok": True, "active": False, **base}


async def handle_start(body):
    uid, err = await _gate(body)
    if err:
        return err
    if uid not in D["phones"]:
        return {"ok": False, "needPhone": True,
                "error": "Share your phone number first / መጀመሪያ ስልክ ቁጥርዎን ያጋሩ"}
    return start_game(uid, body.get("bet"))


async def handle_move(body):
    uid, err = await _gate(body)
    if err:
        return err
    return act(uid, body.get("action"), body.get("src"), body.get("dst"), body.get("n", 1), body.get("seq"))


def routes():
    from aiohttp import web

    async def _json(req):
        try:
            body = await req.json()
        except Exception:
            body = {}
        return body if isinstance(body, dict) else {}

    async def api_state(req):
        return web.json_response(await handle_state(dict(req.query)))

    async def api_start(req):
        return web.json_response(await handle_start(await _json(req)))

    async def api_move(req):
        return web.json_response(await handle_move(await _json(req)))

    return [web.get("/api/sol/state", api_state),
            web.post("/api/sol/start", api_start),
            web.post("/api/sol/move", api_move)]


# ---------- start-up and admin ----------
async def load_state(conn):
    """Load the open games again after a restart (bets already taken are not lost)."""
    rows = await conn.fetch("SELECT id, uid, bet, state, payout, created FROM sol_games WHERE status = 'active'")
    for r in rows:
        games[r["uid"]] = {"id": r["id"], "uid": r["uid"], "bet": r["bet"], "s": json.loads(r["state"]),
                           "status": "active", "payout": 0, "created": r["created"], "ended": None}
    mx = await conn.fetchval("SELECT COALESCE(MAX(id), 0) FROM sol_games")
    _last_id[0] = max(_last_id[0], mx or 0)
    row = await conn.fetchrow("SELECT COUNT(*) n, COALESCE(SUM(bet),0) b, COALESCE(SUM(payout),0) p, "
                              "COUNT(*) FILTER (WHERE status = 'won') w FROM sol_games")
    stats.update(games=row["n"], bets=int(row["b"]), paid=int(row["p"]), wins=row["w"])


def register(dp):
    from aiogram.filters import Command
    from aiogram.types import Message

    @dp.message(Command("solstatus"))
    async def cmd_solstatus(m: Message):
        if not (D.get("admin_id") and m.from_user.id == D["admin_id"]):
            return
        for g in list(games.values()):
            _expire(g)
        profit = stats["bets"] - stats["paid"]
        rate = (stats["wins"] * 100 // stats["games"]) if stats["games"] else 0
        await m.answer(f"🃏 Solitaire is {'OPEN to everyone' if ENABLED else 'OFF (admin only)'}\n"
                       f"Open games now: {len(games)}\n"
                       f"Games played: {stats['games']} · won {stats['wins']} ({rate}%)\n"
                       f"Bets taken: {stats['bets']} ETB · paid out: {stats['paid']} ETB\n"
                       f"Your profit: {profit} ETB\n"
                       f"Bet {MIN_BET}-{MAX_BET} · draw {DRAW} · {TIME_LIMIT // 60} min · win x{PAYOUT_MULT}")
