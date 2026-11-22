"""``replay`` command: replay one concrete, fully timed scenario.

The case's ``replay.events`` give the deterministic action timeline and the
optional ``replay.failure`` gives the single failure-domain window.  Link
delays are concrete values taken from an optional ``replay.link_delays``
override; absent overrides fall back to each link's registered ``delay_min``.
"""

from __future__ import annotations

from typing import Any, Optional

from .engine import SimConfig, Simulator, Violation
from .errors import ErrorCode, ValidationError
from .model import Case


def build_replay_config(case: Case, raw: Optional[dict] = None) -> SimConfig:
    delays: dict[str, int] = {}
    overrides: dict[str, int] = {}
    if raw is not None and isinstance(raw.get("link_delays"), dict):
        overrides = raw["link_delays"]
    for lid, link in case.links.items():
        if lid in overrides:
            val = overrides[lid]
            if not isinstance(val, int) or isinstance(val, bool):
                raise ValidationError(
                    ErrorCode.WRONG_TYPE,
                    f"link delay override for {lid} must be an integer",
                    f"$.replay.link_delays.{lid}",
                )
            if not (link.delay_min <= val <= link.delay_max):
                raise ValidationError(
                    ErrorCode.ILLEGAL_VALUE,
                    f"delay {val} outside [{link.delay_min}, {link.delay_max}]",
                    f"$.replay.link_delays.{lid}",
                )
            delays[lid] = val
        else:
            delays[lid] = link.delay_min

    failure = case.failure
    recovery_delay = None
    if raw is not None and isinstance(raw.get("recovery_delay"), int):
        recovery_delay = raw["recovery_delay"]
    elif failure is not None:
        # default rescaled delay: smallest registered delay of the domain's links
        domain_links = [l for l in case.links.values() if l.failure_domain == failure.domain]
        recovery_delay = min((l.delay_min for l in domain_links), default=1)

    schedule = tuple(
        (ev.step, ev.action, ev.wait_slots) for ev in case.replay_events
    )
    return SimConfig(
        name="replay",
        delays=delays,
        failure_domain=failure.domain if failure else None,
        down_step=failure.down_step if failure else None,
        up_step=failure.up_step if failure else None,
        recovery_delay=recovery_delay,
        horizon=case.replay_horizon,
        schedule=schedule,
    )


def run_replay(case: Case, raw: Optional[dict] = None) -> dict[str, Any]:
    config = build_replay_config(case, raw)
    result = Simulator(case, config).run()

    earliest: Optional[dict] = None
    if result["violations"]:
        ranked = sorted(
            result["violations"],
            key=lambda v: (len(v["prefix"]), [int(p[1:]) for p in v["prefix"]],
                           v["code"], v["event_id"]),
        )
        first = ranked[0]
        earliest = {
            "code": first["code"],
            "step": first["step"],
            "event_id": first["event_id"],
            "detail": first["detail"],
            "request_id": first["request_id"],
            "prefix": first["prefix"],
            "tie_break_rule": "shortest prefix length, then lexicographically "
                              "smallest event-id sequence, then code, then event id",
        }

    delivered_requests = sorted({r["request_id"] for r in result["receipts"] if r["first"]})
    reachable = sorted(
        rid for rid in case.requests
        if rid in delivered_requests
        and not any(v["code"] == Violation.LATE and v["request_id"] == rid
                    for v in result["violations"])
    )
    return {
        "status": "ok",
        "command": "replay",
        "case_id": case.case_id,
        "scenario": {
            "delays": config.delays,
            "failure": (
                None if config.failure_domain is None
                else {
                    "domain": config.failure_domain,
                    "down_step": config.down_step,
                    "up_step": config.up_step,
                    "recovery_delay": config.recovery_delay,
                }
            ),
            "horizon": config.horizon,
        },
        "overall_status": result["status"],
        "earliest_violation": earliest,
        "barriers": result["barriers"],
        "receipts": result["receipts"],
        "drops": result["drops"],
        "anomalies": result["anomalies"],
        "violations": result["violations"],
        "actions": result["actions"],
        "timeline": result["timeline"],
        "reachability": {
            "reachable_requests": reachable,
            "unreachable_requests": sorted(
                rid for rid in case.requests if rid not in reachable
            ),
        },
        "compression": result["compression"],
    }
