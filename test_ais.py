"""Run a short AISStream connection check without writing to the database."""

import asyncio
import json

import websockets
from dotenv import load_dotenv

from stream_client import AISSTREAM_POSITION_TYPES, WS_URL, get_subscription


async def run_live_check():
    load_dotenv()
    subscription = get_subscription()
    received = 0

    async with websockets.connect(WS_URL, compression="deflate") as ws:
        await ws.send(json.dumps(subscription))

        async for raw in ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            event = json.loads(raw)

            if event.get("MessageType") in AISSTREAM_POSITION_TYPES:
                received += 1
                if received == 10:
                    print("AISStream key verified: received 10 position events.")
                    return


if __name__ == "__main__":
    asyncio.run(run_live_check())
