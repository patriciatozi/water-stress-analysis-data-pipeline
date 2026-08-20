from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
from shapely import contains_xy

from water_stress.config import Settings
from water_stress.ingestion import ibge, nasa_power

SOURCE_TO_SILVER = {
    "T2M": ("temperature_mean_c", "C"),
    "T2M_MAX": ("temperature_max_c", "C"),
    "T2M_MIN": ("temperature_min_c", "C"),
    "RH2M": ("relative_humidity_pct", "%"),
    "WS2M": ("wind_speed_ms", "m/s"),
    "ALLSKY_SFC_SW_DWN": ("solar_radiation_mj_m2_day", "MJ/m^2/day"),
    "PRECTOTCORR": ("precipitation_mm_day", "mm/day"),
}
SOLAR_PARAMETER = "ALLSKY_SFC_SW_DWN"


@dataclass(frozen=True)
class WeatherDailyResult:
    dataset_path: Path
    parquet_paths: tuple[Path, ...]
    schema_path: Path
    quality_path: Path
    metadata_path: Path
    row_count: int
    weather_cell_count: int


@dataclass(frozen=True)
class CellSeries:
    elevation_m: float
    values: dict[str, float | None]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key in NASA POWER artifact: {key}")
        result[key] = value
    return result


def _field(name: str, data_type: pa.DataType, unit: str, description: str) -> pa.Field[Any]:
    return pa.field(name, data_type, metadata={"unit": unit, "description": description})


def silver_schema(settings: Settings) -> pa.Schema:
    return pa.schema(
        [
            _field("weather_cell_id", pa.string(), "identifier", "Stable NASA POWER cell key"),
            _field("date", pa.date32(), "date", "Observation date in UTC"),
            _field("latitude", pa.float64(), "degrees_north", "MERRA-2 cell center latitude"),
            _field("longitude", pa.float64(), "degrees_east", "MERRA-2 cell center longitude"),
            _field("elevation_m", pa.float64(), "m", "NASA POWER cell elevation"),
            _field("temperature_mean_c", pa.float64(), "C", "Mean air temperature at 2 m"),
            _field("temperature_max_c", pa.float64(), "C", "Maximum air temperature at 2 m"),
            _field("temperature_min_c", pa.float64(), "C", "Minimum air temperature at 2 m"),
            _field("relative_humidity_pct", pa.float64(), "%", "Relative humidity at 2 m"),
            _field("wind_speed_ms", pa.float64(), "m/s", "Wind speed at 2 m"),
            _field(
                "solar_radiation_mj_m2_day",
                pa.float64(),
                "MJ/m^2/day",
                "Nearest SYN1DEG shortwave radiation",
            ),
            _field("precipitation_mm_day", pa.float64(), "mm/day", "Corrected precipitation"),
            _field(
                "reference_evapotranspiration_mm_day",
                pa.float64(),
                "mm/day",
                "FAO-56 Penman-Monteith grass reference evapotranspiration",
            ),
        ],
        metadata={
            "source": "NASA POWER Daily API",
            "layer": "silver",
            "dataset": "weather_daily",
            "crs": settings.spatial.query_crs,
            "processing_version": settings.project.version,
        },
    )


def _dates(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def weather_cell_id(latitude: float, longitude: float) -> str:
    def token(value: float) -> str:
        return f"{value:.3f}".replace("-", "m").replace(".", "p")

    return f"nasa_power_lat_{token(latitude)}_lon_{token(longitude)}"


def reference_evapotranspiration(
    *,
    observation_date: date,
    latitude: float,
    elevation_m: float,
    temperature_mean_c: float,
    temperature_max_c: float,
    temperature_min_c: float,
    relative_humidity_pct: float,
    wind_speed_ms: float,
    solar_radiation_mj_m2_day: float,
) -> float:
    if temperature_min_c > temperature_max_c:
        raise ValueError("Minimum temperature cannot exceed maximum temperature")
    if not 0 <= relative_humidity_pct <= 100:
        raise ValueError("Relative humidity must be between zero and 100")
    if wind_speed_ms < 0 or solar_radiation_mj_m2_day < 0:
        raise ValueError("Wind speed and solar radiation cannot be negative")

    def saturation_vapor_pressure(temperature: float) -> float:
        return 0.6108 * math.exp(17.27 * temperature / (temperature + 237.3))

    pressure = 101.3 * ((293 - 0.0065 * elevation_m) / 293) ** 5.26
    psychrometric = 0.000665 * pressure
    saturation_mean = (
        saturation_vapor_pressure(temperature_max_c) + saturation_vapor_pressure(temperature_min_c)
    ) / 2
    actual_vapor = relative_humidity_pct / 100 * saturation_vapor_pressure(temperature_mean_c)
    slope = 4098 * saturation_vapor_pressure(temperature_mean_c) / (temperature_mean_c + 237.3) ** 2

    latitude_rad = math.radians(latitude)
    day_of_year = observation_date.timetuple().tm_yday
    inverse_distance = 1 + 0.033 * math.cos(2 * math.pi * day_of_year / 365)
    solar_declination = 0.409 * math.sin(2 * math.pi * day_of_year / 365 - 1.39)
    sunset_angle = math.acos(
        min(1.0, max(-1.0, -math.tan(latitude_rad) * math.tan(solar_declination)))
    )
    extraterrestrial = (
        24
        * 60
        / math.pi
        * 0.0820
        * inverse_distance
        * (
            sunset_angle * math.sin(latitude_rad) * math.sin(solar_declination)
            + math.cos(latitude_rad) * math.cos(solar_declination) * math.sin(sunset_angle)
        )
    )
    clear_sky = max((0.75 + 0.00002 * elevation_m) * extraterrestrial, 1e-9)
    net_shortwave = 0.77 * solar_radiation_mj_m2_day
    radiation_ratio = min(max(solar_radiation_mj_m2_day / clear_sky, 0.3), 1.0)
    cloud_factor = 1.35 * radiation_ratio - 0.35
    net_longwave = (
        4.903e-9
        * (((temperature_max_c + 273.16) ** 4 + (temperature_min_c + 273.16) ** 4) / 2)
        * (0.34 - 0.14 * math.sqrt(max(actual_vapor, 0)))
        * cloud_factor
    )
    net_radiation = net_shortwave - net_longwave
    numerator = 0.408 * slope * net_radiation + psychrometric * 900 / (
        temperature_mean_c + 273
    ) * wind_speed_ms * (saturation_mean - actual_vapor)
    denominator = slope + psychrometric * (1 + 0.34 * wind_speed_ms)
    return float(max(0.0, numerator / denominator))


def source_paths(settings: Settings) -> dict[str, list[Path]]:
    paths_by_parameter: dict[str, list[Path]] = {}
    for parameter in settings.nasa_power.parameters:
        root = nasa_power.regional_artifact_path(settings, parameter, "placeholder").parent.parent
        paths = sorted(root.glob("region_id=*/weather.json"))
        if not paths:
            raise FileNotFoundError(f"No regional NASA POWER artifacts found for {parameter}")
        paths_by_parameter[parameter] = paths
    region_sets = [{path.parent.name for path in paths} for paths in paths_by_parameter.values()]
    if any(regions != region_sets[0] for regions in region_sets[1:]):
        raise ValueError("NASA POWER parameters have inconsistent region sets")
    return paths_by_parameter


def _load_parameter(
    parameter: str, paths: list[Path], expected_unit: str
) -> dict[tuple[float, float], CellSeries]:
    result: dict[tuple[float, float], CellSeries] = {}
    for path in paths:
        document = json.loads(path.read_bytes(), object_pairs_hook=_reject_duplicate_keys)
        unit = document.get("parameters", {}).get(parameter, {}).get("units")
        if unit != expected_unit:
            raise ValueError(
                f"Unexpected unit for {parameter}: expected {expected_unit}, got {unit}"
            )
        fill_value = float(document.get("header", {}).get("fill_value", -999))
        features = document.get("features")
        if not isinstance(features, list):
            raise ValueError(f"NASA POWER artifact has no feature list: {path}")
        for feature in features:
            coordinates = feature.get("geometry", {}).get("coordinates", [])
            raw_values = feature.get("properties", {}).get("parameter", {}).get(parameter)
            if len(coordinates) < 3 or not isinstance(raw_values, dict):
                raise ValueError(f"Invalid NASA POWER feature in {path}")
            key = float(coordinates[1]), float(coordinates[0])
            if key in result:
                raise ValueError(f"Duplicate NASA POWER cell for {parameter}: {key}")
            values = {
                str(key_date): (
                    None if value is None or float(value) == fill_value else float(value)
                )
                for key_date, value in raw_values.items()
            }
            result[key] = CellSeries(float(coordinates[2]), values)
    return result


def build_table(
    settings: Settings,
    *,
    boundary: bytes,
    paths_by_parameter: dict[str, list[Path]],
) -> tuple[pa.Table, float]:
    unsupported = set(paths_by_parameter) - set(SOURCE_TO_SILVER)
    if unsupported:
        raise ValueError(f"Unsupported NASA POWER parameters: {sorted(unsupported)}")
    loaded = {
        parameter: _load_parameter(parameter, paths, SOURCE_TO_SILVER[parameter][1])
        for parameter, paths in paths_by_parameter.items()
    }
    base_parameters = [
        parameter for parameter in settings.nasa_power.parameters if parameter != SOLAR_PARAMETER
    ]
    if not base_parameters or SOLAR_PARAMETER not in loaded:
        raise ValueError("weather_daily requires MERRA-2 parameters and solar radiation")
    base_coordinates = set(loaded[base_parameters[0]])
    for parameter in base_parameters[1:]:
        if set(loaded[parameter]) != base_coordinates:
            raise ValueError(f"NASA POWER grid mismatch for parameter {parameter}")
    geometry = ibge.geometry_from_geojson(boundary)
    coordinates = sorted(
        (latitude, longitude)
        for latitude, longitude in base_coordinates
        if contains_xy(geometry, longitude, latitude)
    )
    solar_coordinates = list(loaded[SOLAR_PARAMETER])
    expected_dates = _dates(settings.study.start_date, settings.study.end_date)
    expected_keys = {value.strftime("%Y%m%d") for value in expected_dates}
    columns: dict[str, list[Any]] = {name: [] for name in silver_schema(settings).names}
    maximum_solar_distance = 0.0
    for latitude, longitude in coordinates:
        solar_coordinate = min(
            solar_coordinates,
            key=lambda value: (value[0] - latitude) ** 2 + (value[1] - longitude) ** 2,
        )
        distance = math.hypot(solar_coordinate[0] - latitude, solar_coordinate[1] - longitude)
        maximum_solar_distance = max(maximum_solar_distance, distance)
        if distance > math.sqrt(0.5**2 + 0.5**2) + 1e-9:
            raise ValueError("No SYN1DEG solar cell close enough to a MERRA-2 cell")
        elevation = loaded[base_parameters[0]][(latitude, longitude)].elevation_m
        for observation_date in expected_dates:
            date_key = observation_date.strftime("%Y%m%d")
            columns["weather_cell_id"].append(weather_cell_id(latitude, longitude))
            columns["date"].append(observation_date)
            columns["latitude"].append(latitude)
            columns["longitude"].append(longitude)
            columns["elevation_m"].append(elevation)
            row_values: dict[str, float | None] = {}
            for parameter in base_parameters:
                series = loaded[parameter][(latitude, longitude)].values
                unexpected = set(series) - expected_keys
                if unexpected:
                    raise ValueError(f"Dates outside study period for {parameter}: {unexpected}")
                row_values[SOURCE_TO_SILVER[parameter][0]] = series.get(date_key)
            solar_values = loaded[SOLAR_PARAMETER][solar_coordinate].values
            row_values[SOURCE_TO_SILVER[SOLAR_PARAMETER][0]] = solar_values.get(date_key)
            for name, value in row_values.items():
                columns[name].append(value)
            eto_inputs = [
                row_values["temperature_mean_c"],
                row_values["temperature_max_c"],
                row_values["temperature_min_c"],
                row_values["relative_humidity_pct"],
                row_values["wind_speed_ms"],
                row_values["solar_radiation_mj_m2_day"],
            ]
            if any(value is None for value in eto_inputs):
                eto: float | None = None
            else:
                complete_inputs = cast(list[float], eto_inputs)
                eto = reference_evapotranspiration(
                    observation_date=observation_date,
                    latitude=latitude,
                    elevation_m=elevation,
                    temperature_mean_c=complete_inputs[0],
                    temperature_max_c=complete_inputs[1],
                    temperature_min_c=complete_inputs[2],
                    relative_humidity_pct=complete_inputs[3],
                    wind_speed_ms=complete_inputs[4],
                    solar_radiation_mj_m2_day=complete_inputs[5],
                )
            columns["reference_evapotranspiration_mm_day"].append(eto)
    table = pa.table(columns, schema=silver_schema(settings))
    keys = list(zip(table["weather_cell_id"].to_pylist(), table["date"].to_pylist(), strict=True))
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate weather_cell_id/date keys in weather_daily")
    return table, maximum_solar_distance


def dataset_path(settings: Settings) -> Path:
    return (
        settings.storage.silver_root_path
        / "weather_daily"
        / settings.study.partition_key
        / f"start_date={settings.study.start_date.isoformat()}"
        / f"end_date={settings.study.end_date.isoformat()}"
    )


def _write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def write_weather_daily(
    settings: Settings,
    table: pa.Table,
    *,
    paths_by_parameter: dict[str, list[Path]],
    maximum_solar_distance_degrees: float,
) -> WeatherDailyResult:
    root = dataset_path(settings)
    parquet_paths: list[Path] = []
    raw_dates = table["date"].to_pylist()
    if any(value is None for value in raw_dates):
        raise ValueError("weather_daily contains a null date")
    dates = cast(list[date], raw_dates)
    for year in sorted({value.year for value in dates}):
        indices = [index for index, value in enumerate(dates) if value.year == year]
        output = root / f"year={year}" / "part-000.parquet"
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".parquet.tmp")
        partition = table.take(pa.array(indices, type=pa.int64()))
        pq.write_table(partition, temporary, compression="zstd")
        temporary.replace(output)
        parquet_paths.append(output)
    manifests = [
        json.loads(path.with_suffix(".manifest.json").read_text())
        for paths in paths_by_parameter.values()
        for path in paths
    ]
    timestamps = sorted(
        value
        for manifest in manifests
        if isinstance(value := manifest.get("downloaded_at_utc"), str)
    )
    missing = {name: table[name].null_count for name in table.column_names}
    cells = set(table["weather_cell_id"].to_pylist())
    numeric_columns = [
        field.name
        for field in table.schema
        if pa.types.is_floating(field.type) and field.name not in {"latitude", "longitude"}
    ]
    ranges = {
        column: {
            "minimum": min(
                (value for value in table[column].to_pylist() if value is not None),
                default=None,
            ),
            "maximum": max(
                (value for value in table[column].to_pylist() if value is not None),
                default=None,
            ),
        }
        for column in numeric_columns
    }
    common = {
        "dataset": "weather_daily",
        "source": "NASA POWER Daily API",
        "source_extraction_timestamp_min": timestamps[0] if timestamps else None,
        "source_extraction_timestamp_max": timestamps[-1] if timestamps else None,
        "crs": settings.spatial.query_crs,
        "native_resolution": {"MERRA2": "0.5 x 0.625 degrees", "SYN1DEG": "1 x 1 degree"},
        "solar_harmonization": "nearest SYN1DEG cell center to each MERRA-2 cell center",
        "maximum_solar_distance_degrees": maximum_solar_distance_degrees,
        "eto_method": "FAO-56 Penman-Monteith, daily grass reference surface",
        "eto_assumptions": [
            "actual vapor pressure estimated from mean relative humidity and mean temperature",
            "atmospheric pressure estimated from NASA POWER elevation",
            "Rs/Rso constrained to the FAO-56 range 0.3 to 1.0",
            "soil heat flux assumed zero for the daily time step",
        ],
        "processing_version": settings.project.version,
        "processed_at_utc": datetime.now(UTC).isoformat(),
        "units": {
            field.name: (field.metadata or {}).get(b"unit", b"").decode() for field in table.schema
        },
    }
    schema_path = root / "_schema.json"
    quality_path = root / "_quality.json"
    metadata_path = root / "_metadata.json"
    _write_json(
        schema_path,
        {
            "dataset": "weather_daily",
            "columns": [
                {
                    "name": field.name,
                    "type": str(field.type),
                    "unit": (field.metadata or {}).get(b"unit", b"").decode(),
                    "description": (field.metadata or {}).get(b"description", b"").decode(),
                }
                for field in table.schema
            ],
        },
    )
    _write_json(
        quality_path,
        {
            **common,
            "row_count": table.num_rows,
            "weather_cell_count": len(cells),
            "duplicate_key_count": 0,
            "missing_by_column": missing,
            "range_by_column": ranges,
        },
    )
    _write_json(metadata_path, common)
    return WeatherDailyResult(
        root,
        tuple(parquet_paths),
        schema_path,
        quality_path,
        metadata_path,
        table.num_rows,
        len(cells),
    )


def transform(settings: Settings) -> WeatherDailyResult:
    boundary_path = ibge.artifact_path(settings)
    if not boundary_path.is_file():
        raise FileNotFoundError(f"IBGE Bronze boundary not found at {boundary_path}")
    paths = source_paths(settings)
    table, maximum_distance = build_table(
        settings, boundary=boundary_path.read_bytes(), paths_by_parameter=paths
    )
    return write_weather_daily(
        settings,
        table,
        paths_by_parameter=paths,
        maximum_solar_distance_degrees=maximum_distance,
    )
