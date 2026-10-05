from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from water_stress.config import Settings
from water_stress.dashboard.analysis import (
    FACTOR_UNITS,
    GeographicBounds,
    geographic_subset,
    risk_keys,
    summarize,
    validate_features,
)
from water_stress.dashboard.data import DashboardReader
from water_stress.transformation import common, gold_weekly, spatial_grid


def feature_table(settings: Settings) -> pa.Table:
    rows = [
        {
            "grid_id": "cell-1",
            "soy_fraction": 0.25,
            "water_stress_score": 0.0,
            "score_status": "complete",
            "water_stress_risk_class": "low",
            "risk_classification_version": "four-level-v1",
            "ndvi_median": 0.7,
            "ndmi_median": 0.2,
        },
        {
            "grid_id": "cell-2",
            "soy_fraction": 0.75,
            "water_stress_score": 1.0,
            "score_status": "partial",
            "water_stress_risk_class": "critical",
            "risk_classification_version": "four-level-v1",
            "ndvi_median": None,
            "ndmi_median": None,
        },
        {
            "grid_id": "cell-3",
            "soy_fraction": 0.5,
            "water_stress_score": None,
            "score_status": "unavailable",
            "water_stress_risk_class": None,
            "risk_classification_version": "four-level-v1",
            "ndvi_median": None,
            "ndmi_median": None,
        },
    ]
    for row in rows:
        row.update(
            week_start=date(2023, 9, 4),
            week_end=date(2023, 9, 10),
            weather_expected_days=7,
            weather_observation_count=7,
            score_component_count=3 if row["score_status"] == "complete" else 1,
            score_available_weight=1 if row["score_status"] == "complete" else 0.5,
            precipitation_mm_7d=10.0,
            eto_mm_7d=20.0,
            water_deficit_mm_7d=10.0,
            temperature_mean_c=25.0,
            consecutive_dry_days=2,
            sand_pct=40.0,
            clay_pct=30.0,
            soc=12.0,
            bulk_density=1.2,
            monitoring_guidance="Orientação acadêmica",
        )
    return pa.Table.from_pylist(rows, schema=gold_weekly.gold_schema(settings))


@pytest.fixture
def dashboard_data(settings: Settings) -> Settings:
    settings = settings.model_copy(
        update={
            "study": settings.study.model_copy(
                update={
                    "start_date": date(2023, 9, 4),
                    "end_date": date(2023, 9, 17),
                }
            )
        }
    )
    grid = pa.table(
        {
            "grid_id": ["cell-1", "cell-2", "cell-3"],
            "centroid_latitude": [-12.0, -13.0, -14.0],
            "centroid_longitude": [-55.0, -56.0, -57.0],
            "area_km2": [1.0, 2.0, 1.0],
        }
    )
    common.write_parquet(spatial_grid.dataset_path(settings) / "grid.parquet", grid)
    table = feature_table(settings)
    for start, end in gold_weekly._study_weeks(settings):
        current = table.set_column(
            table.schema.get_field_index("week_start"),
            "week_start",
            pa.array([start] * 3, type=pa.date32()),
        )
        current = current.set_column(
            current.schema.get_field_index("week_end"),
            "week_end",
            pa.array([end] * 3, type=pa.date32()),
        )
        common.write_parquet(
            gold_weekly.dataset_path(settings) / f"week_start={start}" / "part-000.parquet", current
        )
    common.write_json(
        gold_weekly.dataset_path(settings) / "_metadata.json",
        {
            "score_method_version": "academic-index-v1",
            "score_parameters": {"deficit_weight": 0.5},
            "processed_at_utc": "2026-10-04T00:00:00Z",
        },
    )
    return settings


def test_weighted_score_area_coverage_and_zero(dashboard_data: Settings) -> None:
    reader = DashboardReader(dashboard_data)
    table = reader.week(reader.weeks()[0])
    summary = summarize(table)
    assert summary.soy_area_km2 == 2.25
    assert summary.scored_area_km2 == 1.75
    assert summary.score == pytest.approx(1.5 / 1.75)
    assert summary.coverage_pct == pytest.approx(100 * 1.75 / 2.25)
    assert summary.complete_score == 0
    assert summary.partial_score == 1
    assert summary.high_risk_area_km2 == 1.5
    assert summary.risk_area["missing"] == 0.5
    assert summary.complete_coverage_pct == pytest.approx(100 * 0.25 / 2.25)
    assert summary.factors["ndvi_median"] == 0.7
    assert summary.factor_coverage["ndvi_median"] == pytest.approx(100 * 0.25 / 2.25)
    assert risk_keys(table) == ["low", "critical", "missing"]


def test_geographic_subset_uses_centroids_and_empty_is_not_zero(dashboard_data: Settings) -> None:
    table = DashboardReader(dashboard_data).week(date(2023, 9, 4))
    bounds = GeographicBounds(west=-55.5, east=-54.5, south=-12.5, north=-11.5)
    selected = geographic_subset(table, bounds)
    assert selected["grid_id"].to_pylist() == ["cell-1"]
    assert summarize(selected).score == 0
    assert geographic_subset(table, None).equals(table)
    empty = geographic_subset(table, GeographicBounds(west=0, east=1, south=0, north=1))
    summary = summarize(empty)
    assert summary.score is None
    assert summary.coverage_pct == 0
    assert all(value is None for value in summary.factors.values())
    with pytest.raises(ValueError, match="oeste"):
        GeographicBounds(west=1, east=0, south=0, north=1)


def test_legacy_classification_stays_pending(dashboard_data: Settings) -> None:
    reader = DashboardReader(dashboard_data)
    path = gold_weekly.dataset_path(dashboard_data) / "week_start=2023-09-04" / "part-000.parquet"
    table = (
        pq.ParquetFile(path)
        .read()
        .drop(
            [
                "water_stress_risk_class",
                "risk_classification_version",
                "monitoring_guidance",
            ]
        )
    )
    common.write_parquet(path, table)
    summary = summarize(reader.week(date(2023, 9, 4)))
    assert summary.pending_class_area_km2 == 1.75
    assert summary.high_risk_area_km2 == 0
    assert summary.risk_area["missing"] == 0.5


@pytest.mark.parametrize(
    "column,values",
    [
        ("water_stress_score", [2.0, 1.0, None]),
        ("ndvi_median", [float("nan"), None, None]),
        ("score_status", ["unknown", "partial", "unavailable"]),
        ("water_stress_risk_class", ["incorrect", "critical", None]),
        ("water_stress_score", [None, 1.0, None]),
    ],
)
def test_invalid_features_rejected(settings: Settings, column: str, values: list) -> None:
    table = feature_table(settings)
    table = table.set_column(
        table.schema.get_field_index(column),
        column,
        pa.array(values, type=table.schema.field(column).type),
    )
    with pytest.raises(ValueError):
        validate_features(table)


def test_duplicate_ids_missing_grid_and_invalid_area(dashboard_data: Settings) -> None:
    reader = DashboardReader(dashboard_data)
    table = feature_table(dashboard_data)
    with pytest.raises(ValueError, match="duplicadas"):
        validate_features(pa.concat_tables([table, table.slice(0, 1)]))
    missing = reader.spatial_grid().slice(0, 1)
    with pytest.raises(ValueError, match="centróide"):
        reader.week(date(2023, 9, 4), missing)
    with pytest.raises(ValueError, match="duplicadas"):
        reader.week(date(2023, 9, 4), pa.concat_tables([missing, missing]))
    serving = reader.week(date(2023, 9, 4))
    bad = serving.set_column(
        serving.schema.get_field_index("soy_area_km2"),
        "soy_area_km2",
        pa.array([float("nan"), 1.0, 1.0]),
    )
    with pytest.raises(ValueError, match="Área"):
        summarize(bad)


def test_history_evolution_context_and_cache_invalidation(dashboard_data: Settings) -> None:
    reader = DashboardReader(dashboard_data)
    assert reader.weeks() == (date(2023, 9, 4), date(2023, 9, 11))
    history = reader.history("cell-1")
    assert history.num_rows == 2
    assert history["water_stress_score"].to_pylist() == [0, 0]
    assert reader.history("' OR 1=1 --").num_rows == 0
    evolution = reader.evolution(reader.spatial_grid(), None)
    assert evolution.num_rows == 2
    assert evolution["score"].to_pylist() == [pytest.approx(1.5 / 1.75)] * 2
    assert set(FACTOR_UNITS) <= set(evolution.column_names)
    assert reader.context()["parameters_origin"] == "Gold"
    assert reader.context()["parameters"]["deficit_weight"] == 0.5
    before = reader.fingerprint()
    path = gold_weekly.dataset_path(dashboard_data) / "week_start=2023-09-04" / "part-000.parquet"
    path.touch()
    assert reader.fingerprint() != before
    with pytest.raises(ValueError, match="Semana"):
        reader.week(date(2023, 8, 28))


def test_missing_datasets_and_required_columns_are_actionable(
    settings: Settings, dashboard_data: Settings
) -> None:
    with pytest.raises(FileNotFoundError, match="gold-weekly"):
        DashboardReader(settings).weeks()
    reader = DashboardReader(dashboard_data)
    path = gold_weekly.dataset_path(dashboard_data) / "week_start=2023-09-04" / "part-000.parquet"
    common.write_parquet(path, pq.ParquetFile(path).read().drop(["score_status"]))
    with pytest.raises(ValueError, match="atualizada"):
        reader.week(date(2023, 9, 4))
    (spatial_grid.dataset_path(dashboard_data) / "grid.parquet").unlink()
    with pytest.raises(FileNotFoundError, match="spatial-grid"):
        reader.spatial_grid()


def test_postgres_queries_use_read_only_transactions_and_parameters(
    dashboard_data: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import nullcontext

    from water_stress.dashboard import data

    local = DashboardReader(dashboard_data)
    serving = local.week(date(2023, 9, 4))
    query_log = []

    class Cursor:
        def __init__(self) -> None:
            self.rows: list[dict] = []

        def execute(self, query: str, parameters: tuple = ()) -> None:
            query_log.append((query, parameters))
            if "DISTINCT week_start" in query:
                self.rows = [{"week_start": date(2023, 9, 4)}]
            elif "FROM gold.water_stress_dashboard" in query:
                self.rows = serving.to_pylist()
            elif "FROM silver.dim_spatial_grid" in query:
                self.rows = local.spatial_grid().to_pylist()

        def fetchall(self) -> list[dict]:
            return self.rows

    class Connection:
        def cursor(self, **_kwargs: object) -> object:
            return nullcontext(Cursor())

    monkeypatch.setattr(data, "connect", lambda _: nullcontext(Connection()))
    reader = DashboardReader(dashboard_data, "postgres")
    assert reader.weeks() == (date(2023, 9, 4),)
    assert reader.week(date(2023, 9, 4)).num_rows == 3
    assert reader.spatial_grid().num_rows == 3
    reader.history("' OR 1=1 --")
    assert any(query == "SET TRANSACTION READ ONLY" for query, _ in query_log)
    assert any("statement_timeout" in query for query, _ in query_log)
    assert all("' OR 1=1 --" not in query for query, _ in query_log)
    assert any(parameters and parameters[0] == "' OR 1=1 --" for _, parameters in query_log)
    assert reader.context()["parameters_origin"] == "configuração atual"
    assert "password" not in reader.fingerprint()


@pytest.fixture
def dashboard_app_test(dashboard_data: Settings, monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("streamlit")
    pytest.importorskip("plotly")
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    local = DashboardReader(dashboard_data)

    def query(reader: DashboardReader, sql: str, parameters: tuple = ()) -> list[dict]:
        assert reader.source == "postgres"
        if "DISTINCT week_start" in sql:
            return [{"week_start": start} for start in local.weeks()]
        assert "FROM gold.water_stress_dashboard" in sql
        return local.week(parameters[0]).to_pylist()

    monkeypatch.setattr(DashboardReader, "_query", query)
    monkeypatch.setattr("water_stress.config.load_settings", lambda *_: dashboard_data)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("sys.argv", ["dashboard_app.py"])
    st.cache_data.clear()
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "dashboard_app.py"),
        default_timeout=15,
    )
    return app.run()


def test_week_and_history_reject_wrong_partition_dates(dashboard_data: Settings) -> None:
    path = gold_weekly.dataset_path(dashboard_data) / "week_start=2023-09-04" / "part-000.parquet"
    table = pq.ParquetFile(path).read()
    wrong_dates = table.set_column(
        table.schema.get_field_index("week_start"),
        "week_start",
        pa.array([date(2023, 9, 11)] * table.num_rows, type=pa.date32()),
    )
    common.write_parquet(path, wrong_dates)
    reader = DashboardReader(dashboard_data)
    with pytest.raises(ValueError, match="partição semanal"):
        reader.week(date(2023, 9, 4))
    with pytest.raises(ValueError, match="partição semanal"):
        reader.history("cell-1")


def test_history_rejects_duplicate_cell_weeks(dashboard_data: Settings) -> None:
    path = gold_weekly.dataset_path(dashboard_data) / "week_start=2023-09-04" / "part-000.parquet"
    table = pq.ParquetFile(path).read()
    common.write_parquet(path, pa.concat_tables([table, table.slice(0, 1)]))
    with pytest.raises(ValueError, match="duplicadas"):
        DashboardReader(dashboard_data).history("cell-1")


def test_streamlit_overview_and_map_filters_preserve_denominators(dashboard_app_test) -> None:
    app = dashboard_app_test
    assert not app.exception and not app.error
    assert app.metric[0].value == "85,7"
    assert len(app.metric) == 5  # Three summary metrics plus NDVI and NDMI.
    assert app.metric[1].value == "2 km²"
    assert app.title[0].value == "Estresse hídrico e recomendação de irrigação na soja"
    assert not app.selectbox
    description = next(
        item.proto.body for item in app.get("html") if "O que o score indica" in item.proto.body
    )
    assert "de 0 a 100" in description
    assert "resultados parciais" in description
    assert "evaporação e transpiração" in description
    assert "o vigor e a cobertura da vegetação" in description
    assert "a umidade da vegetação" in description
    assert app.multiselect(key="map_categories").label == "Categorias"
    assert "Sem score" not in app.multiselect(key="map_categories").options
    initial_map = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    assert [row["grid_id"] for row in initial_map["layers"][0]["data"]] == ["cell-1", "cell-2"]
    initial_chart = json.loads(app.get("plotly_chart")[0].proto.spec)["data"][0]
    assert dict(zip(initial_chart["y"], initial_chart["x"], strict=True)) == {
        "Baixo": 0.25,
        "Atenção": 0.0,
        "Alto": 0.0,
        "Crítico": 1.5,
    }
    app.multiselect(key="map_categories").set_value(["low", "complete"]).run()
    assert not app.exception
    assert app.metric[0].value == "85,7"
    spec = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    assert [row["grid_id"] for row in spec["layers"][0]["data"]] == ["cell-1"]
    chart = json.loads(app.get("plotly_chart")[0].proto.spec)["data"][0]
    assert dict(zip(chart["y"], chart["x"], strict=True)) == {
        "Baixo": 0.25,
        "Atenção": 0.0,
        "Alto": 0.0,
        "Crítico": 0.0,
    }
    app.multiselect(key="map_categories").set_value(["partial"]).run()
    spec = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    assert [row["grid_id"] for row in spec["layers"][0]["data"]] == ["cell-2"]
    chart = json.loads(app.get("plotly_chart")[0].proto.spec)["data"][0]
    assert dict(zip(chart["y"], chart["x"], strict=True)) == {
        "Baixo": 0.0,
        "Atenção": 0.0,
        "Alto": 0.0,
        "Crítico": 1.5,
    }
    app.multiselect(key="map_categories").set_value(["critical", "complete"]).run()
    assert not app.exception
    assert not app.get("deck_gl_json_chart")
    chart = json.loads(app.get("plotly_chart")[0].proto.spec)["data"][0]
    assert sum(chart["x"]) == 0
    app.multiselect(key="map_categories").set_value([]).run()
    assert not app.exception
    assert any("Nenhuma célula nas categorias" in item.value for item in app.info)
    assert app.metric[0].value == "85,7"


def test_streamlit_navigation_and_metadata(dashboard_app_test) -> None:
    app = dashboard_app_test
    assert app.radio(key="section").options == ["Visão geral", "Sobre o indicador"]
    app.radio(key="section").set_value("Sobre o indicador").run()
    assert not app.exception
    assert len(app.dataframe) == 1


def test_streamlit_empty_gold_has_actionable_message(
    dashboard_app_test, dashboard_data: Settings
) -> None:
    app = dashboard_app_test
    root = gold_weekly.dataset_path(dashboard_data)
    for start, _ in gold_weekly._study_weeks(dashboard_data):
        path = root / f"week_start={start}" / "part-000.parquet"
        common.write_parquet(path, pq.ParquetFile(path).read().slice(0, 0))
    next(button for button in app.button if button.label == "Atualizar leitura").click().run()
    assert not app.exception
    assert any("Nenhuma célula disponível" in item.value for item in app.info)


def test_streamlit_map_always_uses_carto_background(dashboard_app_test) -> None:
    app = dashboard_app_test
    assert not app.exception
    assert not any(widget.label == "Mapa de fundo" for widget in app.checkbox)
    spec = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    assert spec["mapStyle"] == "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"
    assert spec["mapProvider"] == "carto"


def test_streamlit_pending_classification_is_not_zero_risk(
    dashboard_app_test, dashboard_data: Settings
) -> None:
    root = gold_weekly.dataset_path(dashboard_data)
    for start, _ in gold_weekly._study_weeks(dashboard_data):
        path = root / f"week_start={start}" / "part-000.parquet"
        table = (
            pq.ParquetFile(path)
            .read()
            .drop(["water_stress_risk_class", "risk_classification_version", "monitoring_guidance"])
        )
        common.write_parquet(path, table)
    app = dashboard_app_test
    next(button for button in app.button if button.label == "Atualizar leitura").click().run()
    assert not app.exception
    assert app.metric[1].value == "-"
    assert "Classificação pendente" not in app.multiselect(key="map_categories").options
    assert "Sem score" not in app.multiselect(key="map_categories").options
    assert not app.get("deck_gl_json_chart")
    plot = json.loads(app.get("plotly_chart")[0].proto.spec)
    assert "Classificação pendente" not in plot["data"][0]["y"]
    assert sum(plot["data"][0]["x"]) == 0
    assert not any("metadados de consumo pendentes" in item.value for item in app.info)


def test_streamlit_connection_errors_do_not_expose_credentials(
    dashboard_app_test, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failure(*_args, **_kwargs):
        raise ValueError("password=private-test-secret")

    monkeypatch.setattr(DashboardReader, "_query", failure)
    app = dashboard_app_test
    app.run()
    assert not app.exception
    assert len(app.error) == 1
    assert "private-test-secret" not in app.error[0].value
    assert "PostgreSQL" in app.error[0].value
    assert not app.metric
