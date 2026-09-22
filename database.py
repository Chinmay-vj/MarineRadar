import sqlite3
from datetime import datetime, timedelta, timezone
from math import floor
from pathlib import Path


# ============================================================
# DATABASE CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"

DB_PATH = DATA_DIR / "ships.db"


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_connection():

    connection = sqlite3.connect(
        DB_PATH,
        timeout=10
    )

    connection.row_factory = sqlite3.Row

    # Wait up to 10 seconds if another SQLite operation
    # temporarily has the database locked.
    connection.execute(
        "PRAGMA busy_timeout = 10000"
    )

    return connection

# ============================================================
# INITIALIZE DATABASE
# ============================================================

def initialize_database():

    DATA_DIR.mkdir(
        exist_ok=True
    )

    connection = get_connection()

    # Enable Write-Ahead Logging.
    # This allows SQLite readers and the continuous
    # database writer to work concurrently.
    connection.execute(
        "PRAGMA journal_mode=WAL"
    )

    # Good balance between write performance and durability.
    connection.execute(
        "PRAGMA synchronous=NORMAL"
    )

    cursor = connection.cursor()


    # --------------------------------------------------------
    # CURRENT VESSEL STATE
    # --------------------------------------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vessels (

            mmsi TEXT PRIMARY KEY,

            latitude REAL,

            longitude REAL,

            sog REAL,

            cog REAL,

            heading REAL,

            last_seen TEXT,

            updated_at TEXT

        )
        """
    )


    # --------------------------------------------------------
    # POSITION HISTORY
    # --------------------------------------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS positions (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            mmsi TEXT NOT NULL,

            latitude REAL NOT NULL,

            longitude REAL NOT NULL,

            sog REAL,

            cog REAL,

            heading REAL,

            timestamp TEXT NOT NULL,

            received_at TEXT NOT NULL

        )
        """
    )


    # --------------------------------------------------------
    # INDEX FOR HISTORY LOOKUPS
    # --------------------------------------------------------

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_positions_mmsi_timestamp

        ON positions (
            mmsi,
            timestamp
        )
        """
    )


    # --------------------------------------------------------
    # PREVENT DUPLICATE AIS POSITIONS
    # --------------------------------------------------------

    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
        idx_positions_unique_mmsi_timestamp

        ON positions (
            mmsi,
            timestamp
        )
        """
    )


    # --------------------------------------------------------
    # STATIC / VOYAGE VESSEL METADATA
    # --------------------------------------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vessel_metadata (

            mmsi TEXT PRIMARY KEY,

            imo TEXT,
            shipname TEXT,
            callsign TEXT,
            shiptype INTEGER,
            destination TEXT,
            draught REAL,
            eta TEXT,
            dim_a REAL,
            dim_b REAL,
            dim_c REAL,
            dim_d REAL,
            length REAL,
            beam REAL,
            last_static_update TEXT,
            static_source TEXT

        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_vessel_metadata_shipname

        ON vessel_metadata (shipname)
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_vessel_metadata_imo

        ON vessel_metadata (imo)
        """
    )


    connection.commit()

    connection.close()


# ============================================================
# SAVE ONE VESSEL POSITION
# ============================================================

def save_vessel(
    mmsi,
    latitude,
    longitude,
    sog,
    cog,
    heading,
    timestamp,
    received_at
):

    connection = get_connection()

    cursor = connection.cursor()


    # --------------------------------------------------------
    # UPDATE CURRENT VESSEL STATE
    # --------------------------------------------------------

    cursor.execute(
        """
        INSERT INTO vessels (

            mmsi,
            latitude,
            longitude,
            sog,
            cog,
            heading,
            last_seen,
            updated_at

        )

        VALUES (?, ?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT(mmsi)

        DO UPDATE SET

            latitude = excluded.latitude,

            longitude = excluded.longitude,

            sog = excluded.sog,

            cog = excluded.cog,

            heading = excluded.heading,

            last_seen = excluded.last_seen,

            updated_at = excluded.updated_at
        """,
        (
            mmsi,
            latitude,
            longitude,
            sog,
            cog,
            heading,
            timestamp,
            received_at
        )
    )


    # --------------------------------------------------------
    # SAVE POSITION HISTORY
    # --------------------------------------------------------

    cursor.execute(
        """
        INSERT OR IGNORE INTO positions (

            mmsi,
            latitude,
            longitude,
            sog,
            cog,
            heading,
            timestamp,
            received_at

        )

        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mmsi,
            latitude,
            longitude,
            sog,
            cog,
            heading,
            timestamp,
            received_at
        )
    )


    connection.commit()

    connection.close()


# ============================================================
# SAVE MULTIPLE VESSEL POSITIONS
# ============================================================

def save_vessels_batch(positions):

    if not positions:
        return


    connection = get_connection()

    cursor = connection.cursor()


    try:

        # ====================================================
        # 1. SAVE ALL POSITION HISTORY
        # ====================================================

        position_rows = [

            (
                vessel["mmsi"],
                vessel["lat"],
                vessel["lon"],
                vessel["sog"],
                vessel["cog"],
                vessel["heading"],
                vessel["timestamp"],
                vessel["received_at"]
            )

            for vessel in positions
        ]


        cursor.executemany(
            """
            INSERT OR IGNORE INTO positions (

                mmsi,
                latitude,
                longitude,
                sog,
                cog,
                heading,
                timestamp,
                received_at

            )

            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,

            position_rows
        )


        # ====================================================
        # 2. FIND ONLY LATEST POSITION FOR EACH MMSI
        # ====================================================

        latest_by_mmsi = {}


        for vessel in positions:

            mmsi = vessel["mmsi"]

            existing = latest_by_mmsi.get(
                mmsi
            )


            if (
                existing is None
                or
                vessel["timestamp"] > existing["timestamp"]
            ):

                latest_by_mmsi[mmsi] = vessel


        # ====================================================
        # 3. UPDATE CURRENT VESSEL STATE
        # ====================================================

        vessel_rows = [

            (
                vessel["mmsi"],
                vessel["lat"],
                vessel["lon"],
                vessel["sog"],
                vessel["cog"],
                vessel["heading"],
                vessel["timestamp"],
                vessel["received_at"]
            )

            for vessel in latest_by_mmsi.values()
        ]


        cursor.executemany(
            """
            INSERT INTO vessels (

                mmsi,
                latitude,
                longitude,
                sog,
                cog,
                heading,
                last_seen,
                updated_at

            )

            VALUES (?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(mmsi)

            DO UPDATE SET

                latitude = excluded.latitude,

                longitude = excluded.longitude,

                sog = excluded.sog,

                cog = excluded.cog,

                heading = excluded.heading,

                last_seen = excluded.last_seen,

                updated_at = excluded.updated_at
            """,

            vessel_rows
        )


        # ====================================================
        # 4. COMMIT EVERYTHING TOGETHER
        # ====================================================

        connection.commit()


    except Exception:

        connection.rollback()

        raise


    finally:

        connection.close()


# ============================================================
# SAVE STATIC AIS METADATA
# ============================================================

METADATA_FIELDS = (
    "imo",
    "shipname",
    "callsign",
    "shiptype",
    "destination",
    "draught",
    "eta",
    "dim_a",
    "dim_b",
    "dim_c",
    "dim_d",
    "length",
    "beam",
)


def _save_metadata_batch(cursor, metadata_records):
    """Merge non-empty static AIS fields without erasing known values."""

    if not metadata_records:
        return

    rows = []

    for record in metadata_records:

        mmsi = str(record.get("mmsi") or "").strip()

        if not mmsi:
            continue

        rows.append(
            tuple(
                [mmsi]
                + [record.get(field) for field in METADATA_FIELDS]
                + [
                    record.get("last_static_update"),
                    record.get("static_source"),
                ]
            )
        )

    if not rows:
        return

    cursor.executemany(
        """
        INSERT INTO vessel_metadata (
            mmsi,
            imo,
            shipname,
            callsign,
            shiptype,
            destination,
            draught,
            eta,
            dim_a,
            dim_b,
            dim_c,
            dim_d,
            length,
            beam,
            last_static_update,
            static_source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mmsi)
        DO UPDATE SET
            imo = COALESCE(excluded.imo, vessel_metadata.imo),
            shipname = COALESCE(excluded.shipname, vessel_metadata.shipname),
            callsign = COALESCE(excluded.callsign, vessel_metadata.callsign),
            shiptype = COALESCE(excluded.shiptype, vessel_metadata.shiptype),
            destination = COALESCE(excluded.destination, vessel_metadata.destination),
            draught = COALESCE(excluded.draught, vessel_metadata.draught),
            eta = COALESCE(excluded.eta, vessel_metadata.eta),
            dim_a = COALESCE(excluded.dim_a, vessel_metadata.dim_a),
            dim_b = COALESCE(excluded.dim_b, vessel_metadata.dim_b),
            dim_c = COALESCE(excluded.dim_c, vessel_metadata.dim_c),
            dim_d = COALESCE(excluded.dim_d, vessel_metadata.dim_d),
            length = COALESCE(excluded.length, vessel_metadata.length),
            beam = COALESCE(excluded.beam, vessel_metadata.beam),
            last_static_update = COALESCE(
                excluded.last_static_update,
                vessel_metadata.last_static_update
            ),
            static_source = COALESCE(
                excluded.static_source,
                vessel_metadata.static_source
            )
        """,
        rows,
    )


def save_vessel_metadata_batch(metadata_records):

    if not metadata_records:
        return

    connection = get_connection()

    try:

        _save_metadata_batch(
            connection.cursor(),
            metadata_records,
        )

        connection.commit()

    except Exception:

        connection.rollback()

        raise

    finally:

        connection.close()


def save_stream_batch(positions, metadata_records):
    """Persist position and static AIS update groups through one writer."""

    if positions:
        save_vessels_batch(positions)

    if metadata_records:
        save_vessel_metadata_batch(metadata_records)


# ============================================================
# GET CURRENT VESSELS
# ============================================================

def get_current_vessels(limit=100):

    connection = get_connection()

    cursor = connection.cursor()


    if limit is None:

        cursor.execute(
            """
            SELECT
                vessels.*,
                vessel_metadata.imo,
                vessel_metadata.shipname,
                vessel_metadata.callsign,
                vessel_metadata.shiptype,
                vessel_metadata.destination,
                vessel_metadata.draught,
                vessel_metadata.eta,
                vessel_metadata.length,
                vessel_metadata.beam,
                vessel_metadata.last_static_update

            FROM vessels

            LEFT JOIN vessel_metadata
            ON vessel_metadata.mmsi = vessels.mmsi

            ORDER BY updated_at DESC
            """
        )

    else:

        cursor.execute(
            """
            SELECT
                vessels.*,
                vessel_metadata.imo,
                vessel_metadata.shipname,
                vessel_metadata.callsign,
                vessel_metadata.shiptype,
                vessel_metadata.destination,
                vessel_metadata.draught,
                vessel_metadata.eta,
                vessel_metadata.length,
                vessel_metadata.beam,
                vessel_metadata.last_static_update

            FROM vessels

            LEFT JOIN vessel_metadata
            ON vessel_metadata.mmsi = vessels.mmsi

            ORDER BY updated_at DESC

            LIMIT ?
            """,
            (limit,)
        )


    rows = cursor.fetchall()

    connection.close()


    return [
        dict(row)
        for row in rows
    ]


# ============================================================
# GET RECENT MMSIS FOR STATIC AIS SUBSCRIPTIONS
# ============================================================

def get_recent_vessel_mmsis(limit=1500):

    connection = get_connection()

    try:

        rows = connection.execute(
            """
            SELECT mmsi

            FROM vessels

            WHERE mmsi IS NOT NULL

            ORDER BY updated_at DESC

            LIMIT ?
            """,
            (limit,)
        ).fetchall()

        return [
            str(row["mmsi"])
            for row in rows
        ]

    finally:

        connection.close()

# ============================================================
# GET HISTORY FOR MULTIPLE VESSELS
# ============================================================

def get_vessel_histories(
    mmsis,
    limit=20
):

    if not mmsis:

        return {}


    connection = get_connection()

    cursor = connection.cursor()


    histories = {}


    # --------------------------------------------------------
    # SQLite has a limit on the number of query parameters.
    # Process MMSIs in chunks.
    # --------------------------------------------------------

    chunk_size = 900


    for start in range(
        0,
        len(mmsis),
        chunk_size
    ):

        chunk = mmsis[
            start:start + chunk_size
        ]


        placeholders = ",".join(
            ["?"] * len(chunk)
        )


        query = f"""
            SELECT
                mmsi,
                latitude,
                longitude,
                timestamp

            FROM (

                SELECT

                    mmsi,
                    latitude,
                    longitude,
                    timestamp,

                    ROW_NUMBER() OVER (

                        PARTITION BY mmsi

                        ORDER BY timestamp DESC

                    ) AS row_number

                FROM positions

                WHERE mmsi IN ({placeholders})

            )

            WHERE row_number <= ?

            ORDER BY
                mmsi,
                timestamp ASC
        """


        parameters = (
            list(chunk)
            + [limit]
        )


        cursor.execute(
            query,
            parameters
        )


        rows = cursor.fetchall()


        for row in rows:

            mmsi = row["mmsi"]


            if mmsi not in histories:

                histories[mmsi] = []


            histories[mmsi].append(

                {

                    "lat": row["latitude"],

                    "lon": row["longitude"],

                    "timestamp": row["timestamp"]

                }

            )


    connection.close()


    return histories


# ============================================================
# GET TRAFFIC DENSITY CELLS
# ============================================================

def get_traffic_density(
    hours=24,
    grid_size=2.0,
    max_points=50000
):

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(hours=hours)
    ).isoformat()

    connection = get_connection()

    try:

        rows = connection.execute(
            """
            SELECT mmsi, latitude, longitude
            FROM positions
            WHERE timestamp >= ?
              AND latitude BETWEEN -90 AND 90
              AND longitude BETWEEN -180 AND 180
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (cutoff, max_points)
        ).fetchall()

    finally:

        connection.close()


    cells = {}
    vessels = set()

    for row in rows:

        latitude = float(row["latitude"])
        longitude = float(row["longitude"])

        latitude_cell = floor(latitude / grid_size) * grid_size
        longitude_cell = floor(longitude / grid_size) * grid_size
        key = (latitude_cell, longitude_cell)

        cell = cells.setdefault(
            key,
            {
                "latitude": round(
                    latitude_cell + grid_size / 2,
                    4
                ),
                "longitude": round(
                    longitude_cell + grid_size / 2,
                    4
                ),
                "intensity": 0,
                "vessels": set()
            }
        )

        cell["intensity"] += 1
        cell["vessels"].add(str(row["mmsi"]))
        vessels.add(str(row["mmsi"]))


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
                "vessels": len(cell["vessels"])
            }
            for cell in cells.values()
        ]
    }


# ============================================================
# GET DESTINATION / PORT ANALYSIS
# ============================================================

def get_destination_analysis():

    connection = get_connection()

    try:

        rows = connection.execute(
            """
            SELECT destination, COUNT(*) AS vessel_count
            FROM vessel_metadata
            WHERE destination IS NOT NULL
              AND TRIM(destination) != ''
            GROUP BY destination
            ORDER BY vessel_count DESC, destination ASC
            """
        ).fetchall()

    finally:

        connection.close()


    return [
        {
            "destination": row["destination"],
            "vessel_count": row["vessel_count"]
        }
        for row in rows
    ]


# ============================================================
# GET VESSEL HISTORY
# ============================================================

def get_vessel_history(
    mmsi,
    limit=20
):

    connection = get_connection()

    cursor = connection.cursor()


    cursor.execute(
        """
        SELECT *

        FROM positions

        WHERE mmsi = ?

        ORDER BY timestamp DESC

        LIMIT ?
        """,
        (
            mmsi,
            limit
        )
    )


    rows = cursor.fetchall()

    connection.close()


    # --------------------------------------------------------
    # Reverse to oldest → newest
    # --------------------------------------------------------

    history = [
        dict(row)
        for row in rows
    ]

    history.reverse()


    return history


# ============================================================
# DATABASE TEST
# ============================================================

if __name__ == "__main__":

    # --------------------------------------------------------
    # Initialize database
    # --------------------------------------------------------

    initialize_database()

    print(
        "Database initialized successfully."
    )

    print(
        f"Database location: {DB_PATH}"
    )


    # --------------------------------------------------------
    # Remove old test data
    # --------------------------------------------------------

    connection = get_connection()

    cursor = connection.cursor()


    cursor.execute(
        "DELETE FROM positions WHERE mmsi = ?",
        ("TEST123456",)
    )


    cursor.execute(
        "DELETE FROM vessels WHERE mmsi = ?",
        ("TEST123456",)
    )


    connection.commit()

    connection.close()


    # ========================================================
    # TEST MOVEMENT HISTORY
    # ========================================================

    save_vessel(

        mmsi="TEST123456",

        latitude=26.9124,

        longitude=75.7873,

        sog=12.5,

        cog=185.4,

        heading=183.0,

        timestamp="2026-09-19T12:00:00Z",

        received_at="2026-09-19T12:00:01Z"

    )


    save_vessel(

        mmsi="TEST123456",

        latitude=26.9140,

        longitude=75.7900,

        sog=12.7,

        cog=184.8,

        heading=183.0,

        timestamp="2026-09-19T12:01:00Z",

        received_at="2026-09-19T12:01:01Z"

    )


    save_vessel(

        mmsi="TEST123456",

        latitude=26.9160,

        longitude=75.7930,

        sog=12.9,

        cog=184.2,

        heading=182.0,

        timestamp="2026-09-19T12:02:00Z",

        received_at="2026-09-19T12:02:01Z"

    )


    print(
        "\nThree test positions saved."
    )


    # ========================================================
    # READ CURRENT VESSELS
    # ========================================================

    vessels = get_current_vessels()


    print(
        "\nCurrent vessels:"
    )


    for vessel in vessels:

        print(vessel)


    # ========================================================
    # READ HISTORY
    # ========================================================

    history = get_vessel_history(

        "TEST123456",

        limit=20

    )


    print(
        "\nTest vessel history:"
    )


    for position in history:

        print(position)
