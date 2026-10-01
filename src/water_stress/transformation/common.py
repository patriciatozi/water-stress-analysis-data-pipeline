from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


def write_json(path: Path, document: Mapping[str, Any]) -> None:
    """Atomically write a UTF-8 JSON document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def write_parquet(path: Path, table: pa.Table) -> None:
    """Atomically write a compressed Parquet table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        pq.write_table(table, temporary, compression="zstd")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def schema_document(dataset: str, schema: pa.Schema) -> dict[str, Any]:
    return {
        "dataset": dataset,
        "columns": [
            {
                "name": field.name,
                "type": str(field.type),
                "nullable": field.nullable,
                "unit": (field.metadata or {}).get(b"unit", b"").decode(),
                "description": (field.metadata or {}).get(b"description", b"").decode(),
            }
            for field in schema
        ],
    }


def missing_counts(table: pa.Table) -> dict[str, int]:
    return {name: table[name].null_count for name in table.column_names}


def column_ranges(table: pa.Table, columns: Iterable[str]) -> dict[str, dict[str, Any]]:
    return {
        column: {
            "minimum": min(
                (value for value in table[column].to_pylist() if value is not None),
                default=None,
            ),
            "maximum": max(
                (value for value in table[column].to_pylist() if value is not None),
                default=None,
            ),
        }
        for column in columns
    }


def require_files(paths: Iterable[Path]) -> None:
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Required input artifacts not found: {', '.join(missing)}")


def table_quality(table: pa.Table, key_columns: Iterable[str]) -> dict[str, Any]:
    """Return a consistent quality summary for a table's analytical key."""
    keys = tuple(key_columns)
    missing_columns = [column for column in keys if column not in table.column_names]
    if missing_columns:
        raise ValueError(f"Quality key columns not found: {', '.join(missing_columns)}")

    rows = list(zip(*(table[column].to_pylist() for column in keys), strict=True))
    duplicate_count = len(rows) - len(set(rows))
    null_key_count = sum(any(value is None for value in row) for row in rows)
    missing_by_column = missing_counts(table)
    issues: list[str] = []
    if duplicate_count:
        issues.append(f"duplicate key rows: {duplicate_count}")
    if null_key_count:
        issues.append(f"rows with null key: {null_key_count}")
    if any(count for count in missing_by_column.values()):
        issues.append("missing values detected")
    status = "failed" if duplicate_count or null_key_count else "warning" if issues else "passed"
    return {
        "quality_status": status,
        "quality_issues": issues,
        "duplicate_key_count": duplicate_count,
        "null_key_count": null_key_count,
        "missing_by_column": missing_by_column,
    }
