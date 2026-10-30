"""PostgreSQL (psycopg 3). El catálogo sale de pg_catalog; el DDL de las tablas se arma con pg_get_*def."""

from __future__ import annotations

from .base import Column, DbError, Driver, missing

SSLMODE = {"default": "prefer", "require": "require", "verify": "verify-full", "disable": "disable"}


class PostgresDriver(Driver):
    ENGINE = "postgres"
    PORT = 5432

    def connect(self, profile, host: str, port: int, password: str) -> None:
        try:
            import psycopg
        except ImportError:
            raise missing("psycopg", "python-psycopg") from None
        self.conn = psycopg.connect(host=host, port=port, dbname=profile.database or "postgres",
                                    user=profile.user or None, password=password or None,
                                    sslmode=SSLMODE.get(profile.ssl, "prefer"), connect_timeout=profile.timeout,
                                    application_name="lazydb", autocommit=True)

    def set_readonly(self) -> None:
        self.conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")

    def set_autocommit(self, on: bool) -> None:
        self.conn.autocommit = on

    # ── catálogo ──

    def schemas(self) -> list[str]:
        return [r[0] for r in self.query(
            "SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' "
            "ORDER BY nspname")]

    def default_schema(self) -> str:
        return self.query("SELECT current_schema()")[0][0] or "public"

    def relations(self, schema: str, kind: str) -> list[str]:
        kinds = ["r", "p", "f"] if kind == "table" else ["v", "m"]
        return [r[0] for r in self.query(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind::text = ANY(%s) ORDER BY c.relname", (schema, kinds))]

    def routines(self, schema: str) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self.query(
            "SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')', p.oid::text "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s "
            "ORDER BY 1", (schema,))]

    def columns(self, schema: str, table: str) -> list[Column]:
        rows = self.query(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, "
            "pg_get_expr(d.adbin, d.adrelid), COALESCE(a.attnum = ANY(i.indkey), false), "
            "a.attidentity <> '' OR a.attgenerated <> '' "
            "FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "LEFT JOIN pg_index i ON i.indrelid = a.attrelid AND i.indisprimary "
            "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
            (self.qualified(schema, table),))
        return [Column(n, t, bool(null), d, bool(pk), bool(auto)) for n, t, null, d, pk, auto in rows]

    def indexes(self, schema: str, table: str) -> list[tuple[str, str]]:
        return self.query("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s "
                          "ORDER BY indexname", (schema, table))

    def foreign_keys(self, schema: str, table: str) -> list[tuple[str, str]]:
        return self.query("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                          "WHERE conrelid = %s::regclass AND contype = 'f' ORDER BY conname",
                          (self.qualified(schema, table),))

    def ddl(self, schema: str, name: str, kind: str, detail: str = "") -> str:
        if kind == "routine":
            try:
                return self.query("SELECT pg_get_functiondef(%s::oid)", (detail,))[0][0]
            except Exception as e:  # noqa: BLE001 - agregados y similares no tienen definición
                raise DbError(f"No definition for {name}: {e}") from None
        qual = self.qualified(schema, name)
        if kind == "view":
            relkind, body = self.query("SELECT relkind::text, pg_get_viewdef(%s::regclass, true) "
                                       "FROM pg_class WHERE oid = %s::regclass", (qual, qual))[0]
            what = "MATERIALIZED VIEW" if relkind == "m" else "OR REPLACE VIEW"
            return f"CREATE {what} {qual} AS\n{body.rstrip().rstrip(';')};"
        cons = self.query(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = %s::regclass "
            "ORDER BY CASE contype WHEN 'p' THEN 0 WHEN 'u' THEN 1 WHEN 'c' THEN 2 ELSE 3 END, conname", (qual,))
        out = self.table_ddl(qual, self.columns(schema, name), [f"CONSTRAINT {self.quote(n)} {d}" for n, d in cons])
        idx = self.query(
            "SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i WHERE i.indrelid = %s::regclass "
            "AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid) ORDER BY 1", (qual,))
        return "\n\n".join([out] + [r[0] + ";" for r in idx])
