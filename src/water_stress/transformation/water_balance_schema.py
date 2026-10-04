"""Shared file/relational column contracts for the surface-water model."""

from __future__ import annotations

import pyarrow as pa


def _schema(
    fields: list[tuple[str, pa.DataType, str]], dataset: str, required: set[str]
) -> pa.Schema:
    return pa.schema(
        [
            pa.field(name, dtype, nullable=name not in required, metadata={"unit": unit})
            for name, dtype, unit in fields
        ],
        metadata={"layer": "gold", "dataset": dataset, "model_depth_m": "0.30"},
    )


HYDRAULIC_SCHEMA = _schema(
    [
        ("analysis_id", pa.string(), "identifier"),
        ("grid_id", pa.string(), "identifier"),
        ("model_signature", pa.string(), "sha256"),
        ("soil_method", pa.string(), "version"),
        ("field_capacity", pa.float64(), "m3/m3"),
        ("wilting_point", pa.float64(), "m3/m3"),
        ("available_water_mm", pa.float64(), "mm"),
        ("organic_matter_pct", pa.float64(), "%"),
        ("soil_status", pa.string(), "category"),
        ("soil_issue", pa.string(), "text"),
    ],
    "soil_hydraulics",
    {"analysis_id", "grid_id", "model_signature", "soil_method", "soil_status"},
)

WEEKLY_SCHEMA = _schema(
    [
        ("analysis_id", pa.string(), "identifier"),
        ("grid_id", pa.string(), "identifier"),
        ("scenario_id", pa.string(), "identifier"),
        ("week_start", pa.date32(), "date"),
        ("week_end", pa.date32(), "date"),
        ("model_signature", pa.string(), "sha256"),
        ("score_method_version", pa.string(), "version"),
        ("soy_fraction", pa.float64(), "fraction"),
        ("weather_cell_id", pa.string(), "identifier"),
        ("initial_water_fraction", pa.float64(), "fraction"),
        ("crop_stage", pa.string(), "category"),
        ("active_day_count", pa.int16(), "days"),
        ("valid_day_count", pa.int16(), "days"),
        ("water_start_mm", pa.float64(), "mm"),
        ("water_end_mm", pa.float64(), "mm"),
        ("etc_potential_mm", pa.float64(), "mm"),
        ("etc_adjusted_mm", pa.float64(), "mm"),
        ("retained_rain_mm", pa.float64(), "mm"),
        ("drainage_mm", pa.float64(), "mm"),
        ("water_stress_mean", pa.float64(), "fraction"),
        ("water_stress_max", pa.float64(), "fraction"),
        ("stress_day_count", pa.int16(), "days"),
        ("maximum_stress_run_days", pa.int16(), "days"),
        ("ongoing_stress_run_days", pa.int32(), "days"),
        ("ndvi_median", pa.float64(), "index"),
        ("ndmi_median", pa.float64(), "index"),
        ("satellite_age_days", pa.int16(), "days"),
        ("water_stress_score_v1", pa.float64(), "fraction"),
        ("score_status_v1", pa.string(), "category"),
        ("water_stress_score", pa.float64(), "fraction"),
        ("water_stress_risk_class", pa.string(), "category"),
        ("risk_classification_version", pa.string(), "version"),
        ("monitoring_guidance", pa.string(), "text"),
        ("score_status", pa.string(), "category"),
        ("score_available_weight", pa.float64(), "fraction"),
        ("score_component_count", pa.int16(), "count"),
        ("score_issue", pa.string(), "category"),
    ],
    "water_stress_weekly_v2",
    {
        "analysis_id",
        "grid_id",
        "scenario_id",
        "week_start",
        "week_end",
        "model_signature",
        "score_method_version",
        "soy_fraction",
        "weather_cell_id",
        "initial_water_fraction",
        "active_day_count",
        "valid_day_count",
        "risk_classification_version",
        "monitoring_guidance",
        "score_status",
        "score_available_weight",
        "score_component_count",
    },
)

STATE_SCHEMA = _schema(
    [
        ("grid_id", pa.string(), "identifier"),
        ("water_mm", pa.float64(), "mm"),
        ("initialized", pa.bool_(), "flag"),
        ("run_days", pa.int32(), "days"),
        ("issue", pa.string(), "category"),
    ],
    "water_balance_checkpoint",
    {"grid_id", "initialized", "run_days"},
)
