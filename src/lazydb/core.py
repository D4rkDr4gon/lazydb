"""
core.py — backend de lazydb (sin Textual): perfiles, contraseñas en el
keyring, túnel SSH, partir y clasificar SQL, la sesión contra la base y las
sentencias que aplican las ediciones de la grilla.

- Perfiles: ~/.config/lazydb/profiles.json (LAZYDB_CONF apunta a una
  copia). Nunca guarda contraseñas.
- Contraseñas: libsecret con `secret-tool` (atributos app=lazydb
  profile=<nombre>) si el perfil lo pide; si no, se piden al conectar.
- Túnel: `ssh -N -L` del sistema a un puerto local libre (hereda
  ~/.ssh/config, el agente, known_hosts y ProxyJump).
- Session: una conexión DB-API protegida con un lock, así los workers de la
  TUI (hilos) no la usan a la vez. Trabaja en autocommit y abre una
  transacción antes de la primera sentencia que modifica algo.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from . import drivers
from .drivers import PAGE, Column, DbError, Driver, Result

_XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
_XDG_STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
CONF = Path(os.environ.get("LAZYDB_CONF", _XDG_CONFIG / "lazydb" / "profiles.json"))
# Estado entre aperturas: último perfil, el texto del editor y el historial de consultas por perfil
STATE = Path(os.environ.get("LAZYDB_STATE", _XDG_STATE / "lazydb" / "state.json"))
TABLE_LIMIT = 1000         # filas al abrir una tabla desde el árbol (G suma otras tantas)
MAX_EXPORT = 1_000_000     # filas de un export (Excel / CSV)
MAX_HISTORY = 200          # consultas recordadas por perfil


# ── Perfiles ──────────────────────────────────────────────

@dataclass
class Profile:
    name: str
    engine: str = "postgres"       # postgres | mysql | mssql | sqlite
    host: str = ""
    port: int = 0                  # 0 = el del motor (5432 / 3306 / 1433)
    database: str = ""             # nombre de la base; en SQLite, la ruta del archivo
    user: str = ""
    password: str = "ask"          # ask | keyring | none
    ssl: str = "default"           # default | require | verify | disable
    mode: str = "readonly"         # readonly | readwrite
    ssh_host: str = ""             # túnel SSH (vacío = conexión directa)
    ssh_user: str = ""
    ssh_port: int = 0
    ssh_key: str = ""
    timeout: int = 10

    @property
    def real_port(self) -> int:
        return self.port or drivers.default_port(self.engine)

    @property
    def readonly(self) -> bool:
        return self.mode != "readwrite"

    @property
    def tunneled(self) -> bool:
        return bool(self.ssh_host) and self.engine != "sqlite"

    @property
    def needs_password(self) -> bool:
        return self.engine != "sqlite" and self.password != "none"

    @property
    def url(self) -> str:
        if self.engine == "sqlite":
            return f"sqlite:{self.database}"
        user = f"{self.user}@" if self.user else ""
        db = f"/{self.database}" if self.database else ""
        return f"{self.engine}://{user}{self.host}:{self.real_port}{db}"

    @property
    def ssh_target(self) -> str:
        target = f"{self.ssh_user}@{self.ssh_host}" if self.ssh_user else self.ssh_host
        return f"{target}:{self.ssh_port}" if self.ssh_port else target


def load_profiles() -> list[Profile]:
    try:
        data = json.loads(CONF.read_text())
    except (OSError, ValueError):
        return []
    known = Profile.__dataclass_fields__
    out = []
    for p in data.get("profiles", []) if isinstance(data, dict) else []:
        try:
            out.append(Profile(**{k: v for k, v in p.items() if k in known}))
        except TypeError:
            continue
    return out


def save_profiles(profiles: list[Profile]) -> None:
    CONF.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONF.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"profiles": [asdict(p) for p in profiles]}, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONF)


# ── Estado ────────────────────────────────────────────────

def load_state() -> dict:
    try:
        data = json.loads(STATE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
        os.chmod(tmp, 0o600)       # el historial puede tener datos
        os.replace(tmp, STATE)
    except OSError:
        pass


def add_history(state: dict, profile: str, sql: str) -> None:
    hist = state.setdefault("history", {}).setdefault(profile, [])
    sql = sql.strip()
    if sql in hist:
        hist.remove(sql)
    hist.insert(0, sql)
    del hist[MAX_HISTORY:]


# ── Keyring (libsecret) ───────────────────────────────────

def _secret(args: list[str], data: str | None = None) -> Optional[str]:
    try:
        r = subprocess.run(["secret-tool", *args], input=data, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def secret_get(name: str) -> Optional[str]:
    out = _secret(["lookup", "app", "lazydb", "profile", name])
    return out or None


def secret_set(name: str, password: str) -> bool:
    return _secret(["store", f"--label=lazydb {name}", "app", "lazydb", "profile", name], password) is not None


def secret_clear(name: str) -> None:
    _secret(["clear", "app", "lazydb", "profile", name])


# ── Túnel SSH ─────────────────────────────────────────────

_TUNNELS: set["Tunnel"] = set()


@atexit.register
def _close_tunnels() -> None:
    for t in list(_TUNNELS):
        t.close()


class Tunnel:
    """`ssh -N -L 127.0.0.1:<libre>:<host>:<puerto> bastión`. BatchMode: sin
    preguntas (clave, agente o ~/.ssh/config; un host desconocido falla)."""

    def __init__(self, p: Profile, host: str, port: int) -> None:
        self.p, self.host, self.port = p, host, port
        self.local = 0
        self.proc: Optional[subprocess.Popen] = None

    def command(self) -> list[str]:
        p = self.p
        cmd = ["ssh", "-N", "-L", f"127.0.0.1:{self.local}:{self.host}:{self.port}",
               "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30",
               "-o", f"ConnectTimeout={p.timeout}"]
        if p.ssh_port:
            cmd += ["-p", str(p.ssh_port)]
        if p.ssh_key:
            cmd += ["-i", str(Path(p.ssh_key).expanduser())]
        return cmd + [f"{p.ssh_user}@{p.ssh_host}" if p.ssh_user else p.ssh_host]

    def open(self) -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.local = s.getsockname()[1]
        err = tempfile.TemporaryFile()
        try:
            self.proc = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=err, start_new_session=True)
        except OSError as e:
            err.close()
            raise DbError(f"SSH tunnel: {e}") from None
        _TUNNELS.add(self)
        deadline = time.monotonic() + self.p.timeout + 5
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                err.seek(0)
                msg = err.read().decode(errors="replace").strip().splitlines()
                err.close()
                self.close()
                raise DbError(f"SSH tunnel to {self.p.ssh_target}: {msg[-1] if msg else 'ssh exited'}")
            try:
                socket.create_connection(("127.0.0.1", self.local), timeout=0.3).close()
                err.close()
                return self.local
            except OSError:
                time.sleep(0.1)
        err.close()
        self.close()
        raise DbError(f"SSH tunnel to {self.p.ssh_target}: timed out")

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        _TUNNELS.discard(self)


# ── SQL ───────────────────────────────────────────────────

READ_WORDS = {"select", "with", "show", "explain", "describe", "desc", "pragma", "values", "table"}
_DOLLAR = re.compile(r"\$[A-Za-z_]\w*\$|\$\$")
_GO = re.compile(r"[ \t]*go[ \t]*(?:\n|$)", re.I)


def split_statements(text: str) -> list[tuple[int, int, str]]:
    """Parte un texto en sentencias [(inicio, fin, sql)] por `;` (y líneas `GO`
    de SQL Server), sin cortar dentro de comillas, comentarios ni $$...$$."""
    out: list[tuple[int, int, str]] = []
    start = i = 0
    n = len(text)

    def push(end: int, nxt: int) -> None:
        nonlocal start
        chunk = text[start:end]
        if strip_comments(chunk).strip():
            lead = len(chunk) - len(chunk.lstrip())
            out.append((start + lead, end, chunk.strip()))
        start = nxt

    while i < n:
        c = text[i]
        if c in "'\"`":
            j = i + 1
            while j < n:
                if text[j] == c:
                    if j + 1 < n and text[j + 1] == c:   # '' escapado
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
        elif c == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c == "$" and (m := _DOLLAR.match(text, i)):
            j = text.find(m.group(), m.end())
            i = n if j < 0 else j + len(m.group())
        elif c == ";":
            push(i, i + 1)
            i += 1
        elif c == "\n" and (m := _GO.match(text, i + 1)):
            push(i, m.end())
            i = m.end()
        else:
            i += 1
    push(n, n)
    return out


def statement_at(text: str, offset: int) -> str:
    """La sentencia donde está el cursor (o la anterior, si está después del último `;`)."""
    prev = None
    for s, e, sql in split_statements(text):
        if s <= offset <= e + 1:            # adentro, o justo después de su `;`
            return sql
        if offset < s:                      # entre dos: la anterior si está en la misma línea
            return prev[2] if prev and "\n" not in text[prev[1]:offset] else sql
        prev = (s, e, sql)
    return prev[2] if prev else ""


_TX = re.compile(r"\s*(?:(begin|start\s+transaction)|(commit|end)|(rollback|abort))"
                 r"(?:\s+(?:work|transaction|tran))?\s*;?\s*", re.I)


def tx_command(sql: str) -> str:
    """begin / commit / rollback si la sentencia es solo eso (no BEGIN TRY ni ROLLBACK TO SAVEPOINT)."""
    m = _TX.fullmatch(strip_comments(sql))
    if not m:
        return ""
    return "begin" if m.group(1) else "commit" if m.group(2) else "rollback"


def strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


def first_word(sql: str) -> str:
    m = re.match(r"[\s(]*([A-Za-z]+)", strip_comments(sql))
    return m.group(1).lower() if m else ""


def is_readonly(sql: str) -> bool:
    """Las que no tocan datos ni esquema. EXPLAIN ANALYZE ejecuta la sentencia: no cuenta."""
    word = first_word(sql)
    if word == "explain" and re.search(r"\banaly[sz]e\b", sql, re.I):
        return False
    if word == "with":   # WITH ... DELETE / UPDATE / INSERT (Postgres)
        return not re.search(r"\b(insert|update|delete|merge)\b", strip_comments(sql), re.I)
    return word in READ_WORDS


_IDENT = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)'
_SIMPLE = re.compile(
    rf"^\s*select\s+(?:top\s*\(?\s*\d+\s*\)?\s+)?(?P<cols>.+?)\s+from\s+(?P<table>{_IDENT}(?:\s*\.\s*{_IDENT})?)"
    rf"(?:\s+(?:as\s+)?(?P<alias>{_IDENT}))?\s*(?P<rest>(?:where|order|limit|offset|fetch)\b.*)?;?\s*$",
    re.I | re.S)
_PLAIN_COL = re.compile(rf"^(?:{_IDENT}\s*\.\s*)?(?:{_IDENT}|\*)$")
_KEYWORDS = {"where", "order", "limit", "offset", "fetch", "group", "join", "left", "right", "inner", "cross"}


def unquote(ident: str) -> str:
    ident = ident.strip()
    if ident[:1] in "\"`[" and len(ident) > 1:
        return ident[1:-1]
    return ident


def simple_table(sql: str) -> Optional[tuple[str, str]]:
    """(esquema, tabla) si la consulta es un SELECT de columnas tal cual de una
    sola tabla (sin joins, agregados ni expresiones): sus filas se pueden editar."""
    text = strip_comments(sql).strip()
    m = _SIMPLE.match(text)
    if not m or re.search(r"\b(join|union|group\s+by|having|distinct|intersect|except)\b", text, re.I):
        return None
    if m.group("alias") and m.group("alias").lower() in _KEYWORDS:
        return None
    if not all(_PLAIN_COL.match(c.strip()) for c in m.group("cols").split(",")):
        return None
    parts = [unquote(p) for p in re.split(r"\s*\.\s*(?=[\"`\[A-Za-z_])", m.group("table"))]
    return (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])


def oneline(sql: str, width: int = 120) -> str:
    s = " ".join(sql.split())
    return s if len(s) <= width else s[:width - 1] + "…"


# ── Valores ───────────────────────────────────────────────

NULL_INPUT = r"\N"          # en un formulario de edición: NULL


def cell_text(v: Any) -> str:
    """Valor como texto completo (export, copiar, editar)."""
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "0x" + bytes(v).hex()
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, (date, dtime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return str(v)


def literal(v: Any) -> str:
    """Valor como literal SQL, solo para mostrar las sentencias antes de aplicarlas."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float, Decimal)):
        return cell_text(v)
    return "'" + cell_text(v).replace("'", "''") + "'"


def render_sql(sql: str, params: list, mark: str) -> str:
    out, it = [], iter(params)
    for i, part in enumerate(sql.split(mark)):
        if i:
            out.append(literal(next(it, None)))
        out.append(part)
    return "".join(out)


def change_statements(d: Driver, res: Result, edits: dict[int, dict[int, Any]], deleted: set[int],
                      inserted: list[dict[str, Any]]) -> list[tuple[str, list]]:
    """Las ediciones pendientes de la grilla como sentencias con parámetros:
    primero los DELETE, después los UPDATE y al final los INSERT. Cada fila
    se identifica por los valores originales de su clave primaria."""
    if not res.editable:
        raise DbError("These results can't be edited")
    qt = d.qualified(*res.table)
    ph = d.PARAM
    pk = [res.columns.index(c) for c in res.pk]

    def where(row: list) -> tuple[str, list]:
        parts, params = [], []
        for i in pk:
            col = d.quote(res.columns[i])
            if row[i] is None:
                parts.append(f"{col} IS NULL")
            else:
                parts.append(f"{col} = {ph}")
                params.append(row[i])
        return " AND ".join(parts), params

    out: list[tuple[str, list]] = []
    for r in sorted(deleted):
        w, params = where(res.rows[r])
        out.append((f"DELETE FROM {qt} WHERE {w}", params))
    for r in sorted(edits):
        if r in deleted or not edits[r]:
            continue
        cells = sorted(edits[r].items())
        sets = ", ".join(f"{d.quote(res.columns[c])} = {ph}" for c, _ in cells)
        w, params = where(res.rows[r])
        out.append((f"UPDATE {qt} SET {sets} WHERE {w}", [v for _, v in cells] + params))
    for row in inserted:
        if row:
            cols = ", ".join(d.quote(c) for c in row)
            out.append((f"INSERT INTO {qt} ({cols}) VALUES ({', '.join([ph] * len(row))})", list(row.values())))
        else:
            out.append((d.insert_default_sql(qt), []))
    return out


# ── Árbol del catálogo ────────────────────────────────────

@dataclass
class Obj:
    kind: str                  # schema | folder | table | view | routine | column | index | fk
    name: str
    schema: str = ""
    table: str = ""            # la tabla de una carpeta / columna / índice
    folder: str = ""           # carpeta: tables | views | routines | columns | indexes | fks
    detail: str = ""           # tipo de columna, definición de índice, oid de función...
    leaf: bool = False

    @property
    def key(self) -> str:
        return "\x1f".join((self.kind, self.schema, self.table, self.folder, self.name))


FOLDERS = {"tables": "Tables", "views": "Views", "routines": "Routines",
           "columns": "Columns", "indexes": "Indexes", "fks": "Foreign keys"}


# ── Sesión ────────────────────────────────────────────────

def _err(e: Exception) -> DbError:
    if isinstance(e, DbError):
        return e
    msg = str(e).strip() or type(e).__name__
    # pymssql: (código, b'mensaje...')
    if isinstance(getattr(e, "args", None), tuple) and len(e.args) == 2 and isinstance(e.args[1], bytes):
        msg = e.args[1].decode(errors="replace").split("DB-Lib error")[0].strip()
    return DbError(msg)


class Session:
    """Conexión a un perfil. Todos los métodos bloquean: llamarlos desde un worker."""

    def __init__(self, profile: Profile, password: str = "") -> None:
        self.profile, self.password = profile, password
        self.driver: Optional[Driver] = None
        self.tunnel: Optional[Tunnel] = None
        self.lock = threading.RLock()
        self.log: list[tuple[float, str, str, str]] = []   # (hora, operación, objetivo, resultado)
        self.in_tx = False
        self.tx_writes = 0           # sentencias que modificaron algo en la transacción abierta
        self.schema = ""             # el esquema por defecto de la conexión

    # Separado para que las pruebas lo cambien
    def make_driver(self) -> Driver:
        return drivers.make(self.profile.engine)

    def _log(self, op: str, target: str, result: str) -> None:
        self.log.append((time.time(), op, target, result))
        del self.log[:-500]

    def connect(self) -> None:
        with self.lock:
            p = self.profile
            host, port = p.host, p.real_port
            try:
                if p.tunneled:
                    self.tunnel = Tunnel(p, host, port)
                    port = self.tunnel.open()
                    host = "127.0.0.1"
                    self._log("ssh", p.ssh_target, f"tunnel → 127.0.0.1:{port}")
                d = self.make_driver()
                d.connect(p, host, port, self.password)
                if p.readonly:
                    d.set_readonly()
                self.schema = d.default_schema()
            except Exception as e:  # noqa: BLE001 - errores de cada driver, socket, ssh
                self._log("connect", p.url, "error")
                if self.tunnel:
                    self.tunnel.close()
                    self.tunnel = None
                raise _err(e) from None
            self.driver = d
            self._log("connect", p.url, "success")

    def close(self) -> None:
        with self.lock:
            if self.driver is not None:
                if self.in_tx:
                    self._log("rollback", "on disconnect", f"{self.tx_writes} statements")
                self.driver.close()
            self.driver = None
            self.in_tx = False
            if self.tunnel:
                self.tunnel.close()
                self.tunnel = None
                self._log("ssh", self.profile.ssh_target, "tunnel closed")

    @property
    def connected(self) -> bool:
        return self.driver is not None and self.driver.conn is not None

    def _call(self, fn, *args):
        with self.lock:
            if not self.driver:
                raise DbError("Not connected")
            try:
                return fn(*args)
            except Exception as e:  # noqa: BLE001
                raise _err(e) from None

    # ── catálogo ──

    def children(self, obj: Optional[Obj]) -> list[Obj]:
        d = self.driver
        if obj is None:
            return [Obj("schema", s, schema=s) for s in self._call(d.schemas)]
        s, t = obj.schema, obj.table
        if obj.kind == "schema":
            names = ["tables", "views"] + (["routines"] if d.HAS_ROUTINES else [])
            return [Obj("folder", FOLDERS[f], schema=s, folder=f) for f in names]
        if obj.kind in ("table", "view"):
            names = ["columns"] + (["indexes", "fks"] if obj.kind == "table" else [])
            return [Obj("folder", FOLDERS[f], schema=s, table=obj.name, folder=f) for f in names]
        if obj.kind != "folder":
            return []
        f = obj.folder
        if f in ("tables", "views"):
            kind = f[:-1]
            return [Obj(kind, n, schema=s) for n in self._call(d.relations, s, kind)]
        if f == "routines":
            return [Obj("routine", n, schema=s, detail=str(x), leaf=True) for n, x in self._call(d.routines, s)]
        if f == "columns":
            return [Obj("column", c.name, schema=s, table=t, detail=column_detail(c), leaf=True)
                    for c in self._call(d.columns, s, t)]
        if f == "indexes":
            return [Obj("index", n, schema=s, table=t, detail=x, leaf=True) for n, x in self._call(d.indexes, s, t)]
        if f == "fks":
            return [Obj("fk", n, schema=s, table=t, detail=x, leaf=True) for n, x in self._call(d.foreign_keys, s, t)]
        return []

    def ddl(self, obj: Obj) -> str:
        return self._call(self.driver.ddl, obj.schema, obj.name, obj.kind, obj.detail)

    def count(self, schema: str, table: str) -> int:
        return int(self._call(self.driver.query, f"SELECT COUNT(*) FROM {self.driver.qualified(schema, table)}")[0][0])

    def columns(self, schema: str, table: str) -> list[Column]:
        return self._call(self.driver.columns, schema, table)

    # ── consultas ──

    def open_table(self, schema: str, table: str, kind: str = "table", limit: int = TABLE_LIMIT) -> Result:
        sql = self.driver.select_sql(schema, table, limit)
        res = self._run(sql, page=limit)
        res.limit = limit
        res.more = len(res.rows) >= limit
        if res.cursor is not None:
            res.cursor.close()
            res.cursor = None
        if kind == "table":
            self._editable(res, schema, table)
        return res

    def run(self, sql: str) -> Result:
        if op := tx_command(sql):
            return self.transaction(op)
        write = not is_readonly(sql)
        if write and self.profile.readonly:
            raise DbError("Read-only profile: only SELECT, WITH, SHOW and EXPLAIN run here "
                          "(edit the profile and set Mode to read-write)")
        with self.lock:
            if write and not self.in_tx:
                self._call(self.driver.begin)
                self.in_tx, self.tx_writes = True, 0
                self._log("begin", "", "transaction")
            res = self._run(sql)
            if write:
                self.tx_writes += 1
        if res.columns and (st := simple_table(sql)):
            try:
                self._editable(res, st[0] or self.schema, st[1])
            except DbError:
                pass
        return res

    def _run(self, sql: str, page: int = PAGE) -> Result:
        try:
            res = self._call(self.driver.execute, sql, page)
        except DbError:
            self._log("query", oneline(sql), "error")
            raise
        if res.columns:
            res.message = f"{len(res.rows)}{'+' if res.more else ''} rows"
        else:
            res.message = f"{res.rowcount} rows affected" if res.rowcount >= 0 else "done"
        self._log("query", oneline(sql), f"{res.message} · {res.elapsed * 1000:.0f} ms")
        return res

    def _editable(self, res: Result, schema: str, table: str) -> None:
        res.info = self.columns(schema, table)
        if res.info:
            res.table = (schema, table)

    def more(self, res: Result) -> int:
        return self._call(self.driver.fetch_more, res, PAGE)

    def fetch_all(self, sql: str, limit: int = MAX_EXPORT) -> tuple[list[str], list[list]]:
        if not is_readonly(sql):
            raise DbError("Only a SELECT can be run again to export it")
        return self._call(self.driver.fetch_all, sql, limit)

    # ── transacciones ──

    def transaction(self, op: str) -> Result:
        with self.lock:
            if op == "begin":
                if not self.in_tx:
                    self._call(self.driver.begin)
                    self.in_tx, self.tx_writes = True, 0
                self._log("begin", "", "transaction")
                return Result("BEGIN", message="transaction open")
            if not self.in_tx:
                return Result(op.upper(), message="no open transaction")
            n = self.tx_writes
            try:
                self._call(self.driver.commit if op == "commit" else self.driver.rollback)
            finally:
                # Si falla el COMMIT la base ya la deshizo (o la conexión se cayó)
                self.in_tx, self.tx_writes = False, 0
            self._log(op, "", f"{n} statements")
            return Result(op.upper(), message=f"{'committed' if op == 'commit' else 'rolled back'} "
                                              f"({n} statement{'' if n == 1 else 's'})")

    def apply(self, stmts: list[tuple[str, list]]) -> int:
        """Aplica las ediciones de la grilla en una transacción propia: todo o nada.
        Un UPDATE / DELETE que no toca exactamente una fila deshace todo."""
        if self.profile.readonly:
            raise DbError("Read-only profile")
        with self.lock:
            if self.in_tx:
                raise DbError("There is an open transaction: commit (C) or roll back (R) first")
            d = self.driver
            self._call(d.begin)
            try:
                for sql, params in stmts:
                    n = self._call(d.write, sql, params)
                    if first_word(sql) in ("update", "delete") and n != 1:
                        raise DbError(f"{oneline(sql, 80)} matched {n} rows instead of 1: nothing was saved")
                    self._log("write", oneline(render_sql(sql, params, d.PARAM)), f"{n} row{'' if n == 1 else 's'}")
                self._call(d.commit)
            except Exception as e:  # noqa: BLE001
                try:
                    d.rollback()
                except Exception:  # noqa: BLE001
                    pass
                self._log("rollback", "grid changes", "error")
                raise _err(e) from None
            self._log("commit", "grid changes", f"{len(stmts)} statements")
            return len(stmts)


def column_detail(c: Column) -> str:
    flags = [c.type]
    if c.pk:
        flags.append("PK")
    if not c.nullable:
        flags.append("NOT NULL")
    if c.auto:
        flags.append("auto")
    elif c.default is not None:
        flags.append(f"= {c.default}")
    return "  ".join(f for f in flags if f)
