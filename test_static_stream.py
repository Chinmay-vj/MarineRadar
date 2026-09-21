import asyncio
import json
import os
import random
import signal
import sqlite3
from datetime import datetime, timezone

import websockets
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.path.join("data", "ships.db")
WS_URL = "wss://stream.pelyr.com/v1/stream"

# Pelyr currently allows 4 subscriptions/connection and 500 MMSIs/subscription.
# We reserve 1 subscription for the live position feed and use 3 slots for static AIS.
STATIC_SUB_IDS = ["static_1", "static_2", "static_3"]
STATIC_BATCH_SIZE = 500
TEST_DURATION_SECONDS = 180
ROTATE_SECONDS = 60

shutdown_event = asyncio.Event()


def get_recent_mmsis(limit=1500):
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"Database not found: {DB_PATH}")

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT mmsi
            FROM vessels
            WHERE mmsi IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [str(row[0]) for row in rows]


def chunk(values, size):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def clean_value(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def merge_metadata(store, data):
    mmsi = str(data.get("mmsi") or "").strip()
    if not mmsi:
        return False

    row = store.setdefault(mmsi, {})

    # Merge field-by-field because AIS type 24 arrives in two separate parts.
    fields = [
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
    ]

    changed = False
    for field in fields:
        value = clean_value(data.get(field))
        if value is not None:
            if row.get(field) != value:
                row[field] = value
                changed = True

    if changed:
        row["last_rx_ts"] = data.get("rx_ts")
        row["msg_type"] = data.get("msg_type")

    return changed


def print_metadata(mmsi, row):
    print(
        f"  MMSI {mmsi} | "
        f"name={row.get('shipname') or '-'} | "
        f"IMO={row.get('imo') or '-'} | "
        f"callsign={row.get('callsign') or '-'} | "
        f"type={row.get('shiptype') if row.get('shiptype') is not None else '-'} | "
        f"destination={row.get('destination') or '-'} | "
        f"ETA={row.get('eta') or '-'}"
    )


async def send_subscribe(ws, subscription_id, mmsis):
    payload = {
        "type": "subscribe",
        "id": subscription_id,
        "mmsi": [int(m) for m in mmsis],
        "msg_types": [5, 24],
        "include_positionless": True,
        "fields": "full",
    }
    await ws.send(json.dumps(payload))
    print(
        f"Subscribed request sent: {subscription_id} "
        f"({len(mmsis)} MMSIs)"
    )


async def rotate_static_subscriptions(ws, mmsis, start_index):
    batches = []
    for offset in range(len(STATIC_SUB_IDS)):
        batch_start = start_index + offset * STATIC_BATCH_SIZE
        batch = mmsis[batch_start:batch_start + STATIC_BATCH_SIZE]
        if batch:
            batches.append((STATIC_SUB_IDS[offset], batch))

    # If fewer than 3 batches remain, wrap around so every slot stays active.
    if len(batches) < len(STATIC_SUB_IDS) and mmsis:
        for offset in range(len(batches), len(STATIC_SUB_IDS)):
            batch_start = ((start_index + offset * STATIC_BATCH_SIZE) % len(mmsis))
            batch = [
                mmsis[(batch_start + i) % len(mmsis)]
                for i in range(min(STATIC_BATCH_SIZE, len(mmsis)))
            ]
            batches.append((STATIC_SUB_IDS[offset], batch))

    for subscription_id, batch in batches:
        await send_subscribe(ws, subscription_id, batch)


async def receive_stream(ws, metadata_store, stats):
    async for raw in ws:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            stats["invalid_json"] += 1
            continue

        frame_type = frame.get("type")

        if frame_type == "welcome":
            sources = frame.get("sources", [])
            print("\nPelyr welcome received.")
            print(f"Connection limits: {frame.get('limits', {})}")
            for source in sources:
                attribution = source.get("attribution")
                if attribution:
                    print(f"Attribution: {attribution}")
            continue

        if frame_type == "subscribed":
            effective = frame.get("effective", {})
            print(
                f"Subscription confirmed: {frame.get('id')} | "
                f"MMSIs={effective.get('mmsi_count', 0)} | "
                f"msg_types={effective.get('msg_types', [])}"
            )
            notes = frame.get("notes", [])
            for note in notes:
                print(f"  note: {note}")
            continue

        if frame_type == "error":
            stats["errors"] += 1
            print(f"Pelyr error: {frame}")
            continue

        if frame_type == "heartbeat":
            feed = frame.get("feed", "unknown")
            dropped = frame.get("dropped", 0)
            if dropped:
                print(f"Heartbeat: feed={feed}, dropped={dropped}")
            continue

        if frame_type == "notice":
            print(f"Pelyr notice: {frame}")
            continue

        if frame_type != "position":
            continue

        data = frame.get("data") or {}
        msg_type = data.get("msg_type")

        if msg_type not in (5, 24):
            continue

        stats["static_frames"] += 1

        if merge_metadata(metadata_store, data):
            stats["metadata_updates"] += 1
            mmsi = str(data.get("mmsi"))
            print(
                f"\nStatic AIS update #{stats['metadata_updates']} "
                f"(type {msg_type}):"
            )
            print_metadata(mmsi, metadata_store[mmsi])


async def run():
    api_key = os.getenv("PELYR_API_KEY")
    if not api_key:
        raise RuntimeError("PELYR_API_KEY is missing from .env")

    mmsis = get_recent_mmsis(1500)

    if not mmsis:
        raise RuntimeError("No MMSIs found in data/ships.db")

    print("=" * 70)
    print("GLOBAL SHIP TRACKER - 11D-2 STATIC AIS STREAM TEST")
    print("=" * 70)
    print(f"Database: {os.path.abspath(DB_PATH)}")
    print(f"MMSIs loaded from current vessel cache: {len(mmsis)}")
    print(f"Test duration: {TEST_DURATION_SECONDS} seconds")
    print(f"Static rotation interval: {ROTATE_SECONDS} seconds")
    print()
    print("This test does NOT modify the database.")
    print("It only verifies that Pelyr static AIS metadata can be received.")

    metadata_store = {}
    stats = {
        "static_frames": 0,
        "metadata_updates": 0,
        "errors": 0,
        "invalid_json": 0,
    }

    headers = {"Authorization": f"Bearer {api_key}"}

    async with websockets.connect(
        WS_URL,
        additional_headers=headers,
        ping_interval=20,
        ping_timeout=20,
        max_size=None,
    ) as ws:
        # Pelyr requires the first subscription within 5 seconds.
        await ws.send(
            json.dumps(
                {
                    "type": "subscribe",
                    "id": "global_positions",
                    "bbox": [
                        {
                            "west": -180,
                            "south": -85,
                            "east": 180,
                            "north": 85,
                        }
                    ],
                    "fields": "position",
                }
            )
        )

        await rotate_static_subscriptions(ws, mmsis, 0)

        receiver_task = asyncio.create_task(
            receive_stream(ws, metadata_store, stats)
        )

        started = asyncio.get_running_loop().time()
        next_rotation = started + ROTATE_SECONDS
        rotation_index = 3 * STATIC_BATCH_SIZE

        try:
            while (
                asyncio.get_running_loop().time() - started
                < TEST_DURATION_SECONDS
            ):
                if shutdown_event.is_set():
                    break

                now = asyncio.get_running_loop().time()
                if now >= next_rotation and len(mmsis) > STATIC_BATCH_SIZE:
                    rotation_index %= len(mmsis)
                    print(
                        f"\nRotating static MMSI batches "
                        f"(start index {rotation_index})..."
                    )
                    await rotate_static_subscriptions(
                        ws, mmsis, rotation_index
                    )
                    rotation_index += 3 * STATIC_BATCH_SIZE
                    next_rotation = now + ROTATE_SECONDS

                await asyncio.sleep(0.5)

        finally:
            receiver_task.cancel()
            try:
                await receiver_task
            except asyncio.CancelledError:
                pass

    print("\n" + "=" * 70)
    print("11D-2 STATIC STREAM TEST COMPLETE")
    print("=" * 70)
    print(f"Static frames received: {stats['static_frames']}")
    print(f"Metadata field updates: {stats['metadata_updates']}")
    print(f"Pelyr errors: {stats['errors']}")
    print(f"Invalid JSON frames: {stats['invalid_json']}")
    print(f"Unique MMSIs with metadata: {len(metadata_store)}")

    named = sum(
        1 for row in metadata_store.values()
        if row.get("shipname")
    )
    destinations = sum(
        1 for row in metadata_store.values()
        if row.get("destination")
    )

    print(f"Vessels with shipname: {named}")
    print(f"Vessels with destination: {destinations}")
    print()
    print("Database was NOT modified by this test.")


def request_shutdown():
    shutdown_event.set()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: request_shutdown())
    signal.signal(signal.SIGTERM, lambda *_: request_shutdown())

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nStopped by user.")
    except Exception as exc:
        print(f"\n11D-2 test failed: {exc}")
        raise
