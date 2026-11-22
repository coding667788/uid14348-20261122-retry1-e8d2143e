"""Shared helpers: load the generated example cases and run a replay directly."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from solution.model import load_case  # noqa: E402
from solution.replay import run_replay  # noqa: E402
from solution.engine import SimConfig, Simulator  # noqa: E402

EXAMPLES = ROOT / "examples"


def load_example(name: str):
    path = EXAMPLES / name
    document = json.loads(path.read_text(encoding="utf-8"))
    return document, load_case(document)


def replay_example(name: str) -> dict:
    document, case = load_example(name)
    return run_replay(case, document.get("replay"))


def simulate(case, config: SimConfig) -> dict:
    return Simulator(case, config).run()
