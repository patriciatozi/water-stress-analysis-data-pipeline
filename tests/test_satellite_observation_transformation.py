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
from water_stress.ingestion import mapbiomas, sentinel_2
from water_stress.transformation import crop_mask, satellite_observation, spatial_grid


def _item(item_id: str = "S2B_21LWG_20230901_0_L2A") -> dict[str, object]:
    return {
        "id": item_id,
        "properties": {"datetime": "2023-09-01T14:00:00Z", "eo:cloud_cover": 12.5},
    }


def test_normalized_difference_handles_zero_denominator() -> None:
    first = np.array([0.8, 0.0, 0.2], dtype=np.float32)
    second = np.array([0.2, 0.0, 0.6], dtype=np.float32)

    result = satellite_observation.normalized_difference(first, second)

    assert result[0] == pytest.approx(0.6)
    assert np.isnan(result[1])
    assert result[2] == pytest.approx(-0.5)


def test_applies_soy_and_scl_masks_and_aggregates_indices(settings: Settings) -> None:
    labels = np.array([[1, 1], [2, 2]], dtype=np.int32)
    soy = np.ones((2, 2), dtype=bool)
    scl = np.array([[4, 9], [5, 7]], dtype=np.uint8)
    red = np.full((2, 2), 0.2, dtype=np.float32)
    nir = np.full((2, 2), 0.6, dtype=np.float32)
    swir = np.full((2, 2), 0.3, dtype=np.float32)
    accumulators = satellite_observation._empty_accumulators(2)

    satellite_observation._accumulate(accumulators, labels, soy, scl, red, nir, swir)
    table = satellite_observation._build_table(settings, _item(), ["g1", "g2"], accumulators)

    assert table.num_rows == 2
    assert table["soy_pixel_count"].to_pylist() == [2, 2]
    assert table["valid_pixel_count"].to_pylist() == [1, 2]
    assert table["valid_pixel_pct"].to_pylist() == [50.0, 100.0]
    assert table["cloud_pixel_pct"].to_pylist() == [50.0, 0.0]
    assert table["ndvi_mean"].to_pylist() == pytest.approx([0.5, 0.5])
    assert table["ndmi_mean"].to_pylist() == pytest.approx([1 / 3, 1 / 3])
    assert table.schema.field("ndvi_mean").metadata[b"unit"] == b"index"


def test_selects_explicit_or_limited_catalog_items() -> None:
    first = _item("S2B_21LWG_20230901_0_L2A")
    second = _item("S2B_21LWG_20230902_0_L2A")
    second["properties"] = {"datetime": "2023-09-02T14:00:00Z"}

    assert satellite_observation.select_items([second, first], item_ids=None, max_items=1) == [
        first
    ]
    assert satellite_observation.select_items(
        [first, second], item_ids={str(second["id"])}, max_items=1
    ) == [second]
    with pytest.raises(ValueError, match="not found"):
        satellite_observation.select_items([first], item_ids={"missing"}, max_items=1)


def test_rejects_nonpositive_reflectance_after_l2a_offset() -> None:
    accumulators = satellite_observation._empty_accumulators(1)
    satellite_observation._accumulate(
        accumulators,
        np.ones((1, 1), dtype=np.int32),
        np.ones((1, 1), dtype=bool),
        np.full((1, 1), 4, dtype=np.uint8),
        np.full((1, 1), 0.0, dtype=np.float32),
        np.full((1, 1), 0.5, dtype=np.float32),
        np.full((1, 1), 0.3, dtype=np.float32),
    )

    assert accumulators["soy"][0] == 1
    assert accumulators["valid"][0] == 0


def test_writes_partition_with_quality_and_provenance(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(
        update={
            "storage": settings.storage.model_copy(update={"silver_root_path": tmp_path / "silver"})
        }
    )
    accumulators = satellite_observation._empty_accumulators(1)
    accumulators["soy"][0] = 10
    table = satellite_observation._build_table(settings, _item(), ["g1"], accumulators)

    result = satellite_observation.write_item(
        settings,
        _item(),
        table,
        source_extraction_timestamp="2026-08-15T22:29:36+00:00",
    )

    assert pq.ParquetFile(result.parquet_path).read().num_rows == 1
    assert "year=2023" in str(result.parquet_path)
    quality = json.loads(result.quality_path.read_text())
    metadata = json.loads(
        satellite_observation.dataset_path(settings).joinpath("_metadata.json").read_text()
    )
    assert quality["source_extraction_timestamp"] == "2026-08-15T22:29:36+00:00"
    assert metadata["swir16_resampling"].startswith("bilinear")
    assert metadata["scl_resampling"].startswith("nearest")
    assert metadata["reflectance_validity"].startswith("finite")


def _write_raster(path: Path, values: np.ndarray[object, object], resolution: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=values.shape[1],
        height=values.shape[0],
        count=1,
        dtype=values.dtype,
        crs="EPSG:3857",
        transform=from_origin(0, 200, resolution, resolution),
        nodata=0,
    ) as destination:
        destination.write(values, 1)


def test_processes_aligned_local_assets_without_raster_intermediate(settings: Settings) -> None:
    settings = settings.model_copy(
        update={
            "spatial": settings.spatial.model_copy(
                update={"area_crs": "EPSG:3857", "query_crs": "EPSG:4326"}
            )
        }
    )
    item = _item()
    item["geometry"] = {
        "type": "Polygon",
        "coordinates": [
            [[-0.01, -0.01], [0.01, -0.01], [0.01, 0.01], [-0.01, 0.01], [-0.01, -0.01]]
        ],
    }
    item["assets"] = {
        name: {"raster:bands": [{"scale": 0.0001, "offset": -0.1}]}
        for name in ("red", "nir", "swir16")
    }
    item["assets"]["scl"] = {"raster:bands": [{}]}  # type: ignore[index]
    item_id = str(item["id"])
    _write_raster(
        sentinel_2.asset_path(settings, item_id, "red"),
        np.full((20, 20), 3000, dtype=np.uint16),
        10,
    )
    _write_raster(
        sentinel_2.asset_path(settings, item_id, "nir"),
        np.full((20, 20), 6000, dtype=np.uint16),
        10,
    )
    _write_raster(
        sentinel_2.asset_path(settings, item_id, "swir16"),
        np.full((10, 10), 4000, dtype=np.uint16),
        20,
    )
    _write_raster(
        sentinel_2.asset_path(settings, item_id, "scl"),
        np.full((10, 10), 4, dtype=np.uint8),
        20,
    )
    _write_raster(
        mapbiomas.artifact_path(settings),
        np.full((20, 20), settings.mapbiomas.soybean_class, dtype=np.uint8),
        10,
    )

    table = satellite_observation.process_item(
        settings,
        item,  # type: ignore[arg-type]
        grid_ids=["g1"],
        grid_geometries=[box(0, 0, 200, 200)],
    )

    assert table.num_rows == 1
    assert table["soy_pixel_count"][0].as_py() == 400
    assert table["valid_pixel_count"][0].as_py() == 400
    assert table["ndvi_mean"][0].as_py() == pytest.approx(3 / 7)
    assert table["ndmi_mean"][0].as_py() == pytest.approx(0.25)


def test_loads_catalog_soy_grid_and_remote_asset_contract(settings: Settings) -> None:
    item = _item()
    item["assets"] = {"red": {"href": "https://example.test/red.tif"}}
    catalog_path = sentinel_2.catalog_path(settings)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(json.dumps({"type": "FeatureCollection", "features": [item]}))
    catalog_path.with_suffix(".manifest.json").write_text(
        json.dumps({"downloaded_at_utc": "2026-08-15T22:29:36+00:00"})
    )
    grid_path = spatial_grid.dataset_path(settings) / "grid.parquet"
    grid_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "grid_id": ["soy", "other"],
                "geometry": [box(0, 0, 1, 1).wkb, box(1, 0, 2, 1).wkb],
            }
        ),
        grid_path,
    )
    mask_path = crop_mask.dataset_path(settings) / "part-000.parquet"
    mask_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"grid_id": ["soy", "other"], "soy_fraction": [0.5, 0.0]}),
        mask_path,
    )

    items, manifest = satellite_observation._catalog(settings)
    grid_ids, geometries = satellite_observation._load_soy_grid(settings)

    assert items[0]["id"] == item["id"]
    assert manifest["downloaded_at_utc"].startswith("2026")
    assert grid_ids == ["soy"]
    assert geometries[0].area == 1
    assert satellite_observation._asset_href(settings, item, "red") == (
        "https://example.test/red.tif"
    )
    assert "tile_id=21LWG" in str(satellite_observation.item_output_path(settings, item))
