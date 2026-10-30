"""Backend sin TUI: SQL, perfiles, la sesión contra SQLite y el armado de las ediciones."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

os.environ["LAZYDB_CONF"] = os.path.join(tempfile.mkdtemp(), "profiles.json")

from lazydb import core, demo  # noqa: E402
from lazydb.drivers import DbError  # noqa: E402
from lazydb.drivers.mssql import MssqlDriver  # noqa: E402
from lazydb.drivers.mysql import MysqlDriver  # noqa: E402

DB = Path(tempfile.mkdtemp()) / "shop.sqlite"
demo.build(DB)


def session(mode: str = "readwrite") -> core.Session:
    s = core.Session(core.Profile("t", "sqlite", database=str(DB), mode=mode))
    s.connect()
    return s


class SqlTextTest(unittest.TestCase):
    def test_split_keeps_quotes_comments_and_dollar_bodies(self):
        text = "select 1;\n-- a; b\nselect 'x;y', $$p;q$$ from t;\nupdate t set a = 1\nGO\nselect 2"
        self.assertEqual([s for _, _, s in core.split_statements(text)],
                         ["select 1", "-- a; b\nselect 'x;y', $$p;q$$ from t", "update t set a = 1", "select 2"])

    def test_statement_at_cursor(self):
        text = "select 1;  \nselect 2;"
        self.assertEqual(core.statement_at(text, 0), "select 1")
        self.assertEqual(core.statement_at(text, 10), "select 1")     # después del ; en la misma línea
        self.assertEqual(core.statement_at(text, 13), "select 2")

    def test_readonly_detection(self):
        self.assertTrue(core.is_readonly("/* x */ SELECT 1"))
        self.assertTrue(core.is_readonly("with a as (select 1) select * from a"))
        self.assertFalse(core.is_readonly("with a as (delete from t returning *) select * from a"))
        self.assertFalse(core.is_readonly("EXPLAIN ANALYZE DELETE FROM t"))
        self.assertFalse(core.is_readonly("insert into t values (1)"))

    def test_transaction_commands(self):
        self.assertEqual(core.tx_command("BEGIN"), "begin")
        self.assertEqual(core.tx_command("commit work;"), "commit")
        self.assertEqual(core.tx_command("ROLLBACK"), "rollback")
        self.assertEqual(core.tx_command("BEGIN TRY"), "")
        self.assertEqual(core.tx_command("rollback to savepoint a"), "")

    def test_simple_table(self):
        self.assertEqual(core.simple_table("select * from users where id = 1"), ("", "users"))
        self.assertEqual(core.simple_table('SELECT id, name FROM public."Users" u ORDER BY id'), ("public", "Users"))
        self.assertEqual(core.simple_table("SELECT TOP 10 * FROM [dbo].[x]"), ("dbo", "x"))
        for q in ("select count(*) from t", "select a + 1 from t", "select a from t join b on true",
                  "select distinct a from t", "select a as b from t"):
            self.assertIsNone(core.simple_table(q), q)


class ProfileTest(unittest.TestCase):
    def test_round_trip_without_passwords(self):
        p = core.Profile("crm", "postgres", host="db", user="me", password="keyring", ssh_host="bastion")
        core.save_profiles([p])
        self.assertEqual(core.load_profiles(), [p])
        self.assertEqual(oct(core.CONF.stat().st_mode & 0o777), "0o600")
        self.assertEqual(p.real_port, 5432)
        self.assertTrue(p.tunneled)
        self.assertEqual(p.url, "postgres://me@db:5432")

    def test_tunnel_command(self):
        p = core.Profile("x", "mssql", host="sql01", ssh_host="jump", ssh_user="ops", ssh_port=2222,
                         ssh_key="~/.ssh/id_ed25519")
        t = core.Tunnel(p, "sql01", 1433)
        t.local = 40000
        cmd = t.command()
        self.assertEqual(cmd[:4], ["ssh", "-N", "-L", "127.0.0.1:40000:sql01:1433"])
        self.assertIn("BatchMode=yes", cmd)
        self.assertEqual(cmd[-1], "ops@jump")
        self.assertIn(str(Path("~/.ssh/id_ed25519").expanduser()), cmd)

    def test_tunnel_failure_is_reported(self):
        p = core.Profile("x", "postgres", host="db", ssh_host="nonexistent.invalid", timeout=3)
        with self.assertRaises(DbError) as e:
            core.Tunnel(p, "db", 5432).open()
        self.assertIn("SSH tunnel to nonexistent.invalid", str(e.exception))
        out = subprocess.run(["pgrep", "-f", "nonexistent.invalid"], capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), "")


class SessionTest(unittest.TestCase):
    def test_catalog(self):
        s = session()
        schemas = s.children(None)
        self.assertEqual([o.name for o in schemas], ["main"])
        tables, views = s.children(schemas[0])[:2]
        self.assertEqual([o.name for o in s.children(tables)], ["customers", "order_items", "orders", "products"])
        self.assertEqual([o.name for o in s.children(views)], ["low_stock", "order_totals"])
        items = next(o for o in s.children(tables) if o.name == "order_items")
        cols, idx, fks = s.children(items)
        self.assertEqual([c.name for c in s.children(cols)], ["order_id", "sku", "quantity", "unit_price"])
        self.assertEqual(len(s.children(fks)), 2)
        self.assertIn("CREATE TABLE order_items", s.ddl(items))
        self.assertEqual(s.count("main", "orders"), 60)
        s.close()

    def test_open_table_and_more(self):
        s = session()
        res = s.open_table("main", "orders", limit=25)
        self.assertEqual(len(res.rows), 25)
        self.assertTrue(res.more)
        self.assertTrue(res.editable)
        res = s.run("SELECT * FROM order_items")
        n = len(res.rows)
        while res.more:
            s.more(res)
        self.assertGreaterEqual(len(res.rows), n)
        s.close()

    def test_editor_writes_wait_for_commit(self):
        s = session()
        s.run("UPDATE products SET stock = 999 WHERE sku = 'CB-01'")
        self.assertTrue(s.in_tx)
        other = session()
        self.assertEqual(other.run("SELECT stock FROM products WHERE sku = 'CB-01'").rows, [[300]])
        s.run("ROLLBACK")
        self.assertFalse(s.in_tx)
        self.assertEqual(s.run("SELECT stock FROM products WHERE sku = 'CB-01'").rows, [[300]])
        s.close()
        other.close()

    def test_readonly_refuses_writes(self):
        s = session("readonly")
        with self.assertRaises(DbError):
            s.run("DELETE FROM orders")
        s.close()

    def test_apply_is_all_or_nothing(self):
        s = session()
        res = s.open_table("main", "customers")
        email = res.columns.index("email")
        stmts = core.change_statements(s.driver, res, {0: {email: "dup@example.com"}, 1: {email: "dup@example.com"}},
                                       set(), [])
        with self.assertRaises(DbError):          # UNIQUE(email): el segundo UPDATE falla
            s.apply(stmts)
        self.assertNotEqual(s.run("SELECT email FROM customers WHERE id = 1").rows[0][0], "dup@example.com")
        s.close()


class ChangeStatementsTest(unittest.TestCase):
    def test_quoting_and_placeholders_per_engine(self):
        from lazydb.drivers import Column, Result
        res = Result("", columns=["id", "name"], rows=[[1, "a"], [None, "b"]], table=("dbo", "t"),
                     info=[Column("id", pk=True), Column("name")])
        stmts = core.change_statements(MssqlDriver(), res, {0: {1: "z"}}, {1}, [{"name": "new"}, {}])
        self.assertEqual(stmts, [
            ("DELETE FROM [dbo].[t] WHERE [id] IS NULL", []),
            ("UPDATE [dbo].[t] SET [name] = %s WHERE [id] = %s", ["z", 1]),
            ("INSERT INTO [dbo].[t] ([name]) VALUES (%s)", ["new"]),
            ("INSERT INTO [dbo].[t] DEFAULT VALUES", []),
        ])
        self.assertEqual(MysqlDriver().insert_default_sql("`t`"), "INSERT INTO `t` () VALUES ()")
        self.assertEqual(core.render_sql(*stmts[1], "%s"), "UPDATE [dbo].[t] SET [name] = 'z' WHERE [id] = 1")


if __name__ == "__main__":
    unittest.main()
