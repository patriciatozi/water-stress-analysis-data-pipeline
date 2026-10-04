from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import box

from water_stress.config import DatabaseSettings, Settings
from water_stress.database.client import apply_migrations, connect
from water_stress.database.loader import (
    DATASET_COLUMNS,
    load_dataset,
    load_dataset_files,
    register_bronze_manifests,
)
from water_stress.pipelines.run_database import build_parser
from water_stress.transformation.gold_weekly import gold_schema
from water_stress.transformation.water_balance_schema import HYDRAULIC_SCHEMA, WEEKLY_SCHEMA


class FakeCopy:
    def __init__(self) -> None:
        self.rows: list[tuple[object, ...]] = []

    def __enter__(self) -> FakeCopy:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def write_row(self, row: tuple[object, ...]) -> None:
        self.rows.append(row)


class FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []
        self.copy_stream = FakeCopy()
        self.copy_calls = 0

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: str, params: object = None) -> None:
        self.executed.append((query, params))

    def fetchone(self) -> None:
        return None

    def copy(self, _query: str) -> FakeCopy:
        self.copy_calls += 1
        return self.copy_stream


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_database_settings_builds_dsn_and_rejects_disabled_connection() -> None:
    database = DatabaseSettings(
        enabled=False,
        host="localhost",
        name="water_stress",
        user="app",
        password="secret",
    )

    assert database.host == "localhost"
    with pytest.raises(ValueError, match="disabled"), connect(database):
        pass


def test_applies_pending_sql_migration(tmp_path: Path) -> None:
    migration = tmp_path / "001_test.sql"
    migration.write_text("CREATE TABLE test_table (id integer);")
    connection = FakeConnection()

    applied = apply_migrations(connection, tmp_path)

    assert applied == ["001_test.sql"]
    assert connection.commits == 1
    assert any(
        "CREATE TABLE test_table" in query for query, _ in connection.cursor_instance.executed
    )


def test_registers_bronze_manifest_without_copying_payload(tmp_path: Path) -> None:
    artifact = tmp_path / "weather.json"
    artifact.write_bytes(b"{}")
    manifest = artifact.with_name("weather.manifest.json")
    manifest.write_text(
        json.dumps(
            {
                "source": "nasa_power",
                "url": "https://example.test/weather",
                "request_fingerprint": "a" * 64,
                "sha256": "b" * 64,
                "size_bytes": 2,
                "project_version": "0.1.0",
            }
        )
    )
    connection = FakeConnection()

    assert register_bronze_manifests(connection, tmp_path) == 1
    assert connection.commits == 1
    assert "weather.json" in str(connection.cursor_instance.executed[0][1])


def test_loads_regular_dataset_using_copy_and_upsert() -> None:
    connection = FakeConnection()
    table = pa.table({"grid_id": ["cell-1"], "soy_fraction": [0.5], "year": [2023]})

    count = load_dataset(connection, "crop_mask", table, processing_version="0.1.0")

    assert count == 1
    assert connection.commits == 1
    assert connection.cursor_instance.copy_stream.rows == [("cell-1", 2023, 0.5, "0.1.0")]


def test_streams_parquet_files_in_bounded_batches(tmp_path: Path) -> None:
    first = tmp_path / "part-000.parquet"
    second = tmp_path / "part-001.parquet"
    pq.write_table(
        pa.table(
            {
                "grid_id": ["cell-1", "cell-2", "cell-3"],
                "year": [2023] * 3,
                "soy_fraction": [0.1, 0.2, 0.3],
            }
        ),
        first,
    )
    pq.write_table(
        pa.table({"grid_id": ["cell-4", "cell-5"], "year": [2023] * 2, "soy_fraction": [0.4, 0.5]}),
        second,
    )
    connection = FakeConnection()

    count = load_dataset_files(
        connection,
        "crop_mask",
        [second, first],
        processing_version="0.1.0",
        batch_size=2,
    )

    assert count == 5
    assert connection.cursor_instance.copy_calls == 3
    assert [row[0] for row in connection.cursor_instance.copy_stream.rows] == [
        "cell-1",
        "cell-2",
        "cell-3",
        "cell-4",
        "cell-5",
    ]
    assert connection.commits == 1


def test_loads_grid_geometry_and_weather_point() -> None:
    geometry = box(0, 0, 1, 1).wkb
    grid = pa.table(
        {
            "grid_id": ["cell-1"],
            "geometry": [geometry],
            "centroid_latitude": [0.5],
            "centroid_longitude": [0.5],
            "area_km2": [1.0],
        }
    )
    weather = pa.table(
        {
            "weather_cell_id": ["weather-1"],
            "date": ["2023-09-01"],
            "latitude": [0.5],
            "longitude": [0.5],
            "elevation_m": [100.0],
            "temperature_mean_c": [25.0],
            "temperature_max_c": [30.0],
            "temperature_min_c": [20.0],
            "relative_humidity_pct": [70.0],
            "wind_speed_ms": [2.0],
            "solar_radiation_mj_m2_day": [20.0],
            "precipitation_mm_day": [1.0],
            "reference_evapotranspiration_mm_day": [4.0],
        }
    )

    grid_connection = FakeConnection()
    weather_connection = FakeConnection()
    assert load_dataset(grid_connection, "dim_spatial_grid", grid, processing_version="0.1.0") == 1
    assert (
        load_dataset(weather_connection, "weather_daily", weather, processing_version="0.1.0") == 1
    )
    assert isinstance(grid_connection.cursor_instance.copy_stream.rows[0][1], memoryview)
    assert any("ST_SetSRID" in query for query, _ in weather_connection.cursor_instance.executed)


def test_rejects_missing_columns_and_unknown_dataset() -> None:
    connection = FakeConnection()
    with pytest.raises(ValueError, match="missing columns"):
        load_dataset(
            connection,
            "crop_mask",
            pa.table({"grid_id": ["cell-1"]}),
            processing_version="0.1.0",
        )
    with pytest.raises(ValueError, match="Unsupported relational dataset"):
        load_dataset(connection, "unknown", pa.table({"id": [1]}), processing_version="0.1.0")


def test_database_cli_parser() -> None:
    args = build_parser().parse_args(
        ["--migrate", "--load", "--register-bronze", "--dataset", "crop_mask"]
    )

    assert args.migrate is True
    assert args.load is True
    assert args.register_bronze is True
    assert args.dataset == ["crop_mask"]


@pytest.mark.parametrize("legacy", [False, True])
def test_streams_gold_classification_and_accepts_v1_files(
    settings: Settings, tmp_path: Path, legacy: bool
) -> None:
    row = {
        "grid_id": "soy-cell",
        "week_start": date(2023, 8, 28),
        "week_end": date(2023, 9, 3),
        "water_stress_score": 0.75,
        "water_stress_class": "high",
        "water_stress_risk_class": "high",
        "risk_classification_version": "four-level-v1",
        "monitoring_guidance": "Priorizar avaliação das condições da área.",
    }
    table = pa.Table.from_pylist([row], schema=gold_schema(settings))
    if legacy:
        table = table.drop(
            ["water_stress_risk_class", "risk_classification_version", "monitoring_guidance"]
        )
    path = tmp_path / "gold.parquet"
    pq.write_table(table, path)
    connection = FakeConnection()

    assert (
        load_dataset_files(connection, "water_stress_weekly", [path], processing_version="0.1.0")
        == 1
    )
    copied = dict(
        zip(
            (*DATASET_COLUMNS["water_stress_weekly"], "processing_version"),
            connection.cursor_instance.copy_stream.rows[0],
            strict=True,
        )
    )
    assert copied["water_stress_score"] == 0.75
    assert copied["water_stress_class"] == "high"
    for name in ("water_stress_risk_class", "risk_classification_version", "monitoring_guidance"):
        assert copied[name] == (None if legacy else row[name])
    assert connection.commits == 1


def test_loads_gold_classification_from_in_memory_table(settings: Settings) -> None:
    table = pa.Table.from_pylist(
        [{"water_stress_risk_class": "attention", "risk_classification_version": "four-level-v1"}],
        schema=gold_schema(settings),
    )
    connection = FakeConnection()
    assert load_dataset(connection, "water_stress_weekly", table, processing_version="0.1.0") == 1
    copied = connection.cursor_instance.copy_stream.rows[0]
    assert copied[DATASET_COLUMNS["water_stress_weekly"].index("water_stress_risk_class")] == (
        "attention"
    )


@pytest.mark.parametrize(
    "dataset,schema",
    [("soil_hydraulics", HYDRAULIC_SCHEMA), ("water_stress_weekly_v2", WEEKLY_SCHEMA)],
)
def test_water_balance_reconciliation_is_scoped_and_transactional(
    dataset: str, schema: pa.Schema
) -> None:
    connection = FakeConnection()
    table = pa.Table.from_pylist([{"analysis_id": "pilot", "grid_id": "cell-1"}], schema=schema)
    assert load_dataset(connection, dataset, table, processing_version="test") == 1
    queries = [query for query, _ in connection.cursor_instance.executed]
    assert any(
        f"INSERT INTO gold.{dataset}" in query and "ON CONFLICT" in query for query in queries
    )
    deletion = next(query for query in queries if query.startswith("DELETE FROM"))
    assert "d.analysis_id IN" in deletion and "NOT EXISTS" in deletion
    assert all("TRUNCATE" not in query for query in queries)
    assert connection.commits == 1
    assert connection.rollbacks == 0
    empty = FakeConnection()
    with pytest.raises(ValueError, match="empty"):
        load_dataset(empty, dataset, table.slice(0, 0), processing_version="test")
    assert empty.rollbacks == 1
