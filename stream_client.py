import asyncio
import json
import math
import os
import random
import signal
from datetime import datetime, timezone

import websockets
from dotenv import load_dotenv

from database import (
    initialize_database,
    save_vessels_batch
)


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

API_KEY = os.getenv(
    "PELYR_API_KEY"
)

if not API_KEY:
    raise RuntimeError(
        "PELYR_API_KEY not found in .env"
    )


WS_URL = (
    "wss://stream.pelyr.com/v1/stream"
)


# ============================================================
# AIS QUALITY CONFIGURATION
# ============================================================

# A long observation gap is treated as a track break.
# We do NOT reject the new point just because the vessel
# appears far away after a long gap.
MAX_CONTINUITY_GAP_SECONDS = 30 * 60

# Merchant-vessel movement above this speed is considered
# invalid for observed-track continuity.
#
# Important:
# This is NOT the vessel's reported SOG. It is the speed
# calculated from two consecutive AIS positions.
MAX_IMPLIED_SPEED_KNOTS = 50.0


# ============================================================
# GLOBAL SUBSCRIPTION
# ============================================================

SUBSCRIPTION = {

    "type": "subscribe",

    "id": "global",

    "bbox": [

        {
            "west": -180,
            "south": -85,
            "east": 180,
            "north": 85
        }

    ],

    "fields": "position"
}


# ============================================================
# TEMPORAL VALIDATION STATE
# ============================================================

# Last accepted AIS observation for each MMSI.
#
# This state is intentionally kept in the stream process.
# It allows us to identify impossible short-time jumps while
# still allowing legitimate movement after long AIS gaps.
last_valid_positions = {}


# Diagnostic counters for this running process.
validation_stats = {
    "accepted": 0,
    "rejected_missing": 0,
    "rejected_coordinates": 0,
    "rejected_timestamp": 0,
    "rejected_jump": 0
}


# ============================================================
# DISTANCE / SPEED HELPERS
# ============================================================

def haversine_km(
    latitude1,
    longitude1,
    latitude2,
    longitude2
):
    """
    Calculate great-circle distance between two coordinates.
    """

    earth_radius_km = 6371.0088

    lat1 = math.radians(latitude1)
    lat2 = math.radians(latitude2)

    delta_lat = math.radians(
        latitude2 - latitude1
    )

    delta_lon = math.radians(
        longitude2 - longitude1
    )

    a = (
        math.sin(delta_lat / 2) ** 2
        +
        math.cos(lat1)
        *
        math.cos(lat2)
        *
        math.sin(delta_lon / 2) ** 2
    )

    a = min(
        1.0,
        max(0.0, a)
    )

    return (
        earth_radius_km
        *
        2
        *
        math.asin(math.sqrt(a))
    )


def implied_speed_knots(
    distance_km,
    elapsed_seconds
):
    """
    Convert distance/time into knots.
    """

    if elapsed_seconds <= 0:
        return float("inf")

    km_per_hour = (
        distance_km
        /
        (elapsed_seconds / 3600.0)
    )

    return (
        km_per_hour
        /
        1.852
    )


# ============================================================
# PROCESS AIS POSITION
# ============================================================

def process_position(data):

    mmsi = data.get("mmsi")

    latitude = data.get("lat")

    longitude = data.get("lon")

    timestamp = data.get("rx_ts")


    # --------------------------------------------------------
    # Validate required fields
    # --------------------------------------------------------

    if (
        mmsi is None
        or latitude is None
        or longitude is None
        or timestamp is None
    ):

        validation_stats["rejected_missing"] += 1

        return None


    # --------------------------------------------------------
    # Validate coordinate values
    # --------------------------------------------------------

    try:

        latitude = float(latitude)

        longitude = float(longitude)

    except (
        TypeError,
        ValueError
    ):

        validation_stats["rejected_coordinates"] += 1

        return None


    if not math.isfinite(latitude):

        validation_stats["rejected_coordinates"] += 1

        return None


    if not math.isfinite(longitude):

        validation_stats["rejected_coordinates"] += 1

        return None


    # --------------------------------------------------------
    # Validate geographic bounds
    # --------------------------------------------------------

    if not (
        -90.0 <= latitude <= 90.0
    ):

        validation_stats["rejected_coordinates"] += 1

        return None


    if not (
        -180.0 <= longitude <= 180.0
    ):

        validation_stats["rejected_coordinates"] += 1

        return None


    # --------------------------------------------------------
    # Reject the AIS null/sentinel position (0, 0)
    # --------------------------------------------------------

    # Latitude 0 and longitude 0 is a real geographic
    # coordinate, but it is appearing in this feed as an
    # invalid/missing-position sentinel.
    #
    # We reject only the exact (0, 0) pair here.
    #
    # Longitude 0 by itself is NOT rejected because ships
    # can legitimately cross the Prime Meridian.

    if (
        abs(latitude) < 1e-9
        and
        abs(longitude) < 1e-9
    ):

        validation_stats["rejected_coordinates"] += 1

        return None


    # --------------------------------------------------------
    # Validate AIS timestamp
    # --------------------------------------------------------

    try:

        parsed_timestamp = datetime.fromisoformat(
            str(timestamp).replace(
                "Z",
                "+00:00"
            )
        )

    except (
        TypeError,
        ValueError
    ):

        validation_stats["rejected_timestamp"] += 1

        return None


    # --------------------------------------------------------
    # Normalize timestamp to UTC
    # --------------------------------------------------------

    if parsed_timestamp.tzinfo is None:

        parsed_timestamp = parsed_timestamp.replace(
            tzinfo=timezone.utc
        )

    else:

        parsed_timestamp = parsed_timestamp.astimezone(
            timezone.utc
        )


    normalized_timestamp = (
        parsed_timestamp.isoformat()
    )


    # --------------------------------------------------------
    # Temporal continuity validation
    # --------------------------------------------------------

    vessel_key = str(mmsi)

    previous = last_valid_positions.get(
        vessel_key
    )


    if previous is not None:

        previous_timestamp = previous["timestamp"]

        elapsed_seconds = (
            parsed_timestamp
            -
            previous_timestamp
        ).total_seconds()


        # ----------------------------------------------------
        # Ignore out-of-order/duplicate timestamps.
        # ----------------------------------------------------

        if elapsed_seconds <= 0:

            validation_stats["rejected_timestamp"] += 1

            return None


        # ----------------------------------------------------
        # Only perform speed validation when observations are
        # close enough in time to represent one continuous
        # observed track.
        #
        # A long gap is NOT rejected. It becomes a natural
        # track break for the historical/frontend layer.
        # ----------------------------------------------------

        if (
            elapsed_seconds
            <=
            MAX_CONTINUITY_GAP_SECONDS
        ):

            distance_km = haversine_km(

                previous["latitude"],
                previous["longitude"],

                latitude,
                longitude

            )

            speed_knots = implied_speed_knots(

                distance_km,
                elapsed_seconds

            )


            if (
                speed_knots
                >
                MAX_IMPLIED_SPEED_KNOTS
            ):

                validation_stats["rejected_jump"] += 1

                return None


    # --------------------------------------------------------
    # Local receive timestamp
    # --------------------------------------------------------

    received_at = datetime.now(
        timezone.utc
    ).isoformat()


    # --------------------------------------------------------
    # Store accepted point as temporal reference
    # --------------------------------------------------------

    last_valid_positions[vessel_key] = {

        "latitude": latitude,

        "longitude": longitude,

        "timestamp": parsed_timestamp

    }


    validation_stats["accepted"] += 1


    # --------------------------------------------------------
    # Normalize AIS record
    # --------------------------------------------------------

    return {

        "mmsi": vessel_key,

        "lat": latitude,

        "lon": longitude,

        "sog": data.get("sog"),

        "cog": data.get("cog"),

        "heading": data.get("heading"),

        "timestamp": normalized_timestamp,

        "received_at": received_at

    }


# ============================================================
# DATABASE WRITER
# ============================================================

async def database_writer(
    position_queue,
    stop_event
):

    print(
        "Database writer started."
    )


    while True:

        batch = []


        # ----------------------------------------------------
        # Wait for first position
        # ----------------------------------------------------

        while not batch:

            if stop_event.is_set():

                # --------------------------------------------
                # Shutdown requested.
                # Drain remaining queue.
                # --------------------------------------------

                while not position_queue.empty():

                    try:

                        batch.append(
                            position_queue.get_nowait()
                        )

                    except asyncio.QueueEmpty:

                        break


                if not batch:

                    print(
                        "Database writer stopped."
                    )

                    return


                break


            try:

                position = await asyncio.wait_for(

                    position_queue.get(),

                    timeout=0.5

                )

                batch.append(
                    position
                )

            except asyncio.TimeoutError:

                continue


        # ----------------------------------------------------
        # Collect more positions
        # ----------------------------------------------------

        deadline = (
            asyncio.get_running_loop().time()
            + 0.25
        )


        while len(batch) < 1000:

            remaining = (

                deadline
                -
                asyncio.get_running_loop().time()

            )


            if remaining <= 0:

                break


            try:

                position = await asyncio.wait_for(

                    position_queue.get(),

                    timeout=remaining

                )

                batch.append(
                    position
                )

            except asyncio.TimeoutError:

                break


        # ----------------------------------------------------
        # Save batch
        # ----------------------------------------------------

        try:

            save_vessels_batch(
                batch
            )

            print(
                f"Database: saved "
                f"{len(batch)} positions."
            )


        except Exception as e:

            print(
                "Database write error:",
                type(e).__name__,
                e
            )


# ============================================================
# CONNECT TO PELYR
# ============================================================

async def stream_connection(
    position_queue,
    stop_event
):

    print(
        "\nConnecting to Pelyr WebSocket..."
    )


    async with websockets.connect(

        WS_URL,

        additional_headers={
            "Authorization":
            f"Bearer {API_KEY}"
        },

        ping_interval=None

    ) as ws:

        print(
            "Connected to Pelyr."
        )


        # ----------------------------------------------------
        # RECEIVE WELCOME
        # ----------------------------------------------------

        raw = await ws.recv()

        welcome = json.loads(
            raw
        )

        print(
            "Pelyr welcome received."
        )


        # ----------------------------------------------------
        # SEND SUBSCRIPTION
        # ----------------------------------------------------

        await ws.send(
            json.dumps(
                SUBSCRIPTION
            )
        )


        print(
            "Global subscription sent."
        )


        # ----------------------------------------------------
        # RECEIVE STREAM
        # ----------------------------------------------------

        async for raw in ws:

            # ------------------------------------------------
            # Stop requested
            # ------------------------------------------------

            if stop_event.is_set():

                print(
                    "Stopping WebSocket stream..."
                )

                break


            frame = json.loads(
                raw
            )


            frame_type = frame.get(
                "type"
            )


            # =================================================
            # SUBSCRIBED
            # =================================================

            if frame_type == "subscribed":

                print(
                    "Subscription confirmed."
                )


            # =================================================
            # POSITION
            # =================================================

            elif frame_type == "position":

                data = frame.get(
                    "data",
                    {}
                )


                vessel = process_position(
                    data
                )


                if vessel is None:

                    continue


                # ------------------------------------------------
                # Put into queue.
                #
                # We intentionally WAIT here if the queue is full
                # instead of dropping AIS positions.
                # ------------------------------------------------

                await position_queue.put(
                    vessel
                )


            # =================================================
            # HEARTBEAT
            # =================================================

            elif frame_type == "heartbeat":

                print(
                    "Heartbeat received."
                )


            # =================================================
            # NOTICE
            # =================================================

            elif frame_type == "notice":

                print(
                    "Pelyr notice:",
                    frame
                )


            # =================================================
            # ERROR
            # =================================================

            elif frame_type == "error":

                print(
                    "Pelyr stream error:",
                    frame
                )

                break


# ============================================================
# MAIN STREAM MANAGER
# ============================================================

async def connect_and_stream():

    # --------------------------------------------------------
    # Initialize database
    # --------------------------------------------------------

    initialize_database()

    print(
        "Database initialized."
    )


    # --------------------------------------------------------
    # Queue
    # --------------------------------------------------------

    position_queue = asyncio.Queue(
        maxsize=50000
    )


    # --------------------------------------------------------
    # Stop event
    # --------------------------------------------------------

    stop_event = asyncio.Event()


    # --------------------------------------------------------
    # Handle Ctrl+C
    # --------------------------------------------------------

    def handle_shutdown(
        signum,
        frame
    ):

        print(
            "\nShutdown requested."
        )

        stop_event.set()


    # --------------------------------------------------------
    # Windows / Unix compatible signal handling
    # --------------------------------------------------------

    signal.signal(
        signal.SIGINT,
        handle_shutdown
    )


    if hasattr(
        signal,
        "SIGTERM"
    ):

        signal.signal(
            signal.SIGTERM,
            handle_shutdown
        )


    # --------------------------------------------------------
    # Start database writer
    # --------------------------------------------------------

    writer_task = asyncio.create_task(

        database_writer(

            position_queue,

            stop_event

        )

    )


    try:

        # ====================================================
        # RECONNECT LOOP
        # ====================================================

        while not stop_event.is_set():

            try:

                await stream_connection(

                    position_queue,

                    stop_event

                )


                # ------------------------------------------------
                # If connection closed normally
                # ------------------------------------------------

                if stop_event.is_set():

                    break


                print(
                    "\nWebSocket connection closed."
                )


            except (
                websockets.ConnectionClosed,
                ConnectionError,
                OSError
            ) as e:

                if stop_event.is_set():

                    break


                print(
                    "\nWebSocket connection lost:"
                )

                print(
                    type(e).__name__,
                    e
                )


            except Exception as e:

                if stop_event.is_set():

                    break


                print(
                    "\nUnexpected WebSocket error:"
                )

                print(
                    type(e).__name__,
                    e
                )


            # ------------------------------------------------
            # Reconnect
            # ------------------------------------------------

            if not stop_event.is_set():

                delay = random.randint(
                    5,
                    30
                )


                print(
                    f"Reconnecting in "
                    f"{delay} seconds..."
                )


                # ------------------------------------------------
                # Wait in small increments so Ctrl+C/shutdown
                # can interrupt the reconnect delay.
                # ------------------------------------------------

                for _ in range(
                    delay
                ):

                    if stop_event.is_set():

                        break

                    await asyncio.sleep(
                        1
                    )


    finally:

        # ----------------------------------------------------
        # Tell database writer to stop
        # ----------------------------------------------------

        print(
            "\nStopping database writer..."
        )

        stop_event.set()


        # ----------------------------------------------------
        # Wait for writer to finish pending data
        # ----------------------------------------------------

        try:

            await asyncio.wait_for(

                writer_task,

                timeout=10

            )

        except asyncio.TimeoutError:

            print(
                "Database writer shutdown timed out."
            )

            writer_task.cancel()

            try:

                await writer_task

            except asyncio.CancelledError:

                pass


        # ----------------------------------------------------
        # Print validation statistics
        # ----------------------------------------------------

        print(
            "\nAIS validation statistics:"
        )

        print(
            f"Accepted: "
            f"{validation_stats['accepted']}"
        )

        print(
            f"Rejected missing fields: "
            f"{validation_stats['rejected_missing']}"
        )

        print(
            f"Rejected coordinates: "
            f"{validation_stats['rejected_coordinates']}"
        )

        print(
            f"Rejected timestamps: "
            f"{validation_stats['rejected_timestamp']}"
        )

        print(
            f"Rejected impossible jumps: "
            f"{validation_stats['rejected_jump']}"
        )


        print(
            "Shutdown complete."
        )


# ============================================================
# PROGRAM ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            connect_and_stream()
        )

    except KeyboardInterrupt:

        # ----------------------------------------------------
        # Ctrl+C should now normally be handled by our
        # shutdown handler.
        # ----------------------------------------------------

        print(
            "\nProgram stopped."
        )
