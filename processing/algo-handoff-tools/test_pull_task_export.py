from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("pull-task-export.py")
SPEC = importlib.util.spec_from_file_location("pull_task_export", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
pull_task_export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pull_task_export)


class AuthenticationTest(unittest.TestCase):
    def test_headers_require_api_key_from_environment(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "2"):
                pull_task_export._headers()

    def test_headers_use_api_key_from_environment(self) -> None:
        with mock.patch.dict(os.environ, {"SKDOS_API_KEY": "local-secret"}, clear=True):
            headers = pull_task_export._headers()

        self.assertEqual(headers["X-API-Key"], "local-secret")

    def test_pull_dry_run_does_not_require_api_key(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            result = pull_task_export.main([
                "--task", "example-task",
                "--out", "/tmp/example",
                "--dry-run",
            ])

        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
