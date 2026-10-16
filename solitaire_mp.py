"""
Rodas Friend Zone QUICK MATCH  -  2 to 4 players, Solitaire race, the winner takes the prize.

How it works (why it is safe):
  * Money. The bet is taken when the player joins. The prize is paid ONLY from the pot of that
    match (prize = PRIZE_PERCENT of the pot, never more than the pot), the rest stays with the house.
    Taking a bet, a refund and paying the prize are plain functions with NO "await" inside (same rule
    as solitaire.py / numbergame.py), so they can never be interrupted halfway.
  * One open match per player. Pressing "Find match" twice never charges twice: the open match is shown again.
  * Cards. The SERVER shuffles ONE deal (secrets.SystemRandom, through solitaire.new_deal) and gives every
    player a private copy. Each player plays alone on his own board. The card rules are the rules of
    solitaire.py (imported, not copied). The phone only receives its OWN board (face-down cards = -1) and the
    card COUNT of the others. Every move is checked here.
  * Winner. First player with all 52 cards on the foundations wins at once. If nobody finishes, the player with
    the most cards on the foundations when the time is over wins; on a tie the one who reached that count first.
    If everybody has 0 cards on the foundations, everybody gets the bet back and the house takes no fee.
  * Waiting room. With 4 players the match starts at once. If 4 are not found within WAIT_SECONDS (counted from the
    first player) the match starts with the 2 or 3 players that are there (the prize is PRIZE_PERCENT of THEIR pot).
    With fewer than 2 players everybody is refunded automatically. A waiting player can cancel and gets the bet back.
  * Restart. The match and every board are saved in the database after every move. After a restart
    load_state() brings everything back and ticker() settles matches that ran out of time.
    A player who closes the app keeps playing until the timer ends.

Keep QM_ENABLED off until you are ready: then only the admin can play.
If QM_ENABLED is not set at all, it follows SOLITAIRE_ENABLED.

bot.py needs only these lines (details are in the delivery notes):
    import solitaire_mp as qm
    qm.register(dp)                          (next to sol.register(dp))
    *qm.routes(),                            (inside app.add_routes)
    await c.execute(qm.SCHEMA)               (next to sol.SCHEMA)
    qm.init(**ng.D)                          (next to sol.init(**ng.D))
    await qm.load_state(c)                   (next to sol.load_state)
    asyncio.create_task(qm.ticker())         (next to ng.ticker())
"""
import asyncio
import copy
import json
import os
import time

import solitaire as sol                       # the card / move rules are reused from here

ENABLED = os.getenv("QM_ENABLED", os.getenv("SOLITAIRE_ENABLED", "0")) == "1"


def _env_bets():
    out = []
    for x in os.getenv("QM_BETS", "50,100,200").split(","):
        x = x.strip()
        if x.isdigit() and int(x) > 0 and int(x) not in out:
            out.append(int(x))
    return tuple(sorted(out)) or (50, 100, 200)


BETS = _env_bets()                                              # one queue per bet, 50 is the minimum
PLAYERS = 4                                                     # most players in a match (the match starts at once when full)
MIN_PLAYERS = max(2, min(PLAYERS, int(os.getenv("QM_MIN_PLAYERS", "2"))))   # fewest players: when the wait time is over with at least this many, the match starts; fewer = refund
PRIZE_PERCENT = int(os.getenv("QM_PRIZE_PERCENT", "75"))        # winner gets this % of the pot, house keeps the rest
WAIT_SECONDS = int(os.getenv("QM_WAIT_SECONDS", "60"))          # not 4 players in this time = refund
TIME_LIMIT = int(os.getenv("QM_MINUTES", "6")) * 60             # play time of a match
REQUIRE_SEQ = os.getenv("QM_REQUIRE_SEQ", "1") == "1"           # move/auto must carry the move counter (retry-safe)
RESULT_KEEP = 60                                                # seconds the result stays available for a phone that reconnects
NOTIFY = os.getenv("QM_NOTIFY", "1") == "1"                     # Telegram message when a match starts / ends

D = {}                      # filled by init()
matches = {}                # match id -> match that is waiting or playing
by_uid = {}                 # uid -> the open match of that player
waiting = {}                # bet -> the one waiting match of that bet
recent = {}                 # uid -> {"until", "view"}: last result, for a phone that was away
stats = {"games": 0, "bets": 0, "paid": 0, "refunds": 0}
_last_id = [0]
_outbox = []                # Telegram messages, sent by ticker()

_int = sol._int

# ---------- database ----------
_MCOLS = ("id", "bet", "status", "created", "started", "ended", "pot", "prize", "fee", "pct", "winner", "reason")
_PCOLS = ("id", "match_id", "uid", "bet", "used", "board", "result", "payout", "cards", "joined", "ended")


def _upsert(table, cols):
    return ("INSERT INTO %s(%s) VALUES(%s) ON CONFLICT (id) DO UPDATE SET %s" % (
        table, ", ".join(cols), ", ".join("$%d" % (i + 1) for i in range(len(cols))),
        ", ".join("%s = $%d" % (c, i + 1) for i, c in enumerate(cols) if c != "id")))


_UP_MATCH = _upsert("qm_matches", _MCOLS)
_UP_PLAYER = _upsert("qm_players", _PCOLS)

SCHEMA = """
CREATE TABLE IF NOT EXISTS qm_matches(
    id BIGINT PRIMARY KEY,
    bet BIGINT NOT NULL,
    status TEXT NOT NULL,
    created BIGINT NOT NULL,
    started BIGINT,
    ended BIGINT,
    pot BIGINT NOT NULL DEFAULT 0,
    prize BIGINT NOT NULL DEFAULT 0,
    fee BIGINT NOT NULL DEFAULT 0,
    pct INT NOT NULL DEFAULT 75,
    winner BIGINT,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS qm_matches_status ON qm_matches(status);
CREATE TABLE IF NOT EXISTS qm_players(
    id BIGINT PRIMARY KEY,
    match_id BIGINT NOT NULL,
    uid BIGINT NOT NULL,
    bet BIGINT NOT NULL,
    used BIGINT NOT NULL DEFAULT 0,
    board TEXT,
    result TEXT NOT NULL DEFAULT 'pending',
    payout BIGINT NOT NULL DEFAULT 0,
    cards INT NOT NULL DEFAULT 0,
    joined BIGINT NOT NULL,
    ended BIGINT
);
CREATE INDEX IF NOT EXISTS qm_players_uid ON qm_players(uid, id);
CREATE INDEX IF NOT EXISTS qm_players_match ON qm_players(match_id);
"""


def init(**deps):
    D.update(deps)


def allowed(uid):
    return bool(ENABLED or (D.get("admin_id") and uid == D["admin_id"]))


def _now():
    return time.time()


def _new_id():
    _last_id[0] = max(_last_id[0] + 1, int(_now() * 1000))
    return _last_id[0]


def _name(uid):
    return str(D.get("names", {}).get(uid) or "Player")[:24]


def prize_for(bet, n=None):
    """(pot, prize, percent) for n players (default: a full match). The prize is never more than the pot."""
    pot = bet * (n or PLAYERS)
    pct = max(0, min(100, PRIZE_PERCENT))
    return pot, min(pot, pot * pct // 100), pct


# ---------- saving ----------
def _save_match(m):
    D["enqueue"](_UP_MATCH, m["id"], m["bet"], m["status"], m["created"], m["started"], m["ended"],
                 m["pot"], m["prize"], m["fee"], m["pct"], m["winner"], m["reason"])


def _save_player(m, p):
    board = None
    if p["s"] is not None:
        board = json.dumps({"s": p["s"], "first": p["first"], "done": p["done"]})
    D["enqueue"](_UP_PLAYER, p["eid"], m["id"], p["uid"], p["bet"], p["used"], board, p["result"],
                 p["payout"], p["cards"], p["joined"], p["ended"])


def _notify(uid, text):
    if NOTIFY and D.get("send"):
        _outbox.append((uid, text))


# ---------- helpers ----------
def _new_match(bet):
    pot, prize, pct = prize_for(bet)
    return {"id": _new_id(), "bet": bet, "status": "waiting", "created": int(_now()), "started": None,
            "ended": None, "pot": pot, "prize": prize, "fee": pot - prize, "pct": pct, "winner": None,
            "reason": None, "players": []}


def _new_player(uid, bet, used):
    return {"eid": _new_id(), "uid": uid, "bet": bet, "used": used, "joined": int(_now()), "s": None,
            "first": None, "done": False, "result": "pending", "payout": 0, "cards": 0, "ended": None}


def _mine(m, uid):
    for p in m["players"]:
        if p["uid"] == uid:
            return p
    return None


def _cards(p):
    return sum(p["s"]["found"]) if p["s"] else 0


def _rank_key(p, seat):
    """Best first: most cards, then the one who reached that count first, then the one who joined first."""
    c = p["cards"]
    t = (p["first"][c - 1] if c and p["first"] else 0) or float("inf")
    return (-c, t, seat)


def _ranked(m):
    return [p for _, p in sorted(enumerate(m["players"]), key=lambda x: _rank_key(x[1], x[0]))]


def _left(m):
    now = _now()
    if m["status"] == "waiting":
        return max(0, int(WAIT_SECONDS - (now - m["created"])))
    if m["status"] == "active":
        return max(0, int(TIME_LIMIT - (now - m["started"])))
    return 0


FAIR_BET = 0        # solitaire.py makes a game "hard" when bet > HARD_FROM. In Quick Match every bet must get the SAME
                    # limits, so the rules are always asked with this neutral bet: normal passes and moves for everybody.


def _shim(m, p):
    """The small dict solitaire.py wants: {"s": board, "bet": ...}. The bet is neutral (see FAIR_BET)."""
    return {"s": p["s"], "bet": FAIR_BET}


def _progress(p):
    """After an accepted move: remember when each card count was reached for the first time (tie-break)."""
    c, old = _cards(p), p["cards"]
    if c > old:
        t = _now()
        for n in range(old, c):
            if not p["first"][n]:
                p["first"][n] = t
    p["cards"] = c


# ---------- money: NO "await" may ever be added inside the functions below ----------
def _pay_back(m, p, why):
    """Give one player his bet back (and undo the play-through this bet added). Only once per entry."""
    if p["result"] != "pending":
        return
    uid = p["uid"]
    D["wallets"][uid] = D["wallets"].get(uid, 0) + p["bet"]
    D["wagered"][uid] = max(0, D["wagered"].get(uid, 0) - p["used"])
    p["result"], p["payout"], p["ended"] = "refund", p["bet"], int(_now())
    p["cards"] = _cards(p)
    D["log"](uid, "refund", p["bet"], f"Quick Match #{m['id']} refunded ({why})")      # saves the wallet
    _save_player(m, p)


def _close(m):
    """The match leaves the memory; its players are free to join another one."""
    matches.pop(m["id"], None)
    if waiting.get(m["bet"]) is m:
        del waiting[m["bet"]]
    for p in m["players"]:
        if by_uid.get(p["uid"]) is m:
            del by_uid[p["uid"]]


def _refund_match(m, reason):
    """Waiting match that did not fill up, or a match where nobody got a card to the foundations: everybody gets
    the bet back, the house takes NO fee."""
    if m["status"] not in ("waiting", "active"):
        return
    m["status"], m["ended"], m["reason"], m["prize"], m["fee"] = "refunded", int(_now()), reason, 0, 0
    _close(m)
    for p in m["players"]:
        _pay_back(m, p, reason)
    _save_match(m)
    stats["refunds"] += 1
    for p in m["players"]:
        recent[p["uid"]] = {"until": _now() + RESULT_KEEP, "view": _result(m, p["uid"])}
        _notify(p["uid"], f"↩ Quick Match #{m['id']} was cancelled. Your {p['bet']} ETB was refunded. / ገንዘብዎ ተመልሷል")


def _settle(m, reason, winner=None):
    """Close a playing match ONCE and pay the winner from the pot."""
    if m["status"] != "active":
        return
    for p in m["players"]:
        p["cards"] = _cards(p)
    if winner is None:
        winner = _ranked(m)[0]
        if winner["cards"] <= 0:                          # nobody got a card to the foundations
            _refund_match(m, "nocards")
            return
    pot = m["pot"]
    prize = max(0, min(pot, m["prize"]))                  # never more than the pot
    now = int(_now())
    m["status"], m["ended"], m["reason"] = "done", now, reason
    m["winner"], m["prize"], m["fee"] = winner["uid"], prize, pot - prize
    _close(m)
    for p in m["players"]:
        p["ended"] = now
        p["result"], p["payout"] = ("won", prize) if p is winner else ("lost", 0)
    wid = winner["uid"]
    D["wallets"][wid] = D["wallets"].get(wid, 0) + prize
    D["log"](wid, "win", prize, f"Quick Match #{m['id']} won")                          # saves the wallet
    stats["games"] += 1
    stats["bets"] += pot
    stats["paid"] += prize
    for p in m["players"]:
        _save_player(m, p)
    _save_match(m)
    for p in m["players"]:
        recent[p["uid"]] = {"until": _now() + RESULT_KEEP, "view": _result(m, p["uid"])}
        if p is winner:
            _notify(p["uid"], f"🏆 Quick Match #{m['id']}: you won {prize} ETB! / አሸንፈዋል!")
        else:
            _notify(p["uid"], f"Quick Match #{m['id']} is over. Winner: {_name(wid)} ({winner['cards']} cards). "
                              f"Your cards: {p['cards']}.")


def _start(m):
    """The players are here (2 to 4): one shuffled deal, a private copy for everybody, the pot and the prize are fixed
    for the players that really play, and the clock starts."""
    pot, prize, pct = prize_for(m["bet"], len(m["players"]))
    m["pot"], m["prize"], m["fee"], m["pct"] = pot, prize, pot - prize, pct
    deal = sol.new_deal()
    for p in m["players"]:
        p["s"], p["first"], p["done"], p["cards"] = copy.deepcopy(deal), [0.0] * 52, False, 0
    m["status"], m["started"] = "active", int(_now())
    if waiting.get(m["bet"]) is m:
        del waiting[m["bet"]]
    for p in m["players"]:
        _save_player(m, p)
    _save_match(m)
    for p in m["players"]:
        _notify(p["uid"], f"⚡ Quick Match #{m['id']} started with {len(m['players'])} players! You have {TIME_LIMIT // 60} minutes. Open the app. / ጨዋታው ተጀምሯል")


def _check(m):
    """Close a match whose time is over. Returns True if it was closed."""
    now = _now()
    if m["status"] == "waiting" and now - m["created"] >= WAIT_SECONDS:
        if len(m["players"]) >= MIN_PLAYERS:
            _start(m)                                         # 2 or 3 players are enough: no need to wait for the 4th
            return False
        _refund_match(m, "waiting")
        return True
    if m["status"] == "active" and now - m["started"] >= TIME_LIMIT:
        _settle(m, "time")
        return True
    return False


# ---------- join / cancel ----------
def join(uid, bet):
    """Take the bet and put the player in the waiting room of that bet. If he already has an open match, that
    match is shown again (no second charge)."""
    m = by_uid.get(uid)
    if m is not None:
        _check(m)
        if by_uid.get(uid) is not None:
            return {"ok": True, "resumed": True, **state(uid)}
    b = _int(bet)
    if b not in BETS:
        return {"ok": False, "error": "Bet must be " + " / ".join(str(x) for x in BETS) + " ETB"}
    wallets = D["wallets"]
    if wallets.get(uid, 0) < b:
        return {"ok": False, "error": "Not enough balance / በቂ ሂሳብ የለዎትም", "balance": wallets.get(uid, 0)}
    m = waiting.get(b)
    if m is not None:
        _check(m)                                             # may refund it, or start it (2+ players, time over)
        if m["status"] != "waiting":
            m = None
    if m is None:
        m = _new_match(b)
        matches[m["id"]] = m
        waiting[b] = m
        _save_match(m)
    used = min(b, D["turnover_left"](uid))                      # counts toward the play-through rule
    recent.pop(uid, None)                                       # an old result must not show during the new match
    p = _new_player(uid, b, used)
    m["players"].append(p)
    by_uid[uid] = m
    wallets[uid] = wallets.get(uid, 0) - b
    D["wagered"][uid] = D["wagered"].get(uid, 0) + used
    D["log"](uid, "bet", -b, f"Quick Match #{m['id']}")         # saves the wallet
    _save_player(m, p)
    if len(m["players"]) >= PLAYERS:
        _start(m)
    return {"ok": True, "resumed": False, **state(uid)}


def cancel(uid):
    """Leave the waiting room and get the bet back. Not possible once the match has started."""
    m = by_uid.get(uid)
    if m is None:
        return {"ok": False, "error": "No open match", "none": True, **_late(uid)}
    if _check(m):
        return {"ok": False, "error": "The match is over", **_late(uid)}
    if m["status"] != "waiting":
        return {"ok": False, "error": "The match has already started"}
    p = _mine(m, uid)
    m["players"].remove(p)
    del by_uid[uid]
    _pay_back(m, p, "cancelled")
    if not m["players"]:
        m["status"], m["ended"], m["reason"], m["prize"], m["fee"] = "refunded", int(_now()), "cancel", 0, 0
        _close(m)
    _save_match(m)
    return {"ok": True, "cancelled": True, "balance": D["wallets"].get(uid, 0)}


# ---------- playing ----------
def _late(uid):
    """Answer for a phone that talks to a match that is already over."""
    r = recent.get(uid)
    return {"ended": True, "result": r["view"]} if r and r["until"] >= _now() else {"none": True}


def act(uid, action, src=None, dst=None, n=1, seq=None):
    m = by_uid.get(uid)
    if m is None:
        return {"ok": False, "error": "No open match", **_late(uid)}
    if _check(m):
        return {"ok": False, "error": "The match is over / ጨዋታው አልቋል", **_late(uid)}
    if m["status"] != "active":
        return {"ok": False, "error": "The match has not started yet", "wait": True}
    p = _mine(m, uid)
    if action not in ("draw", "move", "auto"):
        return {"ok": False, "error": "Bad move", **_view(m, p)}
    if p["done"]:
        return {"ok": False, "error": "Your board is finished. Wait for the match to end", **_view(m, p)}
    if seq is None and REQUIRE_SEQ and action in ("move", "auto"):
        return {"ok": False, "sync": True, "error": "Board refreshed", **_view(m, p)}
    if seq is not None:                                          # retry-safe: a repeated request never acts twice
        sq, cur = _int(seq), p["s"]["moves"]
        if sq == cur:
            return {"ok": True, "dup": True, **_view(m, p)}
        if sq != cur + 1:
            return {"ok": False, "sync": True, "error": "Board refreshed", **_view(m, p)}
    g = _shim(m, p)
    if action == "draw":
        err = sol.draw(g)
    elif action == "move":
        err = sol.move(g, src, dst, n)
    else:
        err = sol._auto(g)
    if err:
        return {"ok": False, "error": err, **_view(m, p)}
    p["s"]["moves"] += 1
    _progress(p)
    if sol._won(g):                                              # all 52 cards on the foundations: this player wins now
        p["done"] = True
        _settle(m, "all", p)
        return {"ok": True, "ended": True, "result": _result(m, uid), **_view(m, p)}
    if p["s"]["moves"] >= sol._lim(g)[1]:                        # move limit reached: this board is finished
        p["done"] = True
        _save_player(m, p)
        if all(q["done"] for q in m["players"]):
            _settle(m, "moves")
            if m["status"] != "active":
                return {"ok": True, "ended": True, "result": _result(m, uid), **_view(m, p)}
        return {"ok": True, "reason": "moves", **_view(m, p)}
    _save_player(m, p)
    return {"ok": True, **_view(m, p)}


# ---------- what the phone may see ----------
def _scoreboard(m, uid):
    out = []
    for i, p in enumerate(_ranked(m)):
        out.append({"rank": i + 1, "name": _name(p["uid"]), "cards": p["cards"], "me": p["uid"] == uid,
                    "done": p["done"]})
    return out


def _view(m, p, light=False):
    """The own board (face-down cards are -1) + the card count of everybody. Never the boards of the others."""
    g = {"id": m["id"], "uid": p["uid"], "bet": FAIR_BET, "s": p["s"], "status": "active", "payout": 0,
         "created": m["started"] or m["created"]}
    v = sol.view(g)
    for k in ("mult", "win_amount", "payout", "game", "hard"):
        v.pop(k, None)
    v.update(match=m["id"], bet=m["bet"], status=m["status"], left=_left(m), prize=m["prize"], pot=m["pot"],
             done=p["done"], players=_scoreboard(m, p["uid"]))
    if light:
        for k in ("stock", "waste", "waste_n", "found", "tab"):
            v.pop(k, None)
    return v


def _result(m, uid):
    mine = _mine(m, uid)
    done = m["status"] == "done"
    win = next((p for p in m["players"] if p["uid"] == m["winner"]), None) if done else None
    return {"id": m["id"], "status": m["status"], "reason": m["reason"], "bet": m["bet"], "pot": m["pot"],
            "prize": m["prize"] if done else 0, "winner": _name(win["uid"]) if win else None,
            "winner_me": bool(win and win["uid"] == uid),
            "mine": {"result": mine["result"], "payout": mine["payout"], "cards": mine["cards"]} if mine else None,
            "board": [{"rank": i + 1, "name": _name(p["uid"]), "cards": p["cards"], "me": p["uid"] == uid,
                       "result": p["result"]} for i, p in enumerate(_ranked(m))]}


def _queues():
    out = []
    for b in BETS:
        m = waiting.get(b)
        pot, prize, _ = prize_for(b)
        out.append({"bet": b, "pot": pot, "prize": prize, "n": len(m["players"]) if m else 0,
                    "left": _left(m) if m else WAIT_SECONDS})
    return out


def _cfg():
    pct = prize_for(BETS[0])[2]
    return {"bets": list(BETS), "players": PLAYERS, "min_players": MIN_PLAYERS, "wait": WAIT_SECONDS, "minutes": TIME_LIMIT // 60,
            "pct": pct, "draw": sol.DRAW, "passes": sol.PASSES_NORMAL, "moves": sol.MOVES_NORMAL}


def state(uid, light=False):
    m = by_uid.get(uid)
    if m is not None:
        _check(m)
        m = by_uid.get(uid)
    out = {"ok": True, "balance": D["wallets"].get(uid, 0), "cfg": _cfg(), "queues": _queues(), "phase": "idle"}
    r = recent.get(uid)
    if r is not None and r["until"] >= _now():
        out["result"] = r["view"]
    if m is not None and m["status"] == "waiting":
        out["phase"] = "waiting"
        out["m"] = {"id": m["id"], "bet": m["bet"], "n": len(m["players"]), "need": PLAYERS, "left": _left(m),
                    "min": MIN_PLAYERS, "prize": prize_for(m["bet"], len(m["players"]))[1],
                    "pot": prize_for(m["bet"], len(m["players"]))[0], "max_prize": prize_for(m["bet"])[1],
                    "names": [_name(p["uid"]) for p in m["players"]]}
    elif m is not None:
        out["phase"] = "active"
        out["m"] = _view(m, _mine(m, uid), light)
    return out


# ---------- web api ----------
async def _gate(data):
    user = D["verify"](data.get("initData", ""))
    if not user:
        return None, {"ok": False, "error": "auth"}
    uid = D["touch"](user)
    if uid in D["banned"]:
        return None, {"ok": False, "error": "blocked"}
    if not allowed(uid):
        return None, {"ok": False, "error": "closed", "msg": "Quick Match is not open yet"}
    return uid, None


async def handle_state(query):
    uid, err = await _gate(query)
    if err:
        return err
    return state(uid, light=str(query.get("light", "")) == "1")


async def handle_join(body):
    uid, err = await _gate(body)
    if err:
        return err
    if uid not in D["phones"]:
        return {"ok": False, "needPhone": True,
                "error": "Share your phone number first / መጀመሪያ ስልክ ቁጥርዎን ያጋሩ"}
    return join(uid, body.get("bet"))


async def handle_cancel(body):
    uid, err = await _gate(body)
    if err:
        return err
    return cancel(uid)


async def handle_move(body):
    uid, err = await _gate(body)
    if err:
        return err
    return act(uid, body.get("action"), body.get("src"), body.get("dst"), body.get("n", 1), body.get("seq"))


async def handle_history(query):
    """Last 30 finished matches of this player + the open match (if any) + totals."""
    uid, err = await _gate(query)
    if err:
        return err
    rows = []
    m = by_uid.get(uid)
    if m is not None:
        _check(m)
        m = by_uid.get(uid)
    if m is not None:
        p = _mine(m, uid)
        rows.append({"id": m["id"], "bet": p["bet"], "result": "active", "payout": 0, "cards": _cards(p),
                     "t": p["joined"]})
    total = {"games": 0, "won": 0, "bets": 0, "paid": 0}
    pool = D.get("pool")
    if pool is not None:
        try:
            async with pool.acquire() as c:
                for r in await c.fetch("SELECT match_id, bet, result, payout, cards, joined, ended FROM qm_players "
                                       "WHERE uid = $1 AND result <> 'pending' ORDER BY id DESC LIMIT 30", uid):
                    rows.append({"id": r["match_id"], "bet": int(r["bet"]), "result": r["result"],
                                 "payout": int(r["payout"]), "cards": r["cards"], "t": int(r["ended"] or r["joined"])})
                t = await c.fetchrow("SELECT COUNT(*) FILTER (WHERE result IN ('won','lost')) n, "
                                     "COUNT(*) FILTER (WHERE result = 'won') w, "
                                     "COALESCE(SUM(bet) FILTER (WHERE result IN ('won','lost')), 0) b, "
                                     "COALESCE(SUM(payout) FILTER (WHERE result = 'won'), 0) p "
                                     "FROM qm_players WHERE uid = $1", uid)
                total = {"games": t["n"], "won": t["w"], "bets": int(t["b"]), "paid": int(t["p"])}
        except Exception:
            return {"ok": False, "error": "History is not available now"}
    return {"ok": True, "rows": rows, "total": total}


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

    async def api_history(req):
        return web.json_response(await handle_history(dict(req.query)))

    async def api_join(req):
        return web.json_response(await handle_join(await _json(req)))

    async def api_cancel(req):
        return web.json_response(await handle_cancel(await _json(req)))

    async def api_move(req):
        return web.json_response(await handle_move(await _json(req)))

    return [web.get("/api/qm/state", api_state),
            web.get("/api/qm/history", api_history),
            web.post("/api/qm/join", api_join),
            web.post("/api/qm/cancel", api_cancel),
            web.post("/api/qm/move", api_move)]


# ---------- ticker, start-up and admin ----------
def tick():
    """Once a second: refund waiting rooms that did not fill up, settle matches whose time is over."""
    for m in list(matches.values()):
        try:
            _check(m)
        except Exception as e:
            print("quick match check error:", m.get("id"), repr(e))
    now = _now()
    for uid in [u for u, r in recent.items() if r["until"] < now]:
        del recent[uid]


async def _flush_outbox():
    sent = 0
    while _outbox and sent < 20:
        uid, text = _outbox.pop(0)
        sent += 1
        try:
            await D["send"](uid, text)
        except Exception:
            pass                                              # a player who blocked the bot must not stop anything


async def ticker():
    """bot.py starts this once: asyncio.create_task(qm.ticker())"""
    while True:
        await asyncio.sleep(1)
        try:
            tick()
            await _flush_outbox()
        except Exception as e:
            print("quick match tick error:", repr(e))


async def load_state(conn):
    """Bring waiting and playing matches back after a restart (bets already taken are not lost)."""
    rows = await conn.fetch("SELECT id, bet, status, created, started, pot, prize, fee, pct FROM qm_matches "
                            "WHERE status IN ('waiting', 'active') ORDER BY id")
    for r in rows:
        matches[r["id"]] = {"id": r["id"], "bet": r["bet"], "status": r["status"], "created": r["created"],
                            "started": r["started"], "ended": None, "pot": r["pot"], "prize": r["prize"],
                            "fee": r["fee"], "pct": r["pct"], "winner": None, "reason": None, "players": []}
    ids = list(matches)
    if ids:
        for r in await conn.fetch("SELECT id, match_id, uid, bet, used, board, joined FROM qm_players "
                                  "WHERE result = 'pending' AND match_id = ANY($1::bigint[]) ORDER BY id", ids):
            m = matches.get(r["match_id"])
            if m is None:
                continue
            p = _new_player(r["uid"], r["bet"], r["used"])
            p["eid"], p["joined"] = r["id"], r["joined"]
            if r["board"]:
                b = json.loads(r["board"])
                p["s"], p["first"], p["done"] = b["s"], b.get("first") or [0.0] * 52, bool(b.get("done"))
                p["cards"] = _cards(p)
            m["players"].append(p)
            by_uid[p["uid"]] = m
    mx = await conn.fetchval("SELECT GREATEST(COALESCE((SELECT MAX(id) FROM qm_matches), 0), "
                             "COALESCE((SELECT MAX(id) FROM qm_players), 0))")
    _last_id[0] = max(_last_id[0], mx or 0)
    row = await conn.fetchrow("SELECT COUNT(*) FILTER (WHERE status = 'done') n, "
                              "COALESCE(SUM(pot) FILTER (WHERE status = 'done'), 0) b, "
                              "COALESCE(SUM(prize) FILTER (WHERE status = 'done'), 0) p, "
                              "COUNT(*) FILTER (WHERE status = 'refunded' AND reason <> 'cancel') r "
                              "FROM qm_matches")
    stats.update(games=row["n"], bets=int(row["b"]), paid=int(row["p"]), refunds=row["r"])
    for m in list(matches.values()):
        if m["status"] == "waiting":
            if len(m["players"]) >= PLAYERS:
                _start(m)                                     # it was full when the bot stopped: start it now
            elif not m["players"]:
                m["status"], m["ended"], m["reason"] = "refunded", int(_now()), "cancel"
                _close(m)
                _save_match(m)
            elif m["bet"] not in waiting:
                waiting[m["bet"]] = m
        elif len(m["players"]) < MIN_PLAYERS or any(p["s"] is None for p in m["players"]):
            _refund_match(m, "restart")                       # a broken match is never kept: everybody is refunded


def register(dp):
    from aiogram.filters import Command
    from aiogram.types import Message

    @dp.message(Command("qmstatus"))
    async def cmd_qmstatus(m: Message):
        if not (D.get("admin_id") and m.from_user.id == D["admin_id"]):
            return
        for x in list(matches.values()):
            _check(x)
        playing = [x for x in matches.values() if x["status"] == "active"]
        wait_txt = ", ".join(f"{b}: {len(waiting[b]['players'])}/{PLAYERS}" for b in BETS if b in waiting) or "none"
        lines = [f"⚡ Quick Match is {'OPEN to everyone' if ENABLED else 'OFF (admin only)'}",
                 f"Playing now: {len(playing)} matches · waiting rooms: {wait_txt}"]
        profit = stats["bets"] - stats["paid"]
        lines += [f"Matches played: {stats['games']} · cancelled / refunded: {stats['refunds']}",
                  f"Bets taken: {stats['bets']} ETB · prizes paid: {stats['paid']} ETB",
                  f"Your profit: {profit} ETB",
                  f"Bets {'/'.join(str(b) for b in BETS)} · {MIN_PLAYERS}-{PLAYERS} players · prize {max(0, min(100, PRIZE_PERCENT))}% of the pot"
                  f" · {TIME_LIMIT // 60} min · wait {WAIT_SECONDS}s · draw {sol.DRAW}"]
        await m.answer("\n".join(lines))
