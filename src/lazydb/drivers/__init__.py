"""Motores soportados: sqlite, postgres, mysql (también MariaDB) y mssql (SQL Server)."""

from __future__ import annotations

from .base import PAGE, Column, DbError, Driver, Result

ENGINES = {"postgres": "PostgreSQL", "mysql": "MySQL / MariaDB", "mssql": "SQL Server", "sqlite": "SQLite"}


def make(engine: str) -> Driver:
    if engine == "sqlite":
        from .sqlite import SqliteDriver
        return SqliteDriver()
    if engine == "postgres":
        from .postgres import PostgresDriver
        return PostgresDriver()
    if engine == "mysql":
        from .mysql import MysqlDriver
        return MysqlDriver()
    if engine == "mssql":
        from .mssql import MssqlDriver
        return MssqlDriver()
    raise DbError(f"Unknown engine: {engine}")


def default_port(engine: str) -> int:
    return {"postgres": 5432, "mysql": 3306, "mssql": 1433}.get(engine, 0)


__all__ = ["ENGINES", "PAGE", "Column", "DbError", "Driver", "Result", "default_port", "make"]
