from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from pathlib import Path

from water_stress.cloud_transfer import CloudLocation, DataSnapshot, fetch_data, publish_data
from water_stress.infrastructure import run_services
from water_stress.logging import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run locally or on an AWS EC2 host using existing data"
    )
    parser.add_argument("--infra", choices=("local", "aws"), required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--bucket", help="Existing private S3 bucket for data transfer")
    parser.add_argument("--prefix", default="water-stress")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--snapshot", help="Snapshot ID printed by publish-data")
    parser.add_argument(
        "action", choices=("up", "stop", "status", "load", "publish-data", "fetch-data")
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging()
    try:
        if not Path("compose.airflow.yml").is_file():
            raise ValueError("Run this command from the repository root")
        if args.action in ("publish-data", "fetch-data"):
            if args.infra != "aws" or not args.bucket:
                raise ValueError("Data transfer requires --infra aws and --bucket")
            location = CloudLocation(bucket=args.bucket, prefix=args.prefix, region=args.region)
            if args.action == "publish-data":
                snapshot = publish_data(Path("data"), location)
            else:
                if not args.snapshot:
                    raise ValueError("fetch-data requires --snapshot from publish-data")
                snapshot = fetch_data(Path("data"), location, args.snapshot)
            print(
                json.dumps(
                    {
                        "snapshot_id": snapshot.snapshot_id,
                        "uri": location.snapshot_uri(snapshot.snapshot_id),
                        "file_count": len(snapshot.files),
                        "size_bytes": sum(entry.size_bytes for entry in snapshot.files),
                    }
                )
            )
        else:
            if args.infra == "aws" and args.action in ("up", "load"):
                marker = Path("data/.snapshot.json")
                if not marker.is_file():
                    raise ValueError("Restore existing data with fetch-data before AWS up/load")
                DataSnapshot.model_validate_json(marker.read_bytes())
            env_file = args.env_file or Path(".env.aws" if args.infra == "aws" else ".env.airflow")
            logging.getLogger(__name__).info(
                "Infrastructure operation started",
                extra={
                    "source": args.infra,
                    "operation": args.action,
                    "outcome": "started",
                },
            )
            print(run_services(args.infra, args.action, env_file), end="")
            logging.getLogger(__name__).info(
                "Infrastructure operation completed",
                extra={
                    "source": args.infra,
                    "operation": args.action,
                    "outcome": "success",
                },
            )
    except (ValueError, RuntimeError, OSError) as error:
        logging.getLogger(__name__).error(
            str(error),
            extra={
                "source": args.infra,
                "operation": args.action,
                "outcome": "failed",
            },
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
