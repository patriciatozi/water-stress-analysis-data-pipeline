from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from water_stress.config import Settings, WaterBalanceSettings
from water_stress.database.loader import source_paths
from water_stress.pipelines.run_quality import validate_quality_report
from water_stress.transformation import common, gold_weekly, weather_daily
from water_stress.transformation import gold_water_balance as model


@pytest.fixture
def local_inputs(settings: Settings) -> Settings:
    settings = settings.model_copy(
        update={
            "study": settings.study.model_copy(
                update={
                    "start_date": date(2023, 10, 14),
                    "end_date": date(2023, 11, 5),
                }
            ),
            "water_balance": WaterBalanceSettings(stage_days=(2, 2, 10, 4), block_size=1),
        }
    )
    for start, end in gold_weekly._study_weeks(settings):
        rows = [
            {
                "grid_id": identifier,
                "soy_fraction": 0.5,
                "weather_cell_id": "weather-1",
                "sand_pct": 40.0,
                "clay_pct": 30.0,
                "soc": 12.0,
                "week_start": start,
                "week_end": end,
                "ndvi_median": 0.7 if identifier == "cell-1" else None,
                "ndmi_median": 0.2 if identifier == "cell-1" else None,
                "satellite_age_days": 5 if identifier == "cell-1" else None,
                "water_stress_score": 0.5,
                "score_status": "partial",
            }
            for identifier in ("cell-1", "cell-2")
        ]
        common.write_parquet(
            gold_weekly.dataset_path(settings) / f"week_start={start}" / "part-000.parquet",
            pa.Table.from_pylist(rows, schema=gold_weekly.gold_schema(settings)),
        )
    weather = pa.table(
        {
            "weather_cell_id": ["weather-1"] * 23,
            "date": [settings.study.start_date + timedelta(days=i) for i in range(23)],
            "precipitation_mm_day": [0.0] * 23,
            "reference_evapotranspiration_mm_day": [5.0] * 23,
        }
    )
    common.write_parquet(
        weather_daily.dataset_path(settings) / "year=2023" / "part-000.parquet",
        weather,
    )
    return settings


def _rows(settings: Settings, max_cells: int | None = None) -> list[dict]:
    return [
        row
        for path in source_paths(settings, "water_stress_weekly_v2", max_cells)
        for row in pq.ParquetFile(path).read().to_pylist()
    ]


def test_scenarios_partial_weeks_continuity_and_immutable_v1(local_inputs: Settings) -> None:
    before = {
        path: path.read_bytes()
        for path in gold_weekly.dataset_path(local_inputs).rglob("*.parquet")
    }
    result = model.transform(local_inputs)
    assert result.cell_count == 2
    assert result.row_count == 24
    rows = _rows(local_inputs)
    assert {row["scenario_id"] for row in rows} == {"initial-0.25", "initial-0.5", "initial-0.75"}
    first = next(
        row for row in rows if row["grid_id"] == "cell-1" and row["scenario_id"] == "initial-0.25"
    )
    assert first["active_day_count"] == first["valid_day_count"] == 1
    assert first["water_stress_mean"] == 0.5
    assert first["water_stress_score"] == 0.25
    assert first["water_stress_score_v1"] == 0.5
    assert first["water_stress_risk_class"] == "low"
    assert first["score_status"] == "complete"
    active = sorted(
        (
            row
            for row in rows
            if row["grid_id"] == "cell-1"
            and row["scenario_id"] == "initial-0.25"
            and row["active_day_count"]
        ),
        key=lambda row: row["week_start"],
    )
    assert active[1]["water_start_mm"] == active[0]["water_end_mm"]
    assert active[2]["ongoing_stress_run_days"] > 7
    assert active[2]["maximum_stress_run_days"] == 7
    assert active[-1]["active_day_count"] == active[-1]["valid_day_count"] == 3
    inactive = [row for row in rows if row["active_day_count"] == 0]
    assert all(
        row["score_status"] == "not_applicable" and row["water_stress_score"] is None
        for row in inactive
    )
    assert all(
        row["score_status"] == "partial"
        for row in rows
        if row["grid_id"] == "cell-2" and row["active_day_count"]
    )
    assert all(path.read_bytes() == content for path, content in before.items())
    assert validate_quality_report(result.quality_path).quality_status == "warning"
    assert len(source_paths(local_inputs, "soil_hydraulics")) == 2


def test_restart_reuses_checkpoints_and_repairs_downstream(local_inputs: Settings) -> None:
    result = model.transform(local_inputs)
    paths = source_paths(local_inputs, "water_stress_weekly_v2")
    expected = {path: path.read_bytes() for path in paths}
    times = {path: path.stat().st_mtime_ns for path in paths}
    model.transform(local_inputs)
    assert all(path.stat().st_mtime_ns == times[path] for path in paths)
    damaged = paths[1]
    damaged.write_bytes(b"interrupted")
    with pytest.raises(ValueError, match="corrupted"):
        source_paths(local_inputs, "water_stress_weekly_v2")
    model.transform(local_inputs)
    assert all(path.read_bytes() == content for path, content in expected.items())
    assert paths[0].stat().st_mtime_ns == times[paths[0]]
    assert paths[2].stat().st_mtime_ns != times[paths[2]]
    damaged.with_name("_checkpoint.json").write_text("{")
    model.transform(local_inputs)
    assert all(path.read_bytes() == content for path, content in expected.items())
    assert result.metadata_path.is_file()
    soil_path = source_paths(local_inputs, "soil_hydraulics")[0]
    soil_expected = soil_path.read_bytes()
    soil_path.write_bytes(b"interrupted")
    model.transform(local_inputs)
    assert soil_path.read_bytes() == soil_expected


def test_parameter_changes_pilot_isolation_and_manifest_selection(local_inputs: Settings) -> None:
    full = model.transform(local_inputs)
    pilot = model.transform(local_inputs, max_cells=1)
    assert pilot.analysis_id != full.analysis_id
    assert pilot.cell_count == 1 and pilot.row_count == 12
    assert model.dataset_path(local_inputs) != model.dataset_path(local_inputs, 1)
    changed = local_inputs.model_copy(
        update={
            "water_balance": local_inputs.water_balance.model_copy(
                update={"initial_water_fractions": (0.5,)}
            )
        }
    )
    with pytest.raises(ValueError, match="parameters"):
        source_paths(changed, "soil_hydraulics")
    updated = model.transform(changed)
    assert updated.analysis_id == full.analysis_id
    assert updated.row_count == 8
    assert len(source_paths(changed, "water_stress_weekly_v2")) == 8
    assert len(list(full.dataset_path.rglob("part-000.parquet"))) > 8
    assert all(row["scenario_id"] == "initial-0.5" for row in _rows(changed))


def test_weather_gap_invalidates_state_without_reset(local_inputs: Settings) -> None:
    path = weather_daily.dataset_path(local_inputs) / "year=2023" / "part-000.parquet"
    table = pq.ParquetFile(path).read()
    values = table["precipitation_mm_day"].to_pylist()
    values[3] = None
    common.write_parquet(path, table.set_column(2, "precipitation_mm_day", pa.array(values)))
    model.transform(local_inputs)
    rows = _rows(local_inputs)
    affected = [
        row for row in rows if row["week_start"] >= date(2023, 10, 16) and row["active_day_count"]
    ]
    assert all(
        row["score_issue"] == "weather_gap" and row["water_stress_score"] is None
        for row in affected
    )
    assert all(row["water_end_mm"] is None for row in affected)


def test_invalid_soil_and_stale_satellite_are_explicit(local_inputs: Settings) -> None:
    for path in gold_weekly.dataset_path(local_inputs).rglob("*.parquet"):
        table = pq.ParquetFile(path).read()
        table = table.set_column(
            table.schema.get_field_index("sand_pct"), "sand_pct", pa.array([90.0, 40.0])
        )
        table = table.set_column(
            table.schema.get_field_index("satellite_age_days"),
            "satellite_age_days",
            pa.array([50, 50], type=pa.int16()),
        )
        common.write_parquet(path, table)
    model.transform(local_inputs)
    rows = _rows(local_inputs)
    assert all(row["ndvi_median"] is None for row in rows)
    assert all(
        row["score_issue"] == "invalid_soil"
        for row in rows
        if row["grid_id"] == "cell-1" and row["active_day_count"]
    )
    assert all(
        row["score_status"] == "partial"
        for row in rows
        if row["grid_id"] == "cell-2" and row["active_day_count"]
    )


@pytest.mark.parametrize("change", ["duplicate", "order", "static", "weather_id", "crop"])
def test_gold_contract_rejects_invalid_input(local_inputs: Settings, change: str) -> None:
    paths = sorted(gold_weekly.dataset_path(local_inputs).rglob("*.parquet"))
    path = paths[1]
    table = pq.ParquetFile(path).read()
    if change == "duplicate":
        table = pa.concat_tables([table.slice(0, 1), table.slice(0, 1)])
    elif change == "order":
        table = table.take(pa.array([1, 0]))
    else:
        name, values = {
            "static": ("sand_pct", [41.0, 40.0]),
            "weather_id": ("weather_cell_id", [None, "weather-1"]),
            "crop": ("soy_fraction", [0.1, 0.1]),
        }[change]
        if change == "crop":
            paths = sorted(gold_weekly.dataset_path(local_inputs).rglob("*.parquet"))
        else:
            paths = [path]
        for target in paths:
            original = pq.ParquetFile(target).read()
            common.write_parquet(
                target,
                original.set_column(original.schema.get_field_index(name), name, pa.array(values)),
            )
    if change in ("duplicate", "order"):
        common.write_parquet(path, table)
    with pytest.raises(ValueError):
        model.transform(local_inputs)


@pytest.mark.parametrize("problem", ["duplicate", "negative", "missing"])
def test_weather_boundary_validation(local_inputs: Settings, problem: str) -> None:
    path = weather_daily.dataset_path(local_inputs) / "year=2023" / "part-000.parquet"
    table = pq.ParquetFile(path).read()
    if problem == "missing":
        path.unlink()
        with pytest.raises(FileNotFoundError):
            model.transform(local_inputs)
        return
    if problem == "duplicate":
        table = pa.concat_tables([table, table.slice(0, 1)])
    else:
        table = table.set_column(2, "precipitation_mm_day", pa.array([-1.0] * 23))
    common.write_parquet(path, table)
    with pytest.raises(ValueError):
        model.transform(local_inputs)


def test_missing_publication_invalid_limit_and_planting_coverage(local_inputs: Settings) -> None:
    with pytest.raises(FileNotFoundError, match="Generate"):
        source_paths(local_inputs, "soil_hydraulics")
    with pytest.raises(ValueError, match="positive"):
        model.transform(local_inputs, max_cells=0)
    changed = local_inputs.model_copy(
        update={
            "water_balance": local_inputs.water_balance.model_copy(
                update={"planting_date": date(2023, 10, 1)}
            )
        }
    )
    with pytest.raises(ValueError, match="planting"):
        model.transform(changed)


def test_gold_schema_boundary_requires_valid_column_types(local_inputs: Settings) -> None:
    path = next(gold_weekly.dataset_path(local_inputs).rglob("*.parquet"))
    table = pq.ParquetFile(path).read()
    common.write_parquet(
        path,
        table.set_column(
            table.schema.get_field_index("sand_pct"), "sand_pct", pa.array(["40", "40"])
        ),
    )
    with pytest.raises(ValueError, match="columns/types"):
        model.transform(local_inputs)


def test_manifest_rejects_traversal_and_inconsistent_publication(local_inputs: Settings) -> None:
    result = model.transform(local_inputs)
    manifest = json.loads(result.metadata_path.read_text())
    manifest["files"]["soil_hydraulics"][0]["path"] = "../escape.parquet"
    common.write_json(result.metadata_path, manifest)
    with pytest.raises(ValueError, match="within"):
        source_paths(local_inputs, "soil_hydraulics")
    model.transform(local_inputs)
    report = json.loads(result.quality_path.read_text())
    report["model_signature"] = "outdated"
    common.write_json(result.quality_path, report)
    with pytest.raises(ValueError, match="disagree"):
        source_paths(local_inputs, "soil_hydraulics")


def test_all_active_scores_unavailable_blocks_database_publication(local_inputs: Settings) -> None:
    for path in gold_weekly.dataset_path(local_inputs).rglob("*.parquet"):
        table = pq.ParquetFile(path).read()
        common.write_parquet(
            path,
            table.set_column(
                table.schema.get_field_index("soc"),
                "soc",
                pa.array([None, None], type=pa.float64()),
            ),
        )
    result = model.transform(local_inputs)
    with pytest.raises(ValueError, match="blocked"):
        validate_quality_report(result.quality_path)


def test_cli_pilot_transformation_quality_and_database_scope(
    local_inputs: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from contextlib import nullcontext

    from water_stress.pipelines import run_database, run_quality, run_transformation

    monkeypatch.setattr(run_transformation, "load_settings", lambda _: local_inputs)
    assert run_transformation.main(["--source", "gold-water-balance", "--max-cells", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["cell_count"] == 1
    with pytest.raises(ValueError, match="only"):
        run_transformation.main(["--source", "gold-weekly", "--max-cells", "1"])
    monkeypatch.setattr(run_quality, "load_settings", lambda _: local_inputs)
    monkeypatch.setattr(
        "sys.argv", ["run_quality", "--dataset", "water_stress_weekly_v2", "--max-cells", "1"]
    )
    assert run_quality.main() == 0
    assert json.loads(capsys.readouterr().out)["row_count"] == 12
    monkeypatch.setattr(run_database, "load_settings", lambda _: local_inputs)
    monkeypatch.setattr(run_database, "connect", lambda _: nullcontext(object()))
    calls = []

    def load(_connection: object, dataset: str, paths: tuple[Path, ...], **_: object) -> int:
        calls.append(dataset)
        return sum(pq.ParquetFile(path).metadata.num_rows for path in paths)

    monkeypatch.setattr(run_database, "load_dataset_files", load)
    assert (
        run_database.main(
            [
                "--load",
                "--dataset",
                "soil_hydraulics",
                "--dataset",
                "water_stress_weekly_v2",
                "--max-cells",
                "1",
            ]
        )
        == 0
    )
    assert calls == ["soil_hydraulics", "water_stress_weekly_v2"]
    assert json.loads(capsys.readouterr().out)["loaded"] == {
        "soil_hydraulics": 1,
        "water_stress_weekly_v2": 12,
    }
    with pytest.raises(ValueError, match="only"):
        run_database.main(["--load", "--dataset", "all", "--max-cells", "1"])


def test_database_all_remains_compatible_without_v2(
    local_inputs: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from contextlib import nullcontext

    from water_stress.pipelines import run_database

    monkeypatch.setattr(run_database, "load_settings", lambda _: local_inputs)
    monkeypatch.setattr(run_database, "connect", lambda _: nullcontext(object()))
    monkeypatch.setattr(run_database, "source_paths", lambda *_: ())
    monkeypatch.setattr(run_database, "load_dataset_files", lambda *_args, **_kwargs: 0)
    assert run_database.main(["--load", "--dataset", "all"]) == 0
    loaded = json.loads(capsys.readouterr().out)["loaded"]
    assert "water_stress_weekly" in loaded
    assert "water_stress_weekly_v2" not in loaded
