import sqlite3
from pathlib import Path


# ============================================================
# GLOBAL SHIP TRACKER
# 11D-1 — VESSEL METADATA SCHEMA MIGRATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "ships.db"


def main():

    print("=" * 70)
    print("GLOBAL SHIP TRACKER - 11D-1 METADATA MIGRATION")
    print("=" * 70)
    print()

    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Database not found: {DB_PATH}"
        )

    connection = sqlite3.connect(
        DB_PATH,
        timeout=30
    )

    try:

        # ----------------------------------------------------
        # Enable WAL for safe concurrent map/stream access.
        # ----------------------------------------------------

        connection.execute(
            "PRAGMA journal_mode=WAL"
        )

        connection.execute(
            "PRAGMA busy_timeout=30000"
        )


        # ----------------------------------------------------
        # Create normalized vessel metadata table.
        #
        # This is deliberately separate from `vessels`.
        #
        # `vessels` = current position/state
        # `positions` = historical positions
        # `vessel_metadata` = identity/static/voyage data
        #
        # This lets AIS static messages arrive in separate
        # type-5 / type-24 frames and be merged field-by-field.
        # ----------------------------------------------------

        connection.execute("""
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
        """)


        # ----------------------------------------------------
        # Useful indexes.
        # ----------------------------------------------------

        connection.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_vessel_metadata_shipname
            ON vessel_metadata (shipname)
        """)


        connection.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_vessel_metadata_imo
            ON vessel_metadata (imo)
        """)


        connection.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_vessel_metadata_destination
            ON vessel_metadata (destination)
        """)


        connection.commit()


        # ----------------------------------------------------
        # Verify schema.
        # ----------------------------------------------------

        columns = connection.execute("""
            PRAGMA table_info(vessel_metadata)
        """).fetchall()


        print(
            f"Database: {DB_PATH}"
        )

        print(
            f"Metadata table columns: {len(columns)}"
        )

        print()

        print("Columns:")

        for column in columns:

            print(
                f"  {column[1]}"
            )


        row_count = connection.execute("""
            SELECT COUNT(*)
            FROM vessel_metadata
        """).fetchone()[0]


        print()

        print(
            f"Existing metadata records: {row_count:,}"
        )

        print()

        print("=" * 70)
        print("11D-1 MIGRATION COMPLETE")
        print("=" * 70)
        print()
        print(
            "No existing vessel-position/history records were modified."
        )
        print(
            "The metadata table is ready for the Pelyr static AIS stream."
        )


    except Exception:

        connection.rollback()

        print()
        print(
            "ERROR: Migration failed. Transaction rolled back."
        )

        raise


    finally:

        connection.close()


if __name__ == "__main__":

    main()
