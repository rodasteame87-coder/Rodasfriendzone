"""
Rodas Friend Zone 8-BALL  -  real money, 2 players (1 v 1) or 4 players (2 v 2 teams).

Money (same safety rules as solitaire_mp.py / numbergame.py):
  * The bet is taken when the player joins. The prize is paid ONLY from the pot of that match
    (prize = EB_PRIZE_PERCENT of the pot, never more than the pot). In a 2 v 2 match the prize is
    split between the two winners. The rest stays with the house.
  * Taking a bet, a refund and paying the prize are plain functions with NO "await" inside, so they
    can never be interrupted halfway. A refund / payout happens ONCE per player (result != "pending").
  * One open match per player: joining twice never charges twice, the open match is shown again.
  * Nobody joins in EB_WAIT_SECONDS, the match takes too long, or the server restarts: everybody
    gets the bet back and the house takes no fee.
  * A player who lets the shot timer run out 3 times in a row forfeits: the other team wins.

Cheat protection: the SERVER owns the table. The phone sends only aim, power, spin (and the cue ball
spot when it has ball-in-hand). The server runs the physics (poolgame.simulate), judges the rules and
pays. Rules: solids (1-7) and stripes (9-15), the table is open until the first legal pocket, the
8-ball goes in last, fouls give the next player ball-in-hand, 8-ball early / on a foul = loss.

Keep EB_ENABLED off until you are ready: then only the admin can play.

bot.py needs only these lines:
    import eightball as eb
    eb.register(dp)                          (next to pg.register(dp))
    *eb.routes(),                            (inside app.add_routes)
    await c.execute(eb.SCHEMA)               (next to pg.SCHEMA)
    eb.init(**ng.D)                          (next to qm.init(**ng.D))
    await eb.load_state(c)                   (next to qm.load_state)
    asyncio.create_task(eb.ticker())         (next to qm.ticker())
"""
import asyncio
import json
import math
import os
import random
import time
from pathlib import Path

import poolgame as pg                      # physics and table size only (no money code is used from there)


def _env_int(k, d):
    try:
        return int(os.getenv(k, d))
    except ValueError:
        return int(d)


ENABLED = os.getenv("EB_ENABLED", "0") == "1"                       # off = only the admin can play
BETS = sorted({int(x) for x in os.getenv("EB_BETS", "50,100,200").split(",")
               if x.strip().isdigit() and int(x) >= 10}) or [50]
PRIZE_PERCENT = max(0, min(100, _env_int("EB_PRIZE_PERCENT", 90)))   # winners share this % of the pot
WAIT_SECONDS = _env_int("EB_WAIT_SECONDS", 90)                       # table not full in this time = refund
MATCH_SECONDS = _env_int("EB_MATCH_MINUTES", 30) * 60                # longer than this = refund
SHOT_SECONDS = pg.SHOT_SECONDS
MAX_TIMEOUTS = 3
START_DELAY = 5
KEEP_DONE = 120

D = {}                       # filled by init()
rooms, by_uid, waiting = {}, {}, {}
stats = {"matches": 0, "bets": 0, "paid": 0, "refunds": 0, "shots": 0}
_last_id = [0]
_rng = random.SystemRandom()

SCHEMA = """
CREATE TABLE IF NOT EXISTS eb_matches(id BIGINT PRIMARY KEY, bet INT NOT NULL, n INT NOT NULL, status TEXT NOT NULL,
  created BIGINT NOT NULL, ended BIGINT NOT NULL DEFAULT 0, pot INT NOT NULL DEFAULT 0, prize INT NOT NULL DEFAULT 0,
  fee INT NOT NULL DEFAULT 0, win_team INT NOT NULL DEFAULT -1, reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS eb_players(id BIGINT PRIMARY KEY, match_id BIGINT NOT NULL, uid BIGINT NOT NULL,
  bet INT NOT NULL, used INT NOT NULL DEFAULT 0, seat INT NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT 'pending',
  payout INT NOT NULL DEFAULT 0);
"""
_MC = ("id", "bet", "n", "status", "created", "ended", "pot", "prize", "fee", "win_team", "reason")
_PC = ("id", "match_id", "uid", "bet", "used", "seat", "result", "payout")


def _up(table, cols):
    return (f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('$%d' % (i + 1) for i in range(len(cols)))}) "
            f"ON CONFLICT (id) DO UPDATE SET " + ",".join(f"{c}=EXCLUDED.{c}" for c in cols[1:]))


_UP_M, _UP_P = _up("eb_matches", _MC), _up("eb_players", _PC)


def init(**deps):
    D.update(deps)


def now():
    return time.time()


def allowed(uid):
    return bool(ENABLED or (D.get("admin_id") and uid == D["admin_id"]))


def _new_id():
    _last_id[0] = max(_last_id[0] + 1, int(now() * 1000))
    return _last_id[0]


def pot_prize(bet, n):
    pot = bet * n
    return pot, pot * PRIZE_PERCENT // 100


def _cfg(bet, n, uid=None):
    if bet not in BETS or n not in (2, 4):
        return {"bet": bet, "n": n, "prize": 0, "each": 0, "balance": D.get("wallets", {}).get(uid, 0) if uid else 0}
    pot, prize = pot_prize(bet, n)
    return {"bet": bet, "n": n, "prize": prize, "each": prize // (n // 2),
            "balance": D.get("wallets", {}).get(uid, 0) if uid else 0}


# ---------------------------------------------------------------- saving
def _save_match(room, status, reason="", win_team=-1, pot=0, prize=0, fee=0, ended=0):
    D["enqueue"](_UP_M, room.id, room.bet, room.n, status, int(room.created), int(ended), pot, prize, fee, win_team, reason)


def _save_player(room, p, seat):
    D["enqueue"](_UP_P, p["eid"], room.id, p["uid"], p["bet"], p["used"], seat, p["result"], p["payout"])


# ---------------------------------------------------------------- money (no await anywhere in here)
def _pay_back(room, p, why):
    """One player gets the bet back (and the play-through it added is undone). Only once."""
    if p["result"] != "pending":
        return
    uid = p["uid"]
    D["wallets"][uid] = D["wallets"].get(uid, 0) + p["bet"]
    D["wagered"][uid] = max(0, D["wagered"].get(uid, 0) - p["used"])
    p["result"], p["payout"] = "refund", p["bet"]
    D["log"](uid, "refund", p["bet"], f"8-Ball #{room.id} refunded ({why})")          # saves the wallet
    stats["refunds"] += 1


def _close(room):
    for p in room.seats:
        if p and by_uid.get(p["uid"]) is room:
            del by_uid[p["uid"]]
    if waiting.get((room.bet, room.n)) is room:
        del waiting[(room.bet, room.n)]
    room.finished_at = now()


def _make_result(room, win_team, text, sub, refund=False):
    order = sorted(range(room.n), key=lambda i: (win_team is None or i % 2 != win_team, i))
    players = []
    for i in order:
        p = room.seats[i]
        if p:
            players.append({"seat": i, "name": p["name"], "score": _label(room, i), "pocketed": p["pocketed"],
                            "fouls": p["fouls"], "payout": p["payout"]})
    wins = [] if win_team is None else [i for i in range(room.n) if i % 2 == win_team]
    room.result = {"winner": wins[0] if wins else -1, "wins": wins, "players": players, "text": text, "sub": sub,
                   "refund": refund}


def _refund_all(room, why):
    if room.phase not in ("waiting", "starting", "playing"):
        return
    room.phase = "finished"
    for i, p in enumerate(room.seats):
        if p:
            _pay_back(room, p, why)
            _save_player(room, p, i)
    _save_match(room, "refunded", why, ended=now())
    _make_result(room, None, "Match cancelled", "Everybody got the bet back (" + why + ")", True)
    _close(room)


def _settle(room, win_team, reason):
    """Close a playing match ONCE and pay the winners from the pot."""
    if room.phase != "playing":
        return
    room.phase = "finished"
    pot, prize = pot_prize(room.bet, room.n)
    winners = [i for i in range(room.n) if i % 2 == win_team]
    each = prize // len(winners)
    for i, p in enumerate(room.seats):
        if i in winners:
            uid = p["uid"]
            D["wallets"][uid] = D["wallets"].get(uid, 0) + each
            D["log"](uid, "win", each, f"8-Ball #{room.id} won")                      # saves the wallet
            p["result"], p["payout"] = "won", each
        else:
            p["result"], p["payout"] = "lost", 0
        _save_player(room, p, i)
    fee = pot - each * len(winners)
    _save_match(room, "done", reason, win_team, pot, each * len(winners), fee, now())
    stats["matches"] += 1
    stats["bets"] += pot
    stats["paid"] += each * len(winners)
    names = " & ".join(room.seats[i]["name"] for i in winners)
    _make_result(room, win_team, f"{names} win{'s' if len(winners) == 1 else ''} {each} ETB" + (" each" if len(winners) > 1 else ""),
                 {"8ball": "The 8-ball decided the match", "foul8": "The 8-ball was pocketed too early or on a foul",
                  "forfeit": "The other team ran out of time 3 times"}.get(reason, reason))
    _close(room)


# ---------------------------------------------------------------- rooms
class Room:
    def __init__(self, rid, bet, n):
        self.id, self.bet, self.n = rid, bet, n
        self.seats = [None] * n
        self.conns = {}
        self.phase = "waiting"
        self.created = now()
        self.start_at = self.started = self.shot_deadline = self.busy_until = self.finished_at = 0.0
        self.turn, self.seq, self.bih = 0, 0, False
        self.balls = {}
        self.groups = {0: None, 1: None}
        self.result = None


def seat_of(room, uid):
    for i, p in enumerate(room.seats):
        if p and p["uid"] == uid:
            return i
    return None


def _label(room, seat):
    g = room.groups[seat % 2]
    lab = "Open" if g is None else ("Solids" if g == "solid" else "Stripes")
    return (("Team A · " if seat % 2 == 0 else "Team B · ") + lab) if room.n == 4 else lab


def _kind(n):
    return "solid" if 1 <= n <= 7 else ("stripe" if 9 <= n <= 15 else None)


def _left(room, team):
    """Balls of this team's group still on the table (99 while the table is open)."""
    g = room.groups[team]
    if g is None:
        return 99
    return sum(1 for n in room.balls if n and _kind(n) == g)


def join(uid, name, photo, bet, n):
    """Returns (room, None) or (None, error text). Takes the bet. Never charges twice."""
    room = by_uid.get(uid)
    if room is not None and room.phase in ("waiting", "starting", "playing"):
        return room, None
    if bet not in BETS or n not in (2, 4):
        return None, "Bet must be " + " / ".join(str(b) for b in BETS) + " ETB, 2 or 4 players"
    if D["wallets"].get(uid, 0) < bet:
        return None, "Not enough balance / በቂ ሂሳብ የለዎትም"
    room = waiting.get((bet, n))
    if room is None or room.phase != "waiting" or None not in room.seats:
        room = Room(_new_id(), bet, n)
        rooms[room.id] = room
        waiting[(bet, n)] = room
        _save_match(room, "waiting")
    used = min(bet, D["turnover_left"](uid))                           # counts toward the play-through rule
    seat = room.seats.index(None)
    p = {"uid": uid, "name": name, "photo": photo, "bet": bet, "used": used, "eid": _new_id(),
         "fouls": 0, "pocketed": 0, "timeouts": 0, "result": "pending", "payout": 0}
    room.seats[seat] = p
    by_uid[uid] = room
    D["wallets"][uid] = D["wallets"].get(uid, 0) - bet
    D["wagered"][uid] = D["wagered"].get(uid, 0) + used
    D["log"](uid, "bet", -bet, f"8-Ball #{room.id}")                    # saves the wallet
    _save_player(room, p, seat)
    _check_full(room)
    return room, None


def _check_full(room):
    if room.phase == "waiting" and None not in room.seats:
        room.phase, room.start_at = "starting", now() + START_DELAY
        if waiting.get((room.bet, room.n)) is room:
            del waiting[(room.bet, room.n)]                            # nobody else can sit here
    elif room.phase == "starting" and None in room.seats:
        room.phase = "waiting"
        waiting.setdefault((room.bet, room.n), room)


def leave(uid):
    """Leave before the match starts: the bet comes back."""
    room = by_uid.get(uid)
    if not room or room.phase not in ("waiting", "starting"):
        return room
    seat = seat_of(room, uid)
    p = room.seats[seat]
    room.seats[seat] = None
    del by_uid[uid]
    _pay_back(room, p, "left the table")
    _save_player(room, p, seat)
    if not any(room.seats):
        room.phase = "finished"
        _save_match(room, "refunded", "empty", ended=now())
        _close(room)
        rooms.pop(room.id, None)
    else:
        _check_full(room)
    return room


def _begin(room):
    room.balls = {0: [pg.HEAD[0], pg.HEAD[1]]}
    room.balls.update(pg.new_rack())
    room.phase, room.started = "playing", now()
    room.turn, room.bih = _rng.randrange(room.n), False
    room.busy_until = room.started
    room.shot_deadline = room.started + SHOT_SECONDS + 2
    _save_match(room, "playing")


def _free(room, x, y):
    if not (pg.R <= x <= pg.W - pg.R and pg.R <= y <= pg.H - pg.R):
        return False
    return all((p[0] - x) ** 2 + (p[1] - y) ** 2 >= (2 * pg.R + 1) ** 2 for k, p in room.balls.items() if k != 0)


def _spot(room):
    if _free(room, pg.HEAD[0], pg.HEAD[1]):
        return [pg.HEAD[0], pg.HEAD[1]]
    for r in range(10, 400, 10):
        for a in range(0, 360, 20):
            x, y = pg.HEAD[0] + r * math.cos(math.radians(a)), pg.HEAD[1] + r * math.sin(math.radians(a))
            if _free(room, x, y):
                return [x, y]
    return [pg.HEAD[0], pg.HEAD[1]]


def take_shot(room, seat, msg):
    """The whole shot in one go (no await). Returns ("ok", out) or ("err", text)."""
    if room.phase != "playing":
        return "err", "no match"
    if room.turn != seat:
        return "err", "not your turn"
    if msg.get("seq") != room.seq:
        return "err", "old shot"
    if now() + 0.3 < room.busy_until:
        return "err", "busy"
    try:
        a, pw = float(msg.get("a")), float(msg.get("p"))
        sx, sy = float(msg.get("sx", 0)), float(msg.get("sy", 0))
    except (TypeError, ValueError):
        return "err", "bad shot"
    if not all(math.isfinite(v) for v in (a, pw, sx, sy)):
        return "err", "bad shot"
    pw = max(0.03, min(1.0, pw))
    m = math.hypot(sx, sy)
    if m > 1:
        sx, sy = sx / m, sy / m
    if room.bih or 0 not in room.balls:
        pl, spot = msg.get("place"), None
        room.balls.pop(0, None)
        if room.bih and isinstance(pl, dict):
            try:
                x, y = float(pl.get("x")), float(pl.get("y"))
                if math.isfinite(x) and math.isfinite(y) and _free(room, x, y):
                    spot = [x, y]
            except (TypeError, ValueError):
                pass
        room.balls[0] = spot or _spot(room)
    room.bih = False
    team, player = seat % 2, room.seats[seat]
    left_before = _left(room, team)
    res = pg.simulate(room.balls, math.cos(a) * pw * pg.MAX_SPEED, math.sin(a) * pw * pg.MAX_SPEED, sx, sy)
    info = res["info"]
    poc = [n for n in info["pocketed"] if n != 0]
    first, grp = info["first"], room.groups[team]
    foul = None
    if info["cue_in"]:
        foul = "cue ball pocketed"
    elif info["off"]:
        foul = "a ball left the table"
    elif first is None:
        foul = "no ball was hit"
    elif grp is None:
        if first == 8:
            foul = "8-ball hit first"
    elif left_before > 0:
        if _kind(first) != grp:
            foul = "wrong ball hit first"
    elif first != 8:
        foul = "must hit the 8-ball"
    if not foul and not poc and not info["rail_after"]:
        foul = "no rail after contact"
    old = room.seq
    room.seq += 1
    stats["shots"] += 1
    room.balls = {n: list(v) for n, v in res["final"].items()}
    player["timeouts"] = 0
    out = {"t": "shot", "seq": old, "seat": seat, "frames": res["frames"], "fps": pg.FPS, "events": res["events"],
           "res": {"pocketed": poc, "foul": foul, "foul_text": foul, "points": len(poc), "first": first}}
    room.busy_until = now() + res["duration"] + pg.ANIM_PAD
    room.shot_deadline = room.busy_until + SHOT_SECONDS
    player["pocketed"] += len(poc)
    if foul:
        player["fouls"] += 1
    if 8 in poc or 8 in info["off"]:                                   # the 8-ball decides the match
        win = (not foul) and grp is not None and left_before == 0 and 8 in poc
        _settle(room, team if win else 1 - team, "8ball" if win else "foul8")
        return "ok", out
    if grp is None and not foul and poc:                               # first legal pocket: groups are given
        room.groups[team] = _kind(poc[0])
        room.groups[1 - team] = "stripe" if room.groups[team] == "solid" else "solid"
    if foul:
        room.turn, room.bih = (seat + 1) % room.n, True
    elif not any(_kind(n) == room.groups[team] for n in poc):
        room.turn = (seat + 1) % room.n
    return "ok", out


def _timeout(room):
    seat = room.turn
    p = room.seats[seat]
    p["timeouts"] += 1
    room.seq += 1
    if p["timeouts"] >= MAX_TIMEOUTS:
        _settle(room, 1 - seat % 2, "forfeit")
        return
    room.turn, room.bih = (seat + 1) % room.n, False
    room.busy_until, room.shot_deadline = now(), now() + SHOT_SECONDS


def snapshot(room, uid=None):
    t = now()
    seats = [None if p is None else {"name": p["name"], "photo": p["photo"], "score": _label(room, i),
                                     "fouls": p["fouls"], "pocketed": p["pocketed"], "bot": False,
                                     "on": bool(room.conns.get(p["uid"]))} for i, p in enumerate(room.seats)]
    return {"t": "state", "rid": room.id, "phase": room.phase, "seats": seats,
            "you": seat_of(room, uid) if uid is not None else -1, "turn": room.turn, "bih": room.bih, "seq": room.seq,
            "balls": {str(n): [round(p[0], 1), round(p[1], 1)] for n, p in room.balls.items()},
            "match_left": max(0.0, room.started + MATCH_SECONDS - t) if room.phase == "playing" else 0,
            "shot_left": max(0.0, room.shot_deadline - t) if room.phase == "playing" else 0,
            "start_in": max(0.0, room.start_at - t) if room.phase == "starting" else 0,
            "sd": None, "result": room.result, "toast": None, "admin": False, "free": False,
            "cfg": _cfg(room.bet, room.n, uid)}


# ---------------------------------------------------------------- live part
async def _send(ws, msg):
    try:
        await ws.send_json(msg)
    except Exception:
        pass


async def broadcast_state(room):
    for uid, wss in list(room.conns.items()):
        s = snapshot(room, uid)
        for ws in list(wss):
            await _send(ws, s)


async def broadcast_shot(room, out):
    for uid, wss in list(room.conns.items()):
        m = dict(out)
        m["after"] = snapshot(room, uid)
        for ws in list(wss):
            await _send(ws, m)


async def tick():
    t = now()
    for room in list(rooms.values()):
        changed = False
        if room.phase == "waiting" and t - room.created >= WAIT_SECONDS:
            _refund_all(room, "no opponent found")
            changed = True
        elif room.phase == "starting" and t >= room.start_at:
            _begin(room)
            changed = True
        elif room.phase == "playing":
            if t >= room.started + MATCH_SECONDS and t >= room.busy_until:
                _refund_all(room, "time over")
                changed = True
            elif t >= room.shot_deadline and t >= room.busy_until:
                _timeout(room)
                changed = True
        elif room.phase == "finished" and t - room.finished_at > KEEP_DONE:
            rooms.pop(room.id, None)
            continue
        if changed:
            await broadcast_state(room)


async def ticker():
    while True:
        try:
            await tick()
        except Exception as e:
            print("8-ball tick error:", repr(e))
        await asyncio.sleep(0.25)


async def ws_handler(req):
    from aiohttp import web
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=2048)
    await ws.prepare(req)
    q = dict(req.query)
    user = D["verify"](q.get("initData", ""))
    err = None
    if not user:
        err = "auth"
    else:
        uid = D["touch"](user)
        if uid in D["banned"]:
            err = "blocked"
        elif not allowed(uid):
            err = "closed"
    if err:
        await _send(ws, {"t": "error", "e": err})
        await ws.close()
        return ws
    name, photo = pg._name(user), str(user.get("photo_url") or "")[:300]
    try:
        qbet, qn = int(q.get("bet", 0)), int(q.get("n", 2))
    except ValueError:
        qbet, qn = 0, 2
    room = by_uid.get(uid)
    if room:
        room.conns.setdefault(uid, set()).add(ws)
        await broadcast_state(room)
    else:
        await _send(ws, {"t": "state", "phase": "none", "free": False, "you": -1, "admin": False,
                         "cfg": _cfg(qbet, qn, uid)})
    stamps = []
    try:
        async for m in ws:
            if m.type != 1:
                continue
            t = time.monotonic()
            stamps[:] = [s for s in stamps if t - s < 5] + [t]
            if len(stamps) > 40:
                continue
            try:
                msg = json.loads(m.data)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            kind, room = msg.get("t"), by_uid.get(uid)
            if kind == "join":
                if uid not in D["phones"]:
                    await _send(ws, {"t": "error", "e": "phone", "msg": "Share your phone number in the bot first / መጀመሪያ ስልክ ቁጥርዎን ያጋሩ"})
                    continue
                try:
                    bet, n = int(msg.get("bet")), int(msg.get("n"))
                except (TypeError, ValueError):
                    bet, n = 0, 2
                room, why = join(uid, name, photo, bet, n)
                if why:
                    await _send(ws, {"t": "error", "e": "join", "msg": why})
                    continue
                room.conns.setdefault(uid, set()).add(ws)
                await broadcast_state(room)
            elif kind == "leave":
                old = leave(uid)
                if old:
                    await broadcast_state(old)
                await _send(ws, {"t": "state", "phase": "none", "free": False, "you": -1, "admin": False,
                                 "cfg": _cfg(qbet, qn, uid)})
            elif kind == "shot" and room:
                seat = seat_of(room, uid)
                if seat is None:
                    continue
                res, out = take_shot(room, seat, msg)
                if res == "ok":
                    await broadcast_shot(room, out)
                else:
                    await _send(ws, {"t": "refused", "why": out})
                    await _send(ws, snapshot(room, uid))
            elif kind == "sync":
                await _send(ws, snapshot(room, uid) if room else
                            {"t": "state", "phase": "none", "free": False, "you": -1, "admin": False,
                             "cfg": _cfg(qbet, qn, uid)})
    finally:
        for r in rooms.values():
            s = r.conns.get(uid)
            if s:
                s.discard(ws)
                if not s:
                    del r.conns[uid]
                    await broadcast_state(r)
    return ws


def routes():
    from aiohttp import web

    async def page(req):
        f = Path(__file__).parent / "web" / "eightball.html"
        if not f.exists():
            return web.Response(text="web/eightball.html not found", status=404)
        return web.FileResponse(f, headers={"Cache-Control": "no-cache"})

    return [web.get("/eightball", page), web.get("/ws/eightball", ws_handler)]


async def load_state(conn):
    """After a restart every unfinished match is cancelled and every bet is given back (once)."""
    mx = await conn.fetchval("SELECT COALESCE(MAX(id), 0) FROM eb_matches")
    _last_id[0] = max(_last_id[0], int(mx or 0))
    rows = await conn.fetch("SELECT id, match_id, uid, bet, used FROM eb_players WHERE result = 'pending'")
    for r in rows:
        uid = r["uid"]
        D["wallets"][uid] = D["wallets"].get(uid, 0) + r["bet"]
        D["wagered"][uid] = max(0, D["wagered"].get(uid, 0) - r["used"])
        D["log"](uid, "refund", r["bet"], f"8-Ball #{r['match_id']} refunded (server restarted)")
        await conn.execute("UPDATE eb_players SET result = 'refund', payout = bet WHERE id = $1", r["id"])
    await conn.execute("UPDATE eb_matches SET status = 'refunded', reason = 'restart' "
                       "WHERE status IN ('waiting', 'playing')")
    row = await conn.fetchrow("SELECT COUNT(*) n FROM eb_matches WHERE status = 'done'")
    stats["matches"] = row["n"]


def register(dp):
    from aiogram.filters import Command
    from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

    def base():
        from urllib.parse import urlsplit
        u = urlsplit(str(D.get("webapp_url", "")).strip())
        return (f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else str(D.get("webapp_url", "")).rstrip("/")) + "/eightball"

    @dp.message(Command("eightball"))
    async def cmd_eightball(m: Message):
        if not allowed(m.from_user.id):
            await m.answer("🎱 8-Ball is not open yet.")
            return
        rows = []
        for b in BETS:
            rows.append([InlineKeyboardButton(text=f"1 v 1 · {b} ETB → win {pot_prize(b, 2)[1]}",
                                              web_app=WebAppInfo(url=f"{base()}?bet={b}&n=2")),
                         InlineKeyboardButton(text=f"2 v 2 · {b} ETB → win {pot_prize(b, 4)[1] // 2} each",
                                              web_app=WebAppInfo(url=f"{base()}?bet={b}&n=4"))])
        await m.answer("🎱 Rodas Friend Zone 8-Ball. Pick your bet:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    @dp.message(Command("ebstatus"))
    async def cmd_ebstatus(m: Message):
        if not (D.get("admin_id") and m.from_user.id == D["admin_id"]):
            return
        live = [r for r in rooms.values() if r.phase in ("waiting", "starting", "playing")]
        await m.answer(f"🎱 8-Ball is {'OPEN to everyone' if ENABLED else 'OFF (admin only)'}\n"
                       f"Tables now: {len(live)} · playing: {sum(1 for r in live if r.phase == 'playing')}\n"
                       f"Matches finished: {stats['matches']} · bets taken: {stats['bets']} · paid: {stats['paid']}\n"
                       f"Bets {BETS} · prize {PRIZE_PERCENT}% · shot {SHOT_SECONDS} s · wait {WAIT_SECONDS} s")
