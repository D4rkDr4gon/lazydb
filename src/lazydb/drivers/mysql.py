"""MySQL / MariaDB (PyMySQL). Cada base es un esquema del árbol; el DDL sale de SHOW CREATE."""

from __future__ import annotations

import ssl

from .base import Column, Driver, missing

SYSTEM = {"information_schema", "performance_schema", "mysql", "sys"}


def ssl_context(mode: str):
    if mode == "verify":
        return ssl.create_default_context()
    if mode == "require":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


class MysqlDriver(Driver):
    ENGINE = "mysql"
    PORT = 3306

    def connect(self, profile, host: str, port: int, password: str) -> None:
        try:
            import pymysql
        except ImportError:
            raise missing("pymysql", "python-pymysql") from None
        self.conn = pymysql.connect(host=host, port=port, user=profile.user or None, password=password or "",
                                    database=profile.database or None, connect_timeout=profile.timeout,
                                    charset="utf8mb4", autocommit=True, ssl=ssl_context(profile.ssl),
                                    ssl_disabled=profile.ssl == "disable" or None)

    def set_readonly(self) -> None:
        self.query("SET SESSION TRANSACTION READ ONLY")

    def set_autocommit(self, on: bool) -> None:
        self.conn.autocommit(on)

    # ── catálogo ──

    def schemas(self) -> list[str]:
        rows = [r[0] for r in self.query("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY 1")]
        return [s for s in rows if s.lower() not in SYSTEM] or rows

    def default_schema(self) -> str:
        return self.query("SELECT DATABASE()")[0][0] or ""

    def relations(self, schema: str, kind: str) -> list[str]:
        types = ("VIEW",) if kind == "view" else ("BASE TABLE", "SYSTEM VERSIONED")
        return [r[0] for r in self.query(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_TYPE IN %s "
            "ORDER BY TABLE_NAME", (schema, types))]

    def routines(self, schema: str) -> list[tuple[str, str]]:
        return self.query("SELECT ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES "
                          "WHERE ROUTINE_SCHEMA = %s ORDER BY ROUTINE_NAME", (schema,))

    def columns(self, schema: str, table: str) -> list[Column]:
        rows = self.query(
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE = 'YES', COLUMN_DEFAULT, COLUMN_KEY = 'PRI', "
            "EXTRA LIKE '%%auto_increment%%' OR EXTRA LIKE '%%GENERATED%%' "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            (schema, table))
        return [Column(n, t, bool(null), d, bool(pk), bool(auto)) for n, t, null, d, pk, auto in rows]

    def indexes(self, schema: str, table: str) -> list[tuple[str, str]]:
        rows = self.query(
            "SELECT INDEX_NAME, MIN(NON_UNIQUE), GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR ', ') "
            "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
            "GROUP BY INDEX_NAME ORDER BY INDEX_NAME = 'PRIMARY' DESC, INDEX_NAME", (schema, table))
        return [(n, f"{'' if int(nu) else 'UNIQUE '}({cols})") for n, nu, cols in rows]

    def foreign_keys(self, schema: str, table: str) -> list[tuple[str, str]]:
        rows = self.query(
            "SELECT CONSTRAINT_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY ORDINAL_POSITION SEPARATOR ', '), "
            "REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME, "
            "GROUP_CONCAT(REFERENCED_COLUMN_NAME ORDER BY ORDINAL_POSITION SEPARATOR ', ') "
            "FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
            "AND REFERENCED_TABLE_NAME IS NOT NULL "
            "GROUP BY CONSTRAINT_NAME, REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME ORDER BY CONSTRAINT_NAME",
            (schema, table))
        return [(n, f"({cols}) → {rs}.{rt}({rcols})") for n, cols, rs, rt, rcols in rows]

    def ddl(self, schema: str, name: str, kind: str, detail: str = "") -> str:
        what = {"table": "TABLE", "view": "VIEW"}.get(kind, detail or "PROCEDURE")
        row = self.query(f"SHOW CREATE {what} {self.qualified(schema, name)}")[0]
        return (row[2] if kind == "routine" else row[1]) + ";"

    # ── SQL ──

    def quote(self, name: str) -> str:
        return "`" + name.replace("`", "``") + "`"

    def insert_default_sql(self, qualified: str) -> str:
        return f"INSERT INTO {qualified} () VALUES ()"
