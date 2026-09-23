from pathlib import Path
from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from fastapi.responses import FileResponse, RedirectResponse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from database import (
    initialize_database,
    get_current_vessels,
    get_vessel,
    get_vessel_histories,
    get_vessel_history,
    get_training_histories,
    get_traffic_density,
    get_destination_analysis,
    sync_maritime_alerts,
    get_maritime_alerts,
    acknowledge_maritime_alert,
)
from intelligence import (
    analyze_historical_track,
    analyze_historical_route,
    analyze_maritime_fleet,
    analyze_voyage,
    analyze_vessel,
    build_vessel_profile,
    detect_maritime_anomalies,
    estimate_voyage_route,
    predict_voyage_eta,
)
from ml_prediction import (
    fit_eta_model,
    load_eta_model,
    predict_eta_with_model,
    save_eta_model,
)
from postgres_backend import find_vessels_nearby, postgres_dsn


# ============================================================
# APPLICATION
# ============================================================

app = FastAPI(
    title="Global Ship Tracker Map API",
    description="API layer between SQLite and the live Leaflet map.",
    version="1.0.0"
)

# ============================================================
# FRONTEND
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

MAP_FILE = (
    BASE_DIR
    / "frontend"
    / "map.html"
)


@app.get("/map")
def map_page():

    return FileResponse(
        MAP_FILE
    )


@app.get("/", include_in_schema=False)
def app_entrypoint():

    return RedirectResponse(
        url="/map"
    )

# ============================================================
# DATABASE INITIALIZATION
# ============================================================

initialize_database()


# ============================================================
# CONFIGURATION
# ============================================================

DEFAULT_VESSEL_LIMIT = 100
MAX_VESSEL_LIMIT = 500
DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 100
DEFAULT_ANALYSIS_HOURS = 24
MAX_ANALYSIS_HOURS = 168
DEFAULT_GRID_SIZE = 2.0
MIN_GRID_SIZE = 0.5
MAX_GRID_SIZE = 10.0


def distance_nm(first, second):

    latitude_one = radians(first["lat"])
    latitude_two = radians(second["lat"])
    delta_latitude = radians(
        second["lat"] - first["lat"]
    )
    delta_longitude = radians(
        second["lon"] - first["lon"]
    )

    haversine = (
        sin(delta_latitude / 2) ** 2
        + cos(latitude_one)
        * cos(latitude_two)
        * sin(delta_longitude / 2) ** 2
    )

    return 3440.065 * 2 * asin(
        min(1, sqrt(haversine))
    )


def infer_route(history):

    segments = []
    current_segment = []
    total_distance = 0.0
    total_hours = 0.0

    for point in history:

        current_point = {
            "lat": point["lat"],
            "lon": point["lon"],
            "timestamp": point["timestamp"]
        }

        if current_segment:

            previous_point = current_segment[-1]
            previous_time = datetime.fromisoformat(
                previous_point["timestamp"]
            )
            current_time = datetime.fromisoformat(
                current_point["timestamp"]
            )
            elapsed_hours = (
                current_time - previous_time
            ).total_seconds() / 3600

            if elapsed_hours > 0 and elapsed_hours <= 0.5:

                total_distance += distance_nm(
                    previous_point,
                    current_point
                )
                total_hours += elapsed_hours

            if elapsed_hours > 0.5:

                if len(current_segment) > 1:
                    segments.append(current_segment)

                current_segment = []

        current_segment.append(current_point)

    if len(current_segment) > 1:
        segments.append(current_segment)

    return {
        "method": "observed AIS positions; gaps over 30 minutes are split",
        "point_count": len(history),
        "segment_count": len(segments),
        "distance_nm": round(total_distance, 2),
        "average_speed_knots": round(
            total_distance / total_hours,
            2
        ) if total_hours else None,
        "segments": segments
    }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
def health_check():

    return {
        "status": "ok",
        "service": "Global Ship Tracker Map API",
        "database": "PostgreSQL/PostGIS" if postgres_dsn() else "SQLite",
        "postgis_configured": bool(postgres_dsn()),
    }


# ============================================================
# GET CURRENT VESSELS
# ============================================================

@app.get("/api/v1/vessels")
def get_vessels(
    limit: int = Query(
        DEFAULT_VESSEL_LIMIT,
        ge=1,
        le=MAX_VESSEL_LIMIT
    )
):

    try:

        vessels = get_current_vessels(
            limit=limit
        )

        histories = get_vessel_histories(
            [vessel["mmsi"] for vessel in vessels],
            limit=20,
        )
        for vessel in vessels:
            vessel["intelligence"] = build_vessel_profile(
                vessel,
                analyze_vessel(
                    vessel,
                    histories.get(str(vessel["mmsi"]), []),
                ),
                histories.get(str(vessel["mmsi"]), []),
            )

        return {
            "count": len(vessels),
            "vessels": vessels,
            "intelligence": {
                "healthy": sum(
                    vessel["intelligence"]["health"]["band"] == "healthy"
                    for vessel in vessels
                ),
                "degraded": sum(
                    vessel["intelligence"]["health"]["band"] == "degraded"
                    for vessel in vessels
                ),
                "poor": sum(
                    vessel["intelligence"]["health"]["band"] == "poor"
                    for vessel in vessels
                ),
                "alerts": sum(
                    vessel["intelligence"]["alert_count"]
                    for vessel in vessels
                ),
            },
        }

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=str(error)
        )


@app.get("/api/v1/vessels/{mmsi}/intelligence")
def get_vessel_intelligence(mmsi: str):
    vessel = get_vessel(mmsi)
    if vessel is None:
        raise HTTPException(status_code=404, detail="Vessel not found.")

    history = get_vessel_histories([mmsi], limit=20).get(mmsi, [])
    analysis = analyze_vessel(vessel, history)
    return {
        "vessel": vessel,
        "intelligence": build_vessel_profile(
            vessel,
            analysis,
            history,
        ),
    }


@app.get("/api/v1/vessels/{mmsi}/maritime-anomalies")
def get_maritime_anomalies(
    mmsi: str,
    limit: int = Query(
        MAX_HISTORY_LIMIT,
        ge=2,
        le=MAX_HISTORY_LIMIT,
    ),
):
    vessel = get_vessel(mmsi)
    if vessel is None:
        raise HTTPException(status_code=404, detail="Vessel not found.")

    history = get_vessel_histories([mmsi], limit=limit).get(mmsi, [])
    return {
        "mmsi": mmsi,
        "anomalies": detect_maritime_anomalies(vessel, history),
    }


def _refresh_alert_store(limit=MAX_VESSEL_LIMIT):
    vessels = get_current_vessels(limit=limit)
    histories = get_vessel_histories(
        [vessel["mmsi"] for vessel in vessels],
        limit=20,
    )
    records = []
    for vessel in vessels:
        report = detect_maritime_anomalies(
            vessel,
            histories.get(str(vessel["mmsi"]), []),
        )
        records.append({
            "mmsi": vessel["mmsi"],
            "anomalies": report["anomalies"],
        })
    sync_maritime_alerts(records)


@app.get("/api/v1/alerts")
def get_alert_feed(
    status: str = Query("active", pattern="^(active|acknowledged|resolved|all)$"),
    severity: str | None = Query(None, pattern="^(warning|critical)$"),
    limit: int = Query(100, ge=1, le=1000),
):
    _refresh_alert_store()
    alerts = get_maritime_alerts(
        status=status,
        severity=severity,
        limit=limit,
    )
    return {
        "count": len(alerts),
        "status": status,
        "alerts": alerts,
    }


@app.post("/api/v1/alerts/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: int):
    if not acknowledge_maritime_alert(alert_id):
        raise HTTPException(
            status_code=404,
            detail="Active alert not found.",
        )
    return {
        "id": alert_id,
        "status": "acknowledged",
    }


@app.get("/api/v1/vessels/{mmsi}/historical-intelligence")
def get_historical_vessel_intelligence(
    mmsi: str,
    limit: int = Query(
        MAX_HISTORY_LIMIT,
        ge=2,
        le=MAX_HISTORY_LIMIT,
    ),
):
    vessel = get_vessel(mmsi)
    if vessel is None:
        raise HTTPException(status_code=404, detail="Vessel not found.")

    history = get_vessel_histories([mmsi], limit=limit).get(mmsi, [])
    return {
        "mmsi": mmsi,
        "historical": analyze_historical_track(history),
    }


@app.get("/api/v1/vessels/{mmsi}/historical-route-intelligence")
def get_historical_route_intelligence(
    mmsi: str,
    limit: int = Query(
        MAX_HISTORY_LIMIT,
        ge=2,
        le=MAX_HISTORY_LIMIT,
    ),
):
    vessel = get_vessel(mmsi)
    if vessel is None:
        raise HTTPException(status_code=404, detail="Vessel not found.")

    history = get_vessel_histories([mmsi], limit=limit).get(mmsi, [])
    return {
        "mmsi": mmsi,
        "route": analyze_historical_route(history),
    }


@app.get("/api/v1/vessels/{mmsi}/voyage-intelligence")
def get_voyage_intelligence(mmsi: str):
    vessel = get_vessel(mmsi)
    if vessel is None:
        raise HTTPException(status_code=404, detail="Vessel not found.")

    history = get_vessel_histories([mmsi], limit=20).get(mmsi, [])
    historical = analyze_historical_track(history)
    return {
        "mmsi": mmsi,
        "voyage": analyze_voyage(vessel, historical),
    }


@app.get("/api/v1/vessels/{mmsi}/estimated-route")
def get_estimated_voyage_route(
    mmsi: str,
    destination_latitude: float = Query(..., ge=-90, le=90),
    destination_longitude: float = Query(..., ge=-180, le=180),
    waypoints: int = Query(12, ge=2, le=50),
):
    vessel = get_vessel(mmsi)
    if vessel is None:
        raise HTTPException(status_code=404, detail="Vessel not found.")
    if vessel.get("latitude") is None or vessel.get("longitude") is None:
        raise HTTPException(
            status_code=422,
            detail="Vessel has no valid current position.",
        )

    route = estimate_voyage_route(
        vessel["latitude"],
        vessel["longitude"],
        destination_latitude,
        destination_longitude,
        waypoints,
    )
    if route is None:
        raise HTTPException(status_code=422, detail="Unable to estimate route.")
    return {
        "mmsi": mmsi,
        "route": route,
    }


@app.get("/api/v1/vessels/{mmsi}/eta-prediction")
def get_eta_prediction(
    mmsi: str,
    destination_latitude: float = Query(..., ge=-90, le=90),
    destination_longitude: float = Query(..., ge=-180, le=180),
    waypoints: int = Query(12, ge=2, le=50),
):
    vessel = get_vessel(mmsi)
    if vessel is None:
        raise HTTPException(status_code=404, detail="Vessel not found.")
    history = get_vessel_histories([mmsi], limit=100).get(mmsi, [])
    prediction = predict_voyage_eta(
        vessel,
        destination_latitude,
        destination_longitude,
        history=history,
        waypoint_count=waypoints,
    )
    if prediction is None:
        raise HTTPException(
            status_code=422,
            detail="Vessel has no valid current position.",
        )
    model = load_eta_model()
    prediction["prediction_method"] = "deterministic_baseline"
    prediction["ml_model"] = {
        "status": "not_trained" if model is None else "available",
    }
    if model and prediction.get("status") == "predicted":
        learned = predict_eta_with_model(
            model,
            prediction["distance_nm"],
            prediction["speed_knots"],
            prediction.get("as_of"),
        )
        if learned:
            prediction["baseline_predicted_eta"] = prediction["predicted_eta"]
            prediction["prediction_method"] = "learned_ridge_with_physical_features"
            prediction["ml_model"] = {
                "status": "used",
                "model_version": learned["model_version"],
                "training_rows": learned["training_rows"],
                "training_rmse_hours": learned["training_rmse_hours"],
            }
            prediction["duration_hours"] = learned["duration_hours"]
            as_of = datetime.fromisoformat(prediction["as_of"])
            prediction["predicted_eta"] = (
                as_of + timedelta(hours=learned["duration_hours"])
            ).isoformat()
    return {
        "mmsi": mmsi,
        "prediction": prediction,
    }


@app.get("/api/v1/ml/eta/status")
def get_eta_model_status():
    model = load_eta_model()
    if model is None:
        return {
            "status": "not_trained",
            "model_version": None,
        }
    return {
        "status": "available",
        "model_version": model.get("model_version"),
        "training_rows": model.get("training_rows"),
        "training_rmse_hours": model.get("rmse_hours"),
        "trained_at": model.get("trained_at"),
    }


@app.post("/api/v1/ml/eta/train")
def train_eta_model(
    sample_limit: int = Query(20000, ge=100, le=100000),
):
    histories = get_training_histories(limit=sample_limit)
    model = fit_eta_model(histories)
    if model.get("status") == "trained":
        save_eta_model(model)
    return model


# ============================================================
# GET HISTORY FOR MULTIPLE VESSELS
# ============================================================

@app.get("/api/v1/vessels/history")
def get_histories(
    mmsis: str = Query(
        ...,
        description="Comma-separated MMSIs"
    ),
    limit: int = Query(
        DEFAULT_HISTORY_LIMIT,
        ge=1,
        le=MAX_HISTORY_LIMIT
    )
):

    mmsi_list = [
        mmsi.strip()
        for mmsi in mmsis.split(",")
        if mmsi.strip()
    ]

    if not mmsi_list:

        raise HTTPException(
            status_code=400,
            detail="At least one MMSI is required."
        )

    try:

        histories = get_vessel_histories(
            mmsi_list,
            limit=limit
        )

        return {
            "count": len(histories),
            "histories": histories
        }

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=str(error)
        )


# ============================================================
# GET HISTORY FOR ONE VESSEL
# ============================================================

@app.get("/api/v1/vessels/{mmsi}/history")
def get_single_vessel_history(
    mmsi: str,
    limit: int = Query(
        DEFAULT_HISTORY_LIMIT,
        ge=1,
        le=MAX_HISTORY_LIMIT
    )
):

    try:

        histories = get_vessel_histories(
            [mmsi],
            limit=limit
        )

        history = histories.get(
            mmsi,
            []
        )

        return {
            "mmsi": mmsi,
            "count": len(history),
            "history": history
        }

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=str(error)
        )


@app.get("/api/v1/vessels/{mmsi}/route")
def get_inferred_route(
    mmsi: str,
    limit: int = Query(
        DEFAULT_HISTORY_LIMIT,
        ge=2,
        le=MAX_HISTORY_LIMIT
    )
):

    try:

        history = get_vessel_history(
            mmsi,
            limit=limit
        )

        ordered_history = [
            {
                "lat": row["latitude"],
                "lon": row["longitude"],
                "timestamp": row["timestamp"]
            }
            for row in reversed(history)
        ]

        return {
            "mmsi": mmsi,
            "route": infer_route(ordered_history),
            "route_intelligence": analyze_historical_route(history),
        }

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=str(error)
        )


@app.get("/api/v1/analysis/traffic")
def get_traffic_analysis(
    hours: int = Query(
        DEFAULT_ANALYSIS_HOURS,
        ge=1,
        le=MAX_ANALYSIS_HOURS
    ),
    grid_size: float = Query(
        DEFAULT_GRID_SIZE,
        ge=MIN_GRID_SIZE,
        le=MAX_GRID_SIZE
    )
):

    try:

        return get_traffic_density(
            hours=hours,
            grid_size=grid_size
        )

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=str(error)
        )


@app.get("/api/v1/analysis/maritime")
def get_maritime_analysis(
    hours: int = Query(
        DEFAULT_ANALYSIS_HOURS,
        ge=1,
        le=MAX_ANALYSIS_HOURS,
    ),
    limit: int = Query(
        DEFAULT_VESSEL_LIMIT,
        ge=1,
        le=MAX_VESSEL_LIMIT,
    ),
):
    try:
        vessels = get_current_vessels(limit=limit)
        histories = get_vessel_histories(
            [vessel["mmsi"] for vessel in vessels],
            limit=20,
        )
        traffic = get_traffic_density(hours=hours, grid_size=2.0)
        return analyze_maritime_fleet(vessels, histories, traffic)
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@app.get("/api/v1/spatial/vessels/nearby")
def get_nearby_vessels(
    latitude: float = Query(..., ge=-90, le=90),
    longitude: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(25, gt=0, le=1000),
):
    if not postgres_dsn():
        raise HTTPException(
            status_code=503,
            detail="PostGIS is not configured. Set DATABASE_URL or POSTGRES_DSN.",
        )
    try:
        vessels = find_vessels_nearby(latitude, longitude, radius_km)
        return {
            "latitude": latitude,
            "longitude": longitude,
            "radius_km": radius_km,
            "count": len(vessels),
            "vessels": vessels,
        }
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@app.get("/api/v1/analysis/ports")
def get_port_analysis():

    try:

        destinations = get_destination_analysis()

        return {
            "source": "AIS destination metadata",
            "count": len(destinations),
            "destinations": destinations
        }

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=str(error)
        )


# ============================================================
# SERVER ENTRY POINT
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "map_server:app",
        host="127.0.0.1",
        port=8502,
        reload=False
    )