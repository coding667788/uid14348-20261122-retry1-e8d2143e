"""Structured error codes and the validation exception type.

Every malformed input produces a deterministic envelope containing a JSON-like
field path (``$`` denotes the document root) plus a stable error code.
"""

from __future__ import annotations

from dataclasses import dataclass


class ErrorCode:
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    JSON_PARSE_ERROR = "JSON_PARSE_ERROR"
    NOT_AN_OBJECT = "NOT_AN_OBJECT"
    MISSING_FIELD = "MISSING_FIELD"
    WRONG_TYPE = "WRONG_TYPE"
    ILLEGAL_VALUE = "ILLEGAL_VALUE"
    DUPLICATE_ID = "DUPLICATE_ID"
    UNKNOWN_REFERENCE = "UNKNOWN_REFERENCE"
    CARDINALITY_VIOLATION = "CARDINALITY_VIOLATION"
    SEMANTIC_CONFLICT = "SEMANTIC_CONFLICT"
    UNKNOWN_COMMAND = "UNKNOWN_COMMAND"


@dataclass
class ValidationError(Exception):
    code: str
    message: str
    path: str = "$"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code} at {self.path}: {self.message}"

    def to_dict(self) -> dict:
        return {
            "status": "error",
            "error": {
                "code": self.code,
                "message": self.message,
                "path": self.path,
            },
        }
