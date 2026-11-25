"""The digital twin: the one place where the virtual infrastructure changes.

``apply`` takes an immutable observation, updates the virtual components and reports what changed.
Nothing in here can reach the real system, and nothing handed out can change the twin: views are
built from copies, and ``clone`` gives an independent twin for experiments.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.digital_twin import blueprint as bp
from app.digital_twin.blueprint import Blueprint
from app.digital_twin.health import (
    Persistence,
    StatusTracker,
    entry_findings,
    infra_findings,
    link_findings,
    service_findings,
    store_findings,
    worst,
)
from app.digital_twin.model import (
    Change,
    ComponentType,
    Finding,
    Level,
    LinkKind,
    ObjectState,
    Observation,
    Status,
    Transition,
)

T = ComponentType
TROUBLE = (Status.FAILED, Status.RECOVERING, Status.UNKNOWN)
INFRA_STALE = timedelta(seconds=25)


class Rejected(ValueError):
    """The observation was not applied; the reason is the message."""


class DigitalTwin:
    def __init__(self, dependency_map: dict, persistence: Persistence | None = None,
                 stale_after_s: float = 8.0) -> None:
        self._map = copy.deepcopy(dependency_map)
        self._persistence = persistence or Persistence()
        self.stale_after = timedelta(seconds=stale_after_s)
        self.run_id: UUID = uuid4()
        self.version = 0
        self.structure_version = 1
        self.origin: str | None = None
        self.synchronised = False
        self.last_observed_at: datetime | None = None
        self.last_applied_at: datetime | None = None
        self._source: tuple[str, int] | None = None  # (monitor run, sequence)
        self._deployments: dict[str, str] = {}
        self.blueprint: Blueprint = bp.build(self._map, {})
        self.components: dict[str, ObjectState] = {c: ObjectState()
                                                   for c in self.blueprint.components}
        self.links: dict[str, ObjectState] = {k: ObjectState() for k in self.blueprint.links}
        self._trackers: dict[str, StatusTracker] = {}

    # ------------------------------------------------------------------ changing
    def apply(self, obs: Observation, now: datetime | None = None) -> Change:
        """Bring the twin in line with one observation."""
        now = now or datetime.now(UTC)
        if self._source and obs.run_id == self._source[0] and obs.sequence <= self._source[1]:
            raise Rejected("not newer than what is already applied")
        if self.last_observed_at and obs.observed_at < self.last_observed_at \
                and self._source and obs.run_id == self._source[0]:
            raise Rejected("observed earlier than what is already applied")
        if self.origin and obs.origin != self.origin:
            self._forget()  # measured and synthetic states must not flow into each other
        self._source = (obs.run_id, obs.sequence)
        self.origin, self.synchronised = obs.origin, True
        self.last_observed_at, self.last_applied_at = obs.observed_at, now
        restructured = self._restructure(obs)
        self.version += 1

        before = self._fingerprints()
        transitions: list[Transition] = []
        for cid in self.blueprint.dependencies_first:
            if cid != bp.BANK_NET:
                self._update_component(cid, obs, now, transitions)
        # The network is judged by the calls it carries, and structural links by what they lead to.
        calls = [k.id for k in self.blueprint.links.values() if k.kind is LinkKind.CALLS]
        for lid in calls:
            self._update_link(lid, obs, now, transitions)
        self._update_network(obs, now, transitions)
        for lid in self.blueprint.links:
            if self.blueprint.links[lid].kind is not LinkKind.CALLS:
                self._update_link(lid, obs, now, transitions)
        change = self._change(before, transitions, restructured)
        if not change:
            self.version -= 1  # the version counts changes, not messages
        return replace(change, version=self.version)

    def expire(self, now: datetime | None = None) -> Change:
        """Without fresh data everything becomes unknown. Call this regularly."""
        now = now or datetime.now(UTC)
        if not self.synchronised or self.last_applied_at is None \
                or now - self.last_applied_at <= self.stale_after:
            return Change(self.version)
        self.synchronised = False
        self.version += 1
        before = self._fingerprints()
        transitions: list[Transition] = []
        seconds = (now - self.last_applied_at).total_seconds()
        for kind, states in (("component", self.components), ("link", self.links)):
            for oid, state in states.items():
                self._conclude(kind, oid, state, None,
                               f"no monitoring data for {seconds:.0f} s", now, transitions)
        return self._change(before, transitions, False)

    def clone(self) -> DigitalTwin:
        """An independent copy: nothing done to it affects this twin, or the other way round."""
        twin = copy.deepcopy(self)
        twin.run_id = uuid4()
        return twin

    # --------------------------------------------------------------- structure
    def _restructure(self, obs: Observation) -> bool:
        """Follow services that moved between processes and containers."""
        found = {n: s.deployment for n, s in obs.services.items() if s.deployment}
        deployments = {**self._deployments, **found}  # a silent service stays where it was
        if deployments == self._deployments:
            return False
        self._deployments = deployments
        new = bp.build(self._map, deployments)
        if new.checksum == self.blueprint.checksum:
            return False
        self.blueprint = new
        self.components = {c: self.components.get(c, ObjectState()) for c in new.components}
        self.links = {k: self.links.get(k, ObjectState()) for k in new.links}
        keep = {("component", c) for c in new.components} | {("link", k) for k in new.links}
        self._trackers = {k: t for k, t in self._trackers.items()
                          if tuple(k.split("|", 1)) in keep}
        self.structure_version += 1
        return True

    def _forget(self) -> None:
        self._trackers.clear()
        for state in (*self.components.values(), *self.links.values()):
            state.status, state.since, state.previous = Status.UNKNOWN, None, None
            state.metrics, state.findings, state.quality = {}, (), "none"

    # ------------------------------------------------------------------ updating
    def _update_component(self, cid: str, obs: Observation, now: datetime,
                          out: list[Transition]) -> None:
        spec, state = self.blueprint.components[cid], self.components[cid]
        findings: list[Finding] | None = None
        unknown_reason = "not covered by the current monitoring source"
        if spec.type is T.MICROSERVICE:
            so = obs.services.get(spec.attributes["service"])
            if so is None or so.status == "unresponsive":
                unknown_reason = so.reason if so else "monitoring delivered nothing for it"
                state.metrics, state.quality = {}, "none"
            else:
                state.metrics = _numbers(so.metrics)
                state.quality = "partial" if so.partial else "complete"
                findings = service_findings(spec, so) + self._dependency_findings(cid)
        elif cid == bp.BANK_STORE:
            so = obs.services.get("database")
            if so and so.status == "up":
                keys = ("db_pool_in_use", "db_pool_size", "db_failure_rate", "db_response_ms")
                state.metrics = _numbers({k: so.metrics.get(k) for k in keys})
                state.quality = "complete" if state.metrics else "none"
                findings = store_findings(so.metrics)
            else:
                unknown_reason = "can only be observed through the Database Service"
                state.metrics, state.quality = {}, "none"
        elif cid == bp.CLIENT_ENTRY:
            findings = self._entry(state, obs)
            unknown_reason = "none of the services behind it reports"
        elif cid == bp.EXTERNAL_NET:
            so = obs.services.get("payment")
            if so and so.status == "up":
                state.metrics = _numbers({"calls_per_s": so.metrics.get("rps")})
                state.quality, findings = "derived", []
            else:
                unknown_reason = "can only be observed through the Payment Gateway"
                state.metrics, state.quality = {}, "none"
        elif cid == bp.MONITOR:  # this very message is the evidence
            state.metrics, state.quality = {}, "derived"
            findings = self._dependency_findings(cid)
        else:
            findings, reason = self._infrastructure(cid, state, obs)
            unknown_reason = reason or unknown_reason
        self._conclude("component", cid, state, findings, unknown_reason, now, out)

    def _entry(self, state: ObjectState, obs: Observation) -> list[Finding] | None:
        routes = [(n, self.components[self.blueprint.service_ids[n]].status)
                  for n in bp.ENTRY_SERVICES if n in self.blueprint.service_ids]
        seen = [n for n, s in routes if s is not Status.UNKNOWN]
        if not seen:
            state.metrics, state.quality = {}, "none"
            return None
        traffic = {n: ingress(n, obs) for n in seen}
        total = sum(traffic.values())
        failing = sum(traffic[n] * (obs.services[n].metrics.get("error_rate") or 0.0)
                      for n in seen if n in obs.services)
        failed = sum(s is Status.FAILED for _, s in routes)
        state.metrics = {"ingress_rps": round(total, 2), "routes": float(len(routes)),
                         "routes_failed": float(failed)}
        if total:
            state.metrics["error_rate"] = round(failing / total, 4)
        state.quality = "derived"
        return entry_findings(state.metrics, failed, len(routes)) \
            + self._dependency_findings(bp.CLIENT_ENTRY)

    def _infrastructure(self, cid: str, state: ObjectState,
                        obs: Observation) -> tuple[list[Finding] | None, str]:
        for name, info in obs.infra.items():
            if bp.INFRA_IDS.get(name) != cid:
                continue
            if obs.observed_at - info.observed_at > INFRA_STALE:
                state.metrics, state.quality = {}, "none"
                return None, "its data is out of date"
            if info.status == "unresponsive":
                state.metrics, state.quality = {}, "none"
                return None, info.reason
            state.metrics, state.quality = dict(info.metrics), "complete"
            return infra_findings(info) + self._dependency_findings(cid), ""
        hosted = [obs.services[n] for n, i in self.blueprint.service_ids.items()
                  if n in obs.services and any(k.target == cid and k.kind is LinkKind.HOSTED_ON
                                               for k in self.blueprint.outgoing[i])]
        running = [s for s in hosted if s.status == "up"]
        if not running:
            state.metrics, state.quality = {}, "none"
            return None, "not measured, and no hosted service reports"
        # Not measured itself, but something runs on it: it is evidently working.
        state.quality = "derived"
        state.metrics = {
            "hosted_services": float(len(hosted)), "hosted_running": float(len(running)),
            "hosted_cpu_pct": round(sum(s.metrics.get("cpu_pct") or 0 for s in running), 2),
            "hosted_mem_bytes": sum(s.metrics.get("mem_bytes") or 0 for s in running)}
        return [], ""

    def _update_link(self, lid: str, obs: Observation, now: datetime,
                     out: list[Transition]) -> None:
        spec, state = self.blueprint.links[lid], self.links[lid]
        findings: list[Finding] | None
        reason = ""
        if spec.kind is LinkKind.CALLS:
            name = {v: k for k, v in self.blueprint.service_ids.items()}
            m = obs.links.get((name[spec.source], name[spec.target]))
            if m is None:
                findings, reason = None, "its caller does not report"
                state.metrics, state.quality = {}, "none"
            else:
                state.metrics, state.quality = _numbers(m), "complete"
                findings = link_findings(spec, m)
        else:
            # It has no measurements of its own, and is as good as what it leads to.
            target = self.components[spec.target].status
            state.quality = "derived"
            state.metrics = {}
            if spec.kind is LinkKind.ROUTES_TO:
                state.metrics = self._route_metrics(spec.source, spec.target, obs)
            if target is Status.UNKNOWN:
                findings, reason = None, f"{spec.target} is unknown"
            elif target is Status.FAILED:
                findings = [Finding(Level.FAILURE, "target_failed", f"{spec.target} has failed",
                                    immediate=True)]
            else:
                findings = []
        self._conclude("link", lid, state, findings, reason, now, out)

    def _route_metrics(self, source: str, target: str, obs: Observation) -> dict[str, float]:
        names = {v: k for k, v in self.blueprint.service_ids.items()}
        if source == bp.CLIENT_ENTRY:
            so = obs.services.get(names[target])
            if so is None or so.status != "up":
                return {}
            return _numbers({"calls_per_s": ingress(names[target], obs),
                             "failure_rate": so.metrics.get("error_rate"),
                             "latency_ms": so.metrics.get("latency_mean_ms")})
        so = obs.services.get(names[source])
        return _numbers({"calls_per_s": so.metrics.get("rps")}) if so and so.status == "up" else {}

    def _update_network(self, obs: Observation, now: datetime, out: list[Transition]) -> None:
        """The network is blamed only for failing calls that the called service does not explain."""
        cid, state = bp.BANK_NET, self.components[bp.BANK_NET]
        calls = [(self.blueprint.links[i], s) for i, s in self.links.items()
                 if self.blueprint.links[i].kind is LinkKind.CALLS and s.metrics]
        if not calls:
            state.metrics, state.quality = {}, "none"
            self._conclude("component", cid, state, None, "no connection is being measured", now,
                           out)
            return
        traffic = sum(s.metrics.get("calls_per_s", 0.0) for _, s in calls)
        failed = sum(s.metrics.get("calls_per_s", 0.0) * s.metrics.get("failure_rate", 0.0)
                     for _, s in calls)
        state.metrics = {
            "calls_per_s": round(traffic, 2),
            "failure_rate": round(failed / traffic, 4) if traffic else 0.0,
            "connections": float(len(calls)),
            "connections_failed": float(sum(s.status is Status.FAILED for _, s in calls)),
            "bytes_per_s": round(sum((v.metrics.get("net_tx_bps") or 0.0)
                                     for v in obs.services.values()), 1)}
        state.quality = "derived"
        judged = [s for k, s in calls if self.components[k.target].status not in TROUBLE
                  and s.metrics.get("calls_per_s", 0.0) > 0]
        bad = sum(s.status is Status.FAILED for s in judged)
        findings: list[Finding] = []
        if bad:
            share = bad / len(judged)
            level = Level.FAILURE if share >= 0.5 and len(judged) >= 3 else Level.WARNING
            findings.append(Finding(level, "unexplained", f"{bad} of {len(judged)} connections to "
                                    "working services fail", "connections_failed", bad, 1))
        self._conclude("component", cid, state, findings, "", now, out)

    def _dependency_findings(self, cid: str) -> list[Finding]:
        out = []
        for k in self.blueprint.outgoing[cid]:
            if k.propagates and self.components[k.target].status is Status.FAILED:
                name = self.blueprint.components[k.target].name
                out.append(Finding(Level.WARNING, "dependency_failed",
                                   f"depends on {name}, which has failed", k.target))
        return out

    # ------------------------------------------------------------------ concluding
    def _conclude(self, kind: str, oid: str, state: ObjectState, findings: list[Finding] | None,
                  unknown_reason: str, now: datetime, out: list[Transition]) -> None:
        tracker = self._trackers.setdefault(f"{kind}|{oid}", StatusTracker(self._persistence))
        if findings is None:
            status = tracker.observe(None)
            state.findings = ()
            reason = unknown_reason
        else:
            status = tracker.observe(worst(findings), any(f.immediate for f in findings))
            state.findings = tuple(sorted(findings, key=lambda f: -f.level))
            reason = state.findings[0].message if findings else ""
            if status is Status.RECOVERING:
                reason = f"working again after a failure{'; ' + reason if reason else ''}"
        if status is Status.HEALTHY:
            reason = ""  # findings that have not persisted yet are still listed as findings
        elif not reason:
            reason = state.reason or "recently unstable"  # held by persistence
        if status is not state.status or state.since is None:
            first = state.since is None
            state.previous = None if first else state.status
            state.status, state.since, state.reason = status, now, reason
            causes = tuple(self.root_causes(oid)) if kind == "component" else ()
            out.append(Transition(now, self.version, kind, oid, state.previous, status, reason,
                                  causes, self.origin))
        else:
            state.reason = reason

    def _change(self, before: dict, transitions: list[Transition], restructured: bool) -> Change:
        after = self._fingerprints()
        changed = {k for k, v in after.items() if before.get(k) != v}
        components = {i for k, i in changed if k == "component"}
        for t in transitions:
            components |= self._shows(t)
        return Change(self.version, frozenset(components),
                      frozenset(i for k, i in changed if k == "link"), tuple(transitions),
                      restructured)

    def _shows(self, t: Transition) -> set[str]:
        """Components whose public view contains the status that changed."""
        if t.object_kind == "link":
            k = self.blueprint.links[t.object_id]
            return {k.source, k.target}
        neighbours = {k.target for k in self.blueprint.outgoing[t.object_id]}
        # Everything downstream may now have another root cause.
        return neighbours | set(self.reachable(t.object_id, "downstream"))

    def _fingerprints(self) -> dict[tuple[str, str], tuple]:
        return {(kind, oid): (s.status, s.reason, s.quality, s.findings,
                              tuple(sorted(s.metrics.items())))
                for kind, group in (("component", self.components), ("link", self.links))
                for oid, s in group.items()}

    # -------------------------------------------------------------------- analysis
    def root_causes(self, cid: str) -> list[str]:
        """Failed components behind this one's trouble (itself included) that nothing else explains.
        """
        if self.components[cid].status is Status.HEALTHY:
            return []
        seen: set[str] = set()
        causes: list[str] = []

        def visit(node: str) -> bool:
            """Returns whether a failure was found at or below the node."""
            if node in seen:
                return node in causes
            seen.add(node)
            below = [visit(k.target) for k in self.blueprint.outgoing[node] if k.propagates]
            if any(below):
                return True
            if self.components[node].status is Status.FAILED:
                causes.append(node)
                return True
            return False

        visit(cid)
        return sorted(causes)

    def reachable(self, cid: str, direction: str) -> dict[str, int]:
        """Everything it depends on ("upstream") or that depends on it ("downstream"), with the
        number of steps."""
        edges = self.blueprint.outgoing if direction == "upstream" else self.blueprint.incoming
        found: dict[str, int] = {}
        frontier, depth = [cid], 0
        while frontier:
            depth += 1
            nxt = []
            for node in frontier:
                for k in edges[node]:
                    other = k.target if direction == "upstream" else k.source
                    if other != cid and other not in found:
                        found[other] = depth
                        nxt.append(other)
            frontier = nxt
        return found

    def statuses(self) -> dict[str, str]:
        return {**{c: s.status.value for c, s in self.components.items()},
                **{k: s.status.value for k, s in self.links.items()}}


def ingress(service: str, obs: Observation) -> float:
    """Requests per second from outside: all requests minus the measured internal calls."""
    so = obs.services.get(service)
    if so is None or so.status != "up":
        return 0.0
    internal = sum((m.get("calls_per_s") or 0.0) for (_, target), m in obs.links.items()
                   if target == service)
    outside = (so.metrics.get("rps") or 0.0) - internal
    return round(outside, 2) if outside >= 0.15 else 0.0  # below that it is sampling noise


def _numbers(metrics: dict[str, float | None]) -> dict[str, float]:
    return {k: v for k, v in metrics.items() if v is not None}
