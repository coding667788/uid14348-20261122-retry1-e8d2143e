"""Deterministic discrete-event token simulator.

The simulator propagates *abstract markers* (no payloads) over a fixed
topology.  Time is a global discrete step counter; every hop consumes a
registered link delay expressed in steps.  The engine is deliberately
self-contained: both ``replay`` (one concrete scenario) and ``plan`` (many
scenarios sharing one fixed schedule) drive it.

Key modelling choices (also documented in README):

* TTL is absolute: a token carries ``expiry_step = emit_step + ttl`` and is
  dropped at the first processing step ``t >= expiry_step``.  TTL therefore
  ticks down on every step, including while a token waits on a paused proxy
  or is held behind a failed link.
* A proxy may hold several installed inbound versions simultaneously and a
  single active outbound version.  Inbound accept requires at least one
  ``accept`` match and zero ``deny`` matches across all installed versions.
* Marker rewrites only change the carried marker; the *original tenant* never
  changes and is what the service-side isolation check uses.
* Each barrier bumps a global generation counter.  At barrier completion the
  proxy's minimum-open generation advances, so tokens still stamped with an
  older generation are rejected as generation mismatches.  A barrier may only
  complete when every token in its witness set (tokens that could still reach
  the proxy) is already terminal.
* A failure domain that is down blocks its links: no new token enters, tokens
  already on the links are held.  At recovery the residual delay is rescaled
  by ``recovery_delay``; tokens whose TTL has expired meanwhile are dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .model import Case, Link, Request, WILDCARD


# Drop / anomaly reason codes ------------------------------------------------
class Reason:
    TTL_EXPIRED = "ttl_expired"
    TTL_EXPIRED_RECOVERY = "ttl_expired_at_recovery"
    PAUSED_QUEUED = "paused_queued"
    INBOUND_DENIED = "inbound_denied"
    INBOUND_NO_MATCH = "inbound_no_match"
    OUTBOUND_NO_MAPPING = "outbound_no_mapping"
    STALE_GENERATION = "stale_generation"
    ISOLATION_VIOLATION = "isolation_violation"
    ALLOWLIST_VIOLATION = "allowlist_violation"
    LATE_DELIVERY = "late_delivery"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    NO_ELIGIBLE_LINK = "no_eligible_link"
    BARRIER_INCOMPLETE = "barrier_incomplete"
    WITHDRAW_UNDRAINED = "withdraw_undrained"
    PAUSED_FORWARD = "paused_forward_attempt"


class Violation:
    ISOLATION = "isolation_violation"
    REACHABILITY = "reachability_failure"
    DRAIN_INCOMPLETE = "drain_incomplete"
    WITHDRAW_NO_PROOF = "withdraw_without_drain_proof"
    GENERATION_REINJECTION = "generation_reinjection"
    PAUSED_FORWARD = "paused_proxy_forward"
    RECEIPT_MIX = "receipt_overwrite"
    ALLOWLIST = "allowlist_violation"
    LATE = "late_delivery"
    DUPLICATE = "duplicate_delivery"
    STALE = "stale_generation_delivery"


# --------------------------------------------------------------------------- #
# Configuration and live state
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SimConfig:
    name: str
    delays: dict[str, int]                  # link id -> concrete delay (steps)
    failure_domain: Optional[str] = None
    down_step: Optional[int] = None
    up_step: Optional[int] = None
    recovery_delay: Optional[int] = None    # rescaled residual delay after up
    horizon: int = 100
    # scheduled control actions: (step, action_id, wait_slots for barriers)
    schedule: tuple[tuple[int, str, Optional[int]], ...] = ()


@dataclass
class Token:
    token_id: int
    request_id: str
    branch: str
    identity: str
    tenant: str            # original tenant, immutable
    marker: str            # current marker, rewritable
    target: str
    emit_step: int
    expiry_step: int
    gen_stamps: dict[str, int] = field(default_factory=dict)   # proxy -> gen
    provenance: list[str] = field(default_factory=list)        # proxy ids
    inbound_versions: list[tuple[str, int]] = field(default_factory=list)
    path: list[str] = field(default_factory=list)              # node/link ids
    born_generation: int = 0


@dataclass
class InFlight:
    token: Token
    link_id: str
    arrive_step: int
    base_step: int
    base_delay: int


@dataclass
class Receipt:
    request_id: str
    branch: str
    service: str
    step: int
    tenant: str
    marker: str
    first: bool
    token_id: int


@dataclass
class DropRecord:
    request_id: str
    branch: str
    token_id: int
    step: int
    location: str
    reason: str
    detail: str


@dataclass
class BarrierWitness:
    barrier_id: str
    proxy: str
    generation: int
    insert_step: int
    check_step: int
    snapshot: list[str]          # token ids at insertion that could reach proxy
    terminal: list[str]          # token ids terminal by check time
    outstanding: list[str]       # still live at check time
    completed: bool
    fate: list[dict]             # per-token fate evidence


@dataclass
class ViolationRecord:
    code: str
    step: int
    event_id: str
    detail: str
    request_id: Optional[str] = None
    prefix: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Matching helpers
# --------------------------------------------------------------------------- #
def _match(pattern: str, value: str) -> bool:
    return pattern == WILDCARD or pattern == value


def _rule_matches(rule: tuple[str, str, str, str], values: tuple[str, str, str, str]) -> bool:
    return all(_match(p, v) for p, v in zip(rule, values))


# --------------------------------------------------------------------------- #
# Simulator
# --------------------------------------------------------------------------- #
class Simulator:
    def __init__(self, case: Case, config: SimConfig):
        self.case = case
        self.cfg = config
        self.step = 0

        # runtime control state per proxy
        self.inbound: dict[str, set[int]] = {
            p: set(vs) for p, vs in case.initial_inbound.items()
        }
        self.outbound: dict[str, set[int]] = {
            p: {v} for p, v in case.initial_outbound.items()
        }
        self.paused: dict[str, bool] = {}
        self.pause_since: dict[str, int] = {}
        self.min_open_gen: dict[str, int] = {}

        # live token storage
        self.tokens: dict[int, Token] = {}
        self.inflight: list[InFlight] = []
        self.blocked: list[InFlight] = []          # held by down domain
        self.hold_queues: dict[str, list[Token]] = {}  # paused proxies
        self.next_token_id = 1
        self.next_event_seq = 1
        self.generation = 0

        # outputs
        self.event_log: list[tuple[int, int, str, str]] = []  # step, seq, eid, kind
        self.receipts: list[Receipt] = []
        self.drops: list[DropRecord] = []
        self.anomalies: list[dict] = []
        self.violations: list[ViolationRecord] = []
        self.barriers: dict[str, BarrierWitness] = {}
        self.action_results: list[dict] = []
        self.visited_steps: set[int] = set()
        self.max_live_tokens = 0

        # internal control events: (step, seq, kind, payload)
        self._ctrl: list[tuple[int, int, str, dict]] = []
        self._actions_by_step: dict[int, list[tuple[str, Optional[int]]]] = {}
        for st, aid, wait in config.schedule:
            self._actions_by_step.setdefault(st, []).append((aid, wait))
        for lst in self._actions_by_step.values():
            lst.sort(key=lambda x: x[0])
        self._emitted: set[str] = set()
        self._receipt_keys: set[tuple[str, str]] = set()
        self._barrier_pending: dict[str, tuple[int, str, int]] = {}  # bid -> (check, proxy, gen)
        self._withdrawn_versions: dict[str, set[int]] = {}

    # --------------------------------------------------------------- utils --
    def _eid(self, kind: str) -> str:
        seq = self.next_event_seq
        self.next_event_seq += 1
        return f"{kind}{seq:04d}"

    def _log(self, step: int, eid: str, kind: str) -> None:
        self.event_log.append((step, int(eid[1:]), eid, kind))

    def _domain_down(self, domain: str) -> bool:
        if self.cfg.failure_domain is None or domain != self.cfg.failure_domain:
            return False
        assert self.cfg.down_step is not None and self.cfg.up_step is not None
        return self.cfg.down_step <= self.step < self.cfg.up_step

    def _eligible_links(self, node: str, target: str) -> list[Link]:
        out: list[Link] = []
        for link in self.case.out_links.get(node, []):
            if WILDCARD in link.carries or target in link.carries:
                out.append(link)
        return out

    def _can_reach(self, start_node: str, proxy: str, target: str,
                   seen: Optional[set[str]] = None) -> bool:
        """Forward reachability over target-eligible links (iterative DFS)."""
        if start_node == proxy:
            return True
        visited: set[str] = set()
        stack = [start_node]
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            for link in self.case.out_links.get(node, []):
                if WILDCARD in link.carries or target in link.carries:
                    if link.dst == proxy:
                        return True
                    if link.dst not in visited:
                        stack.append(link.dst)
        return False

    def _live_witness_tokens(self, proxy: str) -> list[Token]:
        """All live tokens that could still arrive at *proxy*.

        A live token lives in exactly one of: in-flight link queue, blocked
        (down-domain) link queue, or a paused-proxy hold queue.
        """
        positions: list[tuple[str, Token]] = []
        for inf in self.inflight:
            positions.append((self.case.links[inf.link_id].dst, inf.token))
        for inf in self.blocked:
            positions.append((self.case.links[inf.link_id].dst, inf.token))
        for node, queue in self.hold_queues.items():
            for tok in queue:
                positions.append((node, tok))
        found: list[Token] = []
        seen_ids: set[int] = set()
        for node, tok in positions:
            if tok.token_id in seen_ids:
                continue
            if self._can_reach(node, proxy, tok.target):
                seen_ids.add(tok.token_id)
                found.append(tok)
        return sorted(found, key=lambda t: t.token_id)

    def _token_position(self, token_id: int) -> str:
        for inf in self.inflight:
            if inf.token.token_id == token_id:
                return f"link:{inf.link_id}"
        for inf in self.blocked:
            if inf.token.token_id == token_id:
                return f"blocked-link:{inf.link_id}"
        for node, queue in self.hold_queues.items():
            for tok in queue:
                if tok.token_id == token_id:
                    return f"hold:{node}"
        return "unknown"

    def _record_drop(self, token: Token, location: str, reason: str, detail: str) -> None:
        self.drops.append(DropRecord(
            token.request_id, token.branch, token.token_id, self.step,
            location, reason, detail,
        ))
        del self.tokens[token.token_id]

    # ------------------------------------------------------------- inbound --
    def _emit_request(self, req: Request) -> None:
        if req.id in self._emitted:
            return
        self._emitted.add(req.id)
        eid = self._eid("E")
        self._log(self.step, eid, "emit")
        gen = self.generation if req.force_gen is None else req.force_gen
        token = Token(
            token_id=self.next_token_id,
            request_id=req.id,
            branch=f"{req.id}#root",
            identity=req.identity,
            tenant=req.tenant,
            marker=req.marker,
            target=req.target,
            emit_step=req.emit_step,
            expiry_step=req.emit_step + req.ttl,
            born_generation=gen,
            path=[req.ingress],
        )
        self.next_token_id += 1
        self.tokens[token.token_id] = token
        self._forward(token, req.ingress, eid)

    # ----------------------------------------------------------- forwarding --
    def _forward(self, token: Token, from_node: str, parent_eid: str) -> None:
        links = self._eligible_links(from_node, token.target)
        if not links:
            if token.token_id in self.tokens:
                self._record_drop(token, from_node, Reason.NO_ELIGIBLE_LINK,
                                  "no registered link carries the target service")
            return
        for link in links:
            self._send_on_link(token, link, parent_eid)

    def _send_on_link(self, token: Token, link: Link, parent_eid: str) -> None:
        delay = self.cfg.delays[link.id]
        arrive = self.step + delay
        branch = f"{token.branch}>{link.id}"
        child = Token(
            token_id=self.next_token_id,
            request_id=token.request_id,
            branch=branch,
            identity=token.identity,
            tenant=token.tenant,
            marker=token.marker,
            target=token.target,
            emit_step=token.emit_step,
            expiry_step=token.expiry_step,
            gen_stamps=dict(token.gen_stamps),
            provenance=list(token.provenance),
            inbound_versions=list(token.inbound_versions),
            path=token.path + [link.id],
            born_generation=token.born_generation,
        )
        self.next_token_id += 1
        self.tokens[child.token_id] = child
        inf = InFlight(child, link.id, arrive, self.step, delay)
        if self._domain_down(link.failure_domain):
            self.blocked.append(inf)
            return
        if arrive >= child.expiry_step:
            # Will die in flight; still scheduled so the expiry is witnessed.
            pass
        self.inflight.append(inf)

    # ------------------------------------------------------------- arrivals --
    def _process_arrival(self, inf: InFlight) -> None:
        token = inf.token
        link = self.case.links[inf.link_id]
        eid = self._eid("A")
        self._log(self.step, eid, "arrival")
        token.path.append(link.dst)

        if self.step >= token.expiry_step:
            self._record_drop(token, link.dst, Reason.TTL_EXPIRED,
                              f"expiry_step={token.expiry_step}")
            return

        node = self.case.nodes[link.dst]
        if node.kind == "service":
            self._deliver_to_service(token, node.id, eid)
            return

        # proxy node
        proxy_id = node.id
        if self.paused.get(proxy_id, False):
            self.hold_queues.setdefault(proxy_id, []).append(token)
            self.anomalies.append({
                "step": self.step, "event_id": eid, "request_id": token.request_id,
                "proxy": proxy_id, "reason": Reason.PAUSED_QUEUED,
            })
            return

        # generation staleness check: applies both to tokens this proxy stamped
        # on an earlier visit and to fresh tokens carrying an explicit (closed)
        # birth generation, so a closed generation cannot be re-injected.
        floor = self.min_open_gen.get(proxy_id, 0)
        stamped = token.gen_stamps.get(proxy_id)
        effective_gen = stamped if stamped is not None else token.born_generation
        if effective_gen < floor:
            self.violations.append(ViolationRecord(
                Violation.GENERATION_REINJECTION, self.step, eid,
                f"token generation {effective_gen} below min open generation "
                f"{floor} at {proxy_id}",
                token.request_id,
            ))
            self._record_drop(token, proxy_id, Reason.STALE_GENERATION,
                              f"gen={effective_gen} min_open={floor}")
            return

        accepted, used_version = self._inbound_check(token, proxy_id)
        if not accepted:
            reason = Reason.INBOUND_DENIED if used_version is not None else Reason.INBOUND_NO_MATCH
            self._record_drop(token, proxy_id, reason,
                              "no installed inbound version admits the token")
            return

        new_marker = self._outbound_map(token, proxy_id)
        if new_marker is None:
            self._record_drop(token, proxy_id, Reason.OUTBOUND_NO_MAPPING,
                              "active outbound version has no matching mapping")
            return

        token.marker = new_marker
        token.provenance.append(proxy_id)
        if used_version is not None:
            token.inbound_versions.append((proxy_id, used_version))
        self.generation = self.generation  # generation only moves on barriers
        token.gen_stamps[proxy_id] = self.generation
        self._forward(token, proxy_id, eid)

    def _inbound_check(self, token: Token, proxy_id: str) -> tuple[bool, Optional[int]]:
        values = (token.identity, token.tenant, token.marker, token.target)

        def specificity(rule: tuple[str, ...]) -> int:
            # concrete constraints outrank "*" fallbacks, regardless of order
            return sum(1 for p in rule[:4] if p != WILDCARD)

        saw_deny = False
        accept_version: Optional[int] = None
        for version in sorted(self.inbound.get(proxy_id, ())):
            ver = self.case.inbound_versions[(proxy_id, version)]
            best: Optional[tuple[tuple[str, str, str, str, str], int]] = None
            for rule in ver.rules:
                if _rule_matches(rule[:4], values):
                    score = specificity(rule)
                    if best is None or score > best[1]:
                        best = (rule, score)
            if best is None:
                continue
            if best[0][4] == "deny":
                saw_deny = True
            elif accept_version is None:
                accept_version = version
        if saw_deny or accept_version is None:
            return False, accept_version
        return True, accept_version

    def _outbound_map(self, token: Token, proxy_id: str) -> Optional[str]:
        active = sorted(self.outbound.get(proxy_id, set()), reverse=True)
        values = (token.marker, token.identity, token.tenant, token.target)

        def specificity(mapping: tuple[str, ...]) -> int:
            return sum(1 for p in mapping[:4] if p != WILDCARD)

        for version in active:
            ver = self.case.outbound_versions[(proxy_id, version)]
            best: Optional[tuple[tuple[str, str, str, str, str], int]] = None
            for mapping in ver.mappings:
                if _rule_matches(mapping[:4], values):
                    score = specificity(mapping)
                    if best is None or score > best[1]:
                        best = (mapping, score)
            if best is not None:
                return best[0][4]
        return None

    def _deliver_to_service(self, token: Token, service_id: str, eid: str) -> None:
        service = self.case.services[service_id]
        req = self.case.requests[token.request_id]

        if token.tenant != service.tenant:
            v = ViolationRecord(
                Violation.ISOLATION, self.step, eid,
                f"original tenant '{token.tenant}' delivered to service of tenant "
                f"'{service.tenant}' ({service_id})",
                token.request_id,
            )
            self.violations.append(v)
            self._record_drop(token, service_id, Reason.ISOLATION_VIOLATION, v.detail)
            return

        if service_id not in req.allow_nodes:
            v = ViolationRecord(
                Violation.ALLOWLIST, self.step, eid,
                f"service {service_id} not in request allow list", token.request_id,
            )
            self.violations.append(v)
            self._record_drop(token, service_id, Reason.ALLOWLIST_VIOLATION, v.detail)
            return

        key = (token.request_id, token.branch)
        if self.step > req.deadline_step:
            self.violations.append(ViolationRecord(
                Violation.LATE, self.step, eid,
                f"delivery at step {self.step} after deadline {req.deadline_step}",
                token.request_id,
            ))
            self.anomalies.append({
                "step": self.step, "event_id": eid, "request_id": token.request_id,
                "service": service_id, "branch": token.branch,
                "reason": Reason.LATE_DELIVERY,
            })
            self._record_drop(token, service_id, Reason.LATE_DELIVERY, "deadline missed")
            return

        if key in self._receipt_keys:
            self.violations.append(ViolationRecord(
                Violation.DUPLICATE, self.step, eid,
                f"duplicate delivery on branch {token.branch}", token.request_id,
            ))
            self.anomalies.append({
                "step": self.step, "event_id": eid, "request_id": token.request_id,
                "service": service_id, "branch": token.branch,
                "reason": Reason.DUPLICATE_DELIVERY,
            })
            self.receipts.append(Receipt(
                token.request_id, token.branch, service_id, self.step,
                token.tenant, token.marker, first=False, token_id=token.token_id,
            ))
            self._record_drop(token, service_id, Reason.DUPLICATE_DELIVERY,
                              "receipt already exists on this branch")
            return

        self._receipt_keys.add(key)
        self.receipts.append(Receipt(
            token.request_id, token.branch, service_id, self.step,
            token.tenant, token.marker, first=True, token_id=token.token_id,
        ))
        del self.tokens[token.token_id]

    # -------------------------------------------------------------- control --
    def _apply_action(self, action_id: str, wait_slots: Optional[int]) -> None:
        action = self.case.actions[action_id]
        eid = self._eid("C")
        self._log(self.step, eid, f"action:{action.type}")
        result = {"step": self.step, "event_id": eid, "action": action_id,
                  "type": action.type, "proxy": action.proxy, "ok": True}
        proxy = action.proxy

        if action.type == "install_inbound":
            assert proxy is not None and action.version is not None
            versions = self.inbound.setdefault(proxy, set())
            if action.version in versions:
                result["ok"] = False
                result["reason"] = "already_installed"
            else:
                versions.add(action.version)
                result["installed_version"] = action.version

        elif action.type == "switch_outbound":
            assert proxy is not None and action.version is not None
            self.outbound.setdefault(proxy, set()).add(action.version)
            # the switched-to version becomes the sole active mapping
            self.outbound[proxy] = {action.version}
            result["active_version"] = action.version

        elif action.type == "withdraw_inbound":
            assert proxy is not None and action.version is not None
            # A real drain proof means no token that could still reach this
            # proxy remains live; the barrier witness set is exactly that set.
            relevant = self._live_witness_tokens(proxy)
            if relevant:
                detail = (f"withdraw of {proxy}/v{action.version} lacks drain proof: "
                          f"{len(relevant)} reachable token(s) still live")
                self.violations.append(ViolationRecord(
                    Violation.WITHDRAW_NO_PROOF, self.step, eid, detail,
                ))
                result["ok"] = False
                result["reason"] = Reason.WITHDRAW_UNDRAINED
                result["witness_tokens"] = [t.token_id for t in relevant]
            else:
                self.inbound.get(proxy, set()).discard(action.version)
                self._withdrawn_versions.setdefault(proxy, set()).add(action.version)
                result["withdrawn_version"] = action.version
                # closing the generation prevents reinjection of the old cohort
                self.generation += 1
                self.min_open_gen[proxy] = max(
                    self.min_open_gen.get(proxy, 0), self.generation)
                result["generation"] = self.generation

        elif action.type == "barrier":
            assert proxy is not None and action.barrier_id is not None
            self.generation += 1
            gen = self.generation
            wait = wait_slots if wait_slots is not None else 1
            check_step = self.step + wait
            witnesses = self._live_witness_tokens(proxy)
            bw = BarrierWitness(
                barrier_id=action.barrier_id, proxy=proxy, generation=gen,
                insert_step=self.step, check_step=check_step,
                snapshot=[t.token_id for t in witnesses],
                terminal=[], outstanding=[], completed=False, fate=[],
            )
            self.barriers[action.barrier_id] = bw
            self._barrier_pending[action.barrier_id] = (check_step, proxy, gen)
            result["barrier_id"] = action.barrier_id
            result["generation"] = gen
            result["check_step"] = check_step
            result["witness_count"] = len(witnesses)

        elif action.type == "pause":
            assert proxy is not None
            if self.paused.get(proxy, False):
                result["ok"] = False
                result["reason"] = "already_paused"
            else:
                self.paused[proxy] = True
                self.pause_since[proxy] = self.step
        elif action.type == "resume":
            assert proxy is not None
            if not self.paused.get(proxy, False):
                result["ok"] = False
                result["reason"] = "not_paused"
            else:
                self.paused[proxy] = False
                queued = self.hold_queues.pop(proxy, [])
                result["released_tokens"] = len(queued)
                for tok in sorted(queued, key=lambda t: t.token_id):
                    if self.step >= tok.expiry_step:
                        self._record_drop(tok, proxy, Reason.TTL_EXPIRED,
                                          f"expired while paused at {proxy}")
                        continue
                    # re-enter inbound validation now that the proxy is back
                    floor = self.min_open_gen.get(proxy, 0)
                    stamped = tok.gen_stamps.get(proxy)
                    effective_gen = stamped if stamped is not None else tok.born_generation
                    if effective_gen < floor:
                        self.violations.append(ViolationRecord(
                            Violation.GENERATION_REINJECTION, self.step, eid,
                            f"queued token generation {effective_gen} below floor {floor}",
                            tok.request_id,
                        ))
                        self._record_drop(tok, proxy, Reason.STALE_GENERATION,
                                          f"gen={effective_gen} floor={floor}")
                        continue
                    accepted, used_version = self._inbound_check(tok, proxy)
                    if not accepted:
                        reason = (Reason.INBOUND_DENIED if used_version is not None
                                  else Reason.INBOUND_NO_MATCH)
                        self._record_drop(tok, proxy, reason, "denied on resume")
                        continue
                    new_marker = self._outbound_map(tok, proxy)
                    if new_marker is None:
                        self._record_drop(tok, proxy, Reason.OUTBOUND_NO_MAPPING,
                                          "no mapping on resume")
                        continue
                    tok.marker = new_marker
                    tok.provenance.append(proxy)
                    if used_version is not None:
                        tok.inbound_versions.append((proxy, used_version))
                    tok.gen_stamps[proxy] = self.generation
                    self._forward(tok, proxy, eid)
        self.action_results.append(result)

    def _check_barrier(self, barrier_id: str) -> None:
        check_step, proxy, gen = self._barrier_pending.pop(barrier_id)
        bw = self.barriers[barrier_id]
        eid = self._eid("B")
        self._log(self.step, eid, "barrier_check")
        live_ids = {t.token_id for t in self._live_witness_tokens(proxy)}
        for tid in bw.snapshot:
            if tid in live_ids:
                bw.outstanding.append(tid)
            else:
                bw.terminal.append(tid)
            bw.fate.append({
                "token_id": tid,
                "status": "outstanding" if tid in live_ids else "terminal",
                "position": (self._token_position(tid) if tid in live_ids
                             else self._fate_of(tid)),
            })
        bw.completed = not bw.outstanding
        if not bw.completed:
            self.violations.append(ViolationRecord(
                Violation.DRAIN_INCOMPLETE, self.step, eid,
                f"barrier {barrier_id} on {proxy} incomplete: "
                f"{len(bw.outstanding)} witness token(s) still live",
            ))
        else:
            # generation closes only after a real drain proof; floors are
            # monotonic so a later barrier can never reopen an old generation
            self.min_open_gen[proxy] = max(self.min_open_gen.get(proxy, 0), gen)

    def _fate_of(self, token_id: int) -> str:
        for drop in self.drops:
            if drop.token_id == token_id:
                return f"dropped:{drop.reason}"
        for receipt in self.receipts:
            if receipt.token_id == token_id:
                return f"delivered:{receipt.service}"
        return "delivered_or_expired"

    # ------------------------------------------------------- failure edges --
    def _on_domain_down(self) -> None:
        dom = self.cfg.failure_domain
        blocked_links = {lid for lid, link in self.case.links.items()
                         if link.failure_domain == dom}
        remaining = []
        for inf in self.inflight:
            if inf.link_id in blocked_links:
                self.blocked.append(inf)
            else:
                remaining.append(inf)
        self.inflight = remaining

    def _on_domain_up(self) -> None:
        dom = self.cfg.failure_domain
        assert dom is not None
        blocked_links = {lid for lid, link in self.case.links.items()
                         if link.failure_domain == dom}
        still_blocked = []
        for inf in self.blocked:
            if inf.link_id not in blocked_links:  # pragma: no cover - defensive
                still_blocked.append(inf)
                continue
            token = inf.token
            if self.step >= token.expiry_step:
                self.drops.append(DropRecord(
                    token.request_id, token.branch, token.token_id, self.step,
                    f"link:{inf.link_id}", Reason.TTL_EXPIRED_RECOVERY,
                    f"TTL expired during outage; recovery at step {self.step}",
                ))
                del self.tokens[token.token_id]
                continue
            scaled = self.cfg.recovery_delay if self.cfg.recovery_delay is not None \
                else self.case.links[inf.link_id].delay_min
            residual = max(1, scaled)
            inf.arrive_step = self.step + residual
            inf.base_step = self.step
            inf.base_delay = residual
            self.inflight.append(inf)
        self.blocked = still_blocked

    # --------------------------------------------------------------- expiry --
    def _sweep_expired(self) -> None:
        keep = []
        for inf in self.inflight:
            if self.step >= inf.token.expiry_step:
                self._record_drop(
                    inf.token, f"link:{inf.link_id}", Reason.TTL_EXPIRED,
                    f"expiry_step={inf.token.expiry_step} while in flight",
                )
            else:
                keep.append(inf)
        self.inflight = keep
        for node, queue in list(self.hold_queues.items()):
            alive: list[Token] = []
            for tok in sorted(queue, key=lambda t: t.token_id):
                if self.step >= tok.expiry_step:
                    self._record_drop(tok, node, Reason.TTL_EXPIRED,
                                      f"expiry_step={tok.expiry_step} while paused")
                else:
                    alive.append(tok)
            if alive:
                self.hold_queues[node] = alive
            else:
                self.hold_queues.pop(node, None)

    # ----------------------------------------------------------------- run --
    def run(self) -> dict:
        horizon = self.cfg.horizon
        while self.step <= horizon:
            self.visited_steps.add(self.step)

            # 1. control actions scheduled at this step
            for aid, wait in self._actions_by_step.get(self.step, []):
                self._apply_action(aid, wait)

            # 2. failure-window boundaries
            if (self.cfg.failure_domain is not None
                    and self.cfg.down_step == self.step):
                self._on_domain_down()
            if (self.cfg.failure_domain is not None
                    and self.cfg.up_step == self.step):
                self._on_domain_up()

            # 3. barrier checks due at this step
            due = [bid for bid, (cs, _p, _g) in self._barrier_pending.items()
                   if cs <= self.step]
            for bid in sorted(due, key=lambda b: self._barrier_pending[b][0]):
                self._check_barrier(bid)

            # 4. request emissions at this step
            for req in sorted(self.case.requests.values(), key=lambda r: r.id):
                if req.emit_step == self.step:
                    self._emit_request(req)

            # 5. arrivals scheduled at or before this step
            arrived = [inf for inf in self.inflight if inf.arrive_step <= self.step]
            arrived.sort(key=lambda i: (i.arrive_step, i.token.token_id, i.link_id))
            self.inflight = [inf for inf in self.inflight if inf.arrive_step > self.step]
            for inf in arrived:
                if inf.token.token_id not in self.tokens:
                    continue  # token already expired/swept
                self._process_arrival(inf)

            # 6. TTL expiry sweep
            self._sweep_expired()
            self.max_live_tokens = max(
                self.max_live_tokens,
                len(self.tokens),
            )

            # 7. decide the next non-idle step (event-driven compression)
            nxt = self._next_step(horizon)
            if nxt is None:
                break
            self.step = nxt

        # horizon closure: reachability and final barrier accounting
        self._finalize(horizon)
        return self.result()

    def _next_step(self, horizon: int) -> Optional[int]:
        candidates: list[int] = []
        for st in self._actions_by_step:
            if st > self.step:
                candidates.append(st)
        if self.cfg.failure_domain is not None:
            if self.cfg.down_step is not None and self.cfg.down_step > self.step:
                candidates.append(self.cfg.down_step)
            if self.cfg.up_step is not None and self.cfg.up_step > self.step:
                candidates.append(self.cfg.up_step)
        for _bid, (cs, _p, _g) in self._barrier_pending.items():
            if cs > self.step:
                candidates.append(cs)
        for req in self.case.requests.values():
            if req.emit_step > self.step and req.id not in self._emitted:
                candidates.append(req.emit_step)
        for inf in self.inflight:
            if inf.arrive_step > self.step:
                candidates.append(inf.arrive_step)
        # expiry steps bound how far idle skipping may jump
        for tok in list(self.tokens.values()):
            if tok.expiry_step > self.step:
                candidates.append(tok.expiry_step)
        candidates = [c for c in candidates if c > self.step and c <= horizon]
        return min(candidates) if candidates else None

    def _finalize(self, horizon: int) -> None:
        # any barrier still pending at horizon is an incomplete drain
        for bid, (cs, proxy, gen) in list(self._barrier_pending.items()):
            bw = self.barriers[bid]
            live = {t.token_id for t in self._live_witness_tokens(proxy)}
            for tid in bw.snapshot:
                if tid in live:
                    bw.outstanding.append(tid)
                else:
                    bw.terminal.append(tid)
            bw.completed = not bw.outstanding
            if not bw.completed and not any(
                    v.code == Violation.DRAIN_INCOMPLETE and bid in v.detail
                    for v in self.violations):
                self.violations.append(ViolationRecord(
                    Violation.DRAIN_INCOMPLETE, min(horizon, cs), "",
                    f"barrier {bid} on {proxy} incomplete at horizon",
                ))

        # reachability: every approved request needs >=1 first-hand receipt in time
        delivered = {r.request_id for r in self.receipts if r.first}
        for req in self.case.requests.values():
            if req.id not in delivered:
                eid = self._eid("R")
                self._log(horizon, eid, "reachability")
                self.violations.append(ViolationRecord(
                    Violation.REACHABILITY, horizon, eid,
                    f"request {req.id} has no legal delivery by deadline "
                    f"{req.deadline_step}", req.id,
                ))

        # attach the earliest-violation prefix once all violations are known
        self._attach_prefixes()

    def _attach_prefixes(self) -> None:
        if not self.violations:
            return
        ordered = sorted(self.event_log, key=lambda e: (e[0], e[1]))
        eids = [e[2] for e in ordered]
        for v in self.violations:
            if not v.event_id:
                v.prefix = list(eids)
                continue
            try:
                idx = eids.index(v.event_id)
            except ValueError:
                v.prefix = list(eids)
                continue
            v.prefix = eids[: idx + 1]

    # ---------------------------------------------------------------- output --
    def result(self) -> dict:
        safe = not self.violations
        return {
            "scenario": self.cfg.name,
            "status": "safe" if safe else "violation",
            "horizon": self.cfg.horizon,
            "steps_visited": len(self.visited_steps),
            "max_live_tokens": self.max_live_tokens,
            "actions": self.action_results,
            "barriers": [self._barrier_dict(b) for b in sorted(
                self.barriers.values(), key=lambda b: b.insert_step)],
            "receipts": [
                {
                    "request_id": r.request_id, "branch": r.branch,
                    "service": r.service, "step": r.step,
                    "tenant": r.tenant, "marker": r.marker, "first": r.first,
                    "token_id": r.token_id,
                } for r in sorted(self.receipts, key=lambda r: (r.request_id, r.step, r.branch))
            ],
            "drops": [
                {
                    "request_id": d.request_id, "branch": d.branch,
                    "token_id": d.token_id, "step": d.step,
                    "location": d.location, "reason": d.reason, "detail": d.detail,
                } for d in sorted(self.drops, key=lambda d: (d.step, d.token_id))
            ],
            "anomalies": sorted(self.anomalies, key=lambda a: (a["step"], a["event_id"])),
            "violations": [
                {
                    "code": v.code, "step": v.step, "event_id": v.event_id,
                    "detail": v.detail, "request_id": v.request_id,
                    "prefix": v.prefix,
                } for v in sorted(self.violations, key=lambda v: (v.step, v.event_id, v.code))
            ],
            "timeline": [
                {"step": s, "event_id": eid, "kind": kind}
                for s, _seq, eid, kind in sorted(self.event_log, key=lambda e: (e[0], e[1]))
            ],
            "compression": {
                "method": "event-driven skip over idle steps; only steps carrying an "
                          "emission, arrival, control action, failure edge or expiry are "
                          "visited; live in-flight state is keyed by (token_id, link, "
                          "arrive_step, expiry_step) and dead tokens are pruned immediately",
                "horizon_steps": self.cfg.horizon,
                "visited_steps": len(self.visited_steps),
                "skipped_steps": self.cfg.horizon - len(self.visited_steps),
            },
        }

    @staticmethod
    def _barrier_dict(bw: BarrierWitness) -> dict:
        return {
            "barrier_id": bw.barrier_id,
            "proxy": bw.proxy,
            "generation": bw.generation,
            "insert_step": bw.insert_step,
            "check_step": bw.check_step,
            "completed": bw.completed,
            "witness_tokens": bw.snapshot,
            "terminal_tokens": bw.terminal,
            "outstanding_tokens": bw.outstanding,
            "fate": bw.fate,
        }
