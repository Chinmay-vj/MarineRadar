-- Global Ship Tracker Phase 19: PostgreSQL + PostGIS schema
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS vessels (
    mmsi TEXT PRIMARY KEY,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    sog DOUBLE PRECISION,
    cog DOUBLE PRECISION,
    heading DOUBLE PRECISION,
    last_seen TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    geom GEOGRAPHY(Point, 4326)
);

CREATE TABLE IF NOT EXISTS positions (
    id BIGSERIAL PRIMARY KEY,
    mmsi TEXT NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    sog DOUBLE PRECISION,
    cog DOUBLE PRECISION,
    heading DOUBLE PRECISION,
    timestamp TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    geom GEOGRAPHY(Point, 4326),
    CONSTRAINT positions_mmsi_timestamp_unique UNIQUE (mmsi, timestamp)
);

CREATE TABLE IF NOT EXISTS vessel_metadata (
    mmsi TEXT PRIMARY KEY,
    imo TEXT,
    shipname TEXT,
    callsign TEXT,
    shiptype INTEGER,
    destination TEXT,
    draught DOUBLE PRECISION,
    eta TEXT,
    dim_a DOUBLE PRECISION,
    dim_b DOUBLE PRECISION,
    dim_c DOUBLE PRECISION,
    dim_d DOUBLE PRECISION,
    length DOUBLE PRECISION,
    beam DOUBLE PRECISION,
    last_static_update TIMESTAMPTZ,
    static_source TEXT
);

CREATE TABLE IF NOT EXISTS maritime_alerts (
    id BIGSERIAL PRIMARY KEY,
    mmsi TEXT NOT NULL,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    evidence TEXT NOT NULL,
    source TEXT NOT NULL,
    score INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL,
    acknowledged_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pg_alert_active
    ON maritime_alerts (mmsi, code) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_pg_vessels_geom ON vessels USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_pg_positions_geom ON positions USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_pg_positions_mmsi_timestamp ON positions (mmsi, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_pg_metadata_destination ON vessel_metadata (destination);
CREATE INDEX IF NOT EXISTS idx_pg_alert_status_seen ON maritime_alerts (status, last_seen DESC);

UPDATE vessels
SET geom = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography
WHERE geom IS NULL AND latitude IS NOT NULL AND longitude IS NOT NULL;

UPDATE positions
SET geom = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography
WHERE geom IS NULL;
