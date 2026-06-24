-- VYOM — PostGIS schema
-- Apply with: psql -U krish -d agentic_gis_db -f db/schema.sql

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── SCENES CATALOG (STAC-style) ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scenes (
    id               TEXT PRIMARY KEY,           -- scene_id from filename
    collection       TEXT NOT NULL,              -- 'ResourceSat-2A_LISS3_L2' etc
    satellite        TEXT NOT NULL,              -- 'ResourceSat-2A'
    sensor           TEXT NOT NULL,              -- 'LISS3', 'LISS4', 'AWiFS'
    geometry         GEOMETRY(Polygon, 4326),    -- footprint WGS84
    acq_datetime     TIMESTAMPTZ NOT NULL,
    cloud_cover      FLOAT,
    gsd_m            FLOAT,
    processing_level TEXT,                       -- 'L2', 'BOA', 'ARD'
    event_key        TEXT,                       -- FK to events_registry
    window_type      TEXT,                       -- 'pre_event','event','post_event','annual'
    properties       JSONB,                      -- sensor-specific long tail
    assets           JSONB,                      -- {band_name: relative_cog_path}
    ingested_at      TIMESTAMPTZ DEFAULT now(),
    pipeline_version TEXT NOT NULL DEFAULT 'v1.0-dos'
);

CREATE INDEX IF NOT EXISTS scenes_geom_idx   ON scenes USING GIST(geometry);
CREATE INDEX IF NOT EXISTS scenes_time_idx   ON scenes(acq_datetime);
CREATE INDEX IF NOT EXISTS scenes_sensor_idx ON scenes(sensor, satellite);
CREATE INDEX IF NOT EXISTS scenes_event_idx  ON scenes(event_key, window_type);
CREATE INDEX IF NOT EXISTS scenes_props_idx  ON scenes USING GIN(properties);
CREATE INDEX IF NOT EXISTS scenes_assets_idx ON scenes USING GIN(assets);

-- ── SCENE METRICS CACHE ────────────────────────────────────────────────────
-- Turns pixel-level content queries into SQL lookups
CREATE TABLE IF NOT EXISTS scene_metrics (
    id               BIGSERIAL PRIMARY KEY,
    scene_id         TEXT REFERENCES scenes(id) ON DELETE CASCADE,
    aoi_hash         TEXT NOT NULL,             -- MD5(ST_AsText(aoi_geom))
    metric           TEXT NOT NULL,             -- 'ndwi_mean','water_area_pct' etc
    value            FLOAT NOT NULL,
    computed_at      TIMESTAMPTZ DEFAULT now(),
    pipeline_version TEXT NOT NULL DEFAULT 'v1.0-dos'
);

-- Composite unique index — hit on every agent content query
CREATE UNIQUE INDEX IF NOT EXISTS scene_metrics_lookup_idx
    ON scene_metrics(scene_id, aoi_hash, metric, pipeline_version);

-- ── DERIVED PRODUCTS REGISTRY ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS derived_products (
    id               BIGSERIAL PRIMARY KEY,
    scene_id         TEXT REFERENCES scenes(id) ON DELETE CASCADE,
    product_type     TEXT NOT NULL,             -- 'ndwi_cog','flood_mask','burn_scar' etc
    relative_path    TEXT NOT NULL,             -- relative to PROJECT_ROOT
    created_at       TIMESTAMPTZ DEFAULT now(),
    pipeline_version TEXT NOT NULL DEFAULT 'v1.0-dos',
    metadata         JSONB
);

-- ── BENCHMARK QUERY SET ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS benchmark_queries (
    id               SERIAL PRIMARY KEY,
    event_key        TEXT NOT NULL,
    hazard_type      TEXT NOT NULL,
    nl_query         TEXT NOT NULL,
    aoi_geojson      JSONB NOT NULL,
    date_range       TSTZRANGE,
    expected_answer  TEXT,
    expected_tools   TEXT[],                    -- tools agent should call
    difficulty       TEXT CHECK (difficulty IN ('easy','medium','hard')),
    created_at       TIMESTAMPTZ DEFAULT now()
);
