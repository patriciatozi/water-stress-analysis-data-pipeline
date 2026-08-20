from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from water_stress.config import load_settings
from water_stress.transformation import (
    crop_mask,
    nasa_power,
    soil_features,
    spatial_grid,
    weather_daily,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transform Bronze data into Silver datasets")
    parser.add_argument("--config", type=Path, default=Path("configs/project.yml"))
    parser.add_argument(
        "--source",
        choices=(
            "nasa-power",
            "spatial-grid",
            "crop-mask",
            "soil-features",
            "weather-daily",
        ),
        default="nasa-power",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
    if args.source == "weather-daily":
        weather_result = weather_daily.transform(settings)
        print(
            json.dumps(
                {
                    "source": args.source,
                    "dataset_path": str(weather_result.dataset_path),
                    "parquet_paths": [str(path) for path in weather_result.parquet_paths],
                    "schema_path": str(weather_result.schema_path),
                    "quality_path": str(weather_result.quality_path),
                    "metadata_path": str(weather_result.metadata_path),
                    "row_count": weather_result.row_count,
                    "weather_cell_count": weather_result.weather_cell_count,
                }
            )
        )
        return 0
    if args.source == "soil-features":
        soil_result = soil_features.transform(settings)
        print(
            json.dumps(
                {
                    "source": args.source,
                    "dataset_path": str(soil_result.dataset_path),
                    "parquet_path": str(soil_result.parquet_path),
                    "schema_path": str(soil_result.schema_path),
                    "quality_path": str(soil_result.quality_path),
                    "metadata_path": str(soil_result.metadata_path),
                    "row_count": soil_result.row_count,
                    "complete_row_count": soil_result.complete_row_count,
                }
            )
        )
        return 0
    if args.source == "crop-mask":
        crop_result = crop_mask.transform(settings)
        print(
            json.dumps(
                {
                    "source": args.source,
                    "dataset_path": str(crop_result.dataset_path),
                    "parquet_path": str(crop_result.parquet_path),
                    "schema_path": str(crop_result.schema_path),
                    "quality_path": str(crop_result.quality_path),
                    "metadata_path": str(crop_result.metadata_path),
                    "row_count": crop_result.row_count,
                    "soybean_cell_count": crop_result.soybean_cell_count,
                }
            )
        )
        return 0
    if args.source == "spatial-grid":
        grid_result = spatial_grid.transform_boundary(settings)
        print(
            json.dumps(
                {
                    "source": args.source,
                    "dataset_path": str(grid_result.dataset_path),
                    "geoparquet_path": str(grid_result.geoparquet_path),
                    "metadata_path": str(grid_result.metadata_path),
                    "row_count": grid_result.row_count,
                }
            )
        )
        return 0
    result = nasa_power.transform(settings)
    print(
        json.dumps(
            {
                "source": args.source,
                "dataset_path": str(result.dataset_path),
                "parquet_paths": [str(path) for path in result.parquet_paths],
                "schema_path": str(result.schema_path),
                "quality_path": str(result.quality_path),
                "row_count": result.row_count,
                "missing_by_column": result.missing_by_column,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
