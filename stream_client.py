import asyncio
import json
import os
import random

import websockets
from dotenv import load_dotenv


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

API_KEY = os.getenv("PELYR_API_KEY")

if not API_KEY:
    raise RuntimeError("PELYR_API_KEY not found in .env")


WS_URL = "wss://stream.pelyr.com/v1/stream"


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
# PROCESS POSITION
# ============================================================

def process_position(data):
    """
    Convert a Pelyr position frame into a clean vessel record.
    """

    vessel = {
        "mmsi": data.get("mmsi"),
        "lat": data.get("lat"),
        "lon": data.get("lon"),
        "sog": data.get("sog"),
        "cog": data.get("cog"),
        "heading": data.get("heading"),
        "timestamp": data.get("rx_ts")
    }

    return vessel


# ============================================================
# CONNECT TO PELYR
# ============================================================

async def connect_and_stream():

    while True:

        try:

            print("\nConnecting to Pelyr WebSocket...")

            async with websockets.connect(
                WS_URL,
                additional_headers={
                    "Authorization": f"Bearer {API_KEY}"
                },
                ping_interval=None
            ) as ws:

                print("Connected to Pelyr.")

                # ------------------------------------------------
                # RECEIVE WELCOME
                # ------------------------------------------------

                raw = await ws.recv()

                welcome = json.loads(raw)

                print("\nPelyr welcome received.")

                # ------------------------------------------------
                # SEND SUBSCRIPTION
                # ------------------------------------------------

                await ws.send(
                    json.dumps(SUBSCRIPTION)
                )

                print("Global subscription sent.")

                # ------------------------------------------------
                # RECEIVE STREAM
                # ------------------------------------------------

                async for raw in ws:

                    frame = json.loads(raw)

                    frame_type = frame.get("type")


                    # --------------------------------------------
                    # SUBSCRIBED
                    # --------------------------------------------

                    if frame_type == "subscribed":

                        print(
                            "Subscription confirmed."
                        )


                    # --------------------------------------------
                    # POSITION
                    # --------------------------------------------

                    elif frame_type == "position":

                        data = frame.get(
                            "data",
                            {}
                        )

                        vessel = process_position(
                            data
                        )

                        print(
                            f"MMSI: {vessel['mmsi']} | "
                            f"Lat: {vessel['lat']} | "
                            f"Lon: {vessel['lon']} | "
                            f"SOG: {vessel['sog']} | "
                            f"COG: {vessel['cog']} | "
                            f"Time: {vessel['timestamp']}"
                        )


                    # --------------------------------------------
                    # HEARTBEAT
                    # --------------------------------------------

                    elif frame_type == "heartbeat":

                        print(
                            "Heartbeat received."
                        )


                    # --------------------------------------------
                    # NOTICE
                    # --------------------------------------------

                    elif frame_type == "notice":

                        print(
                            "Pelyr notice:",
                            frame
                        )


                    # --------------------------------------------
                    # ERROR
                    # --------------------------------------------

                    elif frame_type == "error":

                        print(
                            "Pelyr stream error:",
                            frame
                        )

                        break


        except Exception as e:

            print(
                "\nWebSocket connection lost:"
            )

            print(
                type(e).__name__,
                e
            )


        # ========================================================
        # RECONNECT
        # ========================================================

        delay = random.randint(
            5,
            30
        )

        print(
            f"Reconnecting in {delay} seconds..."
        )

        await asyncio.sleep(
            delay
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

        print(
            "\nStream stopped by user."
        )