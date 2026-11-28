"""How a failure spreads: weighted breadth-first search over the dependency graph.

The graph is built from whatever structure the twin has now (``from_blueprint``), or from any list
of nodes and edges (``from_parts``). Nothing in here knows the names of the bank's services.

An edge ``a -> b`` means *a depends on b*. A failure travels against the edges: when b fails, a is
affected. Every edge has a **strength**: the share of the dependent's work that needs the other
side.

    impact(origin)    = 1
    impact(dependent) = impact(dependency) x strength(edge), over the strongest path
    several origins   = 1 - product(1 - impact from each origin)    (independent causes)

Impact is an estimate of the share of a component's work that fails. Below ``THRESHOLD`` a
component is reachable but not counted as affected. The search is a breadth-first search over
dependents; a component is visited again only when a stronger path to it is found, so it ends
also in cyclic graphs.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

THRESHOLD = 0.05
#: How much a dependent needs what it depends on, by the mode of the dependency.
MODE = {"required": 1.0, "degradable": 0.5, "best_effort": 0.1}
#: Dependencies that are not calls between services.
#: hosted_on     nothing runs without the machine it runs on
#: connected_to  without the network, calls to other services fail; work done alone goes on
#: monitors      monitoring degrades without its source, the bank does not notice
KIND = {"hosted_on": 1.0, "connected_to": 0.7, "monitors": 0.25}
CALL_KINDS = ("calls", "routes_to", "stores_in")  # dependencies that carry work
LEVELS = (("severe", 0.75), ("high", 0.4), ("moderate", 0.15), ("low", THRESHOLD))
LEVEL_MEANING = {
    "severe": "most of its work fails: likely to fail itself",
    "high": "a large part of its work fails",
    "moderate": "some of its work fails",
    "low": "little of its work is affected",
    "none": "not affected",
}


def level(impact: float) -> str:
    return next((name for name, limit in LEVELS if impact >= limit), "none")


def strength(kind: str, mode: str, weight: float) -> float:
    """Share of the dependent's work that needs the other side, 0..1."""
    if kind in KIND:
        return KIND[kind]
    return round(max(0.0, min(1.0, MODE.get(mode, 1.0) * weight)), 4)


@dataclass(frozen=True)
class Node:
    id: str
    name: str
    type: str
    criticality: int  # 1..5
    group: str = "bank"


@dataclass(frozen=True)
class Edge:
    id: str
    source: str  # depends on ...
    target: str  # ... this
    kind: str
    mode: str
    weight: float
    strength: float


@dataclass
class DependencyGraph:
    nodes: dict[str, Node]
    edges: dict[str, Edge]
    dependents: dict[str, list[Edge]] = field(init=False)  # edges pointing at a node
    depends_on: dict[str, list[Edge]] = field(init=False)

    def __post_init__(self) -> None:
        self.dependents = {n: [] for n in self.nodes}
        self.depends_on = {n: [] for n in self.nodes}
        for e in self.edges.values():
            if e.source not in self.nodes or e.target not in self.nodes:
                raise ValueError(f"edge {e.id} connects an unknown node")
            self.dependents[e.target].append(e)
            self.depends_on[e.source].append(e)
        for lst in (*self.dependents.values(), *self.depends_on.values()):
            lst.sort(key=lambda e: e.id)  # the same graph is always searched the same way

    @classmethod
    def from_parts(cls, nodes: Iterable[Node], edges: Iterable[tuple]) -> DependencyGraph:
        """``edges``: (source, target, kind, mode, weight); source depends on target."""
        built = {}
        for source, target, kind, mode, weight in edges:
            e = Edge(f"{source}->{target}", source, target, kind, mode, float(weight),
                     strength(kind, mode, float(weight)))
            built[e.id] = e
        return cls({n.id: n for n in nodes}, built)

    @classmethod
    def from_blueprint(cls, bp) -> DependencyGraph:
        """The twin's current structure. Called again whenever the structure changes."""
        nodes = [Node(c.id, c.name, c.type.value, c.criticality, c.group)
                 for c in bp.components.values()]
        return cls.from_parts(nodes, [(k.source, k.target, k.kind.value, k.mode, k.weight)
                                      for k in bp.links.values()])

    @property
    def checksum(self) -> str:
        data = [sorted(self.nodes), sorted((e.id, e.strength) for e in self.edges.values())]
        return hashlib.sha256(json.dumps(data).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Reach:
    """How one origin reaches one component."""

    origin: str
    impact: float
    steps: int
    path: tuple[str, ...]  # origin first, the affected component last
    links: tuple[str, ...]  # the edges along the path


@dataclass(frozen=True)
class Impact:
    id: str
    impact: float
    level: str
    score: float  # impact x criticality, 0..5
    failed: bool  # one of the origins: a confirmed failure, not a prediction
    via: tuple[Reach, ...]  # strongest first

    @property
    def steps(self) -> int:
        return min((r.steps for r in self.via), default=0)


@dataclass(frozen=True)
class Analysis:
    origins: tuple[str, ...]
    graph: str  # checksum of the graph it was made on
    impacts: dict[str, Impact]  # every reachable component, the origins included
    links: dict[str, tuple[float, int]]  # edge -> (impact carried, step at which it is used)

    @property
    def affected(self) -> list[Impact]:
        """Potentially affected components, strongest first: not the origins, not 'none'."""
        return sorted((i for i in self.impacts.values() if not i.failed and i.level != "none"),
                      key=lambda i: (-i.impact, i.steps, i.id))

    @property
    def reachable(self) -> list[str]:
        """Every component a failure could travel to, however weakly."""
        return sorted(i for i, x in self.impacts.items() if not x.failed)

    def expected_affected(self) -> float:
        """Expected number of affected components: the sum of the shares."""
        return round(sum(i.impact for i in self.affected), 2)

    def count(self, at_least: str = "moderate") -> int:
        names = [n for n, _ in LEVELS]
        keep = set(names[: names.index(at_least) + 1])
        return sum(i.level in keep for i in self.affected)

    def business_impact(self) -> float:
        return round(sum(i.score for i in self.affected), 3)

    def overall(self) -> str:
        return self.affected[0].level if self.affected else "none"


def _search(graph: DependencyGraph, origin: str, threshold: float) -> dict[str, Reach]:
    """Breadth-first over dependents; a node is queued again only on a stronger path."""
    best = {origin: Reach(origin, 1.0, 0, (origin,), ())}
    queue = deque([origin])
    while queue:
        here = best[queue.popleft()]
        for e in graph.dependents[here.path[-1]]:
            nxt = here.impact * e.strength
            if nxt <= 0 or e.source in here.path:
                continue
            old = best.get(e.source)
            stronger = old is None or nxt > old.impact + 1e-12 or \
                (abs(nxt - old.impact) <= 1e-12 and here.steps + 1 < old.steps)
            if stronger:
                best[e.source] = Reach(origin, nxt, here.steps + 1, (*here.path, e.source),
                                       (*here.links, e.id))
                if nxt >= threshold:  # weaker paths are not followed further
                    queue.append(e.source)
    return best


def analyse(graph: DependencyGraph, failed: Iterable[str],
            threshold: float = THRESHOLD) -> Analysis:
    origins = tuple(dict.fromkeys(failed))
    unknown = [o for o in origins if o not in graph.nodes]
    if unknown:
        raise KeyError(f"unknown component(s): {unknown}")
    per: dict[str, list[Reach]] = {}
    for o in origins:
        for cid, r in _search(graph, o, threshold).items():
            per.setdefault(cid, []).append(r)
    impacts: dict[str, Impact] = {}
    links: dict[str, tuple[float, int]] = {}
    for cid, reaches in per.items():
        reaches.sort(key=lambda r: (-r.impact, r.steps, r.origin))
        is_origin = cid in origins
        if is_origin:
            total = 1.0
        else:
            spared = 1.0
            for r in reaches:
                spared *= 1 - r.impact
            total = 1 - spared
        total = round(total, 4)
        node = graph.nodes[cid]
        impacts[cid] = Impact(cid, total, "failed" if is_origin else level(total),
                              round(total * node.criticality, 3), is_origin,
                              tuple(r for r in reaches if not is_origin or r.origin == cid))
        if not is_origin and level(total) != "none":
            for r in reaches:
                if r.impact < threshold:
                    continue
                for step, lid in enumerate(r.links, 1):
                    carried = round(r.impact if step == len(r.links) else
                                    per_link_impact(graph, r, step), 4)
                    old = links.get(lid)
                    if old is None or carried > old[0]:
                        links[lid] = (carried, step)
    return Analysis(origins, graph.checksum, impacts, links)


def per_link_impact(graph: DependencyGraph, r: Reach, step: int) -> float:
    """The impact that has arrived after ``step`` edges of the path."""
    x = 1.0
    for lid in r.links[:step]:
        x *= graph.edges[lid].strength
    return x


# ------------------------------------------------------------------------ the whole bank
@dataclass(frozen=True)
class Criticality:
    id: str
    affected: int  # at least 'moderate'
    severe: int
    expected: float
    business_impact: float
    single_point: bool  # its failure alone makes at least two others likely to fail


def critical_components(graph: DependencyGraph, group: str | None = "bank") -> list[Criticality]:
    """Every component failing alone, ranked by what it would do to the rest."""
    out = []
    for cid, node in graph.nodes.items():
        if group and node.group != group:
            continue
        a = analyse(graph, [cid])
        severe = sum(i.level == "severe" for i in a.affected)
        out.append(Criticality(cid, a.count(), severe, a.expected_affected(),
                               a.business_impact(), severe >= 2))
    return sorted(out, key=lambda c: (-c.business_impact, -c.affected, c.id))


# ------------------------------------------------------------------------------ the report
def report(graph: DependencyGraph, a: Analysis, statuses: Mapping[str, str] | None = None,
           calls: Mapping[str, float] | None = None, title: str = "") -> dict:
    """Everything a person needs to judge the impact, as data; ``markdown`` writes it as text.

    ``statuses``: current status per component, to tell confirmed from predicted.
    ``calls``: calls per second per edge, to say who loses callers.
    """
    statuses = statuses or {}
    calls = calls or {}
    name = lambda c: graph.nodes[c].name  # noqa: E731
    rows = []
    for i in a.affected:
        best = i.via[0]
        status = statuses.get(i.id)
        rows.append({
            "id": i.id, "name": name(i.id), "type": graph.nodes[i.id].type,
            "impact": i.impact, "level": i.level, "meaning": LEVEL_MEANING[i.level],
            "criticality": graph.nodes[i.id].criticality, "score": i.score, "steps": i.steps,
            "chain": [name(c) for c in best.path], "chain_ids": list(best.path),
            "origins": [r.origin for r in i.via], "status": status,
            "confirmed": status in ("failed", "warning", "overloaded"),
        })
    unaffected = []
    for o in a.origins:
        # what the origin depends on keeps working, but loses its calls
        for e in graph.depends_on[o]:
            if e.kind not in CALL_KINDS or (e.target in a.impacts
                                            and a.impacts[e.target].level != "none"):
                continue
            unaffected.append({"id": e.target, "name": name(e.target), "kind": e.kind,
                               "loses_calls_per_s": calls.get(e.id), "of": o})
    return {
        "title": title or "Impact of " + ", ".join(name(o) for o in a.origins),
        "origins": [{"id": o, "name": name(o), "criticality": graph.nodes[o].criticality,
                     "status": statuses.get(o)} for o in a.origins],
        "graph": a.graph, "components": len(graph.nodes), "dependencies": len(graph.edges),
        "reachable": len(a.reachable),
        "affected": a.count(), "affected_severe": a.count("severe"),
        "affected_any": a.count("low"), "expected_affected": a.expected_affected(),
        "business_impact": a.business_impact(), "level": a.overall(),
        "impacts": rows,
        "not_affected_dependencies": unaffected,
        "method": "weighted breadth-first search over dependents; strength = mode x weight; "
                  "several origins combined as independent causes",
    }


def markdown(r: dict) -> str:
    lines = [f"# {r['title']}", "",
             f"Failed: {', '.join(o['name'] for o in r['origins'])}. "
             f"Graph of {r['components']} components and {r['dependencies']} dependencies "
             f"(`{r['graph']}`).", "",
             f"* Potentially affected (at least moderate): **{r['affected']}**, of which "
             f"{r['affected_severe']} likely to fail; reachable at all: {r['reachable']}",
             f"* Expected number of affected components: {r['expected_affected']}",
             f"* Overall impact level: **{r['level']}**; business impact "
             f"{r['business_impact']} (impact x criticality, summed)", ""]
    if r["impacts"]:
        lines += ["| Component | Impact | Level | Steps | Chain | Now |",
                  "|---|---|---|---|---|---|"]
        for x in r["impacts"]:
            lines.append(f"| {x['name']} | {x['impact']:.0%} | {x['level']} | {x['steps']} | "
                         f"{' → '.join(x['chain'])} | {x['status'] or '–'} |")
    else:
        lines.append("Nothing depends on it strongly enough to be affected.")
    if r["not_affected_dependencies"]:
        lines += ["", "Not affected, because the failed component depends on them and not the "
                  "other way round (they only lose its calls):", ""]
        for x in r["not_affected_dependencies"]:
            lost = f", {x['loses_calls_per_s']:.2g} calls/s fewer" \
                if x["loses_calls_per_s"] else ""
            lines.append(f"* {x['name']} ({x['kind']}{lost})")
    lines += ["", f"Method: {r['method']}. Impact is an estimate of the share of a component's "
              "work that fails, not a measurement."]
    return "\n".join(lines) + "\n"
