from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isfinite

from water_stress.config import GoldSettings, WaterBalanceSettings
from water_stress.risk_classification import lower_index_stress

SOIL_METHOD = "saxton-rawls-2006-fc-wp-v1"
SCORE_METHOD = "academic-index-v2-surface-30cm"


@dataclass(frozen=True)
class HydraulicProperties:
    field_capacity: float
    wilting_point: float
    available_water_mm: float
    organic_matter_pct: float


@dataclass(frozen=True)
class DailyBalance:
    available_water_mm: float
    etc_potential_mm: float
    etc_adjusted_mm: float
    drainage_mm: float
    retained_rain_mm: float
    stress: float


@dataclass(frozen=True)
class CombinedScore:
    score: float | None
    status: str
    available_weight: float
    component_count: int


def hydraulic_properties(
    sand_pct: float, clay_pct: float, soc_g_kg: float, parameters: WaterBalanceSettings
) -> HydraulicProperties:
    """Estimate a homogeneous 0-30 cm layer from Silver averages.

    Saxton-Rawls equations 1-2 use sand/clay fractions and organic matter
    percent by mass. The carbon conversion is an explicit scenario assumption.
    Density, gravel and salinity corrections are not applied.
    """
    if any(not isfinite(value) for value in (sand_pct, clay_pct, soc_g_kg)):
        raise ValueError("Soil inputs must be finite")
    if not (0 <= sand_pct <= 100 and 0 <= clay_pct <= 100 and sand_pct + clay_pct <= 100):
        raise ValueError("Sand and clay percentages must be in [0, 100] and sum to at most 100")
    organic_matter = soc_g_kg / 10 * parameters.carbon_to_organic_matter
    if not 0 <= organic_matter <= 8:
        raise ValueError("Estimated organic matter must be in [0, 8]% for this mineral-soil model")
    sand, clay = sand_pct / 100, clay_pct / 100
    wp_estimate = (
        -0.024 * sand
        + 0.487 * clay
        + 0.006 * organic_matter
        + 0.005 * sand * organic_matter
        - 0.013 * clay * organic_matter
        + 0.068 * sand * clay
        + 0.031
    )
    wilting = 1.14 * wp_estimate - 0.02
    fc_estimate = (
        -0.251 * sand
        + 0.195 * clay
        + 0.011 * organic_matter
        + 0.006 * sand * organic_matter
        - 0.027 * clay * organic_matter
        + 0.452 * sand * clay
        + 0.299
    )
    capacity = fc_estimate + 1.283 * fc_estimate**2 - 0.374 * fc_estimate - 0.015
    if not 0 <= wilting < capacity <= 1:
        raise ValueError("Estimated soil water contents must satisfy 0 <= wilting < capacity <= 1")
    return HydraulicProperties(
        capacity, wilting, 1000 * (capacity - wilting) * parameters.depth_m, organic_matter
    )


def crop_coefficient(day: date, parameters: WaterBalanceSettings) -> tuple[str, float] | None:
    """Return scenario stage and Kc; linear ramps reach each stage's end coefficient."""
    age = (day - parameters.planting_date).days
    initial, development, mid, late = parameters.stage_days
    if age < 0 or age >= sum(parameters.stage_days):
        return None
    if age < initial:
        return "initial", parameters.kc_initial
    age -= initial
    if age < development:
        fraction = (age + 1) / development
        return "development", parameters.kc_initial + fraction * (
            parameters.kc_mid - parameters.kc_initial
        )
    age -= development
    if age < mid:
        return "mid", parameters.kc_mid
    fraction = (age - mid + 1) / late
    return "late", parameters.kc_mid + fraction * (parameters.kc_end - parameters.kc_mid)


def daily_balance(
    available_water_mm: float,
    capacity_mm: float,
    precipitation_mm: float,
    eto_mm: float,
    kc: float,
    parameters: WaterBalanceSettings,
) -> DailyBalance:
    """Rain precedes demand; excess drains after ET, with no irrigation/capillary rise."""
    values = (available_water_mm, capacity_mm, precipitation_mm, eto_mm, kc)
    if any(not isfinite(value) for value in values):
        raise ValueError("Daily balance inputs must be finite")
    if capacity_mm <= 0 or not 0 <= available_water_mm <= capacity_mm:
        raise ValueError("Water storage must be between zero and a positive capacity")
    if min(precipitation_mm, eto_mm, kc) < 0:
        raise ValueError("Rain, ETo and Kc cannot be negative")
    rain = precipitation_mm * (1 - parameters.runoff_fraction)
    temporary = available_water_mm + rain
    ks = min(1.0, min(capacity_mm, temporary) / ((1 - parameters.depletion_fraction) * capacity_mm))
    potential = kc * eto_mm
    adjusted = min(temporary, ks * potential)
    drainage = max(0.0, temporary - adjusted - capacity_mm)
    return DailyBalance(
        temporary - adjusted - drainage, potential, adjusted, drainage, rain - drainage, 1 - ks
    )


def combined_score(
    water_stress: float | None, ndvi: float | None, ndmi: float | None, parameters: GoldSettings
) -> CombinedScore:
    """Require the hydrological component; renormalize only missing satellite signals."""
    if parameters.deficit_weight <= 0:
        raise ValueError("The v2 hydrological component must have a positive weight")
    if water_stress is None:
        return CombinedScore(None, "unavailable", 0.0, 0)
    if not isfinite(water_stress) or not 0 <= water_stress <= 1:
        raise ValueError("Hydrological stress must be finite and in [0, 1]")
    components = [(parameters.deficit_weight, water_stress)]
    for value, threshold, weight in (
        (ndvi, parameters.ndvi_stress_threshold, parameters.ndvi_weight),
        (ndmi, parameters.ndmi_stress_threshold, parameters.ndmi_weight),
    ):
        if value is not None:
            if not isfinite(value) or not -1 <= value <= 1:
                raise ValueError("Satellite indices must be finite and in [-1, 1]")
            components.append((weight, lower_index_stress(value, threshold)))
    components = [(weight, value) for weight, value in components if weight > 0]
    available = sum(weight for weight, _ in components)
    total = parameters.deficit_weight + parameters.ndvi_weight + parameters.ndmi_weight
    if not available:
        return CombinedScore(None, "unavailable", 0.0, 0)
    return CombinedScore(
        sum(weight * value for weight, value in components) / available,
        "complete" if available == total else "partial",
        available / total,
        len(components),
    )
