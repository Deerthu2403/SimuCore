"""Measure the frozen models on synthetic episodes nobody has looked at.

    python -m simucore_ai.final

The test set of train.py was looked at while features were being designed, so it is no longer a fair
judge (it stays in the report as 'development test'). This uses seeds never generated before, changes
nothing in the models, and writes its numbers into results/synthetic.json and models/meta.json.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from simucore_ai import labels
from simucore_ai.calibrate import MAP, MODELS
from simucore_ai.generator import SHIFTED, dataset
from simucore_ai.main import load_models
from simucore_ai.report import main as write_report
from simucore_ai.train import (
    RESULTS,
    SPLITS,
    describe,
    judge_detector,
    judge_predictor,
    logistic,
    rows,
    select,
)

FRESH = {"test": (600_000, 260), "shifted": (700_000, 160)}


def main() -> None:
    models = load_models(MODELS, MAP)
    services, prof = models.services, models.profiles
    cal = json.loads((MODELS / "calibration.json").read_text(encoding="utf-8"))
    r = json.loads((RESULTS / "synthetic.json").read_text(encoding="utf-8"))
    meta = json.loads((MODELS / "meta.json").read_text(encoding="utf-8"))
    if meta["version"] != models.version or r["made_at"] != meta["trained_at"]:
        raise SystemExit("report and models do not belong together")
    start, n = SPLITS["train"]
    train = rows(dataset(range(start, start + n), services, cal), services, prof)
    baselines = {}
    for t in labels.TARGETS:
        k = select(train, t, 2)
        baselines[t] = logistic((train.x[k], train.y[t][k]))
    del train
    r.setdefault("development_test", {k: r[k] for k in FRESH})
    for name, (start, n) in FRESH.items():
        table = rows(dataset(range(start, start + n), services, cal,
                             SHIFTED if name == "shifted" else None), services, prof)
        r["sets"][name] = describe(table)
        r[name] = {"anomaly": judge_detector(models.detector, table),
                   **{t: judge_predictor(models.predictors[t], table, t, models.thresholds[t],
                                         {"logistic_regression": baselines[t]})
                      for t in labels.TARGETS}}
        print(name, {t: r[name][t]["xgboost"] for t in labels.TARGETS}, flush=True)
    r["tested_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    meta["test"] = {"anomaly": r["test"]["anomaly"]["isolation_forest"],
                    **{t: {"xgboost": r["test"][t]["xgboost"],
                           "events": {k: v for k, v in r["test"][t]["events"].items()
                                      if k != "by_kind"}} for t in labels.TARGETS}}
    for path, doc in ((RESULTS / "synthetic.json", r), (MODELS / "meta.json", meta)):
        path.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8", newline="\n")
    write_report()


if __name__ == "__main__":
    main()
