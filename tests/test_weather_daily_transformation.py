from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from water_stress.config import Settings
from water_stress.transformation import weather_daily


def _settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "study": settings.study.model_copy(
                update={"start_date": date(2023, 9, 1), "end_date": date(2023, 9, 2)}
            )
        }
    )


def _boundary() -> bytes:
    return (
        b'{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},'
        b'"geometry":{"type":"Polygon","coordinates":[[[-56,-13],[-55,-13],[-55,-12],'
        b"[-56,-12],[-56,-13]]]}}]}"
    )


def _documents(settings: Settings, tmp_path: Path) -> dict[str, list[Path]]:
    values = {
        "T2M": 25.0,
        "T2M_MAX": 31.0,
        "T2M_MIN": 20.0,
        "RH2M": 60.0,
        "WS2M": 2.0,
        "ALLSKY_SFC_SW_DWN": 20.0,
        "PRECTOTCORR": 4.0,
    }
    result: dict[str, list[Path]] = {}
    for parameter, raw_value in values.items():
        _, unit = weather_daily.SOURCE_TO_SILVER[parameter]
        coordinates = (
            [[-55.5, -12.5, 350.0]]
            if parameter == weather_daily.SOLAR_PARAMETER
            else [[-55.625, -12.5, 360.0], [-54.375, -12.5, 300.0]]
        )
        features = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": coordinate},
                "properties": {
                    "parameter": {parameter: {"20230901": raw_value, "20230902": raw_value}}
                },
            }
            for coordinate in coordinates
        ]
        path = tmp_path / f"parameter={parameter.lower()}" / "region_id=r000_c000" / "weather.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "header": {"fill_value": -999.0},
                    "parameters": {parameter: {"units": unit}},
                    "features": features,
                }
            )
        )
        path.with_suffix(".manifest.json").write_text(
            json.dumps({"downloaded_at_utc": "2026-08-14T00:00:00+00:00"})
        )
        result[parameter] = [path]
    return result


def test_builds_one_row_per_weather_cell_and_date(settings: Settings, tmp_path: Path) -> None:
    settings = _settings(settings)
    table, distance = weather_daily.build_table(
        settings,
        boundary=_boundary(),
        paths_by_parameter=_documents(settings, tmp_path),
    )

    assert table.num_rows == 2
    assert len(set(table["weather_cell_id"].to_pylist())) == 1
    assert table["latitude"].to_pylist() == [-12.5, -12.5]
    assert table["longitude"].to_pylist() == [-55.625, -55.625]
    assert table["solar_radiation_mj_m2_day"].to_pylist() == [20.0, 20.0]
    assert all(value > 0 for value in table["reference_evapotranspiration_mm_day"].to_pylist())
    assert distance == pytest.approx(0.125)


def test_fao56_reference_evapotranspiration_and_input_validation() -> None:
    result = weather_daily.reference_evapotranspiration(
        observation_date=date(2023, 9, 1),
        latitude=-12.5,
        elevation_m=360,
        temperature_mean_c=25,
        temperature_max_c=31,
        temperature_min_c=20,
        relative_humidity_pct=60,
        wind_speed_ms=2,
        solar_radiation_mj_m2_day=20,
    )

    assert result == pytest.approx(4.94359, rel=1e-5)
    with pytest.raises(ValueError, match="Minimum temperature"):
        weather_daily.reference_evapotranspiration(
            observation_date=date(2023, 9, 1),
            latitude=-12.5,
            elevation_m=360,
            temperature_mean_c=25,
            temperature_max_c=20,
            temperature_min_c=31,
            relative_humidity_pct=60,
            wind_speed_ms=2,
            solar_radiation_mj_m2_day=20,
        )


def test_writes_year_partitions_quality_and_metadata(settings: Settings, tmp_path: Path) -> None:
    settings = _settings(settings)
    paths = _documents(settings, tmp_path)
    table, distance = weather_daily.build_table(
        settings, boundary=_boundary(), paths_by_parameter=paths
    )

    result = weather_daily.write_weather_daily(
        settings,
        table,
        paths_by_parameter=paths,
        maximum_solar_distance_degrees=distance,
    )

    assert len(result.parquet_paths) == 1
    assert pq.ParquetFile(result.parquet_paths[0]).read().num_rows == 2
    quality = json.loads(result.quality_path.read_text())
    metadata = json.loads(result.metadata_path.read_text())
    assert quality["duplicate_key_count"] == 0
    assert quality["weather_cell_count"] == 1
    assert quality["range_by_column"]["precipitation_mm_day"] == {
        "minimum": 4.0,
        "maximum": 4.0,
    }
    assert metadata["native_resolution"]["MERRA2"] == "0.5 x 0.625 degrees"
    assert metadata["eto_method"].startswith("FAO-56")
    assert len(metadata["eto_assumptions"]) == 4


def test_fill_value_produces_null_observation_and_eto(settings: Settings, tmp_path: Path) -> None:
    settings = _settings(settings)
    paths = _documents(settings, tmp_path)
    temperature_path = paths["T2M"][0]
    document = json.loads(temperature_path.read_text())
    document["features"][0]["properties"]["parameter"]["T2M"]["20230901"] = -999.0
    temperature_path.write_text(json.dumps(document))

    table, _ = weather_daily.build_table(settings, boundary=_boundary(), paths_by_parameter=paths)

    assert table["temperature_mean_c"][0].as_py() is None
    assert table["reference_evapotranspiration_mm_day"][0].as_py() is None
