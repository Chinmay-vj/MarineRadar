"""Migrate the local SQLite store into PostgreSQL/PostGIS."""

import argparse

from database import DB_PATH
from postgres_backend import migrate_sqlite_to_postgres


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", default=str(DB_PATH))
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--batch-size", type=int, default=1000)
    arguments = parser.parse_args()
    migrate_sqlite_to_postgres(
        arguments.sqlite,
        dsn=arguments.dsn,
        batch_size=arguments.batch_size,
    )
    print("SQLite to PostgreSQL/PostGIS migration completed.")


if __name__ == "__main__":
    main()