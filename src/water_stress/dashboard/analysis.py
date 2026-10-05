from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
from pydantic import BaseModel, Field, model_validator

from water_stress.risk_classification import RISK_CLASSIFICATION_POLICY

RISK_LABELS = {band.risk_class.value: band.label for band in RISK_CLASSIFICATION_POLICY.bands}
RISK_LABELS.update({"missing": "Sem score", "pending": "Classificação pendente"})
RISK_COLORS = {
    "low": "#31936e",
    "attention": "#e7b848",
    "high": "#e48243",
    "critical": "#c4535c",
    "missing": "#b5bec8",
    "pending": "#7e8c9a",
}
STATUS_LABELS = {
    "complete": "Completo",
    "partial": "Parcial",
    "unavailable": "Indisponível",
    "pending": "Status pendente",
}
FACTOR_UNITS = {
    "precipitation_mm_7d": "mm",
    "eto_mm_7d": "mm",
    "water_deficit_mm_7d": "mm",
    "ndvi_median": "índice",
    "ndmi_median": "índice",
    "temperature_mean_c": "°C",
    "consecutive_dry_days": "dias",
    "clay_pct": "%",
    "sand_pct": "%",
    "soc": "g/kg",
    "bulk_density": "g/cm³",
}


class GeographicBounds(BaseModel):
    west: float = Field(ge=-180, le=180, allow_inf_nan=False)
    east: float = Field(ge=-180, le=180, allow_inf_nan=False)
    south: float = Field(ge=-90, le=90, allow_inf_nan=False)
    north: float = Field(ge=-90, le=90, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered(self) -> GeographicBounds:
        if self.west >= self.east or self.south >= self.north:
            raise ValueError("O recorte exige oeste < leste e sul < norte")
        return self


@dataclass(frozen=True)
class WeeklySummary:
    cell_count: int
    soy_area_km2: float
    scored_area_km2: float
    complete_area_km2: float
    partial_area_km2: float
    unavailable_area_km2: float
    pending_status_area_km2: float
    high_risk_area_km2: float
    pending_class_area_km2: float
    score: float | None
    complete_score: float | None
    partial_score: float | None
    factors: dict[str, float | None]
    factor_coverage: dict[str, float]
    risk_area: dict[str, float]

    @property
    def coverage_pct(self) -> float:
        return 100 * self.scored_area_km2 / self.soy_area_km2 if self.soy_area_km2 else 0

    @property
    def complete_coverage_pct(self) -> float:
        return 100 * self.complete_area_km2 / self.soy_area_km2 if self.soy_area_km2 else 0


def _numbers(table: pa.Table, column: str) -> np.ndarray[Any, np.dtype[np.float64]]:
    return np.asarray(table[column].to_numpy(zero_copy_only=False), dtype=float)


def _weighted(
    values: np.ndarray[Any, np.dtype[np.float64]], weights: np.ndarray[Any, np.dtype[np.float64]]
) -> float | None:
    valid = np.isfinite(values) & (weights > 0)
    denominator = float(weights[valid].sum())
    return float(np.sum(values[valid] * weights[valid]) / denominator) if denominator else None


def validate_features(table: pa.Table) -> None:
    """Reject invalid analytical keys/ranges; do not infer missing serving metadata."""
    identifiers = table["grid_id"].to_pylist()
    if any(not isinstance(value, str) or not value for value in identifiers):
        raise ValueError("A Gold contém grid_id ausente ou inválido")
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("A Gold contém células duplicadas na mesma semana")
    for name, minimum, maximum in (
        ("soy_fraction", 0, 1),
        ("water_stress_score", 0, 1),
        ("ndvi_median", -1, 1),
        ("ndmi_median", -1, 1),
    ):
        values = _numbers(table, name)
        nulls = np.asarray(pc.is_null(table[name]).to_numpy(zero_copy_only=False))
        if np.any(~nulls & (~np.isfinite(values) | (values < minimum) | (values > maximum))):
            raise ValueError(f"A Gold contém valores fora do contrato em {name}")
    scores = table["water_stress_score"].to_pylist()
    for score, status, risk, version in zip(
        scores,
        table["score_status"].to_pylist(),
        table["water_stress_risk_class"].to_pylist(),
        table["risk_classification_version"].to_pylist(),
        strict=True,
    ):
        if status is not None and status not in ("complete", "partial", "unavailable"):
            raise ValueError("Status de score incompatível com o método v1")
        if status in ("complete", "partial") and score is None:
            raise ValueError("Score completo/parcial não pode ser nulo")
        if status == "unavailable" and score is not None:
            raise ValueError("Score indisponível deve ser nulo")
        if risk is not None and (
            risk not in RISK_LABELS
            or risk in ("pending", "missing")
            or score is None
            or not version
        ):
            raise ValueError("Classificação Gold inválida; regenere/recarregue a Gold")


def geographic_subset(table: pa.Table, bounds: GeographicBounds | None) -> pa.Table:
    """Filter centroids in EPSG:4326; analytical cell areas are never recomputed here."""
    if bounds is None:
        return table
    longitude, latitude = (
        _numbers(table, "centroid_longitude"),
        _numbers(table, "centroid_latitude"),
    )
    mask = (
        (longitude >= bounds.west)
        & (longitude <= bounds.east)
        & (latitude >= bounds.south)
        & (latitude <= bounds.north)
    )
    return table.filter(pa.array(mask))


def risk_keys(table: pa.Table) -> list[str]:
    return [
        "missing" if score is None else risk or "pending"
        for score, risk in zip(
            table["water_stress_score"].to_pylist(),
            table["water_stress_risk_class"].to_pylist(),
            strict=True,
        )
    ]


def summarize(table: pa.Table) -> WeeklySummary:
    """Weight by equivalent soybean area; each factor keeps its own non-null denominator."""
    weights = _numbers(table, "soy_area_km2")
    if np.any(~np.isfinite(weights) | (weights < 0)):
        raise ValueError("Área equivalente de soja deve ser finita e não negativa")
    scores = _numbers(table, "water_stress_score")
    statuses = np.asarray(table["score_status"].to_pylist(), dtype=object)
    risks = np.asarray(risk_keys(table), dtype=object)
    total = float(weights.sum())
    complete = statuses == "complete"
    partial = statuses == "partial"
    risk_area = {key: float(weights[risks == key].sum()) for key in RISK_LABELS}
    factors = {name: _weighted(_numbers(table, name), weights) for name in FACTOR_UNITS}
    coverage = {
        name: 100 * float(weights[np.isfinite(_numbers(table, name))].sum()) / total if total else 0
        for name in FACTOR_UNITS
    }
    return WeeklySummary(
        table.num_rows,
        total,
        float(weights[np.isfinite(scores)].sum()),
        float(weights[complete].sum()),
        float(weights[partial].sum()),
        float(weights[statuses == "unavailable"].sum()),
        float(weights[statuses == None].sum()),  # noqa: E711
        risk_area["high"] + risk_area["critical"],
        risk_area["pending"],
        _weighted(scores, weights),
        _weighted(scores[complete], weights[complete]),
        _weighted(scores[partial], weights[partial]),
        factors,
        coverage,
        risk_area,
    )
