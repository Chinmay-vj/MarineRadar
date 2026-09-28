import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("AISSTREAM_API_KEY", "test-key")

import database
from stream_client import (
    normalize_aisstream_position,
    normalize_aisstream_static,
    process_position,
    process_static_metadata,
)


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

    def test_aisstream_position_and_static_envelopes_normalize(self):

        position_event = {
            "MessageType": "PositionReport",
            "MetaData": {
                "MMSI": 123456789,
                "time_utc": "2026-09-23 12:00:00.000000000 +0000 UTC",
            },
            "Message": {
                "PositionReport": {
                    "UserID": 123456789,
                    "Latitude": 12.5,
                    "Longitude": 77.5,
                    "Sog": 10.0,
                    "Cog": 90.0,
                    "TrueHeading": 91,
                }
            },
        }
        vessel = process_position(normalize_aisstream_position(position_event))

        self.assertEqual(vessel["mmsi"], "123456789")
        self.assertEqual(vessel["lat"], 12.5)
        self.assertEqual(vessel["timestamp"], "2026-09-23T12:00:00+00:00")

        static_event = {
            "MessageType": "ShipStaticData",
            "MetaData": {
                "MMSI": 123456789,
                "time_utc": "2026-09-23 12:00:00.000000000 +0000 UTC",
            },
            "Message": {
                "ShipStaticData": {
                    "UserID": 123456789,
                    "ImoNumber": 9876543,
                    "CallSign": "TEST1",
                    "Name": "EXAMPLE VOYAGER",
                    "Type": 70,
                    "Destination": "SINGAPORE",
                    "MaximumStaticDraught": 12.5,
                    "Eta": {"Month": 10, "Day": 5, "Hour": 14, "Minute": 30},
                    "Dimension": {
                        "A": 100,
                        "B": 120,
                        "C": 15,
                        "D": 17,
                    },
                }
            },
        }
        metadata = process_static_metadata(normalize_aisstream_static(static_event))

        self.assertEqual(metadata["imo"], 9876543)
        self.assertEqual(metadata["destination"], "SINGAPORE")
        self.assertEqual(metadata["eta"], "10051430")
        self.assertEqual(metadata["length"], 220.0)
        self.assertEqual(metadata["beam"], 32.0)
        self.assertEqual(metadata["static_source"], "aisstream")
        self.assertEqual(
            metadata["last_static_update"],
            "2026-09-23T12:00:00+00:00",
        )

    def test_aisstream_class_b_static_report_normalizes_both_parts(self):

        static_event = {
            "MessageType": "StaticDataReport",
            "MetaData": {"MMSI": 123456789},
            "Message": {
                "StaticDataReport": {
                    "UserID": 123456789,
                    "ReportA": {"Name": "CLASS B VESSEL"},
                    "ReportB": {
                        "ShipType": 60,
                        "CallSign": "CLASSB1",
                        "Dimension": {"A": 20, "B": 25, "C": 4, "D": 5},
                    },
                }
            },
        }
        metadata = process_static_metadata(normalize_aisstream_static(static_event))

        self.assertEqual(metadata["shipname"], "CLASS B VESSEL")
        self.assertEqual(metadata["callsign"], "CLASSB1")
        self.assertEqual(metadata["shiptype"], 60)
        self.assertEqual(metadata["length"], 45.0)
        self.assertEqual(metadata["beam"], 9.0)


if __name__ == "__main__":

    unittest.main()
