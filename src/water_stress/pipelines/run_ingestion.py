from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from pathlib import Path

from water_stress.config import Settings, load_settings
from water_stress.http import HttpClient, HttpGetter
from water_stress.ingestion import ibge, nasa_power
from water_stress.logging import configure_logging
from water_stress.models import IngestionResult
from water_stress.storage import LocalStorageClient

LOGGER = logging.getLogger(__name__)


def run(
    settings: Settings,
    *,
    source: str = "all",
    force: bool = False,
    dry_run: bool = False,
    http: HttpGetter | None = None,
) -> list[IngestionResult]:
    storage = LocalStorageClient()
    own_http = http is None
    http_client = http or HttpClient(settings.http)
    results: list[IngestionResult] = []
    try:
        if dry_run:
            if source in {"all", "ibge"}:
                results.append(
                    ibge.ingest(
                        settings, http=http_client, storage=storage, force=force, dry_run=True
                    )
                )
            if source in {"all", "nasa-power"}:
                results.append(
                    nasa_power.ingest(
                        settings,
                        latitude=0.0,
                        longitude=0.0,
                        http=http_client,
                        storage=storage,
                        force=force,
                        dry_run=True,
                    )
                )
            return results

        ibge_content: bytes
        if source in {"all", "ibge"}:
            ibge_result = ibge.ingest(
                settings, http=http_client, storage=storage, force=force, dry_run=False
            )
            results.append(ibge_result)
            ibge_content = storage.read_bytes(ibge_result.artifact_path)
        else:
            boundary_path = ibge.artifact_path(settings)
            if not storage.exists(boundary_path):
                raise FileNotFoundError(
                    "NASA POWER ingestion requires the IBGE Bronze boundary; "
                    "run --source ibge first"
                )
            ibge_content = storage.read_bytes(boundary_path)

        if source in {"all", "nasa-power"}:
            latitude, longitude = ibge.representative_point(ibge_content)
            results.append(
                nasa_power.ingest(
                    settings,
                    latitude=latitude,
                    longitude=longitude,
                    http=http_client,
                    storage=storage,
                    force=force,
                    dry_run=False,
                )
            )
        return results
    finally:
        if own_http:
            assert isinstance(http_client, HttpClient)
            http_client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ingest MVP source data into Bronze storage")
    parser.add_argument("--config", type=Path, default=Path("configs/project.yml"))
    parser.add_argument("--source", choices=("all", "ibge", "nasa-power"), default="all")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()
    settings = load_settings(args.config)
    results = run(settings, source=args.source, force=args.force, dry_run=args.dry_run)
    for result in results:
        print(
            json.dumps(
                {
                    "source": result.source,
                    "state": result.state,
                    "artifact_path": str(result.artifact_path),
                    "manifest_path": str(result.manifest_path),
                    "sha256": result.checksum,
                    "size_bytes": result.size_bytes,
                }
            )
        )
    LOGGER.info("Ingestion finished", extra={"event": "pipeline_complete"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
