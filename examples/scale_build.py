"""Temporary maximal-scale case builder used by local performance checks."""

from __future__ import annotations


def build(feasible: bool = False) -> dict:
    proxies = [f"P{i}" for i in range(1, 15)]
    nodes = [{"id": "IG1", "kind": "ingress", "region": "r1"}]
    nodes += [{"id": p, "kind": "proxy", "region": "r1"} for p in proxies]
    nodes += [{"id": "S1", "kind": "service", "region": "r1"}]
    if feasible:
        # single proxy hop keeps the path inside the TTL<=6 envelope; the
        # other 13 proxies still carry full install/switch actions, exercising
        # the 14-proxy / 16-action / 10-slot limits on a feasible input
        links = [
            {"id": "L0", "src": "IG1", "dst": "P1", "delay_min": 1,
             "delay_max": 1, "failure_domain": "d1", "carries": ["*"]},
            {"id": "L1", "src": "P1", "dst": "S1", "delay_min": 1,
             "delay_max": 1, "failure_domain": "d1", "carries": ["S1"]},
        ]
    else:
        links = [{"id": "L0", "src": "IG1", "dst": "P1", "delay_min": 1,
                  "delay_max": 1, "failure_domain": "d1", "carries": ["*"]}]
        for i in range(1, 14):
            links.append({"id": f"L{i}", "src": f"P{i}", "dst": f"P{i+1}",
                          "delay_min": 1, "delay_max": 1,
                          "failure_domain": "d1", "carries": ["*"]})
        links.append({"id": "L14", "src": "P14", "dst": "S1", "delay_min": 1,
                      "delay_max": 1, "failure_domain": "d1",
                      "carries": ["S1"]})
    inbound, outbound, init_in, init_out = [], [], [], []
    for p in proxies:
        inbound.append({"proxy": p, "version": 1, "rules": [
            {"identity": "*", "tenant": "ta", "marker": "*", "target": "*",
             "effect": "accept"},
            {"identity": "*", "tenant": "*", "marker": "*", "target": "*",
             "effect": "deny"}]})
        inbound.append({"proxy": p, "version": 2, "rules": [
            {"identity": "*", "tenant": "ta", "marker": "*", "target": "*",
             "effect": "accept"},
            {"identity": "*", "tenant": "*", "marker": "*", "target": "*",
             "effect": "deny"}]})
        outbound.append({"proxy": p, "version": 1, "mappings": [
            {"marker": "*", "identity": "*", "tenant": "ta", "target": "*",
             "set_marker": "m"}]})
        outbound.append({"proxy": p, "version": 2, "mappings": [
            {"marker": "*", "identity": "*", "tenant": "ta", "target": "*",
             "set_marker": "m2"}]})
        init_in.append({"proxy": p, "versions": [1]})
        init_out.append({"proxy": p, "active_version": 1})
    actions = []
    for i, p in enumerate(proxies[:7], start=1):
        actions.append({"id": f"a{i}", "type": "install_inbound",
                        "proxy": p, "version": 2})
    for i, p in enumerate(proxies[7:14], start=8):
        actions.append({"id": f"a{i}", "type": "switch_outbound",
                        "proxy": p, "version": 2})
    actions.append({"id": "a15", "type": "pause", "proxy": "P14"})
    actions.append({"id": "a16", "type": "barrier", "proxy": "P1",
                    "barrier_id": "BIG"})
    precedence = [["a1", f"a{i}"] for i in range(2, 8)]
    precedence += [["a8", f"a{i}"] for i in range(9, 15)]
    return {
        "case_id": "scale_max", "nodes": nodes, "identities": ["svc-a"],
        "services": [{"id": "S1", "tenant": "ta"}], "links": links,
        "policies": {"inbound": inbound, "outbound": outbound},
        "initial_state": {"inbound": init_in, "outbound": init_out},
        "requests": [{"id": "R1", "identity": "svc-a", "tenant": "ta",
                      "marker": "ma", "ingress": "IG1", "target": "S1",
                      "emit_step": 0, "ttl": 6, "deadline_step": 20,
                      "allow_nodes": ["S1"]}],
        "actions": actions, "precedence": precedence,
        "replay": {"horizon": 24, "events": []},
        "plan": {"slots": 10, "horizon": 24, "max_wait_slots": 2},
        "baseline": [],
    }
