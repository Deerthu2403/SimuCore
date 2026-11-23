# banking-sim

Eight simulated banking microservices with synthetic data, workloads and failure injection.
Design, dependency diagrams and limits: [docs/08-banking-microservices.md](../docs/08-banking-microservices.md).
Latest measured results: [results/scenarios.md](results/scenarios.md).

## Install (Python 3.12+)

```powershell
cd "D:\Major Projects\SimuCore\banking-sim"
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
```

## Run as local processes

```powershell
.\.venv\Scripts\python bankctl.py up                       # start all eight
.\.venv\Scripts\python bankctl.py status
.\.venv\Scripts\python bankctl.py restart payment          # one service only
.\.venv\Scripts\python bankctl.py kill account             # crash one service
.\.venv\Scripts\python bankctl.py start account
.\.venv\Scripts\python bankctl.py workload peak --duration 30
.\.venv\Scripts\python bankctl.py fault database latency --latency-ms 800 --duration 60
.\.venv\Scripts\python bankctl.py clear
.\.venv\Scripts\python bankctl.py integrity                # audit every money invariant
.\.venv\Scripts\python bankctl.py down
.\.venv\Scripts\python bankctl.py reset                    # delete the synthetic data
```

API docs per service: `http://127.0.0.1:<port>/docs` (8101–8108).

## Run in Docker

```powershell
Copy-Item .env.example .env      # then set BANK_INTERNAL_KEY to 16+ random characters
docker compose up -d --build
docker compose stop payment      # the other seven keep running
docker compose start payment
docker compose down
```

`bankctl.py` manages local processes only. Use one mode at a time: both use ports 8101–8108.

## Test

```powershell
.\.venv\Scripts\python -m pytest -q       # unit and in-process integration tests
.\.venv\Scripts\python scenarios.py       # 23 live scenarios, about 7 minutes, bank must be down
```
