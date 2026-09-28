"""Optional PostgreSQL/PostGIS migration and spatial access layer for Phase 19/20."""

import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from math import floor
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "postgres_schema.sql"


def postgres_dsn():
    return os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN")


def _require_driver():
    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError(
            "PostgreSQL support requires psycopg[binary]. Install requirements.txt."
        ) from error
    return psycopg


def initialize_postgres(connection):
    """Apply the PostGIS schema to an open psycopg connection."""
    connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()


def check_postgres_health(dsn=None):
    """Check PostgreSQL / PostGIS connectivity, latency, and counts."""
    target_dsn = dsn if dsn is not None else postgres_dsn()
    if not target_dsn:
        return {"status": "unconfigured"}
    psycopg = _require_driver()
    start_time = time.perf_counter()
    try:
        with psycopg.connect(target_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.execute("SELECT count(*) FROM vessels")
                vessel_count = cursor.fetchone()[0]
                cursor.execute("SELECT count(*) FROM positions")
                position_count = cursor.fetchone()[0]
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        return {
            "status": "connected",
            "latency_ms": latency_ms,
            "vessels": vessel_count,
            "positions": position_count,
        }
    except Exception as error:
        return {
            "status": "error",
            "error": str(error),
        }


def _timestamp(value):
    if not value:
        return None
    if not isinstance(value, str):
        return value

    normalized = value.strip().replace("Z", "+00:00")

    # AISStream timestamps include a trailing `UTC` and may provide more
    # than six fractional-second digits. Convert them to a Python datetime so
    # psycopg passes a native, PostgreSQL-safe timestamptz value.
    if normalized.endswith(" UTC"):
        normalized = normalized[:-4].strip()

    normalized = re.sub(
        r"\.(\d{6})\d+(?=\s*[+-]\d{2}:?\d{2}$)",
        r".\1",
        normalized,
    )

    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return normalized


def migrate_sqlite_to_postgres(sqlite_path, dsn=None, batch_size=1000):
    """Copy SQLite state into PostGIS with idempotent position inserts."""
    psycopg = _require_driver()
    dsn = dsn or postgres_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL or POSTGRES_DSN is required.")

    sqlite_connection = sqlite3.connect(sqlite_path)
    sqlite_connection.row_factory = sqlite3.Row
    postgres_connection = psycopg.connect(dsn)
    try:
        initialize_postgres(postgres_connection)
        with postgres_connection.cursor() as cursor:
            vessels = sqlite_connection.execute("SELECT * FROM vessels").fetchall()
            for row in vessels:
                cursor.execute(
                    """
                    INSERT INTO vessels
                    (mmsi, latitude, longitude, sog, cog, heading, last_seen, updated_at, geom)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                            CASE WHEN %s IS NULL OR %s IS NULL THEN NULL
                                 ELSE ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography END)
                    ON CONFLICT (mmsi) DO UPDATE SET
                        latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
                        sog = EXCLUDED.sog, cog = EXCLUDED.cog, heading = EXCLUDED.heading,
                        last_seen = EXCLUDED.last_seen, updated_at = EXCLUDED.updated_at,
                        geom = EXCLUDED.geom
                    """,
                    (row["mmsi"], row["latitude"], row["longitude"], row["sog"],
                     row["cog"], row["heading"], _timestamp(row["last_seen"]),
                     _timestamp(row["updated_at"]), row["longitude"], row["latitude"],
                     row["longitude"], row["latitude"]),
                )

            position_cursor = sqlite_connection.execute(
                "SELECT * FROM positions ORDER BY id"
            )
            while True:
                rows = position_cursor.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    cursor.execute(
                        """
                        INSERT INTO positions
                        (mmsi, latitude, longitude, sog, cog, heading, timestamp, received_at, geom)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography)
                        ON CONFLICT (mmsi, timestamp) DO NOTHING
                        """,
                        (row["mmsi"], row["latitude"], row["longitude"], row["sog"],
                         row["cog"], row["heading"], _timestamp(row["timestamp"]),
                         _timestamp(row["received_at"]), row["longitude"], row["latitude"]),
                    )

            for table in ("vessel_metadata", "maritime_alerts"):
                columns = [row["name"] for row in sqlite_connection.execute(f"PRAGMA table_info({table})")]
                rows = sqlite_connection.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
                for row in rows:
                    values = [row[column] for column in columns]
                    placeholders = ", ".join(["%s"] * len(values))
                    conflict_key = "mmsi" if table == "vessel_metadata" else "id"
                    cursor.execute(
                        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
                        f"ON CONFLICT ({conflict_key}) DO NOTHING",
                        values,
                    )
        postgres_connection.commit()
    except Exception:
        postgres_connection.rollback()
        raise
    finally:
        sqlite_connection.close()
        postgres_connection.close()


def find_vessels_nearby(latitude, longitude, radius_km=25, dsn=None):
    """Return vessels within a PostGIS radius, ordered by distance."""
    psycopg = _require_driver()
    dsn = dsn or postgres_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL or POSTGRES_DSN is required.")
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT mmsi, latitude, longitude, sog, cog,
                       ST_Distance(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) / 1000 AS distance_km
                FROM vessels
                WHERE geom IS NOT NULL
                  AND ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
                ORDER BY distance_km
                """,
                (longitude, latitude, longitude, latitude, radius_km * 1000),
            )
            columns = [column.name for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]


def save_stream_batch_postgres(positions, metadata_records, dsn=None):
    """Persist batch of positions and metadata directly into PostgreSQL/PostGIS."""
    if not positions and not metadata_records:
        return
    psycopg = _require_driver()
    dsn = dsn or postgres_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL or POSTGRES_DSN is required.")

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            if positions:
                # 1. Insert positions with calculated geography point
                for vessel in positions:
                    mmsi = str(vessel["mmsi"])
                    lat = float(vessel["lat"])
                    lon = float(vessel["lon"])
                    sog = vessel.get("sog")
                    cog = vessel.get("cog")
                    heading = vessel.get("heading")
                    ts = _timestamp(vessel["timestamp"])
                    rx = _timestamp(vessel["received_at"])
                    cursor.execute(
                        """
                        INSERT INTO positions
                        (mmsi, latitude, longitude, sog, cog, heading, timestamp, received_at, geom)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography)
                        ON CONFLICT (mmsi, timestamp) DO NOTHING
                        """,
                        (mmsi, lat, lon, sog, cog, heading, ts, rx, lon, lat),
                    )

                # 2. Upsert vessels current state
                latest_by_mmsi = {}
                for vessel in positions:
                    mmsi = str(vessel["mmsi"])
                    ts = vessel["timestamp"]
                    if mmsi not in latest_by_mmsi or ts > latest_by_mmsi[mmsi]["timestamp"]:
                        latest_by_mmsi[mmsi] = vessel

                for vessel in latest_by_mmsi.values():
                    mmsi = str(vessel["mmsi"])
                    lat = float(vessel["lat"])
                    lon = float(vessel["lon"])
                    sog = vessel.get("sog")
                    cog = vessel.get("cog")
                    heading = vessel.get("heading")
                    ts = _timestamp(vessel["timestamp"])
                    rx = _timestamp(vessel["received_at"])
                    cursor.execute(
                        """
                        INSERT INTO vessels
                        (mmsi, latitude, longitude, sog, cog, heading, last_seen, updated_at, geom)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography)
                        ON CONFLICT (mmsi) DO UPDATE SET
                            latitude = EXCLUDED.latitude,
                            longitude = EXCLUDED.longitude,
                            sog = EXCLUDED.sog,
                            cog = EXCLUDED.cog,
                            heading = EXCLUDED.heading,
                            last_seen = EXCLUDED.last_seen,
                            updated_at = EXCLUDED.updated_at,
                            geom = EXCLUDED.geom
                        """,
                        (mmsi, lat, lon, sog, cog, heading, ts, rx, lon, lat),
                    )

            if metadata_records:
                for meta in metadata_records:
                    mmsi = str(meta["mmsi"])
                    cursor.execute(
                        """
                        INSERT INTO vessel_metadata
                        (mmsi, imo, shipname, callsign, shiptype, destination, draught, eta,
                         dim_a, dim_b, dim_c, dim_d, length, beam, last_static_update, static_source)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (mmsi) DO UPDATE SET
                            imo = COALESCE(EXCLUDED.imo, vessel_metadata.imo),
                            shipname = COALESCE(EXCLUDED.shipname, vessel_metadata.shipname),
                            callsign = COALESCE(EXCLUDED.callsign, vessel_metadata.callsign),
                            shiptype = COALESCE(EXCLUDED.shiptype, vessel_metadata.shiptype),
                            destination = COALESCE(EXCLUDED.destination, vessel_metadata.destination),
                            draught = COALESCE(EXCLUDED.draught, vessel_metadata.draught),
                            eta = COALESCE(EXCLUDED.eta, vessel_metadata.eta),
                            dim_a = COALESCE(EXCLUDED.dim_a, vessel_metadata.dim_a),
                            dim_b = COALESCE(EXCLUDED.dim_b, vessel_metadata.dim_b),
                            dim_c = COALESCE(EXCLUDED.dim_c, vessel_metadata.dim_c),
                            dim_d = COALESCE(EXCLUDED.dim_d, vessel_metadata.dim_d),
                            length = COALESCE(EXCLUDED.length, vessel_metadata.length),
                            beam = COALESCE(EXCLUDED.beam, vessel_metadata.beam),
                            last_static_update = EXCLUDED.last_static_update,
                            static_source = EXCLUDED.static_source
                        """,
                        (
                            mmsi, meta.get("imo"), meta.get("shipname"), meta.get("callsign"),
                            meta.get("shiptype"), meta.get("destination"), meta.get("draught"),
                            meta.get("eta"), meta.get("dim_a"), meta.get("dim_b"),
                            meta.get("dim_c"), meta.get("dim_d"), meta.get("length"),
                            meta.get("beam"), _timestamp(meta.get("last_static_update")),
                            meta.get("static_source"),
                        ),
                    )
        connection.commit()


def get_current_vessels_postgres(limit=100, dsn=None):
    """Retrieve active vessels with metadata from PostgreSQL."""
    psycopg = _require_driver()
    dsn = dsn or postgres_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL or POSTGRES_DSN is required.")
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            query = """
                SELECT
                    vessels.mmsi, vessels.latitude, vessels.longitude, vessels.sog,
                    vessels.cog, vessels.heading,
                    to_char(vessels.last_seen, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS last_seen,
                    to_char(vessels.updated_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS updated_at,
                    vessel_metadata.imo, vessel_metadata.shipname, vessel_metadata.callsign,
                    vessel_metadata.shiptype, vessel_metadata.destination, vessel_metadata.draught,
                    vessel_metadata.eta, vessel_metadata.length, vessel_metadata.beam,
                    to_char(vessel_metadata.last_static_update, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS last_static_update
                FROM vessels
                LEFT JOIN vessel_metadata ON vessel_metadata.mmsi = vessels.mmsi
                ORDER BY vessels.updated_at DESC
            """
            if limit is not None:
                query += " LIMIT %s"
                cursor.execute(query, (limit,))
            else:
                cursor.execute(query)
            columns = [column.name for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]


def get_vessel_postgres(mmsi, dsn=None):
    """Retrieve a single vessel with metadata from PostgreSQL."""
    psycopg = _require_driver()
    dsn = dsn or postgres_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL or POSTGRES_DSN is required.")
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    vessels.mmsi, vessels.latitude, vessels.longitude, vessels.sog,
                    vessels.cog, vessels.heading,
                    to_char(vessels.last_seen, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS last_seen,
                    to_char(vessels.updated_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS updated_at,
                    vessel_metadata.imo, vessel_metadata.shipname, vessel_metadata.callsign,
                    vessel_metadata.shiptype, vessel_metadata.destination, vessel_metadata.draught,
                    vessel_metadata.eta, vessel_metadata.length, vessel_metadata.beam,
                    to_char(vessel_metadata.last_static_update, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS last_static_update
                FROM vessels
                LEFT JOIN vessel_metadata ON vessel_metadata.mmsi = vessels.mmsi
                WHERE vessels.mmsi = %s
                LIMIT 1
                """,
                (str(mmsi),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            columns = [column.name for column in cursor.description]
            return dict(zip(columns, row))


def get_recent_vessel_mmsis_postgres(limit=1500, dsn=None):
    """Return recently seen MMSIs from PostgreSQL."""
    psycopg = _require_driver()
    dsn = dsn or postgres_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL or POSTGRES_DSN is required.")
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT mmsi
                FROM vessels
                WHERE mmsi IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [str(row[0]) for row in cursor.fetchall() if row[0]]


def get_vessel_histories_postgres(mmsis, limit=20, dsn=None):
    """Retrieve historical position trails for multiple MMSIs from PostgreSQL."""
    if not mmsis:
        return {}
    psycopg = _require_driver()
    dsn = dsn or postgres_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL or POSTGRES_DSN is required.")

    mmsi_list = [str(mmsi) for mmsi in mmsis if mmsi]
    histories = {}
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT mmsi, latitude, longitude,
                       to_char(timestamp, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS timestamp
                FROM (
                    SELECT mmsi, latitude, longitude, timestamp,
                           ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY timestamp DESC) AS row_number
                    FROM positions
                    WHERE mmsi = ANY(%s)
                ) sub
                WHERE row_number <= %s
                ORDER BY mmsi, timestamp ASC
                """,
                (mmsi_list, limit),
            )
            for row in cursor.fetchall():
                mmsi = str(row[0])
                if mmsi not in histories:
                    histories[mmsi] = []
                histories[mmsi].append({
                    "lat": float(row[1]),
                    "lon": float(row[2]),
                    "timestamp": row[3],
                })
    return histories


def get_vessel_history_postgres(mmsi, limit=20, dsn=None):
    """Retrieve position history for one vessel in chronological order from PostgreSQL."""
    psycopg = _require_driver()
    dsn = dsn or postgres_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL or POSTGRES_DSN is required.")
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, mmsi, latitude, longitude, sog, cog, heading,
                       to_char(timestamp, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS timestamp,
                       to_char(received_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS received_at
                FROM positions
                WHERE mmsi = %s
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (str(mmsi), limit),
            )
            columns = [column.name for column in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            rows.reverse()
            return rows


def sync_maritime_alerts_postgres(alert_records, observed_at=None, dsn=None):
    """Upsert current anomalies and resolve disappeared alerts in PostgreSQL."""
    psycopg = _require_driver()
    dsn = dsn or postgres_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL or POSTGRES_DSN is required.")
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    observed_ts = _timestamp(observed_at)

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            active_keys = set()
            for record in alert_records:
                mmsi = str(record.get("mmsi") or "")
                for anomaly in record.get("anomalies", []):
                    code = str(anomaly.get("code") or "")
                    if not mmsi or not code:
                        continue
                    active_keys.add((mmsi, code))
                    cursor.execute(
                        """
                        SELECT id FROM maritime_alerts
                        WHERE mmsi = %s AND code = %s AND status = 'active'
                        """,
                        (mmsi, code),
                    )
                    existing = cursor.fetchone()
                    if existing:
                        cursor.execute(
                            """
                            UPDATE maritime_alerts
                            SET severity = %s, title = %s, evidence = %s, source = %s,
                                score = %s, last_seen = %s
                            WHERE id = %s
                            """,
                            (
                                anomaly["severity"], anomaly["title"],
                                anomaly["evidence"], anomaly["source"],
                                anomaly["score"], observed_ts, existing[0],
                            ),
                        )
                    else:
                        cursor.execute(
                            """
                            INSERT INTO maritime_alerts
                            (mmsi, code, severity, title, evidence, source, score,
                             status, first_seen, last_seen)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, %s)
                            """,
                            (
                                mmsi, code, anomaly["severity"], anomaly["title"],
                                anomaly["evidence"], anomaly["source"], anomaly["score"],
                                observed_ts, observed_ts,
                            ),
                        )

            cursor.execute("SELECT id, mmsi, code FROM maritime_alerts WHERE status = 'active'")
            for row in cursor.fetchall():
                if (row[1], row[2]) not in active_keys:
                    cursor.execute(
                        "UPDATE maritime_alerts SET status = 'resolved', last_seen = %s WHERE id = %s",
                        (observed_ts, row[0]),
                    )
        connection.commit()


def get_maritime_alerts_postgres(status="active", limit=100, severity=None, dsn=None):
    """Retrieve filtered alerts from PostgreSQL."""
    psycopg = _require_driver()
    dsn = dsn or postgres_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL or POSTGRES_DSN is required.")
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            clauses = []
            parameters = []
            if status != "all":
                clauses.append("status = %s")
                parameters.append(status)
            if severity:
                clauses.append("severity = %s")
                parameters.append(severity)
            where = "WHERE " + " AND ".join(clauses) if clauses else ""
            parameters.append(limit)
            cursor.execute(
                f"""
                SELECT id, mmsi, code, severity, title, evidence, source, score, status,
                       to_char(first_seen, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS first_seen,
                       to_char(last_seen, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS last_seen,
                       to_char(acknowledged_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS acknowledged_at
                FROM maritime_alerts
                {where}
                ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                         last_seen DESC
                LIMIT %s
                """,
                parameters,
            )
            columns = [column.name for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]


def acknowledge_maritime_alert_postgres(alert_id, dsn=None):
    """Acknowledge an active maritime alert in PostgreSQL."""
    psycopg = _require_driver()
    dsn = dsn or postgres_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL or POSTGRES_DSN is required.")
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            timestamp = datetime.now(timezone.utc).isoformat()
            cursor.execute(
                """
                UPDATE maritime_alerts
                SET status = 'acknowledged', acknowledged_at = %s
                WHERE id = %s AND status = 'active'
                """,
                (_timestamp(timestamp), alert_id),
            )
            rowcount = cursor.rowcount
        connection.commit()
        return rowcount == 1


def get_traffic_density_postgres(hours=24, grid_size=2.0, max_points=50000, dsn=None):
    """Compute traffic density cells using PostgreSQL."""
    psycopg = _require_driver()
    dsn = dsn or postgres_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL or POSTGRES_DSN is required.")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT mmsi, latitude, longitude
                FROM positions
                WHERE timestamp >= %s
                  AND latitude BETWEEN -90 AND 90
                  AND longitude BETWEEN -180 AND 180
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (cutoff, max_points),
            )
            rows = cursor.fetchall()

    cells = {}
    vessels = set()
    for row in rows:
        mmsi, lat, lon = str(row[0]), float(row[1]), float(row[2])
        lat_cell = floor(lat / grid_size) * grid_size
        lon_cell = floor(lon / grid_size) * grid_size
        key = (lat_cell, lon_cell)
        cell = cells.setdefault(
            key,
            {
                "latitude": round(lat_cell + grid_size / 2, 4),
                "longitude": round(lon_cell + grid_size / 2, 4),
                "intensity": 0,
                "vessels": set(),
            },
        )
        cell["intensity"] += 1
        cell["vessels"].add(mmsi)
        vessels.add(mmsi)

    return {
        "hours": hours,
        "grid_size": grid_size,
        "point_count": len(rows),
        "vessel_count": len(vessels),
        "cells": [
            {
                "latitude": cell["latitude"],
                "longitude": cell["longitude"],
                "intensity": cell["intensity"],
                "vessels": len(cell["vessels"]),
            }
            for cell in cells.values()
        ],
    }


def get_destination_analysis_postgres(dsn=None):
    """Aggregate top destinations from vessel_metadata in PostgreSQL."""
    psycopg = _require_driver()
    dsn = dsn or postgres_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL or POSTGRES_DSN is required.")
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT destination, COUNT(*) AS vessel_count
                FROM vessel_metadata
                WHERE destination IS NOT NULL
                  AND TRIM(destination) != ''
                GROUP BY destination
                ORDER BY vessel_count DESC, destination ASC
                """
            )
            return [
                {"destination": row[0], "vessel_count": row[1]}
                for row in cursor.fetchall()
            ]
