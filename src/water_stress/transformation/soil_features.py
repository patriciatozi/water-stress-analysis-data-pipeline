from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import rasterio  # type: ignore[import-untyped]
from pyproj import CRS, Transformer
from shapely import contains_xy, from_wkb
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from water_stress.config import Settings
from water_stress.ingestion import ibge
from water_stress.transformation import spatial_grid

LOGGER = logging.getLogger(__name__)
SPATIAL_AGGREGATION = "source_pixel_center_arithmetic_mean"
VERTICAL_AGGREGATION = "depth_thickness_weighted_mean"
DEPTH_PATTERN = re.compile(r"^(\d+)-(\d+)cm$")

PROPERTY_COLUMNS = {
    "clay": ("clay_pct", 0.1, "%", "g/kg"),
    "sand": ("sand_pct", 0.1, "%", "g/kg"),
    "soc": ("soc", 0.1, "g/kg", "dg/kg"),
    "bdod": ("bulk_density", 0.01, "g/cm^3", "cg/cm^3"),
}


@dataclass(frozen=True)
class SoilFeaturesResult:
    dataset_path: Path
    parquet_path: Path
    schema_path: Path
    quality_path: Path
    metadata_path: Path
    row_count: int
    complete_row_count: int


def _field(name: str, unit: str, description: str) -> pa.Field[Any]:
    return pa.field(
        name,
        pa.float64(),
        metadata={"unit": unit, "description": description},
    )


def silver_schema(settings: Settings) -> pa.Schema:
    fields: list[pa.Field[Any]] = [
        pa.field(
            "grid_id",
            pa.string(),
            nullable=False,
            metadata={"unit": "identifier", "description": "Spatial grid foreign key"},
        ),
        _field("clay_pct", "%", "Thickness-weighted clay content from 0 to 30 cm"),
        _field("sand_pct", "%", "Thickness-weighted sand content from 0 to 30 cm"),
        _field("soc", "g/kg", "Thickness-weighted soil organic carbon from 0 to 30 cm"),
        _field(
            "bulk_density",
            "g/cm^3",
            "Thickness-weighted dry bulk density from 0 to 30 cm",
        ),
    ]
    return pa.schema(
        fields,
        metadata={
            "source": "ISRIC SoilGrids",
            "layer": "silver",
            "dataset": "soil_features",
            "crs": settings.spatial.area_crs,
            "resolution_meters": str(settings.spatial.screening_grid_meters),
            "processing_version": settings.project.version,
            "spatial_aggregation": SPATIAL_AGGREGATION,
            "vertical_aggregation": VERTICAL_AGGREGATION,
        },
    )


def _depth_thickness(depth: str) -> int:
    match = DEPTH_PATTERN.fullmatch(depth)
    if match is None:
        raise ValueError(f"Invalid SoilGrids depth interval: {depth}")
    top, bottom = (int(value) for value in match.groups())
    if bottom <= top:
        raise ValueError(f"Invalid SoilGrids depth interval: {depth}")
    return bottom - top


def _projected_boundary(settings: Settings, boundary: bytes) -> BaseGeometry:
    geometry = ibge.geometry_from_geojson(boundary)
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError("Study area geometry must be valid and non-empty")
    transformer = Transformer.from_crs(
        settings.spatial.query_crs, settings.spatial.area_crs, always_xy=True
    )
    return shapely_transform(transformer.transform, geometry)


def _grid_layout(table: pa.Table, cell_size: int) -> tuple[float, float, dict[int, int], list[str]]:
    required = {"grid_id", "geometry"}
    if not required.issubset(table.column_names):
        raise ValueError("Spatial grid must contain grid_id and geometry")
    raw_grid_ids = table.column("grid_id").to_pylist()
    if any(value is None or not isinstance(value, str) for value in raw_grid_ids):
        raise ValueError("Spatial grid contains an invalid grid_id")
    grid_ids = [str(value) for value in raw_grid_ids]
    if len(grid_ids) != len(set(grid_ids)):
        raise ValueError("Spatial grid contains duplicate grid_id values")
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
    return origin_x, origin_y, positions, grid_ids


def source_paths(settings: Settings) -> dict[str, dict[tuple[str, str], Path]]:
    grouped: dict[str, dict[tuple[str, str], Path]] = {}
    for property_name in settings.soilgrids.properties:
        if property_name not in PROPERTY_COLUMNS:
            raise ValueError(f"Unsupported SoilGrids property: {property_name}")
        for depth in settings.soilgrids.depths:
            pattern_root = (
                settings.storage.root_path
                / "soilgrids"
                / settings.study.partition_key
                / f"property={property_name}"
                / f"depth={depth}"
            )
            chunk_directories = sorted(pattern_root.glob("chunk_id=*"))
            if not chunk_directories:
                raise FileNotFoundError(
                    f"No SoilGrids chunks found for property={property_name}, depth={depth}"
                )
            filename = f"{property_name}_{depth}_{settings.soilgrids.quantile}.tif"
            for directory in chunk_directories:
                path = directory / filename
                if not path.is_file():
                    raise FileNotFoundError(f"Canonical SoilGrids artifact not found at {path}")
                chunk_id = path.parent.name.removeprefix("chunk_id=")
                grouped.setdefault(chunk_id, {})[(property_name, depth)] = path
    expected = len(settings.soilgrids.properties) * len(settings.soilgrids.depths)
    incomplete = {chunk: len(paths) for chunk, paths in grouped.items() if len(paths) != expected}
    if incomplete:
        raise ValueError(f"Incomplete SoilGrids chunk groups: {incomplete}")
    return grouped


def _pixel_targets(
    settings: Settings,
    *,
    raster_path: Path,
    area_geometry: BaseGeometry,
    origin_x: float,
    origin_y: float,
    positions: dict[int, int],
) -> tuple[np.ndarray[Any, np.dtype[np.bool_]], np.ndarray[Any, np.dtype[np.int64]]]:
    with rasterio.open(raster_path) as source:
        rows, columns = np.indices(source.shape, dtype=np.float64)
        rows += 0.5
        columns += 0.5
        affine = source.transform
        source_x = affine.a * columns + affine.b * rows + affine.c
        source_y = affine.d * columns + affine.e * rows + affine.f
    transformer = Transformer.from_crs(
        CRS.from_user_input(settings.soilgrids.subset_crs),
        settings.spatial.area_crs,
        always_xy=True,
    )
    projected_x, projected_y = transformer.transform(source_x, source_y)
    included = contains_xy(area_geometry, projected_x, projected_y)
    cell_size = settings.spatial.screening_grid_meters
    cell_columns = np.floor((projected_x[included] - origin_x) / cell_size).astype(np.int64)
    cell_rows = np.floor((projected_y[included] - origin_y) / cell_size).astype(np.int64)
    keys = (cell_rows << 32) | cell_columns
    unique_keys, inverse = np.unique(keys, return_inverse=True)
    lookup = np.array([positions.get(int(key), -1) for key in unique_keys], dtype=np.int64)
    targets = lookup[inverse]
    mapped = targets >= 0
    selected = np.flatnonzero(included)
    included.flat[selected[~mapped]] = False
    return included, targets[mapped]


def _validate_aligned(reference_path: Path, candidate_path: Path, expected_crs: str) -> None:
    with rasterio.open(reference_path) as reference, rasterio.open(candidate_path) as candidate:
        if reference.shape != candidate.shape or reference.transform != candidate.transform:
            raise ValueError(
                f"SoilGrids chunks are not aligned: {reference_path} and {candidate_path}"
            )
        if not np.allclose(candidate.res, (250.0, 250.0), rtol=0.01, atol=0):
            raise ValueError(
                f"Unexpected SoilGrids resolution in {candidate_path}: {candidate.res}"
            )
        if candidate.crs is not None and candidate.crs != CRS.from_user_input(expected_crs):
            raise ValueError(f"Unexpected SoilGrids CRS in {candidate_path}: {candidate.crs}")


def aggregate_soil_features(
    settings: Settings,
    *,
    grid: pa.Table,
    boundary: bytes,
    grouped_paths: dict[str, dict[tuple[str, str], Path]],
) -> pa.Table:
    origin_x, origin_y, positions, grid_ids = _grid_layout(
        grid, settings.spatial.screening_grid_meters
    )
    area_geometry = _projected_boundary(settings, boundary)
    sums = {
        key: np.zeros(grid.num_rows, dtype=np.float64)
        for key in (
            (property_name, depth)
            for property_name in settings.soilgrids.properties
            for depth in settings.soilgrids.depths
        )
    }
    counts = {key: np.zeros(grid.num_rows, dtype=np.int64) for key in sums}

    for chunk_id, paths in sorted(grouped_paths.items()):
        reference_path = next(iter(paths.values()))
        included, targets = _pixel_targets(
            settings,
            raster_path=reference_path,
            area_geometry=area_geometry,
            origin_x=origin_x,
            origin_y=origin_y,
            positions=positions,
        )
        for key, path in paths.items():
            _validate_aligned(reference_path, path, settings.soilgrids.subset_crs)
            with rasterio.open(path) as source:
                values = source.read(1, masked=True)
            selected_values = np.asarray(values.data[included], dtype=np.float64)
            valid = ~np.ma.getmaskarray(values)[included]
            valid &= selected_values > 0
            valid_targets = targets[valid]
            sums[key] += np.bincount(
                valid_targets, weights=selected_values[valid], minlength=grid.num_rows
            )
            counts[key] += np.bincount(valid_targets, minlength=grid.num_rows)
        LOGGER.info(
            "SoilGrids chunk aggregated",
            extra={"dataset": "soil_features", "chunk_id": chunk_id},
        )

    columns: dict[str, list[Any]] = {"grid_id": grid_ids}
    thicknesses = {depth: _depth_thickness(depth) for depth in settings.soilgrids.depths}
    for property_name in settings.soilgrids.properties:
        column_name, conversion, _, _ = PROPERTY_COLUMNS[property_name]
        depth_means: list[np.ndarray[Any, np.dtype[np.float64]]] = []
        available: list[np.ndarray[Any, np.dtype[np.bool_]]] = []
        for depth in settings.soilgrids.depths:
            key = property_name, depth
            has_values = counts[key] > 0
            mean = np.zeros(grid.num_rows, dtype=np.float64)
            np.divide(sums[key], counts[key], out=mean, where=has_values)
            depth_means.append(mean)
            available.append(has_values)
        complete = np.logical_and.reduce(available)
        total_thickness = sum(thicknesses.values())
        weighted = np.zeros(grid.num_rows, dtype=np.float64)
        for mean, depth in zip(depth_means, settings.soilgrids.depths, strict=True):
            weighted += mean * thicknesses[depth]
        weighted /= total_thickness
        columns[column_name] = [
            float(value * conversion) if is_complete else None
            for value, is_complete in zip(weighted, complete, strict=True)
        ]
    return pa.table(columns, schema=silver_schema(settings))


def dataset_path(settings: Settings) -> Path:
    return (
        settings.storage.silver_root_path
        / "soil_features"
        / settings.study.partition_key
        / f"resolution_meters={settings.spatial.screening_grid_meters}"
    )


def _write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _schema_document(schema: pa.Schema) -> dict[str, Any]:
    return {
        "dataset": "soil_features",
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


def write_soil_features(
    settings: Settings,
    table: pa.Table,
    *,
    grouped_paths: dict[str, dict[tuple[str, str], Path]],
) -> SoilFeaturesResult:
    root = dataset_path(settings)
    root.mkdir(parents=True, exist_ok=True)
    output = root / "part-000.parquet"
    temporary = output.with_suffix(".parquet.tmp")
    pq.write_table(table, temporary, compression="zstd")
    temporary.replace(output)

    manifests = [
        json.loads(path.with_suffix(".manifest.json").read_text())
        for paths in grouped_paths.values()
        for path in paths.values()
    ]
    timestamps = sorted(
        value
        for manifest in manifests
        if isinstance(value := manifest.get("downloaded_at_utc"), str)
    )
    resolutions: list[float] = []
    for paths in grouped_paths.values():
        reference_path = next(iter(paths.values()))
        with rasterio.open(reference_path) as source:
            resolutions.extend((abs(source.res[0]), abs(source.res[1])))
    missing = {name: table.column(name).null_count for name in table.column_names}
    feature_columns = [column for column in table.column_names if column != "grid_id"]
    ranges = {
        column: {
            "minimum": min(
                (value for value in table.column(column).to_pylist() if value is not None),
                default=None,
            ),
            "maximum": max(
                (value for value in table.column(column).to_pylist() if value is not None),
                default=None,
            ),
        }
        for column in feature_columns
    }
    complete_rows = sum(
        all(table.column(column)[index].as_py() is not None for column in feature_columns)
        for index in range(table.num_rows)
    )
    common = {
        "dataset": "soil_features",
        "source": "ISRIC SoilGrids WCS",
        "source_extraction_timestamp_min": timestamps[0] if timestamps else None,
        "source_extraction_timestamp_max": timestamps[-1] if timestamps else None,
        "source_crs": settings.soilgrids.subset_crs,
        "source_resolution_meters": 250,
        "source_resolution_meters_observed_min": min(resolutions),
        "source_resolution_meters_observed_max": max(resolutions),
        "output_crs": settings.spatial.area_crs,
        "resolution_meters": settings.spatial.screening_grid_meters,
        "depths": settings.soilgrids.depths,
        "quantile": settings.soilgrids.quantile,
        "processing_version": settings.project.version,
        "processed_at_utc": datetime.now(UTC).isoformat(),
        "spatial_aggregation": SPATIAL_AGGREGATION,
        "vertical_aggregation": VERTICAL_AGGREGATION,
        "resampling_method": "none; source pixel centers are assigned to target cells",
        "nodata_rule": (
            "masked and raw values <= 0 are missing; WCS GeoTIFFs do not declare nodata and "
            "zero is outside the valid physical domain of the selected properties"
        ),
        "units": {column_name: unit for column_name, _, unit, _ in PROPERTY_COLUMNS.values()},
        "source_units": {
            property_name: source_unit
            for property_name, (_, _, _, source_unit) in PROPERTY_COLUMNS.items()
        },
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
            "complete_row_count": complete_rows,
            "duplicate_grid_id_count": table.num_rows
            - len(set(table.column("grid_id").to_pylist())),
            "missing_by_column": missing,
            "range_by_column": ranges,
        },
    )
    _write_json(metadata_path, common)
    LOGGER.info(
        "Silver soil features written",
        extra={"dataset": "soil_features", "row_count": table.num_rows, "path": str(root)},
    )
    return SoilFeaturesResult(
        root,
        output,
        schema_path,
        quality_path,
        metadata_path,
        table.num_rows,
        complete_rows,
    )


def transform(settings: Settings) -> SoilFeaturesResult:
    grid_path = spatial_grid.dataset_path(settings) / "grid.parquet"
    boundary_path = ibge.artifact_path(settings)
    missing = [str(path) for path in (grid_path, boundary_path) if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Required input artifacts not found: {', '.join(missing)}")
    grouped = source_paths(settings)
    table = aggregate_soil_features(
        settings,
        grid=pq.read_table(grid_path),
        boundary=boundary_path.read_bytes(),
        grouped_paths=grouped,
    )
    return write_soil_features(settings, table, grouped_paths=grouped)
