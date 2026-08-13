from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from water_stress.config import load_settings


def test_load_default_settings() -> None:
    settings = load_settings()

    assert settings.study.municipality_code == "5107925"
    assert settings.study.start_date.isoformat() == "2023-09-01"
    assert settings.mapbiomas.soybean_class == 39
    assert len(settings.config_hash) == 64


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("municipality_code", "123", "exactly 7 digits"),
        ("municipality_code", "abcdefg", "exactly 7 digits"),
        ("start_date", "2025-01-01", "start_date must be"),
    ],
)
def test_rejects_invalid_study_configuration(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    raw = yaml.safe_load(Path("configs/project.yml").read_text())
    raw["study"][field] = value
    path = tmp_path / "invalid.yml"
    path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match=message):
        load_settings(path)


def test_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yml"
    path.write_text("- item\n")

    with pytest.raises(ValueError, match="must be a YAML mapping"):
        load_settings(path)


def test_rejects_invalid_mapbiomas_class(tmp_path: Path) -> None:
    raw = yaml.safe_load(Path("configs/project.yml").read_text())
    raw["mapbiomas"]["soybean_class"] = 0
    path = tmp_path / "invalid.yml"
    path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        load_settings(path)
