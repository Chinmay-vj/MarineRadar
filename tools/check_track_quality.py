import sqlite3
import math
from pathlib import Path
from datetime import datetime


# ============================================================
# DATABASE
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "ships.db"


# ============================================================
# CONFIGURATION
# ============================================================

MAX_TIME_GAP_MINUTES = 30
MAX_JUMP_DISTANCE_KM = 30
MAX_REASONABLE_SPEED_KNOTS = 50

TOP_RESULTS = 30


# ============================================================
# HAVERSINE DISTANCE
# ============================================================

def calculate_distance_km(
    lat1,
    lon1,
    lat2,
    lon2
):

    earth_radius_km = 6371.0

    lat1 = math.radians(lat1)
    lon1 = math.radians(lon1)

    lat2 = math.radians(lat2)
    lon2 = math.radians(lon2)

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        math.sin(dlat / 2) ** 2
        +
        math.cos(lat1)
        *
        math.cos(lat2)
        *
        math.sin(dlon / 2) ** 2
    )

    c = 2 * math.atan2(
        math.sqrt(a),
        math.sqrt(1 - a)
    )

    return earth_radius_km * c


# ============================================================
# PARSE TIMESTAMP
# ============================================================

def parse_timestamp(timestamp):

    if not timestamp:
        return None

    try:

        return datetime.fromisoformat(
            timestamp.replace(
                "Z",
                "+00:00"
            )
        )

    except ValueError:

        return None


# ============================================================
# MAIN DIAGNOSTIC
# ============================================================

def main():

    print("=" * 70)
    print("GLOBAL SHIP TRACKER - AIS TRACK QUALITY DIAGNOSTIC")
    print("=" * 70)

    print()
    print(f"Database: {DB_PATH}")

    if not DB_PATH.exists():

        print()
        print("ERROR: Database does not exist.")
        return


    connection = sqlite3.connect(
        DB_PATH
    )

    cursor = connection.cursor()


    # ========================================================
    # GET CONSECUTIVE AIS POSITIONS
    # ========================================================

    print()
    print("Reading consecutive AIS positions...")

    cursor.execute(
        """
        SELECT
            mmsi,
            latitude,
            longitude,
            timestamp,

            LAG(latitude)
                OVER (
                    PARTITION BY mmsi
                    ORDER BY timestamp
                ) AS previous_latitude,

            LAG(longitude)
                OVER (
                    PARTITION BY mmsi
                    ORDER BY timestamp
                ) AS previous_longitude,

            LAG(timestamp)
                OVER (
                    PARTITION BY mmsi
                    ORDER BY timestamp
                ) AS previous_timestamp

        FROM positions

        ORDER BY
            mmsi,
            timestamp
        """
    )

    rows = cursor.fetchall()

    connection.close()


    print(
        f"Consecutive position pairs found: {len(rows):,}"
    )


    # ========================================================
    # ANALYZE
    # ========================================================

    large_time_gaps = []
    large_jumps = []
    impossible_speeds = []

    analyzed_pairs = 0


    for row in rows:

        (
            mmsi,
            latitude,
            longitude,
            timestamp,
            previous_latitude,
            previous_longitude,
            previous_timestamp
        ) = row


        if (
            previous_latitude is None
            or previous_longitude is None
            or previous_timestamp is None
        ):

            continue


        current_time = parse_timestamp(
            timestamp
        )

        previous_time = parse_timestamp(
            previous_timestamp
        )


        if (
            current_time is None
            or previous_time is None
        ):

            continue


        time_difference_seconds = (
            current_time - previous_time
        ).total_seconds()


        if time_difference_seconds <= 0:

            continue


        time_difference_minutes = (
            time_difference_seconds / 60
        )


        distance_km = calculate_distance_km(
            previous_latitude,
            previous_longitude,
            latitude,
            longitude
        )


        distance_nm = (
            distance_km * 0.539957
        )


        time_hours = (
            time_difference_seconds / 3600
        )


        implied_speed_knots = (
            distance_nm / time_hours
        )


        analyzed_pairs += 1


        result = {

            "mmsi": mmsi,

            "previous_timestamp":
                previous_timestamp,

            "timestamp":
                timestamp,

            "previous_lat":
                previous_latitude,

            "previous_lon":
                previous_longitude,

            "lat":
                latitude,

            "lon":
                longitude,

            "time_gap_minutes":
                time_difference_minutes,

            "distance_km":
                distance_km,

            "speed_knots":
                implied_speed_knots

        }


        if (
            time_difference_minutes
            >
            MAX_TIME_GAP_MINUTES
        ):

            large_time_gaps.append(
                result
            )


        if (
            distance_km
            >
            MAX_JUMP_DISTANCE_KM
        ):

            large_jumps.append(
                result
            )


        if (
            implied_speed_knots
            >
            MAX_REASONABLE_SPEED_KNOTS
        ):

            impossible_speeds.append(
                result
            )


    # ========================================================
    # SUMMARY
    # ========================================================

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)

    print()
    print(
        f"Analyzed position pairs: "
        f"{analyzed_pairs:,}"
    )

    print(
        f"Time gaps > "
        f"{MAX_TIME_GAP_MINUTES} min: "
        f"{len(large_time_gaps):,}"
    )

    print(
        f"Jumps > "
        f"{MAX_JUMP_DISTANCE_KM} km: "
        f"{len(large_jumps):,}"
    )

    print(
        f"Implied speed > "
        f"{MAX_REASONABLE_SPEED_KNOTS} knots: "
        f"{len(impossible_speeds):,}"
    )


    # ========================================================
    # WORST JUMPS
    # ========================================================

    print()
    print("=" * 70)
    print(
        f"TOP {TOP_RESULTS} LARGEST POSITION JUMPS"
    )
    print("=" * 70)


    large_jumps.sort(
        key=lambda item:
            item["distance_km"],
        reverse=True
    )


    for index, item in enumerate(
        large_jumps[:TOP_RESULTS],
        start=1
    ):

        print()
        print(
            f"{index}. MMSI: "
            f"{item['mmsi']}"
        )

        print(
            f"   Time: "
            f"{item['previous_timestamp']}"
            f" -> "
            f"{item['timestamp']}"
        )

        print(
            f"   Position: "
            f"({item['previous_lat']:.5f}, "
            f"{item['previous_lon']:.5f})"
            f" -> "
            f"({item['lat']:.5f}, "
            f"{item['lon']:.5f})"
        )

        print(
            f"   Time gap: "
            f"{item['time_gap_minutes']:.1f} min"
        )

        print(
            f"   Distance: "
            f"{item['distance_km']:.1f} km"
        )

        print(
            f"   Implied speed: "
            f"{item['speed_knots']:.1f} knots"
        )


    # ========================================================
    # WORST SPEEDS
    # ========================================================

    print()
    print("=" * 70)
    print(
        f"TOP {TOP_RESULTS} HIGHEST IMPLIED SPEEDS"
    )
    print("=" * 70)


    impossible_speeds.sort(
        key=lambda item:
            item["speed_knots"],
        reverse=True
    )


    for index, item in enumerate(
        impossible_speeds[:TOP_RESULTS],
        start=1
    ):

        print()
        print(
            f"{index}. MMSI: "
            f"{item['mmsi']}"
        )

        print(
            f"   Time: "
            f"{item['previous_timestamp']}"
            f" -> "
            f"{item['timestamp']}"
        )

        print(
            f"   Time gap: "
            f"{item['time_gap_minutes']:.1f} min"
        )

        print(
            f"   Distance: "
            f"{item['distance_km']:.1f} km"
        )

        print(
            f"   Implied speed: "
            f"{item['speed_knots']:.1f} knots"
        )


    # ========================================================
    # FINISHED
    # ========================================================

    print()
    print("=" * 70)
    print("DIAGNOSTIC COMPLETE")
    print("=" * 70)


if __name__ == "__main__":

    main()