import sqlite3
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
        DB_PATH
    )

    connection.row_factory = sqlite3.Row

    return connection


# ============================================================
# INITIALIZE DATABASE
# ============================================================

def initialize_database():

    DATA_DIR.mkdir(
        exist_ok=True
    )

    connection = get_connection()

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
# GET CURRENT VESSELS
# ============================================================

def get_current_vessels(limit=100):

    connection = get_connection()

    cursor = connection.cursor()


    if limit is None:

        cursor.execute(
            """
            SELECT *

            FROM vessels

            ORDER BY updated_at DESC
            """
        )

    else:

        cursor.execute(
            """
            SELECT *

            FROM vessels

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