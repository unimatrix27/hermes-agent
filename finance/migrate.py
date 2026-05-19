#!/usr/bin/env python3
"""Apply or roll back finance migrations.

Usage:
    SUPABASE_DB_URL=... python3 finance/migrate.py up           # apply every up migration in order
    SUPABASE_DB_URL=... python3 finance/migrate.py down         # roll back every down migration in reverse order
    SUPABASE_DB_URL=... python3 finance/migrate.py up 001       # apply just 001_*.up.sql
    SUPABASE_DB_URL=... python3 finance/migrate.py down 002     # roll back just 002_*.down.sql

Every SQL file is idempotent on re-apply (CREATE/DROP IF EXISTS) and wrapped in
its own BEGIN/COMMIT, so partial states never linger.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _discover(direction: str) -> list[Path]:
    suffix = ".up.sql" if direction == "up" else ".down.sql"
    files = sorted(MIGRATIONS_DIR.glob(f"*{suffix}"))
    if direction == "down":
        files = list(reversed(files))
    return files


def _filter(files: list[Path], prefix: str | None) -> list[Path]:
    if prefix is None:
        return files
    matched = [f for f in files if f.name.startswith(prefix + "_") or f.stem.startswith(prefix)]
    if not matched:
        raise SystemExit(f"no migration matched prefix {prefix!r}")
    return matched


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("up", "down"):
        print("usage: migrate.py {up|down} [prefix]", file=sys.stderr)
        return 2

    direction = sys.argv[1]
    prefix = sys.argv[2] if len(sys.argv) > 2 else None

    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        print("SUPABASE_DB_URL not set", file=sys.stderr)
        return 2

    files = _filter(_discover(direction), prefix)
    if not files:
        print("no migrations found")
        return 0

    with psycopg2.connect(url) as conn:
        for sql_file in files:
            sql = sql_file.read_text()
            with conn.cursor() as cur:
                cur.execute(sql)
            print(f"Migration {direction!r} applied from {sql_file.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
