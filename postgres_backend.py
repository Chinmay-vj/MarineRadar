"""Optional PostgreSQL/PostGIS migration and spatial access layer."""

import os
import sqlite3
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


def _timestamp(value):
    return value.replace("Z", "+00:00") if isinstance(value, str) else value


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
