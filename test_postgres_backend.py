import unittest
from pathlib import Path

from postgres_backend import SCHEMA_PATH, find_vessels_nearby, postgres_dsn
import postgres_backend


class PostgresBackendTests(unittest.TestCase):

    def test_schema_contains_postgis_geometry_and_spatial_indexes(self):
        schema = SCHEMA_PATH.read_text(encoding="utf-8")

        self.assertIn("CREATE EXTENSION IF NOT EXISTS postgis", schema)
        self.assertIn("GEOGRAPHY(Point, 4326)", schema)
        self.assertIn("USING GIST (geom)", schema)
        self.assertIn("ST_SetSRID", schema)
        self.assertIn("ST_DWithin", postgres_backend.find_vessels_nearby.__doc__ + postgres_backend.Path("postgres_backend.py").read_text())

    def test_spatial_query_requires_optional_postgres_runtime(self):
        if postgres_dsn():
            self.skipTest("PostgreSQL is configured in this environment")
        with self.assertRaises(RuntimeError):
            find_vessels_nearby(0, 0)


if __name__ == "__main__":
    unittest.main()
