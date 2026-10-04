from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from water_stress.config import load_settings
from water_stress.database.client import apply_migrations, connect
from water_stress.database.loader import load_dataset_files, register_bronze_manifests, source_paths

LOAD_ORDER = (
    "dim_spatial_grid",
    "crop_mask",
    "soil_features",
    "weather_daily",
    "satellite_observation",
    "water_stress_weekly",
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.migrate and not args.load and not args.register_bronze:
        raise SystemExit("Use --migrate, --load, --register-bronze or a combination")
    settings = load_settings(args.config)
    datasets = args.dataset or ["all"]
    selected = LOAD_ORDER if "all" in datasets else tuple(dict.fromkeys(datasets))
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
                    source_paths(settings, dataset),
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
