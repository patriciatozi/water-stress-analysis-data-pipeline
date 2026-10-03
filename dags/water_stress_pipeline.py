"""Local orchestration only; each task runs the project's existing pipeline CLI."""

from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow.providers.standard.operators.bash import BashOperator
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.providers.standard.operators.python import BranchPythonOperator
from airflow.sdk import DAG, Param, get_current_context

PIPELINE_PYTHON = "/opt/pipeline-venv/bin/python"
PROJECT_ROOT = "/opt/project"
CONFIG = "configs/project.yml"


def select_mode() -> str:
    mode = get_current_context()["params"]["mode"]
    return {
        "full": "ingest_ibge",
        "satellite-gold": "transform_satellite",
        "gold-only": "transform_gold",
    }[mode]


def select_publication() -> str:
    return (
        "migrate_database" if get_current_context()["params"]["load_database"] else "files_complete"
    )


def command_task(task_id: str, module: str, arguments: str, **kwargs: object) -> BashOperator:
    return BashOperator(
        task_id=task_id,
        bash_command=f"exec {PIPELINE_PYTHON} -m water_stress.pipelines.{module} "
        f"--config {CONFIG} {arguments}",
        cwd=PROJECT_ROOT,
        do_xcom_push=False,
        skip_on_exit_code=None,
        **kwargs,
    )


with DAG(
    dag_id="water_stress_pipeline",
    description="Mato Grosso: Bronze, Silver, Gold and optional PostgreSQL serving",
    start_date=pendulum.datetime(2024, 5, 1, tz="America/Sao_Paulo"),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=True,
    default_args={"retries": 0, "execution_timeout": timedelta(hours=24)},
    params={
        "mode": Param("gold-only", type="string", enum=["full", "satellite-gold", "gold-only"]),
        "load_database": Param(False, type="boolean"),
    },
    tags=["water-stress", "historical", "local"],
) as dag:
    choose_mode = BranchPythonOperator(task_id="select_mode", python_callable=select_mode)
    ingest_ibge = command_task("ingest_ibge", "run_ingestion", "--source ibge")
    ingest_weather = command_task("ingest_weather", "run_ingestion", "--source nasa-power")
    ingest_soil = command_task("ingest_soil", "run_ingestion", "--source soilgrids")
    ingest_crop = command_task("ingest_crop", "run_ingestion", "--source mapbiomas")
    ingest_satellite = command_task("ingest_satellite", "run_ingestion", "--source sentinel-2")
    grid = command_task("transform_grid", "run_transformation", "--source spatial-grid")
    crop = command_task("transform_crop", "run_transformation", "--source crop-mask")
    soil = command_task("transform_soil", "run_transformation", "--source soil-features")
    weather = command_task("transform_weather", "run_transformation", "--source weather-daily")
    satellite = command_task(
        "transform_satellite",
        "run_transformation",
        "--source satellite-observation",
        trigger_rule="none_failed_min_one_success",
    )
    gold = command_task(
        "transform_gold",
        "run_transformation",
        "--source gold-weekly",
        trigger_rule="none_failed_min_one_success",
    )
    quality = command_task("validate_gold", "run_quality", "")
    choose_publication = BranchPythonOperator(
        task_id="select_publication",
        python_callable=select_publication,
    )
    migrate = command_task("migrate_database", "run_database", "--migrate")
    register = command_task("register_bronze", "run_database", "--register-bronze")
    load = command_task("load_database", "run_database", "--load --dataset all")
    files_complete = EmptyOperator(task_id="files_complete")

    choose_mode >> [ingest_ibge, satellite, gold]
    ingest_ibge >> [ingest_weather, ingest_soil, ingest_crop, ingest_satellite, grid]
    [grid, ingest_crop] >> crop
    [grid, ingest_soil] >> soil
    ingest_weather >> weather
    [ingest_satellite, crop] >> satellite
    [crop, soil, weather, satellite] >> gold
    gold >> quality >> choose_publication
    choose_publication >> [migrate, files_complete]
    migrate >> register >> load
