from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq

from water_stress.config import Settings
from water_stress.ingestion import nasa_power

SOURCE_TO_SILVER = {
    "T2M": ("temperature_mean_c", "C", "Mean air temperature at 2 meters"),
    "T2M_MAX": ("temperature_max_c", "C", "Maximum air temperature at 2 meters"),
    "T2M_MIN": ("temperature_min_c", "C", "Minimum air temperature at 2 meters"),
    "RH2M": ("relative_humidity_pct", "%", "Relative humidity at 2 meters"),
    "WS2M": ("wind_speed_ms", "m/s", "Wind speed at 2 meters"),
    "ALLSKY_SFC_SW_DWN": (
        "solar_radiation_mj_m2_day",
        "MJ/m^2/day",
        "All-sky surface shortwave downward irradiance",
    ),
    "PRECTOTCORR": ("precipitation_mm_day", "mm/day", "Corrected precipitation"),
}


class DuplicateDateError(ValueError):
    """Raised when duplicate dates are found in the Bronze payload or Silver rows."""


@dataclass(frozen=True)
class SilverResult:
    dataset_path: Path
    parquet_paths: tuple[Path, ...]
    schema_path: Path
    quality_path: Path
    row_count: int
    missing_by_column: dict[str, int]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            if len(key) == 8 and key.isdigit():
                raise DuplicateDateError(f"Duplicate date in NASA POWER Bronze payload: {key}")
            raise ValueError(f"Duplicate JSON key in NASA POWER Bronze payload: {key}")
        result[key] = value
    return result


def parse_bronze(content: bytes) -> dict[str, Any]:
    try:
        document = json.loads(content, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("NASA POWER Bronze payload is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("NASA POWER Bronze payload must be a JSON object")
    parameters = document.get("properties", {}).get("parameter")
    if not isinstance(parameters, dict) or not parameters:
        raise ValueError("NASA POWER Bronze payload has no parameter data")
    return document


def _date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _coordinates(document: dict[str, Any]) -> tuple[float, float]:
    coordinates = document.get("geometry", {}).get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        raise ValueError("NASA POWER Bronze payload has no valid point coordinates")
    return float(coordinates[1]), float(coordinates[0])


def _field(name: str, data_type: pa.DataType, unit: str, description: str) -> pa.Field[Any]:
    return pa.field(
        name,
        data_type,
        metadata={"unit": unit, "description": description},
    )


def silver_schema() -> pa.Schema:
    fields = [
        _field("date", pa.date32(), "date", "Observation date in UTC"),
        _field("latitude", pa.float64(), "degrees_north", "NASA POWER point latitude"),
        _field("longitude", pa.float64(), "degrees_east", "NASA POWER point longitude"),
    ]
    fields.extend(
        _field(column_name, pa.float64(), unit, description)
        for column_name, unit, description in SOURCE_TO_SILVER.values()
    )
    return pa.schema(fields, metadata={"source": "NASA POWER Daily API", "layer": "silver"})


def transform_document(settings: Settings, document: dict[str, Any]) -> pa.Table:
    latitude, longitude = _coordinates(document)
    source_parameters = document["properties"]["parameter"]
    parameter_metadata = document.get("parameters", {})
    expected_dates = _date_range(settings.study.start_date, settings.study.end_date)
    expected_keys = {value.strftime("%Y%m%d") for value in expected_dates}

    columns: dict[str, list[Any]] = {
        "date": expected_dates,
        "latitude": [latitude] * len(expected_dates),
        "longitude": [longitude] * len(expected_dates),
    }
    for source_name, (column_name, expected_unit, _) in SOURCE_TO_SILVER.items():
        values = source_parameters.get(source_name)
        if not isinstance(values, dict):
            values = {}
        unexpected_dates = set(values) - expected_keys
        if unexpected_dates:
            raise ValueError(
                f"NASA POWER parameter {source_name} contains dates outside the study period: "
                f"{sorted(unexpected_dates)}"
            )
        actual_unit = parameter_metadata.get(source_name, {}).get("units")
        if actual_unit is not None and actual_unit != expected_unit:
            raise ValueError(
                f"Unexpected unit for {source_name}: expected {expected_unit}, got {actual_unit}"
            )
        normalized: list[float | None] = []
        for observation_date in expected_dates:
            raw_value = values.get(observation_date.strftime("%Y%m%d"))
            normalized.append(
                None if raw_value is None or float(raw_value) == -999.0 else float(raw_value)
            )
        columns[column_name] = normalized

    table = pa.table(columns, schema=silver_schema())
    dates = table.column("date").to_pylist()
    if len(dates) != len(set(dates)):
        raise DuplicateDateError("Duplicate dates found in NASA POWER Silver table")
    return table


def missing_counts(table: pa.Table) -> dict[str, int]:
    return {name: table.column(name).null_count for name in table.column_names}


def dataset_path(settings: Settings) -> Path:
    return settings.storage.silver_root_path / "nasa_power" / "daily" / settings.study.partition_key


def _schema_document(schema: pa.Schema) -> dict[str, Any]:
    return {
        "dataset": "nasa_power_daily",
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


def write_partitioned(
    settings: Settings,
    table: pa.Table,
    *,
    source_path: Path,
) -> SilverResult:
    root = dataset_path(settings)
    root.mkdir(parents=True, exist_ok=True)
    raw_dates = table.column("date").to_pylist()
    if any(value is None for value in raw_dates):
        raise ValueError("NASA POWER Silver table contains a null date")
    dates = cast(list[date], raw_dates)
    years = sorted({value.year for value in dates})
    parquet_paths: list[Path] = []
    for year in years:
        indices = [index for index, value in enumerate(dates) if value.year == year]
        partition = table.take(pa.array(indices, type=pa.int64()))
        output = root / f"year={year}" / "part-000.parquet"
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".parquet.tmp")
        pq.write_table(partition, temporary, compression="zstd")
        temporary.replace(output)
        parquet_paths.append(output)

    schema_path = root / "_schema.json"
    quality_path = root / "_quality.json"
    counts = missing_counts(table)
    schema_path.write_text(
        json.dumps(_schema_document(table.schema), ensure_ascii=False, indent=2) + "\n"
    )
    quality_path.write_text(
        json.dumps(
            {
                "dataset": "nasa_power_daily",
                "source_path": str(source_path),
                "row_count": table.num_rows,
                "duplicate_date_count": 0,
                "missing_by_column": counts,
                "study_start_date": settings.study.start_date.isoformat(),
                "study_end_date": settings.study.end_date.isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    return SilverResult(
        dataset_path=root,
        parquet_paths=tuple(parquet_paths),
        schema_path=schema_path,
        quality_path=quality_path,
        row_count=table.num_rows,
        missing_by_column=counts,
    )


def transform(settings: Settings) -> SilverResult:
    source_path = nasa_power.artifact_path(settings)
    if not source_path.is_file():
        raise FileNotFoundError(
            f"NASA POWER Bronze artifact not found at {source_path}; run ingestion first"
        )
    document = parse_bronze(source_path.read_bytes())
    table = transform_document(settings, document)
    return write_partitioned(settings, table, source_path=source_path)
