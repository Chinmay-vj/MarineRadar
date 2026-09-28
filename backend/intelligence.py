"""Deterministic ETA and vessel-health analytics for the tracker."""

from datetime import datetime, timedelta, timezone
from math import asin, atan2, cos, degrees, radians, sin, sqrt
import re


MAX_TRACK_SPEED_KNOTS = 50.0
STALE_AFTER = timedelta(minutes=30)
CRITICAL_STALE_AFTER = timedelta(hours=2)

SHIP_TYPE_NAMES = {
    20: "Wing in ground",
    30: "Fishing",
    31: "Towing",
    32: "Towing, length exceeds 200m or breadth exceeds 25m",
    33: "Dredging or underwater operations",
    34: "Diving operations",
    35: "Military operations",
    36: "Sailing",
    37: "Pleasure craft",
    40: "High-speed craft",
    50: "Pilot vessel",
    51: "Search and rescue vessel",
    52: "Tug",
    53: "Port tender",
    54: "Anti-pollution equipment",
    55: "Law enforcement",
    58: "Medical transport",
    60: "Passenger",
    70: "Cargo",
    80: "Tanker",
    90: "Other type",
}


def parse_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_voyage_eta(value, now=None):
    """Parse ISO timestamps and the AIS MMDDHHMM UTC ETA format."""
    if not value:
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    parsed = parse_timestamp(text)
    if parsed is not None:
        return parsed

    compact = re.sub(r"\D", "", text)
    if len(compact) != 8:
        return None
    try:
        candidate = datetime(
            now.year,
            int(compact[0:2]),
            int(compact[2:4]),
            int(compact[4:6]),
            int(compact[6:8]),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None
    if candidate < now - timedelta(days=180):
        candidate = candidate.replace(year=now.year + 1)
    return candidate


def haversine_nm(latitude1, longitude1, latitude2, longitude2):
    """Return great-circle distance in nautical miles."""
    earth_radius_nm = 3440.065
    lat1 = radians(float(latitude1))
    lat2 = radians(float(latitude2))
    delta_lat = radians(float(latitude2) - float(latitude1))
    delta_lon = radians(float(longitude2) - float(longitude1))
    value = (
        sin(delta_lat / 2) ** 2
        + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
    )
    return earth_radius_nm * 2 * asin(sqrt(min(1.0, max(0.0, value))))


def estimate_voyage_route(
    latitude,
    longitude,
    destination_latitude,
    destination_longitude,
    waypoint_count=12,
):
    """Create an estimated great-circle route between two coordinates."""
    try:
        start_latitude = float(latitude)
        start_longitude = float(longitude)
        end_latitude = float(destination_latitude)
        end_longitude = float(destination_longitude)
        waypoint_count = max(2, min(50, int(waypoint_count)))
    except (TypeError, ValueError):
        return None

    if not (
        -90 <= start_latitude <= 90
        and -180 <= start_longitude <= 180
        and -90 <= end_latitude <= 90
        and -180 <= end_longitude <= 180
    ):
        return None

    start = {
        "lat": start_latitude,
        "lon": start_longitude,
    }
    end = {
        "lat": end_latitude,
        "lon": end_longitude,
    }
    distance = haversine_nm(
        start_latitude,
        start_longitude,
        end_latitude,
        end_longitude,
    )
    angular_distance = distance / 3440.065
    start_latitude_radians = radians(start_latitude)
    end_latitude_radians = radians(end_latitude)
    start_longitude_radians = radians(start_longitude)
    end_longitude_radians = radians(end_longitude)

    waypoints = []
    if angular_distance == 0:
        waypoints = [
            {"latitude": start_latitude, "longitude": start_longitude}
            for _ in range(waypoint_count)
        ]
    else:
        denominator = sin(angular_distance)
        for index in range(waypoint_count):
            fraction = index / (waypoint_count - 1)
            first_weight = sin((1 - fraction) * angular_distance) / denominator
            second_weight = sin(fraction * angular_distance) / denominator
            x = (
                first_weight * cos(start_latitude_radians) * cos(start_longitude_radians)
                + second_weight * cos(end_latitude_radians) * cos(end_longitude_radians)
            )
            y = (
                first_weight * cos(start_latitude_radians) * sin(start_longitude_radians)
                + second_weight * cos(end_latitude_radians) * sin(end_longitude_radians)
            )
            z = (
                first_weight * sin(start_latitude_radians)
                + second_weight * sin(end_latitude_radians)
            )
            interpolated_latitude = degrees(atan2(z, sqrt(x * x + y * y)))
            interpolated_longitude = degrees(atan2(y, x))
            waypoints.append({
                "latitude": round(interpolated_latitude, 6),
                "longitude": round(interpolated_longitude, 6),
            })

    bearing = _route_bearing(start, end) if distance else None
    return {
        "method": "great_circle_interpolation",
        "land_aware": False,
        "limitations": [
            "Does not avoid land, restricted waters, or traffic separation schemes.",
            "Waypoints are geometric estimates, not observed AIS positions.",
        ],
        "start": waypoints[0],
        "destination": waypoints[-1],
        "distance_nm": round(distance, 2),
        "initial_bearing_degrees": None if bearing is None else round(bearing, 1),
        "direction": "unknown" if bearing is None else _cardinal_direction(bearing),
        "waypoint_count": len(waypoints),
        "waypoints": waypoints,
    }


def analyze_historical_track(history, now=None):
    """Summarize observed AIS history without interpolating missing points."""
    points = []
    for point in history or []:
        timestamp = parse_timestamp(point.get("timestamp"))
        try:
            latitude = float(point.get("lat", point.get("latitude")))
            longitude = float(point.get("lon", point.get("longitude")))
        except (TypeError, ValueError):
            continue
        if timestamp is not None:
            points.append({
                "timestamp": timestamp,
                "lat": latitude,
                "lon": longitude,
            })

    points.sort(key=lambda point: point["timestamp"])
    if not points:
        return {
            "observation_count": 0,
            "first_observed": None,
            "last_observed": None,
            "span_hours": 0.0,
            "continuous_hours": 0.0,
            "gap_count": 0,
            "max_gap_minutes": 0.0,
            "segment_count": 0,
            "distance_nm": 0.0,
            "average_speed_knots": None,
            "speed_trend": "unknown",
            "movement_state": "unknown",
            "coverage_score": 0,
        }

    total_distance = 0.0
    continuous_hours = 0.0
    speeds = []
    gap_count = 0
    max_gap_minutes = 0.0
    segment_count = 1

    for previous, current in zip(points, points[1:]):
        elapsed_hours = (
            current["timestamp"] - previous["timestamp"]
        ).total_seconds() / 3600
        if elapsed_hours <= 0:
            continue
        gap_minutes = elapsed_hours * 60
        if gap_minutes > 30:
            gap_count += 1
            segment_count += 1
            max_gap_minutes = max(max_gap_minutes, gap_minutes)
            continue
        distance = haversine_nm(
            previous["lat"], previous["lon"],
            current["lat"], current["lon"],
        )
        speed = distance / elapsed_hours
        total_distance += distance
        continuous_hours += elapsed_hours
        speeds.append(speed)

    average_speed = (
        total_distance / continuous_hours
        if continuous_hours else None
    )
    if not speeds:
        speed_trend = "unknown"
    elif len(speeds) < 2:
        speed_trend = "stable"
    else:
        midpoint = max(1, len(speeds) // 2)
        early_speed = sum(speeds[:midpoint]) / len(speeds[:midpoint])
        recent_speed = sum(speeds[midpoint:]) / len(speeds[midpoint:])
        if recent_speed - early_speed > 2:
            speed_trend = "accelerating"
        elif early_speed - recent_speed > 2:
            speed_trend = "decelerating"
        else:
            speed_trend = "stable"

    if average_speed is None:
        movement_state = "unknown"
    elif average_speed >= 1:
        movement_state = "moving"
    else:
        movement_state = "stationary"

    span_hours = (
        points[-1]["timestamp"] - points[0]["timestamp"]
    ).total_seconds() / 3600
    coverage_score = 0 if len(points) < 2 else max(
        0,
        100 - min(75, gap_count * 15),
    )

    return {
        "observation_count": len(points),
        "first_observed": points[0]["timestamp"].isoformat(),
        "last_observed": points[-1]["timestamp"].isoformat(),
        "span_hours": round(span_hours, 2),
        "continuous_hours": round(continuous_hours, 2),
        "gap_count": gap_count,
        "max_gap_minutes": round(max_gap_minutes, 1),
        "segment_count": segment_count,
        "distance_nm": round(total_distance, 2),
        "average_speed_knots": None if average_speed is None else round(average_speed, 2),
        "speed_trend": speed_trend,
        "movement_state": movement_state,
        "coverage_score": coverage_score,
    }


def _route_bearing(first, second):
    latitude_one = radians(first["lat"])
    latitude_two = radians(second["lat"])
    longitude_delta = radians(second["lon"] - first["lon"])
    y = sin(longitude_delta) * cos(latitude_two)
    x = (
        cos(latitude_one) * sin(latitude_two)
        - sin(latitude_one) * cos(latitude_two) * cos(longitude_delta)
    )
    return (degrees(atan2(y, x)) + 360) % 360


def _cardinal_direction(bearing):
    directions = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return directions[round(bearing / 45) % len(directions)]


def analyze_historical_route(history):
    """Interpret observed route geometry while preserving AIS observation gaps."""
    points = []
    for point in history or []:
        timestamp = parse_timestamp(point.get("timestamp"))
        try:
            latitude = float(point.get("lat", point.get("latitude")))
            longitude = float(point.get("lon", point.get("longitude")))
        except (TypeError, ValueError):
            continue
        if timestamp is not None:
            points.append({
                "timestamp": timestamp,
                "lat": latitude,
                "lon": longitude,
            })
    points.sort(key=lambda point: point["timestamp"])

    if not points:
        return {
            "point_count": 0,
            "segment_count": 0,
            "segments": [],
            "distance_nm": 0.0,
            "displacement_nm": 0.0,
            "efficiency_percent": 0.0,
            "dominant_direction": "unknown",
            "bearing_degrees": None,
            "turn_count": 0,
            "route_shape": "unknown",
        }

    segments = []
    current = [points[0]]
    for previous, point in zip(points, points[1:]):
        elapsed_minutes = (
            point["timestamp"] - previous["timestamp"]
        ).total_seconds() / 60
        if elapsed_minutes > 30:
            segments.append(current)
            current = []
        current.append(point)
    segments.append(current)

    segment_summaries = []
    total_distance = 0.0
    bearings = []
    for segment in segments:
        segment_distance = 0.0
        for previous, point in zip(segment, segment[1:]):
            segment_distance += haversine_nm(
                previous["lat"], previous["lon"],
                point["lat"], point["lon"],
            )
            bearings.append(_route_bearing(previous, point))
        total_distance += segment_distance
        segment_bearing = (
            _route_bearing(segment[0], segment[-1])
            if len(segment) > 1 else None
        )
        segment_summaries.append({
            "point_count": len(segment),
            "start": {
                "latitude": segment[0]["lat"],
                "longitude": segment[0]["lon"],
                "timestamp": segment[0]["timestamp"].isoformat(),
            },
            "end": {
                "latitude": segment[-1]["lat"],
                "longitude": segment[-1]["lon"],
                "timestamp": segment[-1]["timestamp"].isoformat(),
            },
            "distance_nm": round(segment_distance, 2),
            "bearing_degrees": None if segment_bearing is None else round(segment_bearing, 1),
            "direction": "unknown" if segment_bearing is None else _cardinal_direction(segment_bearing),
        })

    first = points[0]
    last = points[-1]
    displacement = haversine_nm(first["lat"], first["lon"], last["lat"], last["lon"])
    efficiency = 0.0 if total_distance == 0 else min(100.0, displacement / total_distance * 100)
    bearing = _route_bearing(first, last) if displacement > 0 else None
    turn_count = sum(
        1 for previous, current in zip(bearings, bearings[1:])
        if abs((current - previous + 180) % 360 - 180) >= 45
    )
    if total_distance < 1:
        route_shape = "stationary"
    elif efficiency >= 85:
        route_shape = "direct"
    elif efficiency >= 45:
        route_shape = "meandering"
    else:
        route_shape = "circuitous"

    return {
        "point_count": len(points),
        "segment_count": len(segments),
        "segments": segment_summaries,
        "distance_nm": round(total_distance, 2),
        "displacement_nm": round(displacement, 2),
        "efficiency_percent": round(efficiency, 1),
        "dominant_direction": "unknown" if bearing is None else _cardinal_direction(bearing),
        "bearing_degrees": None if bearing is None else round(bearing, 1),
        "turn_count": turn_count,
        "route_shape": route_shape,
    }


def analyze_voyage(vessel, historical=None, now=None):
    """Interpret AIS destination/ETA metadata and observed voyage state."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    historical = historical or analyze_historical_track([], now)

    raw_destination = vessel.get("destination")
    destination = str(raw_destination).strip() if raw_destination else None
    destination = destination.upper() if destination else None
    reported_eta = parse_voyage_eta(vessel.get("eta"), now)
    missing_signals = []
    if not destination:
        missing_signals.append("destination")
    if reported_eta is None:
        missing_signals.append("reported_eta")
    if historical.get("observation_count", 0) < 2:
        missing_signals.append("historical_track")
    if vessel.get("sog") is None:
        missing_signals.append("speed_over_ground")

    if not destination:
        voyage_state = "destination_unknown"
    elif historical.get("movement_state") == "moving":
        voyage_state = "underway_to_destination"
    elif historical.get("movement_state") == "stationary":
        voyage_state = "stationary_with_destination"
    else:
        voyage_state = "destination_reported"

    if reported_eta is None:
        eta_status = "not_reported"
    elif now > reported_eta:
        eta_status = "overdue"
    else:
        eta_status = "reported"

    quality_score = 0
    quality_score += 40 if destination else 0
    quality_score += 30 if reported_eta else 0
    quality_score += 20 if historical.get("observation_count", 0) >= 2 else 0
    quality_score += 10 if vessel.get("sog") is not None else 0
    confidence = (
        "high" if quality_score >= 80
        else "medium" if quality_score >= 50
        else "low"
    )

    return {
        "destination": destination,
        "destination_source": "AIS static/voyage metadata" if destination else None,
        "reported_eta": reported_eta.isoformat() if reported_eta else None,
        "eta_source": "AIS voyage metadata" if reported_eta else None,
        "eta_status": eta_status,
        "voyage_state": voyage_state,
        "confidence": confidence,
        "quality_score": quality_score,
        "missing_signals": missing_signals,
        "route_progress": "unavailable_without_destination_coordinates",
    }


def predict_eta(latitude, longitude, destination_latitude, destination_longitude, speed_knots, now=None):
    """Estimate arrival using a straight-line distance and current SOG."""
    try:
        speed_knots = float(speed_knots)
        distance_nm = haversine_nm(
            latitude, longitude, destination_latitude, destination_longitude
        )
    except (TypeError, ValueError):
        return None
    if speed_knots <= 0 or distance_nm < 0:
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return {
        "distance_nm": round(distance_nm, 1),
        "hours": round(distance_nm / speed_knots, 2),
        "eta": (now + timedelta(hours=distance_nm / speed_knots)).isoformat(),
    }


def predict_voyage_eta(
    vessel,
    destination_latitude,
    destination_longitude,
    history=None,
    now=None,
    waypoint_count=12,
):
    """Predict ETA from the estimated route and the best available AIS speed."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    route = estimate_voyage_route(
        vessel.get("latitude"),
        vessel.get("longitude"),
        destination_latitude,
        destination_longitude,
        waypoint_count,
    )
    if route is None:
        return None

    historical = analyze_historical_track(history or [], now)
    speed = None
    speed_source = None
    try:
        current_speed = float(vessel.get("sog"))
        if current_speed > 0:
            speed = current_speed
            speed_source = "current AIS SOG"
    except (TypeError, ValueError):
        pass
    if speed is None and historical.get("average_speed_knots"):
        speed = historical["average_speed_knots"]
        speed_source = "historical observed average speed"

    if speed is None or speed <= 0:
        return {
            "status": "unavailable",
            "reason": "No positive current or historical speed is available.",
            "route": route,
            "speed_knots": None,
            "speed_source": None,
            "confidence": "low",
        }

    duration_hours = route["distance_nm"] / speed
    predicted_eta = now + timedelta(hours=duration_hours)
    reported_eta = parse_voyage_eta(vessel.get("eta"), now)
    delta_minutes = None
    schedule_status = "not_compared"
    if reported_eta is not None:
        delta_minutes = round((predicted_eta - reported_eta).total_seconds() / 60)
        if abs(delta_minutes) <= 30:
            schedule_status = "aligned"
        elif delta_minutes < 0:
            schedule_status = "predicted_early"
        else:
            schedule_status = "predicted_late"

    confidence_score = 0
    if speed_source == "current AIS SOG":
        confidence_score += 50
    else:
        confidence_score += 30
    if historical.get("observation_count", 0) >= 3:
        confidence_score += 25
    if historical.get("coverage_score", 0) >= 85:
        confidence_score += 15
    last_seen = parse_timestamp(vessel.get("last_seen"))
    if last_seen and (now - last_seen).total_seconds() <= STALE_AFTER.total_seconds():
        confidence_score += 10
    confidence = (
        "high" if confidence_score >= 80
        else "medium" if confidence_score >= 50
        else "low"
    )

    return {
        "status": "predicted",
        "as_of": now.isoformat(),
        "predicted_eta": predicted_eta.isoformat(),
        "distance_nm": route["distance_nm"],
        "duration_hours": round(duration_hours, 2),
        "speed_knots": round(speed, 2),
        "speed_source": speed_source,
        "reported_eta": reported_eta.isoformat() if reported_eta else None,
        "schedule_status": schedule_status,
        "delta_from_reported_minutes": delta_minutes,
        "confidence": confidence,
        "confidence_score": confidence_score,
        "route": route,
    }


def _alert(code, severity, title, message):
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "message": message,
    }


def _operational_state(speed):
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        return "unknown"
    if speed >= 1.0:
        return "underway"
    return "stationary"


def _health_band(score):
    if score >= 80:
        return "healthy"
    if score >= 60:
        return "degraded"
    return "poor"


def detect_maritime_anomalies(vessel, history=None, now=None):
    """Detect explainable AIS anomalies and return a normalized risk report."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    history = history or []
    analysis = analyze_vessel(vessel, history, now)
    anomalies = []

    severity_scores = {
        "critical": 80,
        "warning": 40,
    }
    for alert in analysis["alerts"]:
        anomalies.append({
            "code": alert["code"],
            "severity": alert["severity"],
            "score": severity_scores.get(alert["severity"], 10),
            "title": alert["title"],
            "evidence": alert["message"],
            "source": "ais_quality_heuristic",
        })

    implied_speed = analysis["max_implied_speed_knots"]
    reported_speed = vessel.get("sog")
    if implied_speed is not None and reported_speed is not None:
        try:
            reported_speed = float(reported_speed)
            if implied_speed > 3 and abs(implied_speed - reported_speed) > 15:
                anomalies.append({
                    "code": "speed_mismatch",
                    "severity": "warning",
                    "score": 45,
                    "title": "Reported and observed speed disagree",
                    "evidence": f"AIS reports {reported_speed:.1f} knots while history implies {implied_speed:.1f} knots.",
                    "source": "ais_consistency_heuristic",
                })
        except (TypeError, ValueError):
            pass

    if vessel.get("cog") is not None and vessel.get("heading") is not None:
        try:
            course_heading_delta = abs(float(vessel["cog"]) - float(vessel["heading"])) % 360
            course_heading_delta = min(course_heading_delta, 360 - course_heading_delta)
            if course_heading_delta >= 45 and (reported_speed or 0) >= 2:
                anomalies.append({
                    "code": "course_heading_mismatch",
                    "severity": "warning",
                    "score": 35,
                    "title": "Course and heading diverge",
                    "evidence": f"Course and heading differ by {course_heading_delta:.0f} degrees.",
                    "source": "ais_navigation_heuristic",
                })
        except (TypeError, ValueError):
            pass

    last_seen = parse_timestamp(vessel.get("last_seen"))
    if last_seen and (last_seen - now).total_seconds() > 300:
        anomalies.append({
            "code": "future_timestamp",
            "severity": "warning",
            "score": 50,
            "title": "AIS timestamp is in the future",
            "evidence": f"AIS timestamp is {(last_seen - now).total_seconds() / 60:.1f} minutes ahead of the analysis clock.",
            "source": "ais_timestamp_heuristic",
        })

    if vessel.get("latitude") is None or vessel.get("longitude") is None:
        anomalies.append({
            "code": "missing_position",
            "severity": "warning",
            "score": 35,
            "title": "Current position unavailable",
            "evidence": "The vessel has no valid latitude and longitude in current state.",
            "source": "ais_completeness_heuristic",
        })

    total_score = min(100, sum(item["score"] for item in anomalies))
    highest_severity = max(
        (item["severity"] for item in anomalies),
        default="normal",
        key={"normal": 0, "warning": 1, "critical": 2}.get,
    )
    risk_band = (
        "critical" if highest_severity == "critical" or total_score >= 80
        else "elevated" if total_score >= 35
        else "normal"
    )
    return {
        "anomaly_count": len(anomalies),
        "anomaly_score": total_score,
        "highest_severity": highest_severity,
        "risk_band": risk_band,
        "anomalies": anomalies,
        "detector": "explainable AIS consistency heuristics",
    }


def analyze_maritime_fleet(vessels, histories=None, traffic=None, now=None):
    """Create a fleet-level maritime analytics report from current AIS state."""
    histories = histories or {}
    profiles = [
        build_vessel_profile(
            vessel,
            history=histories.get(str(vessel.get("mmsi")), []),
            now=now,
        )
        for vessel in vessels
    ]
    speeds = []
    for vessel in vessels:
        try:
            speed = float(vessel.get("sog"))
            if speed >= 0:
                speeds.append(speed)
        except (TypeError, ValueError):
            continue

    def count_profiles(path, values):
        result = {}
        for value in values:
            current = value
            for key in path:
                current = current.get(key, {}) if isinstance(current, dict) else {}
            result[str(current)] = result.get(str(current), 0) + 1
        return result

    destination_counts = {}
    ship_type_counts = {}
    for vessel, profile in zip(vessels, profiles):
        destination = str(vessel.get("destination") or "Unknown").strip().upper()
        ship_type = profile["identity"]["ship_type"]
        destination_counts[destination] = destination_counts.get(destination, 0) + 1
        ship_type_counts[ship_type] = ship_type_counts.get(ship_type, 0) + 1

    risk_counts = count_profiles(
        ["anomalies", "risk_band"], profiles
    )
    movement_counts = count_profiles(
        ["operational_state"], profiles
    )
    route_shapes = count_profiles(
        ["route", "route_shape"], profiles
    )
    anomaly_total = sum(
        profile["anomalies"]["anomaly_count"] for profile in profiles
    )
    return {
        "vessel_count": len(vessels),
        "positioned_vessel_count": sum(
            vessel.get("latitude") is not None and vessel.get("longitude") is not None
            for vessel in vessels
        ),
        "moving_vessel_count": movement_counts.get("underway", 0),
        "average_speed_knots": round(sum(speeds) / len(speeds), 2) if speeds else None,
        "max_speed_knots": round(max(speeds), 2) if speeds else None,
        "movement_states": movement_counts,
        "risk_bands": risk_counts,
        "route_shapes": route_shapes,
        "ship_types": sorted(
            ({"ship_type": key, "vessel_count": value} for key, value in ship_type_counts.items()),
            key=lambda item: (-item["vessel_count"], item["ship_type"]),
        ),
        "destinations": sorted(
            ({"destination": key, "vessel_count": value} for key, value in destination_counts.items()),
            key=lambda item: (-item["vessel_count"], item["destination"]),
        ),
        "anomaly_count": anomaly_total,
        "hotspots": (traffic or {}).get("cells", []),
        "traffic_window_hours": (traffic or {}).get("hours"),
        "analytics_method": "current AIS state + observed history + explainable intelligence profiles",
    }


def analyze_vessel(vessel, history=None, now=None, destination=None):
    """Return a stable intelligence record for one vessel."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    history = history or []
    alerts = []

    last_seen = parse_timestamp(vessel.get("last_seen"))
    age_seconds = None if last_seen is None else max(0, (now - last_seen).total_seconds())
    if age_seconds is None:
        alerts.append(_alert("missing_timestamp", "warning", "Missing AIS time", "The vessel has no valid last-seen timestamp."))
    elif age_seconds >= CRITICAL_STALE_AFTER.total_seconds():
        alerts.append(_alert("stale_critical", "critical", "AIS signal stale", f"No position received for {age_seconds / 3600:.1f} hours."))
    elif age_seconds > STALE_AFTER.total_seconds():
        alerts.append(_alert("stale", "warning", "AIS signal aging", f"No position received for {age_seconds / 60:.0f} minutes."))

    implied_speeds = []
    course_changes = []
    for previous, current in zip(history, history[1:]):
        previous_time = parse_timestamp(previous.get("timestamp"))
        current_time = parse_timestamp(current.get("timestamp"))
        if not previous_time or not current_time:
            continue
        elapsed = (current_time - previous_time).total_seconds()
        if elapsed <= 0:
            continue
        distance = haversine_nm(previous["lat"], previous["lon"], current["lat"], current["lon"])
        implied_speeds.append(distance / (elapsed / 3600))
        if previous.get("cog") is not None and current.get("cog") is not None:
            change = abs(float(current["cog"]) - float(previous["cog"])) % 360
            course_changes.append(min(change, 360 - change))

    max_implied_speed = max(implied_speeds, default=None)
    if max_implied_speed is not None and max_implied_speed > MAX_TRACK_SPEED_KNOTS:
        alerts.append(_alert("impossible_jump", "critical", "Track jump detected", f"History implies {max_implied_speed:.1f} knots, above the {MAX_TRACK_SPEED_KNOTS:.0f}-knot limit."))

    if vessel.get("sog") is not None and float(vessel["sog"]) > 40:
        alerts.append(_alert("high_speed", "warning", "High reported speed", f"AIS reports {float(vessel['sog']):.1f} knots."))

    average_course_change = sum(course_changes) / len(course_changes) if course_changes else None
    if average_course_change is not None and len(course_changes) >= 2 and average_course_change > 70:
        alerts.append(_alert("course_instability", "warning", "Course instability", "Recent course changes are unusually large."))

    eta_prediction = None
    if destination and vessel.get("latitude") is not None and vessel.get("longitude") is not None:
        eta_prediction = predict_eta(
            vessel["latitude"], vessel["longitude"],
            destination["latitude"], destination["longitude"], vessel.get("sog"), now
        )

    confidence = "low"
    if len(history) >= 3 and age_seconds is not None and age_seconds <= STALE_AFTER.total_seconds():
        confidence = "high"
    elif len(history) >= 2 and age_seconds is not None:
        confidence = "medium"

    return {
        "mmsi": str(vessel.get("mmsi", "")),
        "alert_count": len(alerts),
        "highest_severity": max((item["severity"] for item in alerts), default="normal", key={"normal": 0, "warning": 1, "critical": 2}.get),
        "alerts": alerts,
        "age_seconds": age_seconds,
        "max_implied_speed_knots": None if max_implied_speed is None else round(max_implied_speed, 1),
        "average_course_change": None if average_course_change is None else round(average_course_change, 1),
        "eta_prediction": eta_prediction,
        "eta_confidence": confidence,
    }


def build_vessel_profile(vessel, analysis=None, history=None, now=None):
    """Build the Phase 8 identity, operating-state, and data-health profile."""
    now = now or datetime.now(timezone.utc)
    history = history or []
    analysis = analysis or analyze_vessel(vessel, history, now)
    historical = analyze_historical_track(history, now)
    voyage = analyze_voyage(vessel, historical, now)
    route = analyze_historical_route(history)
    anomalies = detect_maritime_anomalies(vessel, history, now)

    identity_fields = (
        "shipname",
        "imo",
        "callsign",
        "shiptype",
        "destination",
    )
    known_identity = sum(
        1 for field in identity_fields
        if vessel.get(field) not in (None, "", "Unknown vessel")
    )
    identity_completeness = round(known_identity / len(identity_fields) * 100)

    age_seconds = analysis["age_seconds"]
    if age_seconds is None:
        signal_score = 0
    else:
        signal_score = max(0, round(100 - age_seconds / CRITICAL_STALE_AFTER.total_seconds() * 100))

    track_score = 100 if len(history) >= 3 else 60 if len(history) >= 2 else 25
    if analysis["max_implied_speed_knots"] and analysis["max_implied_speed_knots"] > MAX_TRACK_SPEED_KNOTS:
        track_score = 0

    navigation_fields = ("latitude", "longitude", "sog", "cog")
    navigation_completeness = round(
        sum(vessel.get(field) is not None for field in navigation_fields)
        / len(navigation_fields) * 100
    )
    health_score = round(
        signal_score * 0.45
        + track_score * 0.30
        + identity_completeness * 0.15
        + navigation_completeness * 0.10
    )

    return {
        "mmsi": analysis["mmsi"],
        "identity": {
            "name": vessel.get("shipname") or "Unknown vessel",
            "imo": vessel.get("imo"),
            "callsign": vessel.get("callsign"),
            "ship_type": SHIP_TYPE_NAMES.get(vessel.get("shiptype"), "Unknown"),
            "ship_type_code": vessel.get("shiptype"),
            "destination": vessel.get("destination"),
            "identity_completeness": identity_completeness,
        },
        "operational_state": _operational_state(vessel.get("sog")),
        "health": {
            "score": health_score,
            "band": _health_band(health_score),
            "signal_score": signal_score,
            "track_score": track_score,
            "navigation_completeness": navigation_completeness,
        },
        "risk_level": analysis["highest_severity"],
        "signal_age_seconds": age_seconds,
        "alerts": analysis["alerts"],
        "alert_count": analysis["alert_count"],
        "historical": historical,
        "voyage": voyage,
        "route": route,
        "anomalies": anomalies,
    }


def analyze_fleet(vessels, histories=None, now=None):
    histories = histories or {}
    return [analyze_vessel(vessel, histories.get(str(vessel.get("mmsi")), []), now) for vessel in vessels]