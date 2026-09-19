import os
from datetime import datetime, timezone

import requests
import streamlit as st
import folium

from dotenv import load_dotenv
from streamlit_folium import st_folium


# ============================================================
# LOAD API KEY
# ============================================================

load_dotenv()

API_KEY = os.getenv("PELYR_API_KEY")


# ============================================================
# PAGE CONFIGURATION
# ============================================================

st.set_page_config(
    page_title="Global Ship Tracker",
    page_icon="🚢",
    layout="wide"
)


# ============================================================
# CHECK API KEY
# ============================================================

if not API_KEY:
    st.error("PELYR_API_KEY not found in .env file.")
    st.stop()


# ============================================================
# STORE SHIP POSITION HISTORY
# ============================================================

if "ship_history" not in st.session_state:
    st.session_state.ship_history = {}


# ============================================================
# GET AIS DATA
# ============================================================

@st.cache_data(ttl=60)
def get_vessels():

    url = "https://api.pelyr.com/v1/vessels"

    params = {
        "bbox": "-180,-85,180,85",
        "max": 100
    }

    headers = {
        "Authorization": f"Bearer {API_KEY}"
    }

    try:

        response = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=60
        )

        # ----------------------------------------------------
        # Rate limit
        # ----------------------------------------------------

        if response.status_code == 429:

            retry_after = response.headers.get(
                "Retry-After",
                "60"
            )

            st.warning(
                f"AIS API rate limit reached. "
                f"Retrying after {retry_after} seconds."
            )

            return []

        # ----------------------------------------------------
        # Other errors
        # ----------------------------------------------------

        if response.status_code != 200:

            st.error(
                f"AIS API Error: {response.status_code}"
            )

            st.code(response.text)

            return []

        # ----------------------------------------------------
        # Successful response
        # ----------------------------------------------------

        data = response.json()

        return data.get("vessels", [])

    except requests.exceptions.RequestException as e:

        st.error(
            f"Connection error: {e}"
        )

        return []


# ============================================================
# HEADER
# ============================================================

st.title("🌍 Global Ship Tracker")

st.write(
    "Near-real-time maritime traffic visualization using AIS data"
)


# ============================================================
# LIVE MAP
# ============================================================

@st.fragment(run_every="60s")
def live_map():

    # --------------------------------------------------------
    # Get latest AIS positions
    # --------------------------------------------------------

    ships = get_vessels()


    # --------------------------------------------------------
    # Current update time
    # --------------------------------------------------------

    update_time = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


    # --------------------------------------------------------
    # Statistics
    # --------------------------------------------------------

    col1, col2, col3 = st.columns(3)

    with col1:

        st.metric(
            "🚢 Vessels",
            len(ships)
        )

    with col2:

        st.metric(
            "🔄 Update",
            "60 sec"
        )

    with col3:

        st.metric(
            "📍 Tracked ships",
            len(st.session_state.ship_history)
        )


    st.caption(
        f"Last AIS request: {update_time}"
    )


    # ========================================================
    # CREATE WORLD MAP
    # ========================================================

    m = folium.Map(
        location=[20, 0],
        zoom_start=2,
        tiles="OpenStreetMap"
    )


    # ========================================================
    # PROCESS VESSELS
    # ========================================================

    for ship in ships:

        # ----------------------------------------------------
        # Position and static information
        # ----------------------------------------------------

        position = ship.get("position", {})
        static = ship.get("static", {})

        latitude = position.get("lat")
        longitude = position.get("lon")


        # Ignore ships without coordinates

        if latitude is None or longitude is None:
            continue


        # ----------------------------------------------------
        # Ship identification
        # ----------------------------------------------------

        mmsi = ship.get("mmsi")

        if not mmsi:
            continue

        mmsi = str(mmsi)


        # ----------------------------------------------------
        # Ship information
        # ----------------------------------------------------

        name = (
            static.get("name")
            or "Unknown vessel"
        )

        vessel_type = (
            static.get("type")
            or "Unknown"
        )

        imo = (
            static.get("imo")
            or "Unknown"
        )

        speed = position.get("sog")

        course = position.get("cog")

        heading = position.get("heading")

        destination = (
            static.get("dest")
            or "Unknown"
        )

        timestamp = (
            position.get("ts")
            or "Unknown"
        )


        # ====================================================
        # SHIP HISTORY
        # ====================================================

        history = st.session_state.ship_history.get(
            mmsi,
            []
        )


        # ----------------------------------------------------
        # Get previous position BEFORE adding current position
        # ----------------------------------------------------

        if len(history) > 0:

            previous = history[-1]

        else:

            previous = None


        # ====================================================
        # ADD CURRENT POSITION ONLY IF IT IS NEW
        # ====================================================

        new_position = {
            "lat": latitude,
            "lon": longitude,
            "timestamp": timestamp
        }


        # ----------------------------------------------------
        # Prevent duplicate AIS positions
        # ----------------------------------------------------

        if not history or (

            history[-1]["lat"] != latitude

            or history[-1]["lon"] != longitude

            or history[-1]["timestamp"] != timestamp

        ):

            history.append(new_position)


        # ----------------------------------------------------
        # Keep only latest 20 positions
        # ----------------------------------------------------

        history = history[-20:]


        # ----------------------------------------------------
        # Save updated history
        # ----------------------------------------------------

        st.session_state.ship_history[mmsi] = history


        # ====================================================
        # STEP 4C - DRAW COMPLETE MOVEMENT TRAIL
        # ====================================================

        if len(history) >= 2:

            # Create coordinates for complete trail

            trail_coordinates = [

                [
                    point["lat"],
                    point["lon"]
                ]

                for point in history

            ]


            # ------------------------------------------------
            # Draw complete movement trail
            # ------------------------------------------------

            folium.PolyLine(

                locations=trail_coordinates,

                color="red",

                weight=3,

                opacity=0.8,

                tooltip=f"{name} movement trail"

            ).add_to(m)


            # ------------------------------------------------
            # Mark previous positions
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
        # CURRENT / LAST LOCATION
        # ====================================================

        if previous:

            last_lat = f"{previous['lat']:.5f}"

            last_lon = f"{previous['lon']:.5f}"

            last_timestamp = previous.get(
                "timestamp",
                "Unknown"
            )

        else:

            last_lat = "N/A"

            last_lon = "N/A"

            last_timestamp = "N/A"


        # ====================================================
        # SHIP POPUP
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
            {speed if speed is not None else "N/A"}
            knots<br>

            <b>🧭 Course:</b>
            {course if course is not None else "N/A"}°<br>

            <b>🧭 Heading:</b>
            {heading if heading is not None else "N/A"}°

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

            tooltip=name,

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


# ============================================================
# START LIVE MAP
# ============================================================

live_map()