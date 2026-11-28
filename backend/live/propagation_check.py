"""Live validation of the failure propagation engine against the real simulated bank.

    backend/.venv/Scripts/python backend/live/propagation_check.py            about 20 minutes
    backend/.venv/Scripts/python backend/live/propagation_check.py payment    only these origins

Needs the Docker stack (backend, monitoring, PostgreSQL) and a stopped bank: it starts the bank
and a steady synthetic workload, then crashes each banking service in turn. For every crash it
compares what the engine predicted (at the moment the failure was confirmed) with what was then
measured during the following 60 seconds:

* per component: was trouble measured (the twin's own findings, not the rule 'depends on a failed
  component')? -> hits, misses, false alarms, precision and recall;
* per component: how much did its error rate rise? -> rank correlation with the predicted impact.

Nothing but synthetic traffic is used. Results: backend/results/propagation.json and .md.
"""

from __future__ import annotations

import json
import math
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
BANK = ROOT / "banking-sim"
BANK_PY = BANK / ".venv" / "Scripts" / "python.exe"
API = "http://127.0.0.1:8000/api/v2/twin"
SERVICES = list(json.loads((BANK / "dependency_map.json").read_text(encoding="utf-8"))["services"])
WINDOW = 62  # the twin judges an event after 60 s
PREDICTED = ("severe", "high", "moderate")

http = httpx.Client(timeout=10, trust_env=False)


def ctl(*a: str) -> str:
    r = subprocess.run(
        [str(BANK_PY), "bankctl.py", *a], cwd=BANK, capture_output=True, text=True, timeout=400
    )
    if r.returncode:
        raise RuntimeError(f"bankctl {' '.join(a)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def twin() -> dict:
    return http.get(API).raise_for_status().json()


def healthy() -> bool:
    t = twin()
    return (
        all(c["status"] == "healthy" for c in t["components"])
        and not t["summary"]["propagation"]["origins"]
    )


def wait(what: str, f, seconds: float, every: float = 2) -> float:
    t0 = time.monotonic()
    while not f():
        if time.monotonic() - t0 > seconds:
            raise TimeoutError(f"not within {seconds} s: {what}")
        time.sleep(every)
    return round(time.monotonic() - t0, 1)


def errors(t: dict) -> dict[str, float]:
    return {
        c["id"]: c["metrics"]["error_rate"] for c in t["components"] if "error_rate" in c["metrics"]
    }


def sample(seconds: float) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for k, v in errors(twin()).items():
            out.setdefault(k, []).append(v)
        time.sleep(2)
    return out


def spearman(xs: list[float], ys: list[float]) -> float | None:
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            k = i
            while k + 1 < len(v) and v[order[k + 1]] == v[order[i]]:
                k += 1
            for j in range(i, k + 1):
                r[order[j]] = (i + k) / 2
            i = k + 1
        return r

    if len(xs) < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    return round(
        cov / math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)), 3
    )


def crash(service: str) -> dict:
    origin = f"bank-{service}"
    wait("healthy", healthy, 180)
    base = {k: statistics.fmean(v) for k, v in sample(16).items()}
    t0 = datetime.now(UTC)
    ctl("kill", service)
    seen = wait(
        "failure confirmed", lambda: origin in twin()["summary"]["propagation"]["origins"], 40, 0.5
    )
    during = sample(WINDOW - seen)
    events = http.get(f"{API}/propagation/events/live").raise_for_status().json()
    event = next(
        e
        for e in events
        if origin in e["new_origins"] and datetime.fromisoformat(e["opened_at"]) >= t0
    )
    ctl("start", service)
    wait(
        "event closed",
        lambda: any(
            e["id"] == event["id"] and e["closed_at"]
            for e in http.get(f"{API}/propagation/events/live").json()
        ),
        60,
    )
    event = next(
        e for e in http.get(f"{API}/propagation/events/live").json() if e["id"] == event["id"]
    )
    impact = {r["id"]: r["impact"] for r in event["report"]["impacts"]}
    rise = {k: round(max(v) - base.get(k, 0.0), 4) for k, v in during.items() if k != origin}
    pairs = [(impact.get(k, 0.0), r) for k, r in rise.items()]
    stored = http.get(f"{API}/propagation/events", params={"minutes": 60, "limit": 200})
    in_db = stored.status_code == 200 and any(
        e["id"] == event["id"] and e["outcome"] for e in stored.json()
    )
    return {
        "origin": origin,
        "confirmed_after_s": seen,
        "event": event["id"],
        "stored": in_db,
        "origins": event["origins"],
        "level": event["report"]["level"],
        "predicted": event["predicted"],
        "outcome": event["outcome"],
        "impact": impact,
        "error_rate_rise": rise,
        "spearman_impact_vs_error_rise": spearman([p[0] for p in pairs], [p[1] for p in pairs]),
    }


def main() -> int:
    only = sys.argv[1:]
    unknown = set(only) - set(SERVICES)
    if unknown:
        sys.exit(f"unknown service(s): {sorted(unknown)}")
    if any(r["live"] != "stopped" for r in json.loads(ctl("status", "--json"))):
        sys.exit("The bank is running. Stop it first: bankctl.py down")
    ctl("reset")  # fresh synthetic accounts: long runs pay them out and payments stop
    ctl("up")
    load = subprocess.Popen(
        [str(BANK_PY), "bankctl.py", "workload", "normal", "--rps", "10", "--duration", "7200"],
        cwd=BANK,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    results = []
    try:
        wait("bank healthy in the twin", healthy, 240, 3)
        time.sleep(25)  # the measuring windows fill
        for s in only or SERVICES:
            try:
                r = crash(s)
            except Exception as exc:  # one failed run must not hide the others
                r = {"origin": f"bank-{s}", "error": f"{type(exc).__name__}: {exc}"}
                subprocess.run([str(BANK_PY), "bankctl.py", "up"], cwd=BANK, capture_output=True)
            results.append(r)
            o = r.get("outcome") or {}
            print(
                f"{r['origin']}: predicted {r.get('predicted')} observed {o.get('observed')} "
                f"precision {o.get('precision')} recall {o.get('recall')} "
                f"rho {r.get('spearman_impact_vs_error_rise')} {r.get('error', '')}",
                flush=True,
            )
    finally:
        subprocess.run(["taskkill", "/PID", str(load.pid), "/T", "/F"], capture_output=True)
        ctl("down")
    write(results, partial=bool(only))
    return 0 if all("error" not in r for r in results) else 1


def write(results: list[dict], partial: bool) -> None:
    ok = [r for r in results if "error" not in r and r["outcome"]["judged"]]
    hits = sum(len(r["outcome"]["hits"]) for r in ok)
    predicted = sum(len(r["predicted"]) for r in ok)
    observed = sum(len(r["outcome"]["observed"]) for r in ok)
    # all (origin, component) pairs together: does a higher impact go with a larger error rise?
    xs = [r["impact"].get(k, 0.0) for r in ok for k in r["error_rate_rise"]]
    ys = [v for r in ok for v in r["error_rate_rise"].values()]
    total = {
        "runs": len(results),
        "judged": len(ok),
        "predicted": predicted,
        "observed": observed,
        "hits": hits,
        "precision": round(hits / predicted, 3) if predicted else None,
        "recall": round(hits / observed, 3) if observed else None,
        "spearman_all_pairs": spearman(xs, ys),
        "pairs": len(xs),
        "stored_in_db": sum(bool(r.get("stored")) for r in results),
    }
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = ROOT / "backend" / "results" / ("propagation_partial" if partial else "propagation")
    out.with_suffix(".json").write_text(
        json.dumps({"run_at": stamp, "total": total, "runs": results}, indent=1) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    short = lambda ids: ", ".join(i.replace("bank-", "") for i in ids) or "–"  # noqa: E731
    lines = [
        "# Failure propagation: predictions against measurements",
        "",
        f"Run at {stamp} on the real simulated bank (processes, synthetic workload of "
        "10 requests/s). Each banking service was crashed once; the engine's prediction at the "
        "moment the failure was confirmed is compared with what the twin then measured in the "
        "following 60 s. Generated by `backend/live/propagation_check.py`.",
        "",
        f"**Precision {total['precision']}, recall {total['recall']}** over {total['judged']} "
        f"judged crashes ({hits} of {predicted} predicted components showed trouble; {observed} "
        "components showed trouble in all). Rank correlation (Spearman) between predicted impact "
        f"and the measured rise of the error rate over {total['pairs']} component pairs: "
        f"**{total['spearman_all_pairs']}**. Events stored in PostgreSQL with outcome: "
        f"{total['stored_in_db']} of {total['runs']}.",
        "",
        "| Crashed | Confirmed after | Level | Predicted (moderate or worse) | Measured trouble | "
        "Not predicted | Predicted, not measured | Precision | Recall | Spearman |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['origin']} | {r['error'][:120]} |||||||||")
            continue
        o = r["outcome"]
        lines.append(
            f"| {r['origin'].replace('bank-', '')} | {r['confirmed_after_s']} s | {r['level']} | "
            f"{short(r['predicted'])} | {short(o['observed'])} | {short(o['missed'])} | "
            f"{short(o['not_observed'])} | {o['precision']} | {o['recall']} | "
            f"{r['spearman_impact_vs_error_rise']} |"
        )
    lines += [
        "",
        "Measured trouble = the component's own measurements broke a rule of the twin "
        "(errors, latency, status), not the rule that a dependency has failed. Components that "
        "were not predicted but showed trouble are counted against recall; they are often the "
        "callers of the callers whose error rate rises only slightly, or load changes the crash "
        "caused elsewhere. A small sample: these numbers describe this bank and this workload, "
        "not failure propagation in general.",
        "",
    ]
    out.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    sys.exit(main())
