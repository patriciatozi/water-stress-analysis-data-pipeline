from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from water_stress.config import Settings
from water_stress.dashboard.analysis import (
    GeographicBounds,
    geographic_subset,
    summarize,
    validate_features,
)
from water_stress.database.client import connect
from water_stress.risk_classification import RISK_CLASSIFICATION_POLICY
from water_stress.transformation import gold_weekly, spatial_grid

LOGGER = logging.getLogger(__name__)
DataSource = Literal["parquet", "postgres"]
FEATURE_COLUMNS = (
    "grid_id",
    "week_start",
    "week_end",
    "soy_fraction",
    "water_stress_score",
    "score_status",
    "water_stress_risk_class",
    "risk_classification_version",
    "monitoring_guidance",
    "score_component_count",
    "score_available_weight",
    "weather_expected_days",
    "weather_observation_count",
    "precipitation_mm_7d",
    "eto_mm_7d",
    "water_deficit_mm_7d",
    "ndvi_median",
    "ndmi_median",
    "satellite_age_days",
    "satellite_observation_date",
    "temperature_mean_c",
    "consecutive_dry_days",
    "clay_pct",
    "sand_pct",
    "soc",
    "bulk_density",
)
SPATIAL_COLUMNS = ("grid_id", "centroid_latitude", "centroid_longitude", "area_km2")
OPTIONAL_COLUMNS = {"water_stress_risk_class", "risk_classification_version", "monitoring_guidance"}


def _read_features(path: Path, settings: Settings, grid_id: str | None = None) -> pa.Table:
    source = pq.ParquetFile(path)
    missing = set(FEATURE_COLUMNS) - set(source.schema_arrow.names)
    if missing - OPTIONAL_COLUMNS:
        raise ValueError(
            "A Gold precisa ser atualizada com --source gold-weekly: "
            f"colunas ausentes {sorted(missing - OPTIONAL_COLUMNS)}"
        )
    columns = [name for name in FEATURE_COLUMNS if name not in missing]
    pieces = []
    for batch in source.iter_batches(batch_size=10000, columns=columns):
        table = pa.Table.from_batches([batch])
        if grid_id is not None:
            table = table.filter(pc.equal(table["grid_id"], pa.scalar(grid_id)))
        if table.num_rows:
            pieces.append(table)
    table = (
        pa.concat_tables(pieces)
        if pieces
        else pa.Table.from_batches([], schema=source.schema_arrow).select(columns)
    )
    for name in missing:
        table = table.append_column(name, pa.nulls(table.num_rows, type=pa.string()))
    expected = gold_weekly.gold_schema(settings)
    for name in FEATURE_COLUMNS:
        if table.schema.field(name).type != expected.field(name).type:
            raise ValueError(f"Tipo incompatível na Gold: {name}; regenere o dataset")
    return table.select(FEATURE_COLUMNS)


@dataclass(frozen=True)
class DashboardReader:
    settings: Settings
    source: DataSource = "parquet"

    def _query(self, query: str, parameters: Sequence[object] = ()) -> list[dict[str, Any]]:
        from psycopg.rows import dict_row

        with (
            connect(self.settings.database) as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SET LOCAL statement_timeout = '30s'")
            cursor.execute(query, parameters)
            return list(cursor.fetchall())

    def _scope(self) -> tuple[object, ...]:
        return (
            self.settings.study.start_date,
            self.settings.study.end_date,
            f"{self.settings.study.area_code}_{self.settings.spatial.screening_grid_meters}m_%",
        )

    def weeks(self) -> tuple[date, ...]:
        if self.source == "postgres":
            rows = self._query(
                "SELECT DISTINCT week_start FROM gold.water_stress_dashboard "
                "WHERE week_end >= %s AND week_start <= %s AND grid_id LIKE %s ORDER BY week_start",
                self._scope(),
            )
            weeks = tuple(row["week_start"] for row in rows)
        else:
            expected = gold_weekly._study_weeks(self.settings)
            root = gold_weekly.dataset_path(self.settings)
            weeks = tuple(
                start
                for start, _ in expected
                if (root / f"week_start={start}" / "part-000.parquet").is_file()
            )
        if not weeks:
            raise FileNotFoundError(
                "Nenhuma semana Gold v1 disponível. Gere --source gold-weekly "
                "ou carregue gold.water_stress_weekly no PostgreSQL."
            )
        return weeks

    def spatial_grid(self) -> pa.Table:
        """Load centroid/area only; no geometry copies or reprojection of source datasets."""
        if self.source == "postgres":
            rows = self._query(
                "SELECT grid_id, centroid_latitude, centroid_longitude, area_km2 "
                "FROM silver.dim_spatial_grid WHERE grid_id LIKE %s",
                (self._scope()[2],),
            )
            return pa.Table.from_pylist(rows)
        path = spatial_grid.dataset_path(self.settings) / "grid.parquet"
        if not path.is_file():
            raise FileNotFoundError("Dimensão espacial Silver ausente; gere --source spatial-grid")
        return pq.ParquetFile(path).read(columns=list(SPATIAL_COLUMNS))

    def week(self, start: date, grid: pa.Table | None = None) -> pa.Table:
        if start not in self.weeks():
            raise ValueError("Semana fora dos dados disponíveis")
        if self.source == "postgres":
            rows = self._query(
                f"SELECT {', '.join(FEATURE_COLUMNS)}, centroid_latitude, centroid_longitude, "
                "soy_area_km2 FROM gold.water_stress_dashboard "
                "WHERE week_start = %s AND week_end >= %s AND week_start <= %s AND grid_id LIKE %s "
                "ORDER BY grid_id",
                (start, *self._scope()),
            )
            schema = self._serving_schema()
            table = pa.Table.from_pylist(rows, schema=schema)
        else:
            path = (
                gold_weekly.dataset_path(self.settings) / f"week_start={start}" / "part-000.parquet"
            )
            features = _read_features(path, self.settings)
            self._validate_week(features, start)
            grid = grid if grid is not None else self.spatial_grid()
            if (
                grid["grid_id"].null_count
                or pc.count_distinct(grid["grid_id"]).as_py() != grid.num_rows
            ):
                raise ValueError("Dimensão espacial contém chaves ausentes ou duplicadas")
            table = features.join(grid, keys="grid_id", join_type="left outer").sort_by(
                [("grid_id", "ascending")]
            )
            table = table.append_column(
                "soy_area_km2", pc.multiply(table["area_km2"], table["soy_fraction"])
            )
            table = table.select(self._serving_schema().names)
        self._validate_week(table, start)
        for name, minimum, maximum in (
            ("centroid_latitude", -90, 90),
            ("centroid_longitude", -180, 180),
        ):
            values = table[name].to_pylist()
            if any(value is None or not minimum <= value <= maximum for value in values):
                raise ValueError("Gold sem centróide válido; atualize a dimensão espacial")
        LOGGER.info(
            "Dashboard week loaded",
            extra={
                "source": self.source,
                "operation": "dashboard_read",
                "partition": str(start),
                "outcome": "succeeded",
                "row_count": table.num_rows,
            },
        )
        return table

    def _validate_week(self, table: pa.Table, start: date) -> None:
        validate_features(table)
        end = dict(gold_weekly._study_weeks(self.settings)).get(start)
        if end is None:
            raise ValueError("Semana Gold fora do período configurado")
        for column, expected in (("week_start", start), ("week_end", end)):
            consistent = pc.equal(table[column], pa.scalar(expected))
            if table[column].null_count or (table.num_rows and not pc.all(consistent).as_py()):
                raise ValueError("Datas Gold incompatíveis com a partição semanal do estudo")

    def _serving_schema(self) -> pa.Schema:
        schema = gold_weekly.gold_schema(self.settings)
        return pa.schema(
            [schema.field(name) for name in FEATURE_COLUMNS]
            + [
                pa.field("centroid_latitude", pa.float64()),
                pa.field("centroid_longitude", pa.float64()),
                pa.field("soy_area_km2", pa.float64()),
            ]
        )

    def history(self, grid_id: str) -> pa.Table:
        """Return a single cell's observed weekly factors, never simulated/filled values."""
        if self.source == "postgres":
            rows = self._query(
                f"SELECT {', '.join(FEATURE_COLUMNS)} FROM gold.water_stress_dashboard "
                "WHERE grid_id = %s AND week_end >= %s AND week_start <= %s AND grid_id LIKE %s "
                "ORDER BY week_start",
                (grid_id, *self._scope()),
            )
            schema = self._serving_schema()
            table = pa.Table.from_pylist(
                rows, schema=pa.schema([schema.field(name) for name in FEATURE_COLUMNS])
            )
            for start in pc.unique(table["week_start"]).to_pylist():
                if not isinstance(start, date):
                    raise ValueError("Histórico Gold com data semanal ausente")
                self._validate_week(
                    table.filter(pc.equal(table["week_start"], pa.scalar(start))), start
                )
            return table
        root = gold_weekly.dataset_path(self.settings)
        tables = []
        for start in self.weeks():
            table = _read_features(
                root / f"week_start={start}" / "part-000.parquet", self.settings, grid_id
            )
            self._validate_week(table, start)
            tables.append(table)
        return pa.concat_tables(tables)

    def evolution(self, grid: pa.Table | None, bounds: GeographicBounds | None) -> pa.Table:
        rows = []
        for start in self.weeks():
            table = geographic_subset(self.week(start, grid), bounds)
            summary = summarize(table)
            rows.append(
                {
                    "week_start": start,
                    "score": summary.score,
                    "complete_score": summary.complete_score,
                    "partial_score": summary.partial_score,
                    "coverage_pct": summary.coverage_pct,
                    "complete_coverage_pct": summary.complete_coverage_pct,
                    **summary.factors,
                }
            )
        return pa.Table.from_pylist(rows)

    def fingerprint(self) -> str:
        """Cache identity excludes credentials; DB results also expire by UI TTL."""
        if self.source == "postgres":
            database = self.settings.database
            return f"{database.host}:{database.port}/{database.name}/{database.user}"
        files = [
            gold_weekly.dataset_path(self.settings) / f"week_start={start}" / "part-000.parquet"
            for start in self.weeks()
        ]
        files.append(spatial_grid.dataset_path(self.settings) / "grid.parquet")
        return str([(str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in files])

    def context(self) -> dict[str, Any]:
        import json

        path = gold_weekly.dataset_path(self.settings) / "_metadata.json"
        metadata = (
            json.loads(path.read_text()) if self.source == "parquet" and path.is_file() else {}
        )
        return {
            "method": metadata.get("score_method_version", "academic-index-v1"),
            "parameters": metadata.get("score_parameters", self.settings.gold.model_dump()),
            "classification": metadata.get(
                "risk_classification", asdict(RISK_CLASSIFICATION_POLICY)
            ),
            "processed_at": metadata.get("processed_at_utc"),
            "parameters_origin": "Gold" if metadata else "configuração atual",
        }
