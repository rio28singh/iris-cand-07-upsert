"""Connection + migration helpers."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/iris"
ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = ROOT / "migrations"
FIXTURES_DIR = ROOT / "fixtures"
MANIFESTS_DIR = ROOT / "manifests"


def get_dsn() -> str:
    return os.environ.get("IRIS_DATABASE_URL", DEFAULT_DSN)


def connect(dsn: str | None = None) -> psycopg.Connection:
    return psycopg.connect(dsn or get_dsn())


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply every migrations/*.sql in filename order. Migrations are idempotent."""
    applied = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        conn.execute(path.read_text(encoding="utf-8"))
        applied.append(path.name)
    conn.commit()
    return applied
