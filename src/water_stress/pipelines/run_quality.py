from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from water_stress.config import load_settings
from water_stress.logging import configure_logging
from water_stress.transformation.gold_weekly import dataset_path

LOGGER = logging.getLogger(__name__)


class QualityReport(BaseModel):
    quality_status: Literal["passed", "warning", "failed"]
    row_count: int = Field(ge=0)
    quality_issues: list[str] = Field(default_factory=list)


def validate_quality_report(path: Path) -> QualityReport:
    """Allow documented missing attributes, but block failed or empty Gold publication."""
    report = QualityReport.model_validate_json(path.read_bytes())
    if report.quality_status == "failed" or report.row_count == 0:
        raise ValueError(
            f"Gold publication blocked: status={report.quality_status}, "
            f"rows={report.row_count}, issues={report.quality_issues}"
        )
    LOGGER.info(
        "Gold publication quality checked",
        extra={
            "source": "gold",
            "operation": "quality_gate",
            "partition": "study",
            "outcome": report.quality_status,
            "row_count": report.row_count,
        },
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Gold before dashboard publication")
    parser.add_argument("--config", type=Path, default=Path("configs/project.yml"))
    args = parser.parse_args()
    configure_logging()
    report = validate_quality_report(dataset_path(load_settings(args.config)) / "_quality.json")
    print(json.dumps(report.model_dump()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
