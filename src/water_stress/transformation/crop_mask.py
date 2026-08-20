from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import rasterio  # type: ignore[import-untyped]
from pyproj import Transformer
from rasterio.windows import bounds as window_bounds  # type: ignore[import-untyped]
from shapely import contains_xy, from_wkb
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from water_stress.config import Settings
from water_stress.ingestion import ibge, mapbiomas
from water_stress.transformation import spatial_grid

LOGGER = logging.getLogger(__name__)
AGGREGATION_METHOD = "source_pixel_center_count"


@dataclass(frozen=True)
class CropMaskResult:
    dataset_path: Path
    parquet_path: Path
    schema_path: Path
    quality_path: Path
    metadata_path: Path
    row_count: int
    soybean_cell_count: int


def silver_schema(settings: Settings) -> pa.Schema:
    fields: list[pa.Field[Any]] = [
        pa.field(
            "grid_id",
            pa.string(),
            nullable=False,
            metadata={"unit": "identifier", "description": "Spatial grid foreign key"},
        ),
        pa.field(
            "year",
            pa.int16(),
            nullable=False,
            metadata={"unit": "year", "description": "MapBiomas reference year"},
        ),
        pa.field(
            "soy_fraction",
            pa.float64(),
            metadata={
                "unit": "fraction",
                "description": "Fraction of valid AOI pixels classified as soybean",
            },
        ),
    ]
    return pa.schema(
        fields,
        metadata={
            "source": "MapBiomas",
            "layer": "silver",
            "dataset": "crop_mask",
            "crs": settings.spatial.area_crs,
            "resolution_meters": str(settings.spatial.screening_grid_meters),
            "processing_version": settings.project.version,
            "aggregation_method": AGGREGATION_METHOD,
        },
    )


def _grid_layout(table: pa.Table, cell_size: int) -> tuple[float, float, dict[int, int]]:
    geometries: list[BaseGeometry] = []
    for value in table.column("geometry").to_pylist():
        geometry = from_wkb(value)
        if geometry is None or geometry.is_empty or not geometry.is_valid:
            raise ValueError("Spatial grid contains an invalid geometry")
        geometries.append(geometry)
    if not geometries:
        raise ValueError("Spatial grid is empty")
    origin_x = min(geometry.bounds[0] for geometry in geometries)
    origin_y = min(geometry.bounds[1] for geometry in geometries)
    positions: dict[int, int] = {}
    for index, geometry in enumerate(geometries):
        column = round((geometry.bounds[0] - origin_x) / cell_size)
        row = round((geometry.bounds[1] - origin_y) / cell_size)
        key = (row << 32) | column
        if key in positions:
            raise ValueError("Spatial grid contains duplicate cell positions")
        positions[key] = index
    return origin_x, origin_y, positions


def _projected_boundary(settings: Settings, boundary: bytes) -> BaseGeometry:
    geometry = ibge.geometry_from_geojson(boundary)
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError("Study area geometry must be valid and non-empty")
    projector = Transformer.from_crs(
        settings.spatial.query_crs, settings.spatial.area_crs, always_xy=True
    )
    return shapely_transform(projector.transform, geometry)


def aggregate_soy_fraction(
    settings: Settings,
    *,
    grid: pa.Table,
    boundary: bytes,
    raster_path: Path,
) -> pa.Table:
    required = {"grid_id", "geometry"}
    if not required.issubset(grid.column_names):
        raise ValueError("Spatial grid must contain grid_id and geometry")
    grid_ids = grid.column("grid_id").to_pylist()
    if len(grid_ids) != len(set(grid_ids)):
        raise ValueError("Spatial grid contains duplicate grid_id values")

    cell_size = settings.spatial.screening_grid_meters
    origin_x, origin_y, positions = _grid_layout(grid, cell_size)
    area_geometry = _projected_boundary(settings, boundary)
    valid_counts = np.zeros(grid.num_rows, dtype=np.int64)
    soybean_counts = np.zeros(grid.num_rows, dtype=np.int64)

    with rasterio.open(raster_path) as source:
        if source.crs is None:
            raise ValueError("MapBiomas raster has no CRS")
        to_area = Transformer.from_crs(source.crs, settings.spatial.area_crs, always_xy=True)
        for _, window in source.block_windows(1):
            left, bottom, right, top = window_bounds(window, source.transform)
            block_bounds = shapely_transform(
                to_area.transform,
                box(left, bottom, right, top),
            )
            if not block_bounds.intersects(area_geometry):
                continue
            values = source.read(1, window=window, masked=True)
            rows, columns = np.indices(values.shape, dtype=np.float64)
            rows += float(window.row_off) + 0.5
            columns += float(window.col_off) + 0.5
            affine = source.transform
            source_x = affine.a * columns + affine.b * rows + affine.c
            source_y = affine.d * columns + affine.e * rows + affine.f
            projected_x, projected_y = to_area.transform(source_x, source_y)
            included = ~np.ma.getmaskarray(values)
            included &= contains_xy(area_geometry, projected_x, projected_y)
            if not np.any(included):
                continue

            cell_columns = np.floor((projected_x[included] - origin_x) / cell_size).astype(np.int64)
            cell_rows = np.floor((projected_y[included] - origin_y) / cell_size).astype(np.int64)
            keys = (cell_rows << 32) | cell_columns
            source_values = np.asarray(values.data[included])
            for key in np.unique(keys):
                target_index = positions.get(int(key))
                if target_index is None:
                    continue
                selected = keys == key
                valid_counts[target_index] += int(np.count_nonzero(selected))
                soybean_counts[target_index] += int(
                    np.count_nonzero(source_values[selected] == settings.mapbiomas.soybean_class)
                )

    fractions: list[float | None] = [
        None if valid == 0 else float(soybean / valid)
        for soybean, valid in zip(soybean_counts, valid_counts, strict=True)
    ]
    table = pa.table(
        {
            "grid_id": grid_ids,
            "year": [settings.mapbiomas.reference_year] * grid.num_rows,
            "soy_fraction": fractions,
        },
        schema=silver_schema(settings),
    )
    present = [value for value in fractions if value is not None]
    if any(value < 0 or value > 1 for value in present):
        raise ValueError("soy_fraction must be between zero and one")
    return table


def dataset_path(settings: Settings) -> Path:
    return (
        settings.storage.silver_root_path
        / "crop_mask"
        / settings.study.partition_key
        / f"year={settings.mapbiomas.reference_year}"
        / f"resolution_meters={settings.spatial.screening_grid_meters}"
    )


def _schema_document(schema: pa.Schema) -> dict[str, Any]:
    return {
        "dataset": "crop_mask",
        "columns": [
            {
                "name": field.name,
                "type": str(field.type),
                "nullable": field.nullable,
                "unit": (field.metadata or {}).get(b"unit", b"").decode(),
                "description": (field.metadata or {}).get(b"description", b"").decode(),
            }
            for field in schema
        ],
    }


def _write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def write_crop_mask(
    settings: Settings,
    table: pa.Table,
    *,
    source_path: Path,
    source_manifest_path: Path,
) -> CropMaskResult:
    root = dataset_path(settings)
    root.mkdir(parents=True, exist_ok=True)
    output = root / "part-000.parquet"
    temporary = output.with_suffix(".parquet.tmp")
    pq.write_table(table, temporary, compression="zstd")
    temporary.replace(output)

    fractions = table.column("soy_fraction").to_pylist()
    soybean_cells = sum(value is not None and value > 0 for value in fractions)
    source_manifest = json.loads(source_manifest_path.read_text())
    with rasterio.open(source_path) as source:
        if source.crs is None:
            raise ValueError("MapBiomas raster has no CRS")
        source_crs = source.crs.to_string()
        source_resolution = [abs(source.res[0]), abs(source.res[1])]
    common = {
        "dataset": "crop_mask",
        "source": "MapBiomas",
        "source_path": str(source_path),
        "source_extraction_timestamp": source_manifest.get("downloaded_at_utc"),
        "source_crs": source_crs,
        "source_resolution": source_resolution,
        "output_crs": settings.spatial.area_crs,
        "resolution_meters": settings.spatial.screening_grid_meters,
        "processing_version": settings.project.version,
        "processed_at_utc": datetime.now(UTC).isoformat(),
        "aggregation_method": AGGREGATION_METHOD,
        "aggregation_note": (
            "Fraction based on centers of valid source pixels inside the AOI; "
            "categorical values are not interpolated"
        ),
        "units": {"soy_fraction": "fraction (0-1)"},
    }
    schema_path = root / "_schema.json"
    quality_path = root / "_quality.json"
    metadata_path = root / "_metadata.json"
    _write_json(schema_path, _schema_document(table.schema))
    _write_json(
        quality_path,
        {
            **common,
            "row_count": table.num_rows,
            "duplicate_key_count": table.num_rows
            - len(
                set(
                    zip(
                        table.column("grid_id").to_pylist(),
                        table.column("year").to_pylist(),
                        strict=True,
                    )
                )
            ),
            "missing_soy_fraction_count": table.column("soy_fraction").null_count,
            "soybean_cell_count": soybean_cells,
            "minimum_soy_fraction": min(
                (value for value in fractions if value is not None), default=None
            ),
            "maximum_soy_fraction": max(
                (value for value in fractions if value is not None), default=None
            ),
        },
    )
    _write_json(metadata_path, common)
    LOGGER.info(
        "Silver crop mask written",
        extra={"dataset": "crop_mask", "row_count": table.num_rows, "path": str(root)},
    )
    return CropMaskResult(
        root,
        output,
        schema_path,
        quality_path,
        metadata_path,
        table.num_rows,
        soybean_cells,
    )


def transform(settings: Settings) -> CropMaskResult:
    grid_path = spatial_grid.dataset_path(settings) / "grid.parquet"
    boundary_path = ibge.artifact_path(settings)
    source_path = mapbiomas.artifact_path(settings)
    source_manifest_path = source_path.with_suffix(".manifest.json")
    required = (grid_path, boundary_path, source_path, source_manifest_path)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Required input artifacts not found: {', '.join(missing)}")
    table = aggregate_soy_fraction(
        settings,
        grid=pq.read_table(grid_path),
        boundary=boundary_path.read_bytes(),
        raster_path=source_path,
    )
    return write_crop_mask(
        settings,
        table,
        source_path=source_path,
        source_manifest_path=source_manifest_path,
    )
