import streamlit as st
import folium

from datetime import datetime, timezone

from streamlit_folium import st_folium

from database import (
    initialize_database,
    get_current_vessels,
    get_vessel_histories
)
from intelligence import analyze_vessel, build_vessel_profile


# ============================================================
# PAGE CONFIGURATION
# ============================================================

st.set_page_config(
    page_title="Global Ship Tracker",
    page_icon="🚢",
    layout="wide"
)


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

initialize_database()


# ============================================================
# APPLICATION CONFIGURATION
# ============================================================

# Number of vessels rendered on the map.
#
# The database contains thousands of vessels, but rendering
# tens of thousands of Folium markers would make the browser
# extremely slow.
#
# This is ONLY a display limit.
# All AIS data remains stored in SQLite.

MAX_MAP_VESSELS = 100


# Number of historical positions used for the trail.

TRAIL_LENGTH = 20


# Optional straight-line ETA target. AIS destination names do not contain
# coordinates, so operators can supply a port or waypoint here.
with st.sidebar:
    st.header("Intelligence")
    eta_enabled = st.checkbox("Calculate route ETA", value=False)
    eta_latitude = st.number_input("Destination latitude", -90.0, 90.0, 1.0, 0.1, disabled=not eta_enabled)
    eta_longitude = st.number_input("Destination longitude", -180.0, 180.0, 103.0, 0.1, disabled=not eta_enabled)
    st.caption("ETA uses great-circle distance and current AIS speed.")


# ============================================================
# HEADER
# ============================================================

st.title(
    "🌍 Global Ship Tracker"
)

st.write(
    "Near-real-time maritime traffic visualization "
    "using persistent AIS data"
)


# ============================================================
# LIVE MAP
# ============================================================

@st.fragment(run_every="5s")
def live_map():

    # ========================================================
    # READ CURRENT VESSELS FROM SQLITE
    # ========================================================

    ships = get_current_vessels(
        limit=MAX_MAP_VESSELS
    )


    # ========================================================
    # GET MMSIs
    # ========================================================

    mmsis = [

        ship["mmsi"]

        for ship in ships

        if ship.get("mmsi")

    ]


    # ========================================================
    # READ HISTORICAL POSITIONS
    #
    # One batch database operation instead of one query
    # per vessel.
    # ========================================================

    histories = get_vessel_histories(

        mmsis,

        limit=TRAIL_LENGTH

    )

    destination = (
        {"latitude": eta_latitude, "longitude": eta_longitude}
        if eta_enabled
        else None
    )
    intelligence = {
        str(ship["mmsi"]): {
            "analysis": analyze_vessel(
                ship,
                histories.get(str(ship["mmsi"]), []),
                destination=destination,
            ),
            "profile": build_vessel_profile(
                ship,
                analyze_vessel(
                    ship,
                    histories.get(str(ship["mmsi"]), []),
                    destination=destination,
                ),
                histories.get(str(ship["mmsi"]), []),
            ),
        }
        for ship in ships
        if ship.get("mmsi")
    }
    alert_records = [
        {
            "MMSI": analysis["mmsi"],
            "Severity": alert["severity"].upper(),
            "Alert": alert["title"],
            "Details": alert["message"],
        }
        for vessel_data in intelligence.values()
        for alert in vessel_data["analysis"]["alerts"]
    ]


    # ========================================================
    # CURRENT UPDATE TIME
    # ========================================================

    update_time = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


    # ========================================================
    # STATISTICS
    # ========================================================

    col1, col2, col3, col4, col5 = st.columns(5)


    with col1:

        st.metric(
            "🚢 Displayed vessels",
            len(ships)
        )


    with col2:

        st.metric(
            "🔄 Map refresh",
            "5 sec"
        )


    with col3:

        st.metric(
            "💾 Data source",
            "SQLite"
        )

    with col4:
        st.metric(
            "⚠️ Active alerts",
            len(alert_records)
        )

    with col5:
        critical_count = sum(
            1 for item in alert_records if item["Severity"] == "CRITICAL"
        )
        st.metric("🔴 Critical", critical_count)


    st.caption(
        f"Last database refresh: {update_time}"
    )


    # ========================================================
    # CREATE WORLD MAP
    # ========================================================

    m = folium.Map(

        location=[
            20,
            0
        ],

        zoom_start=2,

        tiles="OpenStreetMap"

    )


    # ========================================================
    # PROCESS VESSELS
    # ========================================================

    for ship in ships:

        # ----------------------------------------------------
        # MMSI
        # ----------------------------------------------------

        mmsi = ship.get(
            "mmsi"
        )


        if not mmsi:

            continue


        mmsi = str(
            mmsi
        )


        # ----------------------------------------------------
        # CURRENT POSITION
        # ----------------------------------------------------

        latitude = ship.get(
            "latitude"
        )

        longitude = ship.get(
            "longitude"
        )


        if (
            latitude is None
            or
            longitude is None
        ):

            continue


        # ----------------------------------------------------
        # CURRENT AIS DATA
        # ----------------------------------------------------

        speed = ship.get(
            "sog"
        )

        course = ship.get(
            "cog"
        )

        heading = ship.get(
            "heading"
        )

        timestamp = ship.get(
            "last_seen"
        )


        # ----------------------------------------------------
        # STATIC INFORMATION
        # ----------------------------------------------------

        name = ship.get("shipname") or "Unknown vessel"
        vessel_type = ship.get("shiptype") or "Not available"
        imo = ship.get("imo") or "Not available"
        destination = ship.get("destination") or "Not available"

        vessel_intelligence = intelligence.get(mmsi, {})
        analysis = vessel_intelligence.get("analysis", {})
        profile = vessel_intelligence.get("profile", {})
        alert_count = analysis.get("alert_count", 0)
        health = profile.get("health", {})
        voyage = profile.get("voyage", {})
        anomalies = profile.get("anomalies", {})


        # ====================================================
        # VESSEL HISTORY
        # ====================================================

        history = histories.get(
            mmsi,
            []
        )


        # ----------------------------------------------------
        # Previous position
        #
        # The last history point may be the current position.
        # Therefore we use the second-last point when available.
        # ----------------------------------------------------

        if len(history) >= 2:

            previous = history[-2]

        else:

            previous = None


        # ====================================================
        # DRAW MOVEMENT TRAIL
        # ====================================================

        if len(history) >= 2:

            trail_coordinates = [

                [
                    point["lat"],
                    point["lon"]
                ]

                for point in history

            ]


            folium.PolyLine(

                locations=trail_coordinates,

                color="red",

                weight=3,

                opacity=0.8,

                tooltip=(
                    f"{name} movement trail"
                )

            ).add_to(m)


            # ------------------------------------------------
            # Previous positions
            # ------------------------------------------------

            for point in history[:-1]:

                folium.CircleMarker(

                    location=[

                        point["lat"],
                        point["lon"]

                    ],

                    radius=3,

                    color="red",

                    fill=True,

                    fill_opacity=0.7

                ).add_to(m)


        # ====================================================
        # LAST LOCATION
        # ====================================================

        if previous:

            last_lat = (
                f"{previous['lat']:.5f}"
            )

            last_lon = (
                f"{previous['lon']:.5f}"
            )

            last_timestamp = (
                previous.get(
                    "timestamp",
                    "Unknown"
                )
            )

        else:

            last_lat = "N/A"

            last_lon = "N/A"

            last_timestamp = "N/A"


        # ====================================================
        # POPUP
        # ====================================================

        popup_html = f"""
        <div style="
            font-family: Arial;
            width: 300px;
        ">

            <h4 style="
                margin-bottom: 10px;
            ">
                🚢 {name}
            </h4>

            <b>MMSI:</b> {mmsi}<br>

            <b>IMO:</b> {imo}<br>

            <b>Type:</b> {vessel_type}<br>

            <b>Destination:</b> {destination}<br>

            <br>

            <b>📍 CURRENT LOCATION</b><br>

            Latitude:
            {latitude:.5f}<br>

            Longitude:
            {longitude:.5f}<br>

            AIS Time:
            {timestamp}<br>

            <br>

            <b>🕘 LAST LOCATION</b><br>

            Latitude:
            {last_lat}<br>

            Longitude:
            {last_lon}<br>

            Previous AIS Time:
            {last_timestamp}<br>

            <br>

            <b>🚢 Speed:</b>
            {
                speed
                if speed is not None
                else "N/A"
            }
            knots<br>

            <b>🧭 Course:</b>
            {
                course
                if course is not None
                else "N/A"
            }°<br>

            <b>🧭 Heading:</b>
            {
                heading
                if heading is not None
                else "N/A"
            }°

            <br><br>
            <b>⚠️ Intelligence alerts:</b> {alert_count}

            <br>
            <b>State:</b> {profile.get("operational_state", "unknown")}<br>
            <b>Health:</b> {health.get("score", "N/A")} / 100
            ({health.get("band", "unknown")})<br>
            <b>Risk:</b> {profile.get("risk_level", "normal")}<br>
            <b>Identity completeness:</b>
            {profile.get("identity", {}).get("identity_completeness", 0)}%

            <br><br>
            <b>Voyage state:</b> {voyage.get("voyage_state", "unknown")}<br>
            <b>Reported ETA:</b> {voyage.get("reported_eta", "N/A")}<br>
            <b>Voyage confidence:</b> {voyage.get("confidence", "low")}

            <br>
            <b>Maritime anomaly risk:</b>
            {anomalies.get("risk_band", "normal")}<br>
            <b>Anomaly score:</b>
            {anomalies.get("anomaly_score", 0)} / 100

        </div>
        """


        # ====================================================
        # CURRENT SHIP MARKER
        # ====================================================

        folium.Marker(

            location=[
                latitude,
                longitude
            ],

            popup=folium.Popup(

                popup_html,

                max_width=320

            ),

            tooltip=mmsi,

            icon=folium.Icon(

                icon="ship",

                prefix="fa"

            )

        ).add_to(m)


    # ========================================================
    # DISPLAY MAP
    # ========================================================

    st_folium(

        m,

        width=None,

        height=700

    )

    st.subheader("Intelligence feed")
    if alert_records:
        st.dataframe(
            alert_records,
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.success("No active vessel anomalies detected in the displayed fleet.")

    eta_records = []
    for ship in ships:
        vessel_data = intelligence.get(str(ship.get("mmsi")), {})
        prediction = vessel_data.get("analysis", {}).get("eta_prediction")
        if prediction:
            eta_records.append({
                "MMSI": ship["mmsi"],
                "Destination": f"{eta_latitude:.2f}, {eta_longitude:.2f}",
                "Distance (nm)": prediction["distance_nm"],
                "ETA": prediction["eta"],
                "Confidence": vessel_data["analysis"]["eta_confidence"].upper(),
            })
    if eta_records:
        st.subheader("Predicted arrivals")
        st.dataframe(eta_records, use_container_width=True, hide_index=True)


# ============================================================
# START LIVE MAP
# ============================================================

live_map()