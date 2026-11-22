"""Offline network-security control-plane model (gray-release tenant isolation).

Modules:
    errors  - structured validation / runtime error codes
    model   - typed case model and strict input validation
    engine  - deterministic discrete-event token simulator
    replay  - replay command (fixed timeline, witnesses, earliest violation)
    planner - plan command (safe fixed release table across all scenarios)
    cli     - command line entry point and JSON envelope
"""

__all__ = ["errors", "model", "engine", "replay", "planner", "cli"]
__version__ = "1.0.0"
