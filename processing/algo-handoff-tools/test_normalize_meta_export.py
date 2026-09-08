from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("normalize-meta-export.py")
SPEC = importlib.util.spec_from_file_location("normalize_meta_export", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
normalize_meta_export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(normalize_meta_export)


def _bundle_files(task_id: str, sub_task_id: str) -> dict[str, str]:
    return {
        "task.json": json.dumps(
            {
                "task_id": task_id,
                "sub_task": {"sub_task_id": sub_task_id},
            }
        ),
        "export_meta.json": json.dumps(
            {"sub_task": {"sub_task_id": sub_task_id}}
        ),
        "video_segments.json": json.dumps(
            {
                "task_id": task_id,
                "sub_task_id": sub_task_id,
                "segments": [],
            }
        ),
        "frames.jsonl": "{}\n",
        "grids/1000.png": "png",
    }


class NormalizeMetaExportTest(unittest.TestCase):
    def test_root_single_subtask_is_wrapped_in_canonical_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_id = "task-a"
            sub_task_id = "task-a_1"
            archive = root / "meta_task-a_1.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                for name, content in _bundle_files(task_id, sub_task_id).items():
                    zf.writestr(name, content)

            result = normalize_meta_export.normalize_archive(
                archive, root / "unpacked", task_id
            )

            target = root / "unpacked" / "meta_task-a_1"
            self.assertEqual(result["sub_task_ids"], [sub_task_id])
            self.assertTrue(result["warnings"])
            self.assertEqual((target / "frames.jsonl").read_text(), "{}\n")
            self.assertTrue((target / "grids" / "1000.png").is_file())
            self.assertFalse((root / "unpacked" / "frames.jsonl").exists())

    def test_nested_all_zip_preserves_each_bundle_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_id = "task-b"
            archive = root / "meta_task-b_all.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                for index in (1, 2):
                    sub_task_id = "task-b_%d" % index
                    for name, content in _bundle_files(task_id, sub_task_id).items():
                        zf.writestr("meta_%s/%s" % (sub_task_id, name), content)

            result = normalize_meta_export.normalize_archive(
                archive, root / "unpacked", task_id
            )

            self.assertEqual(result["sub_task_ids"], ["task-b_1", "task-b_2"])
            self.assertEqual(result["warnings"], [])
            for index in (1, 2):
                self.assertTrue(
                    (root / "unpacked" / ("meta_task-b_%d" % index) / "frames.jsonl").is_file()
                )

    def test_inconsistent_subtask_ownership_is_rejected_without_replacing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = root / "meta_task-c_all.zip"
            files = _bundle_files("task-c", "task-c_2")
            with zipfile.ZipFile(archive, "w") as zf:
                for name, content in files.items():
                    zf.writestr("meta_task-c_1/%s" % name, content)
            target = root / "unpacked" / "meta_task-c_1"
            target.mkdir(parents=True)
            (target / "sentinel.txt").write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "ownership"):
                normalize_meta_export.normalize_archive(
                    archive, root / "unpacked", "task-c"
                )

            self.assertEqual((target / "sentinel.txt").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
