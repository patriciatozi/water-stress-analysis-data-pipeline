from __future__ import annotations

import json

import pyarrow.parquet as pq

from water_stress.config import Settings
from water_stress.transformation import spatial_grid


def test_builds_stable_geoparquet_grid_and_metadata(
    settings: Settings, polygon_geojson: bytes
) -> None:
    settings = settings.model_copy(
        update={
            "spatial": settings.spatial.model_copy(
                update={
                    "area_crs": "EPSG:3857",
                    "screening_grid_meters": 100_000,
                    "detail_grid_meters": 25_000,
                }
            )
        }
    )

    table = spatial_grid.build_grid(settings, polygon_geojson)
    result = spatial_grid.write_grid(settings, table)

    assert table.num_rows > 0
    assert len(set(table.column("grid_id").to_pylist())) == table.num_rows
    assert table.column("area_km2")[0].as_py() == 10_000
    geo = json.loads(table.schema.metadata[b"geo"])
    assert geo["primary_column"] == "geometry"
    written = pq.read_table(result.geoparquet_path)
    assert written.num_rows == table.num_rows
    metadata = json.loads(result.metadata_path.read_text())
    assert metadata["resolution_meters"] == 100_000
    assert metadata["area_crs"] == "EPSG:3857"


def test_spatial_grid_requires_existing_boundary(settings: Settings) -> None:
    try:
        spatial_grid.transform_boundary(settings)
    except FileNotFoundError as exc:
        assert "IBGE Bronze boundary" in str(exc)
    else:
        raise AssertionError("Expected missing boundary error")
