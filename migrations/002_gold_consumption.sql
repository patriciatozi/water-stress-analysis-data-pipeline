-- Existing rows retain NULL serving status until Gold is regenerated and reloaded.
ALTER TABLE gold.water_stress_weekly
    ADD COLUMN weather_cell_id TEXT,
    ADD COLUMN weather_expected_days SMALLINT CHECK (weather_expected_days BETWEEN 1 AND 7),
    ADD COLUMN score_status TEXT CHECK (score_status IN ('complete', 'partial', 'unavailable')),
    ADD COLUMN score_available_weight DOUBLE PRECISION CHECK (score_available_weight BETWEEN 0 AND 1);

CREATE VIEW gold.water_stress_dashboard AS
SELECT w.*, g.centroid_latitude, g.centroid_longitude,
       g.area_km2 * w.soy_fraction AS soy_area_km2,
       w.water_stress_score * 100 AS water_stress_score_pct,
       g.geometry
FROM gold.water_stress_weekly w
JOIN silver.dim_spatial_grid g USING (grid_id);
