"""
drivers/base.py — lo común a los motores: la interfaz que usa core.Session y
la ejecución DB-API (cursor, fetchmany, autocommit).

Cada motor (sqlite, postgres, mysql, mssql) es una subclase de Driver que
sabe conectarse, recorrer su catálogo, armar el DDL y citar identificadores.
El módulo de cada driver (psycopg, pymysql, pymssql) se importa recién al
conectar: si falta, el error dice qué instalar.

La conexión trabaja en autocommit. Session llama a begin() antes de una
sentencia que modifica algo y a commit() / rollback() para cerrarla.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

PAGE = 500                 # filas por tanda en la grilla de resultados


class DbError(Exception):
    pass


def missing(module: str, arch: str) -> DbError:
    return DbError(f"This engine needs {module}: pip install {module}  (Arch: pacman -S {arch})")


@dataclass
class Column:
    name: str
    type: str = ""
    nullable: bool = True
    default: Optional[str] = None
    pk: bool = False
    auto: bool = False     # identity / autoincrement / generada: se puede omitir al insertar

    @property
    def optional(self) -> bool:
        """Se puede dejar vacía en un INSERT."""
        return self.nullable or self.default is not None or self.auto


@dataclass
class Result:
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    rowcount: int = -1
    elapsed: float = 0.0
    more: bool = False                         # quedan filas (G las trae)
    cursor: Any = None                         # cursor abierto mientras haya más filas
    table: Optional[tuple[str, str]] = None    # (esquema, tabla) si se puede editar
    info: list[Column] = field(default_factory=list)   # columnas de esa tabla
    limit: int = 0                             # >0: abierta desde el árbol con ese LIMIT
    message: str = ""

    @property
    def pk(self) -> list[str]:
        return [c.name for c in self.info if c.pk]

    @property
    def editable(self) -> bool:
        pk = self.pk
        return bool(self.table and pk and all(c in self.columns for c in pk))


class Driver:
    ENGINE = ""
    PORT = 0
    PARAM = "%s"                 # marcador de parámetros del módulo DB-API
    HAS_ROUTINES = True

    def __init__(self) -> None:
        self.conn: Any = None

    # ── conexión ──

    def connect(self, profile, host: str, port: int, password: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001 - la conexión ya puede estar caída
                pass
        self.conn = None

    def set_readonly(self) -> None:
        """La sesión solo lectura del lado del servidor, si el motor lo permite."""

    def set_autocommit(self, on: bool) -> None:
        raise NotImplementedError

    def begin(self) -> None:
        self.set_autocommit(False)

    def commit(self) -> None:
        self.conn.commit()
        self.set_autocommit(True)

    def rollback(self) -> None:
        self.conn.rollback()
        self.set_autocommit(True)

    # ── ejecución ──

    def execute(self, sql: str, page: int = PAGE) -> Result:
        cur = self.conn.cursor()
        t0 = time.monotonic()
        cur.execute(sql)
        res = Result(sql)
        if cur.description:
            res.columns = [d[0] or f"col{i + 1}" for i, d in enumerate(cur.description)]
            rows = cur.fetchmany(page)
            res.rows = [list(r) for r in rows]
            res.more = len(rows) == page
        res.rowcount = cur.rowcount
        res.elapsed = time.monotonic() - t0
        if res.more:
            res.cursor = cur
        else:
            cur.close()
        return res

    def fetch_more(self, res: Result, page: int = PAGE) -> int:
        if res.cursor is None:
            return 0
        rows = res.cursor.fetchmany(page)
        res.rows.extend(list(r) for r in rows)
        res.more = len(rows) == page
        if not res.more:
            res.cursor.close()
            res.cursor = None
        return len(rows)

    def fetch_all(self, sql: str, limit: int) -> tuple[list[str], list[list]]:
        cur = self.conn.cursor()
        cur.execute(sql)
        cols = [d[0] or f"col{i + 1}" for i, d in enumerate(cur.description or [])]
        rows: list[list] = []
        while cols and len(rows) < limit:
            chunk = cur.fetchmany(min(5000, limit - len(rows)))
            if not chunk:
                break
            rows.extend(list(r) for r in chunk)
        cur.close()
        return cols, rows

    def write(self, sql: str, params: list) -> int:
        """Una sentencia con parámetros (las ediciones de la grilla). Devuelve las filas tocadas."""
        cur = self.conn.cursor()
        cur.execute(sql, params)
        n = cur.rowcount
        cur.close()
        return n

    def query(self, sql: str, params: tuple | list | None = None) -> list[tuple]:
        """Consultas al catálogo."""
        cur = self.conn.cursor()
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return [tuple(r) for r in rows]

    # ── catálogo ──

    def schemas(self) -> list[str]:
        raise NotImplementedError

    def default_schema(self) -> str:
        raise NotImplementedError

    def relations(self, schema: str, kind: str) -> list[str]:
        """kind: table | view."""
        raise NotImplementedError

    def routines(self, schema: str) -> list[tuple[str, str]]:
        """[(nombre visible, dato para el DDL)]."""
        return []

    def columns(self, schema: str, table: str) -> list[Column]:
        raise NotImplementedError

    def indexes(self, schema: str, table: str) -> list[tuple[str, str]]:
        """[(nombre, descripción)]."""
        return []

    def foreign_keys(self, schema: str, table: str) -> list[tuple[str, str]]:
        """[(nombre, columnas → tabla(columnas))]."""
        return []

    def ddl(self, schema: str, name: str, kind: str, detail: str = "") -> str:
        """kind: table | view | routine."""
        raise NotImplementedError

    # ── SQL ──

    def quote(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def qualified(self, schema: str, table: str) -> str:
        return f"{self.quote(schema)}.{self.quote(table)}" if schema else self.quote(table)

    def select_sql(self, schema: str, table: str, limit: int) -> str:
        return f"SELECT * FROM {self.qualified(schema, table)} LIMIT {int(limit)}"

    def insert_default_sql(self, qualified: str) -> str:
        return f"INSERT INTO {qualified} DEFAULT VALUES"

    def table_ddl(self, qualified: str, cols: list[Column], constraints: list[str]) -> str:
        """CREATE TABLE armado a partir de las columnas (motores sin SHOW CREATE)."""
        lines = []
        for c in cols:
            line = f"  {self.quote(c.name)} {c.type}"
            if not c.nullable:
                line += " NOT NULL"
            if c.default is not None:
                line += f" DEFAULT {c.default}"
            lines.append(line)
        lines += [f"  {x}" for x in constraints]
        return f"CREATE TABLE {qualified} (\n" + ",\n".join(lines) + "\n);"
