from pathlib import Path
from datetime import datetime
from math import asin, cos, radians, sin, sqrt
from fastapi.responses import FileResponse, RedirectResponse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from database import (
    initialize_database,
    get_current_vessels,
    get_vessel_histories,
    get_vessel_history,
    get_traffic_density,
    get_destination_analysis
)


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
        "database": "SQLite"
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

        return {
            "count": len(vessels),
            "vessels": vessels
        }

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=str(error)
        )


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
            "route": infer_route(ordered_history)
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