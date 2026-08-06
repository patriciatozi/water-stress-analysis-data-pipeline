from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from water_stress.config import Settings
from water_stress.http import HttpGetter
from water_stress.ingestion.common import (
    fingerprint,
    persist_download,
    reusable_result,
    versioned_paths,
)
from water_stress.models import IngestionResult, IngestionState
from water_stress.storage import StorageClient

SOURCE = "nasa_power"


def build_request(
    settings: Settings, *, latitude: float, longitude: float
) -> tuple[str, dict[str, str | float]]:
    params: dict[str, str | float] = {
        "parameters": ",".join(settings.nasa_power.parameters),
        "community": settings.nasa_power.community,
        "longitude": longitude,
        "latitude": latitude,
        "start": settings.study.start_date.strftime("%Y%m%d"),
        "end": settings.study.end_date.strftime("%Y%m%d"),
        "format": "JSON",
        "time-standard": settings.nasa_power.time_standard,
    }
    return str(settings.nasa_power.base_url), params


def artifact_path(settings: Settings) -> Path:
    return (
        settings.storage.root_path
        / "nasa_power"
        / "daily"
        / f"municipality_code={settings.study.municipality_code}"
        / f"start_date={settings.study.start_date.isoformat()}"
        / f"end_date={settings.study.end_date.isoformat()}"
        / "weather.json"
    )


def validate_response(content: bytes) -> dict[str, Any]:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("NASA POWER response is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("NASA POWER response must be a JSON object")
    parameters = document.get("properties", {}).get("parameter")
    if not isinstance(parameters, dict) or not parameters:
        raise ValueError("NASA POWER response has no daily parameter data")
    return document


def ingest(
    settings: Settings,
    *,
    latitude: float,
    longitude: float,
    http: HttpGetter,
    storage: StorageClient,
    force: bool = False,
    dry_run: bool = False,
) -> IngestionResult:
    url, params = build_request(settings, latitude=latitude, longitude=longitude)
    request_payload = {"url": url, "params": params}
    request_fingerprint = fingerprint(request_payload)
    path, manifest_path = versioned_paths(artifact_path(settings), force=force)
    if dry_run:
        return IngestionResult(SOURCE, path, manifest_path, None, None, IngestionState.PLANNED)
    if not force:
        reused = reusable_result(
            source=SOURCE,
            storage=storage,
            artifact_path=path,
            manifest_path=manifest_path,
            request_fingerprint=request_fingerprint,
        )
        if reused:
            return reused
    response = http.get(source=SOURCE, url=url, params=params)
    validate_response(response.content)
    return persist_download(
        source=SOURCE,
        storage=storage,
        artifact_path=path,
        manifest_path=manifest_path,
        content=response.content,
        manifest={
            "source": SOURCE,
            "dataset": "daily_point_meteorology",
            "url": url,
            "final_url": response.final_url,
            "parameters": params,
            "http_status": response.status_code,
            "content_type": response.content_type,
            "municipality_code": settings.study.municipality_code,
            "municipality_name": settings.study.municipality_name,
            "study_start_date": settings.study.start_date.isoformat(),
            "study_end_date": settings.study.end_date.isoformat(),
            "representative_point": {"latitude": latitude, "longitude": longitude},
            "project_version": settings.project.version,
            "config_sha256": settings.config_hash,
            "request_fingerprint": request_fingerprint,
        },
    )
