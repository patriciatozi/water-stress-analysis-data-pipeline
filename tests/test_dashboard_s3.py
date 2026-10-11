from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from test_dashboard import dashboard_data as dashboard_data

from water_stress.cloud_transfer import CloudLocation, SnapshotFile
from water_stress.config import Settings
from water_stress.dashboard.analysis import summarize
from water_stress.dashboard.data import DashboardReader
from water_stress.dashboard.s3_cache import (
    DashboardSnapshot,
    DashboardStorageSettings,
    S3ObjectStore,
    dashboard_files,
    fetch_dashboard,
    publish_dashboard,
)
from water_stress.transformation import common, gold_weekly, spatial_grid


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.transfers: list[tuple[str, str]] = []

    def read(self, key: str) -> bytes:
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key]

    def write(self, key: str, content: bytes) -> None:
        self.transfers.append(("write", key))
        self.objects[key] = content

    def upload(self, key: str, path: Path) -> None:
        self.transfers.append(("upload", key))
        self.objects[key] = path.read_bytes()

    def download(self, key: str, path: Path) -> None:
        self.transfers.append(("download", key))
        path.write_bytes(self.read(key))


@pytest.fixture
def publication(dashboard_data: Settings, tmp_path: Path):
    root = gold_weekly.dataset_path(dashboard_data)
    common.write_json(root / "_schema.json", {"grain": "grid_id,week_start"})
    common.write_json(root / "_quality.json", {"quality_status": "warning", "row_count": 6})
    common.write_json(
        spatial_grid.dataset_path(dashboard_data) / "_metadata.json",
        {"area_crs": "EPSG:5880", "area_unit": "km2"},
    )
    config = DashboardStorageSettings(
        source="s3",
        location=CloudLocation(bucket="example-water-stress", prefix="dashboard"),
        cache_dir=tmp_path / "cache",
    )
    store = MemoryStore()
    snapshot = publish_dashboard(dashboard_data, config.location, store)
    return dashboard_data, config, store, snapshot


def test_s3_configuration_is_explicit_and_defaults_to_postgres(monkeypatch) -> None:
    assert DashboardStorageSettings().source == "postgres"
    monkeypatch.setenv("WATER_STRESS_DASHBOARD__SOURCE", "s3")
    with pytest.raises(ValidationError, match="BUCKET"):
        DashboardStorageSettings()
    monkeypatch.setenv("WATER_STRESS_DASHBOARD__LOCATION__BUCKET", "example-water-stress")
    monkeypatch.setenv("WATER_STRESS_DASHBOARD__LOCATION__PREFIX", "water-stress/dashboard")
    assert DashboardStorageSettings().location.prefix == "water-stress/dashboard"
    monkeypatch.setenv("WATER_STRESS_DASHBOARD__SOURCE", "unknown")
    with pytest.raises(ValidationError):
        DashboardStorageSettings()


def test_dashboard_publication_is_deterministic_immutable_and_manifest_last(publication) -> None:
    settings, config, store, snapshot = publication
    assert (
        snapshot.snapshot_id
        == snapshot.model_copy(
            update={"created_at_utc": datetime(2020, 1, 1, tzinfo=UTC)}
        ).snapshot_id
    )
    assert len(snapshot.files) == 7
    assert all(entry.path.startswith(("silver/", "gold/")) for entry in snapshot.files)
    assert store.transfers[-2][1].endswith("/manifest.json")
    assert store.transfers[-1] == ("write", "dashboard/current.json")
    before = dict(store.objects)
    store.transfers.clear()
    repeated = publish_dashboard(settings, config.location, store)
    assert repeated == snapshot
    assert store.objects == before
    assert store.transfers == [("write", "dashboard/current.json")]


def test_s3_cache_matches_original_indicators_without_database(publication, monkeypatch) -> None:
    settings, config, store, snapshot = publication
    expected = DashboardReader(settings)
    cached = fetch_dashboard(settings, config, store)
    monkeypatch.setattr(DashboardReader, "_query", Mock(side_effect=AssertionError("DB accessed")))
    reader = DashboardReader(cached)
    assert cached.storage.gold_root_path == config.cache_dir / snapshot.snapshot_id / "gold"
    assert reader.weeks() == expected.weeks()
    for week in reader.weeks():
        assert summarize(reader.week(week)) == summarize(expected.week(week))
    assert reader.context()["parameters_origin"] == "Gold"
    store.transfers.clear()
    assert fetch_dashboard(settings, config, store) == cached
    assert not store.transfers


def test_interrupted_cache_resumes_verified_files_without_promoting_partial(
    publication, monkeypatch
) -> None:
    settings, config, store, snapshot = publication
    original = store.download
    completed = []

    def interrupted(key: str, path: Path) -> None:
        if len(completed) == 1:
            raise RuntimeError("temporary interruption")
        original(key, path)
        completed.append(key)

    monkeypatch.setattr(store, "download", interrupted)
    with pytest.raises(RuntimeError, match="interruption"):
        fetch_dashboard(settings, config, store)
    assert not (config.cache_dir / snapshot.snapshot_id).exists()
    monkeypatch.setattr(store, "download", original)
    fetch_dashboard(settings, config, store)
    assert store.transfers.count(("download", completed[0])) == 1
    assert (config.cache_dir / snapshot.snapshot_id / "manifest.json").is_file()


def test_corrupt_remote_object_is_not_promoted(publication) -> None:
    settings, config, store, snapshot = publication
    key = f"dashboard/snapshots/{snapshot.snapshot_id}/data/{snapshot.files[0].path}"
    store.objects[key] = b"corrupt"
    with pytest.raises(ValueError, match="checksum"):
        fetch_dashboard(settings, config, store)
    assert not (config.cache_dir / snapshot.snapshot_id).exists()


def test_corrupt_reused_cache_is_rejected(publication) -> None:
    settings, config, store, snapshot = publication
    fetch_dashboard(settings, config, store)
    (config.cache_dir / snapshot.snapshot_id / snapshot.files[0].path).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="cache corrupted"):
        fetch_dashboard(settings, config, store)


@pytest.mark.parametrize("changed", ["study", "missing-file"])
def test_manifest_must_match_complete_configured_study(publication, changed: str) -> None:
    settings, config, store, snapshot = publication
    data = snapshot.model_dump(mode="json")
    if changed == "study":
        data["study"]["area_code"] = "52"
    else:
        data["files"].pop()
    store.objects["dashboard/current.json"] = json.dumps(data).encode()
    with pytest.raises(ValueError, match="configured study"):
        fetch_dashboard(settings, config, store)
    assert not config.cache_dir.exists()


def test_failed_quality_and_missing_week_preserve_previous_publication(publication) -> None:
    settings, config, store, _ = publication
    root = gold_weekly.dataset_path(settings)
    before = dict(store.objects)
    common.write_json(root / "_quality.json", {"quality_status": "failed", "row_count": 6})
    with pytest.raises(ValueError, match="blocked"):
        publish_dashboard(settings, config.location, store)
    assert store.objects == before
    common.write_json(root / "_quality.json", {"quality_status": "passed", "row_count": 6})
    next(
        path for path in dashboard_files(settings).values() if path.name == "part-000.parquet"
    ).unlink()
    with pytest.raises(FileNotFoundError, match="incomplete"):
        publish_dashboard(settings, config.location, store)
    assert store.objects == before


def test_interrupted_publication_keeps_previous_current_and_retries(
    publication, monkeypatch
) -> None:
    settings, config, store, first = publication
    common.write_json(gold_weekly.dataset_path(settings) / "_metadata.json", {"revision": 2})
    previous_current = store.objects["dashboard/current.json"]
    original = store.upload
    monkeypatch.setattr(store, "upload", Mock(side_effect=RuntimeError("upload interrupted")))
    with pytest.raises(RuntimeError):
        publish_dashboard(settings, config.location, store)
    assert store.objects["dashboard/current.json"] == previous_current
    monkeypatch.setattr(store, "upload", original)
    second = publish_dashboard(settings, config.location, store)
    assert second.snapshot_id != first.snapshot_id
    assert f"dashboard/snapshots/{first.snapshot_id}/manifest.json" in store.objects
    assert fetch_dashboard(settings, config, store)


def test_mutating_source_during_upload_does_not_publish_manifest(publication, monkeypatch) -> None:
    settings, config, store, _ = publication
    path = gold_weekly.dataset_path(settings) / "_metadata.json"
    common.write_json(path, {"revision": 2})
    previous_current = store.objects["dashboard/current.json"]
    original = store.upload

    def upload(key: str, source: Path) -> None:
        original(key, source)
        if source == path:
            path.write_text('{"revision":3}')

    monkeypatch.setattr(store, "upload", upload)
    with pytest.raises(ValueError, match="changed during"):
        publish_dashboard(settings, config.location, store)
    assert store.objects["dashboard/current.json"] == previous_current


def test_symlinks_and_path_traversal_are_rejected(publication, tmp_path: Path) -> None:
    settings, config, store, snapshot = publication
    entry = snapshot.files[0]
    with pytest.raises(ValidationError):
        SnapshotFile(path="gold/../../secret", size_bytes=0, sha256="a" * 64)
    with pytest.raises(ValidationError, match="Silver and Gold"):
        DashboardSnapshot(
            study=settings.study,
            files=(SnapshotFile(path="bronze/secret", size_bytes=0, sha256="a" * 64),),
        )
    target = tmp_path / "another-cache"
    target.mkdir()
    config.cache_dir.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links"):
        fetch_dashboard(settings, config, store)
    config.cache_dir.unlink()
    source = dashboard_files(settings)[entry.path]
    source.unlink()
    source.symlink_to(tmp_path / "secret")
    with pytest.raises(ValueError, match="symbolic links"):
        publish_dashboard(settings, config.location, store)


def test_sdk_uses_role_chain_bounded_retries_encryption_and_redacted_errors(monkeypatch) -> None:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError

    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr(boto3, "client", factory)
    store = S3ObjectStore(CloudLocation(bucket="example-water-stress"))
    config = factory.call_args.kwargs["config"]
    assert config.connect_timeout == 10
    assert config.read_timeout == 60
    assert config.retries == {"mode": "standard", "total_max_attempts": 3}
    assert "aws_access_key_id" not in factory.call_args.kwargs
    client.get_object.return_value = {"Body": io.BytesIO(b"{}")}
    assert store.read("current.json") == b"{}"
    store.write("current.json", b"{}")
    assert client.put_object.call_args.kwargs["ServerSideEncryption"] == "AES256"
    store.upload("data.parquet", Path("example.parquet"))
    assert client.upload_file.call_args.kwargs["ExtraArgs"] == {"ServerSideEncryption": "AES256"}
    store.download("data.parquet", Path("cache.parquet"))
    client.get_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "secret-token"}}, "GetObject"
    )
    with pytest.raises(RuntimeError, match="permissions") as error:
        store.read("current.json")
    assert "secret-token" not in str(error.value)
    client.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    with pytest.raises(FileNotFoundError):
        store.read("current.json")
    client.get_object.side_effect = NoCredentialsError()
    with pytest.raises(RuntimeError, match="credentials"):
        store.read("current.json")
    client.get_object.side_effect = None
    client.get_object.return_value = {"Body": io.BytesIO(b"x" * (1024 * 1024 + 1))}
    with pytest.raises(ValueError, match="1 MiB"):
        store.read("current.json")


def test_streamlit_s3_source_never_queries_postgres(publication, monkeypatch) -> None:
    pytest.importorskip("streamlit")
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    from water_stress.dashboard import s3_cache

    settings, config, store, _ = publication
    monkeypatch.setenv("WATER_STRESS_DASHBOARD__SOURCE", "s3")
    monkeypatch.setenv("WATER_STRESS_DASHBOARD__LOCATION__BUCKET", config.location.bucket)
    monkeypatch.setenv("WATER_STRESS_DASHBOARD__LOCATION__PREFIX", config.location.prefix)
    monkeypatch.setenv("WATER_STRESS_DASHBOARD__CACHE_DIR", str(config.cache_dir))
    monkeypatch.setattr(s3_cache, "S3ObjectStore", lambda *_: store)
    monkeypatch.setattr("water_stress.config.load_settings", lambda *_: settings)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("sys.argv", ["dashboard_app.py"])
    monkeypatch.setattr(DashboardReader, "_query", Mock(side_effect=AssertionError("DB accessed")))
    st.cache_data.clear()
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "dashboard_app.py"), default_timeout=15
    ).run()
    assert not app.exception and not app.error
    assert app.metric[0].value == "85,7"
    next(button for button in app.button if button.label == "Atualizar leitura").click().run()
    assert not app.exception and not app.error
    assert app.metric[0].value == "85,7"


def test_publication_cli_reports_snapshot_and_rejects_unconfigured_s3(
    publication, monkeypatch, capsys
) -> None:
    from water_stress.pipelines import run_dashboard_publication

    settings, config, _, snapshot = publication
    monkeypatch.setattr("sys.argv", ["run_dashboard_publication"])
    monkeypatch.setattr(run_dashboard_publication, "load_settings", lambda *_: settings)
    monkeypatch.setattr(run_dashboard_publication, "DashboardStorageSettings", lambda: config)
    publish = Mock(return_value=snapshot)
    monkeypatch.setattr(run_dashboard_publication, "publish_dashboard", publish)
    monkeypatch.setattr(run_dashboard_publication, "configure_logging", lambda: None)
    assert run_dashboard_publication.main() == 0
    assert json.loads(capsys.readouterr().out)["snapshot_id"] == snapshot.snapshot_id
    monkeypatch.setattr(
        run_dashboard_publication,
        "DashboardStorageSettings",
        lambda: DashboardStorageSettings(source="postgres"),
    )
    assert run_dashboard_publication.main() == 1
    assert publish.call_count == 1
