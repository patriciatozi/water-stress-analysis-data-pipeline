from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import rasterio  # type: ignore[import-untyped]
from pyproj import Transformer
from rasterio.enums import Resampling  # type: ignore[import-untyped]
from rasterio.features import rasterize  # type: ignore[import-untyped]
from rasterio.transform import array_bounds  # type: ignore[import-untyped]
from rasterio.vrt import WarpedVRT  # type: ignore[import-untyped]
from rasterio.windows import Window  # type: ignore[import-untyped]
from rasterio.windows import transform as window_transform
from shapely import from_wkb
from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform
from shapely.strtree import STRtree

from water_stress.config import Settings
from water_stress.ingestion import mapbiomas, sentinel_2
from water_stress.transformation import crop_mask, spatial_grid

VALID_SCL_CLASSES = frozenset({4, 5, 6, 7})
CLOUD_SCL_CLASSES = frozenset({8, 9, 10})
HISTOGRAM_BINS = 400
BLOCK_SIZE = 512
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SatelliteObservationResult:
    item_id: str
    parquet_path: Path
    quality_path: Path
    row_count: int
    state: str


def _field(name: str, data_type: pa.DataType, unit: str, description: str) -> pa.Field[Any]:
    return pa.field(name, data_type, metadata={"unit": unit, "description": description})


def silver_schema(settings: Settings) -> pa.Schema:
    fields = [
        _field("grid_id", pa.string(), "identifier", "Spatial grid foreign key"),
        _field("date", pa.date32(), "date", "Sentinel-2 acquisition date in UTC"),
        _field("tile_id", pa.string(), "identifier", "MGRS tile identifier"),
        _field("item_id", pa.string(), "identifier", "Sentinel-2 STAC item identifier"),
        _field("ndvi_mean", pa.float64(), "index", "Mean NDVI of valid soybean pixels"),
        _field("ndvi_p10", pa.float64(), "index", "Approximate NDVI 10th percentile"),
        _field("ndvi_p50", pa.float64(), "index", "Approximate NDVI median"),
        _field("ndvi_p90", pa.float64(), "index", "Approximate NDVI 90th percentile"),
        _field("ndmi_mean", pa.float64(), "index", "Mean NDMI of valid soybean pixels"),
        _field("ndmi_p10", pa.float64(), "index", "Approximate NDMI 10th percentile"),
        _field("ndmi_p50", pa.float64(), "index", "Approximate NDMI median"),
        _field("ndmi_p90", pa.float64(), "index", "Approximate NDMI 90th percentile"),
        _field("soy_pixel_count", pa.int64(), "pixels", "MapBiomas soybean pixels at 10 m"),
        _field("valid_pixel_count", pa.int64(), "pixels", "Pixels valid for both indices"),
        _field("valid_pixel_pct", pa.float64(), "%", "Valid pixels among soybean pixels"),
        _field("cloud_pixel_pct", pa.float64(), "%", "SCL cloud pixels among soybean pixels"),
    ]
    return pa.schema(
        fields,
        metadata={
            "source": "Sentinel-2 L2A via Earth Search",
            "layer": "silver",
            "dataset": "satellite_observation",
            "grid_crs": settings.spatial.area_crs,
            "processing_version": settings.project.version,
            "analysis_resolution_meters": "10",
        },
    )


def normalized_difference(
    first: np.ndarray[Any, Any], second: np.ndarray[Any, Any]
) -> np.ndarray[Any, Any]:
    denominator = first + second
    result = np.divide(
        first - second,
        denominator,
        out=np.full(first.shape, np.nan, dtype=np.float32),
        where=np.abs(denominator) > 1e-6,
    )
    return cast(np.ndarray[Any, Any], result)


def _histogram_percentile(histogram: np.ndarray[Any, Any], quantile: float) -> float | None:
    total = int(histogram.sum())
    if total == 0:
        return None
    target = max(1, math.ceil(total * quantile))
    index = int(np.searchsorted(np.cumsum(histogram), target))
    return -1 + (index + 0.5) * (2 / HISTOGRAM_BINS)


def _catalog(settings: Settings) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = sentinel_2.catalog_path(settings)
    manifest_path = path.with_suffix(".manifest.json")
    if not path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError(
            "Sentinel-2 Bronze catalog or manifest not found; run ingestion first"
        )
    document = json.loads(path.read_bytes())
    features = document.get("features")
    if not isinstance(features, list):
        raise ValueError("Sentinel-2 Bronze catalog has no feature list")
    return features, json.loads(manifest_path.read_text())


def _tile_id(item: dict[str, Any]) -> str:
    grid_code = item.get("properties", {}).get("grid:code")
    if isinstance(grid_code, str) and grid_code.startswith("MGRS-"):
        return grid_code.removeprefix("MGRS-")
    parts = str(item.get("id", "")).split("_")
    if len(parts) >= 2 and len(parts[1]) == 5:
        return parts[1]
    raise ValueError(f"Cannot determine MGRS tile for item {item.get('id')}")


def _asset_href(settings: Settings, item: dict[str, Any], asset_name: str) -> str:
    local = sentinel_2.asset_path(settings, str(item["id"]), asset_name)
    if local.is_file():
        return str(local)
    asset = item.get("assets", {}).get(asset_name)
    if not isinstance(asset, dict) or not isinstance(asset.get("href"), str):
        raise ValueError(f"Sentinel-2 item {item['id']} is missing asset {asset_name}")
    return str(asset["href"])


def _load_soy_grid(settings: Settings) -> tuple[list[str], list[BaseGeometry]]:
    grid_path = spatial_grid.dataset_path(settings) / "grid.parquet"
    mask_path = crop_mask.dataset_path(settings) / "part-000.parquet"
    if not grid_path.is_file() or not mask_path.is_file():
        raise FileNotFoundError("dim_spatial_grid and crop_mask Silver inputs are required")
    grid = pq.read_table(grid_path, columns=["grid_id", "geometry"])
    mask = pq.ParquetFile(mask_path).read(columns=["grid_id", "soy_fraction"])
    fractions = dict(
        zip(mask["grid_id"].to_pylist(), mask["soy_fraction"].to_pylist(), strict=True)
    )
    ids: list[str] = []
    geometries: list[BaseGeometry] = []
    for grid_id, raw_geometry in zip(
        grid["grid_id"].to_pylist(), grid["geometry"].to_pylist(), strict=True
    ):
        fraction = fractions.get(grid_id)
        if fraction is None or float(fraction) <= 0:
            continue
        geometry = from_wkb(raw_geometry)
        if geometry is None or not geometry.is_valid or geometry.is_empty:
            raise ValueError(f"Invalid grid geometry for {grid_id}")
        ids.append(str(grid_id))
        geometries.append(geometry)
    return ids, geometries


def select_items(
    items: list[dict[str, Any]], *, item_ids: set[str] | None, max_items: int
) -> list[dict[str, Any]]:
    ordered = sorted(items, key=lambda item: (item["properties"]["datetime"], item["id"]))
    if item_ids:
        selected = [item for item in ordered if item.get("id") in item_ids]
        missing = item_ids - {str(item["id"]) for item in selected}
        if missing:
            raise ValueError(f"Sentinel-2 item IDs not found in catalog: {sorted(missing)}")
        return selected
    if max_items < 1:
        raise ValueError("max_items must be at least one")
    return ordered[:max_items]


def item_output_path(settings: Settings, item: dict[str, Any]) -> Path:
    acquired = datetime.fromisoformat(item["properties"]["datetime"].replace("Z", "+00:00"))
    return (
        dataset_path(settings)
        / f"year={acquired.year}"
        / f"month={acquired.month:02d}"
        / f"tile_id={_tile_id(item)}"
        / f"item_id={item['id']}"
        / "part-000.parquet"
    )


def dataset_path(settings: Settings) -> Path:
    return (
        settings.storage.silver_root_path / "satellite_observation" / settings.study.partition_key
    )


def _candidate_cells(
    settings: Settings,
    item: dict[str, Any],
    grid_ids: list[str],
    grid_geometries: list[BaseGeometry],
    target_crs: Any,
) -> tuple[list[str], list[BaseGeometry]]:
    to_query = Transformer.from_crs(settings.spatial.area_crs, "EPSG:4326", always_xy=True)
    footprint = shape(item["geometry"])
    selected = [
        index
        for index, geometry in enumerate(grid_geometries)
        if footprint.contains(shapely_transform(to_query.transform, geometry.centroid))
    ]
    to_target = Transformer.from_crs(settings.spatial.area_crs, target_crs, always_xy=True)
    return (
        [grid_ids[index] for index in selected],
        [shapely_transform(to_target.transform, grid_geometries[index]) for index in selected],
    )


def _reflectance_parameters(item: dict[str, Any], asset_name: str) -> tuple[float, float]:
    bands = item["assets"][asset_name].get("raster:bands", [{}])
    return float(bands[0].get("scale", 1)), float(bands[0].get("offset", 0))


def _empty_accumulators(cell_count: int) -> dict[str, np.ndarray[Any, Any]]:
    return {
        "soy": np.zeros(cell_count, dtype=np.int64),
        "valid": np.zeros(cell_count, dtype=np.int64),
        "cloud": np.zeros(cell_count, dtype=np.int64),
        "ndvi_sum": np.zeros(cell_count, dtype=np.float64),
        "ndmi_sum": np.zeros(cell_count, dtype=np.float64),
        "ndvi_hist": np.zeros((cell_count, HISTOGRAM_BINS), dtype=np.uint32),
        "ndmi_hist": np.zeros((cell_count, HISTOGRAM_BINS), dtype=np.uint32),
    }


def _accumulate(
    accumulators: dict[str, np.ndarray[Any, Any]],
    labels: np.ndarray[Any, Any],
    soy: np.ndarray[Any, Any],
    scl: np.ndarray[Any, Any],
    red: np.ndarray[Any, Any],
    nir: np.ndarray[Any, Any],
    swir: np.ndarray[Any, Any],
) -> None:
    labelled_soy = (labels > 0) & soy
    if not np.any(labelled_soy):
        return
    indices = labels[labelled_soy].astype(np.int64) - 1
    accumulators["soy"] += np.bincount(indices, minlength=len(accumulators["soy"]))
    cloud = labelled_soy & np.isin(scl, list(CLOUD_SCL_CLASSES))
    accumulators["cloud"] += np.bincount(
        labels[cloud].astype(np.int64) - 1, minlength=len(accumulators["soy"])
    )
    valid = labelled_soy & np.isin(scl, list(VALID_SCL_CLASSES))
    valid &= np.isfinite(red) & np.isfinite(nir) & np.isfinite(swir)
    valid &= (red > 0) & (nir > 0) & (swir > 0)
    ndvi = normalized_difference(nir, red)
    ndmi = normalized_difference(nir, swir)
    valid &= np.isfinite(ndvi) & np.isfinite(ndmi)
    if not np.any(valid):
        return
    valid_indices = labels[valid].astype(np.int64) - 1
    valid_ndvi = np.clip(ndvi[valid], -1, 1)
    valid_ndmi = np.clip(ndmi[valid], -1, 1)
    count = len(accumulators["soy"])
    accumulators["valid"] += np.bincount(valid_indices, minlength=count)
    accumulators["ndvi_sum"] += np.bincount(valid_indices, weights=valid_ndvi, minlength=count)
    accumulators["ndmi_sum"] += np.bincount(valid_indices, weights=valid_ndmi, minlength=count)
    ndvi_bins = np.minimum(((valid_ndvi + 1) / 2 * HISTOGRAM_BINS).astype(int), HISTOGRAM_BINS - 1)
    ndmi_bins = np.minimum(((valid_ndmi + 1) / 2 * HISTOGRAM_BINS).astype(int), HISTOGRAM_BINS - 1)
    np.add.at(accumulators["ndvi_hist"], (valid_indices, ndvi_bins), 1)
    np.add.at(accumulators["ndmi_hist"], (valid_indices, ndmi_bins), 1)


def _build_table(
    settings: Settings,
    item: dict[str, Any],
    grid_ids: list[str],
    accumulators: dict[str, np.ndarray[Any, Any]],
) -> pa.Table:
    acquired = datetime.fromisoformat(item["properties"]["datetime"].replace("Z", "+00:00")).date()
    rows: dict[str, list[Any]] = {name: [] for name in silver_schema(settings).names}
    for index, grid_id in enumerate(grid_ids):
        soy_count = int(accumulators["soy"][index])
        if soy_count == 0:
            continue
        valid_count = int(accumulators["valid"][index])
        rows["grid_id"].append(grid_id)
        rows["date"].append(acquired)
        rows["tile_id"].append(_tile_id(item))
        rows["item_id"].append(str(item["id"]))
        for metric in ("ndvi", "ndmi"):
            rows[f"{metric}_mean"].append(
                None
                if valid_count == 0
                else float(accumulators[f"{metric}_sum"][index] / valid_count)
            )
            histogram = accumulators[f"{metric}_hist"][index]
            for label, quantile in (("p10", 0.1), ("p50", 0.5), ("p90", 0.9)):
                rows[f"{metric}_{label}"].append(_histogram_percentile(histogram, quantile))
        rows["soy_pixel_count"].append(soy_count)
        rows["valid_pixel_count"].append(valid_count)
        rows["valid_pixel_pct"].append(100 * valid_count / soy_count)
        rows["cloud_pixel_pct"].append(100 * int(accumulators["cloud"][index]) / soy_count)
    return pa.table(rows, schema=silver_schema(settings))


def process_item(
    settings: Settings,
    item: dict[str, Any],
    *,
    grid_ids: list[str],
    grid_geometries: list[BaseGeometry],
) -> pa.Table:
    hrefs = {name: _asset_href(settings, item, name) for name in ("red", "nir", "swir16", "scl")}
    mapbiomas_path = mapbiomas.artifact_path(settings)
    if not mapbiomas_path.is_file():
        raise FileNotFoundError(f"MapBiomas Bronze raster not found at {mapbiomas_path}")
    environment = rasterio.Env(
        AWS_NO_SIGN_REQUEST="YES",
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        GDAL_HTTP_MAX_RETRY="3",
        GDAL_HTTP_RETRY_DELAY="1",
    )
    with (
        environment,
        rasterio.open(hrefs["red"]) as red_source,
        rasterio.open(hrefs["nir"]) as nir_source,
        rasterio.open(hrefs["swir16"]) as swir_source,
        rasterio.open(hrefs["scl"]) as scl_source,
        rasterio.open(mapbiomas_path) as crop_source,
    ):
        if red_source.crs is None:
            raise ValueError("Sentinel-2 red asset has no CRS")
        if red_source.shape != nir_source.shape or red_source.transform != nir_source.transform:
            raise ValueError("Sentinel-2 red and NIR assets are not aligned")
        item_grid_ids, item_geometries = _candidate_cells(
            settings, item, grid_ids, grid_geometries, red_source.crs
        )
        if not item_grid_ids:
            return pa.table(
                {name: [] for name in silver_schema(settings).names}, schema=silver_schema(settings)
            )
        tree = STRtree(item_geometries)
        accumulators = _empty_accumulators(len(item_grid_ids))
        vrt_options = {
            "crs": red_source.crs,
            "transform": red_source.transform,
            "width": red_source.width,
            "height": red_source.height,
        }
        with (
            WarpedVRT(swir_source, **vrt_options, resampling=Resampling.bilinear) as swir_vrt,
            WarpedVRT(scl_source, **vrt_options, resampling=Resampling.nearest) as scl_vrt,
            WarpedVRT(crop_source, **vrt_options, resampling=Resampling.nearest) as crop_vrt,
        ):
            red_scale, red_offset = _reflectance_parameters(item, "red")
            nir_scale, nir_offset = _reflectance_parameters(item, "nir")
            swir_scale, swir_offset = _reflectance_parameters(item, "swir16")
            for row in range(0, red_source.height, BLOCK_SIZE):
                for column in range(0, red_source.width, BLOCK_SIZE):
                    window = Window(
                        column,
                        row,
                        min(BLOCK_SIZE, red_source.width - column),
                        min(BLOCK_SIZE, red_source.height - row),
                    )
                    transform = window_transform(window, red_source.transform)
                    bounds = array_bounds(int(window.height), int(window.width), transform)
                    candidate_indices = tree.query(box(*bounds))
                    if len(candidate_indices) == 0:
                        continue
                    labels = rasterize(
                        (
                            (item_geometries[int(index)], int(index) + 1)
                            for index in candidate_indices
                        ),
                        out_shape=(int(window.height), int(window.width)),
                        transform=transform,
                        fill=0,
                        dtype="int32",
                    )
                    red_raw = red_source.read(1, window=window, masked=True)
                    nir_raw = nir_source.read(1, window=window, masked=True)
                    swir_raw = swir_vrt.read(1, window=window, masked=True)
                    scl = scl_vrt.read(1, window=window)
                    crop = crop_vrt.read(1, window=window)
                    red = np.where(
                        np.ma.getmaskarray(red_raw), np.nan, red_raw * red_scale + red_offset
                    )
                    nir = np.where(
                        np.ma.getmaskarray(nir_raw), np.nan, nir_raw * nir_scale + nir_offset
                    )
                    swir = np.where(
                        np.ma.getmaskarray(swir_raw), np.nan, swir_raw * swir_scale + swir_offset
                    )
                    _accumulate(
                        accumulators,
                        labels,
                        crop == settings.mapbiomas.soybean_class,
                        scl,
                        red,
                        nir,
                        swir,
                    )
    return _build_table(settings, item, item_grid_ids, accumulators)


def _write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def write_item(
    settings: Settings,
    item: dict[str, Any],
    table: pa.Table,
    *,
    source_extraction_timestamp: str | None,
) -> SatelliteObservationResult:
    output = item_output_path(settings, item)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".parquet.tmp")
    pq.write_table(table, temporary, compression="zstd")
    temporary.replace(output)
    quality_path = output.with_name("_quality.json")
    range_columns = [
        "ndvi_mean",
        "ndmi_mean",
        "valid_pixel_pct",
        "cloud_pixel_pct",
    ]
    ranges = {
        name: {
            "minimum": min(
                (value for value in table[name].to_pylist() if value is not None), default=None
            ),
            "maximum": max(
                (value for value in table[name].to_pylist() if value is not None), default=None
            ),
        }
        for name in range_columns
    }
    _write_json(
        quality_path,
        {
            "dataset": "satellite_observation",
            "item_id": item["id"],
            "tile_id": _tile_id(item),
            "acquired_at": item["properties"]["datetime"],
            "scene_cloud_cover_pct": item["properties"].get("eo:cloud_cover"),
            "source_extraction_timestamp": source_extraction_timestamp,
            "row_count": table.num_rows,
            "duplicate_key_count": table.num_rows
            - len(
                set(
                    zip(
                        table["grid_id"].to_pylist(),
                        table["date"].to_pylist(),
                        table["item_id"].to_pylist(),
                        strict=True,
                    )
                )
            ),
            "missing_by_column": {name: table[name].null_count for name in table.column_names},
            "range_by_column": ranges,
            "valid_scl_classes": sorted(VALID_SCL_CLASSES),
            "cloud_scl_classes": sorted(CLOUD_SCL_CLASSES),
            "processing_version": settings.project.version,
            "processed_at_utc": datetime.now(UTC).isoformat(),
        },
    )
    root = dataset_path(settings)
    _write_json(
        root / "_metadata.json",
        {
            "dataset": "satellite_observation",
            "source": "Sentinel-2 L2A via Earth Search",
            "source_extraction_timestamp": source_extraction_timestamp,
            "crs": settings.spatial.area_crs,
            "analysis_resolution_meters": 10,
            "soy_mask": "MapBiomas class 39, nearest-neighbor reprojection",
            "reflectance_validity": "finite and strictly positive after L2A scale and offset",
            "swir16_resampling": "bilinear from 20 m to the B04/B08 10 m grid",
            "scl_resampling": "nearest from 20 m to the B04/B08 10 m grid",
            "percentile_method": f"fixed histogram with {HISTOGRAM_BINS} bins from -1 to 1",
            "processing_version": settings.project.version,
        },
    )
    _write_json(
        root / "_schema.json",
        {
            "dataset": "satellite_observation",
            "columns": [
                {
                    "name": field.name,
                    "type": str(field.type),
                    "unit": (field.metadata or {}).get(b"unit", b"").decode(),
                    "description": (field.metadata or {}).get(b"description", b"").decode(),
                }
                for field in table.schema
            ],
        },
    )
    LOGGER.info(
        "Silver satellite observation written",
        extra={
            "dataset": "satellite_observation",
            "item_id": item["id"],
            "row_count": table.num_rows,
        },
    )
    return SatelliteObservationResult(
        str(item["id"]), output, quality_path, table.num_rows, "written"
    )


def transform(
    settings: Settings, *, item_ids: set[str] | None = None, max_items: int = 1
) -> list[SatelliteObservationResult]:
    items, catalog_manifest = _catalog(settings)
    if item_ids:
        selected = select_items(items, item_ids=item_ids, max_items=max_items)
    else:
        ordered = select_items(items, item_ids=None, max_items=len(items))
        pending = [item for item in ordered if not item_output_path(settings, item).is_file()]
        selected = pending[:max_items] if pending else ordered[:max_items]
    grid_ids, grid_geometries = _load_soy_grid(settings)
    results: list[SatelliteObservationResult] = []
    for item in selected:
        output = item_output_path(settings, item)
        quality = output.with_name("_quality.json")
        if output.is_file() and quality.is_file():
            table = pq.ParquetFile(output).read()
            results.append(
                SatelliteObservationResult(
                    str(item["id"]), output, quality, table.num_rows, "reused"
                )
            )
            continue
        table = process_item(settings, item, grid_ids=grid_ids, grid_geometries=grid_geometries)
        timestamp = catalog_manifest.get("downloaded_at_utc")
        results.append(
            write_item(
                settings,
                item,
                table,
                source_extraction_timestamp=timestamp if isinstance(timestamp, str) else None,
            )
        )
    return results
