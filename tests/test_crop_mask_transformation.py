from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from water_stress.config import Settings
from water_stress.transformation import crop_mask


def _grid() -> pa.Table:
    cells = [box(0, 0, 2, 2), box(2, 0, 4, 2), box(0, 2, 2, 4), box(2, 2, 4, 4)]
    return pa.table(
        {
            "grid_id": ["g0", "g1", "g2", "g3"],
            "geometry": [cell.wkb for cell in cells],
        }
    )


def _settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "spatial": settings.spatial.model_copy(
                update={
                    "query_crs": "EPSG:3857",
                    "area_crs": "EPSG:3857",
                    "screening_grid_meters": 2,
                    "detail_grid_meters": 1,
                }
            )
        }
    )


def _write_raster(path: Path) -> None:
    values = np.array(
        [
            [39, 39, 0, 0],
            [39, 0, 0, 0],
            [0, 0, 39, 39],
            [0, 0, 39, 39],
        ],
        dtype=np.uint8,
    )
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=1,
        dtype=values.dtype,
        crs="EPSG:3857",
        transform=from_origin(0, 4, 1, 1),
        nodata=255,
    ) as destination:
        destination.write(values, 1)


def test_aggregates_mapbiomas_class_by_grid_cell(
    settings: Settings, polygon_geojson: bytes, tmp_path: Path
) -> None:
    settings = _settings(settings)
    raster_path = tmp_path / "mapbiomas.tif"
    _write_raster(raster_path)

    table = crop_mask.aggregate_soy_fraction(
        settings,
        grid=_grid(),
        boundary=polygon_geojson,
        raster_path=raster_path,
    )

    assert table.column_names == ["grid_id", "year", "soy_fraction"]
    assert table.column("soy_fraction").to_pylist() == [0.0, 1.0, 0.75, 0.0]
    assert table.schema.metadata[b"crs"] == b"EPSG:3857"
    assert table.schema.field("soy_fraction").metadata[b"unit"] == b"fraction"


def test_writes_parquet_quality_and_provenance(settings: Settings, tmp_path: Path) -> None:
    settings = _settings(settings)
    source_path = tmp_path / "mapbiomas.tif"
    _write_raster(source_path)
    manifest_path = tmp_path / "mapbiomas.manifest.json"
    manifest_path.write_text(
        json.dumps({"downloaded_at_utc": "2026-08-19T10:00:00+00:00", "crs": "EPSG:4326"})
    )
    table = pa.table(
        {
            "grid_id": ["g0", "g1"],
            "year": [2023, 2023],
            "soy_fraction": [0.0, 0.5],
        },
        schema=crop_mask.silver_schema(settings),
    )

    result = crop_mask.write_crop_mask(
        settings,
        table,
        source_path=source_path,
        source_manifest_path=manifest_path,
    )

    assert pq.ParquetFile(result.parquet_path).read().to_pydict() == table.to_pydict()
    quality = json.loads(result.quality_path.read_text())
    metadata = json.loads(result.metadata_path.read_text())
    assert quality["duplicate_key_count"] == 0
    assert quality["soybean_cell_count"] == 1
    assert metadata["source_extraction_timestamp"] == "2026-08-19T10:00:00+00:00"
    assert metadata["source_crs"] == "EPSG:3857"
    assert metadata["source_resolution"] == [1.0, 1.0]
    assert metadata["aggregation_method"] == "source_pixel_center_count"


def test_rejects_duplicate_grid_ids(
    settings: Settings, polygon_geojson: bytes, tmp_path: Path
) -> None:
    settings = _settings(settings)
    raster_path = tmp_path / "mapbiomas.tif"
    _write_raster(raster_path)
    duplicate_grid = _grid().set_column(0, "grid_id", pa.array(["g0", "g0", "g2", "g3"]))

    try:
        crop_mask.aggregate_soy_fraction(
            settings,
            grid=duplicate_grid,
            boundary=polygon_geojson,
            raster_path=raster_path,
        )
    except ValueError as exc:
        assert "duplicate grid_id" in str(exc)
    else:
        raise AssertionError("Expected duplicate grid_id validation")
