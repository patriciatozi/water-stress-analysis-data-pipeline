from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from water_stress.config import load_settings
from water_stress.dashboard.s3_cache import DashboardStorageSettings, publish_dashboard
from water_stress.logging import configure_logging

LOGGER = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish validated Gold v1 for the S3 dashboard")
    parser.add_argument("--config", type=Path, default=Path("configs/project.yml"))
    args = parser.parse_args()
    configure_logging()
    try:
        config = DashboardStorageSettings()
        if config.source != "s3" or config.location is None:
            raise ValueError("Configure the S3 dashboard source and location before publishing")
        snapshot = publish_dashboard(load_settings(args.config), config.location)
    except (ValueError, OSError, RuntimeError) as error:
        LOGGER.error(
            "Dashboard publication failed; check source files, quality, S3 configuration and role",
            extra={
                "source": "s3",
                "operation": "dashboard_publish",
                "outcome": "failed",
                "error_type": type(error).__name__,
            },
        )
        return 1
    print(json.dumps({"snapshot_id": snapshot.snapshot_id, "file_count": len(snapshot.files)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
