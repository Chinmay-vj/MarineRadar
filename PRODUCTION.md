# MarineRadar / Global Ship Tracker - Phase 20: Production Platform Runbook

This guide documents the enterprise deployment, operations, and maintenance procedures for the **Global Ship Tracker (MarineRadar)** production platform.

---

## 1. Production Architecture

The production platform runs as a coordinated multi-container Docker Compose stack with native PostGIS spatial acceleration:

```
                          ┌──────────────────────────┐
                          │    AISStream.io Feed     │
                          └─────────────┬────────────┘
                                        │ (WSS)
                                        ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        Docker Compose Stack                            │
│                                                                        │
│   ┌────────────────────┐                   ┌───────────────────────┐   │
│   │   ingest Service   │                   │      api Service      │   │
│   │ (stream_client.py) │                   │  (map_server:app)     │   │
│   │  • Kinematic Filter│                   │  • Uvicorn (2 workers)│   │
│   │  • Heartbeat Writer│                   │  • CORS & GZip        │   │
│   │  • Batch Ingestion │                   │  • Health Endpoint    │   │
│   └─────────┬──────────┘                   └───────────▲───────────┘   │
│             │                                          │               │
│             │ (Writes)                                 │ (Reads)       │
│             ▼                                          ▼               │
│   ┌────────────────────────────────────────────────────────────────┐   │
│   │                       db Service (PostGIS 16)                  │   │
│   │          • Native GEOGRAPHY(Point, 4326) Points                │   │
│   │          • GiST Spatial Indexes (idx_pg_vessels_geom)          │   │
│   │          • Volume: pgdata                                      │   │
│   └────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────▲─────────────────────┘
                                                   │ (Port 8502)
                                                   │
                                        ┌──────────┴──────────┐
                                        │  Operator / Client  │
                                        │ (Browser Leaflet UI)│
                                        └─────────────────────┘
```

---

## 2. Prerequisites

- **Docker**: Engine version 24.0+ and Docker Compose v2.20+
- **AISStream API Key**: Active key for `wss://stream.aisstream.io/v0/stream`
- **Network**: Outbound WebSocket access on port 443; inbound port 8502 (or reverse proxy port 80/443).

---

## 3. Quickstart Deployment

### Step 1: Clone & Configure Environment
```bash
cp .env.example .env
```
Edit `.env` and fill in your `AISSTREAM_API_KEY` and a strong `POSTGRES_PASSWORD`:
```bash
AISSTREAM_API_KEY=your_actual_api_key_here
POSTGRES_PASSWORD=your_secure_password_here
```

AISStream requires geographic bounding boxes. The default is global coverage;
for a smaller, lower-volume deployment, set `AISSTREAM_BOUNDING_BOXES` as JSON:
```bash
AISSTREAM_BOUNDING_BOXES=[[[25.835,-80.208],[25.603,-79.879]]]
```

### Step 2: Start the Production Stack
```bash
docker compose up -d --build
```

### Step 3: Verify Container Health
```bash
docker compose ps
```
All three services (`marineradar_db`, `marineradar_api`, and `marineradar_ingest`) will report `Up (healthy)`.

### Step 4: Access the Live System
- **Interactive Leaflet Map**: `http://localhost:8502/map`
- **Health Check & Telemetry**: `http://localhost:8502/health`
- **Interactive OpenAPI Documentation**: `http://localhost:8502/docs`

---

## 4. Dual-Engine Database Architecture

The application implements an **adaptive dual-engine database layer**:

| Setting | Engine Used | Use Case |
| :--- | :--- | :--- |
| `DATABASE_URL` is set | **PostgreSQL / PostGIS** | Production Docker stack, enterprise deployments, cloud databases |
| `DATABASE_URL` is unset | **SQLite (`data/ships.db`)** | Local development, offline testing, single-node evaluation |

Both engines support identical data models:
- `vessels`: Current dynamic ship state (MMSI, lat, lon, SOG, COG, heading, last seen)
- `positions`: Historical trajectory breadcrumbs with unique constraint on `(mmsi, timestamp)`
- `vessel_metadata`: Dimensions (length, beam), IMO, ship name, destination, ETA
- `maritime_alerts`: Stateful alert lifecycle (`active` $\rightarrow$ `acknowledged` $\rightarrow$ `resolved`)

---

## 5. SQLite to PostGIS Data Migration

If you have historical data accumulated in local SQLite (`data/ships.db`), migrate it directly into the running PostGIS instance:

```bash
# Execute migration from within the API container or host
python migrate_to_postgis.py \
    --sqlite data/ships.db \
    --dsn "postgresql://marineradar:your_password@localhost:5432/marineradar" \
    --batch-size 2000
```

The migration script creates all tables, enables the `postgis` extension, copies positions in configurable chunks, computes `geom = ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography`, and applies GiST spatial indexes.

---

## 6. Health & Liveness Monitoring

### API & Engine Health Endpoint (`GET /health`)
Querying `http://localhost:8502/health` returns structured JSON telemetry:

```json
{
  "status": "ok",
  "service": "Global Ship Tracker Map API",
  "version": "1.0.0",
  "uptime_seconds": 1420.5,
  "database": {
    "engine": "PostgreSQL/PostGIS",
    "postgis_configured": true,
    "postgres_health": {
      "status": "connected",
      "latency_ms": 1.42,
      "vessels": 4821,
      "positions": 128490
    }
  },
  "ingestion": {
    "status": "streaming",
    "timestamp": "2026-09-23T18:35:00Z",
    "pid": 12,
    "accepted": 4520,
    "rejected_jump": 3,
    "rejected_coordinates": 0
  }
}
```

### Ingestion Liveness Heartbeat
The `ingest` worker continuously records its health to `data/stream_heartbeat.json`:
- If the file's `timestamp` stops updating or status transitions to `stopped`, container orchestrators can trigger an automatic container restart.

---

## 7. Backup & Recovery Runbook

### Creating a PostGIS Backup
```bash
docker exec -t marineradar_db pg_dump -U marineradar -d marineradar -F c -b -v -f /tmp/backup.dump
docker cp marineradar_db:/tmp/backup.dump ./marineradar_backup_$(date +%Y%m%d).dump
```

### Restoring from Backup
```bash
docker cp ./marineradar_backup.dump marineradar_db:/tmp/backup.dump
docker exec -t marineradar_db pg_restore -U marineradar -d marineradar -v --clean /tmp/backup.dump
```

---

## 8. Production Hardening Features Implemented

1. **Non-Root Execution**: Containers run under unprivileged user `appuser` (UID 10001).
2. **CORS Control**: Cross-Origin Resource Sharing is controlled via the `CORS_ORIGINS` environment variable.
3. **HTTP Compression**: `GZipMiddleware` automatically compresses all API responses exceeding 1,000 bytes.
4. **Resilient Signal Handling**: Ingestion daemon intercepts `SIGINT` and `SIGTERM`, flushes queues to the database, records a clean heartbeat state, and terminates gracefully.
5. **Continuous Integration**: Automated GitHub Actions pipeline (`.github/workflows/ci.yml`) runs tests on Python 3.11 and 3.12 and verifies the Docker container build.
