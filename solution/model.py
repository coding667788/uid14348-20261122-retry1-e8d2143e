"""Typed case model and strict JSON validation with field-path error codes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ErrorCode, ValidationError

# Hard limits required by the task statement.
MAX_PROXIES = 14
MAX_ACTIONS = 16
MAX_SLOTS = 10
MAX_TTL = 6
WILDCARD = "*"


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Node:
    id: str
    kind: str  # "ingress" | "proxy" | "service"
    region: str


@dataclass(frozen=True)
class Service:
    id: str
    tenant: str


@dataclass(frozen=True)
class Link:
    id: str
    src: str
    dst: str
    delay_min: int
    delay_max: int
    failure_domain: str
    carries: tuple[str, ...]  # service ids or ("*",)


@dataclass(frozen=True)
class InboundVersion:
    proxy: str
    version: int
    # ordered (identity, tenant, marker, target, effect); "*" is wildcard
    rules: tuple[tuple[str, str, str, str, str], ...]


@dataclass(frozen=True)
class OutboundVersion:
    proxy: str
    version: int
    # ordered (marker, identity, tenant, target, set_marker)
    mappings: tuple[tuple[str, str, str, str, str], ...]


@dataclass(frozen=True)
class InitialInbound:
    proxy: str
    versions: tuple[int, ...]


@dataclass(frozen=True)
class InitialOutbound:
    proxy: str
    active_version: int


@dataclass(frozen=True)
class Request:
    id: str
    identity: str
    tenant: str
    marker: str
    ingress: str
    target: str
    emit_step: int
    ttl: int
    deadline_step: int
    allow_nodes: tuple[str, ...]
    force_gen: int | None = None


@dataclass(frozen=True)
class Action:
    id: str
    type: str  # install_inbound | switch_outbound | withdraw_inbound
    #          # barrier | pause | resume
    proxy: str | None = None
    version: int | None = None
    barrier_id: str | None = None
    duration: int | None = None  # pause only, replay convenience


@dataclass(frozen=True)
class ReplayEvent:
    step: int
    action: str
    wait_slots: int | None = None


@dataclass(frozen=True)
class FailureWindow:
    domain: str
    down_step: int
    up_step: int


@dataclass(frozen=True)
class BaselineEntry:
    slot: int
    action: str
    wait_slots: int | None = None


@dataclass
class Case:
    case_id: str
    nodes: dict[str, Node]
    identities: tuple[str, ...]
    services: dict[str, Service]
    links: dict[str, Link]
    inbound_versions: dict[tuple[str, int], InboundVersion]
    outbound_versions: dict[tuple[str, int], OutboundVersion]
    initial_inbound: dict[str, list[int]]
    initial_outbound: dict[str, int]
    requests: dict[str, Request]
    actions: dict[str, Action]
    precedence: tuple[tuple[str, str], ...]
    replay_events: tuple[ReplayEvent, ...]
    replay_horizon: int
    failure: FailureWindow | None
    plan_slots: int | None
    plan_horizon: int | None
    max_wait_slots: int | None
    baseline: tuple[BaselineEntry, ...]
    failure_domains: tuple[str, ...]

    # derived index helpers --------------------------------------------------
    def build_indexes(self) -> None:
        index: dict[str, list[Link]] = {}
        for link in self.links.values():
            index.setdefault(link.src, []).append(link)
        for lst in index.values():
            lst.sort(key=lambda l: l.id)
        # object.__setattr__ keeps the frozen-ish dataclass usable
        object.__setattr__(self, "_out_links", index)

    @property
    def out_links(self) -> dict[str, list[Link]]:
        return getattr(self, "_out_links", {})


# --------------------------------------------------------------------------- #
# Validation primitives
# --------------------------------------------------------------------------- #
def _err(code: str, message: str, path: str) -> ValidationError:
    return ValidationError(code, message, path)


def _as(value: Any, py_type: type | tuple[type, ...], name: str, path: str):
    if not isinstance(value, py_type):
        want = (
            py_type.__name__
            if isinstance(py_type, type)
            else "|".join(t.__name__ for t in py_type)
        )
        raise _err(ErrorCode.WRONG_TYPE, f"{name} must be {want}", path)
    return value


def _req_field(obj: dict, key: str, path: str) -> Any:
    if key not in obj:
        raise _err(ErrorCode.MISSING_FIELD, f"missing field '{key}'", path)
    return obj[key]


def _int_in(value: Any, lo: int, hi: int, name: str, path: str) -> int:
    _as(value, int, name, path)
    if isinstance(value, bool) or not (lo <= value <= hi):
        raise _err(
            ErrorCode.ILLEGAL_VALUE,
            f"{name} must be an integer in [{lo}, {hi}]",
            path,
        )
    return value


def _nonempty_str(value: Any, name: str, path: str) -> str:
    _as(value, str, name, path)
    if not value:
        raise _err(ErrorCode.ILLEGAL_VALUE, f"{name} must be non-empty", path)
    return value


# --------------------------------------------------------------------------- #
# Section parsers
# --------------------------------------------------------------------------- #
def _parse_nodes(raw: Any) -> dict[str, Node]:
    path = "$.nodes"
    _as(raw, list, "nodes", path)
    nodes: dict[str, Node] = {}
    for i, item in enumerate(raw):
        p = f"{path}[{i}]"
        _as(item, dict, "node entry", p)
        nid = _nonempty_str(_req_field(item, "id", p), "id", f"{p}.id")
        if nid in nodes:
            raise _err(ErrorCode.DUPLICATE_ID, f"duplicate node id '{nid}'", f"{p}.id")
        kind = _nonempty_str(_req_field(item, "kind", p), "kind", f"{p}.kind")
        if kind not in ("ingress", "proxy", "service"):
            raise _err(
                ErrorCode.ILLEGAL_VALUE,
                "kind must be ingress, proxy or service",
                f"{p}.kind",
            )
        region = _nonempty_str(
            item.get("region", "default"), "region", f"{p}.region"
        )
        nodes[nid] = Node(nid, kind, region)
    if not nodes:
        raise _err(ErrorCode.CARDINALITY_VIOLATION, "at least one node required", path)
    proxy_count = sum(1 for n in nodes.values() if n.kind == "proxy")
    if proxy_count > MAX_PROXIES:
        raise _err(
            ErrorCode.CARDINALITY_VIOLATION,
            f"at most {MAX_PROXIES} proxies allowed, found {proxy_count}",
            path,
        )
    return nodes


def _parse_identities(raw: Any) -> tuple[str, ...]:
    path = "$.identities"
    _as(raw, list, "identities", path)
    out: list[str] = []
    for i, item in enumerate(raw):
        p = f"{path}[{i}]"
        sid = _nonempty_str(item, "identity", p)
        if sid in out:
            raise _err(ErrorCode.DUPLICATE_ID, f"duplicate identity '{sid}'", p)
        out.append(sid)
    if not out:
        raise _err(ErrorCode.CARDINALITY_VIOLATION, "at least one identity required", path)
    return tuple(out)


def _parse_services(raw: Any, nodes: dict[str, Node]) -> dict[str, Service]:
    path = "$.services"
    _as(raw, list, "services", path)
    services: dict[str, Service] = {}
    for i, item in enumerate(raw):
        p = f"{path}[{i}]"
        _as(item, dict, "service entry", p)
        sid = _nonempty_str(_req_field(item, "id", p), "id", f"{p}.id")
        if sid in services:
            raise _err(ErrorCode.DUPLICATE_ID, f"duplicate service id '{sid}'", f"{p}.id")
        if sid not in nodes:
            raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown node '{sid}'", f"{p}.id")
        if nodes[sid].kind != "service":
            raise _err(
                ErrorCode.SEMANTIC_CONFLICT,
                f"node '{sid}' is not of kind service",
                f"{p}.id",
            )
        tenant = _nonempty_str(_req_field(item, "tenant", p), "tenant", f"{p}.tenant")
        services[sid] = Service(sid, tenant)
    return services


def _parse_links(
    raw: Any, nodes: dict[str, Node], services: dict[str, Service]
) -> (dict[str, Link]):
    path = "$.links"
    _as(raw, list, "links", path)
    links: dict[str, Link] = {}
    domains: set[str] = set()
    for i, item in enumerate(raw):
        p = f"{path}[{i}]"
        _as(item, dict, "link entry", p)
        lid = _nonempty_str(_req_field(item, "id", p), "id", f"{p}.id")
        if lid in links:
            raise _err(ErrorCode.DUPLICATE_ID, f"duplicate link id '{lid}'", f"{p}.id")
        src = _nonempty_str(_req_field(item, "src", p), "src", f"{p}.src")
        dst = _nonempty_str(_req_field(item, "dst", p), "dst", f"{p}.dst")
        for ref, sub in ((src, "src"), (dst, "dst")):
            if ref not in nodes:
                raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown node '{ref}'", f"{p}.{sub}")
        if nodes[src].kind == "service":
            raise _err(ErrorCode.SEMANTIC_CONFLICT, "service nodes cannot be link sources", f"{p}.src")
        if nodes[dst].kind == "ingress":
            raise _err(ErrorCode.SEMANTIC_CONFLICT, "ingress nodes cannot be link destinations", f"{p}.dst")
        dmin = _int_in(_req_field(item, "delay_min", p), 1, 10_000, "delay_min", f"{p}.delay_min")
        dmax = _int_in(_req_field(item, "delay_max", p), 1, 10_000, "delay_max", f"{p}.delay_max")
        if dmin > dmax:
            raise _err(ErrorCode.ILLEGAL_VALUE, "delay_min must be <= delay_max", f"{p}.delay_min")
        domain = _nonempty_str(
            _req_field(item, "failure_domain", p), "failure_domain", f"{p}.failure_domain"
        )
        domains.add(domain)
        carries_raw = item.get("carries", [WILDCARD])
        _as(carries_raw, list, "carries", f"{p}.carries")
        carries: list[str] = []
        for j, svc in enumerate(carries_raw):
            cp = f"{p}.carries[{j}]"
            svc = _nonempty_str(svc, "carries entry", cp)
            if svc != WILDCARD:
                if svc not in services:
                    raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown service '{svc}'", cp)
            if svc not in carries:
                carries.append(svc)
        if not carries:
            raise _err(ErrorCode.CARDINALITY_VIOLATION, "carries must not be empty", f"{p}.carries")
        links[lid] = Link(lid, src, dst, dmin, dmax, domain, tuple(carries))
    return links, tuple(sorted(domains))


def _check_rule_tuple(item: Any, p: str, fields: tuple[str, ...]) -> tuple[str, ...]:
    _as(item, dict, "rule entry", p)
    out: list[str] = []
    for f in fields:
        val = _req_field(item, f, p)
        out.append(_nonempty_str(val, f, f"{p}.{f}"))
    return tuple(out)


def _parse_policies(
    raw: Any, nodes: dict[str, Node], services: dict[str, Service], identities: tuple[str, ...]
):
    path = "$.policies"
    _as(raw, dict, "policies", path)
    inbound: dict[tuple[str, int], InboundVersion] = {}
    outbound: dict[tuple[str, int], OutboundVersion] = {}

    ib_raw = _req_field(raw, "inbound", path)
    _as(ib_raw, list, "inbound", f"{path}.inbound")
    for i, ver in enumerate(ib_raw):
        p = f"{path}.inbound[{i}]"
        _as(ver, dict, "inbound version", p)
        proxy = _nonempty_str(_req_field(ver, "proxy", p), "proxy", f"{p}.proxy")
        if proxy not in nodes or nodes[proxy].kind != "proxy":
            raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown proxy '{proxy}'", f"{p}.proxy")
        version = _int_in(_req_field(ver, "version", p), 1, 10_000, "version", f"{p}.version")
        key = (proxy, version)
        if key in inbound:
            raise _err(ErrorCode.DUPLICATE_ID, f"duplicate inbound version {proxy}/v{version}", p)
        rules_raw = _req_field(ver, "rules", p)
        _as(rules_raw, list, "rules", f"{p}.rules")
        rules: list[tuple[str, str, str, str, str]] = []
        for j, r in enumerate(rules_raw):
            rp = f"{p}.rules[{j}]"
            identity, tenant, marker, target, effect = _check_rule_tuple(
                r, rp, ("identity", "tenant", "marker", "target", "effect")
            )
            if identity != WILDCARD and identity not in identities:
                raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown identity '{identity}'", f"{rp}.identity")
            if target != WILDCARD and target not in services:
                raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown service '{target}'", f"{rp}.target")
            if effect not in ("accept", "deny"):
                raise _err(ErrorCode.ILLEGAL_VALUE, "effect must be accept or deny", f"{rp}.effect")
            rules.append((identity, tenant, marker, target, effect))
        if not rules:
            raise _err(ErrorCode.CARDINALITY_VIOLATION, "rules must not be empty", f"{p}.rules")
        inbound[key] = InboundVersion(proxy, version, tuple(rules))

    ob_raw = _req_field(raw, "outbound", path)
    _as(ob_raw, list, "outbound", f"{path}.outbound")
    for i, ver in enumerate(ob_raw):
        p = f"{path}.outbound[{i}]"
        _as(ver, dict, "outbound version", p)
        proxy = _nonempty_str(_req_field(ver, "proxy", p), "proxy", f"{p}.proxy")
        if proxy not in nodes or nodes[proxy].kind != "proxy":
            raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown proxy '{proxy}'", f"{p}.proxy")
        version = _int_in(_req_field(ver, "version", p), 1, 10_000, "version", f"{p}.version")
        key = (proxy, version)
        if key in outbound:
            raise _err(ErrorCode.DUPLICATE_ID, f"duplicate outbound version {proxy}/v{version}", p)
        maps_raw = _req_field(ver, "mappings", p)
        _as(maps_raw, list, "mappings", f"{p}.mappings")
        mappings: list[tuple[str, str, str, str, str]] = []
        for j, m in enumerate(maps_raw):
            mp = f"{p}.mappings[{j}]"
            marker, identity, tenant, target, set_marker = _check_rule_tuple(
                m, mp, ("marker", "identity", "tenant", "target", "set_marker")
            )
            if identity != WILDCARD and identity not in identities:
                raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown identity '{identity}'", f"{mp}.identity")
            if target != WILDCARD and target not in services:
                raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown service '{target}'", f"{mp}.target")
            mappings.append((marker, identity, tenant, target, set_marker))
        if not mappings:
            raise _err(ErrorCode.CARDINALITY_VIOLATION, "mappings must not be empty", f"{p}.mappings")
        outbound[key] = OutboundVersion(proxy, version, tuple(mappings))
    return inbound, outbound


def _parse_initial(
    raw: Any,
    nodes: dict[str, Node],
    inbound: dict[tuple[str, int], InboundVersion],
    outbound: dict[tuple[str, int], OutboundVersion],
):
    path = "$.initial_state"
    _as(raw, dict, "initial_state", path)
    init_in: dict[str, list[int]] = {}
    ib_raw = _req_field(raw, "inbound", path)
    _as(ib_raw, list, "inbound", f"{path}.inbound")
    for i, item in enumerate(ib_raw):
        p = f"{path}.inbound[{i}]"
        _as(item, dict, "initial inbound entry", p)
        proxy = _nonempty_str(_req_field(item, "proxy", p), "proxy", f"{p}.proxy")
        if proxy not in nodes or nodes[proxy].kind != "proxy":
            raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown proxy '{proxy}'", f"{p}.proxy")
        if proxy in init_in:
            raise _err(ErrorCode.DUPLICATE_ID, f"duplicate initial inbound for '{proxy}'", p)
        versions = _req_field(item, "versions", p)
        _as(versions, list, "versions", f"{p}.versions")
        vs: list[int] = []
        for j, v in enumerate(versions):
            vp = f"{p}.versions[{j}]"
            v = _int_in(v, 1, 10_000, "version", vp)
            if (proxy, v) not in inbound:
                raise _err(ErrorCode.UNKNOWN_REFERENCE, f"no inbound policy {proxy}/v{v}", vp)
            if v in vs:
                raise _err(ErrorCode.DUPLICATE_ID, f"duplicate version {v}", vp)
            vs.append(v)
        if not vs:
            raise _err(ErrorCode.CARDINALITY_VIOLATION, "versions must not be empty", p)
        init_in[proxy] = sorted(vs)

    init_out: dict[str, int] = {}
    ob_raw = _req_field(raw, "outbound", path)
    _as(ob_raw, list, "outbound", f"{path}.outbound")
    for i, item in enumerate(ob_raw):
        p = f"{path}.outbound[{i}]"
        _as(item, dict, "initial outbound entry", p)
        proxy = _nonempty_str(_req_field(item, "proxy", p), "proxy", f"{p}.proxy")
        if proxy not in nodes or nodes[proxy].kind != "proxy":
            raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown proxy '{proxy}'", f"{p}.proxy")
        if proxy in init_out:
            raise _err(ErrorCode.DUPLICATE_ID, f"duplicate initial outbound for '{proxy}'", p)
        version = _int_in(
            _req_field(item, "active_version", p), 1, 10_000, "active_version", f"{p}.active_version"
        )
        if (proxy, version) not in outbound:
            raise _err(ErrorCode.UNKNOWN_REFERENCE, f"no outbound policy {proxy}/v{version}", f"{p}.active_version")
        init_out[proxy] = version
    return init_in, init_out


def _parse_requests(
    raw: Any,
    nodes: dict[str, Node],
    identities: tuple[str, ...],
    services: dict[str, Service],
) -> dict[str, Request]:
    path = "$.requests"
    _as(raw, list, "requests", path)
    requests: dict[str, Request] = {}
    for i, item in enumerate(raw):
        p = f"{path}[{i}]"
        _as(item, dict, "request entry", p)
        rid = _nonempty_str(_req_field(item, "id", p), "id", f"{p}.id")
        if rid in requests:
            raise _err(ErrorCode.DUPLICATE_ID, f"duplicate request id '{rid}'", f"{p}.id")
        identity = _nonempty_str(_req_field(item, "identity", p), "identity", f"{p}.identity")
        if identity not in identities:
            raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown identity '{identity}'", f"{p}.identity")
        tenant = _nonempty_str(_req_field(item, "tenant", p), "tenant", f"{p}.tenant")
        marker = _nonempty_str(_req_field(item, "marker", p), "marker", f"{p}.marker")
        ingress = _nonempty_str(_req_field(item, "ingress", p), "ingress", f"{p}.ingress")
        if ingress not in nodes or nodes[ingress].kind != "ingress":
            raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown ingress node '{ingress}'", f"{p}.ingress")
        target = _nonempty_str(_req_field(item, "target", p), "target", f"{p}.target")
        if target not in services:
            raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown service '{target}'", f"{p}.target")
        emit = _int_in(_req_field(item, "emit_step", p), 0, 10_000, "emit_step", f"{p}.emit_step")
        ttl = _int_in(_req_field(item, "ttl", p), 1, MAX_TTL, "ttl", f"{p}.ttl")
        deadline = _int_in(
            _req_field(item, "deadline_step", p), 0, 10_000, "deadline_step", f"{p}.deadline_step"
        )
        if deadline < emit:
            raise _err(ErrorCode.ILLEGAL_VALUE, "deadline_step must be >= emit_step", f"{p}.deadline_step")
        allow_raw = _req_field(item, "allow_nodes", p)
        _as(allow_raw, list, "allow_nodes", f"{p}.allow_nodes")
        allow: list[str] = []
        for j, n in enumerate(allow_raw):
            ap = f"{p}.allow_nodes[{j}]"
            n = _nonempty_str(n, "allow node", ap)
            if n not in nodes:
                raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown node '{n}'", ap)
            if n not in allow:
                allow.append(n)
        if not allow:
            raise _err(ErrorCode.CARDINALITY_VIOLATION, "allow_nodes must not be empty", f"{p}.allow_nodes")
        force_gen = None
        if "force_gen" in item and item["force_gen"] is not None:
            force_gen = _int_in(item["force_gen"], 0, 10_000, "force_gen", f"{p}.force_gen")
        requests[rid] = Request(
            rid, identity, tenant, marker, ingress, target, emit, ttl, deadline,
            tuple(allow), force_gen,
        )
    return requests


_ACTION_TYPES = {
    "install_inbound", "switch_outbound", "withdraw_inbound",
    "barrier", "pause", "resume",
}


def _parse_actions(raw: Any, nodes: dict[str, Node],
                   inbound: dict[tuple[str, int], InboundVersion],
                   outbound: dict[tuple[str, int], OutboundVersion]) -> dict[str, Action]:
    path = "$.actions"
    _as(raw, list, "actions", path)
    if len(raw) > MAX_ACTIONS:
        raise _err(ErrorCode.CARDINALITY_VIOLATION, f"at most {MAX_ACTIONS} actions", path)
    actions: dict[str, Action] = {}
    barrier_ids: set[str] = set()
    for i, item in enumerate(raw):
        p = f"{path}[{i}]"
        _as(item, dict, "action entry", p)
        aid = _nonempty_str(_req_field(item, "id", p), "id", f"{p}.id")
        if aid in actions:
            raise _err(ErrorCode.DUPLICATE_ID, f"duplicate action id '{aid}'", f"{p}.id")
        atype = _nonempty_str(_req_field(item, "type", p), "type", f"{p}.type")
        if atype not in _ACTION_TYPES:
            raise _err(ErrorCode.ILLEGAL_VALUE, f"unknown action type '{atype}'", f"{p}.type")
        proxy = None
        if "proxy" in item and item["proxy"] is not None:
            proxy = _nonempty_str(item["proxy"], "proxy", f"{p}.proxy")
            if proxy not in nodes or nodes[proxy].kind != "proxy":
                raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown proxy '{proxy}'", f"{p}.proxy")
        version = None
        if "version" in item and item["version"] is not None:
            version = _int_in(item["version"], 1, 10_000, "version", f"{p}.version")
        barrier_id = None
        if "barrier_id" in item and item["barrier_id"] is not None:
            barrier_id = _nonempty_str(item["barrier_id"], "barrier_id", f"{p}.barrier_id")
        duration = None
        if "duration" in item and item["duration"] is not None:
            duration = _int_in(item["duration"], 1, 10_000, "duration", f"{p}.duration")

        if atype in ("install_inbound", "withdraw_inbound"):
            if proxy is None or version is None:
                raise _err(ErrorCode.MISSING_FIELD, f"{atype} needs proxy and version", p)
            if (proxy, version) not in inbound:
                raise _err(ErrorCode.UNKNOWN_REFERENCE, f"no inbound policy {proxy}/v{version}", p)
        if atype == "switch_outbound":
            if proxy is None or version is None:
                raise _err(ErrorCode.MISSING_FIELD, "switch_outbound needs proxy and version", p)
            if (proxy, version) not in outbound:
                raise _err(ErrorCode.UNKNOWN_REFERENCE, f"no outbound policy {proxy}/v{version}", p)
        if atype == "barrier":
            if proxy is None:
                raise _err(ErrorCode.MISSING_FIELD, "barrier needs proxy", p)
            if barrier_id is None:
                raise _err(ErrorCode.MISSING_FIELD, "barrier needs barrier_id", p)
            if barrier_id in barrier_ids:
                raise _err(ErrorCode.DUPLICATE_ID, f"duplicate barrier_id '{barrier_id}'", f"{p}.barrier_id")
            barrier_ids.add(barrier_id)
        if atype in ("pause", "resume") and proxy is None:
            raise _err(ErrorCode.MISSING_FIELD, f"{atype} needs proxy", p)
        actions[aid] = Action(aid, atype, proxy, version, barrier_id, duration)
    return actions


def _parse_precedence(raw: Any, actions: dict[str, Action]) -> tuple[tuple[str, str], ...]:
    path = "$.precedence"
    _as(raw, list, "precedence", path)
    out: list[tuple[str, str]] = []
    for i, pair in enumerate(raw):
        p = f"{path}[{i}]"
        _as(pair, list, "precedence pair", p)
        if len(pair) != 2:
            raise _err(ErrorCode.CARDINALITY_VIOLATION, "precedence pair must have 2 entries", p)
        a = _nonempty_str(pair[0], "first action", f"{p}[0]")
        b = _nonempty_str(pair[1], "second action", f"{p}[1]")
        for ref, sub in ((a, 0), (b, 1)):
            if ref not in actions:
                raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown action '{ref}'", f"{p}[{sub}]")
        if a == b:
            raise _err(ErrorCode.SEMANTIC_CONFLICT, "action cannot precede itself", p)
        out.append((a, b))
    # cycle detection
    adj: dict[str, set[str]] = {}
    for a, b in out:
        adj.setdefault(a, set()).add(b)
    color: dict[str, int] = {}

    def visit(node: str) -> None:
        color[node] = 1
        for nxt in adj.get(node, ()):
            if color.get(nxt, 0) == 1:
                raise _err(ErrorCode.SEMANTIC_CONFLICT, "precedence cycle detected", path)
            if color.get(nxt, 0) == 0:
                visit(nxt)
        color[node] = 2

    for node in adj:
        if color.get(node, 0) == 0:
            visit(node)
    return tuple(out)


def _parse_replay(raw: Any, actions: dict[str, Action], domains: tuple[str, ...]):
    path = "$.replay"
    _as(raw, dict, "replay", path)
    horizon = _int_in(_req_field(raw, "horizon", path), 1, 10_000, "horizon", f"{path}.horizon")
    events_raw = _req_field(raw, "events", path)
    _as(events_raw, list, "events", f"{path}.events")
    events: list[ReplayEvent] = []
    for i, item in enumerate(events_raw):
        p = f"{path}.events[{i}]"
        _as(item, dict, "replay event", p)
        step = _int_in(_req_field(item, "step", p), 0, 10_000, "step", f"{p}.step")
        if step > horizon:
            raise _err(ErrorCode.ILLEGAL_VALUE, "event step beyond horizon", f"{p}.step")
        act = _nonempty_str(_req_field(item, "action", p), "action", f"{p}.action")
        if act not in actions:
            raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown action '{act}'", f"{p}.action")
        wait = None
        if "wait_slots" in item and item["wait_slots"] is not None:
            wait = _int_in(item["wait_slots"], 1, 10_000, "wait_slots", f"{p}.wait_slots")
        events.append(ReplayEvent(step, act, wait))
    failure = None
    if "failure" in raw and raw["failure"] is not None:
        f = raw["failure"]
        fp = f"{path}.failure"
        _as(f, dict, "failure", fp)
        domain = _nonempty_str(_req_field(f, "domain", fp), "domain", f"{fp}.domain")
        if domain not in domains:
            raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown failure domain '{domain}'", f"{fp}.domain")
        down = _int_in(_req_field(f, "down_step", fp), 0, 10_000, "down_step", f"{fp}.down_step")
        up = _int_in(_req_field(f, "up_step", fp), 0, 10_000, "up_step", f"{fp}.up_step")
        if up <= down:
            raise _err(ErrorCode.ILLEGAL_VALUE, "up_step must be > down_step", f"{fp}.up_step")
        failure = FailureWindow(domain, down, up)
    return tuple(events), horizon, failure


def _parse_plan(raw: Any, actions: dict[str, Action], domains: tuple[str, ...]):
    path = "$.plan"
    _as(raw, dict, "plan", path)
    slots = _int_in(_req_field(raw, "slots", path), 1, MAX_SLOTS, "slots", f"{path}.slots")
    horizon = _int_in(_req_field(raw, "horizon", path), 1, 10_000, "horizon", f"{path}.horizon")
    max_wait = _int_in(_req_field(raw, "max_wait_slots", path), 0, 10_000,
                       "max_wait_slots", f"{path}.max_wait_slots")
    failure = None
    if "failure" in raw and raw["failure"] is not None:
        f = raw["failure"]
        fp = f"{path}.failure"
        _as(f, dict, "failure", fp)
        domain = _nonempty_str(_req_field(f, "domain", fp), "domain", f"{fp}.domain")
        if domain not in domains:
            raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown failure domain '{domain}'", f"{fp}.domain")
        down = _int_in(_req_field(f, "down_step", fp), 0, horizon, "down_step", f"{fp}.down_step")
        up = _int_in(_req_field(f, "up_step", fp), 0, horizon + 10_000, "up_step", f"{fp}.up_step")
        if up <= down:
            raise _err(ErrorCode.ILLEGAL_VALUE, "up_step must be > down_step", f"{fp}.up_step")
        failure = FailureWindow(domain, down, up)
    return slots, horizon, max_wait, failure


def _parse_baseline(raw: Any, actions: dict[str, Action]) -> tuple[BaselineEntry, ...]:
    path = "$.baseline"
    _as(raw, list, "baseline", path)
    out: list[BaselineEntry] = []
    seen_steps: dict[int, str] = {}
    for i, item in enumerate(raw):
        p = f"{path}[{i}]"
        _as(item, dict, "baseline entry", p)
        slot = _int_in(_req_field(item, "slot", p), 0, 10_000, "slot", f"{p}.slot")
        act = _nonempty_str(_req_field(item, "action", p), "action", f"{p}.action")
        if act not in actions:
            raise _err(ErrorCode.UNKNOWN_REFERENCE, f"unknown action '{act}'", f"{p}.action")
        wait = None
        if "wait_slots" in item and item["wait_slots"] is not None:
            wait = _int_in(item["wait_slots"], 1, 10_000, "wait_slots", f"{p}.wait_slots")
        if slot in seen_steps:
            raise _err(
                ErrorCode.SEMANTIC_CONFLICT,
                f"slot {slot} already used by action '{seen_steps[slot]}'",
                f"{p}.slot",
            )
        seen_steps[slot] = act
        out.append(BaselineEntry(slot, act, wait))
    return tuple(out)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def load_case(raw: Any) -> Case:
    if not isinstance(raw, dict):
        raise _err(ErrorCode.NOT_AN_OBJECT, "top-level JSON value must be an object", "$")
    case_id = ""
    if "case_id" in raw:
        case_id = _nonempty_str(raw["case_id"], "case_id", "$.case_id")

    nodes = _parse_nodes(_req_field(raw, "nodes", "$"))
    identities = _parse_identities(_req_field(raw, "identities", "$"))
    services = _parse_services(_req_field(raw, "services", "$"), nodes)
    links_raw = _req_field(raw, "links", "$")
    links, domains = _parse_links(links_raw, nodes, services)
    inbound, outbound = _parse_policies(
        _req_field(raw, "policies", "$"), nodes, services, identities
    )
    init_in, init_out = _parse_initial(
        _req_field(raw, "initial_state", "$"), nodes, inbound, outbound
    )
    # every proxy needs an initial active outbound mapping
    for node in nodes.values():
        if node.kind == "proxy" and node.id not in init_out:
            raise _err(
                ErrorCode.MISSING_FIELD,
                f"proxy '{node.id}' lacks initial outbound active_version",
                "$.initial_state.outbound",
            )
    requests = _parse_requests(
        _req_field(raw, "requests", "$"), nodes, identities, services
    )
    actions = _parse_actions(_req_field(raw, "actions", "$"), nodes, inbound, outbound)
    precedence = _parse_precedence(_req_field(raw, "precedence", "$"), actions)

    replay_events: tuple[ReplayEvent, ...] = ()
    replay_horizon = 0
    failure = None
    if "replay" in raw and raw["replay"] is not None:
        replay_events, replay_horizon, failure = _parse_replay(
            raw["replay"], actions, domains
        )

    plan_slots = plan_horizon = max_wait = None
    plan_failure = None
    if "plan" in raw and raw["plan"] is not None:
        plan_slots, plan_horizon, max_wait, plan_failure = _parse_plan(
            raw["plan"], actions, domains
        )
        if failure is None:
            failure = plan_failure

    baseline: tuple[BaselineEntry, ...] = ()
    if "baseline" in raw and raw["baseline"] is not None:
        baseline = _parse_baseline(raw["baseline"], actions)

    case = Case(
        case_id=case_id,
        nodes=nodes,
        identities=identities,
        services=services,
        links=links,
        inbound_versions=inbound,
        outbound_versions=outbound,
        initial_inbound=init_in,
        initial_outbound=init_out,
        requests=requests,
        actions=actions,
        precedence=precedence,
        replay_events=replay_events,
        replay_horizon=replay_horizon,
        failure=failure,
        plan_slots=plan_slots,
        plan_horizon=plan_horizon,
        max_wait_slots=max_wait,
        baseline=baseline,
        failure_domains=domains,
    )
    case.build_indexes()
    return case
