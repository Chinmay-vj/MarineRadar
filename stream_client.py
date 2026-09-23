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
    save_stream_batch
)


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

API_KEY = os.getenv("AISSTREAM_API_KEY")

WS_URL = "wss://stream.aisstream.io/v0/stream"

# AISStream requires at least one [latitude, longitude] bounding-box pair.
# Keep the historical global coverage by default, but allow deployments to
# narrow the scope (and therefore bandwidth) without code changes.
DEFAULT_BOUNDING_BOXES = [[[-85.0, -180.0], [85.0, 180.0]]]
AISSTREAM_POSITION_TYPES = {
    "PositionReport",
    "StandardClassBPositionReport",
    "ExtendedClassBPositionReport",
    "LongRangeAisBroadcastMessage",
}
AISSTREAM_STATIC_TYPES = {"ShipStaticData", "StaticDataReport"}
AISSTREAM_MESSAGE_TYPES = sorted(
    AISSTREAM_POSITION_TYPES | AISSTREAM_STATIC_TYPES
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
# AISSTREAM SUBSCRIPTION
# ============================================================

def get_subscription():
    """Build one complete AISStream subscription from server-side config."""

    raw_bounding_boxes = os.getenv("AISSTREAM_BOUNDING_BOXES")
    bounding_boxes = DEFAULT_BOUNDING_BOXES

    if raw_bounding_boxes:
        try:
            bounding_boxes = json.loads(raw_bounding_boxes)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "AISSTREAM_BOUNDING_BOXES must be valid JSON, for example "
                "[[[25.835, -80.208], [25.603, -79.879]]]."
            ) from error

    if not API_KEY:
        raise RuntimeError("AISSTREAM_API_KEY is required for live AIS ingestion.")

    return {
        "APIKey": API_KEY,
        "BoundingBoxes": bounding_boxes,
        "FilterMessageTypes": AISSTREAM_MESSAGE_TYPES,
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

HEARTBEAT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "stream_heartbeat.json")


def write_heartbeat(status="streaming"):
    """Write ingestion liveness heartbeat for container and orchestrator monitoring."""
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_PATH), exist_ok=True)
        payload = {
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "accepted": validation_stats["accepted"],
            "rejected_missing": validation_stats["rejected_missing"],
            "rejected_coordinates": validation_stats["rejected_coordinates"],
            "rejected_timestamp": validation_stats["rejected_timestamp"],
            "rejected_jump": validation_stats["rejected_jump"],
        }
        with open(HEARTBEAT_PATH, "w", encoding="utf-8") as file:
            json.dump(payload, file)
    except Exception:
        pass


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


def parse_ais_timestamp(timestamp):
    """Parse the ISO-like timestamp supplied in AISStream MetaData."""

    if timestamp is None:
        raise ValueError("Missing AIS timestamp")

    value = str(timestamp).strip().replace("Z", "+00:00")

    # AISStream examples use a nanosecond timestamp followed by `UTC`, which
    # Python's ISO parser does not accept directly.
    if value.endswith(" UTC"):
        value = value[:-4].strip()

    parsed = datetime.fromisoformat(value)

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def aisstream_message(event):
    """Return the typed AISStream payload and its normalized metadata."""

    message_type = event.get("MessageType")
    message = event.get("Message") or {}
    metadata = event.get("MetaData") or {}

    if not isinstance(message, dict) or not isinstance(metadata, dict):
        return message_type, {}, {}

    payload = message.get(message_type) or {}
    return message_type, payload, metadata


def metadata_value(metadata, *names):
    """Read a field regardless of the casing used by AISStream metadata."""

    for name in names:
        value = metadata.get(name)
        if value is not None:
            return value

    return None


def first_present(*values):
    """Return the first value that is present, preserving valid zeroes."""

    for value in values:
        if value is not None:
            return value

    return None


def normalize_aisstream_position(event):
    """Map an AISStream position envelope into the tracker's internal shape."""

    message_type, payload, metadata = aisstream_message(event)

    if message_type not in AISSTREAM_POSITION_TYPES or not isinstance(payload, dict):
        return None

    return {
        "mmsi": first_present(payload.get("UserID"), metadata_value(metadata, "MMSI", "mmsi")),
        "lat": first_present(payload.get("Latitude"), metadata_value(metadata, "Latitude", "latitude")),
        "lon": first_present(payload.get("Longitude"), metadata_value(metadata, "Longitude", "longitude")),
        "sog": payload.get("Sog"),
        "cog": payload.get("Cog"),
        "heading": payload.get("TrueHeading"),
        "rx_ts": metadata_value(metadata, "time_utc", "TimeUTC", "Timestamp"),
    }


def format_aisstream_eta(payload):
    """Turn AIS type-5 ETA fields into the MMDDHHMM form used by analytics."""

    eta = first_present(payload.get("Eta"), payload.get("ETA"))
    if isinstance(eta, str):
        return eta

    source = eta if isinstance(eta, dict) else payload
    month = first_present(source.get("Month"), source.get("ETAMonth"))
    day = first_present(source.get("Day"), source.get("ETADay"))
    hour = first_present(source.get("Hour"), source.get("ETAHour"))
    minute = first_present(source.get("Minute"), source.get("ETAMinute"))

    try:
        values = [int(month), int(day), int(hour), int(minute)]
    except (TypeError, ValueError):
        return None

    if not (1 <= values[0] <= 12 and 1 <= values[1] <= 31 and 0 <= values[2] <= 23 and 0 <= values[3] <= 59):
        return None

    return f"{values[0]:02d}{values[1]:02d}{values[2]:02d}{values[3]:02d}"


def normalize_aisstream_static(event):
    """Map AISStream type-5/type-24 envelopes into mergeable vessel metadata."""

    message_type, payload, metadata = aisstream_message(event)

    if (
        message_type not in AISSTREAM_STATIC_TYPES | {"ExtendedClassBPositionReport"}
        or not isinstance(payload, dict)
    ):
        return None

    report_a = payload.get("ReportA") or {}
    report_b = payload.get("ReportB") or {}
    if not isinstance(report_a, dict):
        report_a = {}
    if not isinstance(report_b, dict):
        report_b = {}

    dimensions = first_present(report_b.get("Dimension"), payload.get("Dimension")) or {}
    if not isinstance(dimensions, dict):
        dimensions = {}

    return {
        "mmsi": first_present(payload.get("UserID"), metadata_value(metadata, "MMSI", "mmsi")),
        "imo": first_present(payload.get("ImoNumber"), payload.get("IMONumber")),
        "shipname": first_present(report_a.get("Name"), payload.get("Name"), metadata_value(metadata, "ShipName")),
        "callsign": first_present(report_b.get("CallSign"), payload.get("CallSign")),
        "shiptype": first_present(report_b.get("ShipType"), payload.get("Type"), payload.get("ShipType")),
        "destination": payload.get("Destination"),
        "draught": first_present(payload.get("MaximumStaticDraught"), payload.get("Draught")),
        "eta": format_aisstream_eta(payload),
        "dim_a": first_present(dimensions.get("A"), dimensions.get("DimensionToBow"), payload.get("DimensionToBow")),
        "dim_b": first_present(dimensions.get("B"), dimensions.get("DimensionToStern"), payload.get("DimensionToStern")),
        "dim_c": first_present(dimensions.get("C"), dimensions.get("DimensionToPort"), payload.get("DimensionToPort")),
        "dim_d": first_present(dimensions.get("D"), dimensions.get("DimensionToStarboard"), payload.get("DimensionToStarboard")),
        "rx_ts": metadata_value(metadata, "time_utc", "TimeUTC", "Timestamp"),
        "_source": "aisstream",
    }


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

        parsed_timestamp = parse_ais_timestamp(timestamp)

    except (
        TypeError,
        ValueError
    ):

        validation_stats["rejected_timestamp"] += 1

        return None


    # --------------------------------------------------------
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
# PROCESS STATIC AIS METADATA
# ============================================================

def clean_metadata_value(value):

    if isinstance(value, str):

        value = value.strip()

        return value or None

    return value


def optional_number(value):

    if value is None or value == "":

        return None

    try:

        number = float(value)

    except (TypeError, ValueError):

        return None

    return number if math.isfinite(number) else None


def optional_ais_timestamp(value):
    """Return a PostgreSQL-safe UTC timestamp, or omit an invalid source time."""

    try:
        return parse_ais_timestamp(value).isoformat()
    except (TypeError, ValueError):
        return None


def process_static_metadata(data):
    """Normalize an AIS type-5/type-24 record for field-wise merging."""

    mmsi = str(data.get("mmsi") or "").strip()

    if not mmsi:

        return None

    dim_a = optional_number(data.get("dim_a"))
    dim_b = optional_number(data.get("dim_b"))
    dim_c = optional_number(data.get("dim_c"))
    dim_d = optional_number(data.get("dim_d"))

    length = optional_number(data.get("length"))
    beam = optional_number(data.get("beam"))

    if length is None and dim_a is not None and dim_b is not None:

        length = dim_a + dim_b

    if beam is None and dim_c is not None and dim_d is not None:

        beam = dim_c + dim_d

    shiptype = optional_number(data.get("shiptype"))

    record = {
        "mmsi": mmsi,
        "imo": clean_metadata_value(data.get("imo")),
        "shipname": clean_metadata_value(
            data.get("shipname") or data.get("name")
        ),
        "callsign": clean_metadata_value(data.get("callsign")),
        "shiptype": int(shiptype) if shiptype is not None else None,
        "destination": clean_metadata_value(
            data.get("destination") or data.get("dest")
        ),
        "draught": optional_number(data.get("draught")),
        "eta": clean_metadata_value(data.get("eta")),
        "dim_a": dim_a,
        "dim_b": dim_b,
        "dim_c": dim_c,
        "dim_d": dim_d,
        "length": length,
        "beam": beam,
        "last_static_update": optional_ais_timestamp(data.get("rx_ts")),
        "static_source": data.get("_source", "aisstream"),
    }

    if not any(
        record[field] is not None
        for field in (
            "imo",
            "shipname",
            "callsign",
            "shiptype",
            "destination",
            "draught",
            "eta",
            "dim_a",
            "dim_b",
            "dim_c",
            "dim_d",
            "length",
            "beam",
        )
    ):

        return None

    return record


# ============================================================
# DATABASE WRITER
# ============================================================

async def database_writer(
    position_queue,
    metadata_queue,
    stop_event
):

    print(
        "Database writer started."
    )


    while True:

        position_batch = []

        metadata_batch = []


        # ----------------------------------------------------
        # Wait for the first update. Position updates remain the
        # primary feed; static updates are drained in the same writer
        # so SQLite never has competing stream writers.
        # ----------------------------------------------------

        while not position_batch and not metadata_batch:

            if stop_event.is_set():

                # --------------------------------------------
                # Shutdown requested.
                # Drain remaining queue.
                # --------------------------------------------

                while not position_queue.empty():

                    try:

                        position_batch.append(
                            position_queue.get_nowait()
                        )

                    except asyncio.QueueEmpty:

                        break


                while not metadata_queue.empty():

                    try:

                        metadata_batch.append(
                            metadata_queue.get_nowait()
                        )

                    except asyncio.QueueEmpty:

                        break


                if not position_batch and not metadata_batch:

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

                position_batch.append(
                    position
                )

            except asyncio.TimeoutError:

                pass


            while len(metadata_batch) < 1000:

                try:

                    metadata_batch.append(
                        metadata_queue.get_nowait()
                    )

                except asyncio.QueueEmpty:

                    break


        # ----------------------------------------------------
        # Collect more positions
        # ----------------------------------------------------

        deadline = (
            asyncio.get_running_loop().time()
            + 0.25
        )


        while len(position_batch) < 1000:

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

                position_batch.append(
                    position
                )

            except asyncio.TimeoutError:

                break


        while len(metadata_batch) < 1000:

            try:

                metadata_batch.append(
                    metadata_queue.get_nowait()
                )

            except asyncio.QueueEmpty:

                break


        # ----------------------------------------------------
        # Save batch
        # ----------------------------------------------------

        try:

            save_stream_batch(
                position_batch,
                metadata_batch,
            )
            write_heartbeat("streaming")

            print(
                f"Database: saved "
                f"{len(position_batch)} positions, "
                f"{len(metadata_batch)} metadata updates."
            )


        except Exception as e:

            print(
                "Database write error:",
                type(e).__name__,
                e
            )


# ============================================================
# CONNECT TO AISSTREAM
# ============================================================

async def stream_connection(
    position_queue,
    metadata_queue,
    stop_event
):

    print(
        "\nConnecting to AISStream WebSocket..."
    )


    async with websockets.connect(

        WS_URL,
        compression="deflate",
        ping_interval=20,
        ping_timeout=20,

    ) as ws:

        print(
            "Connected to AISStream."
        )


        # ----------------------------------------------------
        # SEND SUBSCRIPTION
        # ----------------------------------------------------

        await ws.send(
            json.dumps(
                get_subscription()
            )
        )


        print(
            "AISStream subscription sent."
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


            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")

            frame = json.loads(raw)

            frame_type = frame.get("MessageType")


            # =================================================
            # SUBSCRIPTION CONFIRMATION
            # =================================================

            if frame_type == "SubscriptionConfirmation":

                print(
                    "Subscription confirmed:",
                    frame.get("Message", {}).get("CompressionEnabled")
                )


            # =================================================
            # STATIC / VOYAGE METADATA
            # =================================================

            elif frame_type in AISSTREAM_STATIC_TYPES:

                metadata = process_static_metadata(
                    normalize_aisstream_static(frame) or {}
                )

                if metadata is not None:
                    await metadata_queue.put(metadata)


            # =================================================
            # POSITION
            # =================================================

            elif frame_type in AISSTREAM_POSITION_TYPES:

                if frame_type == "ExtendedClassBPositionReport":
                    metadata = process_static_metadata(
                        normalize_aisstream_static(frame) or {}
                    )
                    if metadata is not None:
                        await metadata_queue.put(metadata)

                vessel = process_position(
                    normalize_aisstream_position(frame) or {}
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

    metadata_queue = asyncio.Queue(
        maxsize=10000
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

            metadata_queue,

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

                    metadata_queue,

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
        write_heartbeat("stopped")


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
