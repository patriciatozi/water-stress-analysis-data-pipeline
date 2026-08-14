from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from water_stress.config import Settings
from water_stress.http import HttpDownload, HttpResponse
from water_stress.ingestion import ibge
from water_stress.models import IngestionState
from water_stress.pipelines.run_ingestion import build_parser, run
from water_stress.storage import StorageClient


class PipelineHttpClient:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = responses

    def get(self, **kwargs: object) -> HttpResponse:
        return HttpResponse(self.responses.pop(0), 200, "application/json", str(kwargs["url"]))

    def post(self, **kwargs: object) -> HttpResponse:
        return HttpResponse(self.responses.pop(0), 200, "application/json", str(kwargs["url"]))

    def download(self, **kwargs: object) -> HttpDownload:
        content = self.responses.pop(0)
        storage = cast(StorageClient, kwargs["storage"])
        checksum, size, header = storage.write_chunks(cast(Path, kwargs["path"]), [content])
        return HttpDownload(200, "image/tiff", str(kwargs["url"]), checksum, size, header)


def test_full_pipeline_ingests_in_dependency_order(
    settings: Settings, polygon_geojson: bytes, nasa_json: bytes
) -> None:
    boundary_path = ibge.artifact_path(settings)
    boundary_path.parent.mkdir(parents=True)
    boundary_path.write_bytes(polygon_geojson)
    results = run(
        settings,
        source="nasa-power",
        http=PipelineHttpClient([nasa_json] * 7),
    )

    assert [result.source for result in results] == ["nasa_power"] * 7
    assert all(result.state is IngestionState.DOWNLOADED for result in results)


def test_nasa_only_requires_existing_boundary(settings: Settings) -> None:
    with pytest.raises(FileNotFoundError, match="run --source ibge first"):
        run(settings, source="nasa-power", http=PipelineHttpClient([]))


def test_dry_run_plans_all_sources_without_http(settings: Settings) -> None:
    results = run(settings, dry_run=True, http=PipelineHttpClient([]))

    assert len(results) == 22
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


def test_mapbiomas_does_not_require_existing_boundary(settings: Settings) -> None:
    tiff = b"II*\x00mapbiomas"

    results = run(
        settings,
        source="mapbiomas",
        http=PipelineHttpClient([tiff]),
    )

    assert [result.source for result in results] == ["mapbiomas"]
