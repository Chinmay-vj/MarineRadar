"""Manually verify that AISStream static/voyage messages are available."""

import asyncio
import json
import signal

import websockets
from dotenv import load_dotenv

from stream_client import (
    AISSTREAM_STATIC_TYPES,
    WS_URL,
    get_subscription,
    normalize_aisstream_static,
    process_static_metadata,
)


TEST_DURATION_SECONDS = 180
shutdown_event = asyncio.Event()


async def run():
    load_dotenv()
    subscription = get_subscription()
    stats = {"static_frames": 0, "metadata_updates": 0}
    seen_mmsis = set()

    print("AISStream static/voyage message test. This does not modify the database.")
    async with websockets.connect(
        WS_URL,
        compression="deflate",
        ping_interval=20,
        ping_timeout=20,
    ) as ws:
        await ws.send(json.dumps(subscription))
        try:
            async with asyncio.timeout(TEST_DURATION_SECONDS):
                async for raw in ws:
                    if shutdown_event.is_set():
                        break

                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    event = json.loads(raw)
                    if event.get("MessageType") not in AISSTREAM_STATIC_TYPES:
                        continue

                    stats["static_frames"] += 1
                    metadata = process_static_metadata(
                        normalize_aisstream_static(event) or {}
                    )
                    if metadata is None:
                        continue

                    stats["metadata_updates"] += 1
                    seen_mmsis.add(metadata["mmsi"])
                    print(
                        f"MMSI {metadata['mmsi']} | "
                        f"name={metadata.get('shipname') or '-'} | "
                        f"destination={metadata.get('destination') or '-'}"
                    )
        except TimeoutError:
            pass

    print(f"Static frames received: {stats['static_frames']}")
    print(f"Metadata updates: {stats['metadata_updates']}")
    print(f"Unique vessels: {len(seen_mmsis)}")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: shutdown_event.set())
    signal.signal(signal.SIGTERM, lambda *_: shutdown_event.set())
    asyncio.run(run())
