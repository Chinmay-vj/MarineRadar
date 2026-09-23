import os
import unittest
from pathlib import Path

from postgres_backend import (
    SCHEMA_PATH,
    check_postgres_health,
    find_vessels_nearby,
    postgres_dsn,
    _timestamp,
)
import postgres_backend
import database


class PostgresBackendTests(unittest.TestCase):

    def test_schema_contains_postgis_geometry_and_spatial_indexes(self):
        schema = SCHEMA_PATH.read_text(encoding="utf-8")

        self.assertIn("CREATE EXTENSION IF NOT EXISTS postgis", schema)
        self.assertIn("GEOGRAPHY(Point, 4326)", schema)
        self.assertIn("USING GIST (geom)", schema)
        self.assertIn("ST_SetSRID", schema)
        self.assertIn("ST_DWithin", postgres_backend.find_vessels_nearby.__doc__ + postgres_backend.Path("postgres_backend.py").read_text(encoding="utf-8"))

    def test_spatial_query_requires_optional_postgres_runtime(self):
        if postgres_dsn():
            self.skipTest("PostgreSQL is configured in this environment")
        with self.assertRaises(RuntimeError):
            find_vessels_nearby(0, 0)

    def test_health_check_reports_unconfigured_when_no_dsn(self):
        result = check_postgres_health(dsn="")
        self.assertEqual(result["status"], "unconfigured")

    def test_timestamp_normalizes_utc_z(self):
        self.assertEqual(_timestamp("2026-09-23T18:00:00Z"), "2026-09-23T18:00:00+00:00")
        self.assertEqual(_timestamp("2026-09-23T18:00:00+00:00"), "2026-09-23T18:00:00+00:00")
        self.assertIsNone(_timestamp(None))

    def test_postgres_dsn_resolution(self):
        original_db_url = os.environ.get("DATABASE_URL")
        original_pg_dsn = os.environ.get("POSTGRES_DSN")
        try:
            os.environ["DATABASE_URL"] = "postgresql://test:test@localhost:5432/test"
            self.assertEqual(postgres_dsn(), "postgresql://test:test@localhost:5432/test")
            del os.environ["DATABASE_URL"]
            os.environ["POSTGRES_DSN"] = "postgresql://test2:test2@localhost:5432/test2"
            self.assertEqual(postgres_dsn(), "postgresql://test2:test2@localhost:5432/test2")
        finally:
            if original_db_url is not None:
                os.environ["DATABASE_URL"] = original_db_url
            elif "DATABASE_URL" in os.environ:
                del os.environ["DATABASE_URL"]
            if original_pg_dsn is not None:
                os.environ["POSTGRES_DSN"] = original_pg_dsn
            elif "POSTGRES_DSN" in os.environ:
                del os.environ["POSTGRES_DSN"]

    def test_database_dual_engine_fallback_when_dsn_absent(self):
        # When no DSN is provided, database functions default to SQLite without error
        if not postgres_dsn():
            vessels = database.get_current_vessels(limit=5)
            self.assertIsInstance(vessels, list)


if __name__ == "__main__":
    unittest.main()
