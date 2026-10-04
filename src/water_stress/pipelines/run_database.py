from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from water_stress.config import load_settings
from water_stress.database.client import apply_migrations, connect
from water_stress.database.loader import load_dataset_files, register_bronze_manifests, source_paths
from water_stress.pipelines.run_quality import validate_quality_report
from water_stress.transformation.gold_water_balance import dataset_path

LOAD_ORDER = (
    "dim_spatial_grid",
    "crop_mask",
    "soil_features",
    "weather_daily",
    "satellite_observation",
    "water_stress_weekly",
    "soil_hydraulics",
    "water_stress_weekly_v2",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply PostgreSQL migrations and load derived datasets"
    )
    parser.add_argument("--config", type=Path, default=Path("configs/project.yml"))
    parser.add_argument("--migrate", action="store_true", help="Apply pending SQL migrations")
    parser.add_argument("--load", action="store_true", help="Load Parquet datasets into PostgreSQL")
    parser.add_argument(
        "--register-bronze",
        action="store_true",
        help="Register Bronze manifests without copying raw files",
    )
    parser.add_argument(
        "--dataset",
        action="append",
        choices=(*LOAD_ORDER, "all"),
        help="Dataset to load; repeat the option or use all",
    )
    parser.add_argument("--migration-dir", type=Path, default=Path("migrations"))
    parser.add_argument("--max-cells", type=int, help="Load the isolated water-balance pilot scope")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.migrate and not args.load and not args.register_bronze:
        raise SystemExit("Use --migrate, --load, --register-bronze or a combination")
    settings = load_settings(args.config)
    datasets = args.dataset or ["all"]
    selected = LOAD_ORDER if "all" in datasets else tuple(dict.fromkeys(datasets))
    if args.max_cells is not None and (
        args.max_cells < 1
        or any(dataset not in ("soil_hydraulics", "water_stress_weekly_v2") for dataset in selected)
    ):
        raise ValueError("--max-cells requires only water-balance datasets and a positive limit")
    # Preserve the existing 'all' command when no v2 publication has been generated yet.
    if "all" in datasets and not (dataset_path(settings) / "_metadata.json").is_file():
        selected = tuple(
            dataset
            for dataset in selected
            if dataset not in ("soil_hydraulics", "water_stress_weekly_v2")
        )
    if args.load and any(
        dataset in ("soil_hydraulics", "water_stress_weekly_v2") for dataset in selected
    ):
        validate_quality_report(dataset_path(settings, args.max_cells) / "_quality.json")
    with connect(settings.database) as connection:
        migrated = apply_migrations(connection, args.migration_dir) if args.migrate else []
        registered = (
            register_bronze_manifests(connection, settings.storage.root_path)
            if args.register_bronze
            else 0
        )
        loaded: dict[str, int] = {}
        if args.load:
            for dataset in selected:
                loaded[dataset] = load_dataset_files(
                    connection,
                    dataset,
                    source_paths(settings, dataset, args.max_cells),
                    processing_version=settings.project.version,
                )
    print(
        json.dumps(
            {"migrations": migrated, "bronze_manifests_registered": registered, "loaded": loaded},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
