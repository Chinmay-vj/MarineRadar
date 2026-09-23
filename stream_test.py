"""Manually verify that an AISStream key receives live position messages."""

import asyncio
import json

import websockets
from dotenv import load_dotenv

from stream_client import (
    AISSTREAM_POSITION_TYPES,
    WS_URL,
    get_subscription,
    normalize_aisstream_position,
)


async def main():
    load_dotenv()
    subscription = get_subscription()

    print("Connecting to AISStream WebSocket...")
    async with websockets.connect(
        WS_URL,
        compression="deflate",
        ping_interval=20,
        ping_timeout=20,
    ) as ws:
        await ws.send(json.dumps(subscription))
        print("Subscription sent. Waiting for 10 AIS positions...\n")

        position_count = 0
        async for raw in ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            frame = json.loads(raw)

            if frame.get("MessageType") == "SubscriptionConfirmation":
                print("Subscription confirmed.")
                continue

            if frame.get("MessageType") not in AISSTREAM_POSITION_TYPES:
                continue

            position = normalize_aisstream_position(frame)
            print(
                f"MMSI: {position.get('mmsi')} | "
                f"Lat: {position.get('lat')} | "
                f"Lon: {position.get('lon')} | "
                f"Speed: {position.get('sog')} | "
                f"Course: {position.get('cog')} | "
                f"Time: {position.get('rx_ts')}"
            )
            position_count += 1

            if position_count >= 10:
                print("\nReceived 10 AIS positions successfully.")
                return


if __name__ == "__main__":
    asyncio.run(main())
