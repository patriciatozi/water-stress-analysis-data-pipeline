-- Gold v1 rows keep NULL classification fields until regenerated and reloaded.
ALTER TABLE gold.water_stress_weekly
    ADD COLUMN water_stress_risk_class TEXT
        CHECK (water_stress_risk_class IN ('low', 'attention', 'high', 'critical')),
    ADD COLUMN risk_classification_version TEXT,
    ADD COLUMN monitoring_guidance TEXT,
    ADD CONSTRAINT risk_class_requires_score
        CHECK (water_stress_risk_class IS NULL OR
               (water_stress_score IS NOT NULL AND risk_classification_version IS NOT NULL));

-- Append new fields after the existing view columns to preserve dependent consumers.
CREATE OR REPLACE VIEW gold.water_stress_dashboard AS
SELECT w.grid_id, w.week_start, w.week_end, w.soy_fraction,
       w.clay_pct, w.sand_pct, w.soc, w.bulk_density,
       w.precipitation_mm_7d, w.eto_mm_7d,
       w.water_balance_mm_7d, w.water_deficit_mm_7d,
       w.rainy_day_count, w.consecutive_dry_days,
       w.temperature_mean_c, w.temperature_max_c, w.temperature_min_c,
       w.weather_observation_count, w.ndvi_median, w.ndmi_median,
       w.satellite_scene_count, w.satellite_valid_pixel_pct, w.satellite_cloud_pixel_pct,
       w.satellite_observation_date, w.satellite_age_days,
       w.water_stress_score, w.water_stress_class, w.score_component_count,
       w.processing_version, w.loaded_at_utc,
       w.weather_cell_id, w.weather_expected_days, w.score_status, w.score_available_weight,
       g.centroid_latitude, g.centroid_longitude,
       g.area_km2 * w.soy_fraction AS soy_area_km2,
       w.water_stress_score * 100 AS water_stress_score_pct,
       g.geometry,
       w.water_stress_risk_class, w.risk_classification_version, w.monitoring_guidance
FROM gold.water_stress_weekly w
JOIN silver.dim_spatial_grid g USING (grid_id);
