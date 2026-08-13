from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from water_stress.config import Settings
from water_stress.ingestion import nasa_power as nasa_power_ingestion
from water_stress.transformation import nasa_power


def bronze_document() -> dict[str, object]:
    values = {
        "20230901": 10.0,
        "20230902": -999.0,
        "20240101": 20.0,
    }
    parameter_names = list(nasa_power.SOURCE_TO_SILVER)
    units = {
        source: {"units": unit, "longname": description}
        for source, (_, unit, description) in nasa_power.SOURCE_TO_SILVER.items()
    }
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-55.776, -12.692, 370.0]},
        "header": {"fill_value": -999.0},
        "properties": {
            "parameter": {source: dict(values) for source in parameter_names},
        },
        "parameters": units,
    }


def test_transforms_one_row_per_date_with_snake_case_and_coordinates(
    settings: Settings,
) -> None:
    table = nasa_power.transform_document(settings, bronze_document())

    assert table.num_rows == 243
    assert table.column_names == [
        "date",
        "latitude",
        "longitude",
        "temperature_mean_c",
        "temperature_max_c",
        "temperature_min_c",
        "relative_humidity_pct",
        "wind_speed_ms",
        "solar_radiation_mj_m2_day",
        "precipitation_mm_day",
    ]
    assert set(table.column("latitude").to_pylist()) == {-12.692}
    assert set(table.column("longitude").to_pylist()) == {-55.776}
    assert len(set(table.column("date").to_pylist())) == table.num_rows


def test_converts_fill_values_and_absent_observations_to_null(settings: Settings) -> None:
    table = nasa_power.transform_document(settings, bronze_document())
    counts = nasa_power.missing_counts(table)

    assert counts["temperature_mean_c"] == 241
    assert counts["precipitation_mm_day"] == 241
    assert counts["date"] == 0


def test_schema_documents_units() -> None:
    schema = nasa_power.silver_schema()

    temperature_metadata = schema.field("temperature_mean_c").metadata
    wind_metadata = schema.field("wind_speed_ms").metadata
    precipitation_metadata = schema.field("precipitation_mm_day").metadata
    assert temperature_metadata is not None and temperature_metadata[b"unit"] == b"C"
    assert wind_metadata is not None and wind_metadata[b"unit"] == b"m/s"
    assert precipitation_metadata is not None and precipitation_metadata[b"unit"] == b"mm/day"


def test_rejects_duplicate_dates_during_json_parse() -> None:
    content = b'{"properties":{"parameter":{"T2M":{"20230901":1,"20230901":2}}}}'

    with pytest.raises(nasa_power.DuplicateDateError, match="20230901"):
        nasa_power.parse_bronze(content)


def test_rejects_invalid_json_and_missing_coordinates(settings: Settings) -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        nasa_power.parse_bronze(b"not-json")

    document = bronze_document()
    document["geometry"] = {"type": "Point", "coordinates": []}
    with pytest.raises(ValueError, match="no valid point coordinates"):
        nasa_power.transform_document(settings, document)


def test_rejects_unexpected_source_unit(settings: Settings) -> None:
    document = bronze_document()
    document["parameters"]["T2M"]["units"] = "K"  # type: ignore[index]

    with pytest.raises(ValueError, match="Unexpected unit for T2M"):
        nasa_power.transform_document(settings, document)


def test_rejects_dates_outside_study_period(settings: Settings) -> None:
    document = bronze_document()
    document["properties"]["parameter"]["T2M"]["20250101"] = 1.0  # type: ignore[index]

    with pytest.raises(ValueError, match="outside the study period"):
        nasa_power.transform_document(settings, document)


def test_writes_parquet_partitioned_by_year_and_quality_files(
    settings: Settings, tmp_path: Path
) -> None:
    table = nasa_power.transform_document(settings, bronze_document())
    result = nasa_power.write_partitioned(settings, table, source_path=tmp_path / "weather.json")

    assert [path.parent.name for path in result.parquet_paths] == ["year=2023", "year=2024"]
    assert sum(pq.read_table(path).num_rows for path in result.parquet_paths) == 243
    parquet_schema = pq.read_schema(result.parquet_paths[0])
    radiation_metadata = parquet_schema.field("solar_radiation_mj_m2_day").metadata
    assert radiation_metadata is not None
    assert radiation_metadata[b"unit"] == b"MJ/m^2/day"
    quality = json.loads(result.quality_path.read_text())
    schema = json.loads(result.schema_path.read_text())
    assert quality["duplicate_date_count"] == 0
    assert quality["missing_by_column"]["temperature_mean_c"] == 241
    assert (
        next(column for column in schema["columns"] if column["name"] == "latitude")["unit"]
        == "degrees_north"
    )


def test_end_to_end_transform_reads_configured_bronze(settings: Settings) -> None:
    source_path = nasa_power_ingestion.artifact_path(settings)
    source_path.parent.mkdir(parents=True)
    source_path.write_text(json.dumps(bronze_document()))

    result = nasa_power.transform(settings)

    assert result.row_count == 243
    assert len(result.parquet_paths) == 2
    assert all(path.is_file() for path in result.parquet_paths)


def test_transform_requires_bronze_artifact(settings: Settings) -> None:
    with pytest.raises(FileNotFoundError, match="run ingestion first"):
        nasa_power.transform(settings)
