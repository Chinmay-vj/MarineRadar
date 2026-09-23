import tempfile
import unittest
from pathlib import Path

import database


class AlertLifecycleTests(unittest.TestCase):

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

    def test_alerts_deduplicate_acknowledge_and_resolve(self):
        record = {
            "mmsi": "123",
            "anomalies": [{
                "code": "test_anomaly",
                "severity": "warning",
                "score": 40,
                "title": "Test anomaly",
                "evidence": "Test evidence",
                "source": "test",
            }],
        }
        database.sync_maritime_alerts([record])
        database.sync_maritime_alerts([record])

        alerts = database.get_maritime_alerts()
        self.assertEqual(len(alerts), 1)
        database.sync_maritime_alerts([])
        self.assertEqual(database.get_maritime_alerts(status="active"), [])
        self.assertEqual(len(database.get_maritime_alerts(status="resolved")), 1)

        database.sync_maritime_alerts([record])
        alerts = database.get_maritime_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertTrue(database.acknowledge_maritime_alert(alerts[0]["id"]))
        self.assertEqual(database.get_maritime_alerts(status="active"), [])
        self.assertEqual(
            len(database.get_maritime_alerts(status="acknowledged")),
            1,
        )

        self.assertEqual(len(database.get_maritime_alerts(status="all")), 2)


if __name__ == "__main__":
    unittest.main()