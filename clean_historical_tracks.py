import sqlite3
import math
from datetime import datetime

DB_PATH = "data/ships.db"

MAX_CONTINUITY_GAP_SECONDS = 30 * 60
MAX_IMPLIED_SPEED_KNOTS = 50.0


def parse_timestamp(value):
    """Parse an ISO timestamp into a comparable UTC-aware datetime."""
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in kilometres."""
    earth_radius_km = 6371.0088

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)

    delta_lat = math.radians(lat2 - lat1)

    # Normalize longitude difference so the International
    # Date Line does not create an artificial 360-degree jump.
    delta_lon_deg = (lon2 - lon1 + 180.0) % 360.0 - 180.0
    delta_lon = math.radians(delta_lon_deg)

    a = (
        math.sin(delta_lat / 2.0) ** 2
        +
        math.cos(lat1_rad)
        * math.cos(lat2_rad)
        * math.sin(delta_lon / 2.0) ** 2
    )

    a = min(1.0, max(0.0, a))

    return (
        earth_radius_km
        * 2.0
        * math.asin(math.sqrt(a))
    )


def implied_speed_knots(distance_km, elapsed_seconds):
    if elapsed_seconds <= 0:
        return float("inf")

    km_per_hour = distance_km / (elapsed_seconds / 3600.0)

    return km_per_hour / 1.852


def main():
    print("=" * 70)
    print("GLOBAL SHIP TRACKER - HISTORICAL AIS CLEANUP")
    print("=" * 70)
    print()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        # ------------------------------------------------------------
        # Read all historical positions.
        # ------------------------------------------------------------
        rows = conn.execute("""
            SELECT
                id,
                mmsi,
                latitude,
                longitude,
                sog,
                cog,
                heading,
                timestamp
            FROM positions
            ORDER BY mmsi, timestamp, id
        """).fetchall()

        print(f"Historical positions loaded: {len(rows):,}")
        print()

        if not rows:
            print("No historical positions found.")
            return

        # ------------------------------------------------------------
        # Identify impossible observations.
        #
        # Important:
        # - A long gap (>30 minutes) is NOT considered corruption.
        # - A short gap with an implied speed >50 knots is rejected.
        # - The rejected point is NOT used as the reference for the
        #   following point.
        # ------------------------------------------------------------
        last_accepted = {}
        rejected_ids = []

        rejected_by_mmsi = {}

        for row in rows:
            mmsi = str(row["mmsi"])

            try:
                latitude = float(row["latitude"])
                longitude = float(row["longitude"])
                timestamp = parse_timestamp(row["timestamp"])

            except (TypeError, ValueError):
                # These should already have been caught at ingestion,
                # but historical cleanup also protects against them.
                rejected_ids.append(row["id"])

                rejected_by_mmsi.setdefault(mmsi, 0)
                rejected_by_mmsi[mmsi] += 1

                continue

            # Geographic sanity check.
            if (
                not math.isfinite(latitude)
                or not math.isfinite(longitude)
                or not -90.0 <= latitude <= 90.0
                or not -180.0 <= longitude <= 180.0
            ):
                rejected_ids.append(row["id"])

                rejected_by_mmsi.setdefault(mmsi, 0)
                rejected_by_mmsi[mmsi] += 1

                continue

            # Historical null/sentinel position.
            if (
                abs(latitude) < 1e-9
                and abs(longitude) < 1e-9
            ):
                rejected_ids.append(row["id"])

                rejected_by_mmsi.setdefault(mmsi, 0)
                rejected_by_mmsi[mmsi] += 1

                continue

            previous = last_accepted.get(mmsi)

            if previous is not None:

                elapsed_seconds = (
                    timestamp - previous["timestamp"]
                ).total_seconds()

                # Out-of-order or duplicate timestamp.
                if elapsed_seconds <= 0:
                    rejected_ids.append(row["id"])

                    rejected_by_mmsi.setdefault(mmsi, 0)
                    rejected_by_mmsi[mmsi] += 1

                    continue

                # Only test physical continuity for short gaps.
                if elapsed_seconds <= MAX_CONTINUITY_GAP_SECONDS:

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

                    if speed_knots > MAX_IMPLIED_SPEED_KNOTS:

                        rejected_ids.append(row["id"])

                        rejected_by_mmsi.setdefault(mmsi, 0)
                        rejected_by_mmsi[mmsi] += 1

                        continue

            # This is now an accepted observation and becomes the
            # reference point for the next observation of this MMSI.
            last_accepted[mmsi] = {
                "latitude": latitude,
                "longitude": longitude,
                "timestamp": timestamp
            }

        print(f"Records marked for removal: {len(rejected_ids):,}")
        print(f"Records remaining: {len(rows) - len(rejected_ids):,}")
        print()

        if not rejected_ids:
            print("No invalid historical positions found.")
            return

        # ------------------------------------------------------------
        # Show a small summary before modifying the database.
        # ------------------------------------------------------------
        affected_vessels = len(rejected_by_mmsi)

        print(f"Affected MMSIs: {affected_vessels:,}")
        print()

        print("Top affected MMSIs:")

        for mmsi, count in sorted(
            rejected_by_mmsi.items(),
            key=lambda item: item[1],
            reverse=True
        )[:20]:
            print(f"  {mmsi}: {count:,} records")

        print()

        # ------------------------------------------------------------
        # Delete invalid historical observations.
        # ------------------------------------------------------------
        print("Removing invalid historical positions...")

        conn.execute("BEGIN")

        # Delete in batches so the SQL statement remains manageable.
        batch_size = 1000

        for start in range(0, len(rejected_ids), batch_size):
            batch = rejected_ids[start:start + batch_size]

            placeholders = ",".join(
                "?" for _ in batch
            )

            conn.execute(
                f"""
                DELETE FROM positions
                WHERE id IN ({placeholders})
                """,
                batch
            )

        # ------------------------------------------------------------
        # Rebuild current vessel state from the latest clean
        # historical observation for every MMSI.
        #
        # The vessels table is derived state; positions is the
        # historical source of truth.
        # ------------------------------------------------------------
        print("Rebuilding current vessel state...")

        conn.execute("DELETE FROM vessels")

        conn.execute("""
            INSERT INTO vessels (
                mmsi,
                latitude,
                longitude,
                sog,
                cog,
                heading,
                last_seen,
                updated_at
            )
            SELECT
                p.mmsi,
                p.latitude,
                p.longitude,
                p.sog,
                p.cog,
                p.heading,
                p.timestamp,
                p.received_at
            FROM positions p
            INNER JOIN (
                SELECT
                    mmsi,
                    MAX(timestamp) AS max_timestamp
                FROM positions
                GROUP BY mmsi
            ) latest
                ON p.mmsi = latest.mmsi
                AND p.timestamp = latest.max_timestamp
        """)

        conn.commit()

        # ------------------------------------------------------------
        # Verify.
        # ------------------------------------------------------------
        remaining_positions = conn.execute("""
            SELECT COUNT(*)
            FROM positions
        """).fetchone()[0]

        remaining_bad_coordinates = conn.execute("""
            SELECT COUNT(*)
            FROM positions
            WHERE latitude = 0
              AND longitude = 0
        """).fetchone()[0]

        vessel_count = conn.execute("""
            SELECT COUNT(*)
            FROM vessels
        """).fetchone()[0]

        print()
        print("=" * 70)
        print("CLEANUP COMPLETE")
        print("=" * 70)
        print(f"Positions remaining: {remaining_positions:,}")
        print(f"(0,0) positions remaining: {remaining_bad_coordinates:,}")
        print(f"Current vessels rebuilt: {vessel_count:,}")
        print(f"Historical records removed: {len(rejected_ids):,}")
        print()
        print("The database is now ready for the next track-quality diagnostic.")

    except Exception:
        conn.rollback()
        print()
        print("ERROR: Cleanup failed. Database transaction was rolled back.")
        raise

    finally:
        conn.close()


if __name__ == "__main__":
    main()
