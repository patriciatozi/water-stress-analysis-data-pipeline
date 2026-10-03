from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from water_stress.config import Settings
from water_stress.pipelines import run_quality
from water_stress.transformation.gold_weekly import dataset_path


@pytest.mark.parametrize("status", ["passed", "warning"])
def test_quality_gate_accepts_nonempty_gold(tmp_path: Path, status: str) -> None:
    path = tmp_path / "_quality.json"
    path.write_text(json.dumps({"quality_status": status, "row_count": 10}))
    assert run_quality.validate_quality_report(path).quality_status == status


@pytest.mark.parametrize("status,rows", [("failed", 10), ("passed", 0)])
def test_quality_gate_blocks_failed_or_empty_gold(tmp_path: Path, status: str, rows: int) -> None:
    path = tmp_path / "_quality.json"
    path.write_text(json.dumps({"quality_status": status, "row_count": rows}))
    with pytest.raises(ValueError, match="publication blocked"):
        run_quality.validate_quality_report(path)


def test_quality_gate_rejects_unknown_status(tmp_path: Path) -> None:
    path = tmp_path / "_quality.json"
    path.write_text(json.dumps({"quality_status": "unknown", "row_count": 10}))
    with pytest.raises(ValidationError):
        run_quality.validate_quality_report(path)


def test_quality_gate_requires_report(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_quality.validate_quality_report(tmp_path / "missing.json")


def test_quality_command_uses_configured_gold_root(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = dataset_path(settings) / "_quality.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"quality_status": "warning", "row_count": 10}))
    monkeypatch.setattr(run_quality, "load_settings", lambda _: settings)
    monkeypatch.setattr("sys.argv", ["run_quality", "--config", "configs/project.yml"])
    assert run_quality.main() == 0
    assert json.loads(capsys.readouterr().out)["row_count"] == 10
