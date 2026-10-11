from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from water_stress.infrastructure import run_command
from water_stress.storage import LocalStorageClient

LOGGER = logging.getLogger(__name__)
LAYERS = ("bronze", "silver", "gold")
SYNC_FILTERS = (
    ["--exclude", "*"]
    + [value for layer in LAYERS for value in ("--include", f"{layer}/*")]
    + ["--exclude", ".*", "--exclude", "*/.*", "--exclude", "*.tmp"]
)


class CloudLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    prefix: str = "water-stress"
    region: str = Field(default="us-east-1", pattern=r"^[a-z]{2}(?:-[a-z]+)+-\d+$")

    @field_validator("bucket")
    @classmethod
    def valid_bucket(cls, value: str) -> str:
        if ".." in value or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value):
            raise ValueError("Use an S3 bucket name, not a URL or IP address")
        return value

    @field_validator("prefix")
    @classmethod
    def valid_prefix(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", value):
            raise ValueError("S3 prefix must contain simple relative path segments")
        return value

    def snapshot_uri(self, snapshot_id: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{64}", snapshot_id):
            raise ValueError("Snapshot ID must be a SHA-256 digest (64 lowercase hex characters)")
        return f"s3://{self.bucket}/{self.prefix}/snapshots/{snapshot_id}"


class SnapshotFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        parts = PurePosixPath(value).parts
        if (
            not parts
            or parts[0] not in LAYERS
            or len(parts) < 2
            or PurePosixPath(value).as_posix() != value
            or any(part.startswith(".") for part in parts)
            or any(character in value for character in "\\*?[]")
            or value.endswith(".tmp")
        ):
            raise ValueError("Snapshot files must be safe relative Bronze/Silver/Gold paths")
        return value


class DataSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["data-snapshot-v1"] = "data-snapshot-v1"
    created_at_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
    files: tuple[SnapshotFile, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_inventory(self) -> DataSnapshot:
        paths = [entry.path for entry in self.files]
        if paths != sorted(paths) or len({path.casefold() for path in paths}) != len(paths):
            raise ValueError("Snapshot paths must be sorted and unique, including case")
        if {PurePosixPath(path).parts[0] for path in paths} != set(LAYERS):
            raise ValueError("Snapshot requires nonempty Bronze, Silver and Gold layers")
        return self

    @property
    def snapshot_id(self) -> str:
        # Timestamp records publication lineage without changing the identity of
        # identical bytes and paths on a later attempt.
        content = self.model_dump(mode="json", exclude={"created_at_utc"})
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def inventory(data_dir: Path) -> tuple[SnapshotFile, ...]:
    """Hash completed artifacts incrementally; exclude temporary/hidden files."""
    if data_dir.is_symlink():
        raise ValueError(f"Symbolic links are not supported: {data_dir}")
    entries = []
    storage = LocalStorageClient()
    for layer in LAYERS:
        layer_dir = data_dir / layer
        if layer_dir.is_symlink():
            raise ValueError(f"Symbolic links are not supported: {layer_dir}")
        for path in sorted(layer_dir.rglob("*")):
            relative = path.relative_to(data_dir).as_posix()
            if any(part.startswith(".") for part in PurePosixPath(relative).parts):
                continue
            if path.is_symlink():
                raise ValueError(f"Symbolic links are not supported: {path}")
            if not path.is_file() or path.name.endswith(".tmp"):
                continue
            entries.append(
                SnapshotFile(
                    path=relative, size_bytes=path.stat().st_size, sha256=storage.checksum(path)
                )
            )
    return tuple(sorted(entries, key=lambda entry: entry.path))


def aws_command(location: CloudLocation, arguments: list[str]) -> str:
    environment = os.environ.copy()
    environment.update(AWS_MAX_ATTEMPTS="3", AWS_RETRY_MODE="standard", AWS_PAGER="")
    return run_command(
        [
            "aws",
            "--region",
            location.region,
            "--cli-connect-timeout",
            "30",
            "--cli-read-timeout",
            "120",
            *arguments,
        ],
        environment=environment,
    )


def publish_data(data_dir: Path, location: CloudLocation) -> DataSnapshot:
    snapshot = DataSnapshot(files=inventory(data_dir))
    uri = location.snapshot_uri(snapshot.snapshot_id)
    LOGGER.info(
        "Publishing existing data",
        extra={
            "source": "s3",
            "operation": "publish",
            "partition": snapshot.snapshot_id,
            "outcome": "started",
        },
    )
    key = f"{location.prefix}/snapshots/{snapshot.snapshot_id}/manifest.json"
    keys = json.loads(
        aws_command(
            location,
            [
                "s3api",
                "list-objects-v2",
                "--bucket",
                location.bucket,
                "--prefix",
                key,
                "--query",
                "Contents[].Key",
                "--output",
                "json",
            ],
        )
    )
    if keys and key in keys:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "manifest.json"
            aws_command(
                location,
                [
                    "s3",
                    "cp",
                    f"{uri}/manifest.json",
                    str(manifest),
                    "--only-show-errors",
                ],
            )
            published = DataSnapshot.model_validate_json(manifest.read_bytes())
        if published.snapshot_id != snapshot.snapshot_id:
            raise ValueError("Existing remote manifest is inconsistent; use a different S3 prefix")
        LOGGER.info(
            "Published snapshot reused",
            extra={
                "source": "s3",
                "operation": "publish",
                "partition": snapshot.snapshot_id,
                "outcome": "reused",
            },
        )
        return published
    # Recopy an incomplete snapshot instead of relying on timestamps: an earlier
    # interrupted attempt may contain bytes from a concurrently changed source.
    aws_command(
        location,
        [
            "s3",
            "cp",
            str(data_dir),
            f"{uri}/data/",
            "--recursive",
            "--no-follow-symlinks",
            "--only-show-errors",
            "--sse",
            "AES256",
            *SYNC_FILTERS,
        ],
    )
    if inventory(data_dir) != snapshot.files:
        raise ValueError("Data changed during upload; stop writers and publish again")
    # A missing manifest means the snapshot is incomplete. Consumers never infer
    # completeness from a successful upload of just one layer.
    with tempfile.TemporaryDirectory() as temporary:
        manifest = Path(temporary) / "manifest.json"
        manifest.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        aws_command(
            location,
            [
                "s3",
                "cp",
                str(manifest),
                f"{uri}/manifest.json",
                "--only-show-errors",
                "--sse",
                "AES256",
            ],
        )
    LOGGER.info(
        "Data snapshot published",
        extra={
            "source": "s3",
            "operation": "publish",
            "partition": snapshot.snapshot_id,
            "outcome": "success",
            "row_count": len(snapshot.files),
        },
    )
    return snapshot


def fetch_data(data_dir: Path, location: CloudLocation, snapshot_id: str) -> DataSnapshot:
    uri = location.snapshot_uri(snapshot_id)
    with tempfile.TemporaryDirectory() as temporary:
        manifest = Path(temporary) / "manifest.json"
        aws_command(
            location,
            [
                "s3",
                "cp",
                f"{uri}/manifest.json",
                str(manifest),
                "--only-show-errors",
            ],
        )
        snapshot = DataSnapshot.model_validate_json(manifest.read_bytes())
    if snapshot.snapshot_id != snapshot_id:
        raise ValueError("Remote manifest does not match the requested snapshot ID")
    existing = inventory(data_dir)
    expected = {entry.path: entry for entry in snapshot.files}
    if any(expected.get(entry.path) != entry for entry in existing):
        raise ValueError("Destination has conflicting data; use a clean checkout/data directory")
    if existing == snapshot.files:
        LocalStorageClient().write_bytes(
            data_dir / ".snapshot.json", snapshot.model_dump_json(indent=2).encode()
        )
        LOGGER.info(
            "Existing data reused",
            extra={
                "source": "s3",
                "operation": "fetch",
                "partition": snapshot_id,
                "outcome": "reused",
            },
        )
        return snapshot
    staging = data_dir.parent / ".deployment" / snapshot_id
    if staging.is_symlink() or staging.parent.is_symlink():
        raise ValueError("Download staging directory must not be a symbolic link")
    staging.mkdir(parents=True, exist_ok=True)
    for entry in inventory(staging):
        if expected.get(entry.path) != entry:
            (staging / entry.path).unlink()
    aws_command(
        location,
        [
            "s3",
            "sync",
            f"{uri}/data/",
            str(staging),
            "--only-show-errors",
            "--no-follow-symlinks",
            *SYNC_FILTERS,
        ],
    )
    if inventory(staging) != snapshot.files:
        raise ValueError("Downloaded files failed checksum/inventory validation; retry fetch-data")
    # Recheck before installing: a running local writer must never be overwritten.
    if inventory(data_dir) != existing:
        raise ValueError("Destination changed during download; stop writers and retry")
    existing_paths = {entry.path for entry in existing}
    for entry in snapshot.files:
        if entry.path not in existing_paths:
            destination = data_dir / entry.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            (staging / entry.path).replace(destination)
    shutil.rmtree(staging)
    LocalStorageClient().write_bytes(
        data_dir / ".snapshot.json", snapshot.model_dump_json(indent=2).encode()
    )
    LOGGER.info(
        "Data snapshot restored",
        extra={
            "source": "s3",
            "operation": "fetch",
            "partition": snapshot_id,
            "outcome": "success",
            "row_count": len(snapshot.files),
        },
    )
    return snapshot
