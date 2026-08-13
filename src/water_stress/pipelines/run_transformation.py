from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from water_stress.config import load_settings
from water_stress.transformation import nasa_power


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transform Bronze data into Silver datasets")
    parser.add_argument("--config", type=Path, default=Path("configs/project.yml"))
    parser.add_argument("--source", choices=("nasa-power",), default="nasa-power")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
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
