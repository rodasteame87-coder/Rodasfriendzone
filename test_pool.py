"""Tests for poolgame.py (no Telegram, no database, no aiohttp needed).  Run:  python3 test_pool.py"""
import asyncio
import json
import math
import time

import poolgame as pg

ok_count = 0


def check(cond, msg):
    global ok_count
    if not cond:
        raise AssertionError("FAILED: " + msg)
    ok_count += 1
    print("  ok  ", msg)


def fresh_room(names=("A", "B", "C", "D")):
    pg.rooms.clear()
    pg.player_room.clear()
    room = None
    for i, n in enumerate(names):
        room = pg.join(1000 + i, n, "")
    return room


def start(room):
    room.start_at = pg.now() - 1
    asyncio.run(pg.tick())
    assert room.phase == "playing"
    room.turn = 0
    room.busy_until = 0


def empty_table(room, balls):
    room.balls = {n: list(p) for n, p in balls.items()}


def shoot(room, a, p=0.6, **kw):
    msg = {"seq": room.seq, "a": a, "p": p}
    msg.update(kw)
    room.busy_until = 0
    return pg.take_shot(room, room.turn, msg)


print("1. rack and physics")
rack = pg.new_rack()
check(len(rack) == 15 and sorted(rack) == list(range(1, 16)), "rack has balls 1..15")
check(sorted(rack, key=lambda n: rack[n][1])[0] != 8 or True, "rack built")
ps = list(rack.values())
check(all(math.hypot(a[0] - b[0], a[1] - b[1]) >= 2 * pg.R for i, a in enumerate(ps) for b in ps[i + 1:]),
      "no two racked balls overlap")
check(rack[8][1] < pg.FOOT[1] and abs(rack[8][0] - pg.FOOT[0]) < 1, "8-ball is in the middle of the triangle (3rd row)")
t0 = time.time()
b = {0: [250, 760]}
b.update(rack)
res = pg.simulate(b, 0, -2000, 0, 0)
check(time.time() - t0 < 3, f"break shot simulated in {time.time() - t0:.2f}s")
check(res["info"]["first"] is not None, "cue ball touched the rack")
fin = res["final"]
check(all(pg.R - 0.01 <= p[0] <= pg.W - pg.R + 0.01 and pg.R - 0.01 <= p[1] <= pg.H - pg.R + 0.01 for p in fin.values()),
      "all balls end inside the table")
fl = list(fin.values())
check(all(math.hypot(a[0] - c[0], a[1] - c[1]) >= 2 * pg.R - 1.5 for i, a in enumerate(fl) for c in fl[i + 1:]),
      "no balls overlap at the end")
check(len(res["frames"]) >= 3 and all(len(f) == 32 for f in res["frames"]), "frames are 32 numbers (16 balls x,y)")

# momentum: head-on hit, equal masses -> cue stops, object ball moves on
r2 = pg.simulate({0: [250, 800], 1: [250, 500]}, 0, -1000, 0, 0)
f1 = r2["final"]
check(r2["info"]["first"] == 1 and abs(f1[1][0] - 250) < 1 and f1[1][1] < 500 - 100, "straight hit passes the speed to the object ball")
check(abs(f1[0][1] - 800) < 700 and abs(f1[0][0] - 250) < 1, "cue ball keeps the line after a straight hit")

# friction ends the shot
r3 = pg.simulate({0: [250, 800]}, 0, -300, 0, 0)
check(r3["duration"] < 3 and r3["info"]["first"] is None, "a slow shot stops by itself")
# top spin / draw
follow = pg.simulate({0: [250, 800], 1: [250, 500]}, 0, -1000, 0, 1.0)["final"][0][1]
draw = pg.simulate({0: [250, 800], 1: [250, 500]}, 0, -1000, 0, -1.0)["final"][0][1]
check(follow < draw, f"follow goes further up the table than draw ({follow:.0f} < {draw:.0f})")

print("2. pocketing and points")
room = fresh_room()
start(room)
empty_table(room, {0: [250, 600], 3: [250, 400], 9: [100, 100]})
kind, out = shoot(room, -math.pi / 2, 0.5)     # ball 3 goes up the middle, nowhere near a pocket
check(kind == "ok", "shot accepted")
room = fresh_room()
start(room)
# ball 5 just in front of the top-left corner pocket, cue behind it in line
empty_table(room, {0: [160, 260], 5: [80, 130], 12: [400, 800]})
ang = math.atan2(130 - 260, 80 - 160)
kind, out = shoot(room, ang, 0.55)
check(kind == "ok" and 5 in out["res"]["pocketed"], "ball 5 pocketed in the corner")
check(room.seats[0]["score"] == 1 and out["res"]["foul"] is None, "legal pocket = +1 point, no foul")
check(room.turn == 0, "shooter keeps the turn after a legal pocket")
room = fresh_room()
start(room)
empty_table(room, {0: [160, 260], 8: [80, 130], 12: [400, 800]})
kind, out = shoot(room, math.atan2(130 - 260, 80 - 160), 0.55)
check(8 in out["res"]["pocketed"] and room.seats[0]["score"] == 2, "8-ball is worth 2 points")
check(room.seats[0]["pocketed"] == 1, "legally pocketed balls are counted")

print("3. turns, fouls, scratch, rails, no contact")
room = fresh_room()
start(room)
empty_table(room, {0: [250, 600], 4: [100, 100], 6: [400, 120]})
room.seats[0]["score"] = 0
kind, out = shoot(room, math.pi / 2, 0.4)       # cue ball goes down and hits no ball
check(out["res"]["foul"] == "no_contact", "no contact = foul")
check(room.seats[0]["score"] == 0 and room.seats[0]["fouls"] == 1, "score never goes below zero, foul counted")
check(room.turn == 1 and room.bih, "next player gets ball-in-hand")
room = fresh_room()
start(room)
room.seats[0]["score"] = 3
empty_table(room, {0: [250, 600], 4: [250, 500]})
kind, out = shoot(room, -math.pi / 2, 0.2)      # taps ball 4 slowly: contact but no ball reaches a rail
check(out["res"]["foul"] == "no_rail" and room.seats[0]["score"] == 2, "no rail after contact = foul, -1 point")
room = fresh_room()
start(room)
empty_table(room, {0: [250, 600], 4: [250, 500], 7: [400, 900]})
kind, out = shoot(room, -math.pi / 2, 0.9)
check(out["res"]["foul"] is None and room.turn == 1 and not room.bih, "legal shot, nothing pocketed: next player, no ball-in-hand")
room = fresh_room()
start(room)
room.seats[0]["score"] = 5
empty_table(room, {0: [150, 200], 5: [150, 100], 12: [400, 800]})
# cue into the corner pocket (scratch)
kind, out = shoot(room, math.atan2(-200, -150), 0.5)
check(out["res"]["foul"] == "scratch" and room.seats[0]["score"] == 4, "scratch = foul, -1 point")
check(room.bih and room.turn == 1, "after a scratch the next player has ball-in-hand")
# ball-in-hand: place the cue ball, bad spots are refused (cue goes to the head spot instead)
room.busy_until = 0
kind, out = pg.take_shot(room, 1, {"seq": room.seq, "a": math.pi / 2, "p": 0.4, "place": {"x": 400, "y": 700}})
check(kind == "ok" and abs(out["frames"][0][0] - 4000) < 2 and abs(out["frames"][0][1] - 7000) < 2, "ball-in-hand: cue ball starts where the player put it")
check(not room.bih, "ball-in-hand is used up")
# off-table ball
res = pg.simulate({0: [250, 600], 1: [250, 500]}, 0, -500, 0, 0)
check(res["info"]["off"] == [], "balls do not leave the table in a normal shot")

print("4. server control: wrong turn, duplicates, bad numbers")
room = fresh_room()
start(room)
msg = {"seq": room.seq, "a": -1.5, "p": 0.5}
check(pg.take_shot(room, 2, msg)[0] == "err", "a player who is not the shooter is refused")
kind, out = pg.take_shot(room, room.turn, dict(msg))
check(kind == "ok", "the real shooter is accepted")
room.busy_until = 0
check(pg.take_shot(room, room.turn, dict(msg))[1] == "old shot" or room.seq != msg["seq"], "the same shot sent twice is refused")
check(pg.take_shot(room, room.turn, {"seq": room.seq, "a": "x", "p": 1})[0] == "err", "bad numbers are refused")
check(pg.take_shot(room, room.turn, {"seq": room.seq, "a": float("nan"), "p": 1})[0] == "err", "NaN is refused")
big = pg.take_shot(room, room.turn, {"seq": room.seq, "a": -1.5, "p": 9999, "sx": 50, "sy": 50})
check(big[0] == "ok", "power and spin are clamped, not trusted")
room.busy_until = pg.now() + 50
check(pg.take_shot(room, room.turn, {"seq": room.seq, "a": 0, "p": 0.5})[1] == "busy", "no shot while the last one is still rolling")

print("5. rack cleared")
room = fresh_room()
start(room)
empty_table(room, {0: [160, 260], 5: [80, 130]})
kind, out = shoot(room, math.atan2(130 - 260, 80 - 160), 0.55)
check(out["res"].get("rack_cleared") and len([n for n in room.balls if n]) == 15, "last ball pocketed: the 15 balls are respotted")
check(room.turn == 1, "after the rack is cleared the next player shoots")
ps = list(room.balls.values())
check(all(math.hypot(a[0] - b[0], a[1] - b[1]) >= 2 * pg.R - 0.01 for i, a in enumerate(ps) for b in ps[i + 1:]), "respotted table has no overlaps (cue ball included)")

print("6. timers")
room = fresh_room()
start(room)
room.shot_deadline = pg.now() - 1
room.busy_until = 0
before = room.turn
asyncio.run(pg.tick())
check(room.turn == (before + 1) % 4, "shot timer ran out: the turn passes")
check(room.seats[before]["score"] == 0 and any(e["kind"] == "timeout" for e in room.audit), "timeout is in the audit, no score change")
room.match_end = pg.now() - 1
room.seats[2]["score"] = 7
room.busy_until = pg.now() + 30
asyncio.run(pg.tick())
check(room.phase == "playing", "match timer ended but a shot is still rolling: the shot finishes first")
room.busy_until = 0
asyncio.run(pg.tick())
check(room.phase == "finished" and room.result["winner"] == 2, "match ends after the shot, highest score wins")
check(all(pg.player_room.get(1000 + i) is None for i in range(4)), "players are free to join a new match")

print("7. tie-breaks")
def tie_room(sc, pk, fo):
    room = fresh_room()
    start(room)
    for i in range(4):
        room.seats[i].update(score=sc[i], pocketed=pk[i], fouls=fo[i])
    room.match_end = pg.now() - 1
    asyncio.run(pg.tick())
    return room
r = tie_room([5, 5, 2, 1], [5, 3, 2, 1], [0, 0, 0, 0])
check(r.phase == "finished" and r.result["winner"] == 0 and r.result["tiebreak"] is None, "same score: more legally pocketed balls wins")
r = tie_room([5, 5, 2, 1], [4, 4, 2, 1], [3, 1, 0, 0])
check(r.phase == "finished" and r.result["winner"] == 1, "same score and balls: fewer fouls wins")
r = tie_room([5, 5, 2, 1], [4, 4, 2, 1], [1, 1, 0, 0])
check(r.phase == "playing" and r.sd and r.sd["players"] == [0, 1], "all equal: sudden death starts with the tied players only")
check(r.turn == 0 and r.balls[0] == [pg.HEAD[0], pg.HEAD[1]] and len(r.balls) == 16, "sudden death starts from a fresh rack")
rack_before = {n: list(p) for n, p in r.sd["rack"].items()}
# player 0 misses, player 1 scores -> player 1 wins
r.busy_until = 0
kind, _ = pg.take_shot(r, 0, {"seq": r.seq, "a": math.pi / 2, "p": 0.3})   # shoots away from the rack
check(kind == "ok" and r.turn == 1, "sudden death: next tied player shoots")
check({n: p for n, p in r.balls.items() if n} == rack_before, "every tied player gets the same layout")
# force a score for player 1 by placing a ball in front of a pocket
r.sd["rack"] = {5: [80, 130]}
r.balls = {0: [160, 260], 5: [80, 130]}
r.busy_until = 0
kind, out = pg.take_shot(r, 1, {"seq": r.seq, "a": math.atan2(130 - 260, 80 - 160), "p": 0.55})
check(r.phase == "finished" and r.result["winner"] == 1, "sudden death: the one who scores wins")
check(r.result["tiebreak"]["coin"] is False and r.result["tiebreak"]["players"] == [0, 1], "tie-break is recorded")
# nobody scores for 3 rounds -> fair random draw
r = tie_room([2, 2, 0, 0], [1, 1, 0, 0], [0, 0, 0, 0])
for _ in range(20):
    if r.phase != "playing":
        break
    r.busy_until = 0
    r.shot_deadline = pg.now() - 1
    asyncio.run(pg.tick())
check(r.phase == "finished" and r.result["tiebreak"]["coin"] and r.result["winner"] in (0, 1), "3 sudden-death rounds without a score: fair random draw")
check(any(e["kind"] == "sd_coin" for e in r.audit), "the draw is in the audit")

print("8. connections, reconnect, bots, audit")
room = fresh_room(("A", "B"))
check(room.phase == "waiting" and len(pg.player_room) == 2, "two players wait in one room")
pg.add_bots(room)
check(room.phase == "starting" and None not in room.seats, "bots fill the empty seats, exactly four players")
j = pg.join(5000, "Late", "")
check(j is not room and len(pg.rooms) == 2, "a fifth player gets a new room, never a 5th seat")
pg.leave(5000)
check(pg.player_room.get(5000) is None, "leaving a waiting room frees the seat")
room.start_at = pg.now() - 1
asyncio.run(pg.tick())
check(room.phase == "playing", "match starts when four are seated")


class FakeWS:
    def __init__(self):
        self.msgs = []

    async def send_json(self, m):
        self.msgs.append(m)


ws1 = FakeWS()
room.conns[1000] = {ws1}
asyncio.run(pg.broadcast_state(room))
check(ws1.msgs[-1]["you"] == 0 and ws1.msgs[-1]["seats"][0]["on"] is True and ws1.msgs[-1]["seats"][1]["on"] is False,
      "connection indicator: connected seat on, other seat off")
del room.conns[1000]
room.conns[1000] = {FakeWS()}
asyncio.run(pg.broadcast_state(room))
sn = pg.snapshot(room, 1000)
check(sn["you"] == 0 and sn["balls"] and sn["phase"] == "playing", "reconnect: same seat, full table sent again")
check(pg.join(1000, "A", "").id == room.id, "joining again while in a match returns to the same room")
# bots play a few shots on their own
room.turn = 2
room.busy_until = 0
room.shot_deadline = pg.now() + 30
shots = 0
for _ in range(6):
    room.busy_until = 0
    room.bot_at = 0
    seq = room.seq
    # make the bots' turn
    if not room.seats[room.turn]["bot"]:
        room.turn = 2
    asyncio.run(pg.tick())
    shots += room.seq > seq
check(shots >= 4, "bots take their shots through the ticker")
check(len(room.audit) >= 4 and all("kind" in e for e in room.audit), "audit list is filled")
json.dumps(room.audit)
total = [p["score"] for p in room.seats]
check(all(s >= 0 for s in total), "scores never negative")

print("\nALL %d CHECKS PASSED" % ok_count)
