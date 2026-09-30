# Project context

This repository implements an academic geospatial data pipeline for estimating soybean
water-stress risk in Brazil. Prefer simple, reliable solutions that can grow from local execution
to cloud object storage and distributed processing without rewriting business rules.

## Architecture

- Bronze contains immutable source data and request manifests.
- Silver contains validated, standardized Parquet, GeoParquet and GeoTIFF datasets.
- Gold contains analytical space-time features and indicators.
- Keep ingestion, transformation, storage and orchestration as separate concerns.
- Business rules must not depend on CLI, HTTP or local-filesystem implementations.
- Depend on small interfaces at infrastructure boundaries; avoid premature abstractions elsewhere.
- Raw data must never be modified after ingestion.
- New outputs must be deterministic, idempotent and partitioned by stable analytical keys.
- Preserve backward compatibility unless a migration is explicitly documented.

## Code quality

- Write the smallest clear implementation that satisfies the requirement.
- Prefer explicit, cohesive functions and modules over clever or generic frameworks.
- Keep one responsibility per function or class; extract code only when it removes real duplication
  or isolates a business rule.
- Use descriptive domain names and avoid unexplained abbreviations.
- Use Python type hints on public functions, methods and data structures.
- Use `pathlib.Path` for paths and Pydantic for configuration and boundary validation.
- Use dataclasses, enums or Pydantic models for stable contracts instead of untyped dictionaries.
- Do not duplicate configuration, schemas, constants, formulas or path-building rules.
- Keep public APIs narrow and avoid hidden global state and import-time side effects.
- Handle expected errors with actionable messages; never silently ignore invalid data.
- Comments and docstrings must explain intent, assumptions or non-obvious trade-offs, not restate code.
- Remove dead code, unused compatibility layers and speculative features.
- Follow the existing project style before introducing a new dependency or pattern.

## Scalability and sustainability

- Process large datasets incrementally, by window, chunk or partition; avoid loading full rasters or
  tables into memory when bounded processing is possible.
- Stream downloads and writes when supported by the source.
- Use atomic writes and never expose partial outputs as complete datasets.
- Make retries bounded and limited to transient failures; include timeout and backoff.
- Reuse valid artifacts and support safe restart after interruption.
- Minimize network requests, disk copies, repeated reprojections and raster resampling.
- Push source-independent logic into pure functions so execution engines and storage backends can
  change without altering business rules.
- Do not couple dataset paths to a local machine. Storage keys must also work with S3 or ADLS.
- Prefer open, interoperable formats and compression appropriate to the access pattern.
- Record enough lineage to reproduce a result without producing redundant metadata.
- Optimize only from measured bottlenecks; document important performance trade-offs.

## Data contracts and observability

- Every derived dataset must define its grain, primary key, schema, units, CRS, resolution, temporal
  coverage, source and processing version.
- Validate required columns, types, unique keys, ranges, missing values and row counts at boundaries.
- Make null, nodata, duplicate and invalid-geometry policies explicit.
- Use structured logging with source, operation, partition and outcome; never log credentials.
- Logs should describe lifecycle events and failures, not emit noisy per-row messages.
- Manifests and metadata must include extraction or processing timestamp and checksums where relevant.

## Geospatial rules

- Preserve source bytes and the original CRS in Bronze.
- Use EPSG:4326 for API queries.
- Use an appropriate SIRGAS 2000 / UTM CRS for distance and area calculations.
- Validate and, only when safe, repair geometries before spatial operations.
- Do not silently reproject, resample or align rasters.
- Document the target grid, resolution and resampling method. Use nearest neighbour for categorical
  data and an explicitly justified method for continuous data.
- Treat nodata separately from valid zero values.

## Testing

- Add unit tests for business rules, calculations, validation and path construction.
- Add integration tests for external clients and storage boundaries using mocks or small fixtures.
- Never require live network access in the default test suite.
- Cover success, invalid input, empty data, partial data, retry, idempotency and restart behavior when
  applicable.
- Keep fixtures minimal and synthetic; do not commit downloaded source datasets.
- A bug fix must include a regression test.
- Test observable behavior and contracts rather than private implementation details.

## Documentation

- Update `README.md` or `docs/implementation_guide.md` when commands, architecture, schemas or
  business rules change.
- Document formulas, thresholds, assumptions, units and resampling decisions next to the relevant
  dataset description.
- Prefer concise examples and tables over duplicated prose.
- Do not claim that a layer, source or validation exists before it is implemented and tested.

## Security and repository hygiene

- Do not commit credentials, tokens, personal paths, downloaded datasets or generated secrets.
- Validate external input and URLs at system boundaries.
- Add dependencies only when their value exceeds their maintenance and security cost.
- Do not modify unrelated user changes or generated data.

## Commands

- Install: `uv sync`
- Test: `uv run pytest`
- Lint: `uv run ruff check .`
- Format check: `uv run ruff format --check .`
- Type check: `uv run mypy src`

## Definition of done

A task is complete only when:

- implementation and tests satisfy the stated data contract;
- tests, Ruff, formatting and mypy pass for the affected scope;
- error handling and structured logging are appropriate to the operation;
- documentation, assumptions, units and lineage are current;
- outputs are deterministic, idempotent and safely restartable when applicable;
- no credentials, source data or unrelated changes are included;
- the solution introduces no unnecessary abstraction, dependency or verbosity.
