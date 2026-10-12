"""
Stage 1 tests for numbergame.py.
Run from the main folder:   python -m unittest discover -s tests -v
No Telegram, aiohttp or Postgres needed (the SQL is checked on SQLite).
"""
import asyncio
import inspect
import os
import random
import re
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numbergame as ng  # noqa: E402

ADMIN = 999


class Env:
    """Fake of everything bot.py gives the module."""

    def __init__(self):
        self.wallets, self.wagered, self.required = {}, {}, {}
        self.queue, self.history = [], []
        self.phones, self.banned = set(), set()

    def turnover_left(self, uid):
        return max(0, self.required.get(uid, 0) - self.wagered.get(uid, 0))

    def log(self, uid, kind, amount, note="", hidden=False):
        self.history.append((uid, kind, amount, note))
        self.queue.append(("SAVE_USER", (uid, self.wallets.get(uid, 0))))

    def enqueue(self, sql, *args):
        self.queue.append((sql, args))

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
    ng.ENABLED = True
    ng.init(wallets=e.wallets, wagered=e.wagered, turnover_left=e.turnover_left, log=e.log,
            enqueue=e.enqueue, save_counter=e.save_counter, verify=e.verify, touch=e.touch,
            phones=e.phones, banned=e.banned, admin_id=ADMIN, ensure=e.ensure)
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
        self.assertNotIn("await ", inspect.getsource(ng.buy_number))


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
        self.assertEqual(ng.rounds[50]["status"], "full")
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
    def __init__(self, rounds, numbers, counter=0):
        self.rounds, self.numbers, self.counter = rounds, numbers, counter

    async def fetch(self, sql, *args):
        if "FROM ng_rounds" in sql:
            return self.rounds
        if "FROM ng_numbers" in sql:
            return [n for n in self.numbers if n["round_id"] in args[0]]
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "MAX(id)" in sql:
            return max([r["id"] for r in self.rounds] + [0])
        if "FROM kv" in sql:
            return self.counter
        raise AssertionError(sql)


def nrow(rid, lobby, num, uid, slot, idem, name="N", used=0):
    return {"round_id": rid, "lobby": lobby, "num": num, "uid": uid, "name": name,
            "price": lobby, "used": used, "slot": slot, "idem": idem, "t": 1}


class Restart(unittest.TestCase):
    def test_state_comes_back_after_restart(self):
        e = make_env()
        ng.rounds.clear()
        ng.idem_seen.clear()
        ng.ng_counter[0] = 0
        rounds = [{"id": 7, "lobby": 50, "status": "open", "created": 1, "full_at": None}]
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

    def test_hundred_sold_is_marked_full(self):
        make_env()
        ng.rounds.clear()
        ng.idem_seen.clear()
        rounds = [{"id": 2, "lobby": 100, "status": "open", "created": 1, "full_at": None}]
        numbers = [nrow(2, 100, n, 1 + (n - 1) // 5, (n - 1) % 5 + 1, f"{n}:idem-{n:08d}")
                   for n in range(1, 101)]
        asyncio.run(ng.load_state(FakeConn(rounds, numbers, counter=2)))
        self.assertEqual(ng.rounds[100]["status"], "full")


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
            if stmt.strip():
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
        self.assertEqual(con.execute("SELECT status FROM ng_rounds WHERE lobby=100").fetchone()[0], "full")
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


if __name__ == "__main__":
    unittest.main()
