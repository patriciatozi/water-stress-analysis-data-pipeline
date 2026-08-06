from __future__ import annotations

from pathlib import Path

import pytest

from water_stress.config import Settings
from water_stress.http import HttpResponse
from water_stress.models import IngestionState
from water_stress.pipelines.run_ingestion import build_parser, run


class PipelineHttpClient:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = responses

    def get(self, **kwargs: object) -> HttpResponse:
        return HttpResponse(self.responses.pop(0), 200, "application/json", str(kwargs["url"]))


def test_full_pipeline_ingests_in_dependency_order(
    settings: Settings, polygon_geojson: bytes, nasa_json: bytes
) -> None:
    results = run(
        settings,
        http=PipelineHttpClient([polygon_geojson, nasa_json]),
    )

    assert [result.source for result in results] == ["ibge", "nasa_power"]
    assert all(result.state is IngestionState.DOWNLOADED for result in results)


def test_nasa_only_requires_existing_boundary(settings: Settings) -> None:
    with pytest.raises(FileNotFoundError, match="run --source ibge first"):
        run(settings, source="nasa-power", http=PipelineHttpClient([]))


def test_dry_run_plans_both_sources_without_http(settings: Settings) -> None:
    results = run(settings, dry_run=True, http=PipelineHttpClient([]))

    assert len(results) == 2
    assert all(result.state is IngestionState.PLANNED for result in results)
    assert not settings.storage.root_path.exists()


def test_cli_parser_defaults_and_options() -> None:
    defaults = build_parser().parse_args([])
    selected = build_parser().parse_args(
        ["--source", "ibge", "--force", "--dry-run", "--config", "custom.yml"]
    )

    assert defaults.source == "all"
    assert selected.source == "ibge"
    assert selected.force is True
    assert selected.dry_run is True
    assert selected.config == Path("custom.yml")
