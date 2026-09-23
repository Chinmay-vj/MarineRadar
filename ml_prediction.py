"""Small dependency-free learned ETA model trained from observed AIS transitions."""

import json
from datetime import datetime, timezone
from math import cos, log1p, pi, sin, sqrt
from pathlib import Path

from intelligence import haversine_nm, parse_timestamp


MODEL_VERSION = "ridge_eta_v1"
BASE_DIR = Path(__file__).resolve().parent
MODEL_PATH = BASE_DIR / "data" / "eta_model.json"
FEATURE_COUNT = 6


def _features(distance_nm, speed_knots, timestamp):
    speed = max(float(speed_knots), 0.1)
    parsed = parse_timestamp(timestamp) or datetime.now(timezone.utc)
    hour = parsed.hour + parsed.minute / 60
    return [
        1.0,
        log1p(max(float(distance_nm), 0.0)),
        float(distance_nm) / speed,
        speed,
        sin(hour / 24 * 2 * pi),
        cos(hour / 24 * 2 * pi),
    ]


def _solve(matrix, vector):
    size = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                current - factor * pivot_value
                for current, pivot_value in zip(augmented[row], augmented[column])
            ]
    return [augmented[index][-1] for index in range(size)]


def _training_rows(histories):
    rows = []
    for history in (histories or {}).values():
        ordered = sorted(
            history,
            key=lambda point: parse_timestamp(point.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc),
        )
        for previous, current in zip(ordered, ordered[1:]):
            previous_time = parse_timestamp(previous.get("timestamp"))
            current_time = parse_timestamp(current.get("timestamp"))
            if not previous_time or not current_time:
                continue
            elapsed_hours = (current_time - previous_time).total_seconds() / 3600
            if elapsed_hours <= 0 or elapsed_hours > 0.5:
                continue
            try:
                distance = haversine_nm(
                    previous.get("lat", previous.get("latitude")),
                    previous.get("lon", previous.get("longitude")),
                    current.get("lat", current.get("latitude")),
                    current.get("lon", current.get("longitude")),
                )
            except (TypeError, ValueError):
                continue
            if distance <= 0:
                continue
            reported_speed = current.get("sog") or previous.get("sog")
            try:
                speed = float(reported_speed)
            except (TypeError, ValueError):
                speed = distance / elapsed_hours
            if speed <= 0:
                speed = distance / elapsed_hours
            rows.append((_features(distance, speed, current_time), elapsed_hours))
    return rows


def fit_eta_model(histories, regularization=0.01):
    rows = _training_rows(histories)
    if len(rows) < FEATURE_COUNT + 2:
        return {
            "status": "insufficient_data",
            "model_version": MODEL_VERSION,
            "training_rows": len(rows),
            "minimum_rows": FEATURE_COUNT + 2,
        }

    matrix = [[0.0] * FEATURE_COUNT for _ in range(FEATURE_COUNT)]
    vector = [0.0] * FEATURE_COUNT
    for features, target in rows:
        for row in range(FEATURE_COUNT):
            vector[row] += features[row] * target
            for column in range(FEATURE_COUNT):
                matrix[row][column] += features[row] * features[column]
    for index in range(1, FEATURE_COUNT):
        matrix[index][index] += regularization

    coefficients = _solve(matrix, vector)
    if coefficients is None:
        return {
            "status": "training_failed",
            "model_version": MODEL_VERSION,
            "training_rows": len(rows),
        }

    errors = []
    for features, target in rows:
        predicted = max(0.0, sum(weight * value for weight, value in zip(coefficients, features)))
        errors.append((predicted - target) ** 2)
    rmse = sqrt(sum(errors) / len(errors))
    return {
        "status": "trained",
        "model_version": MODEL_VERSION,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "training_rows": len(rows),
        "rmse_hours": round(rmse, 5),
        "coefficients": coefficients,
        "feature_names": [
            "bias",
            "log_distance_nm",
            "distance_over_speed",
            "speed_knots",
            "hour_sin",
            "hour_cos",
        ],
    }


def save_eta_model(model, path=MODEL_PATH):
    if model.get("status") != "trained":
        return False
    path = Path(path)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(model, indent=2), encoding="utf-8")
    return True


def load_eta_model(path=MODEL_PATH):
    path = Path(path)
    if not path.exists():
        return None
    try:
        model = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return model if model.get("status") == "trained" else None


def predict_eta_with_model(model, distance_nm, speed_knots, timestamp=None):
    if not model or model.get("status") != "trained":
        return None
    features = _features(distance_nm, speed_knots, timestamp)
    duration = max(
        0.0,
        sum(weight * value for weight, value in zip(model["coefficients"], features)),
    )
    return {
        "duration_hours": round(duration, 2),
        "model_version": model["model_version"],
        "training_rows": model["training_rows"],
        "training_rmse_hours": model.get("rmse_hours"),
    }
