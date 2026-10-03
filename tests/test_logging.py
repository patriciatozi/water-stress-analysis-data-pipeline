from __future__ import annotations

import json
import logging

from water_stress.logging import JsonFormatter


def test_lifecycle_logging_preserves_operation_partition_and_outcome() -> None:
    record = logging.makeLogRecord(
        {
            "msg": "Quality checked",
            "levelname": "INFO",
            "name": "quality",
            "source": "gold",
            "operation": "quality_gate",
            "partition": "study",
            "outcome": "warning",
            "row_count": 10,
        }
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["source"] == "gold"
    assert payload["operation"] == "quality_gate"
    assert payload["partition"] == "study"
    assert payload["outcome"] == "warning"
    assert payload["row_count"] == 10
