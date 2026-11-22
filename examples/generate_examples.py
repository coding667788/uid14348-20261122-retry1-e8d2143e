"""Generate the six documented synthetic example cases deterministically."""

from __future__ import annotations

import json
from pathlib import Path

from _builder import (  # type: ignore
    action, case, inbound, leaky_policies, outbound, request,
    standard_actions, standard_policies,
)

OUT = Path(__file__).parent


def dump(name: str, body: dict) -> None:
    (OUT / name).write_text(
        json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# 1. safe gray release with barrier drain -----------------------------------
def ex_safe() -> dict:
    c = standard_policies(case("01_safe_gray"))
    c = standard_actions(c)
    c["requests"] = [
        request("R1", identity="svc-a", tenant="ta", marker="ma", target="S1",
                emit=0, ttl=6, deadline=14),
        request("R2", identity="svc-b", tenant="tb", marker="mb", target="S2",
                emit=0, ttl=6, deadline=14, ingress="IG1", allow=["S2"]),
    ]
    c["replay"]["events"] = [
        {"step": 1, "action": "a1"},
        {"step": 2, "action": "a2", "wait_slots": 2},
        {"step": 5, "action": "a3"},
        {"step": 8, "action": "a4"},
    ]
    c["replay"]["horizon"] = 14
    c["plan"] = {"slots": 10, "horizon": 16, "max_wait_slots": 4}
    c["baseline"] = [
        {"slot": 1, "action": "a1"},
        {"slot": 2, "action": "a2", "wait_slots": 2},
        {"slot": 5, "action": "a3"},
        {"slot": 8, "action": "a4"},
    ]
    return c


# 2. leak window between two ultimately legal versions ----------------------
def ex_leak() -> dict:
    c = leaky_policies(case("02_leak_window"))
    c["actions"] = [
        action("b1", "install_inbound", proxy="P1", version=2),
        action("b2", "switch_outbound", proxy="P1", version=2),
        action("b3", "switch_outbound", proxy="P1", version=1),
    ]
    c["precedence"] = [("b1", "b2"), ("b2", "b3")]
    c["requests"] = [
        request("R1", identity="svc-a", tenant="ta", marker="ma", target="S2",
                emit=2, ttl=5, deadline=10, allow=["S1", "S2"]),
    ]
    c["replay"]["events"] = [
        {"step": 1, "action": "b1"},
        {"step": 2, "action": "b2"},   # leak opens
        {"step": 6, "action": "b3"},   # leak closes, final state legal again
    ]
    c["replay"]["horizon"] = 12
    c["plan"] = {"slots": 8, "horizon": 14, "max_wait_slots": 3}
    c["baseline"] = [
        {"slot": 1, "action": "b1"},
        {"slot": 2, "action": "b2"},
        {"slot": 6, "action": "b3"},
    ]
    return c


# 3. ingress swap strands old markers ---------------------------------------
def ex_strand() -> dict:
    c = case("03_ingress_swap_strand")
    # v1 admits the old marker ma; v2 only admits the new marker ma2.  An
    # in-flight ma token that reaches P1 after the swap is denied by v2 and
    # has no other installed version to admit it.
    c["policies"]["inbound"] = [
        inbound("P1", 1, [
            ("*", "ta", "ma", "*", "accept"),
            ("*", "*", "*", "*", "deny"),
        ]),
        inbound("P1", 2, [
            ("*", "ta", "ma2", "*", "accept"),
            ("*", "*", "*", "*", "deny"),
        ]),
        inbound("P2", 1, [
            ("*", "ta", "ma-p1", "*", "accept"),
            ("*", "*", "*", "*", "deny"),
        ]),
    ]
    c["policies"]["outbound"] = [
        outbound("P1", 1, [("ma", "*", "ta", "*", "ma-p1")]),
        outbound("P1", 2, [("ma2", "*", "ta", "*", "ma-p1")]),
        outbound("P2", 1, [("ma-p1", "*", "ta", "S1", "ma-s1")]),
    ]
    c["initial_state"]["inbound"] = [
        {"proxy": "P1", "versions": [1]},
        {"proxy": "P2", "versions": [1]},
    ]
    c["initial_state"]["outbound"] = [
        {"proxy": "P1", "active_version": 1},
        {"proxy": "P2", "active_version": 1},
    ]
    c["actions"] = [
        action("c1", "withdraw_inbound", proxy="P1", version=1),
        action("c2", "install_inbound", proxy="P1", version=2),
    ]
    c["precedence"] = [("c1", "c2")]
    # R-late is still on the wire when the premature withdraw fires (no drain
    # proof); R-old then demonstrates the strand itself after the v2 install.
    c["requests"] = [
        request("R-late", identity="svc-a", tenant="ta", marker="ma", target="S1",
                emit=1, ttl=4, deadline=8),
        request("R-old", identity="svc-a", tenant="ta", marker="ma", target="S1",
                emit=4, ttl=3, deadline=10),
        request("R-new", identity="svc-a", tenant="ta", marker="ma2", target="S1",
                emit=6, ttl=3, deadline=11),
    ]
    c["replay"]["events"] = [
        {"step": 2, "action": "c1"},   # no drain proof: R-late still live
        {"step": 3, "action": "c2"},
    ]
    c["replay"]["horizon"] = 12
    c["plan"] = {"slots": 8, "horizon": 14, "max_wait_slots": 3}
    c["baseline"] = [
        {"slot": 2, "action": "c1"},
        {"slot": 3, "action": "c2"},
    ]
    return c


# 4. same state, different futures across barrier generations ---------------
def ex_generation() -> dict:
    c = standard_policies(case("04_generation_split"))
    c["actions"] = [
        action("d1", "barrier", proxy="P1", barrier_id="B1"),
        action("d2", "barrier", proxy="P1", barrier_id="B2"),
        action("d3", "withdraw_inbound", proxy="P1", version=1),
    ]
    c["precedence"] = [("d1", "d2"), ("d2", "d3")]
    c["requests"] = [
        request("R1", identity="svc-a", tenant="ta", marker="ma", target="S1",
                emit=0, ttl=6, deadline=10),
        request("R2", identity="svc-a", tenant="ta", marker="ma", target="S1",
                emit=7, ttl=4, deadline=12, force_gen=0),  # stale after B2
    ]
    c["replay"]["events"] = [
        {"step": 1, "action": "d1", "wait_slots": 2},
        {"step": 5, "action": "d2", "wait_slots": 2},
        {"step": 9, "action": "d3"},
    ]
    c["replay"]["horizon"] = 13
    c["plan"] = {"slots": 10, "horizon": 15, "max_wait_slots": 3}
    c["baseline"] = [
        {"slot": 1, "action": "d1", "wait_slots": 2},
        {"slot": 5, "action": "d2", "wait_slots": 2},
        {"slot": 9, "action": "d3"},
    ]
    return c


# 5. failure window ending exactly at a TTL expiry step ----------------------
def ex_ttl_failure() -> dict:
    c = standard_policies(case("05_ttl_failure_boundary"))
    c["requests"] = [
        # R1 rides d1 (IG1->P1); its TTL expires on exactly the recovery step,
        # so it must be reported as dropped-at-recovery, never as recovered.
        request("R1", identity="svc-a", tenant="ta", marker="ma", target="S1",
                emit=0, ttl=4, deadline=9),
        # R2 is emitted after P1 paused: it reaches P1 while paused, waits in
        # the hold queue, and is re-validated and released on resume.
        request("R2", identity="svc-b", tenant="tb", marker="mb", target="S2",
                emit=2, ttl=6, deadline=12),
    ]
    c["replay"]["horizon"] = 12
    c["replay"]["failure"] = {"domain": "d1", "down_step": 1, "up_step": 4}
    c["replay"]["events"] = [
        {"step": 1, "action": "e1"},   # pause P1 before R2 is emitted
        {"step": 5, "action": "e2"},   # resume while R2 is still alive
    ]
    c["plan"] = {
        "slots": 8,
        "horizon": 12,
        "max_wait_slots": 3,
        "failure": {"domain": "d1", "down_step": 1, "up_step": 4},
    }
    c["actions"] = [action("e1", "pause", proxy="P1"), action("e2", "resume", proxy="P1")]
    c["precedence"] = [("e1", "e2")]
    c["baseline"] = [{"slot": 1, "action": "e1"}, {"slot": 5, "action": "e2"}]
    return c


# 6. small topology whose per-scenario shortest schedules cannot be shared ---
def ex_conflict() -> dict:
    c = standard_policies(case("06_scenario_conflict"))
    c["actions"] = [
        action("f1", "switch_outbound", proxy="P1", version=3),
        action("f2", "withdraw_inbound", proxy="P1", version=1),
        action("f3", "pause", proxy="P1"),
        action("f4", "resume", proxy="P1"),
    ]
    # add a fresh v3 outbound mapping as the switch target
    c["policies"]["outbound"].append(
        outbound("P1", 3, [("ma", "*", "ta", "S1", "ma-p1")])
    )
    c["precedence"] = [("f1", "f2"), ("f3", "f4")]
    c["requests"] = [
        request("R1", identity="svc-a", tenant="ta", marker="ma", target="S1",
                emit=0, ttl=4, deadline=7),
        request("R2", identity="svc-a", tenant="ta", marker="ma", target="S1",
                emit=5, ttl=4, deadline=11),
    ]
    c["replay"]["events"] = [
        {"step": 1, "action": "f3"},
        {"step": 4, "action": "f4"},
        {"step": 5, "action": "f1"},
        {"step": 8, "action": "f2"},
    ]
    c["replay"]["horizon"] = 13
    c["replay"]["failure"] = {"domain": "d1", "down_step": 2, "up_step": 5}
    c["plan"] = {
        "slots": 10,
        "horizon": 14,
        "max_wait_slots": 3,
        "failure": {"domain": "d1", "down_step": 2, "up_step": 5},
    }
    c["baseline"] = [
        {"slot": 1, "action": "f3"},
        {"slot": 4, "action": "f4"},
        {"slot": 5, "action": "f1"},
        {"slot": 8, "action": "f2"},
    ]
    return c


def main() -> None:
    dump("01_safe_gray.json", ex_safe())
    dump("02_leak_window.json", ex_leak())
    dump("03_ingress_swap_strand.json", ex_strand())
    dump("04_generation_split.json", ex_generation())
    dump("05_ttl_failure_boundary.json", ex_ttl_failure())
    dump("06_scenario_conflict.json", ex_conflict())
    print("generated 6 example cases")


if __name__ == "__main__":
    main()
