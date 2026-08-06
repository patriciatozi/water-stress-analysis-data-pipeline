from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class IngestionState(StrEnum):
    DOWNLOADED = "downloaded"
    REUSED = "reused"
    PLANNED = "planned"


@dataclass(frozen=True)
class IngestionResult:
    source: str
    artifact_path: Path
    manifest_path: Path
    checksum: str | None
    size_bytes: int | None
    state: IngestionState
