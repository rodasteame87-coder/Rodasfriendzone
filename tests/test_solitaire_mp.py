"""
Tests for solitaire_mp.py  -  run:   python test_solitaire_mp.py        (or:  pytest -q test_solitaire_mp.py)

Needs only solitaire.py and solitaire_mp.py in the same folder. No database, no Telegram, no aiohttp:
the wallet, the clock and the database are faked, so every case runs in a few milliseconds.

Cases:  4 players (deal, money, hidden cards)  ·  win with all 52 cards  ·  tie-break  ·  most cards at time-up
        refund: waiting room not full  ·  refund: player cancels  ·  refund: nobody got a card (no fee)
        double "Find match"  ·  illegal / repeated moves  ·  restart in the middle of a match  ·  move limit
        login / ban / closed switch  ·  money check with 400 random actions (not one ETB created or lost)
"""
import asyncio
import copy
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solitaire as sol          # noqa: E402
import solitaire_mp as qm        # noqa: E402

START_MONEY = 1000
REAL_DEAL = sol.new_deal
REAL_ACT = qm.act


def _phone_act(uid, action, src=None, dst=None, n=1, seq=None):
    """Like the phone: move/auto always carry the move counter (unless a test sends its own seq)."""
    m = qm.by_uid.get(uid)
    if seq is None and m is not None and m["status"] == "active" and action in ("move", "auto"):
        seq = qm._mine(m, uid)["s"]["moves"] + 1
    return REAL_ACT(uid, action, src, dst, n, seq)


qm.act = _phone_act


# ---------------------------------------------------------------- fakes
class FakeDB:
    """Remembers what the bot would write to Postgres, so a restart can be simulated."""
    def __init__(self):
        self.matches, self.players = {}, {}

    def enqueue(self, sql, *a):
        if sql == qm._UP_MATCH:
            self.matches[a[0]] = dict(zip(qm._MCOLS, a))
        elif sql == qm._UP_PLAYER:
            self.players[a[0]] = dict(zip(qm._PCOLS, a))
        else:
            raise AssertionError("unexpected SQL: " + sql[:60])


class FakeConn:
    def __init__(self, db):
        self.db = db

    async def fetch(self, sql, *args):
        if "FROM qm_matches" in sql:
            return [dict(r) for _, r in sorted(self.db.matches.items()) if r["status"] in ("waiting", "active")]
        if "FROM qm_players" in sql:
            ids = set(args[0])
            return [dict(r) for _, r in sorted(self.db.players.items())
                    if r["result"] == "pending" and r["match_id"] in ids]
        raise AssertionError(sql[:60])

    async def fetchval(self, sql, *args):
        return max(list(self.db.matches) + list(self.db.players) + [0])

    async def fetchrow(self, sql, *args):
        done = [r for r in self.db.matches.values() if r["status"] == "done"]
        ref = [r for r in self.db.matches.values() if r["status"] == "refunded" and r["reason"] != "cancel"]
        return {"n": len(done), "b": sum(r["pot"] for r in done), "p": sum(r["prize"] for r in done), "r": len(ref)}


class Env:
    def __init__(self, players=8, money=START_MONEY):
        self.t = 1_700_000_000.0
        self.db = FakeDB()
        self.wallets = {u: money for u in range(1, players + 1)}
        self.wagered, self.required, self.logs, self.sent = {}, {}, [], []
        self.names = {u: f"Player{u}" for u in self.wallets}
        self.phones = {u: f"09{u}" for u in self.wallets}
        self.banned, self.admin = set(), 1
        self.initial = sum(self.wallets.values())

    def deps(self):
        async def send(uid, text, **kw):
            self.sent.append((uid, text))
        return dict(
            wallets=self.wallets, wagered=self.wagered, names=self.names, phones=self.phones, banned=self.banned,
            admin_id=self.admin, enqueue=self.db.enqueue, pool=None, send=send,
            turnover_left=lambda uid: max(0, self.required.get(uid, 0) - self.wagered.get(uid, 0)),
            log=lambda uid, kind, amount, note="", hidden=False: self.logs.append((uid, kind, amount, note)),
            verify=lambda init: {"id": int(init)} if str(init).isdigit() else None,
            touch=lambda user: user["id"])


def fresh(players=8, money=START_MONEY, enabled=True):
    env = Env(players, money)
    for d in (qm.matches, qm.by_uid, qm.waiting, qm.recent):
        d.clear()
    qm._outbox.clear()
    qm.stats.update(games=0, bets=0, paid=0, refunds=0)
    qm.D.clear(); sol.D.clear()
    sol.init(**env.deps()); qm.init(**env.deps())
    qm._now = lambda: env.t
    qm.ENABLED = enabled
    qm.PRIZE_PERCENT = 75
    qm.WAIT_SECONDS, qm.TIME_LIMIT = 60, 360
    sol.new_deal = REAL_DEAL
    sol.MOVES_NORMAL, sol.MOVES_HARD = 200, 150
    return env


def fill(env, bet=50, users=(1, 2, 3, 4)):
    for u in users:
        r = qm.join(u, bet)
        assert r["ok"], r
    return qm.by_uid[users[0]]


def escrow():
    return sum(p["bet"] for m in qm.matches.values() for p in m["players"])


def assert_money(env):
    """initial money = wallets + bets inside open matches + house profit. Always."""
    house = qm.stats["bets"] - qm.stats["paid"]
    total = sum(env.wallets.values()) + escrow() + house
    assert total == env.initial, f"money leak: {total} != {env.initial}"
    assert all(v >= 0 for v in env.wallets.values())


def deal_aces():
    """A♠ A♣ A♦ A♥ and 2♠ 2♣ 2♦ are face up on top of the columns: easy to score with real moves."""
    top = [0, 13, 26, 39, 1, 14, 27]
    rest = [c for c in range(52) if c not in top]
    return {"stock": rest, "waste": [], "found": [0, 0, 0, 0], "tab": [[[c, 1]] for c in top], "moves": 0, "pass": 1}


def deal_sorted():
    """All 52 cards face up, Ace on top of every column: 'auto' finishes the game at once."""
    tab = [[[s * 13 + r, 1] for r in range(12, -1, -1)] for s in range(4)] + [[], [], []]
    return {"stock": [], "waste": [], "found": [0, 0, 0, 0], "tab": tab, "moves": 0, "pass": 1}


def put(uid, pile):
    """one real move: the top card of `pile` to its foundation"""
    r = qm.act(uid, "move", pile, "f", 1)
    assert r["ok"], r
    return r


# ---------------------------------------------------------------- cases
def test_four_players_same_deal_hidden_cards():
    env = fresh()
    for u in (1, 2, 3):
        r = qm.join(u, 50)
        assert r["ok"] and r["phase"] == "waiting" and r["m"]["n"] == u
    assert [env.wallets[u] for u in (1, 2, 3)] == [950] * 3
    r = qm.join(2, 50)                                            # pressing "Find match" again
    assert r["ok"] and r.get("resumed") and env.wallets[2] == 950, "second press must not charge"
    r = qm.join(4, 50)
    assert r["phase"] == "active" and r["m"]["status"] == "active"
    m = qm.by_uid[1]
    boards = [json.dumps(p["s"], sort_keys=True) for p in m["players"]]
    assert len(set(boards)) == 1, "everybody must get the SAME deal"
    cards = [c for col in m["players"][0]["s"]["tab"] for c, _ in col] + m["players"][0]["s"]["stock"]
    assert sorted(cards) == list(range(52)), "a deal has 52 different cards"
    m["players"][0]["s"]["found"][0] = 3                          # boards are private copies
    assert m["players"][1]["s"]["found"][0] == 0
    m["players"][0]["s"]["found"][0] = 0
    v = qm.state(2)["m"]
    assert all(c == -1 for col in v["tab"] for c, up in col if not up), "face-down cards must be -1"
    assert "stock" in v and isinstance(v["stock"], int), "the phone only gets the NUMBER of stock cards"
    for row in v["players"]:
        assert set(row) == {"rank", "name", "cards", "me", "done", "quit"}, "others: name and count only"
    assert sum(1 for row in v["players"] if row["me"]) == 1
    assert qm.state(2, light=True)["m"].get("tab") is None
    assert escrow() == 200 and len(qm.waiting) == 0
    assert_money(env)
    print("PASS  4 players: same deal, private boards, hidden cards, no double charge")


def test_win_all_cards_pays_prize_from_pot():
    env = fresh()
    sol.new_deal = deal_sorted
    fill(env, 50)
    before = sum(env.wallets.values())
    r = qm.act(3, "auto")
    assert r["ok"] and r["ended"] and r["result"]["winner_me"] and r["result"]["prize"] == 150
    assert env.wallets[3] == START_MONEY - 50 + 150
    assert [env.wallets[u] for u in (1, 2, 4)] == [950] * 3
    assert qm.stats == {"games": 1, "bets": 200, "paid": 150, "refunds": 0}, qm.stats
    assert qm.stats["bets"] - qm.stats["paid"] == 50, "house keeps 25% of 200"
    assert not qm.by_uid and not qm.matches
    r2 = qm.act(3, "auto")                                        # a late / repeated request can not pay twice
    assert not r2["ok"] and r2.get("ended") and env.wallets[3] == 1100
    wins = [x for x in env.logs if x[1] == "win"]
    assert len(wins) == 1 and wins[0][:3] == (3, "win", 150)
    assert_money(env)
    assert qm.state(1)["result"]["mine"]["result"] == "lost" and qm.state(1)["result"]["winner"] == "Player3"
    for bet, prize in ((100, 300), (200, 600)):                   # the other queues
        qm.PRIZE_PERCENT = 75
        m = fill(env, bet, (5, 6, 7, 8))
        assert (m["pot"], m["prize"]) == (bet * 4, prize)
        qm.act(5, "auto")
        assert env.wallets[5] == START_MONEY - bet + prize
        env.wallets.update({5: START_MONEY, 6: START_MONEY, 7: START_MONEY, 8: START_MONEY}); env.initial = sum(env.wallets.values()) + 0
        qm.stats.update(games=0, bets=0, paid=0)
        env.initial = sum(env.wallets.values())
    print("PASS  first with 52 cards wins; prize 150/300/600 paid only from the pot; no double pay")


def test_prize_percent_is_config_and_fixed_per_match():
    env = fresh()
    sol.new_deal = deal_sorted
    m = fill(env, 50)
    assert m["prize"] == 150
    qm.PRIZE_PERCENT = 60                                         # changed while the match is running
    qm.act(1, "auto")
    assert env.wallets[1] == START_MONEY - 50 + 150, "a running match keeps the prize it started with"
    m2 = fill(env, 50, (5, 6, 7, 8))
    assert m2["prize"] == 120 and m2["fee"] == 80, "a new match uses the new percent"
    qm.PRIZE_PERCENT = 250                                        # nonsense value can never pay more than the pot
    assert qm.prize_for(50)[1] == 200
    print("PASS  PRIZE_PERCENT is a config value; prize never above the pot")


def test_refund_waiting_room_not_full():
    env = fresh()
    env.required = {1: 40}
    qm.join(1, 50)                                                # ONE player is not a match
    assert env.wagered[1] == 40 and escrow() == 50
    env.t += 59; qm.tick()
    assert qm.by_uid, "still waiting at 59 s"
    assert qm.state(1)["m"]["left"] == 1
    env.t += 1; qm.tick()
    assert not qm.by_uid and not qm.waiting and not qm.matches
    assert env.wallets[1] == START_MONEY
    assert env.wagered[1] == 0, "the play-through of a refunded bet is undone"
    r = qm.state(1)
    assert r["phase"] == "idle" and r["result"]["status"] == "refunded" and r["result"]["mine"]["result"] == "refund"
    assert qm.stats["refunds"] == 1 and qm.stats["games"] == 0
    assert_money(env)
    r = qm.join(4, 50)                                            # the next player opens a NEW waiting room
    assert r["m"]["n"] == 1
    asyncio.run(qm._flush_outbox())
    assert env.sent == [], "players get no Telegram message from Quick Match"
    print("PASS  refund: only 1 player after 60 s -> refunded, play-through undone")


def test_starts_with_2_or_3_players_when_wait_is_over():
    for n in (2, 3):
        env = fresh()
        sol.new_deal = deal_aces
        users = tuple(range(1, n + 1))
        for u in users:
            qm.join(u, 100)
        w = qm.state(1)["m"]
        assert w["n"] == n and w["prize"] == 100 * n * 75 // 100 and w["max_prize"] == 300 and w["min"] == 2
        env.t += 59; qm.tick()
        assert qm.state(1)["phase"] == "waiting", "not before the wait time is over"
        env.t += 1; qm.tick()
        m = qm.by_uid[1]
        assert m["status"] == "active" and len(m["players"]) == n and not qm.waiting
        assert m["pot"] == 100 * n and m["prize"] == 100 * n * 75 // 100 and m["fee"] == m["pot"] - m["prize"]
        assert len({json.dumps(p["s"], sort_keys=True) for p in m["players"]}) == 1, "same deal for everybody"
        r = qm.join(8, 100)                                       # a new player opens a NEW waiting room
        assert r["phase"] == "waiting" and qm.by_uid[8] is not m
        put(2, "t0")
        env.t += 360; qm.tick()
        assert env.wallets[2] == START_MONEY - 100 + 100 * n * 75 // 100, n
        assert qm.stats["games"] == 1 and qm.stats["bets"] == 100 * n and qm.stats["paid"] == 100 * n * 75 // 100
        assert_money(env)
    print("PASS  2 or 3 players: match starts when the wait is over, prize = 75% of THEIR pot")


def test_restart_keeps_a_2_player_match():
    env = fresh()
    sol.new_deal = deal_aces
    qm.join(1, 50); qm.join(2, 50)
    env.t += 60; qm.tick()
    put(1, "t0")
    for d in (qm.matches, qm.by_uid, qm.waiting, qm.recent):
        d.clear()
    asyncio.run(qm.load_state(FakeConn(env.db)))
    m = qm.by_uid[1]
    assert m["status"] == "active" and len(m["players"]) == 2 and m["pot"] == 100 and m["prize"] == 75
    env.t += 360; qm.tick()
    assert env.wallets[1] == START_MONEY - 50 + 75 and env.wallets[2] == START_MONEY - 50
    assert_money(env)
    print("PASS  restart: a 2-player match is kept (not refunded) and paid from its own pot")


def test_cancel_in_waiting_room():
    env = fresh()
    qm.join(1, 50); qm.join(2, 50)
    r = qm.cancel(1)
    assert r["ok"] and env.wallets[1] == START_MONEY and 1 not in qm.by_uid
    assert qm.waiting[50]["players"][0]["uid"] == 2, "the other player keeps waiting"
    qm.join(1, 50)                                                # cancel + join again = charged once
    assert env.wallets[1] == START_MONEY - 50
    qm.cancel(1); qm.cancel(2)
    assert not qm.matches and not qm.waiting and env.wallets[2] == START_MONEY
    assert qm.cancel(2)["ok"] is False
    fill(env, 50, (1, 2, 3, 4))
    r = qm.cancel(1)
    assert not r["ok"] and env.wallets[1] == START_MONEY - 50, "no cancel after the start"
    assert_money(env)
    print("PASS  cancel while waiting returns the bet; no cancel after start; no double charge")


def test_refund_when_nobody_has_a_card_no_fee():
    env = fresh()
    fill(env, 100)
    for u in (1, 2, 3, 4):
        assert qm.act(u, "draw")["ok"]
    env.t += 359; qm.tick()
    assert qm.by_uid, "still playing at 5:59"
    env.t += 1; qm.tick()
    assert not qm.by_uid
    assert [env.wallets[u] for u in (1, 2, 3, 4)] == [START_MONEY] * 4, "everybody gets the bet back, no fee"
    assert qm.stats == {"games": 0, "bets": 0, "paid": 0, "refunds": 1}
    res = qm.state(1)["result"]
    assert res["reason"] == "nocards" and res["mine"]["result"] == "refund" and res["prize"] == 0
    assert_money(env)
    print("PASS  refund: time over and all players have 0 cards -> everybody refunded, house fee 0")


def test_time_up_most_cards_wins():
    env = fresh()
    sol.new_deal = deal_aces
    fill(env, 50)
    put(2, "t0"); put(2, "t1")                                    # player 2: 2 cards
    put(4, "t0")                                                  # player 4: 1 card
    env.t += 360; qm.tick()
    assert env.wallets[2] == START_MONEY - 50 + 150 and env.wallets[4] == START_MONEY - 50
    assert qm.state(4)["result"]["winner"] == "Player2" and qm.state(4)["result"]["reason"] == "time"
    assert_money(env)
    print("PASS  time over: most cards on the foundations wins")


def test_tie_goes_to_who_reached_the_count_first():
    env = fresh()
    sol.new_deal = deal_aces
    fill(env, 50)
    for u, piles in ((3, ("t0", "t4")),                           # player 3 (seat 3) reaches 2 cards FIRST ...
                     (1, ("t0",))):
        for pile in piles:
            env.t += 5
            put(u, pile)
    env.t += 5; put(1, "t4")                                      # ... player 1 (seat 1) reaches 2 cards later
    env.t += 5; put(2, "t0")                                      # player 2 only 1 card
    assert qm.state(1)["m"]["players"][0]["cards"] == 2
    env.t += 400; qm.tick()
    assert env.wallets[3] == START_MONEY - 50 + 150, "the player who reached 2 cards first must win the tie"
    assert env.wallets[1] == START_MONEY - 50 and env.wallets[2] == START_MONEY - 50
    assert qm.state(1)["result"]["winner"] == "Player3"
    assert_money(env)
    # the same tie, the other way round: seat order must not decide, the clock does
    env2 = fresh(); sol.new_deal = deal_aces
    fill(env2, 50)
    for u in (4, 2):
        for pile in ("t0", "t4"):
            env2.t += 3; put(u, pile)
    env2.t += 400; qm.tick()
    assert env2.wallets[4] == START_MONEY - 50 + 150 and env2.wallets[2] == START_MONEY - 50
    # still tied on the clock -> the player who joined first
    env3 = fresh(); sol.new_deal = deal_aces
    m = fill(env3, 50)
    put(3, "t0"); put(2, "t0")                                    # same second
    env3.t += 400; qm.tick()
    assert env3.wallets[2] == START_MONEY - 50 + 150, "same second: first seat wins"
    print("PASS  tie: the player who reached the count first wins (seat order only as last resort)")


def test_illegal_and_repeated_moves():
    env = fresh()
    sol.new_deal = deal_aces
    fill(env, 50)
    before = json.dumps(qm.by_uid[1]["players"][0]["s"], sort_keys=True)
    for bad in (("move", "t5", "f", 1),                           # 2♣ is not an ace
                ("move", "t0", "t0", 1), ("move", "zz", "f", 1), ("move", "t0", "t9", 1),
                ("move", "t0", "t1", 5), ("move", "w", "f", 1), ("move", "f0", "f", 1),
                ("move", {"x": 1}, "f", 1), ("fly", None, None, 1)):
        r = qm.act(1, bad[0], bad[1], bad[2], bad[3])
        assert not r["ok"], bad
    assert json.dumps(qm.by_uid[1]["players"][0]["s"], sort_keys=True) == before, "bad moves must change nothing"
    r = qm.act(1, "move", "t0", "f", 1, seq=1)
    assert r["ok"] and r["found"][0] == 1
    r = qm.act(1, "move", "t0", "f", 1, seq=1)                    # the answer got lost, the phone asks again
    assert r["ok"] and r.get("dup") and r["found"][0] == 1 and qm.by_uid[1]["players"][0]["s"]["moves"] == 1
    r = qm.act(1, "move", "t4", "f", 1, seq=7)
    assert not r["ok"] and r.get("sync")
    r = qm.act(9, "draw")                                         # a player that is not in a match
    assert not r["ok"] and r.get("none")
    # the stock-pass limit of solitaire.py still works through the shared code
    assert_money(env)
    print("PASS  server validates every move; repeated request does not act twice")


def test_move_limit_ends_boards_and_match():
    env = fresh()
    sol.new_deal = deal_aces
    sol.MOVES_NORMAL = 3
    fill(env, 50)
    put(2, "t0")                                                  # player 2 scores 1 card (move 1) ...
    for u in (1, 2, 3, 4):
        while True:
            r = qm.act(u, "draw")
            if r.get("ended") or r.get("reason") == "moves":
                break
    assert not qm.by_uid, "all boards finished -> match settled at once"
    assert env.wallets[2] == START_MONEY - 50 + 150
    assert qm.state(1)["result"]["reason"] == "moves"
    assert_money(env)
    print("PASS  move limit: finished boards keep their cards; match settles when all boards are finished")


def test_restart_loses_nothing():
    env = fresh()
    sol.new_deal = deal_aces
    qm.join(7, 50)                                                # a waiting room (1 player) that survives the restart
    fill(env, 100, (1, 2, 3, 4))
    env.t += 5; put(2, "t0"); env.t += 5; put(2, "t4"); env.t += 5; put(3, "t0"); qm.act(1, "draw")
    snap = json.dumps(qm.by_uid[2]["players"][1]["s"], sort_keys=True)
    # --- the bot restarts: memory is gone, only the database is left
    for d in (qm.matches, qm.by_uid, qm.waiting, qm.recent):
        d.clear()
    qm._last_id[0] = 0
    qm.stats.update(games=0, bets=0, paid=0, refunds=0)
    asyncio.run(qm.load_state(FakeConn(env.db)))
    m = qm.by_uid[2]
    assert m["status"] == "active" and len(m["players"]) == 4 and m["bet"] == 100
    assert json.dumps(m["players"][1]["s"], sort_keys=True) == snap, "board must be exactly as it was"
    assert qm.waiting[50]["players"][0]["uid"] == 7 and len(qm.waiting[50]["players"]) == 1
    assert qm.state(2)["m"]["players"][0]["cards"] == 2
    put(3, "t4")                                                  # the game goes on: player 3 now also has 2 cards, later
    assert qm.act(2, "draw", seq=3)["ok"], "the phone's move counter continues"
    env.t += 400; qm.tick()                                       # ticker settles the match that ran out of time
    assert env.wallets[2] == START_MONEY - 100 + 300 and not any(u in qm.by_uid for u in (1, 2, 3, 4))
    assert 7 not in qm.by_uid, "waiting room ran out of time while the bot was down"
    assert env.wallets[7] == START_MONEY, "waiting room with 1 player refunded after the restart"
    old_max = max(list(env.db.matches) + list(env.db.players))
    assert qm.join(1, 50)["m"]["id"] > old_max, "new ids must continue after the restart"
    ids = list(env.db.matches)
    assert len(ids) == len(set(ids))
    assert_money(env)
    # a broken match (no board saved) is refunded, never kept
    env4 = fresh(); fill(env4, 50)
    for p in env4.db.players.values():
        p["board"] = None
    for d in (qm.matches, qm.by_uid, qm.waiting, qm.recent):
        d.clear()
    asyncio.run(qm.load_state(FakeConn(env4.db)))
    assert not qm.by_uid and [env4.wallets[u] for u in (1, 2, 3, 4)] == [START_MONEY] * 4
    print("PASS  restart: boards, waiting rooms, move counters restored; ticker settles; broken match refunded")


def test_login_ban_phone_and_closed_switch():
    env = fresh(enabled=False)
    run = asyncio.run
    assert run(qm.handle_state({"initData": "bad"}))["error"] == "auth"
    assert run(qm.handle_join({"initData": "2", "bet": 50}))["error"] == "closed"
    assert run(qm.handle_join({"initData": "1", "bet": 50}))["ok"], "the admin can play while it is closed"
    qm.ENABLED = True
    env.banned.add(3)
    assert run(qm.handle_join({"initData": "3", "bet": 50}))["error"] == "blocked"
    del env.phones[4]
    assert run(qm.handle_join({"initData": "4", "bet": 50}))["needPhone"]
    assert run(qm.handle_join({"initData": "2", "bet": 75}))["ok"] is False
    env.wallets[5] = 20
    assert "balance" in run(qm.handle_join({"initData": "5", "bet": 50}))["error"].lower() \
        or "ሂሳብ" in run(qm.handle_join({"initData": "5", "bet": 50}))["error"]
    assert run(qm.handle_join({"initData": "5", "bet": 50}))["ok"] is False and env.wallets[5] == 20
    assert run(qm.handle_state({"initData": "2", "light": "1"}))["phase"] == "idle"
    assert run(qm.handle_history({"initData": "1"}))["rows"][0]["result"] == "active"
    print("PASS  verify / ban / phone / allowed-ENABLED switch / bad bet / no balance")


def test_money_never_created_or_lost_400_random_actions():
    for seed in (1, 2, 3):
        rnd = random.Random(seed)
        env = fresh(players=12, money=500)
        for step in range(400):
            u = rnd.randint(1, 12)
            x = rnd.random()
            if x < 0.30:
                qm.join(u, rnd.choice(qm.BETS))
            elif x < 0.38:
                qm.cancel(u)
            elif x < 0.80:
                m = qm.by_uid.get(u)
                if m and m["status"] == "active":
                    v = qm.state(u)["m"]
                    a = rnd.choice(["draw", "move", "move", "move", "auto"])
                    srcs = ["w", "t0", "t1", "t2", "t3", "t4", "t5", "t6", "f0", "f1"]
                    dsts = ["f", "t0", "t1", "t2", "t3", "t4", "t5", "t6"]
                    qm.act(u, a, rnd.choice(srcs), rnd.choice(dsts), rnd.randint(1, 3))
            else:
                env.t += rnd.choice([1, 10, 61, 200, 361])
                qm.tick()
            assert_money(env)
        env.t += 1000; qm.tick(); qm.tick()                       # waiting rooms start (2+) or are refunded (1)
        env.t += 400; qm.tick(); qm.tick()                        # every match runs out of time
        assert not qm.matches, "no match stays open"
        env.t += 100; qm.tick()
        assert not qm.matches and not qm.by_uid
        assert_money(env)
        assert sum(env.wallets.values()) == env.initial - (qm.stats["bets"] - qm.stats["paid"])
    print("PASS  money check: 3 x 400 random actions, not one ETB created or lost")


def test_new_match_clears_old_result():
    env = fresh()
    sol.new_deal = deal_sorted
    fill(env, 50)
    qm.act(3, "auto")
    assert "result" in qm.state(1), "the old result is shown right after the match"
    qm.join(1, 50)
    r = qm.state(1)
    assert r["phase"] == "waiting" and "result" not in r, "old result must be gone in the new match"
    print("PASS  joining a new match removes the old result")


def test_move_and_auto_need_seq():
    env = fresh()
    sol.new_deal = deal_aces
    fill(env, 50)
    before = json.dumps(qm.by_uid[1]["players"][0]["s"], sort_keys=True)
    for action in ("move", "auto"):
        r = REAL_ACT(1, action, "t0", "f", 1)                     # no seq at all
        assert not r["ok"] and r.get("sync"), action
    assert json.dumps(qm.by_uid[1]["players"][0]["s"], sort_keys=True) == before
    assert REAL_ACT(1, "draw")["ok"], "draw is harmless and may go without seq"
    r = REAL_ACT(1, "move", "t0", "f", 1, seq=2)
    assert r["ok"] and r["found"][0] == 1
    qm.REQUIRE_SEQ = False
    assert REAL_ACT(1, "move", "t1", "f", 1)["ok"], "switch off = old behaviour"
    qm.REQUIRE_SEQ = True
    assert_money(env)
    print("PASS  move/auto without seq are refused (draw is allowed); switch QM_REQUIRE_SEQ works")


def test_restart_refund_is_counted():
    env = fresh()
    fill(env, 50)
    for p in env.db.players.values():
        p["board"] = None                                          # broken match
    env.db.matches[999] = dict(zip(qm._MCOLS, (999, 50, "refunded", 1, None, 2, 0, 0, 0, 75, None, "waiting")))
    for d in (qm.matches, qm.by_uid, qm.waiting, qm.recent):
        d.clear()
    qm.stats.update(games=0, bets=0, paid=0, refunds=0)
    late = []
    qm.D["enqueue"] = lambda *a: late.append(a)                    # like the real bot: the write is queued, not done yet
    asyncio.run(qm.load_state(FakeConn(env.db)))
    assert late, "the restart refund was queued"
    assert qm.stats["refunds"] == 2, qm.stats                      # 1 old refund + 1 refund made by the restart
    assert [env.wallets[u] for u in (1, 2, 3, 4)] == [START_MONEY] * 4
    print("PASS  restart: the refund made at start-up is counted once")


def test_same_limits_for_every_bet():
    env = fresh()
    sol.MOVES_NORMAL, sol.MOVES_HARD = 200, 150
    sol.PASSES_NORMAL, sol.PASSES_HARD = 3, 2
    seen = set()
    for bet, users in ((50, (1, 2, 3, 4)), (100, (5, 6, 7, 8))):
        fill(env, bet, users)
        v = qm.state(users[0])["m"]
        assert "hard" not in v and v["bet"] == bet and "passes" not in v and "passes_max" not in v
        seen.add(v["moves_max"])
    env = fresh(players=4)
    fill(env, 200)
    v = qm.state(1)["m"]
    seen.add(v["moves_max"])
    assert seen == {200}, seen
    cfg = qm.state(1)["cfg"]
    assert "hard_from" not in cfg and "moves_hard" not in cfg and "passes" not in cfg
    print("PASS  fair: 50 / 100 / 200 ETB matches all get the same move limit (no hard rule, no pass limit)")


def _craft(p, stock, waste, found, tab, moves=0):
    p["s"] = {"stock": stock, "waste": waste, "found": found, "tab": tab, "moves": moves, "pass": 1}
    qm._progress(p)


def test_unlimited_passes():
    env = fresh()
    sol.new_deal = deal_aces
    fill(env, 50)
    for i in range(70):                                           # 45 stock cards / 3 = 15 draws per pass: 70 draws = 4+ passes
        r = qm.act(1, "draw")
        assert r["ok"], (i, r)
        assert "passes" not in r and "passes_max" not in r
    assert qm.by_uid[1]["players"][0]["s"]["moves"] == 70 and not qm.by_uid[1]["players"][0]["done"]
    sol.MOVES_NORMAL = 75                                         # the move limit still ends the board
    r = None
    for i in range(10):
        r = qm.act(1, "draw")
        if r.get("reason") == "moves":
            break
    assert r.get("reason") == "moves" and r["done"] is True and r["moves"] == 75
    r = qm.act(1, "draw")
    assert not r["ok"] and "finished" in r["error"]
    print("PASS  unlimited passes in Quick Match; the move limit still finishes the board")


def test_no_legal_move_finishes_board():
    env = fresh()
    fill(env, 50)
    m = qm.by_uid[1]
    p = qm._mine(m, 1)
    stuck_tab = lambda last: [[[6, 1]], [[9, 1]], [[22, 1]], [[35, 1]], [[48, 1]], [[11, 1]], last]
    # the waste card 6 of hearts (44) goes on the 7 of spades (6); after that NO move exists
    _craft(p, [], [44], [0, 0, 0, 0], stuck_tab([[24, 1]]))
    assert qm._has_move(p) is True
    r = qm.act(1, "move", "w", "t0", 1)
    assert r["ok"] and r["reason"] == "stuck" and r["done"] is True, r
    assert qm.by_uid[1]["players"][0]["done"] is True
    assert qm._has_move(qm._mine(m, 1)) is False
    r = qm.act(1, "draw")
    assert not r["ok"] and "finished" in r["error"]
    # the same board, but an ace is free to go up: a legal move exists, so the board must NOT be finished
    env2 = fresh()
    fill(env2, 50)
    p2 = qm._mine(qm.by_uid[1], 1)
    _craft(p2, [], [44], [0, 0, 0, 0], stuck_tab([[0, 1]]))
    r = qm.act(1, "move", "w", "t0", 1)
    assert r["ok"] and not r.get("reason") and r["done"] is False, r
    assert qm._has_move(qm._mine(qm.by_uid[1], 1)) is True
    assert qm.act(1, "move", "t6", "f", 1)["ok"]
    print("PASS  no legal move left -> board finished; a free move means NOT finished")


def test_match_ends_at_once_when_decided():
    env = fresh()
    fill(env, 50)
    m = qm.by_uid[1]
    P = {u: qm._mine(m, u) for u in (1, 2, 3, 4)}
    P[1]["s"]["found"] = [5, 0, 0, 0]; qm._progress(P[1]); P[1]["done"] = True     # leader, finished, 5 cards
    P[2]["s"]["moves"] = 198                                       # 2 moves left: can reach 2 cards at most
    P[4]["s"]["moves"] = 198
    P[3]["s"]["moves"] = 199
    r = qm.act(3, "draw")                                          # player 3 reaches the move limit
    assert r.get("ended") and r["result"]["reason"] == "decided", r
    assert not qm.by_uid and env.wallets[1] == START_MONEY - 50 + 150
    assert_money(env)
    # NOT sure: a player who still has many moves can catch up -> the match goes on
    env = fresh()
    fill(env, 50)
    m = qm.by_uid[1]
    P = {u: qm._mine(m, u) for u in (1, 2, 3, 4)}
    P[1]["s"]["found"] = [5, 0, 0, 0]; qm._progress(P[1]); P[1]["done"] = True
    P[3]["s"]["moves"] = 199
    r = qm.act(3, "draw")
    assert r.get("reason") == "moves" and not r.get("ended") and qm.by_uid, "players 2 and 4 can still win"
    # NOT sure: a player who could still auto-complete the deck (few face-down cards, empty stock/waste) keeps the match open
    env = fresh()
    fill(env, 50)
    m = qm.by_uid[1]
    P = {u: qm._mine(m, u) for u in (1, 2, 3, 4)}
    P[1]["s"]["found"] = [13, 2, 0, 0]; qm._progress(P[1]); P[1]["done"] = True       # 15 cards: 10 moves alone could not catch up
    _craft(P[2], [], [], [0, 0, 0, 0], [[[12 + 13 * i, 1]] for i in range(4)] + [[], [], []], moves=190)
    P[2]["s"]["moves"] = 190
    P[4]["done"] = True
    P[3]["s"]["moves"] = 199
    r = qm.act(3, "draw")
    assert r.get("reason") == "moves" and not r.get("ended") and qm.by_uid, "player 2 has 10 moves and could auto-complete"
    print("PASS  match ends at once only when nobody can beat the leader (100% sure); otherwise it goes on")


def test_give_up_means_lost():
    # 1) the leader gives up: his cards do not count any more, another player wins
    env = fresh()
    sol.new_deal = deal_aces
    fill(env, 50)
    put(1, "t0")                                                  # player 1 leads with 1 card
    r = qm.act(1, "quit")
    assert r["ok"] and r["reason"] == "quit" and r["quit"] and r["done"] and not r.get("ended")
    sb = qm.state(2)["m"]["players"]
    assert sb[-1]["quit"] and not sb[-1]["me"], "a player who gave up is shown last"
    assert not qm.act(1, "draw")["ok"] and not qm.act(1, "quit")["ok"]
    put(3, "t0")
    env.t += 360; qm.tick()
    assert env.wallets[3] == START_MONEY - 50 + 150 and env.wallets[1] == START_MONEY - 50
    assert qm.state(1)["result"]["mine"]["result"] == "lost"
    assert_money(env)
    # 2) only one player is left who did not give up: he wins at once, even with 0 cards
    env = fresh()
    fill(env, 50)
    qm.act(1, "quit"); qm.act(2, "quit")
    assert qm.by_uid, "two players are still in"
    r = qm.act(3, "quit")
    assert r.get("ended") and r["result"]["reason"] == "quit" and not r["result"]["winner_me"]
    assert env.wallets[4] == START_MONEY - 50 + 150 and all(env.wallets[u] == START_MONEY - 50 for u in (1, 2, 3))
    assert not qm.by_uid and qm.state(4)["result"]["winner_me"]
    assert_money(env)
    # 3) two players: one gives up, the other wins at once from the pot of the two
    env = fresh()
    qm.join(1, 100); qm.join(2, 100)
    env.t += 60; qm.tick()
    r = qm.act(2, "quit")
    assert r.get("ended") and env.wallets[1] == START_MONEY - 100 + 150 and env.wallets[2] == START_MONEY - 100
    assert_money(env)
    # 4) "gave up" survives a restart
    env = fresh()
    fill(env, 50)
    qm.act(1, "quit")
    for d in (qm.matches, qm.by_uid, qm.waiting, qm.recent):
        d.clear()
    asyncio.run(qm.load_state(FakeConn(env.db)))
    q1 = qm._mine(qm.by_uid[1], 1)
    assert q1["quit"] is True and q1["done"] is True
    assert qm._ranked(qm.by_uid[1])[-1] is q1
    print("PASS  give up = lost: can not win, last one standing wins at once, money exact, survives restart")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nALL {len(tests)} TESTS PASSED")
