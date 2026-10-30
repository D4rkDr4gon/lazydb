"""Punto de entrada: `lazydb [perfil | archivo.sqlite]`, `lazydb --demo`, `python -m lazydb`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


def main() -> None:
    ap = argparse.ArgumentParser(prog="lazydb", description="A terminal database client.")
    ap.add_argument("target", nargs="?", default="",
                    help="connect to this profile on start, or open this SQLite file")
    ap.add_argument("--demo", action="store_true", help="browse a sample SQLite shop database (no server needed)")
    ap.add_argument("--version", action="version", version=f"lazydb {__version__}")
    args = ap.parse_args()
    try:
        import textual  # noqa: F401
    except ImportError:
        sys.exit("lazydb needs textual: pip install textual  (Arch: pacman -S python-textual)")
    from . import core

    target, adhoc = args.target, None
    if args.demo:
        from .demo import setup
        target = setup()
    elif target and Path(target).expanduser().is_file():
        path = Path(target).expanduser().resolve()
        adhoc = core.Profile(path.name, "sqlite", database=str(path), mode="readwrite")
        target = ""
    from .app import LazyDbApp
    LazyDbApp(target, adhoc).run()


if __name__ == "__main__":
    main()
