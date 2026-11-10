"""La TUI headless (Textual run_test) contra la base SQLite del modo demo."""

import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

os.environ["LAZYDB_THEME"] = "/nonexistent"     # tema por defecto

from lazydb import core, demo  # noqa: E402

PROFILE = demo.setup()
DB = next(p.database for p in core.load_profiles() if p.name == PROFILE)

from lazydb.app import LazyDbApp  # noqa: E402


async def settle(app, pilot):
    for _ in range(4):
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)


def tree_keys(v):
    return [r.key.value for r in v.query_one("#tree").ordered_rows]


async def open_table(app, pilot, name):
    v = app.view
    v.action_focus_tree()
    keys = tree_keys(v)
    v.query_one("#tree").move_cursor(row=[k.endswith("\x1f" + name) for k in keys].index(True))
    await pilot.press("enter")
    await settle(app, pilot)


class AppTest(unittest.IsolatedAsyncioTestCase):
    async def test_browse_edit_and_save(self):
        app = LazyDbApp(PROFILE)
        async with app.run_test(size=(180, 50)) as pilot:
            await settle(app, pilot)
            v = app.view
            self.assertTrue(v.session.connected)
            self.assertEqual(v.focused_id(), "tree")
            names = [k.split("\x1f")[-1] for k in tree_keys(v)]
            self.assertEqual(names[:6], ["main", "Tables", "customers", "order_items", "orders", "products"])

            await open_table(app, pilot, "products")
            res = v.result
            self.assertEqual(v.focused_id(), "results")
            self.assertTrue(res.editable)
            self.assertEqual(res.pk, ["sku"])

            grid = v.query_one("#results")
            grid.move_cursor(row=0, column=res.columns.index("stock") + 1)
            await pilot.press("e")
            await pilot.pause()
            app.screen.query_one("#f-value").value = "41"
            await pilot.press("enter")
            await pilot.pause()
            grid.move_cursor(row=5)
            await pilot.press("d")                          # SP-05 tiene pedidos: la FK lo va a frenar
            await pilot.press("a")
            await pilot.pause()
            app.screen.query_one("#f-c0").value = "TB-01"
            app.screen.query_one("#f-c1").value = "Thunderbolt cable"
            app.screen.query_one("#f-c2").value = "Accessories"
            app.screen.query_one("#f-c3").value = "29.5"
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertEqual(v.pending, 3)

            await pilot.press("ctrl+s")                     # confirmación con el SQL
            await pilot.pause()
            self.assertIn("DELETE FROM", app.screen.message)
            await pilot.press("y")
            await settle(app, pilot)
            self.assertEqual(v.pending, 3)                  # todo o nada: quedan pendientes

            grid.move_cursor(row=5)
            await pilot.press("d")                          # desmarca el borrado
            await pilot.press("ctrl+s")
            await pilot.pause()
            await pilot.press("y")
            await settle(app, pilot)
            self.assertEqual(v.pending, 0)
            self.assertIn("TB-01", [r[0] for r in v.result.rows])   # se volvió a leer la tabla

        db = sqlite3.connect(DB)
        self.assertEqual(db.execute("SELECT stock FROM products WHERE sku = 'KB-01'").fetchone()[0], 41)
        self.assertIsNotNone(db.execute("SELECT 1 FROM products WHERE sku = 'SP-05'").fetchone())
        self.assertEqual(db.execute("SELECT stock FROM products WHERE sku = 'TB-01'").fetchone()[0], 0)
        db.close()

    async def test_editor_transaction_and_export(self):
        app = LazyDbApp(PROFILE)
        async with app.run_test(size=(180, 50)) as pilot:
            await settle(app, pilot)
            v = app.view
            v.action_focus_editor()
            v.editor.load_text("")
            await pilot.press("q", "1", "x", "d")          # en el editor las letras son texto
            self.assertEqual(v.editor.text, "q1xd")
            self.assertTrue(app.is_running)
            await pilot.press("escape")
            self.assertEqual(v.focused_id(), "results")

            v.editor.load_text("UPDATE customers SET city = 'Tandil' WHERE id = 3;\n"
                               "SELECT id, city FROM customers WHERE id = 3")
            v.editor.move_cursor((0, 3))
            await pilot.press("f5")
            await settle(app, pilot)
            self.assertTrue(v.session.in_tx)
            self.assertEqual(v.result.message, "1 rows affected")

            await pilot.press("f9")
            await settle(app, pilot)
            self.assertEqual(v.result.rows, [[3, "Tandil"]])
            self.assertTrue(v.result.editable)              # SELECT simple de una tabla

            await pilot.press("R")                          # rollback
            await settle(app, pilot)
            self.assertFalse(v.session.in_tx)
            await pilot.press("r")                          # vuelve a leer: el cambio se deshizo
            await settle(app, pilot)
            self.assertEqual(v.result.rows, [[3, "Rosario"]])

            await open_table(app, pilot, "orders")
            out = Path(tempfile.mkdtemp()) / "orders"
            await pilot.press("X")
            await pilot.pause()
            app.screen.query_one("#f-path").value = str(out)
            await pilot.press("ctrl+s")
            await settle(app, pilot)
            with zipfile.ZipFile(out.with_suffix(".xlsx")) as z:
                sheet = z.read("xl/worksheets/sheet1.xml").decode()
                info = z.read("xl/worksheets/sheet2.xml").decode()
            self.assertIn("customer_id", sheet)
            self.assertIn("SELECT * FROM", info)

            self.assertIn("UPDATE customers", v.state["history"][PROFILE][1])

    async def test_copy_all_results(self):
        from unittest import mock
        app = LazyDbApp(PROFILE)
        async with app.run_test(size=(180, 50)) as pilot:
            await settle(app, pilot)
            v = app.view
            await open_table(app, pilot, "products")
            with mock.patch("lazydb.app.copy_text", return_value=True) as cp:
                await pilot.press("ctrl+y")
                await pilot.pause()
            lines = cp.call_args[0][0].splitlines()
            self.assertEqual([c.strip() for c in lines[0].split("|")[:-1]], v.result.columns)
            self.assertTrue(set(lines[1]) <= {"-", "+"})
            self.assertEqual(len(lines), len(v.result.rows) + 2)
            self.assertEqual(len({len(x) for x in lines}), 1)     # todas alineadas

    def test_text_table(self):
        from lazydb.app import text_table
        self.assertEqual(text_table(["tabla", "columna"], [["Delegation", "UID_PersonWantsOrg"], ["PWO", "a\nb"]]),
                         "tabla     |columna           |\n"
                         "----------+------------------+\n"
                         "Delegation|UID_PersonWantsOrg|\n"
                         "PWO       |a b               |\n")

    async def test_readonly_profile(self):
        ro = core.Profile("ro", "sqlite", database=DB, mode="readonly")
        app = LazyDbApp(adhoc=ro)
        async with app.run_test(size=(180, 50)) as pilot:
            await settle(app, pilot)
            v = app.view
            self.assertEqual(v.session.profile.name, "ro")
            v.editor.load_text("DELETE FROM orders")
            v.action_focus_editor()
            await pilot.press("f5")
            await settle(app, pilot)
            self.assertFalse(v.session.in_tx)
            await open_table(app, pilot, "customers")
            await pilot.press("e")
            await pilot.pause()
            self.assertEqual(type(app.screen).__name__, "Screen")   # no abre el editor de celda
        db = sqlite3.connect(DB)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 60)
        db.close()


if __name__ == "__main__":
    unittest.main()
