from __future__ import annotations

import json
from datetime import date, timedelta

import pyarrow as pa

from water_stress.config import Settings
from water_stress.transformation import (
    common,
    crop_mask,
    gold_weekly,
    soil_features,
    spatial_grid,
    weather_daily,
)


def _weather_table() -> pa.Table:
    dates = [date(2023, 9, 1) + timedelta(days=offset) for offset in range(3)]
    return pa.table(
        {
            "weather_cell_id": ["cell-1"] * 3,
            "date": dates,
            "latitude": [0.0] * 3,
            "longitude": [0.0] * 3,
            "precipitation_mm_day": [0.0, 2.0, 0.0],
            "reference_evapotranspiration_mm_day": [4.0, 4.0, 4.0],
            "temperature_mean_c": [25.0, 26.0, 27.0],
            "temperature_max_c": [30.0, 31.0, 32.0],
            "temperature_min_c": [20.0, 21.0, 22.0],
        }
    )


def test_builds_weekly_features_only_for_soy_cells(settings: Settings) -> None:
    grid = pa.table(
        {
            "grid_id": ["soy-cell", "other-cell"],
            "centroid_latitude": [0.01, 0.02],
            "centroid_longitude": [0.01, 0.02],
        }
    )
    crop = pa.table({"grid_id": ["soy-cell", "other-cell"], "soy_fraction": [0.25, 0.1]})
    soil = pa.table(
        {
            "grid_id": ["soy-cell", "other-cell"],
            "clay_pct": [30.0, 20.0],
            "sand_pct": [40.0, 50.0],
            "soc": [12.0, 10.0],
            "bulk_density": [1.2, 1.3],
        }
    )

    table = gold_weekly.build_weekly_table(
        settings,
        grid=grid,
        crop=crop,
        soil=soil,
        weather=_weather_table(),
        week=(date(2023, 8, 28), date(2023, 9, 3)),
    )

    assert table.num_rows == 1
    assert table["grid_id"].to_pylist() == ["soy-cell"]
    assert table["precipitation_mm_7d"].to_pylist() == [2.0]
    assert table["eto_mm_7d"].to_pylist() == [12.0]
    assert table["water_balance_mm_7d"].to_pylist() == [-10.0]
    assert table["water_deficit_mm_7d"].to_pylist() == [10.0]
    assert table["rainy_day_count"].to_pylist() == [1]
    assert table["consecutive_dry_days"].to_pylist() == [1]
    assert table["water_stress_score"].to_pylist() == [0.5]
    assert table["water_stress_class"].to_pylist() == ["moderate"]
    assert table["score_component_count"].to_pylist() == [1]


def test_includes_optional_satellite_weekly_summary(settings: Settings) -> None:
    grid = pa.table(
        {"grid_id": ["soy-cell"], "centroid_latitude": [0.01], "centroid_longitude": [0.01]}
    )
    crop = pa.table({"grid_id": ["soy-cell"], "soy_fraction": [0.5]})
    soil = pa.table(
        {
            "grid_id": ["soy-cell"],
            "clay_pct": [30.0],
            "sand_pct": [40.0],
            "soc": [12.0],
            "bulk_density": [1.2],
        }
    )
    satellite = pa.table(
        {
            "grid_id": ["soy-cell"],
            "date": [date(2023, 9, 2)],
            "ndvi_mean": [0.7],
            "ndmi_mean": [0.2],
            "valid_pixel_pct": [90.0],
            "cloud_pixel_pct": [5.0],
        }
    )

    table = gold_weekly.build_weekly_table(
        settings,
        grid=grid,
        crop=crop,
        soil=soil,
        weather=_weather_table(),
        satellite=satellite,
        week=(date(2023, 8, 28), date(2023, 9, 3)),
    )

    assert table["ndvi_median"].to_pylist() == [0.7]
    assert table["ndmi_median"].to_pylist() == [0.2]
    assert table["satellite_scene_count"].to_pylist() == [1]
    assert table["satellite_age_days"].to_pylist() == [1]
    assert table["score_component_count"].to_pylist() == [3]


def test_uses_latest_satellite_observation_within_configured_age(settings: Settings) -> None:
    grid = pa.table(
        {"grid_id": ["soy-cell"], "centroid_latitude": [0.01], "centroid_longitude": [0.01]}
    )
    crop = pa.table({"grid_id": ["soy-cell"], "soy_fraction": [0.5]})
    soil = pa.table(
        {
            "grid_id": ["soy-cell"],
            "clay_pct": [30.0],
            "sand_pct": [40.0],
            "soc": [12.0],
            "bulk_density": [1.2],
        }
    )
    satellite = pa.table(
        {
            "grid_id": ["soy-cell"],
            "date": [date(2023, 9, 2)],
            "ndvi_mean": [0.7],
            "ndmi_mean": [0.2],
            "valid_pixel_pct": [90.0],
            "cloud_pixel_pct": [5.0],
        }
    )

    table = gold_weekly.build_weekly_table(
        settings,
        grid=grid,
        crop=crop,
        soil=soil,
        weather=_weather_table(),
        satellite=satellite,
        week=(date(2023, 9, 11), date(2023, 9, 17)),
    )

    assert table["ndvi_median"].to_pylist() == [0.7]
    assert table["satellite_observation_date"].to_pylist() == [date(2023, 9, 2)]
    assert table["satellite_age_days"].to_pylist() == [15]


def test_does_not_use_satellite_observation_older_than_configured_age(
    settings: Settings,
) -> None:
    settings = settings.model_copy(
        update={
            "gold": settings.gold.model_copy(update={"satellite_max_age_days": 7}),
        }
    )
    grid = pa.table(
        {"grid_id": ["soy-cell"], "centroid_latitude": [0.01], "centroid_longitude": [0.01]}
    )
    crop = pa.table({"grid_id": ["soy-cell"], "soy_fraction": [0.5]})
    soil = pa.table(
        {
            "grid_id": ["soy-cell"],
            "clay_pct": [30.0],
            "sand_pct": [40.0],
            "soc": [12.0],
            "bulk_density": [1.2],
        }
    )
    satellite = pa.table(
        {
            "grid_id": ["soy-cell"],
            "date": [date(2023, 9, 2)],
            "ndvi_mean": [0.7],
            "ndmi_mean": [0.2],
            "valid_pixel_pct": [90.0],
            "cloud_pixel_pct": [5.0],
        }
    )

    table = gold_weekly.build_weekly_table(
        settings,
        grid=grid,
        crop=crop,
        soil=soil,
        weather=_weather_table(),
        satellite=satellite,
        week=(date(2023, 9, 11), date(2023, 9, 17)),
    )

    assert table["ndvi_median"].to_pylist() == [None]
    assert table["satellite_observation_date"].to_pylist() == [None]
    assert table["satellite_age_days"].to_pylist() == [None]


def test_writes_partitioned_gold_dataset(settings: Settings) -> None:
    common.write_parquet(
        spatial_grid.dataset_path(settings) / "grid.parquet",
        pa.table(
            {
                "grid_id": ["soy-cell"],
                "centroid_latitude": [0.01],
                "centroid_longitude": [0.01],
            }
        ),
    )
    common.write_parquet(
        crop_mask.dataset_path(settings) / "part-000.parquet",
        pa.table({"grid_id": ["soy-cell"], "soy_fraction": [0.5]}),
    )
    common.write_parquet(
        soil_features.dataset_path(settings) / "part-000.parquet",
        pa.table(
            {
                "grid_id": ["soy-cell"],
                "clay_pct": [30.0],
                "sand_pct": [40.0],
                "soc": [12.0],
                "bulk_density": [1.2],
            }
        ),
    )
    common.write_parquet(
        weather_daily.dataset_path(settings) / "year=2023" / "part-000.parquet",
        _weather_table(),
    )

    result = gold_weekly.transform(settings)

    assert result.row_count > 0
    assert result.week_count > 0
    assert result.parquet_paths[0].is_file()
    assert result.metadata_path.is_file()
    assert result.quality_path.is_file()
    assert json.loads(result.quality_path.read_text())["quality_status"] == "warning"
    first = result.parquet_paths[0]
    modified = first.stat().st_mtime_ns
    assert gold_weekly.transform(settings).row_count == result.row_count
    assert first.stat().st_mtime_ns == modified
    first.write_bytes(b"interrupted output")
    gold_weekly.transform(settings)
    assert first.read_bytes()[:4] == b"PAR1"


def _consumption_table(settings: Settings, weather: pa.Table) -> pa.Table:
    return gold_weekly.build_weekly_table(
        settings,
        grid=pa.table(
            {"grid_id": ["soy"], "centroid_latitude": [0.0], "centroid_longitude": [0.0]}
        ),
        crop=pa.table({"grid_id": ["soy"], "soy_fraction": [0.5]}),
        soil=pa.table({"grid_id": ["soy"]}),
        weather=weather,
        week=(date(2023, 8, 28), date(2023, 9, 3)),
    )


def test_partial_weather_never_produces_risk_score(settings: Settings) -> None:
    table = _consumption_table(settings, _weather_table().slice(0, 2))
    assert table["water_stress_score"].to_pylist() == [None]
    assert table["score_status"].to_pylist() == ["unavailable"]
    assert table["weather_expected_days"].to_pylist() == [3]


def test_complete_weather_without_satellite_has_explicit_partial_status(settings: Settings) -> None:
    table = _consumption_table(settings, _weather_table())
    assert table["score_status"].to_pylist() == ["partial"]
    assert table["score_available_weight"].to_pylist() == [0.5]
    assert table["weather_cell_id"].to_pylist() == ["cell-1"]


def test_duplicate_weather_is_rejected(settings: Settings) -> None:
    import pytest

    with pytest.raises(ValueError, match="duplicate"):
        _consumption_table(settings, pa.concat_tables([_weather_table(), _weather_table()]))


def test_missing_eto_does_not_compare_unpaired_totals(settings: Settings) -> None:
    weather = _weather_table().set_column(
        5, "reference_evapotranspiration_mm_day", pa.array([4.0, None, 4.0])
    )
    table = _consumption_table(settings, weather)
    assert table["water_deficit_mm_7d"].to_pylist() == [None]
    assert table["water_stress_score"].to_pylist() == [None]
