from __future__ import annotations

import fcntl
import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from water_stress.cloud_transfer import CloudLocation, SnapshotFile
from water_stress.config import Settings, StudySettings
from water_stress.pipelines.run_quality import validate_quality_report
from water_stress.storage import LocalStorageClient
from water_stress.transformation import gold_weekly, spatial_grid

LOGGER = logging.getLogger(__name__)


@contextmanager
def _s3_errors() -> Iterator[None]:
    from boto3.exceptions import S3UploadFailedError  # type: ignore[import-untyped]
    from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

    try:
        yield
    except ClientError as error:
        if error.response["Error"]["Code"] in {"NoSuchKey", "404"}:
            raise FileNotFoundError("S3 dashboard publication not found") from None
        raise RuntimeError(
            "S3 request failed; check the instance role and bucket permissions"
        ) from None
    except (BotoCoreError, S3UploadFailedError):
        raise RuntimeError(
            "S3 transfer failed; check credentials and connectivity, then retry"
        ) from None


class DashboardStorageSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WATER_STRESS_DASHBOARD__", env_nested_delimiter="__", extra="ignore"
    )
    source: Literal["postgres", "s3"] = "postgres"
    location: CloudLocation | None = None
    cache_dir: Path = Path(".dashboard-cache")

    @model_validator(mode="after")
    def require_s3_location(self) -> DashboardStorageSettings:
        if self.source == "s3" and self.location is None:
            raise ValueError("Configure WATER_STRESS_DASHBOARD__LOCATION__BUCKET for S3")
        return self


class DashboardSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["dashboard-snapshot-v1"] = "dashboard-snapshot-v1"
    created_at_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
    study: StudySettings
    files: tuple[SnapshotFile, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_files(self) -> DashboardSnapshot:
        paths = [entry.path for entry in self.files]
        if paths != sorted(paths) or len({path.casefold() for path in paths}) != len(paths):
            raise ValueError("Dashboard inventory must be sorted and unique")
        if any(not path.startswith(("silver/", "gold/")) for path in paths):
            raise ValueError("Dashboard snapshots contain only Silver and Gold")
        return self

    @property
    def snapshot_id(self) -> str:
        content = self.model_dump(mode="json", exclude={"created_at_utc"})
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


class ObjectStore(Protocol):
    def read(self, key: str) -> bytes: ...
    def write(self, key: str, content: bytes) -> None: ...
    def upload(self, key: str, path: Path) -> None: ...
    def download(self, key: str, path: Path) -> None: ...


class S3ObjectStore:
    """Use the AWS credential chain, bounded retries and streaming file transfers."""

    def __init__(self, location: CloudLocation) -> None:
        import boto3  # type: ignore[import-untyped]
        from botocore.config import Config  # type: ignore[import-untyped]

        self._bucket = location.bucket
        with _s3_errors():
            self._client = boto3.client(
                "s3",
                region_name=location.region,
                config=Config(
                    connect_timeout=10,
                    read_timeout=60,
                    retries={"mode": "standard", "total_max_attempts": 3},
                ),
            )

    def read(self, key: str) -> bytes:
        with (
            _s3_errors(),
            self._client.get_object(Bucket=self._bucket, Key=key)["Body"] as body,
        ):
            content: bytes = body.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("S3 dashboard manifest exceeds 1 MiB")
        return content

    def write(self, key: str, content: bytes) -> None:
        with _s3_errors():
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType="application/json",
                ServerSideEncryption="AES256",
            )

    def upload(self, key: str, path: Path) -> None:
        with _s3_errors():
            self._client.upload_file(
                str(path), self._bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"}
            )

    def download(self, key: str, path: Path) -> None:
        with _s3_errors():
            self._client.download_file(self._bucket, key, str(path))


def dashboard_files(settings: Settings) -> dict[str, Path]:
    """Publish the original v1 bytes and lineage, with keys independent of host paths."""
    grid = spatial_grid.dataset_path(settings)
    gold = gold_weekly.dataset_path(settings)
    selected = [
        (settings.storage.silver_root_path, grid / "grid.parquet", "silver"),
        (settings.storage.silver_root_path, grid / "_metadata.json", "silver"),
        *[
            (settings.storage.gold_root_path, gold / name, "gold")
            for name in ("_metadata.json", "_schema.json", "_quality.json")
        ],
        *[
            (
                settings.storage.gold_root_path,
                gold / f"week_start={start}" / "part-000.parquet",
                "gold",
            )
            for start, _ in gold_weekly._study_weeks(settings)
        ],
    ]
    return dict(
        sorted(
            (f"{layer}/{path.relative_to(root).as_posix()}", path) for root, path, layer in selected
        )
    )


def _inventory(files: dict[str, Path]) -> tuple[SnapshotFile, ...]:
    storage = LocalStorageClient()
    entries = []
    for key, path in files.items():
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ValueError("Dashboard files must not use symbolic links")
        if not path.is_file():
            raise FileNotFoundError(f"Dashboard dataset incomplete: {key}")
        entries.append(
            SnapshotFile(path=key, size_bytes=path.stat().st_size, sha256=storage.checksum(path))
        )
    return tuple(entries)


def publish_dashboard(
    settings: Settings, location: CloudLocation, store: ObjectStore | None = None
) -> DashboardSnapshot:
    validate_quality_report(gold_weekly.dataset_path(settings) / "_quality.json")
    files = dashboard_files(settings)
    snapshot = DashboardSnapshot(study=settings.study, files=_inventory(files))
    store = store if store is not None else S3ObjectStore(location)
    key = f"{location.prefix}/snapshots/{snapshot.snapshot_id}"
    try:
        published = DashboardSnapshot.model_validate_json(store.read(f"{key}/manifest.json"))
    except FileNotFoundError:
        published = None
    if published is not None and published.snapshot_id != snapshot.snapshot_id:
        raise ValueError("Existing S3 dashboard manifest is inconsistent")
    if published is None:
        for name, path in files.items():
            store.upload(f"{key}/data/{name}", path)
        if _inventory(files) != snapshot.files:
            raise ValueError(
                "Dashboard files changed during upload; retry after processing finishes"
            )
        store.write(f"{key}/manifest.json", snapshot.model_dump_json(indent=2).encode())
    else:
        snapshot = published
    # Readers only see a new revision after every object and its manifest are complete.
    store.write(f"{location.prefix}/current.json", snapshot.model_dump_json(indent=2).encode())
    LOGGER.info(
        "Dashboard published",
        extra={
            "source": "s3",
            "operation": "dashboard_publish",
            "partition": snapshot.snapshot_id,
            "outcome": "reused" if published else "succeeded",
            "row_count": len(snapshot.files),
        },
    )
    return snapshot


def _cached_settings(settings: Settings, root: Path) -> Settings:
    return settings.model_copy(
        update={
            "storage": settings.storage.model_copy(
                update={
                    "silver_root_path": root / "silver",
                    "gold_root_path": root / "gold",
                }
            )
        }
    )


def _matches(path: Path, entry: SnapshotFile) -> bool:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Dashboard cache must not use symbolic links")
    return (
        path.is_file()
        and path.stat().st_size == entry.size_bytes
        and LocalStorageClient().checksum(path) == entry.sha256
    )


def fetch_dashboard(
    settings: Settings, config: DashboardStorageSettings, store: ObjectStore | None = None
) -> Settings:
    if config.location is None:
        raise ValueError("Configure the S3 dashboard location")
    location = config.location
    store = store if store is not None else S3ObjectStore(location)
    snapshot = DashboardSnapshot.model_validate_json(store.read(f"{location.prefix}/current.json"))
    if snapshot.study != settings.study or set(dashboard_files(settings)) != {
        entry.path for entry in snapshot.files
    }:
        raise ValueError("S3 dashboard inventory does not match the configured study")
    root = config.cache_dir / snapshot.snapshot_id
    staging = config.cache_dir / f".{snapshot.snapshot_id}.partial"
    if config.cache_dir.is_symlink() or root.is_symlink() or staging.is_symlink():
        raise ValueError("Dashboard cache must not use symbolic links")
    config.cache_dir.mkdir(parents=True, exist_ok=True)
    with (config.cache_dir / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if root.exists():
            if not (root / "manifest.json").is_file() or any(
                not _matches(root / entry.path, entry) for entry in snapshot.files
            ):
                raise ValueError(
                    "Dashboard cache corrupted; remove only this cached snapshot and retry"
                )
            if (
                DashboardSnapshot.model_validate_json(
                    (root / "manifest.json").read_bytes()
                ).snapshot_id
                != snapshot.snapshot_id
            ):
                raise ValueError("Dashboard cache manifest is inconsistent")
            outcome = "reused"
        else:
            for entry in snapshot.files:
                path = staging / entry.path
                if not _matches(path, entry):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    store.download(
                        f"{location.prefix}/snapshots/{snapshot.snapshot_id}/data/{entry.path}",
                        path,
                    )
                if not _matches(path, entry):
                    raise ValueError("S3 dashboard checksum mismatch; retry the download")
            validate_quality_report(
                gold_weekly.dataset_path(_cached_settings(settings, staging)) / "_quality.json"
            )
            LocalStorageClient().write_bytes(
                staging / "manifest.json", snapshot.model_dump_json(indent=2).encode()
            )
            staging.rename(root)
            outcome = "succeeded"
    LOGGER.info(
        "S3 dashboard cache ready",
        extra={
            "source": "s3",
            "operation": "dashboard_fetch",
            "partition": snapshot.snapshot_id,
            "outcome": outcome,
            "row_count": len(snapshot.files),
        },
    )
    return _cached_settings(settings, root)
