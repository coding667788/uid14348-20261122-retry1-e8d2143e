"""End-to-end CLI behavior: envelope, exit codes, deterministic UTF-8 output."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import EXAMPLES, ROOT


class CliTests(unittest.TestCase):
    def _run(self, *args):
        proc = subprocess.run(
            [sys.executable, "-m", "solution", *args],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        )
        return proc.returncode, json.loads(proc.stdout), proc.stderr

    def test_validate_envelope(self):
        code, payload, _ = self._run("validate", str(EXAMPLES / "01_safe_gray.json"))
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ok")

    def test_replay_envelope_is_deterministic(self):
        path = str(EXAMPLES / "02_leak_window.json")
        _, first, _ = self._run("replay", path)
        _, second, _ = self._run("replay", path)
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )
        self.assertIn("earliest_violation", first)

    def test_missing_file_error_envelope(self):
        code, payload, _ = self._run("validate", "does-not-exist.json")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "FILE_NOT_FOUND")
        self.assertIn("path", payload["error"])

    def test_malformed_json_reports_parse_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text("{not valid json", encoding="utf-8")
            code, payload, _ = self._run("validate", str(p))
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "JSON_PARSE_ERROR")

    def test_structural_error_includes_field_path(self):
        import copy
        good = json.loads((EXAMPLES / "01_safe_gray.json").read_text(encoding="utf-8"))
        good["requests"][0]["ttl"] = 99
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text(json.dumps(good), encoding="utf-8")
            code, payload, _ = self._run("validate", str(p))
        self.assertEqual(code, 2)
        self.assertIn("ttl", payload["error"]["path"])

    def test_unknown_command(self):
        code, payload, _ = self._run("frobnicate", str(EXAMPLES / "01_safe_gray.json"))
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "UNKNOWN_COMMAND")


if __name__ == "__main__":
    unittest.main()
