-- Parallel scenario datasets: v1 tables and serving view remain available.
CREATE TABLE gold.soil_hydraulics (
    analysis_id TEXT NOT NULL,
    grid_id TEXT NOT NULL REFERENCES silver.dim_spatial_grid (grid_id),
    model_signature TEXT NOT NULL,
    soil_method TEXT NOT NULL,
    field_capacity DOUBLE PRECISION CHECK (field_capacity BETWEEN 0 AND 1),
    wilting_point DOUBLE PRECISION CHECK (wilting_point BETWEEN 0 AND 1),
    available_water_mm DOUBLE PRECISION CHECK (available_water_mm > 0),
    organic_matter_pct DOUBLE PRECISION CHECK (organic_matter_pct BETWEEN 0 AND 8),
    soil_status TEXT NOT NULL CHECK (soil_status IN ('valid', 'invalid')),
    soil_issue TEXT,
    processing_version TEXT NOT NULL,
    loaded_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (analysis_id, grid_id),
    CHECK (soil_status <> 'valid' OR
           (field_capacity IS NOT NULL AND wilting_point IS NOT NULL AND
            field_capacity > wilting_point AND available_water_mm IS NOT NULL AND
            organic_matter_pct IS NOT NULL))
);

CREATE TABLE gold.water_stress_weekly_v2 (
    analysis_id TEXT NOT NULL,
    grid_id TEXT NOT NULL REFERENCES silver.dim_spatial_grid (grid_id),
    scenario_id TEXT NOT NULL,
    week_start DATE NOT NULL,
    week_end DATE NOT NULL,
    model_signature TEXT NOT NULL,
    score_method_version TEXT NOT NULL,
    soy_fraction DOUBLE PRECISION NOT NULL CHECK (soy_fraction BETWEEN 0 AND 1),
    weather_cell_id TEXT NOT NULL,
    initial_water_fraction DOUBLE PRECISION NOT NULL CHECK (initial_water_fraction BETWEEN 0 AND 1),
    crop_stage TEXT CHECK (crop_stage IN ('initial', 'development', 'mid', 'late')),
    active_day_count SMALLINT NOT NULL CHECK (active_day_count BETWEEN 0 AND 7),
    valid_day_count SMALLINT NOT NULL CHECK (valid_day_count BETWEEN 0 AND active_day_count),
    water_start_mm DOUBLE PRECISION CHECK (water_start_mm >= 0),
    water_end_mm DOUBLE PRECISION CHECK (water_end_mm >= 0),
    etc_potential_mm DOUBLE PRECISION CHECK (etc_potential_mm >= 0),
    etc_adjusted_mm DOUBLE PRECISION CHECK (etc_adjusted_mm >= 0),
    retained_rain_mm DOUBLE PRECISION CHECK (retained_rain_mm >= 0),
    drainage_mm DOUBLE PRECISION CHECK (drainage_mm >= 0),
    water_stress_mean DOUBLE PRECISION CHECK (water_stress_mean BETWEEN 0 AND 1),
    water_stress_max DOUBLE PRECISION CHECK (water_stress_max BETWEEN 0 AND 1),
    stress_day_count SMALLINT CHECK (stress_day_count BETWEEN 0 AND active_day_count),
    maximum_stress_run_days SMALLINT CHECK (maximum_stress_run_days BETWEEN 0 AND active_day_count),
    ongoing_stress_run_days INTEGER CHECK (ongoing_stress_run_days >= 0),
    ndvi_median DOUBLE PRECISION CHECK (ndvi_median BETWEEN -1 AND 1),
    ndmi_median DOUBLE PRECISION CHECK (ndmi_median BETWEEN -1 AND 1),
    satellite_age_days SMALLINT,
    water_stress_score_v1 DOUBLE PRECISION CHECK (water_stress_score_v1 BETWEEN 0 AND 1),
    score_status_v1 TEXT CHECK (score_status_v1 IN ('complete', 'partial', 'unavailable')),
    water_stress_score DOUBLE PRECISION CHECK (water_stress_score BETWEEN 0 AND 1),
    water_stress_risk_class TEXT CHECK (water_stress_risk_class IN ('low', 'attention', 'high', 'critical')),
    risk_classification_version TEXT NOT NULL,
    monitoring_guidance TEXT NOT NULL,
    score_status TEXT NOT NULL CHECK (score_status IN ('complete', 'partial', 'unavailable', 'not_applicable')),
    score_available_weight DOUBLE PRECISION NOT NULL CHECK (score_available_weight BETWEEN 0 AND 1),
    score_component_count SMALLINT NOT NULL CHECK (score_component_count BETWEEN 0 AND 3),
    score_issue TEXT,
    processing_version TEXT NOT NULL,
    loaded_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (analysis_id, grid_id, week_start, scenario_id),
    CHECK (week_end BETWEEN week_start AND week_start + 6),
    CHECK (water_stress_risk_class IS NULL OR water_stress_score IS NOT NULL),
    CHECK ((score_status IN ('complete', 'partial') AND water_stress_score IS NOT NULL
            AND water_stress_mean IS NOT NULL AND active_day_count > 0
            AND valid_day_count = active_day_count)
           OR (score_status IN ('unavailable', 'not_applicable') AND water_stress_score IS NULL)),
    CHECK (score_status <> 'not_applicable' OR active_day_count = 0)
);

CREATE INDEX idx_water_stress_v2_week_scenario
    ON gold.water_stress_weekly_v2 (analysis_id, week_start, scenario_id);

CREATE VIEW gold.water_stress_dashboard_v2 AS
SELECT w.*, h.field_capacity, h.wilting_point, h.available_water_mm,
       h.organic_matter_pct, h.soil_status, h.soil_issue,
       0.30::double precision AS model_depth_m,
       g.centroid_latitude, g.centroid_longitude,
       g.area_km2 * w.soy_fraction AS soy_area_km2,
       w.water_stress_score * 100 AS water_stress_score_pct,
       g.geometry
FROM gold.water_stress_weekly_v2 w
JOIN silver.dim_spatial_grid g USING (grid_id)
LEFT JOIN gold.soil_hydraulics h
    ON h.analysis_id = w.analysis_id AND h.grid_id = w.grid_id
    AND h.model_signature = w.model_signature;

COMMENT ON TABLE gold.water_stress_weekly_v2 IS
    'Weekly hypothetical soybean cycle; daily 0-30 cm balance, not measured full-root-zone stress.';
COMMENT ON TABLE gold.soil_hydraulics IS
    'Saxton-Rawls estimates from existing 0-30 cm averages; water contents m3/m3, available water mm.';
