"""``plan`` command: search a fixed gray-release table.

One fixed schedule must be safe *and* reachability-preserving for every
admitted scenario the offline input allows:

* every assignment of registered link delays (the search defaults to the
  delay extremes, which are sufficient because intermediate delays only shift
  arrival steps inside the same interleaving envelope; ``delay_mode="grid"``
  enumerates the full Cartesian product);
* the no-failure case and the single failure-domain window (either at the
  input's fixed steps, or aligned to every relevant step boundary when
  ``failure_mode="aligned"``).

The plan never observes future run-time delays: barrier drain actions may only
choose a wait length and check at the slot end.  Schedules are minimised in
the lexicographic order (total paused proxy time, release slots used, action
id sequence).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any, Optional

from .engine import SimConfig, Simulator, Violation
from .errors import ErrorCode, ValidationError
from .model import Case, WILDCARD

# Optional tuning knobs accepted under $.plan.search (all have safe defaults).
DEFAULT_DELAY_MODE = "extremes"
DEFAULT_FAILURE_MODE = "fixed"
DEFAULT_SCENARIO_CAP = 4096


# --------------------------------------------------------------------------- #
# Scenario generation
# --------------------------------------------------------------------------- #
def delay_scenarios(case: Case, mode: str, cap: int) -> list[dict[str, int]]:
    links = sorted(case.links.values(), key=lambda l: l.id)
    if mode == "grid":
        ranges = [range(l.delay_min, l.delay_max + 1) for l in links]
        out: list[dict[str, int]] = []
        for combo in itertools.product(*ranges):
            out.append({l.id: d for l, d in zip(links, combo)})
            if len(out) >= cap:
                break
        return out
    if mode != "extremes":
        raise ValidationError(
            ErrorCode.ILLEGAL_VALUE,
            "delay_mode must be 'extremes' or 'grid'",
            "$.plan.search.delay_mode",
        )
    # extremes: every (min, max) corner of the delay hyper-rectangle
    out = []
    for bits in itertools.product((0, 1), repeat=len(links)):
        assignment = {}
        for link, bit in zip(links, bits):
            assignment[link.id] = link.delay_max if bit else link.delay_min
        out.append(assignment)
        if len(out) >= cap:
            break
    return out


def failure_scenarios(case: Case, mode: str, anchor_steps: list[int]) -> list[Optional[dict]]:
    base = case.failure
    if base is None:
        return [None]
    if mode == "fixed":
        return [{
            "domain": base.domain,
            "down_step": base.down_step,
            "up_step": base.up_step,
        }]
    if mode != "aligned":
        raise ValidationError(
            ErrorCode.ILLEGAL_VALUE,
            "failure_mode must be 'fixed' or 'aligned'",
            "$.plan.search.failure_mode",
        )
    duration = base.up_step - base.down_step
    horizons = [h for h in (case.plan_horizon, case.replay_horizon) if h]
    horizon = max(horizons) if horizons else base.up_step
    windows: list[Optional[dict]] = [None]
    for anchor in sorted(set(anchor_steps)):
        down = anchor
        up = anchor + duration
        if down >= horizon:
            continue
        windows.append({
            "domain": base.domain,
            "down_step": down,
            "up_step": min(up, horizon + 1),
        })
    # de-duplicate identical windows deterministically
    seen: set[tuple] = set()
    unique: list[Optional[dict]] = []
    for w in windows:
        key = ("none",) if w is None else (w["domain"], w["down_step"], w["up_step"])
        if key not in seen:
            seen.add(key)
            unique.append(w)
    return unique


def build_scenarios(case: Case, anchor_steps: list[int],
                    settings: dict) -> list[SimConfig]:
    delay_mode = settings.get("delay_mode", DEFAULT_DELAY_MODE)
    failure_mode = settings.get("failure_mode", DEFAULT_FAILURE_MODE)
    cap = int(settings.get("scenario_cap", DEFAULT_SCENARIO_CAP))
    horizon = case.plan_horizon or case.replay_horizon

    delays = delay_scenarios(case, delay_mode, cap)
    failures = failure_scenarios(case, failure_mode, anchor_steps)
    configs: list[SimConfig] = []
    seen: set[tuple] = set()
    idx = 0
    for failure in failures:
        for assignment in delays:
            signature = (tuple(sorted(assignment.items())),
                         None if failure is None
                         else (failure["domain"], failure["down_step"], failure["up_step"]))
            if signature in seen:
                continue  # collapse identical (e.g. delay_min == delay_max) corners
            seen.add(signature)
            idx += 1
            recovery = None
            if failure is not None:
                domain_links = [l for l in case.links.values()
                                if l.failure_domain == failure["domain"]]
                recovery = min((l.delay_min for l in domain_links), default=1)
            configs.append(SimConfig(
                name=f"scenario-{idx:04d}",
                delays=dict(assignment),
                failure_domain=None if failure is None else failure["domain"],
                down_step=None if failure is None else failure["down_step"],
                up_step=None if failure is None else failure["up_step"],
                recovery_delay=recovery,
                horizon=horizon,
                schedule=(),
            ))
            if idx >= cap:
                return configs
    return configs


# --------------------------------------------------------------------------- #
# Schedule representation and constraints
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Placement:
    slot: int
    action_id: str
    wait_slots: Optional[int]


def _topo_order(case: Case) -> list[str]:
    preds: dict[str, set[str]] = {a: set() for a in case.actions}
    for a, b in case.precedence:
        preds[b].add(a)
    ordered: list[str] = []
    ready = sorted(a for a, p in preds.items() if not p)
    done: set[str] = set()
    while ready:
        pick = ready.pop(0)
        ordered.append(pick)
        done.add(pick)
        for a, b in case.precedence:
            if a == pick:
                preds[b].discard(pick)
        newly = sorted(a for a, p in preds.items() if not p and a not in done and a not in ready)
        ready.extend(newly)
        ready.sort()
    if len(ordered) != len(case.actions):
        raise ValidationError(
            ErrorCode.SEMANTIC_CONFLICT, "precedence graph is not schedulable", "$.precedence"
        )
    return ordered


class ScheduleBuilder:
    def __init__(self, case: Case, slots: int, max_wait: int):
        self.case = case
        self.slots = slots
        self.max_wait = max_wait
        self.order = _topo_order(case)
        self.proxy_of = {aid: act.proxy for aid, act in case.actions.items()}
        self.type_of = {aid: act.type for aid, act in case.actions.items()}
        # precedence position requirements
        self.preds: dict[str, list[str]] = {a: [] for a in case.actions}
        self.succs: dict[str, list[str]] = {a: [] for a in case.actions}
        for a, b in case.precedence:
            self.preds[b].append(a)
            self.succs[a].append(b)
        self.best: Optional[tuple] = None
        self.best_placements: Optional[tuple[Placement, ...]] = None
        self.evaluated = 0
        self.pruned = 0
        # Bounded completeness: the cross-product of independent components can
        # be enormous on a deliberately adversarial input.  The search stays
        # exhaustive up to these budgets and, if they are hit, reports
        # ``search_truncated`` instead of silently claiming completeness.
        self.node_budget = 20_000
        self.component_budget = 2_500
        self.nodes_seen = 0
        self.truncated = False
        # connected components of the "same proxy OR precedence" conflict graph:
        # actions in different components never constrain each other and may be
        # scheduled independently, which collapses the cross-product search.
        self._components = self._build_components()

    def _build_components(self) -> list[set[str]]:
        parent = {a: a for a in self.case.actions}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            parent[find(a)] = find(b)

        ids = list(self.case.actions)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if self.proxy_of[a] is not None and self.proxy_of[a] == self.proxy_of[b]:
                    union(a, b)
        for a, b in self.case.precedence:
            union(a, b)
        groups: dict[str, set[str]] = {}
        for a in ids:
            groups.setdefault(find(a), set()).add(a)
        return sorted(groups.values(), key=lambda g: min(g))

    # -- hard scheduling constraints --------------------------------------
    def _slot_bounds(self, action_id: str, placements: dict[str, Placement]) -> tuple[int, int]:
        lo = 0
        for pred in self.preds[action_id]:
            if pred in placements:
                lo = max(lo, placements[pred].slot + 1)
        hi = self.slots - 1
        for succ in self.succs[action_id]:
            if succ in placements:
                hi = min(hi, placements[succ].slot - 1)
        return lo, hi

    def _proxy_free(self, action_id: str, slot: int,
                    placements: dict[str, Placement]) -> bool:
        proxy = self.proxy_of[action_id]
        for other in placements.values():
            if self.proxy_of[other.action_id] != proxy:
                continue
            if other.slot == slot:
                return False
            # an open barrier wait blocks the proxy until its check slot
            other_act = self.case.actions[other.action_id]
            if other_act.type == "barrier":
                wait = other.wait_slots or 1
                check = other.slot + wait
                if other.slot < slot <= check:
                    return False
        return True

    def _pause_interval_ok(self, action_id: str, slot: int,
                           placements: dict[str, Placement]) -> bool:
        proxy = self.proxy_of[action_id]
        atype = self.type_of[action_id]
        if atype not in ("pause", "resume"):
            return True
        pause_slot = resume_slot = None
        for other in placements.values():
            if self.proxy_of[other.action_id] != proxy:
                continue
            if self.type_of[other.action_id] == "pause":
                pause_slot = other.slot
            if self.type_of[other.action_id] == "resume":
                resume_slot = other.slot
        if atype == "pause":
            return resume_slot is None  # no dangling resume before its pause
        return pause_slot is not None and pause_slot < slot

    # -- search ------------------------------------------------------------
    def _enumerate_component(self, members: set[str]) -> list[tuple[Placement, ...]]:
        """All *normalised* legal partial tables for one conflict component.

        A normalised table is anchored at slot 0 (its earliest placement is on
        slot 0); absolute placement against the failure window is recovered at
        merge time by enumerating a per-component offset.  This keeps component
        relative timing canonical while the offset enumeration still explores
        every cross-component interleaving.
        """
        local_order = [a for a in self.order if a in members]
        results: list[tuple[Placement, ...]] = []

        def dfs(index: int, placements: dict[str, Placement]) -> None:
            if len(results) >= self.component_budget:
                self.truncated = True
                return
            if index == len(local_order):
                tup = tuple(sorted(placements.values(),
                                   key=lambda p: (p.slot, p.action_id)))
                anchor = min(p.slot for p in tup)
                normalised = tuple(
                    Placement(p.slot - anchor, p.action_id, p.wait_slots)
                    for p in tup
                )
                if normalised not in results:
                    results.append(normalised)
                return
            action_id = local_order[index]
            action = self.case.actions[action_id]
            lo, hi = self._slot_bounds(action_id, placements)
            if index == 0:
                lo = hi = 0
            wait_options: list[Optional[int]] = [None]
            if action.type == "barrier":
                wait_options = list(range(1, self.max_wait + 1))
            for slot in range(lo, hi + 1):
                if not self._proxy_free(action_id, slot, placements):
                    continue
                if not self._pause_interval_ok(action_id, slot, placements):
                    continue
                for wait in wait_options:
                    if action.type == "barrier" and slot + (wait or 1) > self.slots:
                        continue
                    placements[action_id] = Placement(slot, action_id, wait)
                    dfs(index + 1, placements)
                    del placements[action_id]

        dfs(0, {})
        # order component tables by their own footprint for objective-first merge
        results.sort(key=lambda t: (max(p.slot for p in t),
                                    tuple(p.action_id for p in t)))
        return results

    @staticmethod
    def _shift(table: tuple[Placement, ...], offset: int) -> tuple[Placement, ...]:
        return tuple(Placement(p.slot + offset, p.action_id, p.wait_slots)
                     for p in table)

    def search(self, evaluate) -> Optional[tuple[Placement, ...]]:
        component_tables = [self._enumerate_component(m) for m in self._components]
        if any(not tables for tables in component_tables):
            return None
        # largest component first tightens pruning earliest
        order_idx = sorted(range(len(component_tables)),
                           key=lambda i: -len(component_tables[i]))
        tables = [component_tables[i] for i in order_idx]

        def merge(idx: int, acc: tuple[Placement, ...]) -> None:
            if self.nodes_seen >= self.node_budget:
                self.truncated = True
                return
            if idx == len(tables):
                self.nodes_seen += 1
                merged = tuple(sorted(acc, key=lambda p: (p.slot, p.action_id)))
                self.evaluated += 1
                verdict = evaluate(merged)
                if verdict is None:
                    return
                obj = self._objective(merged)
                if self.best is None or obj < self.best:
                    self.best = obj
                    self.best_placements = merged
                return
            for partial in tables[idx]:
                max_rel = max(p.slot for p in partial)
                # whole-table translation symmetry: pin the first component to
                # offset 0; later components may sit at any feasible offset
                offset_hi = self.slots - 1 - max_rel
                for offset in range(0, offset_hi + 1):
                    shifted = self._shift(partial, offset)
                    candidate = acc + shifted
                    # branch-and-bound: cannot improve the incumbent's slot goal
                    if self.best is not None:
                        cand_max = max(p.slot for p in candidate)
                        if cand_max + 1 > self.best[1]:
                            self.pruned += 1
                            continue
                    # components share no proxy/precedence, so no slot conflict
                    merge(idx + 1, candidate)

        merge(0, ())
        return self.best_placements

    def _objective(self, placements: tuple[Placement, ...]) -> tuple:
        pause_time = 0
        by_proxy: dict[str, dict[str, int]] = {}
        for pl in placements:
            atype = self.type_of[pl.action_id]
            proxy = self.proxy_of[pl.action_id]
            by_proxy.setdefault(proxy, {})[atype] = pl.slot
        for proxy, marks in by_proxy.items():
            if "pause" in marks:
                end = marks.get("resume", self.slots)
                pause_time += end - marks["pause"]
        max_slot = max(pl.slot for pl in placements)
        seq = tuple(pl.action_id for pl in sorted(placements, key=lambda p: (p.slot, p.action_id)))
        return (pause_time, max_slot + 1, seq)


# --------------------------------------------------------------------------- #
# Static, schedule-independent reachability pre-check
# --------------------------------------------------------------------------- #
def static_reachability_bounds(case: Case) -> list[dict]:
    """Shortest possible hop time for each request, independent of scheduling.

    If even the all-minimum-delay path cannot finish inside the TTL/deadline,
    no release table can ever restore reachability, so the planner can reject
    the input without enumerating a single schedule.
    """
    report: list[dict] = []
    for req in case.requests.values():
        best = _dijkstra_min_time(case, req.ingress, req.target)
        report.append({
            "request_id": req.id,
            "minimum_path_steps": best,
            "ttl": req.ttl,
            "deadline_step": req.deadline_step,
            "structurally_unreachable": (
                best is None
                or best > req.ttl
                or req.emit_step + best > req.deadline_step
            ),
        })
    return report


def _dijkstra_min_time(case: Case, src: str, target: str) -> Optional[int]:
    import heapq

    distances = {src: 0}
    heap = [(0, src)]
    while heap:
        dist, node = heapq.heappop(heap)
        if dist != distances.get(node):
            continue
        if node == target:
            return dist
        for link in case.out_links.get(node, []):
            if WILDCARD not in link.carries and target not in link.carries:
                continue
            nd = dist + link.delay_min
            if nd < distances.get(link.dst, 10**18):
                distances[link.dst] = nd
                heapq.heappush(heap, (nd, link.dst))
    return None


# --------------------------------------------------------------------------- #
# Evaluation across all scenarios
# --------------------------------------------------------------------------- #
def placements_to_schedule(placements: tuple[Placement, ...]) -> tuple[tuple[int, str, Optional[int]], ...]:
    return tuple(sorted(
        ((p.slot, p.action_id, p.wait_slots) for p in placements),
        key=lambda x: (x[0], x[1]),
    ))


def evaluate_factory(case: Case, base_configs: list[SimConfig]):
    cache: dict[tuple, Optional[dict]] = {}
    # cheapest, most-permissive scenario first: no failure and all-minimum
    # delays rejects the largest number of bad schedules at the lowest cost
    ordered = sorted(
        base_configs,
        key=lambda c: (c.failure_domain is not None,
                       sum(c.delays.values()), c.name),
    )

    def evaluate(placements: tuple[Placement, ...]) -> Optional[dict]:
        sched = placements_to_schedule(placements)
        if sched in cache:
            return cache[sched]
        worst: Optional[dict] = None
        for base in ordered:
            cfg = SimConfig(
                name=base.name,
                delays=dict(base.delays),
                failure_domain=base.failure_domain,
                down_step=base.down_step,
                up_step=base.up_step,
                recovery_delay=base.recovery_delay,
                horizon=base.horizon,
                schedule=sched,
            )
            result = Simulator(case, cfg).run()
            if result["violations"]:
                worst = {
                    "scenario": base.name,
                    "first_violation": result["violations"][0],
                    "violation_count": len(result["violations"]),
                }
                break
            if worst is None:
                worst = {"scenario": base.name, "first_violation": None, "violation_count": 0}
        cache[sched] = None if worst and worst["first_violation"] is not None else worst
        return cache[sched]

    evaluate.cache = cache  # type: ignore[attr-defined]
    return evaluate


# --------------------------------------------------------------------------- #
# Baseline analysis for infeasible inputs
# --------------------------------------------------------------------------- #
def baseline_constraint_conflict(case: Case) -> Optional[dict]:
    """Walk the baseline order and return the earliest scheduling conflict."""
    positions: dict[str, int] = {}
    proxy_slots: dict[str, list[int]] = {}
    open_barriers: dict[str, tuple[int, int]] = {}  # proxy -> (slot, check)
    pause_open: dict[str, int] = {}
    for i, entry in enumerate(sorted(case.baseline, key=lambda e: (e.slot, e.action))):
        action = case.actions[entry.action]
        for pred in [a for a, b in case.precedence if b == entry.action]:
            if pred not in positions:
                return {
                    "kind": "precedence_unsatisfied",
                    "index": i,
                    "slot": entry.slot,
                    "action": entry.action,
                    "missing_predecessor": pred,
                    "prefix": [e.action for e in case.baseline[: i + 1]],
                }
            if positions[pred] >= entry.slot:
                return {
                    "kind": "precedence_order_violation",
                    "index": i,
                    "slot": entry.slot,
                    "action": entry.action,
                    "predecessor": pred,
                    "predecessor_slot": positions[pred],
                    "prefix": [e.action for e in case.baseline[: i + 1]],
                }
        proxy = action.proxy
        if proxy is not None:
            if entry.slot in proxy_slots.setdefault(proxy, []):
                return {
                    "kind": "proxy_slot_conflict",
                    "index": i,
                    "slot": entry.slot,
                    "action": entry.action,
                    "proxy": proxy,
                    "prefix": [e.action for e in case.baseline[: i + 1]],
                }
            if proxy in open_barriers:
                bslot, check = open_barriers[proxy]
                if bslot < entry.slot <= check:
                    return {
                        "kind": "barrier_window_violation",
                        "index": i,
                        "slot": entry.slot,
                        "action": entry.action,
                        "proxy": proxy,
                        "barrier_slot": bslot,
                        "check_slot": check,
                        "prefix": [e.action for e in case.baseline[: i + 1]],
                    }
            if action.type == "pause":
                if proxy in pause_open:
                    return {
                        "kind": "nested_pause", "index": i, "slot": entry.slot,
                        "action": entry.action, "proxy": proxy,
                        "prefix": [e.action for e in case.baseline[: i + 1]],
                    }
                pause_open[proxy] = entry.slot
            if action.type == "resume":
                if proxy not in pause_open:
                    return {
                        "kind": "resume_without_pause", "index": i, "slot": entry.slot,
                        "action": entry.action, "proxy": proxy,
                        "prefix": [e.action for e in case.baseline[: i + 1]],
                    }
                del pause_open[proxy]
        if action.type == "barrier":
            wait = entry.wait_slots or 1
            assert proxy is not None
            open_barriers[proxy] = (entry.slot, entry.slot + wait)
        positions[entry.action] = entry.slot
        proxy_slots.setdefault(proxy or "", []).append(entry.slot)
    return None


def baseline_simulation_failure(case: Case, base_configs: list[SimConfig]) -> dict:
    """The baseline respects scheduling constraints; find its shortest failing prefix."""
    sched = tuple((e.slot, e.action, e.wait_slots) for e in
                  sorted(case.baseline, key=lambda e: (e.slot, e.action)))
    candidates: list[dict] = []
    for base in base_configs:
        cfg = SimConfig(
            name=base.name, delays=dict(base.delays),
            failure_domain=base.failure_domain, down_step=base.down_step,
            up_step=base.up_step, recovery_delay=base.recovery_delay,
            horizon=base.horizon, schedule=sched,
        )
        result = Simulator(case, cfg).run()
        for v in result["violations"]:
            candidates.append({
                "scenario": base.name,
                "code": v["code"],
                "step": v["step"],
                "event_id": v["event_id"],
                "detail": v["detail"],
                "request_id": v["request_id"],
                "prefix": v["prefix"],
            })
    if not candidates:
        return {"kind": "no_failure", "scenarios_checked": len(base_configs)}
    candidates.sort(key=lambda c: (
        len(c["prefix"]),
        [int(p[1:]) for p in c["prefix"]],
        c["code"], c["event_id"],
    ))
    winner = candidates[0]
    winner["kind"] = "simulation_failure"
    winner["tie_break_rule"] = (
        "shortest prefix length, then lexicographically smallest event-id "
        "sequence, then violation code, then event id"
    )
    winner["scenarios_checked"] = len(base_configs)
    return winner


# --------------------------------------------------------------------------- #
# Exhaustive cross-check for small topologies
# --------------------------------------------------------------------------- #
def exhaustive_crosscheck(case: Case, base_configs: list[SimConfig],
                          slots: int, max_wait: int) -> dict:
    """Enumerate *every* legal schedule and independently verify the optimum.

    Intended for small topologies (few actions/proxies); used by the test
    suite to cross-check the DFS search result.
    """
    actions = sorted(case.actions)
    legal: list[tuple[Placement, ...]] = []

    def all_placements(index: int, current: dict[str, Placement]):
        if index == len(actions):
            legal.append(tuple(sorted(current.values(), key=lambda p: (p.slot, p.action_id))))
            return
        aid = actions[index]
        action = case.actions[aid]
        waits = [None] if action.type != "barrier" else list(range(1, max_wait + 1))
        for slot in range(slots):
            for wait in waits:
                if action.type == "barrier" and slot + (wait or 1) > slots:
                    continue
                current[aid] = Placement(slot, aid, wait)
                if _locally_legal(case, current):
                    all_placements(index + 1, current)
                del current[aid]

    all_placements(0, {})
    feasible: list[tuple[tuple, tuple[Placement, ...]]] = []
    evaluate = evaluate_factory(case, base_configs)
    for placements in legal:
        if evaluate(placements) is not None:
            builder = ScheduleBuilder(case, slots, max_wait)
            feasible.append((builder._objective(placements), placements))
    if not feasible:
        return {"legal_schedules": len(legal), "feasible_schedules": 0, "optimum": None}
    feasible.sort(key=lambda x: x[0])
    optimum = feasible[0]
    return {
        "legal_schedules": len(legal),
        "feasible_schedules": len(feasible),
        "optimum": {
            "objective": list(optimum[0]),
            "schedule": [
                {"slot": p.slot, "action": p.action_id, "wait_slots": p.wait_slots}
                for p in optimum[1]
            ],
        },
    }


def _locally_legal(case: Case, placements: dict[str, Placement]) -> bool:
    positions = {a: p.slot for a, p in placements.items()}
    for a, b in case.precedence:
        if a in positions and b in positions and positions[a] >= positions[b]:
            return False
    proxy_used: dict[tuple[str, int], str] = {}
    for aid, p in placements.items():
        proxy = case.actions[aid].proxy
        if proxy is None:
            continue
        key = (proxy, p.slot)
        if key in proxy_used:
            return False
        proxy_used[key] = aid
        if case.actions[aid].type == "barrier":
            wait = p.wait_slots or 1
            for other_aid, q in placements.items():
                if other_aid == aid or case.actions[other_aid].proxy != proxy:
                    continue
                if p.slot < q.slot <= p.slot + wait:
                    return False
    return True


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def run_plan(case: Case, raw_plan: Optional[dict] = None) -> dict[str, Any]:
    if case.plan_slots is None:
        raise ValidationError(
            ErrorCode.MISSING_FIELD, "plan section required for the plan command", "$.plan"
        )
    settings = dict(raw_plan.get("search", {}) if raw_plan else {})
    for key in settings:
        if key not in ("delay_mode", "failure_mode", "scenario_cap"):
            raise ValidationError(
                ErrorCode.ILLEGAL_VALUE,
                f"unknown search setting '{key}'",
                f"$.plan.search.{key}",
            )
    anchor_steps = list(range(case.plan_slots))
    anchor_steps += [r.emit_step for r in case.requests.values()]
    anchor_steps += [r.deadline_step for r in case.requests.values()]
    base_configs = build_scenarios(case, anchor_steps, settings)

    # schedule-independent structural rejection (no release table can help)
    static_report = static_reachability_bounds(case)
    structurally_dead = [r for r in static_report if r["structurally_unreachable"]]

    builder = ScheduleBuilder(case, case.plan_slots, case.max_wait_slots or 1)
    evaluate = evaluate_factory(case, base_configs)
    best = None if structurally_dead else builder.search(evaluate)

    if best is not None:
        obj = builder._objective(best)
        return {
            "status": "ok",
            "command": "plan",
            "case_id": case.case_id,
            "feasible": True,
            "objective": {
                "total_paused_proxy_slots": obj[0],
                "release_slots_used": obj[1],
                "action_id_sequence": list(obj[2]),
            },
            "schedule": [
                {"slot": p.slot, "action": p.action_id, "wait_slots": p.wait_slots}
                for p in best
            ],
            "scenarios_evaluated": len(base_configs),
            "schedules_evaluated": builder.evaluated,
            "branches_pruned": builder.pruned,
            "search_truncated": builder.truncated,
            "conflict_components": len(builder._components),
            "scenario_envelope": _scenario_envelope(base_configs),
            "compression_note": (
                "Timeline compression: the simulator visits only steps that carry "
                "an emission, arrival, control action, failure edge or TTL expiry; "
                "idle steps are jumped directly.  A live state is the tuple "
                "(in-flight token positions with absolute arrive/expiry steps, "
                "per-proxy installed/active versions, pause flags, generation "
                "floors).  States whose tokens are all terminal collapse to the "
                "same empty-in-flight signature, so histories that differ only in "
                "dead tokens share one continuation.  TTL <= 6 bounds the number of "
                "in-flight steps any token can occupy."
            ),
        }

    # infeasible: baseline analysis as required by the specification
    conflict = baseline_constraint_conflict(case)
    if conflict is not None:
        return {
            "status": "ok",
            "command": "plan",
            "case_id": case.case_id,
            "feasible": False,
            "reason": "baseline_scheduling_conflict",
            "earliest_conflict": conflict,
            "scenarios_evaluated": len(base_configs),
            "schedules_evaluated": builder.evaluated,
            "search_truncated": builder.truncated,
            "conflict_components": len(builder._components),
        }
    if structurally_dead:
        return {
            "status": "ok",
            "command": "plan",
            "case_id": case.case_id,
            "feasible": False,
            "reason": "structurally_unreachable",
            "static_reachability": static_report,
            "scenarios_evaluated": len(base_configs),
            "schedules_evaluated": builder.evaluated,
            "branches_pruned": builder.pruned,
            "search_truncated": builder.truncated,
            "conflict_components": len(builder._components),
        }
    failure = baseline_simulation_failure(case, base_configs) if case.baseline else {
        "kind": "no_baseline",
        "message": "no legal schedule exists and no baseline was provided",
    }
    return {
        "status": "ok",
        "command": "plan",
        "case_id": case.case_id,
        "feasible": False,
        "reason": "no_safe_schedule",
        "baseline_report": failure,
        "static_reachability": static_report,
        "scenarios_evaluated": len(base_configs),
        "schedules_evaluated": builder.evaluated,
        "search_truncated": builder.truncated,
        "conflict_components": len(builder._components),
    }


def _scenario_envelope(configs: list[SimConfig]) -> dict:
    domains = sorted({c.failure_domain for c in configs if c.failure_domain})
    delay_keys = sorted({k for c in configs for k in c.delays})
    return {
        "scenario_count": len(configs),
        "failure_domains_covered": domains,
        "delay_links_covered": delay_keys,
        "includes_no_failure": any(c.failure_domain is None for c in configs),
    }
