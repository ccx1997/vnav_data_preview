from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("list-task-ids.py")
SPEC = importlib.util.spec_from_file_location("list_task_ids", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
list_task_ids = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(list_task_ids)


class AuthenticationTest(unittest.TestCase):
    def test_headers_require_api_key_from_environment(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "2"):
                list_task_ids._headers()

    def test_headers_use_api_key_from_environment(self) -> None:
        with mock.patch.dict(os.environ, {"SKDOS_API_KEY": "local-secret"}, clear=True):
            headers = list_task_ids._headers()

        self.assertEqual(headers["X-API-Key"], "local-secret")

    def test_dry_run_does_not_require_api_key(self) -> None:
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(stdout):
            result = list_task_ids.main([
                "--from", "2026-08-26",
                "--to", "2026-08-26",
                "--robot", "Robot U2/V1",
                "--dry-run",
            ])

        self.assertEqual(result, 0)
        self.assertIn("robot=Robot+U2%2FV1", stdout.getvalue())


class QueryTest(unittest.TestCase):
    def test_build_ids_url_preserves_time_range_and_encodes_robot(self) -> None:
        url = list_task_ids.build_ids_url(
            "https://example.test",
            "2026-08-26 15:00",
            "2026-08-26 20:00",
            "Robot U2/V1",
        )

        parsed = urllib.parse.urlparse(url)
        self.assertEqual(parsed.path, "/api/annotation-tasks/ids")
        self.assertEqual(
            urllib.parse.parse_qs(parsed.query),
            {
                "from": ["2026-08-26 15:00"],
                "to": ["2026-08-26 20:00"],
                "robot": ["Robot U2/V1"],
            },
        )

    def test_ids_only_prints_task_ids_and_uses_environment_auth(self) -> None:
        stdout = io.StringIO()
        response = {
            "tasks": [
                {"task_id": "task-1", "robot_id": "robot-a"},
                {"task_id": "task-2", "robot_id": "robot-b"},
            ],
            "count": 2,
        }
        with mock.patch.dict(
            os.environ,
            {"SKDOS_API_KEY": "local-secret"},
            clear=True,
        ):
            with mock.patch.object(
                list_task_ids,
                "api_get_json",
                return_value=response,
            ) as get_json:
                with contextlib.redirect_stdout(stdout):
                    result = list_task_ids.main([
                        "--from", "2026-08-26",
                        "--to", "2026-08-27",
                        "--ids-only",
                    ])

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().splitlines(), ["task-1", "task-2"])
        self.assertEqual(get_json.call_args.args[1]["X-API-Key"], "local-secret")


if __name__ == "__main__":
    unittest.main()
