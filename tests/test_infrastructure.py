from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from water_stress import cloud_transfer, infrastructure
from water_stress.cloud_transfer import (
    CloudLocation,
    DataSnapshot,
    fetch_data,
    inventory,
    publish_data,
)
from water_stress.pipelines import run_infrastructure
from water_stress.pipelines.run_infrastructure import main


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    root = tmp_path / "source" / "data"
    for layer in ("bronze", "silver", "gold"):
        path = root / layer / "state_code=51" / "example.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(layer.encode())
    return root


@pytest.fixture
def location() -> CloudLocation:
    return CloudLocation(bucket="example-water-stress")


def mock_aws(monkeypatch, snapshot: DataSnapshot, *, corrupt: bool = False) -> list[list[str]]:
    calls: list[list[str]] = []

    def execute(location: CloudLocation, arguments: list[str]) -> str:
        calls.append(arguments)
        if arguments[:2] == ["s3api", "list-objects-v2"]:
            return "null"
        if (
            arguments[:2] == ["s3", "cp"]
            and arguments[2].startswith("s3://")
            and arguments[2].endswith("manifest.json")
        ):
            Path(arguments[3]).write_text(snapshot.model_dump_json())
        if arguments[:2] == ["s3", "sync"]:
            staging = Path(arguments[3])
            for entry in snapshot.files:
                path = staging / entry.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"corrupt" if corrupt else entry.path.split("/")[0].encode())
        return ""

    monkeypatch.setattr(cloud_transfer, "aws_command", execute)
    return calls


def test_snapshot_is_deterministic_and_excludes_temporary_files(data_dir: Path) -> None:
    (data_dir / "bronze" / ".env").write_text("do not publish")
    (data_dir / "silver" / "unfinished.parquet.tmp").write_text("partial")
    first = DataSnapshot(files=inventory(data_dir))
    second = DataSnapshot(files=inventory(data_dir))
    assert first.snapshot_id == second.snapshot_id
    assert len(first.files) == 3
    assert first.created_at_utc.tzinfo is not None
    (data_dir / first.files[0].path).write_bytes(b"changed")
    assert DataSnapshot(files=inventory(data_dir)).snapshot_id != first.snapshot_id


def test_inventory_rejects_symlinks(data_dir: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("secret")
    (data_dir / "bronze" / "linked.json").symlink_to(secret)
    with pytest.raises(ValueError, match="Symbolic links"):
        inventory(data_dir)


@pytest.mark.parametrize("bucket", ["s3://example", "a", "127.0.0.1", "example..bucket"])
def test_invalid_bucket(bucket: str) -> None:
    with pytest.raises(ValidationError):
        CloudLocation(bucket=bucket)


@pytest.mark.parametrize("prefix", ["../escape", "/root", "a//b", "x?query"])
def test_invalid_prefix(prefix: str) -> None:
    with pytest.raises(ValidationError):
        CloudLocation(bucket="example-bucket", prefix=prefix)


def test_empty_and_partial_data_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        DataSnapshot(files=inventory(tmp_path))
    (tmp_path / "bronze").mkdir()
    (tmp_path / "bronze" / "file.json").write_text("source")
    with pytest.raises(ValidationError, match="nonempty"):
        DataSnapshot(files=inventory(tmp_path))


def test_publish_commits_manifest_last(
    data_dir: Path, location: CloudLocation, monkeypatch
) -> None:
    snapshot = DataSnapshot(files=inventory(data_dir))
    calls = mock_aws(monkeypatch, snapshot)
    assert publish_data(data_dir, location).snapshot_id == snapshot.snapshot_id
    assert "--recursive" in calls[-2]
    assert calls[-1][3].endswith("/manifest.json")
    assert all("--delete" not in call for call in calls)


def test_publish_failure_leaves_no_completion_manifest(
    data_dir: Path, location: CloudLocation, monkeypatch
) -> None:
    execute = Mock(side_effect=["null", RuntimeError("interrupted")])
    monkeypatch.setattr(cloud_transfer, "aws_command", execute)
    with pytest.raises(RuntimeError, match="interrupted"):
        publish_data(data_dir, location)
    assert execute.call_count == 2


def test_publish_reuses_completed_snapshot(
    data_dir: Path, location: CloudLocation, monkeypatch
) -> None:
    snapshot = DataSnapshot(files=inventory(data_dir))
    calls = mock_aws(monkeypatch, snapshot)
    original = cloud_transfer.aws_command

    def execute(target: CloudLocation, arguments: list[str]) -> str:
        if arguments[0] == "s3api":
            return json.dumps([f"{target.prefix}/snapshots/{snapshot.snapshot_id}/manifest.json"])
        return original(target, arguments)

    monkeypatch.setattr(cloud_transfer, "aws_command", execute)
    result = publish_data(data_dir, location)
    assert result.created_at_utc == snapshot.created_at_utc
    assert len(calls) == 1  # Only the manifest is read; no data uploads.


def test_changed_source_prevents_manifest_commit(
    data_dir: Path, location: CloudLocation, monkeypatch
) -> None:
    snapshot = DataSnapshot(files=inventory(data_dir))
    original_inventory = cloud_transfer.inventory
    monkeypatch.setattr(cloud_transfer, "inventory", Mock(side_effect=[snapshot.files, ()]))
    calls = mock_aws(monkeypatch, snapshot)
    with pytest.raises(ValueError, match="changed during upload"):
        publish_data(data_dir, location)
    assert len(calls) == 2
    assert original_inventory(data_dir) == snapshot.files


def test_fetch_validates_bytes_and_reuses_existing_data(
    data_dir: Path, location: CloudLocation, tmp_path: Path, monkeypatch
) -> None:
    snapshot = DataSnapshot(files=inventory(data_dir))
    calls = mock_aws(monkeypatch, snapshot)
    destination = tmp_path / "destination" / "data"
    fetch_data(destination, location, snapshot.snapshot_id)
    assert inventory(destination) == snapshot.files
    assert (destination / ".snapshot.json").is_file()
    count = len(calls)
    fetch_data(destination, location, snapshot.snapshot_id)
    assert len(calls) == count + 1  # The manifest is read, but data are reused.


def test_corrupt_download_can_restart_without_publishing_partial_data(
    data_dir: Path, location: CloudLocation, tmp_path: Path, monkeypatch
) -> None:
    snapshot = DataSnapshot(files=inventory(data_dir))
    destination = tmp_path / "destination" / "data"
    mock_aws(monkeypatch, snapshot, corrupt=True)
    with pytest.raises(ValueError, match="checksum"):
        fetch_data(destination, location, snapshot.snapshot_id)
    assert not destination.exists()
    mock_aws(monkeypatch, snapshot)
    fetch_data(destination, location, snapshot.snapshot_id)
    assert inventory(destination) == snapshot.files


def test_fetch_refuses_conflicts_before_downloading(
    data_dir: Path, location: CloudLocation, monkeypatch
) -> None:
    snapshot = DataSnapshot(files=inventory(data_dir))
    (data_dir / snapshot.files[0].path).write_bytes(b"different source")
    calls = mock_aws(monkeypatch, snapshot)
    with pytest.raises(ValueError, match="conflicting"):
        fetch_data(data_dir, location, snapshot.snapshot_id)
    assert len(calls) == 1


def test_fetch_rejects_wrong_manifest_and_unsafe_paths(
    data_dir: Path, location: CloudLocation, tmp_path: Path, monkeypatch
) -> None:
    snapshot = DataSnapshot(files=inventory(data_dir))
    mock_aws(monkeypatch, snapshot)
    with pytest.raises(ValueError, match="requested snapshot"):
        fetch_data(tmp_path / "target", location, "a" * 64)
    raw = snapshot.model_dump(mode="json")
    raw["files"][0]["path"] = "bronze/../../escape"
    with pytest.raises(ValidationError, match="safe relative"):
        DataSnapshot.model_validate(raw)
    raw = snapshot.model_dump(mode="json")
    raw["files"].append(raw["files"][0])
    with pytest.raises(ValidationError, match="sorted and unique"):
        DataSnapshot.model_validate(raw)


def test_compose_selection_and_load_use_existing_cli(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "aws.env"
    env_file.touch()
    execute = Mock(return_value="ok")
    monkeypatch.setattr(infrastructure, "run_command", execute)
    infrastructure.run_services("aws", "load", env_file)
    command = execute.call_args.args[0]
    assert "compose.aws.yml" in command
    assert "water-stress-aws" in command
    assert "water_stress.pipelines.run_database" in command
    assert "--no-deps" in command
    assert "--dataset" in command
    assert "compose.aws.yml" not in infrastructure.compose_command("local", env_file)
    assert "--project-name" not in infrastructure.compose_command("local", env_file)


def test_aws_retries_are_bounded(location: CloudLocation, monkeypatch) -> None:
    execute = Mock(return_value="ok")
    monkeypatch.setattr(cloud_transfer, "run_command", execute)
    cloud_transfer.aws_command(location, ["s3", "ls"])
    assert execute.call_args.kwargs["environment"]["AWS_MAX_ATTEMPTS"] == "3"
    assert "--cli-read-timeout" in execute.call_args.args[0]


def test_missing_environment_and_tool_errors_are_actionable(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(ValueError, match=r"aws\.env\.example"):
        infrastructure.run_services("aws", "up", tmp_path / "missing.env")
    monkeypatch.setattr(infrastructure.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="Install aws"):
        infrastructure.run_command(["aws", "s3", "ls"])
    monkeypatch.setattr(infrastructure.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        infrastructure.subprocess,
        "run",
        Mock(side_effect=(subprocess.CalledProcessError(1, "aws", stderr="secret password"))),
    )
    with pytest.raises(RuntimeError, match="permissions") as error:
        infrastructure.run_command(["aws", "s3", "ls"])
    assert "secret password" not in str(error.value)


def test_cli_requires_explicit_data_transfer_parameters(capsys) -> None:
    assert main(["--infra", "local", "publish-data"]) == 1
    assert main(["--infra", "aws", "--bucket", "example-bucket", "fetch-data"]) == 1
    assert "requires --snapshot" in capsys.readouterr().err


def test_cli_selects_infrastructure_and_requires_completed_restore(
    tmp_path: Path, monkeypatch, capsys, data_dir: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "compose.airflow.yml").touch()
    monkeypatch.setattr(run_infrastructure, "configure_logging", lambda: None)
    execute = Mock(return_value="services ready\n")
    monkeypatch.setattr(run_infrastructure, "run_services", execute)
    assert main(["--infra", "aws", "up"]) == 1
    execute.assert_not_called()
    assert main(["--infra", "local", "up"]) == 0
    assert execute.call_args.args == ("local", "up", Path(".env.airflow"))
    snapshot = DataSnapshot(files=inventory(data_dir))
    marker = tmp_path / "data/.snapshot.json"
    marker.parent.mkdir()
    marker.write_text(snapshot.model_dump_json())
    assert main(["--infra", "aws", "load"]) == 0
    assert execute.call_args.args == ("aws", "load", Path(".env.aws"))
    assert "services ready" in capsys.readouterr().out


def test_cli_reports_published_snapshot_without_reprocessing(
    data_dir: Path, monkeypatch, capsys
) -> None:
    snapshot = DataSnapshot(files=inventory(data_dir))
    monkeypatch.setattr(run_infrastructure, "configure_logging", lambda: None)
    publish = Mock(return_value=snapshot)
    monkeypatch.setattr(run_infrastructure, "publish_data", publish)
    assert main(["--infra", "aws", "--bucket", "example-bucket", "publish-data"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["snapshot_id"] == snapshot.snapshot_id
    assert output["file_count"] == 3
    assert publish.call_args.args[0] == Path("data")


@pytest.mark.parametrize("action,expected", [("up", "up"), ("stop", "stop"), ("status", "ps")])
def test_service_lifecycle_preserves_volumes(
    tmp_path: Path, monkeypatch, action: str, expected: str
) -> None:
    env_file = tmp_path / "environment"
    env_file.touch()
    execute = Mock(return_value="ok")
    monkeypatch.setattr(infrastructure, "run_command", execute)
    assert infrastructure.run_services("local", action, env_file) == "ok"
    command = execute.call_args.args[0]
    assert expected in command
    assert "down" not in command
    assert "--volumes" not in command
