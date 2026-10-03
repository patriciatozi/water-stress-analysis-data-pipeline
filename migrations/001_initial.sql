CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;
CREATE SCHEMA IF NOT EXISTS control;

CREATE TABLE IF NOT EXISTS bronze.artifact_manifest (
    artifact_id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    artifact_uri TEXT NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    source_url TEXT,
    extracted_at_utc TIMESTAMPTZ,
    status_http SMALLINT,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    sha256 CHAR(64) NOT NULL,
    period_start DATE,
    period_end DATE,
    area_type TEXT,
    area_code TEXT,
    processing_version TEXT NOT NULL,
    config_hash CHAR(64),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    registered_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (request_fingerprint, sha256)
);

CREATE TABLE IF NOT EXISTS control.schema_migration (
    version TEXT PRIMARY KEY,
    applied_at_utc TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control.load_run (
    run_id UUID PRIMARY KEY,
    dataset TEXT NOT NULL,
    source_checksum CHAR(64),
    config_hash CHAR(64),
    started_at_utc TIMESTAMPTZ NOT NULL,
    finished_at_utc TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    row_count BIGINT CHECK (row_count >= 0),
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS control.dataset_load (
    dataset_load_id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES control.load_run (run_id),
    dataset TEXT NOT NULL,
    source_uri TEXT,
    processing_version TEXT NOT NULL,
    config_hash CHAR(64),
    row_count BIGINT NOT NULL CHECK (row_count >= 0),
    loaded_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS silver.dim_spatial_grid (
    grid_id TEXT PRIMARY KEY,
    geometry geometry(Polygon, 5880) NOT NULL,
    centroid_latitude DOUBLE PRECISION NOT NULL,
    centroid_longitude DOUBLE PRECISION NOT NULL,
    area_km2 DOUBLE PRECISION NOT NULL CHECK (area_km2 > 0),
    processing_version TEXT NOT NULL,
    loaded_at_utc TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dim_spatial_grid_geometry
    ON silver.dim_spatial_grid USING GIST (geometry);

CREATE TABLE IF NOT EXISTS silver.crop_mask (
    grid_id TEXT NOT NULL REFERENCES silver.dim_spatial_grid (grid_id),
    year SMALLINT NOT NULL,
    soy_fraction DOUBLE PRECISION CHECK (soy_fraction IS NULL OR (soy_fraction >= 0 AND soy_fraction <= 1)),
    processing_version TEXT NOT NULL,
    loaded_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (grid_id, year)
);

CREATE TABLE IF NOT EXISTS silver.soil_features (
    grid_id TEXT PRIMARY KEY REFERENCES silver.dim_spatial_grid (grid_id),
    clay_pct DOUBLE PRECISION,
    sand_pct DOUBLE PRECISION,
    soc DOUBLE PRECISION,
    bulk_density DOUBLE PRECISION,
    processing_version TEXT NOT NULL,
    loaded_at_utc TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS silver.weather_daily (
    weather_cell_id TEXT NOT NULL,
    date DATE NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    elevation_m DOUBLE PRECISION,
    temperature_mean_c DOUBLE PRECISION,
    temperature_max_c DOUBLE PRECISION,
    temperature_min_c DOUBLE PRECISION,
    relative_humidity_pct DOUBLE PRECISION CHECK (relative_humidity_pct IS NULL OR relative_humidity_pct BETWEEN 0 AND 100),
    wind_speed_ms DOUBLE PRECISION CHECK (wind_speed_ms IS NULL OR wind_speed_ms >= 0),
    solar_radiation_mj_m2_day DOUBLE PRECISION CHECK (solar_radiation_mj_m2_day IS NULL OR solar_radiation_mj_m2_day >= 0),
    precipitation_mm_day DOUBLE PRECISION CHECK (precipitation_mm_day IS NULL OR precipitation_mm_day >= 0),
    reference_evapotranspiration_mm_day DOUBLE PRECISION CHECK (reference_evapotranspiration_mm_day IS NULL OR reference_evapotranspiration_mm_day >= 0),
    geometry geometry(Point, 4326) NOT NULL,
    processing_version TEXT NOT NULL,
    loaded_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (weather_cell_id, date)
);

CREATE INDEX IF NOT EXISTS idx_weather_daily_date
    ON silver.weather_daily (date);

CREATE INDEX IF NOT EXISTS idx_weather_daily_geometry
    ON silver.weather_daily USING GIST (geometry);

CREATE TABLE IF NOT EXISTS silver.satellite_observation (
    grid_id TEXT NOT NULL REFERENCES silver.dim_spatial_grid (grid_id),
    date DATE NOT NULL,
    tile_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    ndvi_mean DOUBLE PRECISION,
    ndvi_p10 DOUBLE PRECISION,
    ndvi_p50 DOUBLE PRECISION,
    ndvi_p90 DOUBLE PRECISION,
    ndmi_mean DOUBLE PRECISION,
    ndmi_p10 DOUBLE PRECISION,
    ndmi_p50 DOUBLE PRECISION,
    ndmi_p90 DOUBLE PRECISION,
    soy_pixel_count BIGINT CHECK (soy_pixel_count IS NULL OR soy_pixel_count >= 0),
    valid_pixel_count BIGINT CHECK (valid_pixel_count IS NULL OR valid_pixel_count >= 0),
    valid_pixel_pct DOUBLE PRECISION CHECK (valid_pixel_pct IS NULL OR valid_pixel_pct BETWEEN 0 AND 100),
    cloud_pixel_pct DOUBLE PRECISION CHECK (cloud_pixel_pct IS NULL OR cloud_pixel_pct BETWEEN 0 AND 100),
    processing_version TEXT NOT NULL,
    loaded_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (grid_id, date, tile_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_satellite_observation_date
    ON silver.satellite_observation (date);

CREATE TABLE IF NOT EXISTS gold.water_stress_weekly (
    grid_id TEXT NOT NULL REFERENCES silver.dim_spatial_grid (grid_id),
    week_start DATE NOT NULL,
    week_end DATE NOT NULL,
    soy_fraction DOUBLE PRECISION CHECK (soy_fraction IS NULL OR (soy_fraction >= 0 AND soy_fraction <= 1)),
    clay_pct DOUBLE PRECISION,
    sand_pct DOUBLE PRECISION,
    soc DOUBLE PRECISION,
    bulk_density DOUBLE PRECISION,
    precipitation_mm_7d DOUBLE PRECISION,
    eto_mm_7d DOUBLE PRECISION,
    water_balance_mm_7d DOUBLE PRECISION,
    water_deficit_mm_7d DOUBLE PRECISION CHECK (water_deficit_mm_7d IS NULL OR water_deficit_mm_7d >= 0),
    rainy_day_count SMALLINT CHECK (rainy_day_count IS NULL OR rainy_day_count >= 0),
    consecutive_dry_days SMALLINT CHECK (consecutive_dry_days IS NULL OR consecutive_dry_days >= 0),
    temperature_mean_c DOUBLE PRECISION,
    temperature_max_c DOUBLE PRECISION,
    temperature_min_c DOUBLE PRECISION,
    weather_observation_count SMALLINT CHECK (weather_observation_count IS NULL OR weather_observation_count >= 0),
    ndvi_median DOUBLE PRECISION,
    ndmi_median DOUBLE PRECISION,
    satellite_scene_count SMALLINT CHECK (satellite_scene_count IS NULL OR satellite_scene_count >= 0),
    satellite_valid_pixel_pct DOUBLE PRECISION CHECK (satellite_valid_pixel_pct IS NULL OR satellite_valid_pixel_pct BETWEEN 0 AND 100),
    satellite_cloud_pixel_pct DOUBLE PRECISION CHECK (satellite_cloud_pixel_pct IS NULL OR satellite_cloud_pixel_pct BETWEEN 0 AND 100),
    satellite_observation_date DATE,
    satellite_age_days SMALLINT CHECK (satellite_age_days IS NULL OR satellite_age_days >= 0),
    water_stress_score DOUBLE PRECISION CHECK (water_stress_score IS NULL OR water_stress_score BETWEEN 0 AND 1),
    water_stress_class TEXT CHECK (water_stress_class IS NULL OR water_stress_class IN ('low', 'moderate', 'high')),
    score_component_count SMALLINT CHECK (score_component_count IS NULL OR score_component_count >= 0),
    processing_version TEXT NOT NULL,
    loaded_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (grid_id, week_start),
    CHECK (week_end >= week_start)
);

CREATE INDEX IF NOT EXISTS idx_water_stress_weekly_week_start
    ON gold.water_stress_weekly (week_start);
