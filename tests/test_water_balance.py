from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from water_stress.config import GoldSettings, WaterBalanceSettings
from water_stress.water_balance import (
    combined_score,
    crop_coefficient,
    daily_balance,
    hydraulic_properties,
)


def test_soil_units_and_published_equation_example() -> None:
    # Independent substitution: S=.4, C=.3, OM=2%; SR equations 1 and 2.
    parameters = WaterBalanceSettings(carbon_to_organic_matter=2)
    soil = hydraulic_properties(40, 30, 10, parameters)
    assert soil.organic_matter_pct == 2
    assert soil.wilting_point == pytest.approx(0.1896004)
    assert soil.field_capacity == pytest.approx(0.3195114414988)
    assert soil.available_water_mm == pytest.approx(38.97331244964)


@pytest.mark.parametrize(
    "values",
    [(80, 30, 10), (-1, 30, 10), (40, 30, -1), (40, 30, 100), (float("nan"), 30, 10), (100, 0, 0)],
)
def test_invalid_soil_has_no_silent_adjustment(values: tuple[float, float, float]) -> None:
    with pytest.raises(ValueError):
        hydraulic_properties(*values, WaterBalanceSettings())


@pytest.mark.parametrize(
    "arguments",
    [
        dict(stage_days=(0, 30, 60, 30)),
        dict(depth_m=1),
        dict(initial_water_fractions=()),
        dict(initial_water_fractions=(0.5, 0.5)),
        dict(initial_water_fractions=(float("nan"),)),
        dict(depletion_fraction=1),
    ],
)
def test_configuration_rejects_unsupported_assumptions(arguments: dict) -> None:
    with pytest.raises(ValidationError):
        WaterBalanceSettings(**arguments)


def test_calendar_all_boundaries_and_linear_ramps() -> None:
    parameters = WaterBalanceSettings()
    plant = parameters.planting_date
    assert crop_coefficient(plant - timedelta(days=1), parameters) is None
    assert crop_coefficient(plant, parameters) == ("initial", 0.4)
    assert crop_coefficient(plant + timedelta(days=19), parameters) == ("initial", 0.4)
    assert crop_coefficient(plant + timedelta(days=20), parameters) == (
        "development",
        pytest.approx(0.425),
    )
    assert crop_coefficient(plant + timedelta(days=49), parameters) == ("development", 1.15)
    assert crop_coefficient(plant + timedelta(days=109), parameters) == ("mid", 1.15)
    stage, kc = crop_coefficient(plant + timedelta(days=110), parameters)
    assert stage == "late"
    assert kc == pytest.approx(1.15 - 0.65 / 30)
    assert crop_coefficient(date(2024, 3, 2), parameters) == ("late", 0.5)
    assert crop_coefficient(date(2024, 3, 3), parameters) is None


def test_daily_dry_and_rainy_mass_conservation() -> None:
    parameters = WaterBalanceSettings()
    dry = daily_balance(10, 40, 0, 5, 1, parameters)
    assert dry.stress == 0.5
    assert dry.etc_adjusted_mm == 2.5
    assert dry.available_water_mm == 7.5
    wet = daily_balance(10, 40, 100, 5, 1, parameters)
    assert wet.stress == 0
    assert wet.drainage_mm == 65
    assert wet.retained_rain_mm == 35
    assert wet.available_water_mm == 40
    empty = daily_balance(0, 40, 0, 5, 1, parameters)
    assert empty.stress == 1
    assert empty.etc_adjusted_mm == empty.available_water_mm == 0
    for initial in (0, 10, 40):
        for rain in (0, 3, 100):
            for eto in (0, 3, 100):
                model = parameters.model_copy(update={"runoff_fraction": 0.2})
                result = daily_balance(initial, 40, rain, eto, 1, model)
                assert result.available_water_mm + result.etc_adjusted_mm + result.drainage_mm == (
                    pytest.approx(initial + 0.8 * rain)
                )
                assert 0 <= result.available_water_mm <= 40
                assert 0 <= result.retained_rain_mm <= rain
                assert 0 <= result.stress <= 1


@pytest.mark.parametrize(
    "values",
    [
        (-1, 40, 0, 5, 1),
        (50, 40, 0, 5, 1),
        (0, 0, 0, 5, 1),
        (10, 40, -1, 5, 1),
        (10, 40, 0, float("inf"), 1),
    ],
)
def test_invalid_daily_balance(values: tuple[float, ...]) -> None:
    with pytest.raises(ValueError):
        daily_balance(*values, WaterBalanceSettings())


def test_combined_score_preserves_weights_and_explicit_partial_status() -> None:
    complete = combined_score(0.5, 0.7, 0.2, GoldSettings())
    assert complete.score == 0.25
    assert complete.status == "complete"
    assert complete.component_count == 3
    assert complete.available_weight == 1
    assert combined_score(0.5, 0.35, 0.1, GoldSettings()).score == pytest.approx(0.5)
    partial = combined_score(0.5, None, None, GoldSettings())
    assert partial.score == 0.5
    assert partial.status == "partial"
    assert partial.available_weight == 0.5
    assert combined_score(None, 0.7, 0.2, GoldSettings()).score is None
    assert combined_score(0, 0.7, 0.2, GoldSettings()).score == 0
    with pytest.raises(ValueError, match="positive"):
        combined_score(0, 0, 0, GoldSettings(deficit_weight=0))
    with pytest.raises(ValueError, match="Hydrological"):
        combined_score(float("nan"), None, None, GoldSettings())
    with pytest.raises(ValueError, match="Satellite"):
        combined_score(0, 2, None, GoldSettings())
