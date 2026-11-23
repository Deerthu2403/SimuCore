"""Control the simulated bank as local processes (no Docker needed).

  python bankctl.py up | down | status [--json]
  python bankctl.py start|stop|restart|kill <service>
  python bankctl.py fault <service> <type> [--mode M] [--latency-ms N]
                [--magnitude F] [--duration S]
  python bankctl.py clear [<service>]
  python bankctl.py integrity | logs <service> | reset
  python bankctl.py workload <normal|peak|abnormal> [--rps N] [--duration S]

``stop`` is graceful, ``kill`` is a crash. Each acts on one service only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil

from bankcommon.config import ROOT, registry

RUNTIME = ROOT / ".runtime"
START_ORDER = ("database", "authentication", "customer", "account", "notification", "payment",
               "fraud", "transaction")


class BankCtlError(RuntimeError):
    pass


class Bank:
    def __init__(self, runtime: Path = RUNTIME, env: dict[str, str] | None = None) -> None:
        self.runtime, self.extra_env = runtime, env or {}
        runtime.mkdir(parents=True, exist_ok=True)
        self.http = httpx.Client(timeout=3.0, trust_env=False)

    # ------------------------------------------------------------------ basics
    @property
    def key(self) -> str:
        """Random per-installation key, generated locally and never committed."""
        f = self.runtime / "internal.key"
        if not f.exists():
            f.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        return f.read_text(encoding="utf-8").strip()

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Internal-Key": self.key}

    def url(self, name: str) -> str:
        if name not in registry().services:
            raise BankCtlError(f"unknown service {name!r}")
        return f"http://127.0.0.1:{registry().services[name]['port']}"

    def _record(self, name: str) -> Path:
        return self.runtime / f"{name}.json"

    def process(self, name: str) -> psutil.Process | None:
        """The recorded process, only if it is provably the one we started (PIDs get reused)."""
        f = self._record(name)
        if not f.exists():
            return None
        rec = json.loads(f.read_text(encoding="utf-8"))
        try:
            p = psutil.Process(rec["pid"])
            if abs(p.create_time() - rec["create_time"]) < 1 and p.is_running():
                return p
        except psutil.NoSuchProcess:
            pass
        return None

    def live(self, name: str) -> dict | None:
        try:
            r = self.http.get(f"{self.url(name)}/health/live", timeout=1.0)
            return r.json() if r.status_code == 200 else {"status": "down", "service": name}
        except httpx.HTTPError:
            return None

    # --------------------------------------------------------------- lifecycle
    def start(self, name: str, wait_s: float = 25.0) -> dict:
        if self.process(name):
            raise BankCtlError(f"{name} is already running")
        if self.live(name) is not None:
            raise BankCtlError(f"the port of {name} is used by another process")
        env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1",
               "BANK_INTERNAL_KEY": self.key, "BANK_DATA_DIR": str(self.runtime / "data"),
               **self.extra_env}
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        with (self.runtime / f"{name}.log").open("ab") as out:
            proc = subprocess.Popen([sys.executable, "-m", "bankcommon.run", name], cwd=ROOT,
                                    env=env, stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                                    creationflags=flags, start_new_session=sys.platform != "win32")
        self._record(name).write_text(json.dumps(
            {"pid": proc.pid, "create_time": psutil.Process(proc.pid).create_time()}),
            encoding="utf-8")
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            info = self.live(name)
            if info and info["status"] == "alive":
                return info
            if proc.poll() is not None:
                raise BankCtlError(f"{name} exited at start-up; see {self.runtime / name}.log")
            time.sleep(0.1)
        raise BankCtlError(f"{name} did not become live within {wait_s}s")

    def stop(self, name: str) -> str:
        p = self.process(name)
        if p is None:
            return "not running"
        tree = [p, *p.children(recursive=True)]
        how = "graceful"
        try:
            self.http.post(f"{self.url(name)}/control/shutdown", headers=self.headers)
        except httpx.HTTPError:
            how = "forced"
        _, alive = psutil.wait_procs(tree, timeout=8 if how == "graceful" else 0.1)
        if alive:
            how = "forced"
            self._kill_all(alive)
        self._record(name).unlink(missing_ok=True)
        return how

    def kill(self, name: str) -> str:
        p = self.process(name)
        if p is None:
            return "not running"
        self._kill_all([p, *p.children(recursive=True)])
        self._record(name).unlink(missing_ok=True)
        return "killed"

    @staticmethod
    def _kill_all(procs: list[psutil.Process]) -> None:
        for q in procs:
            try:
                q.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(procs, timeout=5)

    def restart(self, name: str) -> dict:
        self.stop(name)
        return self.start(name)

    def up(self) -> None:
        for name in START_ORDER:
            if not self.process(name):
                self.start(name)

    def down(self) -> None:
        for name in reversed(START_ORDER):
            self.stop(name)

    def status(self) -> list[dict]:
        rows = []
        for name in START_ORDER:
            info, proc = self.live(name), self.process(name)
            ready = "-"
            if info and info["status"] == "alive":
                try:
                    ready = self.http.get(f"{self.url(name)}/health/ready").json()["status"]
                except (httpx.HTTPError, KeyError, ValueError):
                    ready = "unknown"
            rows.append({"service": name, "port": registry().services[name]["port"],
                         "live": info["status"] if info else "stopped", "readiness": ready,
                         "pid": (info or {}).get("pid") or (proc.pid if proc else None)})
        return rows

    # ------------------------------------------------------------------ faults
    def fault(self, name: str, type_: str, duration_s: float = 30, **kw) -> dict:
        r = self.http.post(f"{self.url(name)}/control/faults", headers=self.headers,
                           json={"type": type_, "duration_s": duration_s, **kw}, timeout=10)
        if r.status_code != 200:
            raise BankCtlError(r.text)
        return r.json()

    def clear(self, name: str | None = None) -> None:
        for n in [name] if name else START_ORDER:
            try:
                self.http.delete(f"{self.url(n)}/control/faults", headers=self.headers, timeout=10)
            except httpx.HTTPError:
                if name:
                    raise

    def telemetry(self, name: str) -> dict:
        return self.http.get(f"{self.url(name)}/telemetry").json()

    def integrity(self) -> dict:
        r = self.http.get(f"{self.url('database')}/internal/integrity", headers=self.headers,
                          timeout=10)
        r.raise_for_status()
        return r.json()

    def reset(self) -> None:
        if any(self.process(n) for n in START_ORDER):
            raise BankCtlError("stop the bank first: bankctl.py down")
        shutil.rmtree(self.runtime / "data", ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    names = list(START_ORDER)
    for c in ("up", "down", "integrity", "reset"):
        sub.add_parser(c)
    sub.add_parser("status").add_argument("--json", action="store_true")
    for c in ("start", "stop", "restart", "kill", "logs"):
        sub.add_parser(c).add_argument("service", choices=names)
    sub.add_parser("clear").add_argument("service", nargs="?", choices=names)
    f = sub.add_parser("fault")
    f.add_argument("service", choices=names)
    f.add_argument("type", choices=["downtime", "latency", "resource_exhaustion",
                                    "db_connection_failure"])
    f.add_argument("--mode")
    f.add_argument("--latency-ms", type=float, default=0)
    f.add_argument("--magnitude", type=float, default=1.0)
    f.add_argument("--duration", type=float, default=30)
    w = sub.add_parser("workload")
    w.add_argument("profile", choices=["normal", "peak", "abnormal"])
    w.add_argument("--rps", type=float)
    w.add_argument("--duration", type=float, default=20)
    w.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    bank = Bank()
    try:
        if a.cmd in ("up", "down"):
            getattr(bank, a.cmd)()
        elif a.cmd in ("start", "restart", "stop", "kill"):
            print(getattr(bank, a.cmd)(a.service))
        elif a.cmd == "clear":
            bank.clear(a.service)
        elif a.cmd == "fault":
            print(json.dumps(bank.fault(a.service, a.type, a.duration, mode=a.mode,
                                        latency_ms=a.latency_ms, magnitude=a.magnitude), indent=2))
        elif a.cmd == "integrity":
            print(json.dumps(bank.integrity(), indent=2))
        elif a.cmd == "logs":
            f = bank.runtime / f"{a.service}.log"
            print("\n".join(f.read_text(errors="replace").splitlines()[-60:]))
        elif a.cmd == "reset":
            bank.reset()
        elif a.cmd == "workload":
            import workload
            report = asyncio.run(workload.run(a.profile, bank.key, rps=a.rps,
                                              duration_s=a.duration, seed=a.seed))
            print(json.dumps(report, indent=2))
        if a.cmd in ("up", "down", "status"):
            rows = bank.status()
            if getattr(a, "json", False):
                print(json.dumps(rows, indent=2))
            else:
                print(f"{'SERVICE':<16}{'PORT':<7}{'LIVE':<10}{'READINESS':<12}PID")
                for r in rows:
                    print(f"{r['service']:<16}{r['port']:<7}{r['live']:<10}"
                          f"{r['readiness']:<12}{r['pid'] or '-'}")
    except (BankCtlError, httpx.HTTPError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
