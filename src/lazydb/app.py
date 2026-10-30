"""
app.py — la TUI de lazydb: un cliente de bases de datos en la terminal
(estilo DBeaver, con la forma de lazygit).

- [1] Profiles: perfiles de conexión (motor, servidor, base, usuario, TLS,
  túnel SSH, modo solo lectura / lectura-escritura). enter conecta; la
  contraseña se pide al conectar o sale del keyring (secret-tool).
- [2] Objects: el catálogo (esquemas → tablas, vistas, rutinas → columnas,
  índices, claves foráneas). Se carga por niveles al expandir; enter sobre
  una tabla la abre en los resultados.
- [3] Editor: SQL con historial. F5 corre la sentencia del cursor (o la
  selección) y F9 todo el texto.
- [4] Results: grilla de resultados por tandas, export a Excel o CSV. Con
  un perfil read-write y los resultados de una sola tabla con clave
  primaria: editar celdas, agregar y borrar filas; los cambios quedan
  pendientes hasta ctrl+s, que los aplica en una transacción.
- Lo que el editor modifica corre en una transacción que queda abierta
  hasta COMMIT / ROLLBACK (C / R).

Backend sin Textual: core.py y drivers/. Piezas de interfaz: ui.py.
"""

from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static, TextArea

from . import core as C
from .drivers import ENGINES, DbError, Result
from .ui import (THEME, ConfirmModal, DView, Field, FormModal, Panel, PickModal, Table, TextModal, ViewApp,
                 bindings_of, css, key_display)
from .xlsx import write_xlsx

M, OK, WARN, ERR, P = THEME["muted"], THEME["status_ok"], THEME["status_warn"], THEME["status_error"], THEME["primary"]

ENGINE = [(label, key) for key, label in ENGINES.items()]
PASSWORD = [("Ask when connecting", "ask"), ("Store in keyring", "keyring"), ("No password", "none")]
SSL = [("Driver default", "default"), ("Require TLS", "require"), ("Require TLS + verify certificate", "verify"),
       ("Disable TLS", "disable")]
MODE = [("Read-only", "readonly"), ("Read-write", "readwrite")]

# Íconos (Nerd Font, Material Design)
ICONS = {"schema": "\U000F01BC", "folder": "\U000F024B", "table": "\U000F04EB", "view": "\U000F0208",
         "routine": "\U000F0295", "column": "\U000F0835", "index": "\U000F0279", "fk": "\U000F0337",
         "pk": "\U000F0306", "ssh": "\U000F08C0"}
CELL_WIDTH = 60


def copy_text(text: str) -> bool:
    cmd = ["wl-copy"] if os.environ.get("WAYLAND_DISPLAY") else ["xclip", "-selection", "clipboard"]
    if not shutil.which(cmd[0]):
        return False
    try:
        subprocess.run(cmd, input=text, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def cell(v: Any, style: str = "") -> Text:
    """Celda de la grilla: NULL y binarios atenuados, números a la derecha, texto recortado."""
    if v is None:
        return Text("NULL", style=f"italic {M} {style}".strip())
    if isinstance(v, (bytes, bytearray, memoryview)):
        return Text(f"<{len(bytes(v))} bytes>", style=f"italic {M} {style}".strip())
    s = C.cell_text(v).replace("\r", "").replace("\n", "↵")
    if len(s) > CELL_WIDTH:
        s = s[:CELL_WIDTH - 1] + "…"
    num = isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)
    return Text(s, style=style, justify="right" if num else "left")


def slug(text: str) -> str:
    return re.sub(r"[^\w.-]+", "_", text).strip("_")[:40] or "query"


class ObjTable(Table):
    """El árbol: l / → expanden y h / ← colapsan (o suben al padre)."""

    BINDINGS = [Binding("l,right", "expand", show=False), Binding("h,left", "collapse", show=False)]

    def view(self) -> "LazyDbView":
        return next(a for a in self.ancestors if isinstance(a, LazyDbView))

    def action_expand(self) -> None:
        self.view().action_expand()

    def action_collapse(self) -> None:
        self.view().action_collapse()


class GridTable(Table):
    """Los resultados: cursor de celda, h / l se mueven entre columnas."""

    BINDINGS = [Binding("h", "cursor_left", show=False), Binding("l", "cursor_right", show=False)]

    def __init__(self, **kw) -> None:
        kw.setdefault("cursor_type", "cell")
        super().__init__(**kw)


class Node:
    def __init__(self, obj: C.Obj, depth: int = 0) -> None:
        self.obj, self.depth = obj, depth
        self.children: Optional[list[str]] = None     # claves; None = sin cargar
        self.expanded = False
        self.loading = False


class LazyDbView(DView):
    FOCUS = "#profiles"

    DEFAULT_CSS = css("""
    LazyDbView > Horizontal { height: 1fr; }
    #left { width: 1fr; min-width: 34; max-width: 56; }
    #right { width: 3fr; }
    #profiles-panel { height: auto; max-height: 10; }
    #profiles { height: auto; max-height: 8; }
    #tree-panel { height: 1fr; }
    #tree { height: 1fr; }
    #session-panel { height: auto; }
    #session { height: auto; }
    #editor-panel { height: 12; }
    #editor { height: 1fr; }
    #results-panel { height: 1fr; }
    #results { height: 1fr; overflow-x: auto; scrollbar-size-horizontal: 1; }
    """)

    BINDINGS = [
        Binding("1", "focus_profiles", "profiles", show=False),
        Binding("2", "focus_tree", "objects", show=False),
        Binding("3", "focus_editor", "editor", show=False),
        Binding("4", "focus_results", "results", show=False),
        Binding("f5,ctrl+r", "run_statement", "run statement"),
        Binding("f9,alt+x", "run_script", "run all statements"),
        Binding("n", "new", "new"),
        Binding("e", "edit", "edit"),
        Binding("a", "add", "add row"),
        Binding("d", "delete", "delete"),
        Binding("u", "undo", "undo pending changes", show=False),
        Binding("ctrl+s", "save", "save pending changes"),
        Binding("C", "commit", "commit"),
        Binding("R", "rollback", "rollback"),
        Binding("G", "more", "load more rows", show=False),
        Binding("s", "select", "SELECT into the editor", show=False),
        Binding("D", "ddl", "DDL", show=False),
        Binding("c", "count", "count rows", show=False),
        Binding("slash", "filter", "filter objects"),
        Binding("H", "history", "query history", show=False),
        Binding("X", "export", "export (Excel / CSV)", show=False),
        Binding("r", "refresh", "refresh"),
        Binding("y", "copy", "copy"),
        Binding("Y", "copy_row", "copy row", show=False),
        Binding("L", "log", "session log", show=False),
        Binding("question_mark", "help", "help", show=False),
        Binding("x", "disconnect", "disconnect"),
        Binding("escape", "back", "back / quit", show=False),
        Binding("q", "quit_safe", "quit", show=False),
    ]

    def __init__(self, autoconnect: str = "", adhoc: Optional[C.Profile] = None, **kw) -> None:
        super().__init__(**kw)
        self.autoconnect, self.adhoc = autoconnect, adhoc
        self.profiles: list[C.Profile] = []
        self.session: Optional[C.Session] = None
        self.connecting = ""
        self.nodes: dict[str, Node] = {}
        self.roots: list[str] = []
        self.tree_filter = ""
        self.result: Optional[Result] = None
        self.source: tuple = ()             # ("table", esquema, nombre, tipo) | ("query", sql): para refrescar
        self.edits: dict[int, dict[int, Any]] = {}
        self.deleted: set[int] = set()
        self.inserted: list[dict[str, Any]] = []
        self.busy = ""
        self.editor_text = ""
        self.state = C.load_state()         # último perfil, texto del editor e historial (entre aperturas)

    def compose(self) -> ComposeResult:
        try:
            editor = TextArea(id="editor", language="sql", show_line_numbers=True, soft_wrap=False)
        except Exception:  # noqa: BLE001 - sin tree-sitter: sin resaltado
            editor = TextArea(id="editor", show_line_numbers=True, soft_wrap=False)
        editor.placeholder = "SELECT ...   (F5 runs the statement under the cursor, F9 everything)"
        yield Horizontal(
            Vertical(Panel(Table(id="profiles"), title="[1] Profiles", id="profiles-panel"),
                     Panel(ObjTable(id="tree", show_header=False), title="[2] Objects", id="tree-panel"),
                     id="left"),
            Vertical(Panel(Static(id="session"), title="Session", id="session-panel"),
                     Panel(editor, title="[3] Editor", id="editor-panel"),
                     Panel(GridTable(id="results", zebra_stripes=False), title="[4] Results", id="results-panel"),
                     id="right"))

    def on_mount(self) -> None:
        p = self.query_one("#profiles", Table)
        p.add_column(" ", key="st", width=1)
        p.add_column("Name", key="name", width=14)
        p.add_column("Engine", key="engine", width=12)
        self.query_one("#tree", Table).add_column("object", key="o")
        self.reload_profiles()
        self.render_session()
        self.render_results()
        target = self.adhoc.name if self.adhoc else self.autoconnect
        if target:
            prof = next((p for p in self.profiles if p.name == target), None)
            if prof:
                self.call_after_refresh(self.connect_profile, prof)
            else:
                self.app.notify_err(f"No profile called {target}")
        elif last := self.state.get("last_profile"):
            names = [p.name for p in self.profiles]
            if last in names:
                self.query_one("#profiles", Table).move_cursor(row=names.index(last))

    def on_unmount(self) -> None:
        self.remember()
        if self.session:
            self.session.close()

    def remember(self) -> None:
        """Guarda el texto del editor del perfil conectado para la próxima vez."""
        if self.session and not self.adhoc_session:
            self.state.setdefault("editor", {})[self.session.profile.name] = self.editor_text
        C.save_state(self.state)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        # Copia del texto: al cerrar la app el editor ya no está montado
        self.editor_text = event.text_area.text

    @property
    def adhoc_session(self) -> bool:
        return bool(self.adhoc and self.session and self.session.profile is self.adhoc)

    @property
    def editor(self) -> TextArea:
        return self.query_one("#editor", TextArea)

    # ── foco y atajos ──

    def focused_id(self) -> str:
        f = self.app.focused
        return f.id if f is not None and f.id else ""

    def hints(self) -> list[tuple[str, str]]:
        fid = self.focused_id()
        if fid == "tree":
            return [("enter", "open"), ("l/h", "expand/collapse"), ("s", "select"), ("D", "DDL"), ("c", "count"),
                    ("/", "filter"), ("r", "refresh"), ("y", "copy name"), ("tab", "panel")]
        if fid == "editor":
            return [("F5", "run statement"), ("F9", "run all"), ("esc", "results"), ("tab", "panel")]
        if fid == "results":
            hints = [("enter", "view"), ("e", "edit"), ("a", "add row"), ("d", "delete row")]
            if self.pending:
                hints += [("ctrl+s", "save"), ("u", "undo")]
            hints += [("G", "more"), ("X", "export"), ("y/Y", "copy cell/row")]
            if self.session and self.session.in_tx:
                hints += [("C", "commit"), ("R", "rollback")]
            return hints + [("tab", "panel")]
        return [("enter", "connect"), ("n", "new"), ("e", "edit"), ("d", "delete"), ("x", "disconnect"),
                ("1-4", "panels"), ("?", "help"), ("q", "quit")]

    def action_focus_profiles(self) -> None:
        self.query_one("#profiles", Table).focus()

    def action_focus_tree(self) -> None:
        self.query_one("#tree", Table).focus()

    def action_focus_editor(self) -> None:
        self.editor.focus()

    def action_focus_results(self) -> None:
        self.query_one("#results", Table).focus()

    def action_back(self) -> None:
        if self.focused_id() == "editor":
            self.action_focus_results()
        else:
            self.action_quit_safe()

    def action_quit_safe(self) -> None:
        warn = self.unsaved_warning()
        if not warn:
            self.app.exit()
            return
        self.app.push_screen(ConfirmModal("Quit", f"{warn}\n\nQuit anyway?"), lambda yes: yes and self.app.exit())

    def unsaved_warning(self) -> str:
        out = []
        if self.pending:
            out.append(f"{self.pending} pending change{'' if self.pending == 1 else 's'} in the grid will be lost.")
        if self.session and self.session.in_tx:
            out.append(f"The open transaction ({self.session.tx_writes} statements) will be rolled back.")
        return "\n".join(out)

    # ── perfiles ──

    def reload_profiles(self, select: str | None = None) -> None:
        self.profiles = C.load_profiles()
        if self.adhoc:
            self.profiles.append(self.adhoc)
        live = self.session.profile.name if self.session and self.session.connected else ""
        rows = []
        for p in self.profiles:
            st = (Text("●", style=OK) if p.name == live else Text("…", style=WARN) if p.name == self.connecting
                  else Text("○", style=M))
            eng = Text(p.engine, style=M)
            if p.tunneled:
                eng.append(" " + ICONS["ssh"], style=WARN)
            rows.append((p.name, [st, Text(p.name, style="bold" if p.name == live else ""), eng]))
        t = self.query_one("#profiles", Table)
        t.set_rows(rows)
        if select and select in [r[0] for r in rows]:
            t.move_cursor(row=[r[0] for r in rows].index(select))
        self.query_one("#profiles-panel").border_subtitle = f" {len(rows)} " if rows else " press n "

    def saved_profiles(self) -> list[C.Profile]:
        return [p for p in self.profiles if p is not self.adhoc]

    def current_profile(self) -> Optional[C.Profile]:
        key = self.query_one("#profiles", Table).selected_key()
        return next((p for p in self.profiles if p.name == key), None)

    def profile_form(self, p: Optional[C.Profile]) -> None:
        editing = p is not None
        if editing and p is self.adhoc:
            self.app.notify_err("This profile comes from the command line: create a new one (n) to keep it")
            return
        p = p or C.Profile(name="")
        taken = {x.name for x in self.profiles} - ({p.name} if editing else set())

        def check(v: dict) -> Optional[str]:
            if not v["name"]:
                return "The name is missing"
            if v["name"] in taken:
                return f"There is already a profile called {v['name']}"
            if v["engine"] == "sqlite":
                return None if v["database"] else "SQLite needs the path to the database file"
            if not v["host"]:
                return "The host is missing"
            for k in ("port", "ssh_port"):
                if v[k] and not v[k].isdigit():
                    return "Ports must be numbers"
            return None

        def done(v: Optional[dict]) -> None:
            if not v:
                return
            new = C.Profile(name=v["name"], engine=v["engine"], host=v["host"], port=int(v["port"] or 0),
                            database=v["database"], user=v["user"], password=v["password"], ssl=v["ssl"],
                            mode=v["mode"], ssh_host=v["ssh_host"], ssh_user=v["ssh_user"],
                            ssh_port=int(v["ssh_port"] or 0), ssh_key=v["ssh_key"], timeout=p.timeout)
            if editing and p.password == "keyring":
                if new.password != "keyring":
                    C.secret_clear(p.name)
                elif new.name != p.name and (pw := C.secret_get(p.name)):
                    C.secret_set(new.name, pw)
                    C.secret_clear(p.name)
            saved = self.saved_profiles()
            C.save_profiles([new if x.name == p.name else x for x in saved] if editing else saved + [new])
            self.reload_profiles(select=new.name)
            if editing and self.session and self.session.profile.name == p.name:
                self.app.notify_ok("Saved: disconnect (x) and connect again to apply it")
            else:
                self.app.notify_ok(f"Profile {new.name} saved")

        fields = [
            Field("name", "Name", p.name, "crm-prod"),
            Field("engine", "Engine", p.engine, choices=ENGINE),
            Field("host", "Host", p.host, "db.example.com  (not used by SQLite)"),
            Field("port", "Port", str(p.port or ""), "empty = 5432 / 3306 / 1433"),
            Field("database", "Database", p.database, "database name  ·  path to the file for SQLite", files=True),
            Field("user", "User", p.user, "postgres  ·  app_reader  ·  DOMAIN\\user"),
            Field("password", "Password", p.password, choices=PASSWORD),
            Field("ssl", "TLS", p.ssl, choices=SSL),
            Field("mode", "Mode", p.mode, choices=MODE),
            Field("ssh_host", "SSH tunnel: host", p.ssh_host, "empty = direct connection  ·  bastion.example.com"),
            Field("ssh_user", "SSH tunnel: user", p.ssh_user, "empty = the one in ~/.ssh/config"),
            Field("ssh_port", "SSH tunnel: port", str(p.ssh_port or ""), "empty = 22"),
            Field("ssh_key", "SSH tunnel: identity file", p.ssh_key, "empty = ssh-agent / ~/.ssh/config", files=True),
        ]
        self.app.push_screen(FormModal("Edit profile" if editing else "New profile", fields, validate=check,
                                       hint="Passwords are never written to profiles.json. The tunnel uses your ssh "
                                            "(keys or agent, no password prompts)."), done)

    def connect_profile(self, p: C.Profile) -> None:
        if self.connecting:
            return
        if self.session and self.session.connected and self.session.profile.name == p.name:
            self.action_focus_tree()
            return

        def go() -> None:
            if not p.needs_password:
                self.do_connect(p, "")
            elif p.password == "keyring":
                self.keyring_connect(p)
            else:
                self.ask_password(p)

        if self.session and (warn := self.unsaved_warning()):
            self.app.push_screen(ConfirmModal("Switch connection", f"{warn}\n\nConnect to {p.name}?"),
                                 lambda yes: yes and go())
        else:
            go()

    def ask_password(self, p: C.Profile, store: bool = False, error: str = "") -> None:
        def done(v: Optional[dict]) -> None:
            if v:
                self.do_connect(p, v["password"], store=store)

        hint = f"✗ {error}" if error else ("It will be stored in the keyring." if store else "")
        self.app.push_screen(FormModal(f"Log in · {p.name}",
                                       [Field("password", f"Password for {p.user or 'the default user'}",
                                              password=True)], hint=hint), done)

    @work(thread=True, group="secret")
    def keyring_connect(self, p: C.Profile) -> None:
        pw = C.secret_get(p.name)
        if pw is None:
            self.app.call_from_thread(self.ask_password, p, True)
        else:
            self.app.call_from_thread(self.do_connect, p, pw, False, True)

    def do_connect(self, p: C.Profile, pw: str, store: bool = False, from_keyring: bool = False) -> None:
        self.remember()
        self.connecting = p.name
        self.reload_profiles()
        self.render_session(f"connecting to {p.url}{' through ' + p.ssh_target if p.tunneled else ''}…")
        self.connect_worker(p, pw, store, from_keyring)

    @work(thread=True, group="db")
    def connect_worker(self, p: C.Profile, pw: str, store: bool, from_keyring: bool) -> None:
        old, s = self.session, C.Session(p, pw)
        if old:
            old.close()
        try:
            s.connect()
            schemas = s.children(None)
            # Abre el esquema por defecto y sus tablas, como DBeaver
            preload = []
            first = next((o for o in schemas if o.name == s.schema), schemas[0] if schemas else None)
            if first:
                folders = s.children(first)
                preload.append((first.key, folders))
                if folders and folders[0].folder == "tables":
                    preload.append((folders[0].key, s.children(folders[0])))
        except DbError as e:
            self.app.call_from_thread(self.connect_failed, p, str(e), from_keyring)
            return
        if store:
            C.secret_set(p.name, pw)
        self.app.call_from_thread(self.connected, s, schemas, preload)

    def connect_failed(self, p: C.Profile, msg: str, from_keyring: bool) -> None:
        self.connecting = ""
        self.session = None
        self.reload_profiles()
        self.clear_tree()
        low = msg.lower()
        if from_keyring and ("password" in low or "authentication" in low or "login failed" in low):
            self.ask_password(p, store=True, error="The stored password was rejected")
            return
        self.render_session()
        self.app.notify_err(f"{p.name}: {msg}")

    def connected(self, s: C.Session, schemas: list[C.Obj], preload: list) -> None:
        self.connecting = ""
        self.session = s
        self.clear_results()
        self.reload_profiles()
        self.clear_tree()
        for o in schemas:
            self.nodes[o.key] = Node(o)
            self.roots.append(o.key)
        for key, children in preload:
            self.set_children(key, children)
            self.nodes[key].expanded = True
        self.render_tree()
        self.render_session()
        tree = self.query_one("#tree", Table)
        tree.focus()
        tree.first_selectable()
        if not self.adhoc_session:
            self.editor.load_text(self.state.get("editor", {}).get(s.profile.name, ""))
            self.state["last_profile"] = s.profile.name
            C.save_state(self.state)
        self.app.notify_ok(f"Connected to {s.profile.name}")

    def action_disconnect(self) -> None:
        if not self.session:
            return

        def go() -> None:
            self.remember()
            s, self.session = self.session, None
            self.close_worker(s)
            self.clear_tree()
            self.clear_results()
            self.reload_profiles()
            self.render_session()
            self.action_focus_profiles()

        if warn := self.unsaved_warning():
            self.app.push_screen(ConfirmModal("Disconnect", f"{warn}\n\nDisconnect?"), lambda yes: yes and go())
        else:
            go()

    @work(thread=True, group="close")
    def close_worker(self, s: C.Session) -> None:
        s.close()

    # ── sesión ──

    def render_session(self, busy: str = "") -> None:
        t = Text()

        def row(label: str, value: str | Text, style: str = "") -> None:
            if t:
                t.append("\n")
            t.append(f"{label:<12}", style=f"bold {M}")
            t.append(value, style=style) if isinstance(value, str) else t.append_text(value)

        s = self.session
        busy = busy or self.busy
        if busy and not (s and s.connected):
            row("Status", busy, WARN)
        elif not s or not s.connected:
            row("Status", "○ not connected", M)
            row("", "pick a profile and press enter" if self.profiles else "press n to create a profile", M)
        else:
            p = s.profile
            server = Text.assemble((p.url, ""), ("  ● connected", OK))
            if p.tunneled:
                server.append(f"  via ssh {p.ssh_target}", style=M)
            row("Server", server)
            mode = Text("read-only" if p.readonly else "read-write", style=OK if p.readonly else WARN)
            mode.append(f"   {ENGINES.get(p.engine, p.engine)} · default schema {s.schema or '-'}", style=M)
            row("Mode", mode)
            if s.in_tx:
                row("Transaction", Text.assemble((f"● open · {s.tx_writes} statement"
                                                  f"{'' if s.tx_writes == 1 else 's'}", WARN),
                                                 ("   C commit · R rollback", M)))
            else:
                row("Transaction", "auto-commit", M)
            if busy:
                row("Running", busy, WARN)
        self.query_one("#session", Static).update(t)

    # ── árbol ──

    def clear_tree(self) -> None:
        self.nodes, self.roots = {}, []
        self.render_tree()

    def set_children(self, key: str, children: list[C.Obj]) -> None:
        node = self.nodes[key]
        node.children, node.loading = [], False
        for o in children:
            old = self.nodes.get(o.key)
            if old is not None:   # refresco: conserva lo expandido
                old.obj = o
            else:
                self.nodes[o.key] = Node(o, node.depth + 1)
            node.children.append(o.key)

    def visible(self, n: Node) -> bool:
        """Con un filtro, solo las tablas / vistas / rutinas que lo contienen."""
        return not self.tree_filter or n.obj.kind not in ("table", "view", "routine") \
            or self.tree_filter in n.obj.name.lower()

    def render_tree(self) -> None:
        rows: list[tuple[str, list]] = []

        def walk(key: str) -> None:
            n = self.nodes[key]
            o = n.obj
            if not self.visible(n):
                return
            if n.loading:
                marker = Text("… ", style=WARN)
            elif o.leaf or (n.children is not None and not n.children):
                marker = Text("  ")
            elif n.expanded:
                marker = Text("▾ ", style=M)
            else:
                marker = Text("▸ ", style=M)
            label = Text("  " * n.depth)
            label.append_text(marker)
            icon = ICONS["pk"] if o.kind == "column" and "  PK" in o.detail else ICONS.get(o.kind, "")
            label.append(icon + " ", style=P if o.kind == "schema" else M)
            label.append(o.name, style="bold" if o.kind == "schema" else "")
            if o.kind == "folder" and n.children is not None:
                shown = [c for c in n.children if self.visible(self.nodes[c])]
                label.append(f" {len(shown)}", style=M)
            elif o.kind in ("column", "index", "fk") and o.detail:
                label.append("  " + o.detail[:80], style=M)
            rows.append((key, [label]))
            if n.expanded and n.children is not None:
                for c in n.children:
                    walk(c)

        for r in self.roots:
            walk(r)
        self.query_one("#tree", Table).set_rows(rows)
        sub = f" filter: {self.tree_filter} · " if self.tree_filter else " "
        self.query_one("#tree-panel").border_subtitle = f"{sub}{len(rows)} shown " if rows else sub.rstrip(" ·")

    def tree_node(self) -> Optional[Node]:
        k = self.query_one("#tree", Table).selected_key()
        return self.nodes.get(k) if k else None

    def toggle(self, key: str, expand: Optional[bool] = None) -> None:
        n = self.nodes[key]
        if n.obj.leaf:
            return
        want = not n.expanded if expand is None else expand
        if want and n.children is None:
            if not n.loading:
                n.loading = True
                self.render_tree()
                self.load_children(key)
            return
        n.expanded = want
        self.render_tree()

    @work(thread=True, group="tree")
    def load_children(self, key: str) -> None:
        s, n = self.session, self.nodes.get(key)
        if not s or not n:
            return
        try:
            children = s.children(n.obj)
        except DbError as e:
            self.app.call_from_thread(self.children_failed, key, str(e))
            return
        self.app.call_from_thread(self.children_loaded, s, key, children)

    def children_failed(self, key: str, msg: str) -> None:
        if key in self.nodes:
            self.nodes[key].loading = False
        self.render_tree()
        self.app.notify_err(msg)

    def children_loaded(self, s: C.Session, key: str, children: list[C.Obj]) -> None:
        if s is not self.session or key not in self.nodes:
            return
        self.set_children(key, children)
        self.nodes[key].expanded = True
        self.render_tree()

    def action_expand(self) -> None:
        if not (n := self.tree_node()):
            return
        if n.expanded and n.children:   # ya abierto: baja al primer hijo
            self.query_one("#tree", Table).action_cursor_down()
        else:
            self.toggle(n.obj.key, True)

    def action_collapse(self) -> None:
        if not (n := self.tree_node()):
            return
        if n.expanded:
            self.toggle(n.obj.key, False)
            return
        parent = next((k for k, x in self.nodes.items() if x.children and n.obj.key in x.children), None)
        if parent:   # cerrado: sube al padre
            t = self.query_one("#tree", Table)
            keys = [r.key.value for r in t.ordered_rows]
            if parent in keys:
                t.move_cursor(row=keys.index(parent))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        tid = event.data_table.id
        if tid == "profiles" and (p := self.current_profile()):
            self.connect_profile(p)
        elif tid == "tree" and (n := self.tree_node()):
            if n.obj.kind in ("table", "view"):
                self.open_table(n.obj)
            elif n.obj.kind == "routine":
                self.action_ddl()
            else:
                self.toggle(n.obj.key)

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        if event.data_table.id == "results":
            self.view_cell()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id in ("profiles", "tree"):
            self.update_hints()

    def action_filter(self) -> None:
        if not self.session:
            return

        def done(v: Optional[dict]) -> None:
            if v is not None:
                self.tree_filter = v["text"].lower()
                self.render_tree()
                self.query_one("#tree", Table).first_selectable()

        self.app.push_screen(FormModal("Filter objects", [Field("text", "Name contains", self.tree_filter)],
                                       hint="Filters the tables, views and routines already loaded. "
                                            "Empty clears it."), done)

    # ── resultados ──

    @property
    def pending(self) -> int:
        return sum(len(c) for r, c in self.edits.items() if r not in self.deleted) + len(self.deleted) \
            + len(self.inserted)

    def clear_pending(self) -> None:
        self.edits, self.deleted, self.inserted = {}, set(), []

    def clear_results(self) -> None:
        self.result, self.source = None, ()
        self.clear_pending()
        self.render_results()

    def guard_pending(self, then) -> None:
        """Antes de reemplazar los resultados: si hay cambios sin guardar, pregunta."""
        if not self.pending:
            then()
            return
        self.app.push_screen(ConfirmModal("Discard changes",
                                          f"{self.pending} pending change{'' if self.pending == 1 else 's'} "
                                          "will be lost. Go on?"), lambda yes: yes and then())

    def set_busy(self, text: str) -> None:
        self.busy = text
        self.render_session()

    def show_result(self, s: C.Session, res: Result, source: tuple, focus: bool = True) -> None:
        if s is not self.session:
            return
        self.busy = ""
        self.result, self.source = res, source
        self.clear_pending()
        self.render_results(reset=True)
        self.render_session()
        if focus and res.columns:
            self.action_focus_results()

    def query_failed(self, s: C.Session, msg: str) -> None:
        self.busy = ""
        self.render_session()
        if s is self.session:
            self.app.notify_err(msg)

    def render_results(self, reset: bool = False) -> None:
        t = self.query_one("#results", Table)
        panel = self.query_one("#results-panel")
        coord = t.cursor_coordinate
        t.clear(columns=True)
        res = self.result
        if res is None:
            panel.border_title = " [4] Results "
            panel.border_subtitle = ""
            return
        name = ".".join(x for x in res.table if x) if res.table else ""
        if self.source and self.source[0] == "table":
            name = f"{self.source[1]}.{self.source[2]}"
        panel.border_title = f" [4] {name} " if name else " [4] Results "
        if not res.columns:
            t.add_column("Result", key="msg")
            t.add_row(Text(f"✓ {res.message}", style=OK), key="gap:msg")
            panel.border_subtitle = f" {res.elapsed * 1000:.0f} ms "
            return
        pk = set(res.pk) if res.table else set()
        t.add_column(" ", key="#", width=1)
        for i, c in enumerate(res.columns):
            t.add_column(Text(c + (" " + ICONS["pk"] if c in pk else ""), style=f"bold {P}" if c in pk else "bold"),
                         key=f"c{i}")
        for i, row in enumerate(res.rows):
            gone = i in self.deleted
            edits = self.edits.get(i, {})
            mark = Text("-", style=ERR) if gone else Text("*", style=WARN) if edits else Text(" ")
            cells = []
            for j, v in enumerate(row):
                if gone:
                    cells.append(cell(v, f"strike {ERR}"))
                elif j in edits:
                    cells.append(cell(edits[j], f"bold {WARN}"))
                else:
                    cells.append(cell(v))
            t.add_row(mark, *cells, key=f"r{i}")
        for k, new in enumerate(self.inserted):
            t.add_row(Text("+", style=OK), *[cell(new[c], OK) if c in new else Text("default", style=f"italic {M}")
                                             for c in res.columns], key=f"n{k}")
        if not reset and t.row_count:
            t.move_cursor(row=min(coord.row, t.row_count - 1), column=min(coord.column, len(res.columns)))
        elif t.row_count:
            t.move_cursor(row=0, column=1)
        parts = [f"{len(res.rows)}{'+' if res.more else ''} rows"]
        if res.more:
            parts.append("G more")
        parts.append(f"{res.elapsed * 1000:.0f} ms")
        if self.session and not self.session.profile.readonly and res.editable:
            parts.append("editable")
        if self.pending:
            parts.append(f"{self.pending} pending · ctrl+s save · u undo")
        panel.border_subtitle = f" {' · '.join(parts)} "
        self.update_hints()

    def grid_pos(self) -> Optional[tuple[str, int, int]]:
        """("r", fila, columna) o ("n", fila agregada, columna); columna -1 = la marca."""
        res = self.result
        t = self.query_one("#results", Table)
        if not res or not res.columns or not t.row_count:
            return None
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        if not key or key.startswith("gap:"):
            return None
        return key[0], int(key[1:]), t.cursor_coordinate.column - 1

    def value_at(self, kind: str, i: int, col: int) -> Any:
        res = self.result
        if kind == "n":
            return self.inserted[i].get(res.columns[col])
        return self.edits.get(i, {}).get(col, res.rows[i][col])

    def view_cell(self) -> None:
        pos = self.grid_pos()
        if not pos or pos[2] < 0:
            return
        kind, i, col = pos
        v = self.value_at(kind, i, col)
        text = "NULL" if v is None else C.cell_text(v)
        if isinstance(v, (bytes, bytearray, memoryview)):
            text = f"{len(bytes(v))} bytes\n\n" + bytes(v)[:4096].hex(" ", 2)
        self.app.push_screen(TextModal(f"{self.result.columns[col]} · row {i + 1}", Text(text), end=False))

    def can_edit(self) -> bool:
        s, res = self.session, self.result
        if not s or not s.connected or not res or not res.columns:
            return False
        if s.profile.readonly:
            self.app.notify_err("Read-only profile: edit it (e in Profiles) and set Mode to read-write")
            return False
        if not res.table:
            self.app.notify_err("Only rows from a single table can be edited: open it from the tree, or "
                                "SELECT plain columns FROM one table")
            return False
        if not res.pk:
            self.app.notify_err(f"{res.table[1]} has no primary key: its rows can't be told apart")
            return False
        if not res.editable:
            self.app.notify_err(f"Select the primary key too ({', '.join(res.pk)}) to edit these rows")
            return False
        return True

    def edit_cell(self) -> None:
        pos = self.grid_pos()
        if not pos or not self.can_edit():
            return
        kind, i, col = pos
        if col < 0:
            return
        res = self.result
        name = res.columns[col]
        info = next((c for c in res.info if c.name == name), None)
        cur = self.value_at(kind, i, col)
        if isinstance(cur, (bytes, bytearray, memoryview)):
            self.app.notify_err("Binary value: it can't be edited here")
            return

        def done(v: Optional[dict]) -> None:
            if v is None:
                return
            new = None if v["value"] == C.NULL_INPUT else v["value"]
            if kind == "n":
                self.inserted[i][name] = new
            else:
                orig = res.rows[i][col]
                row = self.edits.setdefault(i, {})
                if (new is None and orig is None) or (new is not None and orig is not None
                                                      and new == C.cell_text(orig)):
                    row.pop(col, None)
                else:
                    row[col] = new
                if not row:
                    self.edits.pop(i, None)
            self.render_results()

        hint = f"{info.type if info else ''}  ·  \\N = NULL  ·  saved with ctrl+s".strip(" ·")
        self.app.push_screen(FormModal(f"Edit {name} · row {i + 1}",
                                       [Field("value", name, C.NULL_INPUT if cur is None else C.cell_text(cur))],
                                       hint=hint), done)

    def add_row(self) -> None:
        if not self.can_edit():
            return
        res = self.result
        cols = [c for c in res.info if c.name in res.columns]

        def label(c) -> str:
            return f"{c.name}  ({c.type})" + ("" if c.optional else "  required")

        def placeholder(c) -> str:
            if c.auto:
                return "empty = automatic"
            if c.default is not None:
                return f"empty = default ({c.default})"
            return "empty = NULL" if c.nullable else ""

        fields = [Field(f"c{k}", label(c), placeholder=placeholder(c)) for k, c in enumerate(cols)]

        def check(v: dict) -> Optional[str]:
            missing = [c.name for k, c in enumerate(cols) if not c.optional and not v[f"c{k}"]]
            return f"Missing: {', '.join(missing)}" if missing else None

        def done(v: Optional[dict]) -> None:
            if not v:
                return
            row = {c.name: (None if v[f"c{k}"] == C.NULL_INPUT else v[f"c{k}"])
                   for k, c in enumerate(cols) if v[f"c{k}"] != ""}
            self.inserted.append(row)
            self.render_results()
            t = self.query_one("#results", Table)
            t.move_cursor(row=t.row_count - 1)

        self.app.push_screen(FormModal(f"New row · {res.table[1]}", fields, validate=check,
                                       hint="\\N = NULL. The row is saved with ctrl+s."), done)

    def delete_row(self) -> None:
        pos = self.grid_pos()
        if not pos or not self.can_edit():
            return
        kind, i, _ = pos
        if kind == "n":
            del self.inserted[i]
        elif i in self.deleted:
            self.deleted.discard(i)
        else:
            self.deleted.add(i)
        self.render_results()

    def action_undo(self) -> None:
        if self.pending:
            n = self.pending
            self.clear_pending()
            self.render_results()
            self.app.notify_ok(f"{n} pending change{'' if n == 1 else 's'} discarded")

    def action_save(self) -> None:
        if not self.pending or not self.session or not self.result:
            return
        s = self.session
        try:
            stmts = C.change_statements(s.driver, self.result, self.edits, self.deleted, self.inserted)
        except DbError as e:
            self.app.notify_err(str(e))
            return
        shown = [C.render_sql(sql, params, s.driver.PARAM) + ";" for sql, params in stmts]
        preview = "\n".join(shown[:25]) + (f"\n… and {len(shown) - 25} more" if len(shown) > 25 else "")
        self.app.push_screen(ConfirmModal("Save changes", f"Run {len(stmts)} statement{'' if len(stmts) == 1 else 's'}"
                                                          f" in one transaction?\n\n{preview}"),
                             lambda yes: yes and self.apply_worker(s, stmts))

    @work(thread=True, group="db")
    def apply_worker(self, s: C.Session, stmts: list) -> None:
        try:
            n = s.apply(stmts)
        except DbError as e:
            self.app.call_from_thread(self.app.notify_err, f"Nothing saved: {e}")
            return
        self.app.call_from_thread(self.applied, s, n)

    def applied(self, s: C.Session, n: int) -> None:
        if s is not self.session:
            return
        self.clear_pending()
        self.app.notify_ok(f"Saved: {n} statement{'' if n == 1 else 's'} committed")
        self.rerun()

    def rerun(self) -> None:
        """Vuelve a leer los resultados actuales (después de guardar o con r)."""
        if not self.source or not self.session:
            return
        if self.source[0] == "table":
            _, schema, name, kind = self.source
            self.table_worker(self.session, schema, name, kind, self.result.limit if self.result else C.TABLE_LIMIT)
        elif C.is_readonly(self.source[1]):
            self.sql_worker(self.session, [self.source[1]], False)
        else:
            self.app.notify_err("The last statement modified data: it isn't run again")

    def action_more(self) -> None:
        res, s = self.result, self.session
        if not res or not s or not res.more:
            return
        if self.pending:
            self.app.notify_err("Save (ctrl+s) or undo (u) the pending changes first")
            return
        if self.source and self.source[0] == "table":
            _, schema, name, kind = self.source
            self.table_worker(s, schema, name, kind, res.limit + C.TABLE_LIMIT, False)
        else:
            self.more_worker(s, res)

    @work(thread=True, group="db")
    def more_worker(self, s: C.Session, res: Result) -> None:
        try:
            n = s.more(res)
        except DbError as e:
            self.app.call_from_thread(self.app.notify_err, str(e))
            return
        self.app.call_from_thread(self.more_loaded, res, n)

    def more_loaded(self, res: Result, n: int) -> None:
        if res is self.result:
            self.render_results()
            self.app.notify_ok(f"{n} more rows" if n else "No more rows")

    # ── abrir tablas y correr SQL ──

    def open_table(self, o: C.Obj) -> None:
        if not self.session:
            return
        self.guard_pending(lambda: self.table_worker(self.session, o.schema, o.name, o.kind, C.TABLE_LIMIT))

    @work(thread=True, group="db", exclusive=True)
    def table_worker(self, s: C.Session, schema: str, name: str, kind: str, limit: int, focus: bool = True) -> None:
        self.app.call_from_thread(self.set_busy, f"reading {schema}.{name}…")
        try:
            res = s.open_table(schema, name, kind, limit)
        except DbError as e:
            self.app.call_from_thread(self.query_failed, s, str(e))
            return
        self.app.call_from_thread(self.show_result, s, res, ("table", schema, name, kind), focus)

    def editor_sql(self, whole: bool) -> list[str]:
        ed = self.editor
        text = ed.selected_text or ed.text
        if whole or ed.selected_text:
            return [sql for _, _, sql in C.split_statements(text)] if whole else [text.strip()]
        row, col = ed.cursor_location
        lines = ed.text.split("\n")
        offset = sum(len(line) + 1 for line in lines[:row]) + col
        sql = C.statement_at(ed.text, offset)
        return [sql] if sql else []

    def action_run_statement(self) -> None:
        self.run_editor(False)

    def action_run_script(self) -> None:
        self.run_editor(True)

    def run_editor(self, whole: bool) -> None:
        if not self.session or not self.session.connected:
            self.app.notify_err("Not connected")
            return
        stmts = [s for s in self.editor_sql(whole) if s]
        if not stmts:
            self.app.notify_err("The editor is empty")
            return
        self.guard_pending(lambda: self.sql_worker(self.session, stmts, True))

    @work(thread=True, group="db", exclusive=True)
    def sql_worker(self, s: C.Session, stmts: list[str], remember: bool) -> None:
        last: Optional[Result] = None
        last_sql = ""
        done = 0
        for k, sql in enumerate(stmts):
            self.app.call_from_thread(self.set_busy, f"{k + 1}/{len(stmts)} · {C.oneline(sql, 60)}"
                                      if len(stmts) > 1 else C.oneline(sql, 70))
            if remember and not self.adhoc_session:
                C.add_history(self.state, s.profile.name, sql)
            try:
                res = s.run(sql)
            except DbError as e:
                where = f" (statement {k + 1} of {len(stmts)})" if len(stmts) > 1 else ""
                if last is not None:
                    self.app.call_from_thread(self.show_result, s, last, ("query", last_sql), False)
                self.app.call_from_thread(self.query_failed, s, f"{e}{where}")
                return
            done += 1
            # La grilla muestra el último resultado con filas (o el último mensaje)
            if res.columns or last is None or not last.columns:
                last, last_sql = res, sql
        if len(stmts) > 1 and last is not None and not last.columns:
            last.message = f"{done} statements · last: {last.message}"
        if remember:
            C.save_state(self.state)
        self.app.call_from_thread(self.show_result, s, last, ("query", last_sql), True)

    def action_select(self) -> None:
        n = self.tree_node() if self.focused_id() == "tree" else None
        if not n or not self.session or n.obj.kind not in ("table", "view", "column"):
            return
        d = self.session.driver
        table = n.obj.table if n.obj.kind == "column" else n.obj.name
        sql = d.select_sql(n.obj.schema, table, 100)
        if n.obj.kind == "column":
            sql = sql.replace("*", d.quote(n.obj.name), 1)
        self.append_to_editor(sql)

    def append_to_editor(self, sql: str) -> None:
        ed = self.editor
        ed.text = (ed.text.rstrip() + "\n\n" if ed.text.strip() else "") + sql.rstrip().rstrip(";") + ";"
        ed.move_cursor((len(ed.text.split("\n")) - 1, 0))
        ed.focus()

    def action_history(self) -> None:
        if not self.session:
            return
        hist = self.state.get("history", {}).get(self.session.profile.name, [])
        if not hist:
            self.app.notify_err("No queries yet")
            return

        def pick(key: Optional[str]) -> None:
            if key is not None:
                self.append_to_editor(hist[int(key)])

        self.app.push_screen(PickModal(f"History · {self.session.profile.name}", ["Query"],
                                       [(str(i), [Text(C.oneline(q, 200))]) for i, q in enumerate(hist)],
                                       hint="enter put it in the editor · esc close"), pick)

    # ── transacciones ──

    def action_commit(self) -> None:
        self.tx("commit")

    def action_rollback(self) -> None:
        self.tx("rollback")

    def tx(self, op: str) -> None:
        s = self.session
        if not s or not s.in_tx:
            if s:
                self.app.notify_err("No open transaction")
            return
        self.tx_worker(s, op)

    @work(thread=True, group="db")
    def tx_worker(self, s: C.Session, op: str) -> None:
        try:
            res = s.transaction(op)
        except DbError as e:
            self.app.call_from_thread(self.app.notify_err, f"{op}: {e}")
            self.app.call_from_thread(self.render_session)
            return
        self.app.call_from_thread(self.app.notify_ok, res.message.capitalize())
        self.app.call_from_thread(self.render_session)
        self.app.call_from_thread(self.update_hints)

    # ── acciones según el panel ──

    def action_new(self) -> None:
        if self.focused_id() == "profiles":
            self.profile_form(None)

    def action_edit(self) -> None:
        fid = self.focused_id()
        if fid == "profiles" and (p := self.current_profile()):
            self.profile_form(p)
        elif fid == "results":
            self.edit_cell()

    def action_add(self) -> None:
        if self.focused_id() == "results":
            self.add_row()

    def action_delete(self) -> None:
        fid = self.focused_id()
        if fid == "results":
            self.delete_row()
        elif fid == "profiles" and (p := self.current_profile()):
            if p is self.adhoc:
                return

            def gone(yes: bool) -> None:
                if not yes:
                    return
                if self.session and self.session.profile.name == p.name:
                    s, self.session = self.session, None
                    self.close_worker(s)
                    self.clear_tree()
                    self.clear_results()
                if p.password == "keyring":
                    C.secret_clear(p.name)
                C.save_profiles([x for x in self.saved_profiles() if x.name != p.name])
                self.state.get("history", {}).pop(p.name, None)
                self.state.get("editor", {}).pop(p.name, None)
                C.save_state(self.state)
                self.reload_profiles()
                self.render_session()

            self.app.push_screen(ConfirmModal("Delete profile", f"Delete the profile {p.name}?\n\n"
                                                                "Its query history goes with it."), gone)

    def action_refresh(self) -> None:
        fid = self.focused_id()
        if fid == "profiles":
            self.reload_profiles()
        elif fid == "tree" and (n := self.tree_node()):
            if n.obj.leaf:
                parent = next((k for k, x in self.nodes.items() if x.children and n.obj.key in x.children), None)
                n = self.nodes.get(parent) if parent else None
            if n:
                n.loading = True
                self.render_tree()
                self.load_children(n.obj.key)
        elif fid == "results":
            self.guard_pending(self.rerun)

    def action_ddl(self) -> None:
        n = self.tree_node() if self.focused_id() == "tree" else None
        if not n or not self.session:
            return
        o = n.obj
        if o.kind in ("column", "index", "fk") or o.folder in ("columns", "indexes", "fks"):
            o = next((x.obj for x in self.nodes.values() if x.obj.kind == "table" and x.obj.name == o.table
                      and x.obj.schema == o.schema), o)
        if o.kind not in ("table", "view", "routine"):
            return
        self.ddl_worker(self.session, o)

    @work(thread=True, group="db")
    def ddl_worker(self, s: C.Session, o: C.Obj) -> None:
        try:
            text = s.ddl(o)
        except DbError as e:
            self.app.call_from_thread(self.app.notify_err, f"DDL: {e}")
            return
        self.app.call_from_thread(self.app.push_screen, TextModal(f"DDL · {o.schema}.{o.name}",
                                                                  Text(text or "(no definition)"), False))

    def action_count(self) -> None:
        n = self.tree_node() if self.focused_id() == "tree" else None
        if n and self.session and n.obj.kind in ("table", "view"):
            self.count_worker(self.session, n.obj)

    @work(thread=True, group="db")
    def count_worker(self, s: C.Session, o: C.Obj) -> None:
        try:
            n = s.count(o.schema, o.name)
        except DbError as e:
            self.app.call_from_thread(self.app.notify_err, str(e))
            return
        self.app.call_from_thread(self.app.notify_ok, f"{o.schema}.{o.name}: {n:,} rows")

    # ── export ──

    def action_export(self) -> None:
        res, s = self.result, self.session
        if not res or not res.columns or not s:
            return
        if self.source and self.source[0] == "table":
            base, full = self.source[2], s.driver.select_sql(self.source[1], self.source[2], C.MAX_EXPORT)
        else:
            base = res.table[1] if res.table else "query"
            full = self.source[1] if self.source and C.is_readonly(self.source[1]) else ""
        choices = [(f"Loaded rows ({len(res.rows)})", "loaded")]
        if full and res.more:
            choices.insert(0, (f"All rows (runs it again, up to {C.MAX_EXPORT:,})", "all"))
        fields = [Field("path", "File", str(Path.home() / f"lazydb-{slug(base)}-{datetime.now():%Y%m%d-%H%M}"),
                        files=True),
                  Field("format", "Format", "xlsx", choices=[("Excel (.xlsx)", "xlsx"), ("CSV (.csv)", "csv")]),
                  Field("rows", "Rows", choices[0][1], choices=choices)]

        def done(v: Optional[dict]) -> None:
            if not v:
                return
            ext = "." + v["format"]
            path = Path(v["path"]).expanduser()
            path = path if path.suffix.lower() == ext else path.with_name(path.name + ext)
            self.export_worker(s, res, full if v["rows"] == "all" else "", path, v["format"])

        self.app.push_screen(FormModal("Export results", fields, hint="The extension is added if it's missing.",
                                       validate=lambda v: None if v["path"] else "The file is missing"), done)

    @work(thread=True, group="export")
    def export_worker(self, s: C.Session, res: Result, sql: str, path: Path, fmt: str) -> None:
        try:
            cols, rows = s.fetch_all(sql) if sql else (res.columns, res.rows)
            data = [[C.cell_text(v) for v in r] for r in rows]
            path.parent.mkdir(parents=True, exist_ok=True)
            if fmt == "csv":
                with path.open("w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(cols)
                    w.writerows(data)
            else:
                p = s.profile
                info = [["Server", p.url], ["Engine", ENGINES.get(p.engine, p.engine)], ["User", p.user or "-"],
                        ["SQL", sql or res.sql], ["Rows", str(len(rows))],
                        ["Exported", datetime.now().strftime("%Y-%m-%d %H:%M:%S")]]
                write_xlsx(path, [("Results", cols, data), ("Query", ["Field", "Value"], info)])
        except (DbError, OSError) as e:
            self.app.call_from_thread(self.app.notify_err, f"Export: {e}")
            return
        self.app.call_from_thread(self.app.notify_ok, f"{len(rows):,} rows → {path}")

    # ── varios ──

    def action_copy(self) -> None:
        fid = self.focused_id()
        text = ""
        if fid == "results" and (pos := self.grid_pos()) and pos[2] >= 0:
            text = C.cell_text(self.value_at(*pos))
        elif fid == "tree" and (n := self.tree_node()) and self.session:
            o = n.obj
            text = self.session.driver.qualified(o.schema, o.name) if o.kind in ("table", "view") else o.name
        if text:
            self.app.notify_ok("Copied") if copy_text(text) else self.app.notify_err("Could not copy (wl-copy?)")

    def action_copy_row(self) -> None:
        pos = self.grid_pos() if self.focused_id() == "results" else None
        if not pos:
            return
        kind, i, _ = pos
        res = self.result
        text = "\t".join(C.cell_text(self.value_at(kind, i, j)) for j in range(len(res.columns)))
        self.app.notify_ok("Row copied") if copy_text(text) else self.app.notify_err("Could not copy (wl-copy?)")

    def action_help(self) -> None:
        t = Text()
        t.append("Keys (remappable in ~/.config/lazydb/keymap.json)\n\n", style=f"bold {M}")
        for b in bindings_of(type(self)):
            t.append(f"{key_display(b.key):<8}", style=f"bold {P}")
            t.append(f"{b.description or b.action:<26}", style="")
            t.append(f"{b.id}\n", style=M)
        t.append("\nenter   connect / open table / view cell\nl / h   expand / collapse (tree), "
                 "next / previous column (results)\ntab     next panel\n\n", style="")
        t.append("In the editor letters are text: use F5 / F9, esc to leave it and tab to change panels.\n"
                 "Editing a cell: \\N means NULL.\n", style=M)
        self.app.push_screen(TextModal("Help", t, end=False))

    def action_log(self) -> None:
        if not self.session:
            return
        t = Text()
        for when, op, target, res in self.session.log:
            t.append(time.strftime("%H:%M:%S ", time.localtime(when)), style=M)
            t.append(f"{op:<9}", style="bold")
            t.append(f"{res:<26} ", style=ERR if res == "error" else OK)
            t.append(target + "\n")
        self.app.push_screen(TextModal(f"Session log · {self.session.profile.name}", t or "Nothing yet"))


def LazyDbApp(autoconnect: str = "", adhoc: Optional[C.Profile] = None) -> ViewApp:
    return ViewApp(LazyDbView, title="lazydb", autoconnect=autoconnect, adhoc=adhoc)
