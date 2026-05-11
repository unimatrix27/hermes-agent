#!/usr/bin/env python3
"""Apply or roll back the finance receipt-tables migration.

Usage:
    SUPABASE_DB_URL=... python3 finance/migrate.py up
    SUPABASE_DB_URL=... python3 finance/migrate.py down

The script reads SQL from finance/migrations/001_receipt_tables.{up,down}.sql
and applies it inside a single transaction. Both files are idempotent on a
fresh clone (CREATE IF NOT EXISTS / DROP IF EXISTS).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
UP_FILE = MIGRATIONS_DIR / "001_receipt_tables.up.sql"
DOWN_FILE = MIGRATIONS_DIR / "001_receipt_tables.down.sql"


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("up", "down"):
        print("usage: migrate.py {up|down}", file=sys.stderr)
        return 2

    direction = sys.argv[1]
    sql_file = UP_FILE if direction == "up" else DOWN_FILE
    sql = sql_file.read_text()

    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        print("SUPABASE_DB_URL not set", file=sys.stderr)
        return 2

    with psycopg2.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    print(f"Migration {direction!r} applied from {sql_file.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
