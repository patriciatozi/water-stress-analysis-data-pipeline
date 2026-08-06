from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from water_stress.models import IngestionResult, IngestionState
from water_stress.storage import StorageClient


def utc_run_token(now: datetime | None = None) -> str:
    value = now or datetime.now(UTC)
    return value.strftime("%Y%m%dT%H%M%S%fZ")


def versioned_paths(artifact_path: Path, *, force: bool) -> tuple[Path, Path]:
    if force:
        token = utc_run_token()
        artifact_path = artifact_path.with_name(
            f"{artifact_path.stem}.{token}{artifact_path.suffix}"
        )
    manifest_path = artifact_path.with_name(f"{artifact_path.stem}.manifest.json")
    return artifact_path, manifest_path


def reusable_result(
    *,
    source: str,
    storage: StorageClient,
    artifact_path: Path,
    manifest_path: Path,
    request_fingerprint: str,
) -> IngestionResult | None:
    if not storage.exists(artifact_path) or not storage.exists(manifest_path):
        return None
    try:
        manifest = json.loads(storage.read_bytes(manifest_path))
        actual_checksum = storage.checksum(artifact_path)
    except (OSError, ValueError, TypeError):
        return None
    if (
        manifest.get("sha256") != actual_checksum
        or manifest.get("request_fingerprint") != request_fingerprint
    ):
        return None
    return IngestionResult(
        source=source,
        artifact_path=artifact_path,
        manifest_path=manifest_path,
        checksum=actual_checksum,
        size_bytes=artifact_path.stat().st_size,
        state=IngestionState.REUSED,
    )


def fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def persist_download(
    *,
    source: str,
    storage: StorageClient,
    artifact_path: Path,
    manifest_path: Path,
    content: bytes,
    manifest: dict[str, Any],
) -> IngestionResult:
    checksum = hashlib.sha256(content).hexdigest()
    completed_manifest = {
        **manifest,
        "downloaded_at_utc": datetime.now(UTC).isoformat(),
        "size_bytes": len(content),
        "sha256": checksum,
    }
    storage.write_bytes(artifact_path, content)
    storage.write_bytes(
        manifest_path,
        (
            json.dumps(completed_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode(),
    )
    return IngestionResult(
        source=source,
        artifact_path=artifact_path,
        manifest_path=manifest_path,
        checksum=checksum,
        size_bytes=len(content),
        state=IngestionState.DOWNLOADED,
    )
