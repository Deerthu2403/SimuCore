# SimuCore

**An Intelligent, Self-Calibrating 3D Digital Twin Framework for Banking Infrastructure**

SimuCore mirrors a banking software estate — microservices, databases, servers,
network devices and their dependencies — in an interactive 3D environment. It
monitors the twin in real time, predicts failures, analyses how failures
propagate through dependencies, runs what-if simulations, optimises resources
with NSGA-II, calibrates itself from actual-vs-predicted outcomes and explains
every recommendation.

> **Status: foundation, simulated banking microservices, live twin.** The 3D twin shows either its
> built-in synthetic data or the eight services running in [`banking-sim/`](banking-sim). No real
> bank is involved.
> Production data enters later only through secure, read-only telemetry adapters.

## Capabilities and status

| Capability | Module | Phase | Status |
|---|---|---|---|
| Real-time infrastructure monitoring | `backend/app/twin`, `adapters` | 1 | ✅ working (simulated source) |
| Interactive 3D digital twin | `frontend/src/scene` | 1 | ✅ working |
| Dependency-aware failure propagation | `backend/app/propagation` | 1 / 4 | ✅ v1 (weighted max-product BFS) |
| Explainable recommendations | `backend/app/explain`, `recommendation` | 1 / 5 | ✅ propagation explanations; rest in P5 |
| AI anomaly & failure prediction | `backend/app/ml` | 3 | 📐 contract defined |
| What-if simulation (SimPy) | `backend/app/simulation/whatif.py` | 4 | 📐 contract defined |
| Resource optimisation (NSGA-II) | `backend/app/optimization` | 5 | 📐 contract defined |
| Self-calibration | `backend/app/calibration` | 5 | 📐 contract defined |

## Repository layout

```
SimuCore/
├── backend/                  FastAPI service (Python)
│   ├── app/
│   │   ├── api/v1/           REST routes (health, twin, propagation) + WebSocket
│   │   ├── core/             config, logging, event bus
│   │   ├── domain/           models, inter-module contracts, topology graph
│   │   ├── adapters/         read-only telemetry adapters (simulated, prometheus)
│   │   ├── simulation/       reference bank topology, synthetic telemetry, SimPy what-if
│   │   ├── digital_twin/     twin core (API v2): model, health, sync, history
│   │   ├── twin/             Phase 1 twin state + synchroniser (API v1)
│   │   ├── propagation/      failure propagation engine
│   │   ├── ml/               anomaly detection & failure prediction   (P3)
│   │   ├── optimization/     NSGA-II optimiser                         (P5)
│   │   ├── calibration/      self-calibration loop                     (P5)
│   │   ├── explain/          explanation builders
│   │   ├── recommendation/   recommendation service                    (P5)
│   │   ├── db/               SQLAlchemy persistence                    (P2)
│   │   └── main.py           application factory
│   ├── tests/                pytest suite
│   └── requirements*.txt
├── frontend/                 React + TypeScript + React Three Fiber
│   └── src/{scene,components,store,services,types}
├── banking-sim/              Eight simulated banking microservices, workloads, failure scenarios
├── monitoring/               Monitoring service: collection, checks, history, streaming
├── database/schema/          PostgreSQL DDL (auto-applied by the container)
├── infra/                    docker-compose, Prometheus, Grafana provisioning
└── docs/                     architecture & design documents
```

## Documentation

1. [System architecture](docs/01-system-architecture.md)
2. [Backend & frontend architecture](docs/02-backend-frontend-architecture.md)
3. [Twin synchronisation & secure adapters](docs/03-twin-synchronization.md)
4. [Module / algorithm communication](docs/04-module-communication.md)
5. [Database schema](docs/05-database-schema.md)
6. [3D user interface](docs/06-3d-user-interface.md)
7. [Development roadmap](docs/07-roadmap.md)
8. [Simulated banking microservices](docs/08-banking-microservices.md)
9. [Monitoring and data collection](docs/09-monitoring.md)
10. [Digital twin core](docs/10-digital-twin-core.md)

## Getting started (local, Windows PowerShell)

Prerequisites: Python 3.11+, Node.js 20+. Docker Desktop is optional (needed
only for PostgreSQL / Prometheus / Grafana).

```powershell
# 1. Backend
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
```

```powershell
# 2. Frontend (new terminal)
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 — the twin connects over WebSocket and starts
updating every second. Click a component, then **Simulate failure → blast radius**.

API docs: http://localhost:8000/docs · Prometheus metrics: http://localhost:8000/metrics/

### Show the running banking services

```powershell
cd banking-sim; .\.venv\Scripts\python bankctl.py up
```

```powershell
cd backend; .\.venv\Scripts\python -m uvicorn app.main:app --env-file bank-services.env --port 8000
```

Then `npm run dev` in `frontend` as above. Leave out `--env-file` to use the built-in
simulation. Docker Compose for the backend still uses the built-in simulation.

### Full stack with Docker

```powershell
Copy-Item .env.example .env
```

Fill in the six empty passwords in `.env` (the file says how), then:

```powershell
docker compose --env-file .env -f infra/docker-compose.yml up -d --build
```

Without `--env-file` it refuses to start rather than using default passwords. All ports are reachable
from this PC only. PostgreSQL and Grafana take their passwords on first start; to change them later,
remove the volumes (this deletes the data): `docker compose --env-file .env -f
infra/docker-compose.yml down --volumes`.

| Service | URL |
|---|---|
| Frontend | http://localhost:8080 |
| Backend API | http://localhost:8000/docs |
| Monitoring API | http://localhost:8200/docs |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 (dashboard *SimuCore - Twin Overview*) |
| PostgreSQL | localhost:5432 (schema from `database/schema/001_init.sql`) |

### Tests

```powershell
cd backend; .\.venv\Scripts\python -m pytest -q
cd frontend; npm run typecheck; npm run build
```

```powershell
backend\.venv\Scripts\python infra/tests/stack_check.py   # the whole Docker stack, about 3 minutes
```

Latest stack results: [infra/results/stack.md](infra/results/stack.md).

Optional ML stack for Phase 3+: `pip install -r requirements-ml.txt`.
