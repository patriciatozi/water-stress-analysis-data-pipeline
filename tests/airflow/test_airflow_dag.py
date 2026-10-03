"""Run explicitly inside the Airflow environment; default tests need no Airflow install."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

pytest.importorskip("airflow.sdk")

NAMESPACE = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "dags/water_stress_pipeline.py")
)
DAG = NAMESPACE["dag"]


def test_dag_is_manual_and_serial() -> None:
    assert DAG.schedule is None
    assert DAG.catchup is False
    assert DAG.max_active_runs == 1
    assert DAG.max_active_tasks == 1
    assert DAG.params["mode"] == "gold-only"
    assert DAG.params["load_database"] is False


def test_full_pipeline_obeys_source_dependencies() -> None:
    assert {"transform_grid", "ingest_crop"} <= DAG.get_task("transform_crop").upstream_task_ids
    assert {"transform_grid", "ingest_soil"} <= DAG.get_task("transform_soil").upstream_task_ids
    assert "ingest_weather" in DAG.get_task("transform_weather").upstream_task_ids
    assert {"transform_crop", "ingest_satellite"} <= DAG.get_task(
        "transform_satellite"
    ).upstream_task_ids
    assert {"transform_crop", "transform_soil", "transform_weather", "transform_satellite"} <= (
        DAG.get_task("transform_gold").upstream_task_ids
    )


def test_publication_is_blocked_by_quality_failure() -> None:
    assert DAG.get_task("validate_gold").upstream_task_ids == {"transform_gold"}
    assert DAG.get_task("select_publication").upstream_task_ids == {"validate_gold"}
    assert DAG.get_task("migrate_database").upstream_task_ids == {"select_publication"}
    assert DAG.get_task("load_database").upstream_task_ids == {"register_bronze"}
    assert DAG.get_task("register_bronze").upstream_task_ids == {"migrate_database"}


@pytest.mark.parametrize(
    "mode,entry",
    [
        ("full", "ingest_ibge"),
        ("satellite-gold", "transform_satellite"),
        ("gold-only", "transform_gold"),
    ],
)
def test_modes_select_existing_entries(
    monkeypatch: pytest.MonkeyPatch, mode: str, entry: str
) -> None:
    callback = NAMESPACE["select_mode"]
    monkeypatch.setitem(
        callback.__globals__, "get_current_context", lambda: {"params": {"mode": mode}}
    )
    assert callback() == entry
    assert entry in DAG.get_task("select_mode").downstream_task_ids


@pytest.mark.parametrize("enabled,entry", [(False, "files_complete"), (True, "migrate_database")])
def test_database_is_optional(monkeypatch: pytest.MonkeyPatch, enabled: bool, entry: str) -> None:
    callback = NAMESPACE["select_publication"]
    monkeypatch.setitem(
        callback.__globals__, "get_current_context", lambda: {"params": {"load_database": enabled}}
    )
    assert callback() == entry


def test_commands_do_not_interpolate_run_configuration() -> None:
    for task in DAG.tasks:
        assert task.retries == 0
        if hasattr(task, "bash_command"):
            assert "{{" not in task.bash_command
            assert task.do_xcom_push is False
            assert task.skip_on_exit_code == []
            assert task.cwd == "/opt/project"


def test_dag_can_be_serialized() -> None:
    from airflow.serialization.serialized_objects import DagSerialization

    serialized = DagSerialization.to_dict(DAG)
    assert serialized["dag"]["dag_id"] == "water_stress_pipeline"
