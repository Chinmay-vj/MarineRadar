import asyncio
import json
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

    if mmsi is None:
        return None

    if latitude is None:
        return None

    if longitude is None:
        return None

    if timestamp is None:
        return None


    # --------------------------------------------------------
    # Local receive timestamp
    # --------------------------------------------------------

    received_at = datetime.now(
        timezone.utc
    ).isoformat()


    # --------------------------------------------------------
    # Normalize AIS record
    # --------------------------------------------------------

    return {

        "mmsi": str(mmsi),

        "lat": latitude,

        "lon": longitude,

        "sog": data.get("sog"),

        "cog": data.get("cog"),

        "heading": data.get("heading"),

        "timestamp": timestamp,

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