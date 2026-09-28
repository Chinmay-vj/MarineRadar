import sqlite3
from pathlib import Path

DB_PATH = Path("data") / "ships.db"


def main():
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)

    try:
        # Count bad historical positions
        before = conn.execute("""
            SELECT COUNT(*)
            FROM positions
            WHERE latitude = 0
              AND longitude = 0
        """).fetchone()[0]

        print(f"Bad (0,0) position records found: {before}")

        if before == 0:
            print("Nothing to clean.")
            return

        # Remove invalid historical positions
        conn.execute("""
            DELETE FROM positions
            WHERE latitude = 0
              AND longitude = 0
        """)

        # Remove vessels whose current position is (0,0)
        conn.execute("""
            DELETE FROM vessels
            WHERE latitude = 0
              AND longitude = 0
        """)

        conn.commit()

        after = conn.execute("""
            SELECT COUNT(*)
            FROM positions
            WHERE latitude = 0
              AND longitude = 0
        """).fetchone()[0]

        print(f"Bad records remaining: {after}")
        print("Cleanup completed successfully.")

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


if __name__ == "__main__":
    main()