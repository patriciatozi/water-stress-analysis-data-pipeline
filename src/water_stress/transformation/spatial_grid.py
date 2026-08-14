from __future__ import annotations

import json
from dataclasses import dataclass
from math import ceil, floor
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import Transformer
from shapely.geometry import box
from shapely.ops import transform

from water_stress.config import Settings
from water_stress.ingestion import ibge


@dataclass(frozen=True)
class SpatialGridResult:
    dataset_path: Path
    geoparquet_path: Path
    metadata_path: Path
    row_count: int


def build_grid(settings: Settings, boundary: bytes) -> pa.Table:
    source_geometry = ibge.geometry_from_geojson(boundary)
    if source_geometry.is_empty or not source_geometry.is_valid:
        raise ValueError("Study area geometry must be valid and non-empty")

    to_area = Transformer.from_crs(
        settings.spatial.query_crs, settings.spatial.area_crs, always_xy=True
    )
    to_query = Transformer.from_crs(
        settings.spatial.area_crs, settings.spatial.query_crs, always_xy=True
    )
    area_geometry = transform(to_area.transform, source_geometry)
    cell_size = settings.spatial.screening_grid_meters
    min_x, min_y, max_x, max_y = area_geometry.bounds
    origin_x = floor(min_x / cell_size) * cell_size
    origin_y = floor(min_y / cell_size) * cell_size
    columns = ceil((max_x - origin_x) / cell_size)
    rows = ceil((max_y - origin_y) / cell_size)

    records: dict[str, list[object]] = {
        "grid_id": [],
        "geometry": [],
        "centroid_latitude": [],
        "centroid_longitude": [],
        "area_km2": [],
    }
    for row in range(rows):
        for column in range(columns):
            cell = box(
                origin_x + column * cell_size,
                origin_y + row * cell_size,
                origin_x + (column + 1) * cell_size,
                origin_y + (row + 1) * cell_size,
            )
            if not cell.intersects(area_geometry):
                continue
            centroid = cell.centroid
            longitude, latitude = to_query.transform(centroid.x, centroid.y)
            records["grid_id"].append(
                f"{settings.study.area_code}_{cell_size}m_r{row:05d}_c{column:05d}"
            )
            records["geometry"].append(cell.wkb)
            records["centroid_latitude"].append(latitude)
            records["centroid_longitude"].append(longitude)
            records["area_km2"].append(cell.area / 1_000_000)

    geo_metadata = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "geometry_types": ["Polygon"],
                "crs": settings.spatial.area_crs,
            }
        },
    }
    fields: list[pa.Field[Any]] = [
        pa.field("grid_id", pa.string(), nullable=False),
        pa.field("geometry", pa.binary(), nullable=False),
        pa.field("centroid_latitude", pa.float64(), nullable=False),
        pa.field("centroid_longitude", pa.float64(), nullable=False),
        pa.field("area_km2", pa.float64(), nullable=False),
    ]
    schema = pa.schema(
        fields,
        metadata={
            "geo": json.dumps(geo_metadata, separators=(",", ":")),
            "layer": "silver",
            "dataset": "dim_spatial_grid",
        },
    )
    return pa.table(records, schema=schema)


def dataset_path(settings: Settings) -> Path:
    return (
        settings.storage.silver_root_path
        / "dim_spatial_grid"
        / settings.study.partition_key
        / f"resolution_meters={settings.spatial.screening_grid_meters}"
    )


def write_grid(settings: Settings, table: pa.Table) -> SpatialGridResult:
    root = dataset_path(settings)
    root.mkdir(parents=True, exist_ok=True)
    output = root / "grid.parquet"
    temporary = output.with_suffix(".parquet.tmp")
    pq.write_table(table, temporary, compression="zstd")
    temporary.replace(output)
    metadata_path = root / "_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "dataset": "dim_spatial_grid",
                "area_type": settings.study.area_type,
                "area_code": settings.study.area_code,
                "area_name": settings.study.area_name,
                "row_count": table.num_rows,
                "query_crs": settings.spatial.query_crs,
                "area_crs": settings.spatial.area_crs,
                "resolution_meters": settings.spatial.screening_grid_meters,
                "detail_resolution_meters": settings.spatial.detail_grid_meters,
                "processing_version": settings.project.version,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    return SpatialGridResult(root, output, metadata_path, table.num_rows)


def transform_boundary(settings: Settings) -> SpatialGridResult:
    source_path = ibge.artifact_path(settings)
    if not source_path.is_file():
        raise FileNotFoundError(f"IBGE Bronze boundary not found at {source_path}")
    return write_grid(settings, build_grid(settings, source_path.read_bytes()))
