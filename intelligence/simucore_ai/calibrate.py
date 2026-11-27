"""Learn from recordings of normal operation what is normal, and how the numbers hang together.

    python -m simucore_ai.calibrate

Reads recordings/calibration (the real simulated bank, nothing wrong, eight different loads) and
writes models/calibration.json: the profile of every service, and the parameters the synthetic data
generator needs to make normal data that resembles it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from simucore_ai.features import IDX, Profile, Service, load_services
from simucore_ai.recordings import Episode, load_set

PKG = Path(__file__).resolve().parents[1]
MAP = PKG.parent / "banking-sim" / "dependency_map.json"
MODELS = PKG / "models"
SKIP = 10  # ticks: the 10 s window of the rates fills


def _line(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if len(x) < 20:
        return (float(np.median(y)) if len(y) else 0.0), 0.0, 0.0
    b, a = np.polyfit(x, y, 1)
    b = max(0.0, b)
    a = max(0.0, float(np.median(y - b * x)))
    return a, float(b), float(np.std(y - a - b * x))


def calibrate(episodes: list[Episode], services: dict[str, Service]) -> dict:
    out: dict = {"made_at": datetime.now(UTC).isoformat(timespec="seconds"),
                 "recordings": [e.name for e in episodes], "services": {}}
    for s in services:
        raw = np.concatenate([e.raw[s][SKIP:] for e in episodes])
        rate = np.concatenate([np.full(len(e.raw[s]) - SKIP, e.meta["rate"]) for e in episodes])
        raw_up = raw[raw[:, IDX["up"]] > 0]

        def col(k: str, rows: np.ndarray = raw) -> np.ndarray:
            return rows[:, IDX[k]]

        rps = col("requests")
        share = float(np.nanmedian(rps / rate))
        lat = np.log(np.where(col("latency") > 0, col("latency"), np.nan))
        db = np.log(np.where(col("database") > 0, col("database"), np.nan))
        with np.errstate(invalid="ignore", divide="ignore"):
            per_request = col("network")[rps > 0.5] / rps[rps > 0.5]
        mem = col("memory", raw_up)
        out["services"][s] = {
            "profile": Profile.fit(raw).to_json(),
            "share": round(share, 4),
            "cpu": [round(x, 4) for x in _line(rps, col("cpu"))],
            "memory": [round(float(np.nanmedian(mem)), 3), round(float(np.nanstd(mem)), 4)],
            "latency": [round(float(np.nanmedian(lat)), 4), round(float(np.nanstd(lat)), 4)],
            "database": None if np.isnan(db).all() else
            [round(float(np.nanmedian(db)), 4), round(float(np.nanstd(db)), 4)],
            "bytes": [round(float(np.nanmedian(per_request)), 2),
                      round(float(np.nanstd(np.log(per_request[per_request > 0]))), 4)]
            if len(per_request) else [2000.0, 0.1],
            "connections": [round(x, 4) for x in _line(rps, col("connections"))],
        }
    return out


def load_calibration(path: Path = MODELS / "calibration.json") -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def profiles(calibration: dict) -> dict[str, Profile]:
    return {s: Profile.from_json(c["profile"]) for s, c in calibration["services"].items()}


def main() -> None:
    services = load_services(MAP)
    episodes = load_set(PKG / "recordings" / "calibration", services)
    if not episodes:
        raise SystemExit("no calibration recordings")
    MODELS.mkdir(exist_ok=True)
    c = calibrate(episodes, services)
    (MODELS / "calibration.json").write_text(json.dumps(c, indent=1) + "\n", encoding="utf-8",
                                             newline="\n")
    for s, v in c["services"].items():
        print(s, {k: v[k] for k in v if k != "profile"})


if __name__ == "__main__":
    main()
