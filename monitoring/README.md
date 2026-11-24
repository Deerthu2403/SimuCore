# monitoring

Collects, checks, stores and streams the metrics of the simulated bank and its platform. Read-only.
Design, metrics, API and limits: [docs/09-monitoring.md](../docs/09-monitoring.md).

## Install (Python 3.12+)

```powershell
cd "D:\Major Projects\SimuCore\monitoring"
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
```

## Run

With Docker (Prometheus, history): see the repository README. Without Docker:

```powershell
.\.venv\Scripts\python -m uvicorn monitor.main:app --port 8200
```

Set `MONITOR_SOURCE=synthetic` to run without a bank. API documentation: http://localhost:8200/docs

## Test

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python live\pipeline_check.py                                  # needs Docker, about 16 minutes
$env:BANKCTL_MODE = "docker"; .\.venv\Scripts\python live\pipeline_check.py    # bank as containers
```

Results: [processes](results/pipelines.md), [containers](results/pipelines_docker.md).
