from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from water_stress.config import Settings
from water_stress.transformation import (
    crop_mask,
    gold_weekly,
    satellite_observation,
    soil_features,
    spatial_grid,
    weather_daily,
)

DATASET_COLUMNS: dict[str, tuple[str, ...]] = {
    "dim_spatial_grid": (
        "grid_id",
        "geometry_wkb",
        "centroid_latitude",
        "centroid_longitude",
        "area_km2",
    ),
    "crop_mask": ("grid_id", "year", "soy_fraction"),
    "soil_features": ("grid_id", "clay_pct", "sand_pct", "soc", "bulk_density"),
    "weather_daily": (
        "weather_cell_id",
        "date",
        "latitude",
        "longitude",
        "elevation_m",
        "temperature_mean_c",
        "temperature_max_c",
        "temperature_min_c",
        "relative_humidity_pct",
        "wind_speed_ms",
        "solar_radiation_mj_m2_day",
        "precipitation_mm_day",
        "reference_evapotranspiration_mm_day",
    ),
    "satellite_observation": (
        "grid_id",
        "date",
        "tile_id",
        "item_id",
        "ndvi_mean",
        "ndvi_p10",
        "ndvi_p50",
        "ndvi_p90",
        "ndmi_mean",
        "ndmi_p10",
        "ndmi_p50",
        "ndmi_p90",
        "soy_pixel_count",
        "valid_pixel_count",
        "valid_pixel_pct",
        "cloud_pixel_pct",
    ),
    "water_stress_weekly": (
        "grid_id",
        "week_start",
        "week_end",
        "soy_fraction",
        "clay_pct",
        "sand_pct",
        "soc",
        "bulk_density",
        "precipitation_mm_7d",
        "eto_mm_7d",
        "water_balance_mm_7d",
        "water_deficit_mm_7d",
        "rainy_day_count",
        "consecutive_dry_days",
        "temperature_mean_c",
        "temperature_max_c",
        "temperature_min_c",
        "weather_observation_count",
        "ndvi_median",
        "ndmi_median",
        "satellite_scene_count",
        "satellite_valid_pixel_pct",
        "satellite_cloud_pixel_pct",
        "satellite_observation_date",
        "satellite_age_days",
        "water_stress_score",
        "water_stress_class",
        "score_component_count",
        "weather_cell_id",
        "weather_expected_days",
        "score_status",
        "score_available_weight",
    ),
}

PRIMARY_KEYS = {
    "dim_spatial_grid": ("grid_id",),
    "crop_mask": ("grid_id", "year"),
    "soil_features": ("grid_id",),
    "weather_daily": ("weather_cell_id", "date"),
    "satellite_observation": ("grid_id", "date", "tile_id", "item_id"),
    "water_stress_weekly": ("grid_id", "week_start"),
}

LOAD_BATCH_SIZE = 10_000


def source_paths(settings: Settings, dataset: str) -> tuple[Path, ...]:
    """Return the Parquet files for a relational dataset in stable order."""
    paths: dict[str, Iterable[Path]] = {
        "dim_spatial_grid": (spatial_grid.dataset_path(settings) / "grid.parquet",),
        "crop_mask": (crop_mask.dataset_path(settings) / "part-000.parquet",),
        "soil_features": (soil_features.dataset_path(settings) / "part-000.parquet",),
        "weather_daily": weather_daily.dataset_path(settings).glob("year=*/part-000.parquet"),
        "satellite_observation": satellite_observation.dataset_path(settings).rglob("*.parquet"),
        "water_stress_weekly": gold_weekly.dataset_path(settings).glob(
            "week_start=*/part-000.parquet"
        ),
    }
    if dataset not in paths:
        raise ValueError(f"Unsupported relational dataset: {dataset}")
    files = tuple(sorted(paths[dataset]))
    if not files:
        raise FileNotFoundError(f"No Parquet files found for database dataset {dataset}")
    return files


def register_bronze_manifests(connection: Any, root: Path) -> int:
    """Register Bronze lineage without copying raw payloads into PostgreSQL."""
    manifests = sorted(root.rglob("*.manifest.json"))
    count = 0
    with connection.cursor() as cursor:
        for path in manifests:
            document = json.loads(path.read_text(encoding="utf-8"))
            artifact_stem = path.name.removesuffix(".manifest.json")
            artifact_candidates = sorted(
                candidate
                for candidate in path.parent.iterdir()
                if candidate.is_file() and candidate != path and candidate.stem == artifact_stem
            )
            artifact_path = (
                artifact_candidates[0] if artifact_candidates else path.parent / artifact_stem
            )
            fingerprint = document.get("request_fingerprint")
            checksum = document.get("sha256")
            if not isinstance(fingerprint, str) or not isinstance(checksum, str):
                raise ValueError(f"Bronze manifest lacks request fingerprint or checksum: {path}")
            cursor.execute(
                """
                INSERT INTO bronze.artifact_manifest
                    (source, artifact_uri, request_fingerprint, source_url,
                     extracted_at_utc, status_http, size_bytes, sha256,
                     period_start, period_end, area_type, area_code,
                     processing_version, config_hash, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (request_fingerprint, sha256) DO NOTHING
                """,
                (
                    document.get("source", "unknown"),
                    str(artifact_path),
                    fingerprint,
                    document.get("final_url") or document.get("url"),
                    document.get("downloaded_at_utc"),
                    document.get("http_status"),
                    int(document.get("size_bytes", 0)),
                    checksum,
                    document.get("study_start_date"),
                    document.get("study_end_date"),
                    document.get("area_type"),
                    document.get("area_code"),
                    document.get("project_version", "unknown"),
                    document.get("config_sha256"),
                    json.dumps(document),
                ),
            )
            count += 1
    connection.commit()
    return count


def _rows(table: pa.Table, columns: tuple[str, ...]) -> Iterable[tuple[object, ...]]:
    values = {name: table[name].to_pylist() for name in table.column_names}
    for index in range(table.num_rows):
        yield tuple(values[column][index] for column in columns)


def _copy_rows(
    connection: Any, table_name: str, columns: tuple[str, ...], rows: Iterable[tuple[object, ...]]
) -> None:
    quoted_columns = ", ".join(columns)
    with (
        connection.cursor() as cursor,
        cursor.copy(f"COPY {table_name} ({quoted_columns}) FROM STDIN") as copy,
    ):
        for row in rows:
            copy.write_row(row)


def _copy_batch(
    connection: Any,
    dataset: str,
    batch: pa.RecordBatch,
    processing_version: str,
) -> None:
    columns = DATASET_COLUMNS[dataset]
    values = batch.to_pylist()
    rows: Iterable[tuple[object, ...]]
    if dataset == "dim_spatial_grid":
        rows = (
            (
                row["grid_id"],
                memoryview(row["geometry"]),
                row["centroid_latitude"],
                row["centroid_longitude"],
                row["area_km2"],
                processing_version,
            )
            for row in values
        )
    else:
        rows = (
            (*tuple(row.get(column) for column in columns), processing_version) for row in values
        )
    _copy_rows(connection, f"_stage_{dataset}", (*columns, "processing_version"), rows)


def _merge_stage(connection: Any, dataset: str) -> None:
    columns = DATASET_COLUMNS[dataset]
    if dataset == "dim_spatial_grid":
        sql = """
            INSERT INTO silver.dim_spatial_grid
                (grid_id, geometry, centroid_latitude, centroid_longitude,
                 area_km2, processing_version)
            SELECT grid_id, ST_GeomFromWKB(geometry_wkb, 5880), centroid_latitude,
                   centroid_longitude, area_km2, processing_version
            FROM _stage_dim_spatial_grid
            ON CONFLICT (grid_id) DO UPDATE SET
                geometry = EXCLUDED.geometry,
                centroid_latitude = EXCLUDED.centroid_latitude,
                centroid_longitude = EXCLUDED.centroid_longitude,
                area_km2 = EXCLUDED.area_km2,
                processing_version = EXCLUDED.processing_version,
                loaded_at_utc = now()
        """
    else:
        destination = (
            f"silver.{dataset}" if dataset != "water_stress_weekly" else "gold.water_stress_weekly"
        )
        updates = ", ".join(
            f"{column} = EXCLUDED.{column}"
            for column in (*columns, "processing_version")
            if column not in PRIMARY_KEYS[dataset]
        )
        keys = ", ".join(PRIMARY_KEYS[dataset])
        target_columns = (*columns, "processing_version")
        if dataset == "weather_daily":
            target_columns = (*target_columns, "geometry")
            select_sql = (
                f"SELECT {', '.join(columns)}, processing_version, "
                f"ST_SetSRID(ST_MakePoint(longitude, latitude), 4326) FROM _stage_{dataset}"
            )
        else:
            select_sql = f"SELECT {', '.join(target_columns)} FROM _stage_{dataset}"
        sql = f"""
            INSERT INTO {destination} ({", ".join(target_columns)})
            {select_sql}
            ON CONFLICT ({keys}) DO UPDATE SET {updates}, loaded_at_utc = now()
        """
    with connection.cursor() as cursor:
        cursor.execute(sql)


def _begin_run(connection: Any, dataset: str) -> uuid.UUID:
    run_id = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO control.load_run (run_id, dataset, started_at_utc, status)
            VALUES (%s, %s, %s, 'running')
            """,
            (run_id, dataset, datetime.now(UTC)),
        )
    return run_id


def _finish_run(
    connection: Any,
    run_id: uuid.UUID,
    dataset: str,
    row_count: int,
    processing_version: str,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE control.load_run
            SET finished_at_utc = %s, status = 'succeeded', row_count = %s
            WHERE run_id = %s
            """,
            (datetime.now(UTC), row_count, run_id),
        )
        cursor.execute(
            """
            INSERT INTO control.dataset_load
                (run_id, dataset, processing_version, row_count)
            VALUES (%s, %s, %s, %s)
            """,
            (run_id, dataset, processing_version, row_count),
        )


def _validate_parquet_files(dataset: str, paths: tuple[Path, ...]) -> None:
    expected = set(DATASET_COLUMNS[dataset]) - {"geometry_wkb"}
    for path in paths:
        names = set(pq.ParquetFile(path).schema_arrow.names)
        missing = expected - names
        if missing:
            raise ValueError(f"Dataset {dataset} is missing columns in {path}: {sorted(missing)}")


def _create_stage(connection: Any, dataset: str) -> None:
    with connection.cursor() as cursor:
        stage = f"_stage_{dataset}"
        if dataset == "dim_spatial_grid":
            cursor.execute(
                """
                CREATE TEMP TABLE _stage_dim_spatial_grid (
                    grid_id text,
                    geometry_wkb bytea,
                    centroid_latitude double precision,
                    centroid_longitude double precision,
                    area_km2 double precision,
                    processing_version text
                ) ON COMMIT DROP
                """
            )
        else:
            destination = (
                f"silver.{dataset}"
                if dataset != "water_stress_weekly"
                else "gold.water_stress_weekly"
            )
            cursor.execute(
                f"CREATE TEMP TABLE {stage} (LIKE {destination} INCLUDING DEFAULTS) ON COMMIT DROP"
            )
            if dataset == "weather_daily":
                cursor.execute(f"ALTER TABLE {stage} ALTER COLUMN geometry DROP NOT NULL")


def _load_batches(
    connection: Any,
    dataset: str,
    batches: Iterable[pa.RecordBatch],
    *,
    processing_version: str,
) -> int:
    run_id = _begin_run(connection, dataset)
    try:
        _create_stage(connection, dataset)
        count = 0
        for batch in batches:
            _copy_batch(connection, dataset, batch, processing_version)
            count += batch.num_rows
        _merge_stage(connection, dataset)
        _finish_run(connection, run_id, dataset, count, processing_version)
        connection.commit()
        return count
    except Exception as exc:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE control.load_run SET status = 'failed', finished_at_utc = %s, "
                "error_message = %s WHERE run_id = %s",
                (datetime.now(UTC), str(exc), run_id),
            )
        connection.commit()
        raise


def load_dataset(
    connection: Any,
    dataset: str,
    table: pa.Table,
    *,
    processing_version: str,
) -> int:
    if dataset not in DATASET_COLUMNS:
        raise ValueError(f"Unsupported relational dataset: {dataset}")
    expected = set(DATASET_COLUMNS[dataset]) - {"geometry_wkb"}
    missing = expected - set(table.column_names)
    if missing:
        raise ValueError(f"Dataset {dataset} is missing columns: {sorted(missing)}")
    return _load_batches(
        connection,
        dataset,
        table.to_batches(max_chunksize=LOAD_BATCH_SIZE),
        processing_version=processing_version,
    )


def load_dataset_files(
    connection: Any,
    dataset: str,
    paths: Iterable[Path],
    *,
    processing_version: str,
    batch_size: int = LOAD_BATCH_SIZE,
) -> int:
    """Stream Parquet row groups to PostgreSQL with bounded client memory."""
    if dataset not in DATASET_COLUMNS:
        raise ValueError(f"Unsupported relational dataset: {dataset}")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    files = tuple(sorted(paths))
    if not files:
        raise FileNotFoundError(f"No Parquet files found for database dataset {dataset}")
    _validate_parquet_files(dataset, files)

    def batches() -> Iterable[pa.RecordBatch]:
        for path in files:
            yield from pq.ParquetFile(path).iter_batches(batch_size=batch_size)

    return _load_batches(
        connection,
        dataset,
        batches(),
        processing_version=processing_version,
    )
