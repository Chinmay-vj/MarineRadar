from pathlib import Path
from fastapi.responses import FileResponse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from database import (
    initialize_database,
    get_current_vessels,
    get_vessel_histories
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