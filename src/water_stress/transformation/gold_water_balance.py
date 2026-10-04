from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from water_stress.config import Settings
from water_stress.risk_classification import RISK_CLASSIFICATION_POLICY, classify_score
from water_stress.transformation import common, gold_weekly, weather_daily
from water_stress.transformation.water_balance_schema import (
    HYDRAULIC_SCHEMA,
    STATE_SCHEMA,
    WEEKLY_SCHEMA,
)
from water_stress.water_balance import (
    SCORE_METHOD,
    SOIL_METHOD,
    combined_score,
    crop_coefficient,
    daily_balance,
    hydraulic_properties,
)

LOGGER = logging.getLogger(__name__)
SOURCE_COLUMNS = (
    "grid_id",
    "soy_fraction",
    "weather_cell_id",
    "clay_pct",
    "sand_pct",
    "soc",
    "ndvi_median",
    "ndmi_median",
    "satellite_age_days",
    "water_stress_score",
    "score_status",
    "week_start",
    "week_end",
)


@dataclass(frozen=True)
class WeatherDay:
    precipitation: float | None
    eto: float | None


@dataclass
class WaterState:
    grid_id: str
    water_mm: float | None = None
    initialized: bool = False
    run_days: int = 0
    issue: str | None = None


@dataclass(frozen=True)
class WaterBalanceResult:
    dataset_path: Path
    metadata_path: Path
    quality_path: Path
    row_count: int
    cell_count: int
    analysis_id: str


def dataset_path(settings: Settings, max_cells: int | None = None) -> Path:
    scope = "full" if max_cells is None else f"pilot-{max_cells}"
    return (
        settings.storage.gold_root_path
        / "water_balance"
        / settings.study.partition_key
        / f"start_date={settings.study.start_date}"
        / f"end_date={settings.study.end_date}"
        / f"scope={scope}"
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def published_paths(
    settings: Settings, dataset: str, max_cells: int | None = None
) -> tuple[Path, ...]:
    """Use only the complete publication manifest; checkpoints and old runs are not datasets."""
    root = dataset_path(settings, max_cells)
    manifest = root / "_metadata.json"
    if not manifest.is_file():
        raise FileNotFoundError(
            f"Generate gold-water-balance first; no complete manifest at {manifest}"
        )
    document = json.loads(manifest.read_text())
    quality = json.loads((root / "_quality.json").read_text())
    if quality["model_signature"] != document["model_signature"]:
        raise ValueError("Publication metadata and quality report disagree; rerun transformation")
    if document["parameters"] != settings.water_balance.model_dump(mode="json") or document[
        "score_parameters"
    ] != settings.gold.model_dump(mode="json"):
        raise ValueError("Published parameters differ from configuration; rerun transformation")
    paths = []
    for entry in document["files"][dataset]:
        path = root / entry["path"]
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Publication paths must stay within the dataset root")
        if not path.is_file() or _checksum(path) != entry["sha256"]:
            raise ValueError(
                f"Incomplete or corrupted published output: {path}; rerun the transformation"
            )
        paths.append(path)
    if not paths:
        raise ValueError("Cannot publish an empty water-balance dataset")
    return tuple(paths)


def _weather(settings: Settings) -> dict[tuple[str, date], WeatherDay]:
    result: dict[tuple[str, date], WeatherDay] = {}
    files = sorted(weather_daily.dataset_path(settings).glob("year=*/part-000.parquet"))
    if not files:
        raise FileNotFoundError("Silver weather_daily is missing; reuse the existing local dataset")
    for path in files:
        for batch in pq.ParquetFile(path).iter_batches(
            batch_size=10000,
            columns=[
                "weather_cell_id",
                "date",
                "precipitation_mm_day",
                "reference_evapotranspiration_mm_day",
            ],
        ):
            for row in batch.to_pylist():
                day = row["date"]
                if not isinstance(day, date):
                    raise ValueError("weather_daily has invalid dates")
                if not settings.study.start_date <= day <= settings.study.end_date:
                    continue
                key = (str(row["weather_cell_id"]), day)
                if key in result:
                    raise ValueError("weather_daily has duplicate cell/date keys")
                rain, eto = row["precipitation_mm_day"], row["reference_evapotranspiration_mm_day"]
                if any(
                    value is not None and (not isfinite(value) or value < 0)
                    for value in (rain, eto)
                ):
                    raise ValueError("weather_daily rain and ETo must be finite and nonnegative")
                result[key] = WeatherDay(rain, eto)
    return result


def _week_values(
    settings: Settings,
    row: dict[str, Any],
    state: WaterState,
    capacity: float | None,
    fraction: float,
    weather: dict[tuple[str, date], WeatherDay],
) -> dict[str, Any]:
    """Carry state across weeks; a missing active day invalidates all subsequent state."""
    days = []
    current = max(row["week_start"], settings.study.start_date)
    while current <= min(row["week_end"], settings.study.end_date):
        coefficient = crop_coefficient(current, settings.water_balance)
        if coefficient is not None:
            days.append((current, coefficient))
        current += timedelta(days=1)
    values: dict[str, Any] = {
        "active_day_count": len(days),
        "valid_day_count": 0,
        "crop_stage": days[-1][1][0] if days else None,
    }
    if not days:
        values["score_issue"] = "outside_scenario_cycle"
        return values
    if capacity is None:
        values["score_issue"] = "invalid_soil"
        return values
    if not state.initialized:
        state.water_mm = capacity * fraction
        state.initialized = True
    values["water_start_mm"] = state.water_mm
    balances = []
    week_run = maximum = stress_days = 0
    for day, (_, kc) in days:
        observation = weather.get((row["weather_cell_id"], day))
        if observation is None or observation.precipitation is None or observation.eto is None:
            state.water_mm = None
            state.issue = "weather_gap"
        if state.water_mm is None:
            state.run_days = 0
            continue
        assert (
            observation is not None
            and observation.precipitation is not None
            and observation.eto is not None
        )
        balance = daily_balance(
            state.water_mm,
            capacity,
            observation.precipitation,
            observation.eto,
            kc,
            settings.water_balance,
        )
        state.water_mm = balance.available_water_mm
        balances.append(balance)
        if balance.stress > 0:
            stress_days += 1
            week_run += 1
            state.run_days += 1
            maximum = max(maximum, week_run)
        else:
            week_run = state.run_days = 0
    values["valid_day_count"] = len(balances)
    values["water_end_mm"] = state.water_mm
    if len(balances) != len(days):
        values["score_issue"] = state.issue or "invalid_state"
        return values
    values.update(
        {
            "etc_potential_mm": sum(value.etc_potential_mm for value in balances),
            "etc_adjusted_mm": sum(value.etc_adjusted_mm for value in balances),
            "retained_rain_mm": sum(value.retained_rain_mm for value in balances),
            "drainage_mm": sum(value.drainage_mm for value in balances),
            "water_stress_mean": sum(value.stress for value in balances) / len(days),
            "water_stress_max": max(value.stress for value in balances),
            "stress_day_count": stress_days,
            "maximum_stress_run_days": maximum,
            "ongoing_stress_run_days": state.run_days,
        }
    )
    return values


def _weekly_table(
    settings: Settings,
    rows: list[dict[str, Any]],
    states: list[WaterState],
    capacities: list[float | None],
    fraction: float,
    weather: dict[tuple[str, date], WeatherDay],
    analysis_id: str,
    signature: str,
) -> pa.Table:
    output = []
    for row, state, capacity in zip(rows, states, capacities, strict=True):
        values = _week_values(settings, row, state, capacity, fraction, weather)
        ndvi, ndmi = row["ndvi_median"], row["ndmi_median"]
        age = row["satellite_age_days"]
        if age is None or not 0 <= age <= settings.gold.satellite_max_age_days:
            ndvi = ndmi = None
        score = combined_score(values.get("water_stress_mean"), ndvi, ndmi, settings.gold)
        band = classify_score(score.score)
        inactive = values["active_day_count"] == 0
        output.append(
            {
                **values,
                "analysis_id": analysis_id,
                "model_signature": signature,
                "grid_id": row["grid_id"],
                "scenario_id": f"initial-{fraction:g}",
                "initial_water_fraction": fraction,
                "week_start": row["week_start"],
                "week_end": row["week_end"],
                "soy_fraction": row["soy_fraction"],
                "weather_cell_id": row["weather_cell_id"],
                "score_method_version": SCORE_METHOD,
                "ndvi_median": ndvi,
                "ndmi_median": ndmi,
                "satellite_age_days": age,
                "water_stress_score_v1": row["water_stress_score"],
                "score_status_v1": row["score_status"],
                "water_stress_score": score.score,
                "score_status": "not_applicable" if inactive else score.status,
                "score_available_weight": score.available_weight,
                "score_component_count": score.component_count,
                "water_stress_risk_class": band.risk_class.value if band else None,
                "risk_classification_version": RISK_CLASSIFICATION_POLICY.version,
                "monitoring_guidance": (
                    "Fora do ciclo de cultivo do cenário."
                    if inactive
                    else band.monitoring_guidance
                    if band
                    else RISK_CLASSIFICATION_POLICY.missing_score_guidance
                ),
            }
        )
    return pa.Table.from_pylist(output, schema=WEEKLY_SCHEMA)


def _reusable(output: Path, state_path: Path, checkpoint: Path, signature: str) -> bool:
    if not all(path.is_file() for path in (output, state_path, checkpoint)):
        return False
    try:
        saved = json.loads(checkpoint.read_text())
        return bool(
            saved["signature"] == signature
            and saved["output_checksum"] == _checksum(output)
            and saved["state_checksum"] == _checksum(state_path)
        )
    except (ValueError, KeyError):
        return False


def transform(settings: Settings, *, max_cells: int | None = None) -> WaterBalanceResult:
    if max_cells is not None and max_cells < 1:
        raise ValueError("max_cells must be positive")
    if settings.gold.deficit_weight <= 0:
        raise ValueError("The v2 hydrological component must have a positive weight")
    cycle_end = settings.water_balance.planting_date + timedelta(
        days=sum(settings.water_balance.stage_days) - 1
    )
    if settings.water_balance.planting_date < settings.study.start_date <= cycle_end:
        raise ValueError("The study must include planting day to initialize the water reservoir")
    weeks = gold_weekly._study_weeks(settings)
    inputs = [
        gold_weekly.dataset_path(settings) / f"week_start={start}" / "part-000.parquet"
        for start, _ in weeks
    ]
    common.require_files(inputs)
    expected_schema = gold_weekly.gold_schema(settings)
    for path in inputs:
        schema = pq.ParquetFile(path).schema_arrow
        if any(
            name not in schema.names or schema.field(name).type != expected_schema.field(name).type
            for name in SOURCE_COLUMNS
        ):
            raise ValueError(
                f"Gold v1 has incompatible columns/types in {path}; regenerate Gold v1"
            )
    weather_paths = sorted(weather_daily.dataset_path(settings).glob("year=*/part-000.parquet"))
    lineage = {
        str(
            path.relative_to(
                settings.storage.gold_root_path
                if path in inputs
                else settings.storage.silver_root_path
            )
        ): _checksum(path)
        for path in [*inputs, *weather_paths]
    }
    root = dataset_path(settings, max_cells)
    analysis_id = _digest(
        {
            "study": settings.study.model_dump(mode="json"),
            "scope": root.name,
            "resolution": settings.spatial.screening_grid_meters,
        }
    )
    signature = _digest(
        {
            "inputs": lineage,
            "water_balance": settings.water_balance.model_dump(mode="json"),
            "gold": settings.gold.model_dump(mode="json"),
            "model": SCORE_METHOD,
            "contract": "surface-water-balance-v1",
            "soil": SOIL_METHOD,
            "classification": asdict(RISK_CLASSIFICATION_POLICY),
            "analysis_id": analysis_id,
        }
    )
    weather = _weather(settings)
    iterators = [
        pq.ParquetFile(path).iter_batches(
            batch_size=settings.water_balance.block_size, columns=list(SOURCE_COLUMNS)
        )
        for path in inputs
    ]
    files: dict[str, list[dict[str, str]]] = {"soil_hydraulics": [], "water_stress_weekly_v2": []}
    counts: Counter[str] = Counter()
    cell_count = row_count = 0
    seen_ids: set[str] = set()
    for block, batches in enumerate(zip(*iterators, strict=True)):
        if max_cells is not None and cell_count >= max_cells:
            break
        tables = [batch.to_pylist() for batch in batches]
        if max_cells is not None:
            tables = [rows[: max_cells - cell_count] for rows in tables]
        first = tables[0]
        ids = [row["grid_id"] for row in first]
        if any(not isinstance(grid_id, str) or not grid_id for grid_id in ids):
            raise ValueError("Gold v1 has invalid grid identifiers")
        if len(ids) != len(set(ids)) or seen_ids.intersection(ids):
            raise ValueError("Gold v1 has duplicate grid/week keys")
        seen_ids.update(ids)
        for (start, end), rows in zip(weeks, tables, strict=True):
            if [row["grid_id"] for row in rows] != ids or any(
                row["week_start"] != start or row["week_end"] != end for row in rows
            ):
                raise ValueError(
                    "Gold v1 weekly partitions must have matching ordered cells and study weeks"
                )
            for row, reference in zip(rows, first, strict=True):
                if (
                    not row["weather_cell_id"]
                    or row["weather_cell_id"] != reference["weather_cell_id"]
                ):
                    raise ValueError(
                        "Regenerate Gold v1 with valid, stable weather_cell_id associations"
                    )
                for name in ("soy_fraction", "clay_pct", "sand_pct", "soc"):
                    if row[name] != reference[name]:
                        raise ValueError(
                            f"Gold v1 has inconsistent static {name}; regenerate Gold v1"
                        )
                if row["soy_fraction"] is None or not (
                    settings.gold.soy_fraction_threshold <= row["soy_fraction"] <= 1
                ):
                    raise ValueError("Gold v1 soybean selection differs from current configuration")
        cell_count += len(first)
        block_root = root / "runs" / signature / f"block_id={block:06d}"
        hydraulics = []
        capacities: list[float | None] = []
        for row in first:
            values: dict[str, Any] = {
                "analysis_id": analysis_id,
                "grid_id": row["grid_id"],
                "model_signature": signature,
                "soil_method": SOIL_METHOD,
            }
            try:
                if any(row[name] is None for name in ("sand_pct", "clay_pct", "soc")):
                    raise ValueError("Missing sand, clay or organic carbon")
                soil = hydraulic_properties(
                    row["sand_pct"], row["clay_pct"], row["soc"], settings.water_balance
                )
                values.update(
                    {
                        "field_capacity": soil.field_capacity,
                        "wilting_point": soil.wilting_point,
                        "available_water_mm": soil.available_water_mm,
                        "organic_matter_pct": soil.organic_matter_pct,
                        "soil_status": "valid",
                    }
                )
                capacities.append(soil.available_water_mm)
            except ValueError as exc:
                values.update({"soil_status": "invalid", "soil_issue": str(exc)})
                capacities.append(None)
            hydraulics.append(values)
        hydro_path = block_root / "soil_hydraulics" / "part-000.parquet"
        hydro_table = pa.Table.from_pylist(hydraulics, schema=HYDRAULIC_SCHEMA)
        try:
            reuse_soil = hydro_path.is_file() and pq.ParquetFile(hydro_path).read().equals(
                hydro_table
            )
        except (pa.ArrowInvalid, OSError):
            reuse_soil = False
        if not reuse_soil:
            common.write_parquet(hydro_path, hydro_table)
        files["soil_hydraulics"].append(
            {"path": str(hydro_path.relative_to(root)), "sha256": _checksum(hydro_path)}
        )
        for fraction in settings.water_balance.initial_water_fractions:
            states = [WaterState(grid_id) for grid_id in ids]
            previous = signature
            invalidated = False
            for (start, _), rows in zip(weeks, tables, strict=True):
                partition = block_root / f"scenario_id=initial-{fraction:g}" / f"week_start={start}"
                output, state_path, checkpoint = (
                    partition / name
                    for name in ("part-000.parquet", "_state.parquet", "_checkpoint.json")
                )
                chain = _digest({"previous": previous, "week": str(start), "fraction": fraction})
                reused = not invalidated and _reusable(output, state_path, checkpoint, chain)
                if reused:
                    states = [
                        WaterState(**value)
                        for value in pq.ParquetFile(state_path).read().to_pylist()
                    ]
                    report = json.loads(checkpoint.read_text())
                else:
                    invalidated = True
                    table = _weekly_table(
                        settings,
                        rows,
                        states,
                        capacities,
                        fraction,
                        weather,
                        analysis_id,
                        signature,
                    )
                    common.write_parquet(output, table)
                    common.write_parquet(
                        state_path,
                        pa.Table.from_pylist(
                            [asdict(state) for state in states], schema=STATE_SCHEMA
                        ),
                    )
                    report = {
                        "signature": chain,
                        "output_checksum": _checksum(output),
                        "state_checksum": _checksum(state_path),
                        "row_count": table.num_rows,
                        "statuses": dict(Counter(table["score_status"].to_pylist())),
                    }
                    common.write_json(checkpoint, report)
                previous = _digest(report)
                counts.update(report["statuses"])
                row_count += report["row_count"]
                files["water_stress_weekly_v2"].append(
                    {"path": str(output.relative_to(root)), "sha256": report["output_checksum"]}
                )
            LOGGER.info(
                "Surface water balance block processed",
                extra={
                    "source": "local_gold_silver",
                    "operation": "water_balance",
                    "partition": f"{block:06d}/initial-{fraction:g}",
                    "outcome": "written" if invalidated else "reused",
                    "row_count": len(first) * len(weeks),
                },
            )
    if not cell_count:
        raise ValueError("Gold v1 contains no soybean cells; no water-balance publication created")
    metadata = {
        "analysis_id": analysis_id,
        "model_signature": signature,
        "model": SCORE_METHOD,
        "contract": "surface-water-balance-v1",
        "processing_version": settings.project.version,
        "soil_method": SOIL_METHOD,
        "processed_at_utc": datetime.now(UTC).isoformat(),
        "parameters": settings.water_balance.model_dump(mode="json"),
        "score_parameters": settings.gold.model_dump(mode="json"),
        "inputs": lineage,
        "files": files,
        "cell_count": cell_count,
        "row_count": row_count,
        "statuses": dict(counts),
        "primary_key": ["analysis_id", "grid_id", "week_start", "scenario_id"],
        "temporal_grain": "Monday-start week; only active scenario days contribute",
        "crs": settings.spatial.area_crs,
        "resolution_meters": settings.spatial.screening_grid_meters,
        "depth_m": 0.3,
        "scope": root.name,
        "validation": "scenario model; no field validation",
        "assumptions": [
            "homogeneous 0-30 cm Silver averages",
            "no density correction",
            "no irrigation or capillary rise",
            "rain before ET; drainage after ET",
            "no off-season simulation",
            "weather gaps invalidate subsequent state",
        ],
        "classification": asdict(RISK_CLASSIFICATION_POLICY),
    }
    quality = {
        **metadata,
        "quality_status": (
            "failed"
            if counts["unavailable"] and not counts["complete"] + counts["partial"]
            else "warning"
        ),
        "quality_issues": [
            "Scenario assumptions and shallow profile; independent validation pending",
            *(["Unavailable active scores detected"] if counts["unavailable"] else []),
        ],
    }
    common.write_json(
        root / "_schema.json",
        {
            "soil_hydraulics": common.schema_document("soil_hydraulics", HYDRAULIC_SCHEMA),
            "water_stress_weekly_v2": common.schema_document(
                "water_stress_weekly_v2", WEEKLY_SCHEMA
            ),
        },
    )
    common.write_json(root / "_quality.json", quality)
    # This is the publication boundary: loaders never discover incomplete runs by globbing.
    common.write_json(root / "_metadata.json", metadata)
    return WaterBalanceResult(
        root, root / "_metadata.json", root / "_quality.json", row_count, cell_count, analysis_id
    )
