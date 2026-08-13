# Project context

This repository implements an academic geospatial data pipeline for
estimating soybean water-stress risk in Brazil.

## Architecture

- Bronze contains immutable source data.
- Silver contains standardized Parquet, GeoParquet and GeoTIFF files.
- Gold contains analytical space-time features.
- Raw data must never be modified after ingestion.

## Engineering rules

- Use Python type hints.
- Use pathlib instead of string-based paths.
- Use Pydantic for configuration.
- Add unit tests for calculations.
- Add integration tests for external data clients using mocks.
- Do not commit credentials or downloaded datasets.
- Use structured logging.
- Every dataset must include source, extraction timestamp, CRS,
  resolution, units and processing version.

## Geospatial rules

- Preserve the original CRS in Bronze.
- Use EPSG:4326 for API queries.
- Use an appropriate SIRGAS 2000 / UTM CRS for area calculations.
- Validate geometries before spatial operations.
- Do not silently resample rasters.
- Document the selected resampling method.

## Commands

- Install: uv sync
- Test: uv run pytest
- Lint: uv run ruff check .
- Format: uv run ruff format .

## Definition of done

A task is complete only when:
- tests pass;
- lint passes;
- documentation is updated;
- no source data is committed;
- assumptions and units are documented.