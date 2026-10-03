from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg

from water_stress.config import DatabaseSettings


@contextmanager
def connect(settings: DatabaseSettings) -> Iterator[psycopg.Connection[Any]]:
    if not settings.enabled:
        raise ValueError("Database persistence is disabled in configuration")
    connection = psycopg.connect(
        host=settings.host,
        port=settings.port,
        dbname=settings.name,
        user=settings.user,
        password=settings.password.get_secret_value() if settings.password else None,
        sslmode=settings.sslmode,
        connect_timeout=settings.connect_timeout_seconds,
    )
    try:
        yield connection
    finally:
        connection.close()


def apply_migrations(
    connection: psycopg.Connection[Any], migration_root: Path = Path("migrations")
) -> list[str]:
    migration_files = sorted(migration_root.glob("*.sql"))
    if not migration_files:
        raise FileNotFoundError(f"No SQL migrations found under {migration_root}")
    applied: list[str] = []
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE SCHEMA IF NOT EXISTS control;
            CREATE TABLE IF NOT EXISTS control.schema_migration (
                version TEXT PRIMARY KEY,
                applied_at_utc TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        for path in migration_files:
            version = path.name
            cursor.execute("SELECT 1 FROM control.schema_migration WHERE version = %s", (version,))
            if cursor.fetchone() is not None:
                continue
            cursor.execute(path.read_text(encoding="utf-8"))
            cursor.execute("INSERT INTO control.schema_migration (version) VALUES (%s)", (version,))
            applied.append(version)
    connection.commit()
    return applied
