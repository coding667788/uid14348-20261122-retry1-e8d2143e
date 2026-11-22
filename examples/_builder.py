"""Synthetic topology builders shared by examples and tests.

Everything here is fictional: nodes, identities, tenants and markers carry no
payloads, only abstract identity labels.  The builders return plain dicts that
serialise directly to the documented case JSON.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Optional


def case(case_id: str, *, ttl: int = 6, horizon: int = 12,
         slots: int = 10, plan_horizon: int = 14, max_wait: int = 4) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "nodes": [
            {"id": "IG1", "kind": "ingress", "region": "r1"},
            {"id": "P1", "kind": "proxy", "region": "r1"},
            {"id": "P2", "kind": "proxy", "region": "r2"},
            {"id": "S1", "kind": "service", "region": "r1"},
            {"id": "S2", "kind": "service", "region": "r2"},
        ],
        "identities": ["svc-a", "svc-b"],
        "services": [
            {"id": "S1", "tenant": "ta"},
            {"id": "S2", "tenant": "tb"},
        ],
        "links": [
            {"id": "L0", "src": "IG1", "dst": "P1", "delay_min": 1, "delay_max": 1,
             "failure_domain": "d1", "carries": ["*"]},
            {"id": "L1", "src": "P1", "dst": "P2", "delay_min": 1, "delay_max": 2,
             "failure_domain": "d1", "carries": ["*"]},
            {"id": "L2", "src": "P2", "dst": "S1", "delay_min": 1, "delay_max": 1,
             "failure_domain": "d2", "carries": ["S1"]},
            {"id": "L3", "src": "P2", "dst": "S2", "delay_min": 1, "delay_max": 1,
             "failure_domain": "d2", "carries": ["S2"]},
        ],
        "policies": {"inbound": [], "outbound": []},
        "initial_state": {"inbound": [], "outbound": []},
        "requests": [],
        "actions": [],
        "precedence": [],
        "replay": {"horizon": horizon, "events": [], "failure": None},
        "plan": {
            "slots": slots,
            "horizon": plan_horizon,
            "max_wait_slots": max_wait,
        },
        "baseline": [],
    }


def inbound(proxy: str, version: int, rules: list[tuple[str, str, str, str, str]]) -> dict:
    return {
        "proxy": proxy,
        "version": version,
        "rules": [
            {"identity": i, "tenant": t, "marker": m, "target": s, "effect": e}
            for (i, t, m, s, e) in rules
        ],
    }


def outbound(proxy: str, version: int, mappings: list[tuple[str, str, str, str, str]]) -> dict:
    return {
        "proxy": proxy,
        "version": version,
        "mappings": [
            {"marker": m, "identity": i, "tenant": t, "target": s, "set_marker": nm}
            for (m, i, t, s, nm) in mappings
        ],
    }


def request(rid: str, *, identity: str, tenant: str, marker: str, target: str,
            emit: int = 0, ttl: int = 6, deadline: int = 10,
            ingress: str = "IG1", allow: Optional[list[str]] = None,
            force_gen: Optional[int] = None) -> dict:
    body = {
        "id": rid,
        "identity": identity,
        "tenant": tenant,
        "marker": marker,
        "ingress": ingress,
        "target": target,
        "emit_step": emit,
        "ttl": ttl,
        "deadline_step": deadline,
        "allow_nodes": allow if allow is not None else [target],
    }
    if force_gen is not None:
        body["force_gen"] = force_gen
    return body


def action(aid: str, atype: str, *, proxy: Optional[str] = None,
           version: Optional[int] = None, barrier_id: Optional[str] = None) -> dict:
    body: dict[str, Any] = {"id": aid, "type": atype}
    if proxy is not None:
        body["proxy"] = proxy
    if version is not None:
        body["version"] = version
    if barrier_id is not None:
        body["barrier_id"] = barrier_id
    return body


def standard_policies(c: dict) -> dict:
    """Linear tenant-safe chain IG1 -> P1 -> P2 -> S1/S2.

    P1 admits the raw ingress markers and rewrites them to the inter-proxy
    markers P2 validates; P2 performs the final service-facing rewrite.
    """
    c = deepcopy(c)
    c["policies"]["inbound"] = [
        inbound("P1", 1, [
            ("*", "ta", "ma", "*", "accept"),
            ("*", "tb", "mb", "*", "accept"),
            ("*", "*", "*", "*", "deny"),
        ]),
        inbound("P1", 2, [
            ("*", "ta", "ma", "*", "accept"),
            ("*", "tb", "mb", "*", "accept"),
            ("*", "*", "*", "*", "deny"),
        ]),
        inbound("P2", 1, [
            ("*", "ta", "ma-p1", "*", "accept"),
            ("*", "tb", "mb-p1", "*", "accept"),
            ("*", "*", "*", "*", "deny"),
        ]),
    ]
    c["policies"]["outbound"] = [
        outbound("P1", 1, [
            ("ma", "*", "ta", "*", "ma-p1"),
            ("mb", "*", "tb", "*", "mb-p1"),
        ]),
        outbound("P1", 2, [
            ("ma", "*", "ta", "*", "ma-p1-v2"),
            ("mb", "*", "tb", "*", "mb-p1-v2"),
        ]),
        outbound("P2", 1, [
            ("ma-p1", "*", "ta", "S1", "ma-s1"),
            ("mb-p1", "*", "tb", "S2", "mb-s2"),
        ]),
    ]
    c["initial_state"]["inbound"] = [
        {"proxy": "P1", "versions": [1]},
        {"proxy": "P2", "versions": [1]},
    ]
    c["initial_state"]["outbound"] = [
        {"proxy": "P1", "active_version": 1},
        {"proxy": "P2", "active_version": 1},
    ]
    return c


def standard_actions(c: dict) -> dict:
    c = deepcopy(c)
    c["actions"] = [
        action("a1", "install_inbound", proxy="P1", version=2),
        action("a2", "barrier", proxy="P1", barrier_id="B1"),
        action("a3", "switch_outbound", proxy="P1", version=2),
        action("a4", "withdraw_inbound", proxy="P1", version=1),
    ]
    c["precedence"] = [("a1", "a2"), ("a2", "a3"), ("a3", "a4")]
    return c


def leaky_policies(c: dict) -> dict:
    """v2 on P1 rewrites a ta marker so it can cross to tb's service.

    The steady state before and after the window is tenant-safe; only while
    P1's outbound v2 is active does a ta-origin token leave P1 carrying the
    marker that P2's cross-tenant rule accepts.  The original tenant label
    remains ``ta``, so the eventual S2 delivery is caught as an isolation
    violation at the service boundary.
    """
    c = deepcopy(c)
    c["policies"]["inbound"] = [
        inbound("P1", 1, [
            ("*", "ta", "ma", "*", "accept"),
            ("*", "*", "*", "*", "deny"),
        ]),
        inbound("P1", 2, [
            ("*", "ta", "ma", "*", "accept"),
            ("*", "*", "*", "*", "deny"),
        ]),
        inbound("P2", 1, [
            ("*", "tb", "mb", "S2", "accept"),
            # leak window: P2 temporarily trusts the rewritten ta marker
            ("*", "ta", "mb-fake", "S2", "accept"),
            ("*", "*", "*", "*", "deny"),
        ]),
    ]
    c["policies"]["outbound"] = [
        outbound("P1", 1, [("ma", "*", "ta", "S1", "ma-p1")]),
        # v2 leaks: rewrite the ta marker into the marker P2's rule accepts,
        # while the original tenant label remains ta
        outbound("P1", 2, [("ma", "*", "ta", "S2", "mb-fake")]),
        outbound("P2", 1, [
            ("mb", "*", "tb", "S2", "mb-s2"),
            ("mb-fake", "*", "ta", "S2", "mb-fake"),
        ]),
    ]
    c["initial_state"]["inbound"] = [
        {"proxy": "P1", "versions": [1]},
        {"proxy": "P2", "versions": [1]},
    ]
    c["initial_state"]["outbound"] = [
        {"proxy": "P1", "active_version": 1},
        {"proxy": "P2", "active_version": 1},
    ]
    return c
