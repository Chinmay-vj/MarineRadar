import asyncio
import json
import os

import websockets
from dotenv import load_dotenv


# ============================================================
# LOAD API KEY
# ============================================================

load_dotenv()

API_KEY = os.getenv("PELYR_API_KEY")

if not API_KEY:
    raise RuntimeError("PELYR_API_KEY not found in .env")


# ============================================================
# PELYR STREAM
# ============================================================

URL = "wss://stream.pelyr.com/v1/stream"


async def main():

    print("Connecting to Pelyr WebSocket...")

    async with websockets.connect(
        URL,
        additional_headers={
            "Authorization": f"Bearer {API_KEY}"
        },
        ping_interval=None
    ) as ws:

        print("Connected successfully.")
        print("Waiting for welcome message...")

        # ----------------------------------------------------
        # Welcome
        # ----------------------------------------------------

        raw = await ws.recv()

        frame = json.loads(raw)

        print("\nWELCOME:")
        print(json.dumps(frame, indent=2))


        # ----------------------------------------------------
        # Subscribe to global vessel positions
        # ----------------------------------------------------

        subscription = {
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

        await ws.send(
            json.dumps(subscription)
        )

        print("\nSubscription sent.")
        print("Waiting for AIS positions...\n")


        # ----------------------------------------------------
        # Receive stream
        # ----------------------------------------------------

        position_count = 0

        async for raw in ws:

            frame = json.loads(raw)

            frame_type = frame.get("type")


            # -----------------------------------------------
            # Subscription confirmation
            # -----------------------------------------------

            if frame_type == "subscribed":

                print("SUBSCRIBED:")
                print(json.dumps(frame, indent=2))


            # -----------------------------------------------
            # Vessel position
            # -----------------------------------------------

            elif frame_type == "position":

                data = frame.get("data", {})

                print(
                    f"MMSI: {data.get('mmsi')} | "
                    f"Lat: {data.get('lat')} | "
                    f"Lon: {data.get('lon')} | "
                    f"Speed: {data.get('sog')} | "
                    f"Course: {data.get('cog')} | "
                    f"Time: {data.get('rx_ts')}"
                )

                position_count += 1

                # Stop after 10 positions for testing
                if position_count >= 10:

                    print(
                        "\nReceived 10 AIS positions successfully."
                    )

                    break


            # -----------------------------------------------
            # Heartbeat
            # -----------------------------------------------

            elif frame_type == "heartbeat":

                print(
                    "Heartbeat received."
                )


            # -----------------------------------------------
            # Error
            # -----------------------------------------------

            elif frame_type == "error":

                print(
                    "\nSTREAM ERROR:"
                )

                print(
                    json.dumps(frame, indent=2)
                )

                break


            # -----------------------------------------------
            # Notice
            # -----------------------------------------------

            elif frame_type == "notice":

                print(
                    "\nSTREAM NOTICE:"
                )

                print(
                    json.dumps(frame, indent=2)
                )


# ============================================================
# START
# ============================================================

try:

    asyncio.run(main())

except Exception as e:

    print("\nConnection failed:")
    print(type(e).__name__, e)