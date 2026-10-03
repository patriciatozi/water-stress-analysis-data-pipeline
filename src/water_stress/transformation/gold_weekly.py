from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import fmean, median
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
from shapely.geometry import Point
from shapely.strtree import STRtree

from water_stress.config import Settings
from water_stress.transformation import (
    common,
    crop_mask,
    satellite_observation,
    soil_features,
    spatial_grid,
    weather_daily,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GoldWeeklyResult:
    dataset_path: Path
    parquet_paths: tuple[Path, ...]
    metadata_path: Path
    quality_path: Path
    row_count: int
    week_count: int


def _field(name: str, data_type: pa.DataType, unit: str, description: str) -> pa.Field[Any]:
    return pa.field(name, data_type, metadata={"unit": unit, "description": description})


def gold_schema(settings: Settings) -> pa.Schema:
    return pa.schema(
        [
            _field("grid_id", pa.string(), "identifier", "Spatial grid foreign key"),
            _field("week_start", pa.date32(), "date", "Monday starting the UTC analysis week"),
            _field("week_end", pa.date32(), "date", "Last study date in the analysis week"),
            _field("soy_fraction", pa.float64(), "fraction", "MapBiomas soybean fraction"),
            _field("clay_pct", pa.float64(), "%", "Soil clay content"),
            _field("sand_pct", pa.float64(), "%", "Soil sand content"),
            _field("soc", pa.float64(), "g/kg", "Soil organic carbon"),
            _field("bulk_density", pa.float64(), "g/cm^3", "Soil bulk density"),
            _field("precipitation_mm_7d", pa.float64(), "mm", "Weekly precipitation total"),
            _field("eto_mm_7d", pa.float64(), "mm", "Weekly reference evapotranspiration total"),
            _field("water_balance_mm_7d", pa.float64(), "mm", "Precipitation minus ETo"),
            _field("water_deficit_mm_7d", pa.float64(), "mm", "Positive ETo minus precipitation"),
            _field("rainy_day_count", pa.int16(), "days", "Days with positive precipitation"),
            _field("consecutive_dry_days", pa.int16(), "days", "Maximum observed dry-day run"),
            _field("temperature_mean_c", pa.float64(), "C", "Weekly mean air temperature"),
            _field("temperature_max_c", pa.float64(), "C", "Weekly mean daily maximum temperature"),
            _field("temperature_min_c", pa.float64(), "C", "Weekly mean daily minimum temperature"),
            _field(
                "weather_observation_count",
                pa.int16(),
                "observations",
                "Available daily weather rows",
            ),
            _field("ndvi_median", pa.float64(), "index", "Median weekly NDVI when available"),
            _field("ndmi_median", pa.float64(), "index", "Median weekly NDMI when available"),
            _field(
                "satellite_scene_count",
                pa.int16(),
                "scenes",
                "Sentinel-2 observations in the week",
            ),
            _field("satellite_valid_pixel_pct", pa.float64(), "%", "Mean valid-pixel percentage"),
            _field("satellite_cloud_pixel_pct", pa.float64(), "%", "Mean cloud-pixel percentage"),
            _field("satellite_observation_date", pa.date32(), "date", "Most recent scene date"),
            _field(
                "satellite_age_days",
                pa.int16(),
                "days",
                "Age of most recent scene at week end",
            ),
            _field(
                "water_stress_score",
                pa.float64(),
                "score",
                "Provisional explainable score from 0 to 1",
            ),
            _field(
                "water_stress_class",
                pa.string(),
                "category",
                "Provisional low, moderate or high risk",
            ),
            _field(
                "score_component_count",
                pa.int16(),
                "components",
                "Available score components",
            ),
        ],
        metadata={
            "source": "Silver thematic datasets",
            "layer": "gold",
            "dataset": "water_stress_weekly",
            "crs": settings.spatial.area_crs,
            "resolution_meters": str(settings.spatial.screening_grid_meters),
            "processing_version": settings.project.version,
            "temporal_grain": "weekly, Monday start",
        },
    )


def _week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _study_weeks(settings: Settings) -> list[tuple[date, date]]:
    first = _week_start(settings.study.start_date)
    weeks: list[tuple[date, date]] = []
    current = first
    while current <= settings.study.end_date:
        weeks.append((current, min(current + timedelta(days=6), settings.study.end_date)))
        current += timedelta(days=7)
    return weeks


def _required_float(value: object, label: str) -> float:
    if value is None:
        raise ValueError(f"Missing numeric value for {label}")
    return float(cast(Any, value))


def _read_parquet(path: Path, columns: list[str] | None = None) -> pa.Table:
    # Read the file directly. ``pq.read_table`` infers Hive partition columns
    # from parent directories (for example ``year=2023``), which can collide
    # with columns already persisted in the Parquet schema.
    return pq.ParquetFile(path).read(columns=columns)


def _read_partitioned(root: Path, pattern: str = "year=*/part-000.parquet") -> pa.Table:
    paths = sorted(root.glob(pattern))
    if not paths:
        raise FileNotFoundError(f"No Parquet partitions found under {root}")
    return pa.concat_tables([_read_parquet(path) for path in paths])


def _weather_by_week(table: pa.Table) -> dict[tuple[str, date], dict[str, Any]]:
    grouped: dict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    columns = {name: table[name].to_pylist() for name in table.column_names}
    for index, observation_date in enumerate(columns["date"]):
        if not isinstance(observation_date, date):
            continue
        key = (str(columns["weather_cell_id"][index]), _week_start(observation_date))
        grouped[key].append({name: values[index] for name, values in columns.items()})

    result: dict[tuple[str, date], dict[str, Any]] = {}
    for key, rows in grouped.items():
        precipitation = [
            float(row["precipitation_mm_day"])
            for row in rows
            if row["precipitation_mm_day"] is not None
        ]
        eto = [
            float(row["reference_evapotranspiration_mm_day"])
            for row in rows
            if row["reference_evapotranspiration_mm_day"] is not None
        ]
        balance = None if not precipitation or not eto else sum(precipitation) - sum(eto)
        result[key] = {
            "latitude": rows[0]["latitude"],
            "longitude": rows[0]["longitude"],
            "precipitation_mm_7d": sum(precipitation) if precipitation else None,
            "eto_mm_7d": sum(eto) if eto else None,
            "water_balance_mm_7d": balance,
            "water_deficit_mm_7d": None if balance is None else max(0.0, -balance),
            "rainy_day_count": sum(value > 0 for value in precipitation),
            "consecutive_dry_days": _maximum_dry_run(rows),
            "temperature_mean_c": _mean(rows, "temperature_mean_c"),
            "temperature_max_c": _mean(rows, "temperature_max_c"),
            "temperature_min_c": _mean(rows, "temperature_min_c"),
            "weather_observation_count": len(rows),
        }
    return result


def _mean(rows: Iterable[dict[str, Any]], column: str) -> float | None:
    values = [float(row[column]) for row in rows if row[column] is not None]
    return fmean(values) if values else None


def _maximum_dry_run(rows: list[dict[str, Any]]) -> int:
    maximum = current = 0
    for row in sorted(rows, key=lambda value: value["date"]):
        precipitation = row["precipitation_mm_day"]
        if precipitation is not None and float(precipitation) <= 0:
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0
    return maximum


def _nearest_weather_cells(grid: pa.Table, weather: pa.Table) -> dict[str, str]:
    weather_ids = [str(value) for value in weather["weather_cell_id"].to_pylist()]
    points = [
        Point(
            _required_float(longitude, "weather longitude"),
            _required_float(latitude, "weather latitude"),
        )
        for latitude, longitude in zip(
            weather["latitude"].to_pylist(), weather["longitude"].to_pylist(), strict=True
        )
    ]
    if not points:
        raise ValueError("weather_daily has no spatial cells")
    tree = STRtree(points)
    result: dict[str, str] = {}
    for grid_id, latitude, longitude in zip(
        grid["grid_id"].to_pylist(),
        grid["centroid_latitude"].to_pylist(),
        grid["centroid_longitude"].to_pylist(),
        strict=True,
    ):
        nearest = tree.nearest(
            Point(
                _required_float(longitude, "grid centroid longitude"),
                _required_float(latitude, "grid centroid latitude"),
            )
        )
        result[str(grid_id)] = weather_ids[int(nearest)]
    return result


def _satellite_by_grid(table: pa.Table) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    columns = {name: table[name].to_pylist() for name in table.column_names}
    for index, observation_date in enumerate(columns["date"]):
        if isinstance(observation_date, date):
            grouped[str(columns["grid_id"][index])].append(
                {name: values[index] for name, values in columns.items()}
            )
    return grouped


def _satellite_for_week(
    observations: dict[str, list[dict[str, Any]]],
    *,
    grid_id: str,
    week_end: date,
    max_age_days: int,
) -> dict[str, Any]:
    candidates = [
        row
        for row in observations.get(grid_id, [])
        if isinstance(row["date"], date)
        and row["date"] <= week_end
        and (week_end - row["date"]).days <= max_age_days
    ]
    if not candidates:
        return {}
    latest_date = max(row["date"] for row in candidates)
    rows = [row for row in candidates if row["date"] == latest_date]
    return {
        "ndvi_median": _median(rows, "ndvi_mean"),
        "ndmi_median": _median(rows, "ndmi_mean"),
        "satellite_scene_count": len(rows),
        "satellite_valid_pixel_pct": _mean(rows, "valid_pixel_pct"),
        "satellite_cloud_pixel_pct": _mean(rows, "cloud_pixel_pct"),
        "satellite_observation_date": latest_date,
    }


def _median(rows: Iterable[dict[str, Any]], column: str) -> float | None:
    values = [float(row[column]) for row in rows if row[column] is not None]
    return float(median(values)) if values else None


def _bounded_stress(value: float, reference: float) -> float:
    return max(0.0, min(1.0, value / reference))


def _lower_is_stress(value: float, threshold: float) -> float:
    denominator = max(abs(threshold), 1e-9)
    return max(0.0, min(1.0, (threshold - value) / denominator))


def _score(settings: Settings, values: dict[str, Any]) -> tuple[float | None, str | None, int]:
    components: list[tuple[float, float]] = []
    deficit = values.get("water_deficit_mm_7d")
    if deficit is not None:
        components.append(
            (
                settings.gold.deficit_weight,
                _bounded_stress(float(deficit), settings.gold.deficit_reference_mm),
            )
        )
    ndvi = values.get("ndvi_median")
    if ndvi is not None:
        components.append(
            (
                settings.gold.ndvi_weight,
                _lower_is_stress(float(ndvi), settings.gold.ndvi_stress_threshold),
            )
        )
    ndmi = values.get("ndmi_median")
    if ndmi is not None:
        components.append(
            (
                settings.gold.ndmi_weight,
                _lower_is_stress(float(ndmi), settings.gold.ndmi_stress_threshold),
            )
        )
    available_weight = sum(weight for weight, _ in components)
    if available_weight <= 0:
        return None, None, 0
    score = sum(weight * value for weight, value in components) / available_weight
    label = "low" if score < 1 / 3 else "moderate" if score < 2 / 3 else "high"
    return score, label, len(components)


def build_weekly_table(
    settings: Settings,
    *,
    grid: pa.Table,
    crop: pa.Table,
    soil: pa.Table,
    weather: pa.Table,
    satellite: pa.Table | None = None,
    week: tuple[date, date],
) -> pa.Table:
    if not {"grid_id", "centroid_latitude", "centroid_longitude"}.issubset(grid.column_names):
        raise ValueError("dim_spatial_grid lacks the required analytical columns")
    crop_by_grid = {
        str(grid_id): value
        for grid_id, value in zip(
            crop["grid_id"].to_pylist(), crop["soy_fraction"].to_pylist(), strict=True
        )
        if value is not None and float(value) >= settings.gold.soy_fraction_threshold
    }
    soil_columns = {name: soil[name].to_pylist() for name in soil.column_names}
    soil_by_grid = {
        str(grid_id): {name: values[index] for name, values in soil_columns.items()}
        for index, grid_id in enumerate(soil_columns["grid_id"])
        if str(grid_id) in crop_by_grid
    }
    weather_by_key = _weather_by_week(weather)
    weather_ids = _nearest_weather_cells(grid, weather)
    satellite_by_grid = _satellite_by_grid(satellite) if satellite is not None else {}
    week_start, week_end = week
    columns: dict[str, list[Any]] = {name: [] for name in gold_schema(settings).names}
    grid_ids = [str(value) for value in grid["grid_id"].to_pylist() if str(value) in crop_by_grid]
    for grid_id in grid_ids:
        weather_key = (weather_ids[grid_id], week_start)
        weather_values = weather_by_key.get(weather_key, {})
        satellite_values = _satellite_for_week(
            satellite_by_grid,
            grid_id=grid_id,
            week_end=week_end,
            max_age_days=settings.gold.satellite_max_age_days,
        )
        soil_values = soil_by_grid.get(grid_id, {})
        values: dict[str, Any] = {
            "grid_id": grid_id,
            "week_start": week_start,
            "week_end": week_end,
            "soy_fraction": crop_by_grid[grid_id],
            **{
                name: soil_values.get(name)
                for name in ("clay_pct", "sand_pct", "soc", "bulk_density")
            },
            **weather_values,
            **satellite_values,
        }
        observation_date = values.get("satellite_observation_date")
        values["satellite_age_days"] = (
            (week_end - observation_date).days if isinstance(observation_date, date) else None
        )
        score, label, component_count = _score(settings, values)
        values["water_stress_score"] = score
        values["water_stress_class"] = label
        values["score_component_count"] = component_count
        for name in columns:
            columns[name].append(values.get(name))
    return pa.table(columns, schema=gold_schema(settings))


def dataset_path(settings: Settings) -> Path:
    return (
        settings.storage.gold_root_path
        / "water_stress_weekly"
        / settings.study.partition_key
        / f"start_date={settings.study.start_date.isoformat()}"
        / f"end_date={settings.study.end_date.isoformat()}"
    )


def _satellite_table(settings: Settings) -> pa.Table | None:
    root = satellite_observation.dataset_path(settings)
    paths = sorted(root.rglob("*.parquet")) if root.is_dir() else []
    return pa.concat_tables([_read_parquet(path) for path in paths]) if paths else None


def transform(settings: Settings) -> GoldWeeklyResult:
    grid_path = spatial_grid.dataset_path(settings) / "grid.parquet"
    crop_path = crop_mask.dataset_path(settings) / "part-000.parquet"
    soil_path = soil_features.dataset_path(settings) / "part-000.parquet"
    weather_root = weather_daily.dataset_path(settings)
    common.require_files((grid_path, crop_path, soil_path))
    grid = _read_parquet(grid_path)
    crop = _read_parquet(crop_path)
    soil = _read_parquet(soil_path)
    weather = _read_partitioned(weather_root)
    satellite = _satellite_table(settings)
    output_root = dataset_path(settings)
    parquet_paths: list[Path] = []
    row_count = 0
    quality_reports: list[dict[str, Any]] = []
    for week in _study_weeks(settings):
        table = build_weekly_table(
            settings,
            grid=grid,
            crop=crop,
            soil=soil,
            weather=weather,
            satellite=satellite,
            week=week,
        )
        output = output_root / f"week_start={week[0].isoformat()}" / "part-000.parquet"
        common.write_parquet(output, table)
        parquet_paths.append(output)
        row_count += table.num_rows
        quality_reports.append(common.table_quality(table, ["grid_id", "week_start"]))

    metadata_path = output_root / "_metadata.json"
    quality_path = output_root / "_quality.json"
    metadata = {
        "dataset": "water_stress_weekly",
        "source": "Silver thematic datasets",
        "temporal_grain": "Monday-start week; first and last weeks may be partial",
        "soy_fraction_threshold": settings.gold.soy_fraction_threshold,
        "satellite_max_age_days": settings.gold.satellite_max_age_days,
        "water_balance_formula": "precipitation_mm_7d - eto_mm_7d",
        "water_deficit_formula": "max(0, -water_balance_mm_7d)",
        "score_formula": "weighted normalized deficit, NDVI and NDMI stress components",
        "score_status": "provisional; thresholds and weights require agronomic validation",
        "score_parameters": {
            "deficit_reference_mm": settings.gold.deficit_reference_mm,
            "ndvi_stress_threshold": settings.gold.ndvi_stress_threshold,
            "ndmi_stress_threshold": settings.gold.ndmi_stress_threshold,
            "deficit_weight": settings.gold.deficit_weight,
            "ndvi_weight": settings.gold.ndvi_weight,
            "ndmi_weight": settings.gold.ndmi_weight,
        },
        "consecutive_dry_days_rule": "daily precipitation <= 0; missing precipitation breaks a run",
        "processing_version": settings.project.version,
        "processed_at_utc": datetime.now().astimezone().isoformat(),
    }
    common.write_json(metadata_path, metadata)
    statuses = [str(report["quality_status"]) for report in quality_reports]
    quality_status = (
        "failed" if "failed" in statuses else "warning" if "warning" in statuses else "passed"
    )
    quality_issues = [
        f"week {index + 1}: {issue}"
        for index, report in enumerate(quality_reports)
        for issue in report["quality_issues"]
    ]
    common.write_json(
        quality_path,
        {
            **metadata,
            "quality_status": quality_status,
            "quality_issues": quality_issues,
            "row_count": row_count,
            "week_count": len(parquet_paths),
            "satellite_available": satellite is not None,
        },
    )
    LOGGER.info(
        "Gold weekly dataset written",
        extra={"dataset": "water_stress_weekly", "row_count": row_count},
    )
    return GoldWeeklyResult(
        output_root,
        tuple(parquet_paths),
        metadata_path,
        quality_path,
        row_count,
        len(parquet_paths),
    )
