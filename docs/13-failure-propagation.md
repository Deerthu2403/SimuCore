# 13 · Failure propagation analysis

Phase 7. How a failure in one banking service can affect the others: a directed, weighted dependency
graph built from the twin's current structure, a breadth-first search over it, and a 3D view of the
result. The engine estimates; it does not measure. Every estimate is later compared with what was
measured.

| Part | Where |
|---|---|
| Graph, BFS, impact, critical components, report | `backend/app/digital_twin/propagation.py` |
| Live analysis in the twin, propagation events, outcome | `backend/app/digital_twin/impact.py` |
| API | `backend/app/api/v2.py` (`/api/v2/twin/propagation…`) |
| Storage | `database/migrations/006_propagation.sql` (table `propagation_event`) |
| 3D view and panel | `frontend/src/scene/*`, `frontend/src/ui/Impact.tsx`, `frontend/src/twin/impact.ts` |
| Tests | `backend/tests/test_propagation.py`, `frontend/src/test/impact.test.ts`, e2e U37–U39 |
| Validation on the real bank | `backend/live/propagation_check.py` → `backend/results/propagation.md` |

The Phase 1 engine (`backend/app/propagation`) still serves the old API v1 on its fixed reference
topology. The Phase 7 engine works on whatever the live twin holds.

## 1. The graph

Every component of the twin is a **node** (id, name, type, criticality 1–5). Every link is a
**directed edge** `a → b`, meaning *a depends on b*. The graph is not written anywhere by hand: it is
built from the twin's blueprint (`DependencyGraph.from_blueprint`) and rebuilt whenever the blueprint's
checksum changes, for example when services move from processes to containers and the bank host
disappears, or when a different dependency map is loaded. `GET /api/v2/twin/propagation/graph`
returns the graph in use.

The **strength** of an edge, 0–1, is how much of the failure of `b` reaches `a`:

| | strength |
|---|---|
| `calls`, `routes_to`, `stores_in` | mode × weight from the dependency map; mode: required 1.0, degradable 0.5, best effort 0.1 |
| `hosted_on` (runs on a machine) | 1.0: the machine goes, everything on it goes |
| `connected_to` (network) | 0.7 |
| `monitors` | 0.25: monitoring loses data, the bank keeps working |

Example: Transaction → Fraud is *degradable* with weight 0.5, so its strength is 0.25.

## 2. Breadth-first search

From each failed component (an **origin**, impact 1) the search walks to its *dependents*, never to
what it depends on. Along a path, impacts multiply:

```
impact(dependent) = max over paths  Π strength(edge)
```

The search is a BFS with relaxation: a node is put back in the queue only when a stronger path to it
has been found, so it ends on graphs with cycles, and with equal strength the path with fewer steps
wins. It does not continue from a node below the threshold 0.05. A unit test compares it with an
exhaustive search of all simple paths on random graphs.

Several origins are treated as independent causes: `1 − Π(1 − impactᵢ)`.

| level | impact | meaning |
|---|---|---|
| severe | ≥ 0.75 | likely to fail as well |
| high | ≥ 0.40 | a large part of its work is likely to fail |
| moderate | ≥ 0.15 | some of its work may fail |
| low | ≥ 0.05 | little effect expected |

**Score** = impact × criticality. **Business impact** = sum of the scores. **Affected** = components
at least moderately affected; **expected affected** = sum of the impacts (a count weighted by
likelihood).

## 3. The example of the brief

*If Transaction Processing fails, are Payment Gateway, Account Management, Fraud Detection and
Notification affected?* **No.** Transaction depends on them (it calls them), they do not depend on
Transaction. They keep working and only receive fewer calls. What is affected is the client entry
(load balancer), whose payments and transfers fail: impact 0.4, *high*. The report lists the four
under *not affected dependencies*, with the calls per second each one loses.

The reverse is different: if **Payment Gateway** fails, Transaction (required, weight 0.6) is
affected *high* (0.6), and the client entry *moderate* (0.24) through Transaction. If the **Database**
fails, seven components are affected, five of them severely. Critical components of the bank (each
failing alone, ranked by business impact): bank host, bank database, Database service, bank network,
Authentication; of these, all but the network are *single points of failure* (alone they make at least
two others likely to fail).

## 4. In the running twin

After every change the twin takes the **root causes** of its failed components as origins (a service
that fails because the database failed is not an origin itself) and runs the analysis. Each affected
component gets a role:

* **origin**: a confirmed failure that nothing else explains;
* **confirmed**: affected, and its own measurements show trouble (errors, latency, status). The twin's
  rule *depends on a failed component* does not count: that is the dependency map again, not a
  measurement;
* **predicted**: affected according to the graph only.

Every component view carries `impact` (role, impact, level, score, steps, chain, links, origins);
every link `display.propagation` and `display.propagation_step`; the summary `propagation`. They come
with the WebSocket deltas like everything else.

**Propagation events.** A new origin opens an event, stored with the full report. Sixty seconds later
it is closed with its outcome: which predicted components showed trouble (*hits*), which did not
(*not observed*), what showed trouble without being predicted (*missed*), precision and recall. If
the twin loses contact meanwhile the event is closed as *not judged*.

## 5. API

| | |
|---|---|
| `GET /api/v2/twin/propagation` | the current analysis and report; empty if nothing failed |
| `GET /api/v2/twin/propagation/analyze?failed=a,b[&format=markdown]` | what if these failed: nothing is stored or changed |
| `GET /api/v2/twin/propagation/critical?group=bank\|platform\|all` | critical components |
| `GET /api/v2/twin/propagation/graph` | the graph in use, strengths, thresholds |
| `GET /api/v2/twin/propagation/events` | stored events with outcomes (PostgreSQL) |
| `GET /api/v2/twin/propagation/events/live` | the events of this run, also without a database |

## 6. 3D view

The propagation has its own colour, teal, which is neither a status colour nor one of the AI's.

* **Failed component** (origin): its status already shows red; teal waves run outwards from it.
* **Confirmed impact**: a solid teal band, outermost around the component.
* **Predicted impact**: a *hatched* teal band; the brighter, the higher the impact.
* **Propagation path**: the links along which the failure travels get thicker and teal, and a pulse
  runs from the failed component, link after link, to the last affected one. Cables (runs on,
  connected to) are shown when a failure travels along them.
* **Label**: *confirmed impact: high 60 %, 1 step from Payment Gateway*.
* **Inspector**: the dependency chain, each step clickable.
* **Failure impact tab**: overall level, affected, likely to fail, expected number, business impact;
  each affected component with impact, evidence and chain; components not affected; critical
  components; propagation events with outcome. **Simulate a failure** lets one choose components and
  shows the result in the scene in place of the live estimate, clearly marked, with a Markdown report
  to download. A simulation switches nothing off.

With reduced motion the waves and pulses stand still; band and hatching remain.

## 7. Tests and validation

* `backend/tests/test_propagation.py`: chains, diamonds, fan-out, cycles, thresholds, zero weights,
  disconnected parts, several origins, determinism, random graphs against brute force, every failure
  location in the bank, a differently built bank, the graph after restructuring, roles, events and
  their outcome, API, WebSocket.
* `frontend/src/test/impact.test.ts`: roles and path in the scene, simulation and return to live,
  cables shown only on a path.
* e2e **U37**: real crash of Payment; origin, affected, path, label, teal pixels around Transaction,
  panel, event judged and stored. **U38**: simulated database failure. **U39**: graph and ranking
  equal the twin's structure.
* `backend/live/propagation_check.py` crashes each service of the real bank and compares prediction
  with measurement: [results](../backend/results/propagation.md).

**Result of the last run** (8 crashes, synthetic workload of 10 requests/s): precision 0.83, recall
0.83 (15 of 18 predicted components showed trouble; 18 showed trouble in all); rank correlation between
predicted impact and the measured rise of the error rate over 64 component pairs 0.88. All 8 events
were stored with their outcome. Where the engine was wrong:

* **Notification** is needed by Payment only *best effort* (strength 0.01 after weight), so nothing was
  predicted, yet Payment and Transaction showed trouble: Payment waits for the notification call until
  its timeout, and that latency travels upwards. Timeouts are not in the graph (see limits).
* **Customer** and **Transaction**: the client entry was predicted *high*, but its own rule did not
  fire. The failing requests were a small share of all customer traffic, below its warning threshold.
* **Fraud**: the client entry was affected through Transaction but predicted only *low*.

These are the cases a calibration of the strengths from recorded failures (planned P5) would correct.

## 8. Limits

* The strengths come from the dependency map's modes and weights. They are declared, not learned;
  how well they match reality is what the validation measures.
* Impact is a share of work that may fail, not a probability of failure, and the engine assumes
  independent causes.
* Retries, timeouts, circuit breakers and load shifting are not modelled: a dependent may suffer less
  (fallbacks) or something else may suffer more (load moving elsewhere) than the graph says.
