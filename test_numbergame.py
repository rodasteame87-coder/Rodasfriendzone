"""
Stage 1 tests for numbergame.py.
Run from the main folder:   python -m unittest discover -s tests -v
No Telegram, aiohttp or Postgres needed (the SQL is checked on SQLite).
"""
import ast
import asyncio
import inspect
import textwrap
import os
import random
import re
import sqlite3
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numbergame as ng  # noqa: E402


def has_await(fn):
    """True if the function body contains a real 'await' (comments and docstrings do not count)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return any(isinstance(n, ast.Await) for n in ast.walk(tree))

ADMIN = 999


class Env:
    """Fake of everything bot.py gives the module."""

    def __init__(self):
        self.wallets, self.wagered, self.required = {}, {}, {}
        self.queue, self.history = [], []
        self.phones, self.banned = set(), set()
        self.names, self.sent, self.markups = {}, [], []
        self.enq_n, self.done_n = 0, 0

    def turnover_left(self, uid):
        return max(0, self.required.get(uid, 0) - self.wagered.get(uid, 0))

    def log(self, uid, kind, amount, note="", hidden=False):
        self.history.append((uid, kind, amount, note))
        self.queue.append(("SAVE_USER", (uid, self.wallets.get(uid, 0))))

    def enqueue(self, sql, *args):
        self.enq_n += 1
        self.queue.append((sql, args))

    def mark(self):
        return self.enq_n

    def done(self):
        return self.done_n

    async def send(self, uid, text, **kw):
        self.sent.append((uid, text))
        if kw.get("reply_markup") is not None:
            self.markups.append((uid, kw["reply_markup"]))

    def save_counter(self, key, value):
        self.queue.append(("COUNTER", (key, value)))

    def verify(self, s):
        return {"id": int(s[1:]), "first_name": "P"} if isinstance(s, str) and s.startswith("u") else None

    def touch(self, user):
        self.wallets.setdefault(user["id"], 0)
        return user["id"]

    def ensure(self, u):
        self.wallets.setdefault(u.id, 0)
        return u.id

    def fund(self, uid, amount):
        self.wallets[uid] = amount
        self.phones.add(uid)


def make_env():
    e = Env()
    ng.rounds.clear()
    ng.idem_seen.clear()
    ng.ng_counter[0] = 0
    for p in ng.PRICES:
        ng.game_counter[p] = 0
    ng.start_notices.clear()
    ng.ENABLED = True
    ng.D.pop("pool", None)
    ng._kb = lambda rows: rows            # no Telegram library in the tests: keep the plain rows
    ng.init(wallets=e.wallets, wagered=e.wagered, turnover_left=e.turnover_left, log=e.log,
            enqueue=e.enqueue, save_counter=e.save_counter, verify=e.verify, touch=e.touch,
            phones=e.phones, banned=e.banned, admin_id=ADMIN, ensure=e.ensure,
            names=e.names, send=e.send, db_mark=e.mark, db_done=e.done)
    ng.history[50].clear()
    ng.history[100].clear()
    ng.pending_notify.clear()
    ng.to_save.clear()
    ng.ensure_rounds()
    e.queue.clear()
    return e


_k = [0]


def key():
    _k[0] += 1
    return f"key-{_k[0]:08d}"


def buy(uid, lobby, num, name="Abebe", idem=None):
    return ng.buy_number(uid, lobby, num, name, idem or key())


class PurchaseRules(unittest.TestCase):
    def setUp(self):
        self.e = make_env()

    def test_success_charges_once_and_records(self):
        self.e.fund(1, 1000)
        r = buy(1, 50, 17, "Abebe")
        self.assertTrue(r["ok"])
        self.assertEqual(self.e.wallets[1], 950)
        self.assertEqual(ng.rounds[50]["owners"][17]["uid"], 1)
        self.assertEqual(ng.rounds[50]["owners"][17]["name"], "Abebe")
        self.assertEqual(self.e.history[-1][1:3], ("bet", -50))

    def test_wallet_and_number_saved_in_same_step(self):
        self.e.fund(1, 1000)
        buy(1, 50, 17)
        kinds = [q[0] for q in self.e.queue]
        self.assertEqual(kinds[0], "SAVE_USER")
        self.assertTrue(kinds[1].startswith("INSERT INTO ng_numbers"))

    def test_number_cannot_be_bought_twice(self):
        self.e.fund(1, 1000)
        self.e.fund(2, 1000)
        self.assertTrue(buy(1, 50, 5)["ok"])
        r = buy(2, 50, 5)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "taken")
        self.assertEqual(self.e.wallets[2], 1000)          # not charged

    def test_max_five_per_player_per_lobby(self):
        self.e.fund(1, 10000)
        for n in range(1, 6):
            self.assertTrue(buy(1, 50, n)["ok"])
        r = buy(1, 50, 6)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "limit")
        self.assertEqual(self.e.wallets[1], 10000 - 250)   # exactly 5 x 50
        self.assertNotIn(6, ng.rounds[50]["owners"])       # not reserved

    def test_five_numbers_cost_250_and_500(self):
        self.e.fund(1, 5000)
        for n in range(1, 6):
            buy(1, 50, n)
        self.assertEqual(self.e.wallets[1], 5000 - 250)
        for n in range(1, 6):
            buy(1, 100, n)
        self.assertEqual(self.e.wallets[1], 5000 - 250 - 500)

    def test_lobbies_are_independent(self):
        self.e.fund(1, 5000)
        self.e.fund(2, 5000)
        self.assertTrue(buy(1, 50, 7)["ok"])
        self.assertTrue(buy(2, 100, 7)["ok"])              # same number, other lobby
        self.assertNotEqual(ng.rounds[50]["id"], ng.rounds[100]["id"])
        for n in range(10, 14):                            # player 1 now owns 5 in lobby 50
            self.assertTrue(buy(1, 50, n)["ok"])
        self.assertEqual(buy(1, 50, 14)["code"], "limit")
        self.assertTrue(buy(1, 100, 20)["ok"])             # limit is per lobby: 100 still free

    def test_insufficient_funds(self):
        self.e.fund(1, 49)
        r = buy(1, 50, 3)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "funds")
        self.assertEqual(self.e.wallets[1], 49)
        self.assertNotIn(3, ng.rounds[50]["owners"])
        self.assertTrue(buy(1, 100, 3)["ok"] is False)

    def test_exact_balance_is_enough(self):
        self.e.fund(1, 50)
        self.assertTrue(buy(1, 50, 3)["ok"])
        self.assertEqual(self.e.wallets[1], 0)

    def test_same_request_twice_charges_once(self):
        self.e.fund(1, 1000)
        k = key()
        a = buy(1, 50, 9, idem=k)
        b = buy(1, 50, 9, idem=k)
        self.assertTrue(a["ok"] and b["ok"])
        self.assertTrue(b.get("replay"))
        self.assertEqual(self.e.wallets[1], 950)
        self.assertEqual(len(ng.rounds[50]["by_uid"][1]), 1)

    def test_key_reused_for_other_number_is_rejected(self):
        self.e.fund(1, 1000)
        k = key()
        buy(1, 50, 9, idem=k)
        r = buy(1, 50, 10, idem=k)
        self.assertFalse(r["ok"])
        self.assertEqual(self.e.wallets[1], 950)

    def test_other_players_key_does_not_collide(self):
        self.e.fund(1, 1000)
        self.e.fund(2, 1000)
        k = key()
        self.assertTrue(buy(1, 50, 1, idem=k)["ok"])
        self.assertTrue(buy(2, 50, 2, idem=k)["ok"])

    def test_bad_inputs(self):
        self.e.fund(1, 1000)
        for bad in (0, 101, -1, "x", None, 3.5, True, [1]):
            self.assertFalse(buy(1, 50, bad)["ok"], bad)
        self.assertFalse(buy(1, 75, 5)["ok"])              # no such lobby
        self.assertFalse(buy(1, None, 5)["ok"])
        self.assertFalse(buy(1, 50, 5, idem="short")["ok"])
        self.assertEqual(self.e.wallets[1], 1000)

    def test_name_rules(self):
        self.e.fund(1, 10000)
        for bad in ("", "   ", "x" * 21, None, 5, "\u200b\u200b"):
            r = buy(1, 50, 1, name=bad)
            self.assertFalse(r["ok"], repr(bad))
            self.assertEqual(r["code"], "name")
        self.assertEqual(ng.clean_name("  Abe \n\t be  "), "Abe be")
        self.assertEqual(ng.clean_name("አበበ ከበደ"), "አበበ ከበደ")
        self.assertEqual(ng.clean_name("<b>x</b>"), "<b>x</b>")   # stored as typed, screens must escape
        self.assertIsNotNone(ng.clean_name("x" * 20))

    def test_play_through_counts_but_never_above_required(self):
        self.e.fund(1, 1000)
        self.e.required[1] = 70
        buy(1, 50, 1)
        self.assertEqual(self.e.wagered[1], 50)
        buy(1, 50, 2)
        self.assertEqual(self.e.wagered[1], 70)            # capped at what is owed
        buy(1, 50, 3)
        self.assertEqual(self.e.wagered[1], 70)

    def test_state_hides_other_players_names(self):
        self.e.fund(1, 1000)
        self.e.fund(2, 1000)
        buy(1, 50, 4, "Secret")
        s2 = ng.state_for(2, 50)
        self.assertEqual(s2["taken"], [4])
        self.assertEqual(s2["mine"], [])
        self.assertNotIn("Secret", str(s2))
        self.assertEqual(ng.state_for(1, 50)["mine"], [{"num": 4, "name": "Secret"}])

    def test_buy_function_can_not_be_interrupted(self):
        """Guard: nobody may add an await inside buy_number (it would break safety)."""
        self.assertFalse(inspect.iscoroutinefunction(ng.buy_number))
        self.assertFalse(has_await(ng.buy_number))


class FullRound(unittest.TestCase):
    def test_round_closes_only_after_100(self):
        e = make_env()
        for u in range(1, 21):
            e.fund(u, 1000)
        n = 0
        for u in range(1, 21):
            for _ in range(5):
                n += 1
                r = buy(u, 50, n)
                self.assertTrue(r["ok"])
                self.assertEqual(r["full"], n == 100)
                if n < 100:
                    self.assertEqual(ng.rounds[50]["status"], "open")
        self.assertEqual(ng.rounds[50]["status"], "countdown")     # frozen the moment 100 are sold
        self.assertEqual(len(ng.rounds[50]["owners"]), 100)
        e.fund(21, 1000)
        r = buy(21, 50, 1)
        self.assertEqual(r["code"], "closed")
        self.assertEqual(e.wallets[21], 1000)
        self.assertEqual(ng.rounds[100]["status"], "open")   # other lobby not affected


class Concurrency(unittest.TestCase):
    def test_many_players_one_number(self):
        e = make_env()
        for u in range(1, 41):
            e.fund(u, 500)

        async def worker(u):
            await asyncio.sleep(random.random() * 0.01)
            return await ng.handle_buy({"initData": f"u{u}", "lobby": 50, "num": 7,
                                        "name": "X", "idem": f"idem-{u:06d}"})

        async def go():
            return await asyncio.gather(*[worker(u) for u in range(1, 41)])

        res = asyncio.run(go())
        self.assertEqual(sum(1 for r in res if r["ok"]), 1)
        winner = ng.rounds[50]["owners"][7]["uid"]
        for u in range(1, 41):
            self.assertEqual(e.wallets[u], 450 if u == winner else 500)

    def test_double_tap_storm_same_key(self):
        e = make_env()
        e.fund(1, 500)

        async def worker():
            await asyncio.sleep(random.random() * 0.01)
            return await ng.handle_buy({"initData": "u1", "lobby": 50, "num": 3,
                                        "name": "X", "idem": "same-key-123"})

        async def go():
            return await asyncio.gather(*[worker() for _ in range(25)])

        res = asyncio.run(go())
        self.assertTrue(all(r["ok"] for r in res))
        self.assertEqual(e.wallets[1], 450)
        self.assertEqual(len(ng.rounds[50]["owners"]), 1)

    def test_one_player_many_numbers_at_once(self):
        e = make_env()
        e.fund(1, 5000)

        async def worker(n):
            await asyncio.sleep(random.random() * 0.01)
            return await ng.handle_buy({"initData": "u1", "lobby": 50, "num": n,
                                        "name": f"N{n}", "idem": f"idem-n{n:05d}"})

        async def go():
            return await asyncio.gather(*[worker(n) for n in range(1, 21)])

        res = asyncio.run(go())
        self.assertEqual(sum(1 for r in res if r["ok"]), 5)
        self.assertEqual(e.wallets[1], 5000 - 250)


class Access(unittest.TestCase):
    def test_needs_login_phone_and_open_game(self):
        e = make_env()
        run = asyncio.run
        body = {"lobby": 50, "num": 1, "name": "A", "idem": "idem-000001"}
        self.assertEqual(run(ng.handle_buy({**body, "initData": "bad"}))["error"], "auth")
        e.wallets[1] = 500                                   # known player, no phone yet
        self.assertTrue(run(ng.handle_buy({**body, "initData": "u1"}))["needPhone"])
        e.phones.add(1)
        e.banned.add(1)
        self.assertFalse(run(ng.handle_buy({**body, "initData": "u1"}))["ok"])
        e.banned.clear()
        self.assertTrue(run(ng.handle_buy({**body, "initData": "u1"}))["ok"])

    def test_switch_off_only_admin(self):
        e = make_env()
        ng.ENABLED = False
        e.fund(1, 500)
        e.fund(ADMIN, 500)
        body = {"lobby": 50, "num": 1, "name": "A"}
        r = asyncio.run(ng.handle_buy({**body, "idem": "idem-000001", "initData": "u1"}))
        self.assertFalse(r["ok"])
        self.assertEqual(e.wallets[1], 500)
        self.assertEqual(asyncio.run(ng.handle_state({"initData": "u1"}))["error"], "closed")
        r = asyncio.run(ng.handle_buy({**body, "idem": "idem-000002", "initData": f"u{ADMIN}"}))
        self.assertTrue(r["ok"])
        ng.ENABLED = True


class FakeConn:
    def __init__(self, active=(), numbers=(), settled=(), prizes=(), unnotified=(), counter=0, votes=()):
        self.active, self.numbers, self.settled = list(active), list(numbers), list(settled)
        self.prizes, self.unnotified, self.counter = list(prizes), list(unnotified), counter
        self.votes = list(votes)

    def _all(self):
        return self.active + self.settled

    async def fetch(self, sql, *args):
        if "FROM ng_rounds WHERE status IN" in sql:
            return self.active
        if "FROM ng_rounds WHERE status = 'settled'" in sql:
            return self.settled
        if "MAX(game_no)" in sql:
            best = {}
            for r in self._all():
                if r["test"] == 0 and r.get("game_no") and r["status"] in ("countdown", "settled"):
                    best[r["lobby"]] = max(best.get(r["lobby"], 0), r["game_no"])
            return [{"lobby": k, "g": v} for k, v in best.items()]
        if "game_no IS NULL" in sql and "SELECT id, lobby" in sql:
            return sorted([{"id": r["id"], "lobby": r["lobby"]} for r in self._all()
                           if r["test"] == 0 and not r.get("game_no")
                           and r["status"] in ("countdown", "settled")], key=lambda x: x["id"])
        if "WHERE lobby = $1 AND status = 'settled'" in sql:       # list_games
            lobby, limit, before = args
            rows = [r for r in self.settled if r["lobby"] == lobby and r["test"] == 0
                    and r.get("game_no") and (before is None or r["game_no"] < before)]
            return sorted(rows, key=lambda r: -r["game_no"])[:limit]
        if "WHERE lobby = $1 AND game_no = $2" in sql:             # load_game
            return [r for r in self.settled if r["lobby"] == args[0] and r.get("game_no") == args[1]
                    and r["test"] == 0]
        if "FROM ng_numbers" in sql and "ANY" in sql:
            return [n for n in self.numbers if n["round_id"] in args[0]]
        if "FROM ng_numbers" in sql:                                # board of one round
            return sorted([n for n in self.numbers if n["round_id"] == args[0]], key=lambda n: n["num"])
        if "FROM ng_votes" in sql:
            return [v for v in self.votes if v["round_id"] in args[0]]
        if "p.notified = 0" in sql:
            return self.unnotified
        if "FROM ng_prizes" in sql:
            return [p for p in self.prizes if p["round_id"] in args[0]]
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "MAX(id)" in sql:
            return max([r["id"] for r in self.active + self.settled] + [0])
        if "FROM kv" in sql:
            return self.counter
        raise AssertionError(sql)


def rrow(rid, lobby, status="open", close_at=None, early=0, test=0, data=None, settled_at=None,
         game_no=None, poll_at=None):
    import json as _j
    return {"id": rid, "lobby": lobby, "status": status, "created": 1, "full_at": None,
            "close_at": close_at, "settled_at": settled_at, "early": early, "test": test,
            "game_no": game_no, "poll_at": poll_at, "data": _j.dumps(data or {})}


def nrow(rid, lobby, num, uid, slot, idem, name="N", used=0):
    return {"round_id": rid, "lobby": lobby, "num": num, "uid": uid, "name": name,
            "price": lobby, "used": used, "slot": slot, "idem": idem, "t": 1}


class Restart(unittest.TestCase):
    def test_state_comes_back_after_restart(self):
        e = make_env()
        ng.rounds.clear()
        ng.idem_seen.clear()
        ng.ng_counter[0] = 0
        rounds = [rrow(7, 50)]
        numbers = [nrow(7, 50, 3, 1, 1, "1:idem-aaaa1111", "Abe"),
                   nrow(7, 50, 4, 1, 2, "1:idem-bbbb2222", "Kebede")]
        asyncio.run(ng.load_state(FakeConn(rounds, numbers, counter=7)))
        self.assertEqual(ng.rounds[50]["id"], 7)
        self.assertEqual(sorted(ng.rounds[50]["owners"]), [3, 4])
        self.assertEqual(ng.rounds[50]["by_uid"][1], [3, 4])
        self.assertEqual(ng.rounds[100]["id"], 8)            # the other lobby got a new round
        e.fund(2, 500)
        self.assertEqual(buy(2, 50, 3)["code"], "taken")     # purchases survived
        e.fund(1, 500)
        r = ng.buy_number(1, 50, 3, "Abe", "idem-aaaa1111")  # a retry after restart
        self.assertTrue(r["ok"] and r["replay"])
        self.assertEqual(e.wallets[1], 500)                  # no second charge
        self.assertTrue(buy(1, 50, 5)["ok"])
        self.assertEqual(ng.rounds[50]["owners"][5]["slot"], 3)   # next free slot

    def test_hundred_sold_is_frozen_after_restart(self):
        make_env()
        ng.rounds.clear()
        ng.idem_seen.clear()
        for status in ("open", "full"):                    # "full" = left by stage 1
            ng.rounds.clear()
            rounds = [rrow(2, 100, status)]
            numbers = [nrow(2, 100, n, 1 + (n - 1) // 5, (n - 1) % 5 + 1, f"{n}:idem-{n:08d}")
                       for n in range(1, 101)]
            asyncio.run(ng.load_state(FakeConn(rounds, numbers, counter=2)))
            self.assertEqual(ng.rounds[100]["status"], "countdown")
            self.assertEqual(len(ng.rounds[100]["draw"]["winners"]), 3)


class SqlCheck(unittest.TestCase):
    """The same CREATE TABLE and INSERT statements, run on SQLite
    (SQLite is only a stand-in: real Postgres is NOT tested here)."""

    @staticmethod
    def lite(sql):
        sql = sql.replace("'{}'::jsonb", "'{}'").replace("::jsonb", "")
        sql = sql.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY").replace("JSONB", "TEXT")
        return re.sub(r"\$\d+", "?", sql)

    def db(self):
        con = sqlite3.connect(":memory:")
        for stmt in ng.SCHEMA.split(";"):
            if stmt.strip() and not stmt.strip().startswith("ALTER"):   # ALTER ... IF NOT EXISTS is Postgres only
                con.execute(self.lite(stmt))
        return con

    def test_constraints(self):
        con = self.db()
        ins = ("INSERT INTO ng_numbers(round_id, lobby, num, uid, name, price, used, slot, idem, t) "
               "VALUES(?,?,?,?,?,?,?,?,?,?)")

        def add(rid, num, uid, slot, idem):
            con.execute(ins, (rid, 50, num, uid, "n", 50, 0, slot, idem, 1))

        add(1, 5, 10, 1, "a")
        with self.assertRaises(sqlite3.IntegrityError):
            add(1, 5, 11, 1, "b")           # same number twice in a round
        with self.assertRaises(sqlite3.IntegrityError):
            add(1, 6, 10, 1, "c")           # same player, same slot
        with self.assertRaises(sqlite3.IntegrityError):
            add(1, 6, 10, 6, "d")           # a 6th slot does not exist
        with self.assertRaises(sqlite3.IntegrityError):
            add(1, 101, 10, 2, "e")         # number out of range
        with self.assertRaises(sqlite3.IntegrityError):
            add(1, 7, 10, 2, "a")           # same request key twice
        add(2, 5, 11, 1, "f")               # same number, another round: fine
        for s in range(2, 6):
            add(1, 20 + s, 10, s, f"s{s}")  # slots 2..5 fine (5 numbers total)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM ng_numbers WHERE round_id=1 AND uid=10").fetchone()[0], 5)

    def test_real_statements_run(self):
        """Replay every SQL the module really sends for a purchase."""
        e = make_env()
        ng.rounds.clear()
        ng.ng_counter[0] = 0
        e.queue.clear()
        ng.ensure_rounds()
        e.fund(1, 5000)
        for n in range(1, 6):
            buy(1, 50, n)
        buy(1, 50, 6)                        # refused: writes an audit row
        for n in range(1, 101):              # fill lobby 100 -> "full" update
            e.fund(100 + n, 1000)
            self.assertTrue(buy(100 + n, 100, n)["ok"])
        con = self.db()
        count = 0
        for sql, args in e.queue:
            if sql.startswith(("INSERT INTO ng_", "UPDATE ng_")):
                con.execute(self.lite(sql), args)
                count += 1
        self.assertGreater(count, 100)
        self.assertEqual(con.execute("SELECT status FROM ng_rounds WHERE lobby=100 AND status='countdown'").fetchone()[0], "countdown")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM ng_numbers WHERE lobby=50").fetchone()[0], 5)
        self.assertGreaterEqual(con.execute("SELECT COUNT(*) FROM ng_audit WHERE kind='buy_fail'").fetchone()[0], 1)


class AdminText(unittest.TestCase):
    def test_status_text(self):
        e = make_env()
        e.fund(1, 500)
        buy(1, 50, 1)
        t = ng.status_text()
        self.assertIn("50 ETB", t)
        self.assertIn("Sold 1/100", t)
        self.assertIn("Sales 50 ETB", t)



# =====================  STAGE 2  =====================
import hashlib  # noqa: E402
import json  # noqa: E402


class FixedRng:
    """Test double: always 'draws' the numbers we choose."""

    def __init__(self, picks):
        self.picks = picks

    def sample(self, pop, k):
        return list(self.picks)[:k]


def fill_round(e, lobby, count=100, first_uid=1):
    """Players buy numbers 1..count, 5 each. Owner of number n = first_uid + (n-1)//5."""
    users = (count + 4) // 5
    for u in range(first_uid, first_uid + users):
        e.fund(u, 10000)
        e.names[u] = f"Player{u}"
    for n in range(1, count + 1):
        r = buy(first_uid + (n - 1) // 5, lobby, n, f"Name{n}")
        assert r["ok"], r


class Stage2Base(unittest.TestCase):
    def setUp(self):
        self.e = make_env()
        self._rng = ng._rng

    def tearDown(self):
        ng._rng = self._rng
        ng.ENABLED = True


class Prizes(Stage2Base):
    def test_full_round_prizes_are_your_numbers(self):
        self.assertEqual(ng.prizes_for(50, 100), [3000, 500, 300])
        self.assertEqual(ng.prizes_for(100, 100), [7000, 1000, 500])

    def test_early_start_prizes_scale_with_numbers_sold(self):
        self.assertEqual(ng.prizes_for(50, 50), [1500, 250, 150])
        self.assertEqual(ng.prizes_for(50, 70), [2100, 350, 210])
        self.assertEqual(ng.prizes_for(100, 50), [3500, 500, 250])
        self.assertEqual(ng.prizes_for(100, 70), [4900, 700, 350])

    def test_always_whole_birr_and_always_below_sales(self):
        for lobby in (50, 100):
            for sold in range(50, 101):
                pr = ng.prizes_for(lobby, sold)
                self.assertTrue(all(isinstance(x, int) and x > 0 for x in pr))
                self.assertLess(sum(pr), sold * lobby)          # the house never pays out more than it took in
                self.assertGreater(pr[0], pr[1])
                self.assertGreater(pr[1], pr[2])


class Freeze(Stage2Base):
    def test_hundredth_purchase_freezes_and_draws(self):
        fill_round(self.e, 50)
        r = ng.rounds[50]
        d = r["draw"]
        self.assertEqual(r["status"], "countdown")
        self.assertEqual(len(set(d["winners"])), 3)
        self.assertTrue(all(1 <= w <= 100 for w in d["winners"]))
        self.assertEqual(r["close_at"], d["frozen_at"] + ng.COUNTDOWN_SECONDS)
        self.assertEqual(d["prizes"], [3000, 500, 300])
        self.assertEqual(d["sold"], 100)
        self.assertFalse(r["early"])
        self.e.fund(500, 1000)
        self.assertEqual(buy(500, 50, 1)["code"], "closed")      # nothing can be bought now
        self.assertEqual(ng.rounds[100]["status"], "open")       # the other lobby is not affected

    def test_hash_matches_and_names_the_winners(self):
        fill_round(self.e, 100)
        r = ng.rounds[100]
        d = r["draw"]
        self.assertEqual(hashlib.sha256(d["verify"].encode()).hexdigest(), d["hash"])
        self.assertEqual(d["verify"], ng._verify_text(d["secret"], r["id"], 100,
                                                      sorted(r["owners"]), d["winners"]))
        self.assertTrue(d["verify"].endswith(",".join(map(str, d["winners"]))))
        # the public view shows the hash but never the secret before settlement
        v = ng.result_view(r, now=r["close_at"] - 5)
        self.assertEqual(v["hash"], d["hash"])
        self.assertNotIn("secret", v)
        self.assertNotIn(d["secret"], json.dumps(v))

    def test_draw_is_saved_with_the_freeze(self):
        fill_round(self.e, 50)
        ups = [q for q in self.e.queue if q[0].startswith("UPDATE ng_rounds SET status = 'countdown'")]
        self.assertEqual(len(ups), 1)
        saved = json.loads(ups[0][1][2])
        self.assertEqual(saved["winners"], ng.rounds[50]["draw"]["winners"])
        self.assertEqual(ups[0][1][0], ng.rounds[50]["close_at"])      # countdown end is saved

    def test_never_drawn_twice(self):
        fill_round(self.e, 50)
        before = json.dumps(ng.rounds[50]["draw"])
        close = ng.rounds[50]["close_at"]
        res = ng.freeze_round(50)
        self.assertFalse(res["ok"])
        res = ng.freeze_round(50, early=True)
        self.assertFalse(res["ok"])
        self.assertEqual(json.dumps(ng.rounds[50]["draw"]), before)
        self.assertEqual(ng.rounds[50]["close_at"], close)

    def test_early_start_needs_50_sold(self):
        fill_round(self.e, 50, count=49)
        res = ng.freeze_round(50, early=True)
        self.assertFalse(res["ok"])
        self.assertEqual(ng.rounds[50]["status"], "open")
        self.assertFalse(ng.freeze_round(50)["ok"])               # normal start needs all 100
        e = self.e
        e.fund(77, 1000)
        buy(77, 50, 50)                                            # the 50th number
        res = ng.freeze_round(50, early=True)
        self.assertTrue(res["ok"])
        r = ng.rounds[50]
        self.assertEqual(r["status"], "countdown")
        self.assertTrue(r["early"])
        self.assertEqual(r["draw"]["prizes"], [1500, 250, 150])
        self.assertTrue(set(r["draw"]["winners"]) <= set(r["owners"]))   # only SOLD numbers can win

    def test_draw_uses_only_the_sold_numbers(self):
        sold = [4, 9, 15, 22, 31, 40, 41, 77, 90, 99]
        for _ in range(300):
            w = ng.draw_winners(sold)
            self.assertEqual(len(set(w)), 3)
            self.assertTrue(set(w) <= set(sold))
        self.assertEqual(list(inspect.signature(ng.draw_winners).parameters), ["sold_numbers"])

    def test_draw_is_fair_over_many_draws(self):
        sold = list(range(1, 101))
        gold = [0] * 101
        for _ in range(6000):
            g, s_, b = ng.draw_winners(sold)
            self.assertEqual(len({g, s_, b}), 3)
            gold[g] += 1
        # expected 60 each; these limits are far outside normal luck
        self.assertTrue(all(20 <= gold[n] <= 110 for n in range(1, 101)), min(gold[1:]))

    def test_freeze_and_settle_can_not_be_interrupted(self):
        for fn in (ng.freeze_round, ng.settle_round, ng.buy_number):
            self.assertFalse(inspect.iscoroutinefunction(fn))
            self.assertFalse(has_await(fn), fn.__name__)


class Timeline(Stage2Base):
    def test_phases_and_timed_reveal(self):
        ng._rng = FixedRng([1, 6, 11])
        fill_round(self.e, 50)
        r = ng.rounds[50]
        c = r["close_at"]
        v = ng.result_view(r, now=c - 10)
        self.assertEqual((v["phase"], v["seconds_left"], v["winners"]), ("countdown", 10, []))
        v = ng.result_view(r, now=c + 1)
        self.assertEqual((v["phase"], v["winners"]), ("drawing", []))
        v = ng.result_view(r, now=c + ng.DRAW_INTRO)
        self.assertEqual([w["place"] for w in v["winners"]], ["gold"])
        self.assertEqual(v["winners"][0]["num"], 1)
        self.assertEqual(v["winners"][0]["prize"], 3000)
        self.assertEqual(v["winners"][0]["name"], "Name1")
        self.assertEqual(v["winners"][0]["player"], "Player1")
        v = ng.result_view(r, now=c + ng.DRAW_INTRO + ng.REVEAL_GAP)
        self.assertEqual([w["place"] for w in v["winners"]], ["gold", "silver"])
        v = ng.result_view(r, now=c + ng.DRAW_INTRO + 2 * ng.REVEAL_GAP + 1)
        self.assertEqual([w["place"] for w in v["winners"]], ["gold", "silver", "bronze"])
        self.assertNotIn("secret", v)
        v = ng.result_view(r, now=ng.settle_time(r) + 1)
        self.assertEqual(v["phase"], "done")

    def test_countdown_comes_from_saved_time_not_from_the_page(self):
        fill_round(self.e, 50)
        r = ng.rounds[50]
        a = ng.result_view(r, now=r["close_at"] - 30)["seconds_left"]
        b = ng.result_view(r, now=r["close_at"] - 30)["seconds_left"]   # a refresh: same answer
        self.assertEqual((a, b), (30, 30))
        r2 = ng.state_for(1, 50)
        self.assertEqual(r2["draw"]["draw_at"], r["close_at"])

    def test_state_shows_prize_rules(self):
        s_ = ng.state_for(1, 100)
        self.assertEqual(s_["prize_full"], [7000, 1000, 500])
        self.assertEqual(s_["min_early"], 50)
        self.assertIsNone(s_["draw"])
        self.assertIsNone(s_["last"])


class Settlement(Stage2Base):
    def full_round(self, lobby=50, picks=(1, 6, 11)):
        ng._rng = FixedRng(list(picks))
        fill_round(self.e, lobby)
        return ng.rounds[lobby]

    def test_nothing_happens_before_the_end(self):
        r = self.full_round()
        self.assertIsNone(ng.settle_round(50, now=ng.settle_time(r) - 1))
        self.assertEqual(r["status"], "countdown")
        self.assertEqual(self.e.wallets[1], 10000 - 250)

    def test_prizes_credited_exactly_once(self):
        r = self.full_round(50, (1, 6, 11))               # owners: player 1, 2, 3
        old = {u: self.e.wallets[u] for u in (1, 2, 3, 4)}
        t = ng.settle_time(r)
        self.assertIs(ng.settle_round(50, now=t), r)
        self.assertEqual(self.e.wallets[1], old[1] + 3000)
        self.assertEqual(self.e.wallets[2], old[2] + 500)
        self.assertEqual(self.e.wallets[3], old[3] + 300)
        self.assertEqual(self.e.wallets[4], old[4])
        for _ in range(5):                                # retries / ticks / second ticker
            self.assertIsNone(ng.settle_round(50, now=t + 1))
            ng.tick(now=t + 2)
        self.assertEqual(self.e.wallets[1], old[1] + 3000)
        self.assertEqual(self.e.wallets[2], old[2] + 500)
        self.assertEqual(self.e.wallets[3], old[3] + 300)
        self.assertEqual(sum(1 for q in self.e.queue if q[0].startswith("INSERT INTO ng_prizes")), 3)
        self.assertEqual([h[1:3] for h in self.e.history if h[1] == "win"],
                         [("win", 3000), ("win", 500), ("win", 300)])

    def test_new_round_and_history(self):
        r = self.full_round()
        old_id = r["id"]
        ng.settle_round(50, now=ng.settle_time(r))
        self.assertEqual(r["status"], "settled")
        new = ng.rounds[50]
        self.assertGreater(new["id"], old_id)
        self.assertEqual((new["status"], len(new["owners"])), ("open", 0))
        self.assertIs(ng.history[50][0], r)               # the old result is kept
        self.assertEqual(ng.history[100], [])

    def test_player_owning_all_three_winning_numbers_gets_all_three(self):
        r = self.full_round(50, (1, 2, 3))                # all owned by player 1
        before = self.e.wallets[1]
        ng.settle_round(50, now=ng.settle_time(r))
        self.assertEqual(self.e.wallets[1], before + 3800)

    def test_100_lobby_full_round(self):
        r = self.full_round(100, (1, 6, 11))
        ng.settle_round(100, now=ng.settle_time(r))
        self.assertEqual([self.e.wallets[u] for u in (1, 2, 3)],
                         [10000 - 500 + 7000, 10000 - 500 + 1000, 10000 - 500 + 500])

    def test_early_round_pays_scaled_prizes(self):
        ng._rng = FixedRng([1, 6, 11])
        fill_round(self.e, 50, count=70)
        ng.freeze_round(50, early=True)
        r = ng.rounds[50]
        ng.settle_round(50, now=ng.settle_time(r))
        self.assertEqual(self.e.wallets[1], 10000 - 250 + 2100)
        self.assertEqual(self.e.wallets[2], 10000 - 250 + 350)
        self.assertEqual(self.e.wallets[3], 10000 - 250 + 210)

    def test_credit_and_status_are_saved_together(self):
        r = self.full_round()
        self.e.queue.clear()
        ng.settle_round(50, now=ng.settle_time(r))
        kinds = [q[0][:30] for q in self.e.queue]
        first_prize = next(i for i, k in enumerate(kinds) if k.startswith("INSERT INTO ng_prizes"))
        status = next(i for i, k in enumerate(kinds) if k.startswith("UPDATE ng_rounds SET status"))
        saves = [i for i, k in enumerate(kinds) if k == "SAVE_USER"]
        self.assertEqual(len(saves), 3)
        self.assertTrue(all(i < status for i in saves))
        self.assertTrue(first_prize < status)

    def test_crash_half_way_never_pays_a_place_twice(self):
        r = self.full_round(50, (1, 6, 11))
        self.e.wallets[1] += 3000                          # place 0 was paid, then the server died
        r["paid"].add(0)
        before1 = self.e.wallets[1]
        ng.settle_round(50, now=ng.settle_time(r))
        self.assertEqual(self.e.wallets[1], before1)
        self.assertEqual(self.e.wallets[2], 10000 - 250 + 500)
        self.assertEqual(self.e.wallets[3], 10000 - 250 + 300)


class Announce(Stage2Base):
    def test_waits_for_the_database_before_announcing(self):
        ng._rng = FixedRng([1, 6, 11])
        fill_round(self.e, 50)
        r = ng.rounds[50]
        ng.settle_round(50, now=ng.settle_time(r))
        self.assertEqual(ng.result_view(r, now=ng.settle_time(r))["settlement"], "settling")
        asyncio.run(ng.after_settle())                     # database has NOT finished yet
        self.assertEqual(self.e.sent, [])
        self.assertFalse(r["saved"])
        self.e.done_n = self.e.enq_n                       # now it has
        asyncio.run(ng.after_settle())
        self.assertTrue(r["saved"])
        self.assertEqual(ng.result_view(r, now=ng.settle_time(r))["settlement"], "paid")
        self.assertEqual(sorted(u for u, _ in self.e.sent), sorted([ADMIN, 1, 2, 3]))
        gold = next(t for u, t in self.e.sent if u == 1)
        self.assertIn("3000 ETB", gold)
        self.assertIn("🥇", gold)
        n_sent = len(self.e.sent)
        asyncio.run(ng.after_settle())                     # nothing is sent twice
        self.assertEqual(len(self.e.sent), n_sent)
        notified = [q for q in self.e.queue if q[0].startswith("UPDATE ng_prizes SET notified")]
        self.assertEqual(len(notified), 3)

    def test_failed_message_does_not_stop_the_game(self):
        ng._rng = FixedRng([1, 6, 11])
        fill_round(self.e, 50)
        r = ng.rounds[50]
        ng.settle_round(50, now=ng.settle_time(r))
        self.e.done_n = self.e.enq_n

        async def boom(uid, text):
            raise RuntimeError("blocked")
        ng.D["send"] = boom
        asyncio.run(ng.after_settle())
        self.assertTrue(r["saved"])
        self.assertEqual(ng.pending_notify, [])
        self.assertEqual(self.e.wallets[1], 10000 - 250 + 3000)


class RestartStage2(Stage2Base):
    def counting_rows(self, status="countdown", winners=(1, 6, 11), close_at=1000):
        info = [{"num": w, "name": f"Name{w}", "uid": 1 + (w - 1) // 5} for w in winners]
        data = {"secret": "S" * 20, "verify": "v", "hash": "h" * 64, "winners": list(winners),
                "sold": 100, "prizes": [3000, 500, 300], "frozen_at": close_at - 60,
                "winners_info": info}
        numbers = [nrow(9, 50, n, 1 + (n - 1) // 5, (n - 1) % 5 + 1, f"{1 + (n - 1) // 5}:idem-{n:08d}", f"Name{n}")
                   for n in range(1, 101)]
        return rrow(9, 50, status, close_at=close_at, data=data), numbers, data

    def test_countdown_survives_restart_without_a_new_draw(self):
        row, numbers, data = self.counting_rows()
        for u in range(1, 21):
            self.e.fund(u, 5000)
        ng.rounds.clear()
        ng.idem_seen.clear()
        asyncio.run(ng.load_state(FakeConn([row], numbers, counter=9)))
        r = ng.rounds[50]
        self.assertEqual((r["status"], r["close_at"]), ("countdown", 1000))
        self.assertEqual(r["draw"]["hash"], data["hash"])          # same draw, nothing re-drawn
        self.assertEqual(r["draw"]["winners"], [1, 6, 11])
        self.assertEqual(ng.rounds[100]["status"], "open")
        self.assertEqual(buy(99, 50, 1)["code"], "closed")
        self.assertIsNone(ng.settle_round(50, now=1020))           # still counting
        ng.settle_round(50, now=1000 + ng.DRAW_INTRO + 3 * ng.REVEAL_GAP)
        self.assertEqual(self.e.wallets[1], 5000 + 3000)
        self.assertEqual(self.e.wallets[2], 5000 + 500)
        self.assertEqual(self.e.wallets[3], 5000 + 300)

    def test_places_already_paid_are_skipped_after_restart(self):
        row, numbers, _ = self.counting_rows()
        for u in range(1, 21):
            self.e.fund(u, 5000)
        ng.rounds.clear()
        asyncio.run(ng.load_state(FakeConn([row], numbers, prizes=[{"round_id": 9, "place": 1}], counter=9)))
        self.assertEqual(ng.rounds[50]["paid"], {0})
        ng.settle_round(50, now=1000 + ng.DRAW_INTRO + 3 * ng.REVEAL_GAP)
        self.assertEqual(self.e.wallets[1], 5000)                  # gold had been paid before
        self.assertEqual(self.e.wallets[2], 5000 + 500)

    def test_settled_round_is_never_paid_again(self):
        row, _, _ = self.counting_rows("settled")
        row["settled_at"] = 2000
        for u in range(1, 21):
            self.e.fund(u, 5000)
        ng.rounds.clear()
        ng.history[50].clear()
        asyncio.run(ng.load_state(FakeConn([], [], settled=[row], counter=9)))
        self.assertEqual(ng.rounds[50]["status"], "open")
        self.assertGreater(ng.rounds[50]["id"], 9)                 # a new round, id not reused
        self.assertEqual(ng.history[50][0]["id"], 9)
        self.assertTrue(ng.history[50][0]["saved"])
        ng.tick(now=99999)
        self.assertEqual([self.e.wallets[u] for u in (1, 2, 3)], [5000, 5000, 5000])
        v = ng.result_view(ng.history[50][0])
        self.assertEqual([w["place"] for w in v["winners"]], ["gold", "silver", "bronze"])
        self.assertEqual(v["settlement"], "paid")
        self.assertEqual(v["secret"], "S" * 20)

    def test_unsent_winner_messages_are_sent_after_restart(self):
        row, _, _ = self.counting_rows("settled")
        ng.rounds.clear()
        ng.history[50].clear()
        un = [{"round_id": 9, "place": 1, "num": 1, "uid": 1, "amount": 3000, "lobby": 50,
               "data": row["data"]}]
        asyncio.run(ng.load_state(FakeConn([], [], settled=[row], unnotified=un, counter=9)))
        asyncio.run(ng.after_settle())
        self.assertEqual([u for u, _ in self.e.sent], [1])
        self.assertIn("3000 ETB", self.e.sent[0][1])


class TestMode(Stage2Base):
    def test_fake_round_runs_the_whole_cycle_without_money(self):
        ng.ENABLED = False
        wallets_before = dict(self.e.wallets)
        res = ng.test_fill(50, 100)
        self.assertTrue(res["ok"] and res.get("frozen"))
        r = ng.rounds[50]
        self.assertEqual(r["status"], "countdown")
        self.assertTrue(r["test"])
        ng.settle_round(50, now=ng.settle_time(r))
        self.assertEqual(self.e.wallets, wallets_before)           # no wallet touched, none created
        self.assertEqual(self.e.history, [])
        prizes = [q[1] for q in self.e.queue if q[0].startswith("INSERT INTO ng_prizes")]
        self.assertEqual({p[5] for p in prizes}, {"test"})
        self.assertIsNone(ng.last_result(50))                      # players never see test rounds
        self.assertIs(ng.last_result(50, include_test=True), r)
        self.assertIn("TEST", ng.summary_text(r))
        self.assertIn("Test", ng.result_text(r))

    def test_early_start_of_a_fake_round(self):
        ng.ENABLED = False
        self.assertFalse(ng.test_freeze(50)["ok"])                 # not a test round yet
        ng.test_fill(50, 49)
        self.assertFalse(ng.test_freeze(50)["ok"])                 # 49 is not enough
        ng.test_fill(50, 1)
        self.assertTrue(ng.test_freeze(50)["ok"])
        self.assertTrue(ng.rounds[50]["early"])

    def test_safety_rules(self):
        ng.ENABLED = False
        self.e.fund(1, 1000)
        buy(1, 50, 5)                                              # a real purchase
        self.assertFalse(ng.test_fill(50, 10)["ok"])               # refused: real money in the round
        ng.test_fill(100, 10)
        self.e.fund(2, 1000)
        r = buy(2, 100, 99)
        self.assertEqual(r["code"], "test")                        # real money refused in test round
        self.assertEqual(self.e.wallets[2], 1000)
        ng.ENABLED = True
        self.assertFalse(ng.test_fill(100, 1)["ok"])               # no test mode once the game is on
        self.assertFalse(ng.test_freeze(100)["ok"])

    def test_results_api_hides_test_rounds(self):
        ng.ENABLED = False
        ng.test_fill(50, 100)
        r = ng.rounds[50]
        ng.settle_round(50, now=ng.settle_time(r))
        self.e.fund(ADMIN, 100)
        res = asyncio.run(ng.handle_results({"initData": f"u{ADMIN}", "lobby": "50"}))
        self.assertTrue(res["ok"])
        self.assertEqual(res["results"], [])


class ResultsApi(Stage2Base):
    def test_results_list_and_state_last(self):
        ng._rng = FixedRng([1, 6, 11])
        fill_round(self.e, 50)
        r = ng.rounds[50]
        ng.settle_round(50, now=ng.settle_time(r))
        self.e.done_n = self.e.enq_n
        asyncio.run(ng.after_settle())
        res = asyncio.run(ng.handle_results({"initData": "u1", "lobby": "50"}))
        v = res["results"][0]
        self.assertEqual((v["round"], v["lobby"], v["sold"]), (r["id"], 50, 100))
        self.assertEqual((v["total_sales"], v["prize_total"]), (5000, 3800))
        self.assertEqual([w["place"] for w in v["winners"]], ["gold", "silver", "bronze"])
        self.assertEqual([w["prize"] for w in v["winners"]], [3000, 500, 300])
        self.assertEqual((v["settlement"], v["status"]), ("paid", "settled"))
        self.assertEqual(hashlib.sha256(v["verify"].encode()).hexdigest(), v["hash"])
        st = asyncio.run(ng.handle_state({"initData": "u1", "lobby": "50"}))
        self.assertEqual(st["last"]["round"], r["id"])
        self.assertEqual(st["status"], "open")
        self.assertIsNone(st["draw"])


class FullCycleSql(Stage2Base):
    def test_every_statement_of_a_whole_round_runs(self):
        """SQLite stand-in: checks column names / order of the real statements."""
        ng._rng = FixedRng([1, 6, 11])
        self.e.queue.clear()
        ng.rounds.clear()
        ng.ng_counter[0] = 0
        ng.ensure_rounds()
        fill_round(self.e, 50)
        r = ng.rounds[50]
        ng.settle_round(50, now=ng.settle_time(r))
        self.e.done_n = self.e.enq_n
        asyncio.run(ng.after_settle())
        con = SqlCheck().db()
        for sql, args in self.e.queue:
            if sql.startswith(("INSERT INTO ng_", "UPDATE ng_")):
                con.execute(SqlCheck.lite(sql), args)
        row = con.execute("SELECT status, close_at, early, test, settled_at FROM ng_rounds WHERE id=?",
                          (r["id"],)).fetchone()
        self.assertEqual(row[0], "settled")
        self.assertIsNotNone(row[1])
        self.assertEqual(row[2:4], (0, 0))
        prizes = con.execute("SELECT place, num, uid, amount, status, notified FROM ng_prizes "
                             "WHERE round_id=? ORDER BY place", (r["id"],)).fetchall()
        self.assertEqual(prizes, [(1, 1, 1, 3000, "paid", 1), (2, 6, 2, 500, "paid", 1),
                                  (3, 11, 3, 300, "paid", 1)])
        data = json.loads(con.execute("SELECT data FROM ng_rounds WHERE id=?", (r["id"],)).fetchone()[0])
        self.assertEqual(data["winners"], [1, 6, 11])
        with self.assertRaises(sqlite3.IntegrityError):            # a place can never be saved twice
            con.execute("INSERT INTO ng_prizes(round_id, place, num, uid, amount, status, notified, t) "
                        "VALUES(?,1,1,1,1,'paid',0,1)", (r["id"],))


# =====================  STAGE 3 (server side)  =====================
import contextlib  # noqa: E402
from unittest import mock  # noqa: E402


@contextlib.contextmanager
def fast_sleep():
    """The module pauses 0.06 s between Telegram messages. Skip the pause in tests."""
    real = asyncio.sleep

    async def quick(delay, *a, **k):
        await real(0)
    with mock.patch.object(ng.asyncio, "sleep", quick):
        yield


def settle_now(lobby):
    r = ng.rounds[lobby]
    return ng.settle_round(lobby, now=ng.settle_time(r))


def play_game(e, lobby, picks=(1, 6, 11), count=100, test=False):
    """Play one whole game to the end and return the settled round."""
    ng._rng = FixedRng(list(picks))
    if test:
        ng.ENABLED = False
        ng.test_fill(lobby, count)
        ng.ENABLED = True
    else:
        fill_round(e, lobby, count)
        if count < 100:
            assert ng.admin_start(lobby)["ok"]
    return settle_now(lobby)


def rows_of(r):
    """Database rows (like Postgres would return them) for a settled round."""
    rr = rrow(r["id"], r["lobby"], "settled", close_at=r["close_at"], early=int(r["early"]),
              test=int(r["test"]), data=r["draw"], settled_at=r["settled_at"], game_no=r["game_no"])
    nn = [nrow(r["id"], r["lobby"], n, o["uid"], o["slot"], f"{n}:k{r['id']}-{n}", o["name"])
          for n, o in sorted(r["owners"].items())]
    return rr, nn


class GameNumbers(Stage2Base):
    def test_each_lobby_counts_on_its_own(self):
        self.assertEqual((ng.rounds[50]["game_no"], ng.rounds[100]["game_no"]), (1, 1))
        r1 = play_game(self.e, 50)
        self.assertEqual(r1["game_no"], 1)
        self.assertEqual(ng.rounds[50]["game_no"], 2)        # the new 50 round is Game 2
        self.assertEqual(ng.rounds[100]["game_no"], 1)       # the 100 lobby has not moved
        r2 = play_game(self.e, 50)
        self.assertEqual((r2["game_no"], ng.rounds[50]["game_no"]), (2, 3))
        r3 = play_game(self.e, 100)
        self.assertEqual((r3["game_no"], ng.rounds[100]["game_no"]), (1, 2))

    def test_game_number_is_final_when_frozen_and_never_changes(self):
        fill_round(self.e, 50)
        r = ng.rounds[50]
        self.assertEqual(r["status"], "countdown")
        self.assertEqual(r["game_no"], 1)
        self.assertEqual(ng.game_counter[50], 1)
        settle_now(50)
        self.assertEqual(r["game_no"], 1)
        self.assertEqual(ng.rounds[50]["game_no"], 2)

    def test_test_rounds_never_take_a_game_number(self):
        t = play_game(self.e, 50, test=True)
        self.assertTrue(t["test"])
        self.assertIsNone(t["game_no"])
        self.assertEqual(ng.game_counter[50], 0)
        self.assertEqual(ng.rounds[50]["game_no"], 1)        # next real game is still Game 1
        real = play_game(self.e, 50)
        self.assertEqual(real["game_no"], 1)

    def test_game_number_is_in_every_view_and_text(self):
        r = play_game(self.e, 50)
        v = ng.result_view(r)
        self.assertEqual(v["game"], 1)
        self.assertIn("Game 1", ng.summary_text(r))
        self.assertIn("Game 1", ng.result_text(r))
        item = ng.pending_notify[0] if ng.pending_notify else None
        self.e.done_n = self.e.enq_n                         # the database has written everything
        asyncio.run(ng.after_settle())
        self.assertTrue(any("50 ETB · Game 1" in text for _, text in self.e.sent))
        st = ng.state_for(1, 50)
        self.assertEqual(st["game"], 2)
        self.assertEqual({x["price"]: x["game"] for x in st["lobbies"]}, {50: 2, 100: 1})
        self.assertIsNotNone(item)

    def test_status_text_shows_game(self):
        self.assertIn("50 ETB · Game 1", ng.status_text())


class PollRules(Stage2Base):
    def setUp(self):
        super().setUp()
        fill_round(self.e, 50, count=60)         # 60 numbers, players 1..12 (5 each)

    def test_poll_needs_50_numbers(self):
        ng.rounds.clear()
        ng.ensure_rounds()
        fill_round(self.e, 50, count=40)
        res = ng.open_poll(50)
        self.assertFalse(res["ok"])
        self.assertIn("50", res["error"])
        self.assertIsNone(ng.rounds[50]["poll_at"])

    def test_poll_not_for_test_rounds_or_frozen_rounds(self):
        ng.rounds.clear()
        ng.ensure_rounds()
        ng.ENABLED = False
        ng.test_fill(50, 60)
        ng.ENABLED = True
        self.assertFalse(ng.open_poll(50)["ok"])
        r = play_game(self.e, 100)                   # a real 100 game, now settled
        self.assertEqual(r["status"], "settled")
        fill_round(self.e, 100)                      # frozen (full) again
        self.assertEqual(ng.rounds[100]["status"], "countdown")
        self.assertFalse(ng.open_poll(100)["ok"])

    def test_opening_twice_does_nothing_new(self):
        self.e.queue.clear()
        a = ng.open_poll(50)
        self.assertTrue(a["ok"] and not a["already"])
        n = len(self.e.queue)
        self.assertGreater(n, 0)
        b = ng.open_poll(50)
        self.assertTrue(b["ok"] and b["already"])
        self.assertEqual(len(self.e.queue), n)       # nothing written the second time

    def test_buying_still_works_while_poll_is_open(self):
        ng.open_poll(50)
        self.e.fund(500, 1000)
        self.assertTrue(buy(500, 50, 61)["ok"])
        self.assertEqual(ng.rounds[50]["status"], "open")

    def test_only_owners_vote_and_only_when_poll_is_open(self):
        rid = ng.rounds[50]["id"]
        self.assertFalse(ng.cast_vote(1, rid, True)["ok"])          # no poll yet
        ng.open_poll(50)
        self.assertTrue(ng.cast_vote(1, rid, True)["ok"])
        res = ng.cast_vote(777, rid, True)                          # owns nothing
        self.assertFalse(res["ok"])
        self.assertNotIn(777, ng.rounds[50]["votes"])
        self.assertFalse(ng.cast_vote(1, 99999, True)["ok"])        # unknown round
        self.e.banned.add(2)
        self.assertFalse(ng.cast_vote(2, rid, True)["ok"])
        self.assertNotIn(2, ng.rounds[50]["votes"])

    def test_one_vote_each_and_changing_mind_does_not_double_count(self):
        rid = ng.rounds[50]["id"]
        ng.open_poll(50)
        ng.cast_vote(1, rid, True)
        ng.cast_vote(1, rid, True)
        self.assertEqual(ng.poll_tally(ng.rounds[50])["yes"], 1)
        ng.cast_vote(1, rid, False)
        t = ng.poll_tally(ng.rounds[50])
        self.assertEqual((t["yes"], t["no"]), (0, 1))
        ng.cast_vote(2, rid, True)
        t = ng.poll_tally(ng.rounds[50])
        self.assertEqual((t["yes"], t["no"], t["players"], t["not_voted"]), (1, 1, 12, 10))

    def test_votes_are_anonymous_in_everything_the_admin_sees(self):
        rid = ng.rounds[50]["id"]
        ng.open_poll(50)
        ng.cast_vote(3, rid, True)
        t = ng.poll_tally(ng.rounds[50])
        self.assertEqual(set(t), {"yes", "no", "players", "not_voted"})   # counts only
        text = ng.panel_text(ng.rounds[50])
        self.assertIn("Yes 1", text)
        self.assertNotIn("Player3", text)
        self.assertNotIn("uid", text)

    def test_vote_closes_when_the_game_starts(self):
        rid = ng.rounds[50]["id"]
        ng.open_poll(50)
        self.assertTrue(ng.admin_start(50)["ok"])
        res = ng.cast_vote(1, rid, True)
        self.assertFalse(res["ok"])
        self.assertIn("started", res["error"])

    def test_poll_result_is_saved_with_the_game(self):
        rid = ng.rounds[50]["id"]
        ng.open_poll(50)
        for u in (1, 2, 3):
            ng.cast_vote(u, rid, True)
        ng.cast_vote(4, rid, False)
        r = ng.rounds[50]
        ng.admin_start(50)
        poll = r["draw"]["poll"]
        self.assertEqual((poll["yes"], poll["no"]), (3, 1))
        self.assertEqual(ng.result_view(r)["poll"]["yes"], 3)

    def test_no_poll_means_no_poll_record(self):
        r = ng.rounds[50]
        ng.admin_start(50)
        self.assertIsNone(r["draw"]["poll"])

    def test_player_text_tells_the_real_prizes(self):
        text = ng.poll_player_text(ng.rounds[50])
        self.assertIn("60/100", text)
        self.assertIn("1800", text)          # 30 * 60
        self.assertIn("300", text)           # 5 * 60
        self.assertIn("180", text)           # 3 * 60

    def test_no_await_inside_the_money_and_draw_steps(self):
        for fn in (ng.open_poll, ng.cast_vote, ng.admin_start, ng.freeze_round, ng.settle_round):
            self.assertFalse(has_await(fn), fn.__name__)


class AdminStart(Stage2Base):
    def test_start_needs_50_numbers(self):
        fill_round(self.e, 50, count=49)
        res = ng.admin_start(50)
        self.assertFalse(res["ok"])
        self.assertEqual(ng.rounds[50]["status"], "open")
        self.assertEqual(ng.game_counter[50], 0)           # a refused start uses no game number

    def test_start_freezes_draws_and_starts_the_60_seconds(self):
        ng._rng = FixedRng([2, 7, 12])
        fill_round(self.e, 50, count=60)
        before = int(time.time())
        res = ng.admin_start(50)
        r = ng.rounds[50]
        self.assertTrue(res["ok"])
        self.assertEqual((r["status"], r["early"]), ("countdown", True))
        self.assertEqual(res["sold"], 60)
        self.assertGreaterEqual(r["close_at"], before + ng.COUNTDOWN_SECONDS)
        self.assertEqual(r["draw"]["winners"], [2, 7, 12])
        self.assertEqual(r["draw"]["prizes"], [1800, 300, 180])     # scaled to 60 sold
        self.assertEqual(r["game_no"], 1)

    def test_cannot_start_twice_or_buy_afterwards(self):
        fill_round(self.e, 50, count=60)
        self.assertTrue(ng.admin_start(50)["ok"])
        win = ng.rounds[50]["draw"]["winners"][:]
        self.assertFalse(ng.admin_start(50)["ok"])
        self.assertEqual(ng.rounds[50]["draw"]["winners"], win)     # never drawn again
        self.e.fund(900, 1000)
        self.assertEqual(buy(900, 50, 99)["code"], "closed")

    def test_start_refused_for_test_rounds(self):
        ng.ENABLED = False
        ng.test_fill(50, 60)
        ng.ENABLED = True
        res = ng.admin_start(50)
        self.assertFalse(res["ok"])
        self.assertEqual(ng.rounds[50]["status"], "open")

    def test_start_by_poll_game_is_paid_like_any_other(self):
        ng._rng = FixedRng([1, 6, 11])
        fill_round(self.e, 50, count=60)
        before = dict(self.e.wallets)
        ng.admin_start(50)
        settle_now(50)
        self.assertEqual(self.e.wallets[1], before[1] + 1800)       # number 1 -> player 1
        self.assertEqual(self.e.wallets[2], before[2] + 300)        # number 6 -> player 2
        self.assertEqual(self.e.wallets[3], before[3] + 180)        # number 11 -> player 3
        settle_now(50) if ng.rounds[50]["status"] == "countdown" else None
        self.assertEqual(self.e.wallets[1], before[1] + 1800)       # paid exactly once


class StartNotice(Stage2Base):
    def test_every_player_gets_the_60_second_message_once(self):
        fill_round(self.e, 50, count=60)
        ng.admin_start(50)
        self.assertEqual(len(ng.start_notices), 1)
        with fast_sleep():
            asyncio.run(ng.send_start_notices())
            asyncio.run(ng.send_start_notices())               # a second run sends nothing more
        got = [(u, t) for u, t in self.e.sent if "60 seconds" in t]
        self.assertEqual(sorted(u for u, _ in got), list(range(1, 13)))     # 12 players, once each
        self.assertIn("Good luck", got[0][1])
        self.assertIn("Game 1", got[0][1])
        self.assertEqual(ng.start_notices, [])

    def test_a_full_round_also_tells_the_players(self):
        fill_round(self.e, 50)
        self.assertEqual(len(ng.start_notices), 1)
        with fast_sleep():
            asyncio.run(ng.send_start_notices())
        self.assertEqual(len([1 for _, t in self.e.sent if "60 seconds" in t]), 20)

    def test_test_rounds_send_nothing(self):
        ng.ENABLED = False
        ng.test_fill(50, 100)
        ng.ENABLED = True
        self.assertEqual(ng.start_notices, [])

    def test_the_ticker_sends_the_notice_by_itself(self):
        fill_round(self.e, 50, count=50)
        ng.admin_start(50)

        async def go():
            t = asyncio.ensure_future(ng.ticker())
            await asyncio.sleep(1.4)
            t.cancel()
            await asyncio.gather(*list(ng._tasks), return_exceptions=True)
        asyncio.run(go())
        self.assertEqual(len([1 for _, t in self.e.sent if "60 seconds" in t]), 10)

    def test_poll_message_goes_to_every_player_with_yes_and_no_buttons(self):
        fill_round(self.e, 50, count=60)
        ng.open_poll(50)
        rid = ng.rounds[50]["id"]
        with fast_sleep():
            asyncio.run(ng.send_poll(ng.rounds[50]))
        self.assertEqual(sorted(u for u, _ in self.e.markups), list(range(1, 13)))
        rows = self.e.markups[0][1]
        self.assertEqual([d for _, d in rows[0]], [f"ngv:{rid}:1", f"ngv:{rid}:0"])


class RestartStage3(Stage2Base):
    def test_poll_and_votes_come_back(self):
        ng.rounds.clear()
        ng.idem_seen.clear()
        rounds = [rrow(7, 50, poll_at=1234, game_no=3)]
        numbers = [nrow(7, 50, n, 1 + (n - 1) // 5, (n - 1) % 5 + 1, f"{n}:idem-{n:08d}") for n in range(1, 61)]
        votes = [{"round_id": 7, "uid": 1, "vote": 1}, {"round_id": 7, "uid": 2, "vote": 0}]
        asyncio.run(ng.load_state(FakeConn(rounds, numbers, counter=7, votes=votes)))
        r = ng.rounds[50]
        self.assertEqual((r["poll_at"], r["game_no"]), (1234, 3))
        self.assertEqual(r["votes"], {1: 1, 2: 0})
        t = ng.poll_tally(r)
        self.assertEqual((t["yes"], t["no"], t["players"]), (1, 1, 12))
        self.assertTrue(ng.cast_vote(3, 7, True)["ok"])             # voting continues after a restart
        self.assertTrue(ng.admin_start(50)["ok"])

    def test_game_counter_comes_back_and_open_round_gets_next_number(self):
        e = self.e
        r1 = play_game(e, 50)
        r2 = play_game(e, 50)
        rr1, nn1 = rows_of(r1)
        rr2, nn2 = rows_of(r2)
        open_row = rrow(30, 50, game_no=3)
        ng.rounds.clear()
        ng.history[50].clear()
        ng.history[100].clear()
        for p in ng.PRICES:
            ng.game_counter[p] = 0
        e.queue.clear()
        asyncio.run(ng.load_state(FakeConn([open_row], nn1 + nn2, settled=[rr2, rr1], counter=30)))
        self.assertEqual(ng.game_counter[50], 2)
        self.assertEqual(ng.rounds[50]["game_no"], 3)
        self.assertEqual(ng.rounds[100]["game_no"], 1)
        self.assertEqual([h["game_no"] for h in ng.history[50]], [2, 1])

    def test_old_rounds_without_a_number_are_numbered_oldest_first(self):
        r1 = play_game(self.e, 50)
        r2 = play_game(self.e, 50)
        t = play_game(self.e, 50, test=True)
        rows = []
        for r in (r1, r2, t):
            rr, _ = rows_of(r)
            rr["game_no"] = None                                   # data from before game numbers existed
            rows.append(rr)
        legacy_open = rrow(40, 50, game_no=None)
        ng.rounds.clear()
        ng.history[50].clear()
        ng.history[100].clear()
        for p in ng.PRICES:
            ng.game_counter[p] = 0
        self.e.queue.clear()
        asyncio.run(ng.load_state(FakeConn([legacy_open], [], settled=list(reversed(rows)), counter=40)))
        by_id = {h["id"]: h["game_no"] for h in ng.history[50]}
        self.assertEqual(by_id[r1["id"]], 1)
        self.assertEqual(by_id[r2["id"]], 2)
        self.assertIsNone(by_id[t["id"]])                          # the test round got none
        self.assertEqual(ng.rounds[50]["game_no"], 3)
        updates = [a for sql, a in self.e.queue if sql.startswith("UPDATE ng_rounds SET game_no")]
        self.assertEqual(sorted(a[0] for a in updates), [1, 2, 3])  # saved to the database too

    def test_unsent_winner_message_names_the_game(self):
        ng.pending_notify.clear()
        draw = {"winners_info": [{"num": 5, "name": "Abe", "uid": 1}] * 3}
        un = [{"round_id": 9, "place": 1, "num": 5, "uid": 1, "amount": 3000, "lobby": 50,
               "game_no": 4, "data": json.dumps(draw)}]
        row = rrow(9, 50, "settled", close_at=1, data=draw, settled_at=2, game_no=4)
        asyncio.run(ng.load_state(FakeConn([], [], settled=[row], unnotified=un, counter=9)))
        asyncio.run(ng.after_settle())
        self.assertTrue(any("Game 4" in t for _, t in self.e.sent))


class FakePool:
    def __init__(self, conn, fail=False):
        self.conn, self.fail = conn, fail

    def acquire(self):
        pool = self

        class Ctx:
            async def __aenter__(self):
                if pool.fail:
                    raise RuntimeError("database down")
                return pool.conn

            async def __aexit__(self, *a):
                return False
        return Ctx()


class PastGames(Stage2Base):
    def setUp(self):
        super().setUp()
        e = self.e
        self.g = [play_game(e, 50, picks=(1 + i, 6 + i, 11 + i)) for i in range(3)]   # games 1, 2, 3
        self.h = play_game(e, 100, picks=(2, 3, 4))                                  # 100 lobby, game 1
        self.t = play_game(e, 50, test=True)                                          # a test round
        rows, nums = [], []
        for r in self.g + [self.h, self.t]:
            rr, nn = rows_of(r)
            rows.append(rr)
            nums += nn
        self.conn = FakeConn([], nums, settled=rows)
        ng.D["pool"] = self.conn and FakePool(self.conn)

    def tearDown(self):
        ng.D.pop("pool", None)
        super().tearDown()

    def ask(self, fn, **q):
        q.setdefault("initData", "u1")
        return asyncio.run(fn({k: str(v) for k, v in q.items()}))

    def test_list_is_newest_first_and_per_lobby(self):
        res = self.ask(ng.handle_results, lobby=50)
        self.assertTrue(res["ok"])
        self.assertEqual([v["game"] for v in res["results"]], [3, 2, 1])
        self.assertEqual([v["game"] for v in self.ask(ng.handle_results, lobby=100)["results"]], [1])
        self.assertTrue(all(not v["test"] for v in res["results"]))       # test round never listed

    def test_paging_backwards(self):
        a = self.ask(ng.handle_results, lobby=50, limit=2)
        self.assertEqual([v["game"] for v in a["results"]], [3, 2])
        self.assertTrue(a["more"])
        self.assertEqual(a["next_before"], 2)
        b = self.ask(ng.handle_results, lobby=50, limit=2, before=a["next_before"])
        self.assertEqual([v["game"] for v in b["results"]], [1])
        self.assertFalse(b["more"])

    def test_old_games_beyond_the_memory_history_are_still_readable(self):
        ng.history[50].clear()
        ng.history[100].clear()                                          # memory forgot everything
        self.assertEqual([v["game"] for v in self.ask(ng.handle_results, lobby=50)["results"]], [3, 2, 1])
        res = self.ask(ng.handle_game, lobby=50, game=1)
        self.assertTrue(res["ok"])

    def test_one_game_by_its_number(self):
        res = self.ask(ng.handle_game, lobby=50, game=2, initData="u2")
        self.assertTrue(res["ok"])
        g = res["game"]
        self.assertEqual((g["game"], g["lobby"], g["sold"], g["status"]), (2, 50, 100, "settled"))
        self.assertEqual([w["place"] for w in g["winners"]], ["gold", "silver", "bronze"])
        self.assertEqual([w["num"] for w in g["winners"]], [2, 7, 12])
        self.assertEqual([w["prize"] for w in g["winners"]], [3000, 500, 300])
        self.assertEqual(g["board"], list(range(1, 101)))                # the numbers that were sold
        self.assertEqual([m["num"] for m in g["mine"]], [6, 7, 8, 9, 10])    # player 2 owned 6..10
        self.assertEqual(g["replay"], {"intro": ng.DRAW_INTRO, "gap": ng.REVEAL_GAP,
                                       "order": list(ng.REVEAL_ORDER)})
        self.assertEqual(hashlib.sha256(g["verify"].encode()).hexdigest(), g["hash"])
        self.assertEqual(g["settlement"], "paid")

    def test_unknown_or_other_lobby_game_is_not_found(self):
        self.assertEqual(self.ask(ng.handle_game, lobby=50, game=9)["error"], "notfound")
        self.assertEqual(self.ask(ng.handle_game, lobby=100, game=2)["error"], "notfound")
        self.assertEqual(self.ask(ng.handle_game, lobby=77, game=1)["error"], "notfound")
        self.assertEqual(self.ask(ng.handle_game, lobby=50, game="x")["error"], "notfound")
        self.assertEqual(self.ask(ng.handle_game, lobby=50, game=0)["error"], "notfound")

    def test_a_game_still_running_is_not_in_the_records(self):
        fill_round(self.e, 100)                                          # game 2 of 100: counting down
        self.assertEqual(self.ask(ng.handle_game, lobby=100, game=2)["error"], "notfound")
        self.assertEqual([v["game"] for v in self.ask(ng.handle_results, lobby=100)["results"]], [1])

    def test_closed_blocked_and_bad_login(self):
        self.assertEqual(self.ask(ng.handle_game, lobby=50, game=1, initData="bad")["error"], "auth")
        self.e.banned.add(1)
        self.assertEqual(self.ask(ng.handle_game, lobby=50, game=1)["error"], "blocked")
        self.assertEqual(self.ask(ng.handle_results, lobby=50)["error"], "blocked")
        self.e.banned.clear()
        ng.ENABLED = False
        self.assertEqual(self.ask(ng.handle_game, lobby=50, game=1)["error"], "closed")
        self.assertEqual(self.ask(ng.handle_results, lobby=50)["error"], "closed")

    def test_database_down_gives_a_calm_answer(self):
        ng.D["pool"] = FakePool(self.conn, fail=True)
        self.assertEqual(self.ask(ng.handle_results, lobby=50)["error"], "busy")
        self.assertEqual(self.ask(ng.handle_game, lobby=50, game=1)["error"], "busy")

    def test_without_a_database_the_memory_history_is_used(self):
        ng.D.pop("pool")
        self.assertEqual([v["game"] for v in self.ask(ng.handle_results, lobby=50)["results"]], [3, 2, 1])
        res = self.ask(ng.handle_game, lobby=50, game=3)
        self.assertEqual(res["game"]["board"], list(range(1, 101)))

    def test_routes_exist(self):
        try:
            import aiohttp  # noqa: F401
        except ImportError:
            self.skipTest("aiohttp is not installed here")
        paths = [r.resource.canonical for r in ng.routes()]
        self.assertIn("/api/ng/game", paths)
        self.assertIn("/api/ng/results", paths)


class Stage3Sql(Stage2Base):
    def test_every_new_statement_runs_and_the_constraints_hold(self):
        ng.rounds.clear()
        ng.ng_counter[0] = 0
        self.e.queue.clear()
        ng.ensure_rounds()
        ng._rng = FixedRng([1, 6, 11])
        fill_round(self.e, 50, count=60)
        rid = ng.rounds[50]["id"]
        ng.open_poll(50)
        ng.cast_vote(1, rid, True)
        ng.cast_vote(1, rid, False)                     # changed his mind: updates the same row
        ng.cast_vote(2, rid, True)
        r = ng.rounds[50]
        ng.admin_start(50)
        settle_now(50)
        con = SqlCheck().db()
        for sql, args in self.e.queue:
            if sql.startswith(("INSERT INTO ng_", "UPDATE ng_")):
                con.execute(SqlCheck.lite(sql), args)
        row = con.execute("SELECT game_no, poll_at, early, status FROM ng_rounds WHERE id=?", (rid,)).fetchone()
        self.assertEqual((row[0], row[2], row[3]), (1, 1, "settled"))
        self.assertIsNotNone(row[1])
        votes = con.execute("SELECT uid, vote FROM ng_votes WHERE round_id=? ORDER BY uid", (rid,)).fetchall()
        self.assertEqual(votes, [(1, 0), (2, 1)])          # one row per player, last choice kept
        nxt = con.execute("SELECT game_no FROM ng_rounds WHERE lobby=50 AND id<>?", (rid,)).fetchall()
        self.assertIn((2,), nxt)                           # the next round is Game 2
        data = json.loads(con.execute("SELECT data FROM ng_rounds WHERE id=?", (rid,)).fetchone()[0])
        self.assertEqual((data["poll"]["yes"], data["poll"]["no"]), (1, 1))
        self.assertEqual(r["game_no"], 1)

    def test_game_number_is_unique_per_lobby_and_votes_are_checked(self):
        con = SqlCheck().db()
        ins = "INSERT INTO ng_rounds(id, lobby, status, created, game_no) VALUES(?,?,?,?,?)"
        con.execute(ins, (1, 50, "settled", 1, 1))
        con.execute(ins, (2, 100, "settled", 1, 1))        # same number, other lobby: fine
        con.execute(ins, (3, 50, "settled", 1, None))      # test rounds have none: many allowed
        con.execute(ins, (4, 50, "settled", 1, None))
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute(ins, (5, 50, "settled", 1, 1))     # Game 1 of the 50 lobby twice
        con.execute("INSERT INTO ng_votes(round_id, uid, vote, t) VALUES(1, 9, 1, 1)")
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO ng_votes(round_id, uid, vote, t) VALUES(1, 9, 0, 1)")   # second row
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO ng_votes(round_id, uid, vote, t) VALUES(1, 10, 2, 1)")  # bad value


# ---- the Telegram side: commands and buttons, with a pretend Telegram ----
def telegram_stubs():
    """The tests do not need the real aiogram. If it is missing, put small stand-ins in its place."""
    try:
        import aiogram  # noqa: F401
        return []
    except ImportError:
        pass
    import types

    class Magic:
        def __getattr__(self, name):
            return Magic()

        def startswith(self, prefix):
            return ("startswith", prefix)

    mods = {}
    root = types.ModuleType("aiogram")
    root.F = Magic()
    flt = types.ModuleType("aiogram.filters")
    flt.Command = lambda *names: ("command", names)
    flt.CommandObject = type("CommandObject", (), {})
    typ = types.ModuleType("aiogram.types")
    for n in ("Message", "CallbackQuery", "InlineKeyboardMarkup", "InlineKeyboardButton"):
        setattr(typ, n, type(n, (), {}))
    mods.update({"aiogram": root, "aiogram.filters": flt, "aiogram.types": typ})
    sys.modules.update(mods)
    return list(mods)


class FakeDp:
    """Remembers the handler functions by name, like a very small Dispatcher."""

    def __init__(self):
        self.h = {}

    def _reg(self, *a, **k):
        def deco(fn):
            self.h[fn.__name__] = fn
            return fn
        return deco
    message = callback_query = _reg


class FakeUser:
    def __init__(self, uid):
        self.id, self.first_name = uid, "P"


class FakeMsg:
    def __init__(self, uid, text=""):
        self.from_user, self.text, self.reply_markup = FakeUser(uid), text, None
        self.said, self.edited = [], []

    async def answer(self, text, **kw):
        self.said.append((text, kw.get("reply_markup")))

    async def edit_text(self, text, reply_markup=None):
        self.edited.append((text, reply_markup))
        self.text = text


class FakeCb:
    def __init__(self, uid, data, msg=None):
        self.from_user, self.data = FakeUser(uid), data
        self.message = msg or FakeMsg(uid, "poll text")
        self.alerts, self.toasts = [], []

    async def answer(self, text=None, show_alert=False):
        (self.alerts if show_alert else self.toasts).append(text)


class Cmd:
    def __init__(self, args=None):
        self.args = args


class TelegramSide(Stage2Base):
    def setUp(self):
        super().setUp()
        self._stubs = telegram_stubs()
        self.dp = FakeDp()
        ng.register(self.dp)
        self.h = self.dp.h
        fill_round(self.e, 50, count=60)               # players 1..12
        self.rid = ng.rounds[50]["id"]

    def tearDown(self):
        for m in self._stubs:
            sys.modules.pop(m, None)
        super().tearDown()

    def run_all(self, coro):
        async def go():
            out = await coro
            await asyncio.gather(*list(ng._tasks))
            return out
        with fast_sleep():
            return asyncio.run(go())

    def test_all_handlers_are_registered(self):
        for name in ("cmd_ngpoll", "cmd_ngstart", "cb_vote", "cb_panel", "cmd_ngresult", "cmd_ngstatus"):
            self.assertIn(name, self.h)

    def test_only_the_admin_can_use_poll_and_start_commands(self):
        m = FakeMsg(5)
        self.run_all(self.h["cmd_ngpoll"](m, Cmd("50")))
        self.run_all(self.h["cmd_ngstart"](m, Cmd("50")))
        self.assertEqual(m.said, [])
        self.assertEqual(ng.rounds[50]["status"], "open")
        self.assertIsNone(ng.rounds[50]["poll_at"])

    def test_ngpoll_shows_the_panel_and_sends_the_vote_to_players(self):
        m = FakeMsg(ADMIN)
        self.run_all(self.h["cmd_ngpoll"](m, Cmd("50")))
        self.assertIn("Poll", m.said[0][0])
        self.assertIn("Start game now", [t for t, _ in m.said[0][1][0]][1])
        self.assertEqual(len(self.e.markups), 12)
        m2 = FakeMsg(ADMIN)
        self.run_all(self.h["cmd_ngpoll"](m2, Cmd("50")))         # again: panel only, nobody is spammed
        self.assertEqual(len(self.e.markups), 12)
        self.assertEqual(len(m2.said), 1)

    def test_ngpoll_refusals(self):
        m = FakeMsg(ADMIN)
        self.run_all(self.h["cmd_ngpoll"](m, Cmd("")))
        self.run_all(self.h["cmd_ngpoll"](m, Cmd("77")))
        self.run_all(self.h["cmd_ngpoll"](m, Cmd("100")))         # only 0 sold there
        self.assertTrue(all("❌" in t or "Send:" in t for t, _ in m.said))
        self.assertEqual(self.e.markups, [])

    def test_player_votes_with_the_buttons(self):
        self.run_all(self.h["cmd_ngpoll"](FakeMsg(ADMIN), Cmd("50")))
        cb = FakeCb(3, f"ngv:{self.rid}:1")
        self.run_all(self.h["cb_vote"](cb))
        self.assertEqual(ng.rounds[50]["votes"][3], 1)
        self.assertIn("Yes", cb.toasts[0])
        self.assertIn("Your vote", cb.message.edited[0][0])
        cb2 = FakeCb(3, f"ngv:{self.rid}:0")                      # changes his mind
        self.run_all(self.h["cb_vote"](cb2))
        self.assertEqual(ng.rounds[50]["votes"][3], 0)
        self.assertEqual(ng.poll_tally(ng.rounds[50])["yes"], 0)

    def test_a_stranger_and_bad_buttons_cannot_vote(self):
        self.run_all(self.h["cmd_ngpoll"](FakeMsg(ADMIN), Cmd("50")))
        cb = FakeCb(777, f"ngv:{self.rid}:1")
        self.run_all(self.h["cb_vote"](cb))
        self.assertEqual(len(cb.alerts), 1)
        self.assertEqual(ng.rounds[50]["votes"], {})
        for bad in ("ngv:abc:1", f"ngv:{self.rid}:7", "ngv:", f"ngv:{self.rid}"):
            self.run_all(self.h["cb_vote"](FakeCb(1, bad)))
        self.assertEqual(ng.rounds[50]["votes"], {})

    def test_panel_buttons_refresh_confirm_and_start(self):
        self.run_all(self.h["cmd_ngpoll"](FakeMsg(ADMIN), Cmd("50")))
        self.run_all(self.h["cb_vote"](FakeCb(1, f"ngv:{self.rid}:1")))
        refresh = FakeCb(ADMIN, "ngp:50:r")
        self.run_all(self.h["cb_panel"](refresh))
        self.assertIn("Yes 1", refresh.message.edited[0][0])
        ask = FakeCb(ADMIN, "ngp:50:s")
        self.run_all(self.h["cb_panel"](ask))
        self.assertIn("cannot be undone", ask.message.edited[0][0])
        self.assertEqual(ng.rounds[50]["status"], "open")           # asking does NOT start the game
        back = FakeCb(ADMIN, "ngp:50:n")
        self.run_all(self.h["cb_panel"](back))
        self.assertEqual(ng.rounds[50]["status"], "open")
        go = FakeCb(ADMIN, "ngp:50:y")
        self.run_all(self.h["cb_panel"](go))
        self.assertEqual(ng.rounds[50]["status"], "countdown")
        self.assertIn("Started", go.message.edited[0][0])
        again = FakeCb(ADMIN, "ngp:50:y")
        self.run_all(self.h["cb_panel"](again))
        self.assertEqual(len(again.alerts), 1)                      # second press: refused, not drawn twice

    def test_nobody_but_the_admin_can_press_the_panel_buttons(self):
        self.run_all(self.h["cmd_ngpoll"](FakeMsg(ADMIN), Cmd("50")))
        for who in (1, 5, 999999):
            for act in ("y", "s", "r", "n"):
                cb = FakeCb(who, f"ngp:50:{act}")
                self.run_all(self.h["cb_panel"](cb))
                self.assertEqual(cb.message.edited, [])
        self.assertEqual(ng.rounds[50]["status"], "open")           # a crafted button did not start it

    def test_panel_after_the_round_started_is_refused(self):
        self.run_all(self.h["cmd_ngpoll"](FakeMsg(ADMIN), Cmd("50")))
        ng.admin_start(50)
        cb = FakeCb(ADMIN, "ngp:50:s")
        self.run_all(self.h["cb_panel"](cb))
        self.assertEqual(len(cb.alerts), 1)
        self.assertEqual(cb.message.edited, [])

    def test_ngstart_command_starts_and_the_players_are_told(self):
        m = FakeMsg(ADMIN)
        self.run_all(self.h["cmd_ngstart"](m, Cmd("50")))
        self.assertIn("started", m.said[0][0])
        self.assertEqual(ng.rounds[50]["status"], "countdown")
        self.run_all(ng.send_start_notices())
        self.assertEqual(len([1 for _, t in self.e.sent if "60 seconds" in t]), 12)
        m2 = FakeMsg(ADMIN)
        self.run_all(self.h["cmd_ngstart"](m2, Cmd("50")))
        self.assertIn("❌", m2.said[0][0])

    def test_ngstart_refuses_too_few_numbers(self):
        m = FakeMsg(ADMIN)
        self.run_all(self.h["cmd_ngstart"](m, Cmd("100")))
        self.assertIn("❌", m.said[0][0])
        self.assertEqual(ng.rounds[100]["status"], "open")


if __name__ == "__main__":
    unittest.main()
