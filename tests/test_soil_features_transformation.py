from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from water_stress.config import Settings
from water_stress.transformation import soil_features


def _settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "spatial": settings.spatial.model_copy(
                update={
                    "query_crs": "EPSG:3857",
                    "area_crs": "EPSG:3857",
                    "screening_grid_meters": 500,
                    "detail_grid_meters": 250,
                }
            ),
            "soilgrids": settings.soilgrids.model_copy(update={"subset_crs": "EPSG:3857"}),
        }
    )


def _grid() -> pa.Table:
    cells = [
        box(0, 0, 500, 500),
        box(500, 0, 1000, 500),
        box(0, 500, 500, 1000),
        box(500, 500, 1000, 1000),
    ]
    return pa.table(
        {
            "grid_id": ["g0", "g1", "g2", "g3"],
            "geometry": [cell.wkb for cell in cells],
        }
    )


def _boundary() -> bytes:
    return (
        b'{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},'
        b'"geometry":{"type":"Polygon","coordinates":[[[0,0],[1000,0],[1000,1000],'
        b"[0,1000],[0,0]]]}}]}"
    )


def _write_sources(settings: Settings) -> dict[str, dict[tuple[str, str], Path]]:
    raw_values = {
        "clay": [100, 200, 300],
        "sand": [300, 400, 500],
        "soc": [50, 100, 150],
        "bdod": [100, 120, 140],
    }
    grouped: dict[str, dict[tuple[str, str], Path]] = {"r000_c000": {}}
    for property_name, values_by_depth in raw_values.items():
        for depth, value in zip(settings.soilgrids.depths, values_by_depth, strict=True):
            path = (
                settings.storage.root_path
                / "soilgrids"
                / settings.study.partition_key
                / f"property={property_name}"
                / f"depth={depth}"
                / "chunk_id=r000_c000"
                / f"{property_name}_{depth}_Q0.5.tif"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            values = np.full((4, 4), value, dtype=np.int16)
            with rasterio.open(
                path,
                "w",
                driver="GTiff",
                width=4,
                height=4,
                count=1,
                dtype=values.dtype,
                transform=from_origin(0, 1000, 250, 250),
            ) as destination:
                destination.write(values, 1)
            path.with_suffix(".manifest.json").write_text(
                json.dumps({"downloaded_at_utc": "2026-08-14T00:00:00+00:00"})
            )
            grouped["r000_c000"][(property_name, depth)] = path
    return grouped


def test_aggregates_spatial_and_depth_weighted_soil_properties(settings: Settings) -> None:
    settings = _settings(settings)
    grouped = _write_sources(settings)

    table = soil_features.aggregate_soil_features(
        settings,
        grid=_grid(),
        boundary=_boundary(),
        grouped_paths=grouped,
    )

    assert table.column_names == ["grid_id", "clay_pct", "sand_pct", "soc", "bulk_density"]
    assert table.column("clay_pct")[0].as_py() == pytest.approx(23.333333)
    assert table.column("sand_pct")[0].as_py() == pytest.approx(43.333333)
    assert table.column("soc")[0].as_py() == pytest.approx(11.666667)
    assert table.column("bulk_density")[0].as_py() == pytest.approx(1.266667)
    assert table.schema.field("soc").metadata[b"unit"] == b"g/kg"
    assert table.schema.metadata[b"vertical_aggregation"] == b"depth_thickness_weighted_mean"


def test_discovers_complete_chunks_and_writes_quality(settings: Settings) -> None:
    settings = _settings(settings)
    grouped = _write_sources(settings)
    assert soil_features.source_paths(settings).keys() == grouped.keys()
    table = soil_features.aggregate_soil_features(
        settings,
        grid=_grid(),
        boundary=_boundary(),
        grouped_paths=grouped,
    )

    result = soil_features.write_soil_features(settings, table, grouped_paths=grouped)

    assert pq.ParquetFile(result.parquet_path).read().num_rows == 4
    quality = json.loads(result.quality_path.read_text())
    metadata = json.loads(result.metadata_path.read_text())
    assert quality["duplicate_grid_id_count"] == 0
    assert quality["complete_row_count"] == 4
    assert quality["range_by_column"]["clay_pct"] == {
        "minimum": pytest.approx(23.333333),
        "maximum": pytest.approx(23.333333),
    }
    assert metadata["source_crs"] == "EPSG:3857"
    assert metadata["source_resolution_meters"] == 250
    assert metadata["source_resolution_meters_observed_min"] == 250
    assert metadata["source_resolution_meters_observed_max"] == 250
    assert metadata["resampling_method"].startswith("none")
    assert "raw values <= 0" in metadata["nodata_rule"]


def test_treats_undeclared_wcs_zero_fill_as_missing(settings: Settings) -> None:
    settings = _settings(settings)
    grouped = _write_sources(settings)
    for path in grouped["r000_c000"].values():
        with rasterio.open(path, "r+") as dataset:
            values = dataset.read(1)
            values[:2, 2:] = 0
            dataset.write(values, 1)

    table = soil_features.aggregate_soil_features(
        settings,
        grid=_grid(),
        boundary=_boundary(),
        grouped_paths=grouped,
    )

    assert all(table.column(column)[3].as_py() is None for column in table.column_names[1:])


def test_rejects_invalid_depth_and_incomplete_source_groups(settings: Settings) -> None:
    settings = _settings(settings)
    _write_sources(settings)
    missing_path = next(
        (
            settings.storage.root_path
            / "soilgrids"
            / settings.study.partition_key
            / "property=clay"
            / "depth=0-5cm"
        ).glob("chunk_id=*/*.tif")
    )
    missing_path.unlink()

    with pytest.raises(FileNotFoundError, match="Canonical SoilGrids artifact not found"):
        soil_features.source_paths(settings)
    with pytest.raises(ValueError, match="Invalid SoilGrids depth interval"):
        soil_features._depth_thickness("30-15cm")
