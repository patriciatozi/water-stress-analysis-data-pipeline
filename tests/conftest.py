from __future__ import annotations

from pathlib import Path

import pytest

from water_stress.config import Settings, load_settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    loaded = load_settings(Path("configs/project.yml"))
    return loaded.model_copy(
        update={"storage": loaded.storage.model_copy(update={"root_path": tmp_path / "bronze"})}
    )


@pytest.fixture
def polygon_geojson() -> bytes:
    return (
        b'{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},'
        b'"geometry":{"type":"Polygon","coordinates":[[[0,0],[4,0],[4,4],[0,4],[0,0]]]}}]}'
    )


@pytest.fixture
def nasa_json() -> bytes:
    return b'{"properties":{"parameter":{"T2M":{"20230901":25.0}}}}'
