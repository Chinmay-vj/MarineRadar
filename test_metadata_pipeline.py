import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("PELYR_API_KEY", "test-key")

import database
from stream_client import process_static_metadata


class MetadataPipelineTests(unittest.TestCase):

    def setUp(self):

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_data_dir = database.DATA_DIR
        self.original_db_path = database.DB_PATH

        database.DATA_DIR = Path(self.temporary_directory.name)
        database.DB_PATH = database.DATA_DIR / "ships.db"
        database.initialize_database()

    def tearDown(self):

        database.DATA_DIR = self.original_data_dir
        database.DB_PATH = self.original_db_path
        self.temporary_directory.cleanup()

    def test_static_metadata_merges_and_is_exposed_with_position(self):

        database.save_vessels_batch(
            [
                {
                    "mmsi": "123456789",
                    "lat": 12.5,
                    "lon": 77.5,
                    "sog": 10.0,
                    "cog": 90.0,
                    "heading": 91.0,
                    "timestamp": "2026-09-23T12:00:00+00:00",
                    "received_at": "2026-09-23T12:00:01+00:00",
                }
            ]
        )

        database.save_vessel_metadata_batch(
            [
                {
                    "mmsi": "123456789",
                    "imo": "9876543",
                    "shipname": "EXAMPLE VOYAGER",
                    "destination": "SINGAPORE",
                    "length": 220.0,
                    "beam": 32.0,
                    "last_static_update": "2026-09-23T12:00:00+00:00",
                    "static_source": "test",
                }
            ]
        )

        # A partial type-24 update must not erase static fields
        # received previously in a type-5 message.
        database.save_vessel_metadata_batch(
            [
                {
                    "mmsi": "123456789",
                    "callsign": "TEST1",
                    "last_static_update": "2026-09-23T12:01:00+00:00",
                    "static_source": "test",
                }
            ]
        )

        vessel = database.get_current_vessels(limit=1)[0]

        self.assertEqual(vessel["shipname"], "EXAMPLE VOYAGER")
        self.assertEqual(vessel["destination"], "SINGAPORE")
        self.assertEqual(vessel["callsign"], "TEST1")
        self.assertEqual(vessel["length"], 220.0)

    def test_static_message_normalizes_dimensions(self):

        metadata = process_static_metadata(
            {
                "mmsi": 123456789,
                "shipname": "  Example Voyager  ",
                "dim_a": 100,
                "dim_b": 120,
                "dim_c": 15,
                "dim_d": 17,
            }
        )

        self.assertEqual(metadata["mmsi"], "123456789")
        self.assertEqual(metadata["shipname"], "Example Voyager")
        self.assertEqual(metadata["length"], 220.0)
        self.assertEqual(metadata["beam"], 32.0)


if __name__ == "__main__":

    unittest.main()
