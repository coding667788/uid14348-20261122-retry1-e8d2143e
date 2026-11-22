"""Command line entry point: ``python -m solution <command> CASE.json``.

stdout always carries deterministic UTF-8 JSON.  Structural input errors are
reported with a field path and stable error code instead of a traceback.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .errors import ErrorCode, ValidationError
from .model import load_case
from .replay import run_replay

_COMMANDS = ("replay", "plan", "validate")


def _emit(payload: dict) -> None:
    sys.stdout.buffer.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    )
    sys.stdout.buffer.write(b"\n")


def _load_document(path: str) -> Any:
    p = Path(path)
    if not p.is_file():
        raise ValidationError(
            ErrorCode.FILE_NOT_FOUND, f"case file not found: {path}", "$"
        )
    raw_bytes = p.read_bytes()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(
            ErrorCode.JSON_PARSE_ERROR, f"file is not valid UTF-8: {exc}", "$"
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            ErrorCode.JSON_PARSE_ERROR,
            f"JSON parse error at line {exc.lineno} col {exc.colno}: {exc.msg}",
            f"${_json_pointer(exc)}",
        )


def _json_pointer(exc: json.JSONDecodeError) -> str:
    # The stdlib gives line/column only; report it in the message and keep
    # the pointer at document root for parse-level failures.
    return ""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] not in _COMMANDS:
        _emit({
            "status": "error",
            "error": {
                "code": ErrorCode.UNKNOWN_COMMAND,
                "message": "usage: python -m solution {replay|plan|validate} CASE.json",
                "path": "$",
            },
        })
        return 2
    command, path = argv
    try:
        document = _load_document(path)
        case = load_case(document)
        if command == "validate":
            payload = {
                "status": "ok",
                "command": "validate",
                "case_id": case.case_id,
                "summary": {
                    "nodes": len(case.nodes),
                    "proxies": sum(1 for n in case.nodes.values() if n.kind == "proxy"),
                    "links": len(case.links),
                    "requests": len(case.requests),
                    "actions": len(case.actions),
                    "failure_domains": list(case.failure_domains),
                },
            }
        elif command == "replay":
            payload = run_replay(case, document.get("replay"))
        else:
            from .planner import run_plan
            payload = run_plan(case, document.get("plan"))
    except ValidationError as exc:
        _emit(exc.to_dict())
        return 2
    except RecursionError:
        _emit({
            "status": "error",
            "error": {
                "code": ErrorCode.SEMANTIC_CONFLICT,
                "message": "topology depth exceeded (likely an unresolved cycle in link graph)",
                "path": "$.links",
            },
        })
        return 2
    _emit(payload)
    return 0
