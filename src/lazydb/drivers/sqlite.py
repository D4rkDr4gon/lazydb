"""SQLite (módulo sqlite3 de la stdlib). La "base" del perfil es la ruta del archivo."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from .base import Column, DbError, Driver


class SqliteDriver(Driver):
    ENGINE = "sqlite"
    PARAM = "?"
    HAS_ROUTINES = False

    def connect(self, profile, host: str, port: int, password: str) -> None:
        path = Path(profile.database).expanduser()
        if not path.is_file():
            raise DbError(f"No such SQLite file: {path}")
        # mode=ro: el archivo no se puede modificar; rw: no crea uno nuevo por un error de tipeo
        mode = "ro" if profile.readonly else "rw"
        self.conn = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode={mode}", uri=True,
                                    isolation_level=None, check_same_thread=False, timeout=profile.timeout)
        self.conn.execute("PRAGMA foreign_keys = ON")

    def set_autocommit(self, on: bool) -> None:
        pass   # isolation_level=None: autocommit salvo entre BEGIN y COMMIT

    def begin(self) -> None:
        self.conn.execute("BEGIN")

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    # ── catálogo ──

    def schemas(self) -> list[str]:
        return [r[1] for r in self.query("PRAGMA database_list") if r[1] != "temp"]

    def default_schema(self) -> str:
        return "main"

    def relations(self, schema: str, kind: str) -> list[str]:
        return [r[0] for r in self.query(
            f"SELECT name FROM {self.quote(schema)}.sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name", (kind,))]

    def columns(self, schema: str, table: str) -> list[Column]:
        rows = self.query(f"PRAGMA {self.quote(schema)}.table_info({self.quote(table)})")
        pks = [r for r in rows if r[5]]
        out = []
        for _cid, name, typ, notnull, default, pk in rows:
            # INTEGER PRIMARY KEY solo es alias del rowid: se numera solo
            auto = bool(pk) and len(pks) == 1 and (typ or "").upper() == "INTEGER"
            out.append(Column(name, typ or "", not notnull and not pk, default, bool(pk), auto))
        return out

    def indexes(self, schema: str, table: str) -> list[tuple[str, str]]:
        rows = self.query(f"SELECT name, sql FROM {self.quote(schema)}.sqlite_master WHERE type = 'index' "
                          "AND tbl_name = ? ORDER BY name", (table,))
        return [(n, sql or "(automatic, from a UNIQUE or PRIMARY KEY constraint)") for n, sql in rows]

    def foreign_keys(self, schema: str, table: str) -> list[tuple[str, str]]:
        groups: dict[int, list] = {}
        for fid, _seq, ref, frm, to, *_ in self.query(
                f"PRAGMA {self.quote(schema)}.foreign_key_list({self.quote(table)})"):
            groups.setdefault(fid, []).append((ref, frm, to))
        out = []
        for fid, parts in sorted(groups.items()):
            ref = parts[0][0]
            out.append((f"fk{fid}", f"({', '.join(p[1] for p in parts)}) → {ref}"
                                    f"({', '.join(p[2] or '?' for p in parts)})"))
        return out

    def ddl(self, schema: str, name: str, kind: str, detail: str = "") -> str:
        master = f"{self.quote(schema)}.sqlite_master"
        rows = self.query(f"SELECT sql FROM {master} WHERE name = ?", (name,))
        parts = [r[0] + ";" for r in rows if r[0]]
        if kind == "table":
            parts += [r[0] + ";" for r in self.query(
                f"SELECT sql FROM {master} WHERE type IN ('index', 'trigger') AND tbl_name = ? AND sql IS NOT NULL "
                "ORDER BY type, name", (name,))]
        return "\n\n".join(parts)
