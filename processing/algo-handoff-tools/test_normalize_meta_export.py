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

    def test_valid_export_cannot_replace_manual_labels_or_trimmed_meta(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = root / "meta_task-d_all.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                for name, content in _bundle_files("task-d", "task-d_1").items():
                    zf.writestr("meta_task-d_1/" + name, content)
            target = root / "unpacked" / "meta_task-d_1"
            target.mkdir(parents=True)
            frames = target / "frames.jsonl"
            frames.write_text('{"ts": 42, "map_name": "B10_map"}\n', encoding="utf-8")
            original = frames.read_bytes(), frames.stat()
            (target / "trim.json").write_text('{"start": 42}', encoding="utf-8")

            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                normalize_meta_export.normalize_archive(archive, root / "unpacked", "task-d")

            self.assertEqual((frames.read_bytes(), frames.stat()), original)
            self.assertEqual((target / "trim.json").read_text(), '{"start": 42}')
            self.assertEqual(sorted(p.name for p in target.iterdir()), ["frames.jsonl", "trim.json"])
            self.assertEqual(list((root / "unpacked").iterdir()), [target])

    def test_collision_is_checked_before_publishing_other_subtasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = root / "meta_task-e_all.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                for index in (1, 2):
                    subtask = "task-e_%d" % index
                    for name, content in _bundle_files("task-e", subtask).items():
                        zf.writestr("meta_%s/%s" % (subtask, name), content)
            existing = root / "unpacked" / "meta_task-e_2"
            existing.mkdir(parents=True)
            (existing / "frames.jsonl").write_text('{"map_name":"P_map"}\n')

            with self.assertRaises(FileExistsError):
                normalize_meta_export.normalize_archive(archive, root / "unpacked", "task-e")

            self.assertFalse((root / "unpacked" / "meta_task-e_1").exists())
            self.assertEqual((existing / "frames.jsonl").read_text(), '{"map_name":"P_map"}\n')


if __name__ == "__main__":
    unittest.main()
