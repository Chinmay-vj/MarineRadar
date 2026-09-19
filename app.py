import streamlit as st
import folium

from datetime import datetime, timezone

from streamlit_folium import st_folium

from database import (
    initialize_database,
    get_current_vessels,
    get_vessel_histories
)


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

    col1, col2, col3 = st.columns(3)


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
        #
        # Not yet available in the current position database.
        # We deliberately don't invent these values.
        # ----------------------------------------------------

        name = "Unknown vessel"

        vessel_type = (
            "Not available"
        )

        imo = (
            "Not available"
        )

        destination = (
            "Not available"
        )


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


# ============================================================
# START LIVE MAP
# ============================================================

live_map()