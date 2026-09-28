import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from intelligence import (
    analyze_historical_track,
    analyze_historical_route,
    analyze_maritime_fleet,
    detect_maritime_anomalies,
    analyze_voyage,
    analyze_vessel,
    build_vessel_profile,
    estimate_voyage_route,
    parse_voyage_eta,
    predict_eta,
    predict_voyage_eta,
)
from ml_prediction import fit_eta_model, predict_eta_with_model
import database


class IntelligenceTests(unittest.TestCase):

    def setUp(self):
        self.now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)

    def test_predict_eta_uses_distance_and_speed(self):
        prediction = predict_eta(0, 0, 0, 1, 10, self.now)

        self.assertAlmostEqual(prediction["hours"], 6.0, delta=0.1)
        predicted_eta = datetime.fromisoformat(prediction["eta"])
        self.assertAlmostEqual(
            (predicted_eta - self.now).total_seconds() / 3600,
            6.0,
            delta=0.05,
        )

    def test_impossible_jump_creates_critical_alert(self):
        result = analyze_vessel(
            {
                "mmsi": "123",
                "latitude": 0,
                "longitude": 1,
                "sog": 10,
                "last_seen": self.now.isoformat(),
            },
            [
                {"lat": 0, "lon": 0, "timestamp": "2026-09-23T11:59:00+00:00"},
                {"lat": 0, "lon": 1, "timestamp": "2026-09-23T12:00:00+00:00"},
            ],
            self.now,
        )

        self.assertEqual(result["highest_severity"], "critical")
        self.assertEqual(result["alerts"][0]["code"], "impossible_jump")

    def test_old_signal_creates_stale_alert(self):
        result = analyze_vessel(
            {
                "mmsi": "456",
                "last_seen": "2026-09-23T10:00:00+00:00",
            },
            now=self.now,
        )

        self.assertEqual(result["alerts"][0]["code"], "stale_critical")

    def test_vessel_profile_contains_health_state_and_identity(self):
        vessel = {
            "mmsi": "789",
            "shipname": "TEST CARRIER",
            "imo": "1234567",
            "callsign": "TST1",
            "shiptype": 70,
            "destination": "SINGAPORE",
            "latitude": 10.0,
            "longitude": 20.0,
            "sog": 12.0,
            "cog": 90.0,
            "last_seen": self.now.isoformat(),
        }

        profile = build_vessel_profile(
            vessel,
            history=[
                {"lat": 9.998, "lon": 19.998, "timestamp": "2026-09-23T11:58:00+00:00"},
                {"lat": 9.999, "lon": 19.999, "timestamp": "2026-09-23T11:59:00+00:00"},
                {"lat": 10.0, "lon": 20.0, "timestamp": self.now.isoformat()},
            ],
            now=self.now,
        )

        self.assertEqual(profile["identity"]["ship_type"], "Cargo")
        self.assertEqual(profile["operational_state"], "underway")
        self.assertEqual(profile["health"]["band"], "healthy")
        self.assertEqual(profile["identity"]["identity_completeness"], 100)

    def test_historical_track_reports_distance_gaps_and_trend(self):
        history = [
            {"lat": 0.0, "lon": 0.0, "timestamp": "2026-09-23T10:00:00+00:00"},
            {"lat": 0.01, "lon": 0.0, "timestamp": "2026-09-23T10:10:00+00:00"},
            {"lat": 0.02, "lon": 0.0, "timestamp": "2026-09-23T11:10:00+00:00"},
            {"lat": 0.08, "lon": 0.0, "timestamp": "2026-09-23T11:20:00+00:00"},
        ]

        result = analyze_historical_track(history)

        self.assertEqual(result["observation_count"], 4)
        self.assertEqual(result["gap_count"], 1)
        self.assertEqual(result["segment_count"], 2)
        self.assertEqual(result["speed_trend"], "accelerating")
        self.assertEqual(result["movement_state"], "moving")
        self.assertEqual(result["coverage_score"], 85)

    def test_voyage_intelligence_parses_ais_eta_and_state(self):
        vessel = {
            "mmsi": "999",
            "destination": "  singapore  ",
            "eta": "09241830",
            "sog": 12.0,
        }
        history = [
            {"lat": 1.0, "lon": 1.0, "timestamp": "2026-09-23T17:00:00+00:00"},
            {"lat": 1.01, "lon": 1.0, "timestamp": "2026-09-23T17:10:00+00:00"},
        ]

        result = analyze_voyage(
            vessel,
            analyze_historical_track(history, self.now),
            self.now,
        )

        self.assertEqual(
            parse_voyage_eta("09241830", self.now).isoformat(),
            "2026-09-24T18:30:00+00:00",
        )
        self.assertEqual(result["destination"], "SINGAPORE")
        self.assertEqual(result["voyage_state"], "underway_to_destination")
        self.assertEqual(result["eta_status"], "reported")
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["missing_signals"], [])

    def test_historical_route_reports_geometry_and_turns(self):
        result = analyze_historical_route([
            {"lat": 0.0, "lon": 0.0, "timestamp": "2026-09-23T10:00:00+00:00"},
            {"lat": 0.0, "lon": 0.1, "timestamp": "2026-09-23T10:10:00+00:00"},
            {"lat": 0.1, "lon": 0.1, "timestamp": "2026-09-23T10:20:00+00:00"},
        ])

        self.assertEqual(result["point_count"], 3)
        self.assertEqual(result["segment_count"], 1)
        self.assertEqual(result["route_shape"], "meandering")
        self.assertEqual(result["dominant_direction"], "NE")
        self.assertEqual(result["turn_count"], 1)
        self.assertEqual(len(result["segments"]), 1)

    def test_estimated_voyage_route_returns_great_circle_waypoints(self):
        result = estimate_voyage_route(0, 0, 0, 1, waypoint_count=5)

        self.assertEqual(result["method"], "great_circle_interpolation")
        self.assertFalse(result["land_aware"])
        self.assertEqual(result["waypoint_count"], 5)
        self.assertEqual(result["waypoints"][0], {"latitude": 0.0, "longitude": 0.0})
        self.assertEqual(result["waypoints"][-1], {"latitude": 0.0, "longitude": 1.0})
        self.assertAlmostEqual(result["distance_nm"], 60.04, delta=0.1)
        self.assertEqual(result["direction"], "E")

    def test_voyage_eta_prediction_compares_with_reported_eta(self):
        result = predict_voyage_eta(
            {
                "latitude": 0.0,
                "longitude": 0.0,
                "sog": 10.0,
                "eta": "09232000",
                "last_seen": self.now.isoformat(),
            },
            0.0,
            1.0,
            history=[
                {"lat": 0.0, "lon": 0.0, "timestamp": "2026-09-23T11:50:00+00:00"},
                {"lat": 0.0, "lon": 0.01, "timestamp": "2026-09-23T11:55:00+00:00"},
                {"lat": 0.0, "lon": 0.02, "timestamp": self.now.isoformat()},
            ],
            now=self.now,
        )

        self.assertEqual(result["status"], "predicted")
        self.assertEqual(result["speed_source"], "current AIS SOG")
        self.assertEqual(result["schedule_status"], "predicted_early")
        self.assertAlmostEqual(result["duration_hours"], 6.0, delta=0.1)
        self.assertEqual(result["confidence"], "high")

    def test_learned_eta_model_trains_and_predicts(self):
        histories = {
            "one": [
                {
                    "lat": 0.0,
                    "lon": index * 0.01,
                    "sog": 6.0,
                    "timestamp": f"2026-09-23T10:{index:02d}:00+00:00",
                }
                for index in range(8)
            ],
            "two": [
                {
                    "lat": 1.0,
                    "lon": index * 0.01,
                    "sog": 8.0,
                    "timestamp": f"2026-09-23T11:{index:02d}:00+00:00",
                }
                for index in range(8)
            ],
        }

        model = fit_eta_model(histories)
        prediction = predict_eta_with_model(
            model,
            distance_nm=60.0,
            speed_knots=8.0,
            timestamp=self.now,
        )

        self.assertEqual(model["status"], "trained")
        self.assertGreaterEqual(model["training_rows"], 10)
        self.assertIsNotNone(prediction)
        self.assertGreater(prediction["duration_hours"], 0)

    def test_maritime_anomaly_detector_reports_explainable_risk(self):
        result = detect_maritime_anomalies(
            {
                "mmsi": "1000",
                "latitude": 0.0,
                "longitude": 1.0,
                "sog": 10.0,
                "cog": 180.0,
                "heading": 90.0,
                "last_seen": self.now.isoformat(),
            },
            history=[
                {"lat": 0.0, "lon": 0.0, "timestamp": "2026-09-23T11:59:00+00:00"},
                {"lat": 0.0, "lon": 1.0, "timestamp": self.now.isoformat()},
            ],
            now=self.now,
        )

        codes = {item["code"] for item in result["anomalies"]}
        self.assertIn("impossible_jump", codes)
        self.assertIn("course_heading_mismatch", codes)
        self.assertEqual(result["risk_band"], "critical")
        self.assertTrue(all(item["evidence"] for item in result["anomalies"]))

    def test_maritime_fleet_analytics_aggregates_operational_metrics(self):
        vessels = [
            {
                "mmsi": "2001", "shipname": "A", "shiptype": 70,
                "destination": "PORT A", "latitude": 1.0,
                "longitude": 2.0, "sog": 10.0, "cog": 90.0,
                "last_seen": self.now.isoformat(),
            },
            {
                "mmsi": "2002", "shipname": "B", "shiptype": 80,
                "destination": "PORT B", "latitude": 3.0,
                "longitude": 4.0, "sog": 0.0, "cog": 0.0,
                "last_seen": self.now.isoformat(),
            },
        ]
        histories = {
            "2001": [
                {"lat": 1.0, "lon": 2.0, "timestamp": "2026-09-23T11:50:00+00:00"},
                {"lat": 1.01, "lon": 2.0, "timestamp": self.now.isoformat()},
            ],
            "2002": [],
        }

        result = analyze_maritime_fleet(
            vessels,
            histories,
            {"hours": 24, "cells": [{"latitude": 1, "longitude": 2, "intensity": 4}]},
            self.now,
        )

        self.assertEqual(result["vessel_count"], 2)
        self.assertEqual(result["moving_vessel_count"], 1)
        self.assertEqual(result["average_speed_knots"], 5.0)
        self.assertEqual(result["traffic_window_hours"], 24)
        self.assertEqual(result["ship_types"][0]["vessel_count"], 1)
        self.assertEqual(len(result["hotspots"]), 1)


if __name__ == "__main__":
    unittest.main()