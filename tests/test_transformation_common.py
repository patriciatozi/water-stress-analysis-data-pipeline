from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from water_stress.transformation.common import (
    column_ranges,
    missing_counts,
    require_files,
    schema_document,
    table_quality,
    write_json,
    write_parquet,
)


def test_writes_json_and_parquet_atomically(tmp_path: Path) -> None:
    json_path = tmp_path / "nested" / "metadata.json"
    parquet_path = tmp_path / "nested" / "part.parquet"
    table = pa.table({"value": [1, None, 3]})

    write_json(json_path, {"dataset": "example"})
    write_parquet(parquet_path, table)

    assert json.loads(json_path.read_text()) == {"dataset": "example"}
    assert pq.read_table(parquet_path).equals(table)
    assert not list(tmp_path.rglob("*.tmp"))


def test_builds_schema_and_quality_metrics() -> None:
    schema = pa.schema(
        [
            pa.field(
                "value",
                pa.float64(),
                metadata={"unit": "mm", "description": "Measured value"},
            )
        ]
    )
    table = pa.table({"value": [1.0, None, 3.0]}, schema=schema)

    document = schema_document("example", schema)

    assert document["columns"][0]["unit"] == "mm"
    assert missing_counts(table) == {"value": 1}
    assert column_ranges(table, ["value"]) == {"value": {"minimum": 1.0, "maximum": 3.0}}


def test_requires_all_input_files(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.touch()
    missing = tmp_path / "missing"

    with pytest.raises(FileNotFoundError, match=str(missing)):
        require_files((existing, missing))


@pytest.mark.parametrize(
    ("values", "status"),
    [
        ([["a", 1], ["b", 2]], "passed"),
        ([["a", 1], ["a", 2]], "failed"),
        ([["a", None], ["b", 2]], "warning"),
    ],
)
def test_table_quality_classifies_keys_and_missing_values(
    values: list[list[object]], status: str
) -> None:
    table = pa.table({"grid_id": [row[0] for row in values], "value": [row[1] for row in values]})

    report = table_quality(table, ["grid_id"])

    assert report["quality_status"] == status
