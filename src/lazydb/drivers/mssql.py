"""SQL Server (pymssql). El catálogo sale de sys.*; el DDL de las tablas se arma desde las columnas."""

from __future__ import annotations

from .base import Column, Driver, missing

ENCRYPTION = {"require": "require", "verify": "require", "disable": "off"}

_TYPE = """TYPE_NAME(c.user_type_id) + CASE
    WHEN TYPE_NAME(c.user_type_id) IN ('varchar', 'char', 'varbinary', 'binary')
        THEN '(' + CASE WHEN c.max_length = -1 THEN 'max' ELSE CAST(c.max_length AS varchar(10)) END + ')'
    WHEN TYPE_NAME(c.user_type_id) IN ('nvarchar', 'nchar')
        THEN '(' + CASE WHEN c.max_length = -1 THEN 'max' ELSE CAST(c.max_length / 2 AS varchar(10)) END + ')'
    WHEN TYPE_NAME(c.user_type_id) IN ('decimal', 'numeric')
        THEN '(' + CAST(c.precision AS varchar(10)) + ',' + CAST(c.scale AS varchar(10)) + ')'
    ELSE '' END"""


class MssqlDriver(Driver):
    ENGINE = "mssql"
    PORT = 1433

    def connect(self, profile, host: str, port: int, password: str) -> None:
        try:
            import pymssql
        except ImportError:
            raise missing("pymssql", "python-pymssql") from None
        kw = {}
        if profile.ssl in ENCRYPTION:
            kw["encryption"] = ENCRYPTION[profile.ssl]
        self.conn = pymssql.connect(server=host, port=str(port), user=profile.user or None, password=password or None,
                                    database=profile.database or "", login_timeout=profile.timeout,
                                    appname="lazydb", autocommit=True, read_only=profile.readonly, **kw)

    def set_autocommit(self, on: bool) -> None:
        self.conn.autocommit(on)

    # ── catálogo ──

    def schemas(self) -> list[str]:
        return [r[0] for r in self.query(
            "SELECT s.name FROM sys.schemas s WHERE s.name = 'dbo' OR EXISTS (SELECT 1 FROM sys.objects o "
            "WHERE o.schema_id = s.schema_id AND o.is_ms_shipped = 0) ORDER BY s.name")]

    def default_schema(self) -> str:
        return self.query("SELECT SCHEMA_NAME()")[0][0] or "dbo"

    def relations(self, schema: str, kind: str) -> list[str]:
        return [r[0] for r in self.query(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = %s "
            "ORDER BY TABLE_NAME", (schema, "VIEW" if kind == "view" else "BASE TABLE"))]

    def routines(self, schema: str) -> list[tuple[str, str]]:
        return self.query("SELECT o.name, o.type FROM sys.objects o WHERE SCHEMA_NAME(o.schema_id) = %s "
                          "AND o.type IN ('P', 'FN', 'IF', 'TF') ORDER BY o.name", (schema,))

    def columns(self, schema: str, table: str) -> list[Column]:
        rows = self.query(
            f"SELECT c.name, {_TYPE}, c.is_nullable, OBJECT_DEFINITION(c.default_object_id), "
            "CASE WHEN EXISTS (SELECT 1 FROM sys.index_columns ic JOIN sys.indexes i ON i.object_id = ic.object_id "
            "AND i.index_id = ic.index_id WHERE i.is_primary_key = 1 AND ic.object_id = c.object_id "
            "AND ic.column_id = c.column_id) THEN 1 ELSE 0 END, "
            "CASE WHEN c.is_identity = 1 OR c.is_computed = 1 OR TYPE_NAME(c.user_type_id) IN ('timestamp', "
            "'rowversion') THEN 1 ELSE 0 END "
            "FROM sys.columns c WHERE c.object_id = OBJECT_ID(%s) ORDER BY c.column_id",
            (self.qualified(schema, table),))
        return [Column(n, t, bool(null), d, bool(pk), bool(auto)) for n, t, null, d, pk, auto in rows]

    def indexes(self, schema: str, table: str) -> list[tuple[str, str]]:
        rows = self.query(
            "SELECT i.name, i.type_desc, i.is_unique, STRING_AGG(c.name, ', ') WITHIN GROUP (ORDER BY ic.key_ordinal) "
            "FROM sys.indexes i JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
            "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
            "WHERE i.object_id = OBJECT_ID(%s) AND i.name IS NOT NULL AND ic.is_included_column = 0 "
            "GROUP BY i.name, i.type_desc, i.is_unique, i.is_primary_key ORDER BY i.is_primary_key DESC, i.name",
            (self.qualified(schema, table),))
        return [(n, f"{'UNIQUE ' if u else ''}{t} ({cols})") for n, t, u, cols in rows]

    def foreign_keys(self, schema: str, table: str) -> list[tuple[str, str]]:
        rows = self.query(
            "SELECT fk.name, STRING_AGG(pc.name, ', ') WITHIN GROUP (ORDER BY fkc.constraint_column_id), "
            "OBJECT_SCHEMA_NAME(fk.referenced_object_id) + '.' + OBJECT_NAME(fk.referenced_object_id), "
            "STRING_AGG(rc.name, ', ') WITHIN GROUP (ORDER BY fkc.constraint_column_id) "
            "FROM sys.foreign_keys fk JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
            "JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id "
            "JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id "
            "WHERE fk.parent_object_id = OBJECT_ID(%s) GROUP BY fk.name, fk.referenced_object_id ORDER BY fk.name",
            (self.qualified(schema, table),))
        return [(n, f"({cols}) → {ref}({rcols})") for n, cols, ref, rcols in rows]

    def ddl(self, schema: str, name: str, kind: str, detail: str = "") -> str:
        qual = self.qualified(schema, name)
        if kind != "table":
            return (self.query("SELECT OBJECT_DEFINITION(OBJECT_ID(%s))", (qual,))[0][0] or "").strip()
        cols = self.columns(schema, name)
        pk = [c.name for c in cols if c.pk]
        cons = [f"PRIMARY KEY ({', '.join(self.quote(c) for c in pk)})"] if pk else []
        out = ["-- Built from sys.* by lazydb (identity seeds, collations and options are left out)",
               self.table_ddl(qual, cols, cons)]
        for n, desc in self.foreign_keys(schema, name):
            cols_, _, ref = desc.partition(" → ")
            out.append(f"ALTER TABLE {qual} ADD CONSTRAINT {self.quote(n)} FOREIGN KEY {cols_} REFERENCES {ref};")
        idx = self.indexes(schema, name)
        for n, desc in idx[1:] if pk else idx:      # el primero es el de la clave primaria
            out.append(f"-- index {n}: {desc}")
        return "\n\n".join(out)

    # ── SQL ──

    def quote(self, name: str) -> str:
        return "[" + name.replace("]", "]]") + "]"

    def select_sql(self, schema: str, table: str, limit: int) -> str:
        return f"SELECT TOP ({int(limit)}) * FROM {self.qualified(schema, table)}"
