"""
Rodas Friend Zone POOL  -  4 players, one table, free demo.

FREE DEMO ONLY. This file never touches wallets, balances, deposits or withdrawals. It does not
receive them from bot.py and has no payment endpoint. Scores are plain match points that can not be
bought, moved, redeemed or exchanged. Real-money play is NOT part of this game.

How it works (why a changed phone can not cheat):
  * The SERVER owns the table. The phone sends only: aim angle, power, spin (and where to put the
    cue ball when it has ball-in-hand). The server runs the physics, decides the fouls, adds the
    points, keeps both timers and picks the winner. The phones only play back the result.
  * One shot = one plain function (take_shot) with no "await" inside, so it can never be
    interrupted halfway. Every shot carries the room's shot number "seq"; a second tap of the same
    shot (or a late old one) is refused.
  * Every score change is written into an audit list which is saved in the database
    (table pool_matches) when the match ends.
  * A player who loses connection keeps the seat and can come back (same Telegram account). If it is
    their turn and the 20 s shot timer runs out, the turn passes on.

Rules of this game (custom points game, NOT standard 8-ball):
  * Legal shot that pockets balls: +1 per numbered ball, +2 for the 8-ball, the shooter shoots again.
  * Legal shot that pockets nothing: the next player shoots.
  * Foul: -1 point (never below 0), the next player has ball-in-hand, balls pocketed on a foul do not score.
    Fouls: cue ball pocketed, a ball leaves the table, the cue ball touches no ball, or (after the first
    contact) no ball is pocketed and no ball touches a rail.
  * Table cleared: the 15 balls are put back and the next player shoots.
  * 10 minute match timer (the shot in progress finishes), 20 second shot timer.
  * Tie: most points, then most legally pocketed balls, then fewest fouls, then SUDDEN DEATH:
    every tied player takes one shot from the same fresh rack, those who score most go on,
    up to 3 rounds, then a fair random draw (the audit says so).

bot.py needs only these lines (see the notes in the answer):
    import poolgame as pg
    pg.register(dp)                         (next to ng.register(dp))
    *pg.routes(),                           (inside app.add_routes)
    await c.execute(pg.SCHEMA)              (next to ng.SCHEMA)
    pg.init(verify=verify, touch=touch, banned=banned, admin_id=ADMIN_ID, enqueue=_enqueue, webapp_url=WEBAPP_URL)
    await pg.load_state(c)                  (next to ng.load_state)
    asyncio.create_task(pg.ticker())        (next to ng.ticker)
"""
import asyncio
import hashlib
import json
import math
import os
import random
import re
import time
from pathlib import Path

ENABLED = os.getenv("POOL_ENABLED", "0") == "1"          # off = only the admin can play
DEV = os.getenv("POOL_DEV", "0") == "1"                  # TEST ONLY: ?dev=name login without Telegram. Keep 0 online.
MATCH_SECONDS = int(os.getenv("POOL_MATCH_SECONDS", "600"))
SHOT_SECONDS = int(os.getenv("POOL_SHOT_SECONDS", "20"))
START_DELAY = 5            # seconds between "4 players seated" and the first shot
SEATS = 4
SD_MAX_ROUNDS = 3
FOUL_IF_8_FIRST = False    # True = hitting the 8-ball first is a foul

# ---- table (portrait, units are "pixels" of the table picture) ----
W, H = 500.0, 1000.0
R = 14.0                                   # ball radius
POCKETS = [(0, 0), (W, 0), (0, H), (W, H), (0, H / 2), (W, H / 2)]
POCKET_R = [34.0, 34.0, 34.0, 34.0, 30.0, 30.0]
HEAD = (W / 2, 760.0)                      # cue ball start / fallback spot
FOOT = (W / 2, 300.0)                      # front ball of the rack

# ---- physics ----
DT = 1 / 240
FRAME_EVERY = 12                           # 20 frames per second go to the phones
FPS = int(round(1 / DT / FRAME_EVERY))
FRICTION = 420.0                           # px/s^2 slowing every ball
STOP_V = 6.0
E_BALL = 0.96
E_RAIL = 0.80
MAX_SPEED = 2300.0
MAX_SIM = 25.0                             # seconds, then every ball is stopped by force
SPIN_K = 0.22                              # side spin effect on a rail
TOP_K = 0.35                               # follow / draw after the first contact
ANIM_PAD = 0.8                             # seconds added after the animation before the next timer starts

FOULS = {"scratch": "cue ball pocketed", "off_table": "a ball left the table",
         "no_contact": "cue ball hit no ball", "bad_first": "8-ball hit first",
         "no_rail": "no ball reached a rail"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS pool_matches(
    id BIGINT PRIMARY KEY,
    started BIGINT NOT NULL,
    ended BIGINT NOT NULL,
    status TEXT NOT NULL,
    players TEXT NOT NULL,
    result TEXT NOT NULL,
    audit TEXT NOT NULL
);
"""

D = {}                      # filled by init()
rooms = {}                  # room id -> Room
player_room = {}            # uid -> room id (only while the match is not over)
stats = {"matches": 0, "shots": 0}
_last_id = [0]
_rng = random.SystemRandom()


def now():
    return time.time()


def init(**deps):
    D.update(deps)


def allowed(uid, dev=False):
    return bool(ENABLED or dev or (D.get("admin_id") and uid == D["admin_id"]))


# =====================================================================================
#  PHYSICS
# =====================================================================================
def new_rack():
    """Positions of balls 1..15 in a triangle. The 8 is in the middle, the rest is random."""
    nums = [n for n in range(1, 16) if n != 8]
    _rng.shuffle(nums)
    dy = math.sqrt(3) * (R + 0.25)
    out = {}
    i = 0
    for row in range(5):
        for k in range(row + 1):
            x = FOOT[0] + (k - row / 2) * (2 * R + 0.5)
            y = FOOT[1] - row * dy
            n = 8 if i == 4 else nums.pop()
            out[n] = [x, y]
            i += 1
    return out


def simulate(balls, cvx, cvy, spin_x=0.0, spin_y=0.0):
    """Run one shot. balls = {n: [x, y]} (0 is the cue ball). Returns a dict with the frames for the
    phones, sound events, what happened (info) and where the balls stopped (final)."""
    B = {n: [float(p[0]), float(p[1]), 0.0, 0.0] for n, p in balls.items()}
    B[0][2], B[0][3] = cvx, cvy
    live = sorted(B)
    info = {"first": None, "pocketed": [], "cue_in": False, "off": [], "rail_after": False}
    events, frames = [], []
    gone = set()

    def rec():
        row = []
        for n in range(16):
            if n in B and n not in gone:
                row.append(int(round(B[n][0] * 10)))
                row.append(int(round(B[n][1] * 10)))
            else:
                row.append(-1)
                row.append(-1)
        frames.append(row)

    contact = False
    spin = spin_x
    D2 = (2 * R) ** 2
    TWO_R = 2 * R
    step = 0
    max_steps = int(MAX_SIM / DT)
    rec()
    while step < max_steps:
        moving = []
        for n in live:
            b = B[n]
            if b[2] != 0.0 or b[3] != 0.0:
                sp = math.hypot(b[2], b[3])
                if sp <= STOP_V:
                    b[2] = b[3] = 0.0
                    continue
                k = max(0.0, sp - FRICTION * DT) / sp
                b[2] *= k
                b[3] *= k
                b[0] += b[2] * DT
                b[1] += b[3] * DT
                moving.append(n)
        if not moving:
            break
        fr = step // FRAME_EVERY
        # ---- pockets, off-table, cushions ----
        for n in moving:
            b = B[n]
            x, y = b[0], b[1]
            hole = -1
            for pi in range(6):
                px, py = POCKETS[pi]
                if (x - px) ** 2 + (y - py) ** 2 < POCKET_R[pi] ** 2:
                    hole = pi
                    break
            if hole >= 0:
                live.remove(n)
                gone.add(n)
                b[2] = b[3] = 0.0
                events.append([fr, "p", n, hole])
                if n == 0:
                    info["cue_in"] = True
                else:
                    info["pocketed"].append(n)
                continue
            if x < -6 or x > W + 6 or y < -6 or y > H + 6:
                live.remove(n)
                gone.add(n)
                b[2] = b[3] = 0.0
                info["off"].append(n)
                continue
            hit = 0.0
            if x < R:
                b[0] = R
                if b[2] < 0:
                    hit = -b[2]
                    b[2] = hit * E_RAIL
                    if n == 0 and spin:
                        b[3] -= spin * SPIN_K * hit
                        spin *= 0.6
            elif x > W - R:
                b[0] = W - R
                if b[2] > 0:
                    hit = b[2]
                    b[2] = -hit * E_RAIL
                    if n == 0 and spin:
                        b[3] += spin * SPIN_K * hit
                        spin *= 0.6
            h2 = 0.0
            if y < R:
                b[1] = R
                if b[3] < 0:
                    h2 = -b[3]
                    b[3] = h2 * E_RAIL
                    if n == 0 and spin:
                        b[2] += spin * SPIN_K * h2
                        spin *= 0.6
            elif y > H - R:
                b[1] = H - R
                if b[3] > 0:
                    h2 = b[3]
                    b[3] = -h2 * E_RAIL
                    if n == 0 and spin:
                        b[2] -= spin * SPIN_K * h2
                        spin *= 0.6
            hit = max(hit, h2)
            if hit > 0:
                if contact:
                    info["rail_after"] = True
                if hit > 120:
                    events.append([fr, "r", n, round(min(1.0, hit / 1200), 2)])
        # ---- ball against ball ----
        L = len(live)
        for i in range(L):
            ni = live[i]
            bi = B[ni]
            mi = bi[2] != 0.0 or bi[3] != 0.0
            for j in range(i + 1, L):
                nj = live[j]
                bj = B[nj]
                if not mi and bj[2] == 0.0 and bj[3] == 0.0:
                    continue
                dx = bj[0] - bi[0]
                if dx > TWO_R or dx < -TWO_R:
                    continue
                dy = bj[1] - bi[1]
                d2 = dx * dx + dy * dy
                if d2 >= D2:
                    continue
                d = math.sqrt(d2)
                if d < 1e-6:
                    nx, ny, d = 1.0, 0.0, 1e-6
                else:
                    nx, ny = dx / d, dy / d
                first_cue = (ni == 0 or nj == 0) and not contact
                if first_cue:
                    contact = True
                    info["first"] = nj if ni == 0 else ni
                    pvx, pvy = B[0][2], B[0][3]
                vrel = (bi[2] - bj[2]) * nx + (bi[3] - bj[3]) * ny
                if vrel > 0:
                    j_imp = (1 + E_BALL) / 2 * vrel
                    bi[2] -= j_imp * nx
                    bi[3] -= j_imp * ny
                    bj[2] += j_imp * nx
                    bj[3] += j_imp * ny
                    if vrel > 80:
                        events.append([fr, "h", ni, nj, round(min(1.0, vrel / 1500), 2)])
                if first_cue and spin_y:
                    B[0][2] += pvx * spin_y * TOP_K
                    B[0][3] += pvy * spin_y * TOP_K
                ov = (TWO_R - d) / 2
                bi[0] -= nx * ov
                bi[1] -= ny * ov
                bj[0] += nx * ov
                bj[1] += ny * ov
        step += 1
        if step % FRAME_EVERY == 0:
            rec()
    # everything that still moves is stopped (time limit)
    for n in live:
        B[n][2] = B[n][3] = 0.0
    if step % FRAME_EVERY != 0 or len(frames) == 1:
        rec()
    final = {n: [round(B[n][0], 2), round(B[n][1], 2)] for n in live}
    return {"frames": frames, "events": events, "info": info, "final": final,
            "duration": (len(frames) - 1) / FPS}


def judge(info):
    """Returns (foul_code or None, points for the pocketed balls)."""
    pts = sum(2 if n == 8 else 1 for n in info["pocketed"])
    if info["cue_in"]:
        return "scratch", pts
    if info["off"]:
        return "off_table", pts
    if info["first"] is None:
        return "no_contact", pts
    if FOUL_IF_8_FIRST and info["first"] == 8:
        return "bad_first", pts
    if not info["pocketed"] and not info["rail_after"]:
        return "no_rail", pts
    return None, pts


# =====================================================================================
#  ROOMS
# =====================================================================================
class Room:
    def __init__(self, rid):
        self.id = rid
        self.seats = [None] * SEATS
        self.conns = {}               # uid -> set of websockets
        self.phase = "waiting"        # waiting, starting, playing, finished, aborted
        self.created = now()
        self.start_at = 0.0
        self.started = 0.0
        self.match_end = 0.0
        self.turn = 0
        self.seq = 0
        self.bih = False
        self.balls = {}
        self.shot_deadline = 0.0
        self.busy_until = 0.0
        self.audit = []
        self.sd = None
        self.result = None
        self.empty_since = None
        self.finished_at = 0.0
        self.bot_at = 0.0
        self.toast = None


def seat_of(room, uid):
    for i, p in enumerate(room.seats):
        if p and not p["bot"] and p["uid"] == uid:
            return i
    return None


def log_audit(room, kind, seat=None, **data):
    e = {"t": int(now()), "seq": room.seq, "kind": kind}
    if seat is not None:
        e["seat"] = seat
        p = room.seats[seat]
        e["name"] = p["name"]
        e["score"] = p["score"]
    e.update(data)
    room.audit.append(e)


def _free(room, x, y, skip0=True):
    if not (R <= x <= W - R and R <= y <= H - R):
        return False
    for n, p in room.balls.items():
        if n == 0 and skip0:
            continue
        if (p[0] - x) ** 2 + (p[1] - y) ** 2 < (2 * R + 1) ** 2:
            return False
    return True


def _find_spot(room):
    """A free place for the cue ball, as near to the head spot as possible."""
    if _free(room, HEAD[0], HEAD[1]):
        return [HEAD[0], HEAD[1]]
    for r in range(10, 400, 10):
        for a in range(0, 360, 20):
            x = HEAD[0] + r * math.cos(math.radians(a))
            y = HEAD[1] + r * math.sin(math.radians(a))
            if _free(room, x, y):
                return [x, y]
    return [HEAD[0], HEAD[1]]


def start_rack(room):
    room.balls = {0: [HEAD[0], HEAD[1]]}
    room.balls.update(new_rack())


def respot(room):
    """Table cleared: the 15 balls are put back. The cue ball stays unless it is in the way."""
    cue = room.balls.get(0)
    room.balls = {}
    room.balls.update(new_rack())
    if cue is not None:
        room.balls[0] = cue
        if not _free(room, cue[0], cue[1]):
            room.balls[0] = _find_spot(room)
    else:
        room.balls[0] = _find_spot(room)


def next_seat(room, seat):
    return (seat + 1) % SEATS


def begin_match(room):
    start_rack(room)
    room.phase = "playing"
    room.started = now()
    room.match_end = room.started + MATCH_SECONDS
    room.turn = _rng.randrange(SEATS)
    room.bih = False
    room.busy_until = room.started
    room.shot_deadline = room.started + SHOT_SECONDS + 2
    log_audit(room, "start", players=[p["name"] for p in room.seats], first=room.turn)


def snapshot(room, uid=None):
    t = now()
    seats = []
    for i, p in enumerate(room.seats):
        if p is None:
            seats.append(None)
        else:
            seats.append({"name": p["name"], "photo": p["photo"], "score": p["score"], "fouls": p["fouls"],
                          "pocketed": p["pocketed"], "bot": p["bot"],
                          "on": bool(p["bot"] or room.conns.get(p["uid"]))})
    s = {"t": "state", "rid": room.id, "phase": room.phase, "seats": seats,
         "you": seat_of(room, uid) if uid is not None else -1,
         "turn": room.turn, "bih": room.bih, "seq": room.seq,
         "balls": {str(n): [round(p[0], 1), round(p[1], 1)] for n, p in room.balls.items()},
         "match_left": max(0.0, room.match_end - t) if room.phase == "playing" else 0,
         "shot_left": max(0.0, room.shot_deadline - t) if room.phase == "playing" else 0,
         "start_in": max(0.0, room.start_at - t) if room.phase == "starting" else 0,
         "sd": None, "result": room.result, "toast": room.toast,
         "admin": bool(uid is not None and uid in D.get("_admins", ())),
         "free": True}
    if room.sd:
        s["sd"] = {"players": room.sd["players"], "round": room.sd["round"]}
    return s


def _place_ok(room, x, y):
    return _free(room, x, y)


def take_shot(room, seat, msg):
    """The whole shot, in one go. Returns ("ok", out) or ("err", text). No await in here."""
    if room.phase != "playing":
        return "err", "no match"
    if room.turn != seat:
        return "err", "not your turn"
    if msg.get("seq") != room.seq:
        return "err", "old shot"
    if now() + 0.3 < room.busy_until:
        return "err", "busy"
    try:
        a = float(msg.get("a"))
        p = float(msg.get("p"))
        sx = float(msg.get("sx", 0))
        sy = float(msg.get("sy", 0))
    except (TypeError, ValueError):
        return "err", "bad shot"
    if not all(math.isfinite(v) for v in (a, p, sx, sy)):
        return "err", "bad shot"
    p = max(0.03, min(1.0, p))
    m = math.hypot(sx, sy)
    if m > 1:
        sx, sy = sx / m, sy / m
    sd = room.sd is not None
    if sd:
        room.bih = False
    # the cue ball: ball-in-hand lets the player choose the spot
    if room.bih or 0 not in room.balls:
        pl = msg.get("place")
        room.balls.pop(0, None)
        spot = None
        if room.bih and isinstance(pl, dict):
            try:
                x, y = float(pl.get("x")), float(pl.get("y"))
                if math.isfinite(x) and math.isfinite(y) and _free(room, x, y):
                    spot = [x, y]
            except (TypeError, ValueError):
                pass
        room.balls[0] = spot or _find_spot(room)
    room.bih = False
    speed = p * MAX_SPEED
    res = simulate(room.balls, math.cos(a) * speed, math.sin(a) * speed, sx, sy)
    info = res["info"]
    foul, pts = judge(info)
    player = room.seats[seat]
    old = room.seq
    room.seq += 1
    stats["shots"] += 1
    # balls left on the table
    start_positions = {n: list(pp) for n, pp in room.balls.items()}
    room.balls = {n: list(v) for n, v in res["final"].items()}
    out = {"t": "shot", "seq": old, "seat": seat, "frames": res["frames"], "fps": FPS, "events": res["events"],
           "res": {"pocketed": info["pocketed"], "foul": foul, "foul_text": FOULS.get(foul), "points": 0, "first": info["first"]}}
    delta = 0
    if sd:
        gain = 0 if foul else pts
        room.sd["gains"][seat] = gain
        out["res"]["points"] = gain
        log_audit(room, "sd_shot", seat, round=room.sd["round"], pocketed=info["pocketed"], foul=foul, gain=gain)
        room.toast = None
    else:
        if foul:
            before = player["score"]
            player["score"] = max(0, before - 1)
            delta = player["score"] - before
            player["fouls"] += 1
            room.bih = True
            room.turn = next_seat(room, seat)
        else:
            player["score"] += pts
            delta = pts
            player["pocketed"] += len(info["pocketed"])
            out["res"]["points"] = pts
            if not info["pocketed"]:
                room.turn = next_seat(room, seat)
        cleared = not any(n != 0 for n in room.balls)
        if cleared:
            respot(room)
            if not foul and info["pocketed"]:
                room.turn = next_seat(room, seat)
            out["res"]["rack_cleared"] = True
        out["res"]["delta"] = delta
        log_audit(room, "shot", seat, pocketed=info["pocketed"], foul=foul, delta=delta,
                  first=info["first"], rack_cleared=cleared)
    room.busy_until = now() + res["duration"] + ANIM_PAD
    room.shot_deadline = room.busy_until + SHOT_SECONDS
    if sd:
        sd_next(room)
    return "ok", out


def take_timeout(room):
    """The shooter did not shoot within the shot timer: the turn passes on (no foul)."""
    seat = room.turn
    room.seq += 1
    if room.sd:
        room.sd["gains"][seat] = 0
        log_audit(room, "sd_timeout", seat, round=room.sd["round"])
        sd_next(room)
    else:
        room.seats[seat]["timeouts"] = room.seats[seat].get("timeouts", 0) + 1
        log_audit(room, "timeout", seat)
        room.turn = next_seat(room, seat)
        room.bih = False
    room.busy_until = now()
    room.shot_deadline = now() + SHOT_SECONDS


# ---- end of the match, tie-break ----
def _key(room, i):
    p = room.seats[i]
    return (-p["score"], -p["pocketed"], p["fouls"])


def ranking(room, first=None):
    order = sorted(range(SEATS), key=lambda i: (_key(room, i), i))
    if first is not None:
        order.remove(first)
        order.insert(0, first)
    return order


def end_regular(room):
    order = ranking(room)
    top = [i for i in order if _key(room, i) == _key(room, order[0])]
    log_audit(room, "time_up", scores=[p["score"] for p in room.seats])
    if len(top) > 1:
        room.sd = {"players": top, "round": 1, "idx": 0, "gains": {}, "rack": None, "coin": False, "all": list(top)}
        log_audit(room, "sudden_death", players=top)
        sd_setup_shot(room)
    else:
        finish(room, top[0])


def sd_setup_shot(room):
    """Every sudden-death shot starts from the same fresh rack, so it is fair to all."""
    if room.sd["rack"] is None:
        room.sd["rack"] = {n: list(p) for n, p in new_rack().items()}
    room.balls = {0: [HEAD[0], HEAD[1]]}
    room.balls.update({n: list(p) for n, p in room.sd["rack"].items()})
    room.bih = False
    room.turn = room.sd["players"][room.sd["idx"]]


def sd_next(room):
    sd = room.sd
    sd["idx"] += 1
    if sd["idx"] < len(sd["players"]):
        sd_setup_shot(room)
        return
    gains = sd["gains"]
    mx = max(gains.values())
    cand = [s for s in sd["players"] if gains[s] == mx] if mx > 0 else list(sd["players"])
    if mx > 0 and len(cand) == 1:
        finish(room, cand[0])
        return
    if sd["round"] >= SD_MAX_ROUNDS:
        sd["coin"] = True
        win = _rng.choice(cand)
        log_audit(room, "sd_coin", players=cand, winner=win)
        finish(room, win)
        return
    sd["round"] += 1
    sd["players"] = cand
    sd["idx"] = 0
    sd["gains"] = {}
    log_audit(room, "sd_round", round=sd["round"], players=cand)
    sd_setup_shot(room)


def finish(room, winner):
    order = ranking(room, first=winner)
    room.result = {"winner": winner, "order": order,
                   "players": [{"seat": i, "name": room.seats[i]["name"], "score": room.seats[i]["score"],
                                "pocketed": room.seats[i]["pocketed"], "fouls": room.seats[i]["fouls"]} for i in order],
                   "tiebreak": ({"rounds": room.sd["round"], "coin": room.sd["coin"], "players": room.sd["all"]}
                                if room.sd else None)}
    room.phase = "finished"
    room.finished_at = now()
    room.sd = None
    log_audit(room, "end", winner=winner, order=order)
    stats["matches"] += 1
    _release(room)
    _save(room, "finished")


def _release(room):
    for p in room.seats:
        if p and not p["bot"] and player_room.get(p["uid"]) == room.id:
            del player_room[p["uid"]]


def _save(room, status):
    enq = D.get("enqueue")
    if not enq:
        return
    players = [({"seat": i, "uid": p["uid"], "name": p["name"], "bot": p["bot"], "score": p["score"],
                 "fouls": p["fouls"], "pocketed": p["pocketed"]} if p else None) for i, p in enumerate(room.seats)]
    try:
        enq("INSERT INTO pool_matches(id, started, ended, status, players, result, audit) "
            "VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (id) DO NOTHING",
            room.id, int(room.started or room.created), int(now()), status,
            json.dumps(players), json.dumps(room.result or {}), json.dumps(room.audit))
    except Exception:
        pass


def abort(room, why):
    room.phase = "aborted"
    room.finished_at = now()
    room.sd = None
    log_audit(room, "abort", why=why)
    _release(room)
    _save(room, "aborted")


# ---- seating ----
def _new_player(uid, name, photo, bot=False):
    return {"uid": uid, "name": name, "photo": photo, "score": 0, "fouls": 0, "pocketed": 0, "timeouts": 0, "bot": bot}


def join(uid, name, photo):
    """Quick match. Returns the room."""
    rid = player_room.get(uid)
    if rid in rooms and rooms[rid].phase in ("waiting", "starting", "playing"):
        return rooms[rid]
    room = None
    for r in rooms.values():
        if r.phase == "waiting" and None in r.seats:
            room = r
            break
    if room is None:
        _last_id[0] += 1
        room = Room(_last_id[0])
        rooms[room.id] = room
    seat = room.seats.index(None)
    room.seats[seat] = _new_player(uid, name, photo)
    player_room[uid] = room.id
    _check_full(room)
    return room


def _check_full(room):
    if room.phase == "waiting" and None not in room.seats:
        room.phase = "starting"
        room.start_at = now() + START_DELAY
    elif room.phase == "starting" and None in room.seats:
        room.phase = "waiting"


def leave(uid):
    rid = player_room.get(uid)
    room = rooms.get(rid)
    if not room or room.phase not in ("waiting", "starting"):
        return room
    seat = seat_of(room, uid)
    if seat is not None:
        room.seats[seat] = None
    player_room.pop(uid, None)
    _check_full(room)
    return room


def add_bots(room):
    n = 0
    while None in room.seats:
        n += 1
        seat = room.seats.index(None)
        room.seats[seat] = _new_player(-n, f"Bot {n}", "", bot=True)
    _check_full(room)


def bot_shot(room, seat):
    """A very simple test player: aims at the nearest ball."""
    cue = room.balls.get(0, [HEAD[0], HEAD[1]])
    targets = [(math.hypot(p[0] - cue[0], p[1] - cue[1]), n) for n, p in room.balls.items() if n != 0]
    a = -math.pi / 2
    if targets:
        _, n = min(targets)
        p = room.balls[n]
        a = math.atan2(p[1] - cue[1], p[0] - cue[0]) + _rng.uniform(-0.04, 0.04)
    return {"seq": room.seq, "a": a, "p": _rng.uniform(0.45, 0.85), "sx": 0, "sy": 0}


# ---- connections ----
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
    """Called 4 times a second: start countdown, timers, bots, clean-up."""
    t = now()
    for room in list(rooms.values()):
        humans = [p for p in room.seats if p and not p["bot"]]
        online = any(room.conns.get(p["uid"]) for p in humans)
        if online or not humans:
            room.empty_since = None
        elif room.empty_since is None:
            room.empty_since = t
        if room.phase in ("finished", "aborted"):
            if t - room.finished_at > 300:
                del rooms[room.id]
            continue
        if room.empty_since and t - room.empty_since > 120:
            if room.phase == "playing":
                abort(room, "all players left")
            else:
                for p in humans:
                    player_room.pop(p["uid"], None)
                del rooms[room.id]
                continue
            await broadcast_state(room)
            continue
        if room.phase == "starting" and t >= room.start_at:
            begin_match(room)
            await broadcast_state(room)
        elif room.phase == "playing":
            if t < room.busy_until:
                continue
            if not room.sd and t >= room.match_end:
                end_regular(room)
                await broadcast_state(room)
                continue
            seat = room.turn
            p = room.seats[seat]
            if p["bot"] and t >= room.busy_until + 1.0 and t >= room.bot_at:
                room.bot_at = t + 1.0
                kind, out = take_shot(room, seat, bot_shot(room, seat))
                if kind == "ok":
                    await broadcast_shot(room, out)
                continue
            if t >= room.shot_deadline:
                take_timeout(room)
                await broadcast_state(room)


async def ticker():
    """bot.py starts this once: asyncio.create_task(pg.ticker())"""
    while True:
        try:
            await tick()
        except Exception as e:
            print("pool tick error:", repr(e))
        await asyncio.sleep(0.25)


# ---- websocket and page ----
def _name(user):
    n = (user.get("first_name") or user.get("username") or "Player")
    n = re.sub(r"[^\w .'-]", "", str(n), flags=re.UNICODE).strip()[:14]
    return n or "Player"


def _login(q):
    dev = False
    if DEV and q.get("dev"):
        nm = re.sub(r"[^A-Za-z0-9_-]", "", q["dev"])[:12] or "dev"
        uid = 9_000_000_000 + int(hashlib.md5(nm.encode()).hexdigest()[:7], 16)
        user = {"first_name": nm}
        dev = True
    else:
        user = D["verify"](q.get("initData", ""))
        if not user:
            return None, None, False, "auth"
        uid = D["touch"](user)
        if uid in D["banned"]:
            return None, None, False, "blocked"
    if not allowed(uid, dev):
        return None, None, False, "closed"
    return uid, user, dev, None


async def ws_handler(req):
    from aiohttp import web
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=2048)
    await ws.prepare(req)
    uid, user, dev, err = _login(dict(req.query))
    if err:
        await _send(ws, {"t": "error", "e": err})
        await ws.close()
        return ws
    if dev or uid == D.get("admin_id"):
        D.setdefault("_admins", set()).add(uid)
    name = _name(user)
    photo = str(user.get("photo_url") or "")[:300]
    rid = player_room.get(uid)
    room = rooms.get(rid)
    if room:
        room.conns.setdefault(uid, set()).add(ws)
        await broadcast_state(room)
    else:
        await _send(ws, {"t": "state", "phase": "none", "free": True, "you": -1,
                         "admin": uid in D.get("_admins", ())})
    stamps = []
    try:
        async for m in ws:
            if m.type != 1:       # text only
                continue
            t = time.monotonic()
            stamps[:] = [s for s in stamps if t - s < 5] + [t]
            if len(stamps) > 40:  # more than 40 messages in 5 s: ignored
                continue
            try:
                msg = json.loads(m.data)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("t")
            room = rooms.get(player_room.get(uid))
            if kind == "join":
                room = join(uid, name, photo)
                room.conns.setdefault(uid, set()).add(ws)
                await broadcast_state(room)
            elif kind == "leave":
                old = room
                room = leave(uid)
                if old:
                    await broadcast_state(old)
                await _send(ws, {"t": "state", "phase": "none", "free": True, "you": -1,
                                 "admin": uid in D.get("_admins", ())})
            elif kind == "bots" and uid in D.get("_admins", ()) and room and room.phase in ("waiting", "starting"):
                add_bots(room)
                await broadcast_state(room)
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
            elif kind == "sync" and room:
                await _send(ws, snapshot(room, uid))
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
        f = Path(__file__).parent / "web" / "pool.html"
        if not f.exists():
            return web.Response(text="web/pool.html not found", status=404)
        return web.FileResponse(f, headers={"Cache-Control": "no-cache"})

    return [web.get("/pool", page), web.get("/ws/pool", ws_handler)]


async def load_state(conn):
    mx = await conn.fetchval("SELECT COALESCE(MAX(id), 0) FROM pool_matches")
    _last_id[0] = max(_last_id[0], int(mx or 0))
    row = await conn.fetchrow("SELECT COUNT(*) n FROM pool_matches WHERE status = 'finished'")
    stats["matches"] = row["n"]


def register(dp):
    from aiogram.filters import Command
    from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

    @dp.message(Command("pool"))
    async def cmd_pool(m: Message):
        if not allowed(m.from_user.id):
            await m.answer("🎱 Pool is not open yet.")
            return
        from urllib.parse import urlsplit
        u = urlsplit(str(D.get("webapp_url", "")).strip())
        # domain only: anything after it in WEBAPP_URL (path, ?query, #hash) is ignored
        url = (f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else str(D.get("webapp_url", "")).rstrip("/")) + "/pool"
        print("pool button url:", url)
        await m.answer("🎱 Rodas Friend Zone Pool - free demo, 4 players, no money.",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                           InlineKeyboardButton(text="🎱 Play Pool (free)", web_app=WebAppInfo(url=url))]]))

    @dp.message(Command("poolstatus"))
    async def cmd_poolstatus(m: Message):
        if not (D.get("admin_id") and m.from_user.id == D["admin_id"]):
            return
        live = [r for r in rooms.values() if r.phase in ("waiting", "starting", "playing")]
        await m.answer(f"🎱 Pool is {'OPEN to everyone' if ENABLED else 'OFF (admin only)'} - FREE DEMO\n"
                       f"Rooms now: {len(live)} · playing: {sum(1 for r in live if r.phase == 'playing')}\n"
                       f"Matches finished: {stats['matches']} · shots: {stats['shots']}\n"
                       f"Match {MATCH_SECONDS // 60} min · shot {SHOT_SECONDS} s")
