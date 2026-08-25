from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("pull-oss-videos.py")
SPEC = importlib.util.spec_from_file_location("pull_oss_videos", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
pull_oss_videos = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pull_oss_videos)


class BundleLoadingTest(unittest.TestCase):
    def test_video_segments_uses_sibling_export_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "video_segments.json").write_text(
                json.dumps({
                    "task_id": "task-a",
                    "sub_task_id": "task-a_2",
                    "segments": [{
                        "camera": "cam0",
                        "start_ts": 100.0,
                        "end_ts": 110.0,
                        "keep_windows": [{"from": 101.0, "to": 109.0}],
                    }],
                }),
                encoding="utf-8",
            )
            (root / "export_meta.json").write_text(
                json.dumps({
                    "keep_windows": [
                        {"from": 102.0, "to": 104.0},
                        {"from": 106.0, "to": 108.0},
                    ]
                }),
                encoding="utf-8",
            )
            (root / "task.json").write_text(
                json.dumps({
                    "task_id": "task-a",
                    "sub_task": {"sub_task_id": "task-a_2"},
                    "keep_windows": [{"from": 1.0, "to": 999.0}],
                }),
                encoding="utf-8",
            )

            bundle = pull_oss_videos.load_bundle(str(root / "video_segments.json"))

            self.assertEqual(bundle["keep_windows_source"], "export_meta")
            self.assertEqual(bundle["keep_windows"], [(102.0, 104.0), (106.0, 108.0)])

    def test_segment_windows_beat_unscoped_task_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            segment_window = {"from": 20.0, "to": 30.0}
            (root / "video_segments.json").write_text(
                json.dumps({
                    "task_id": "task-b",
                    "sub_task_id": "task-b_1",
                    "segments": [
                        {"camera": "cam0", "keep_windows": [segment_window]},
                        {"camera": "cam1", "keep_windows": [segment_window]},
                    ],
                }),
                encoding="utf-8",
            )
            (root / "task.json").write_text(
                json.dumps({
                    "task_id": "task-b",
                    "sub_task": {"sub_task_id": "task-b_1"},
                    "keep_windows": [{"from": 1.0, "to": 100.0}],
                }),
                encoding="utf-8",
            )

            bundle = pull_oss_videos.load_bundle(str(root / "video_segments.json"))

            self.assertEqual(bundle["keep_windows_source"], "segments")
            self.assertEqual(bundle["keep_windows"], [(20.0, 30.0)])


class AlignmentPlanningTest(unittest.TestCase):
    def test_parallel_map_runs_concurrently_and_keeps_input_order(self) -> None:
        barrier = threading.Barrier(2)

        def worker(value: int) -> int:
            barrier.wait(timeout=2)
            return value * 10

        self.assertEqual(
            pull_oss_videos._parallel_map_ordered(worker, [2, 1], jobs=2),
            [20, 10],
        )

    def test_job_budget_is_shared_by_subtasks_and_cameras(self) -> None:
        self.assertEqual(pull_oss_videos._split_job_budget(1, 4), (1, [4]))
        self.assertEqual(pull_oss_videos._split_job_budget(3, 4), (3, [2, 1, 1]))
        self.assertEqual(pull_oss_videos._split_job_budget(6, 4), (4, [1, 1, 1, 1, 1, 1]))

    def test_hardware_mode_requires_complete_timestamps_for_every_segment(self) -> None:
        valid_top_level = {"t0_hw": 1_700_000_000.0, "t1_hw": 1_700_000_002.0}
        valid_json_spec = {
            "spec": json.dumps({
                "t0_hw": 1_700_000_002.0,
                "t1_hw": 1_700_000_004.0,
                "frame_count": 30,
            })
        }
        invalid_missing_end = {"t0_hw": 1_700_000_004.0}

        self.assertEqual(
            pull_oss_videos.choose_align_mode([valid_top_level, valid_json_spec]),
            "hw_ts",
        )
        self.assertEqual(
            pull_oss_videos.choose_align_mode([valid_top_level, invalid_missing_end]),
            "wall_clock",
        )

    def test_pad_plan_covers_the_exact_keep_window(self) -> None:
        pieces, holes, content_seconds, window_seconds = pull_oss_videos.pad_pieces_for_window(
            [{"start_ts": 102.0, "end_ts": 105.0}],
            (100.0, 110.0),
            "wall_clock",
        )

        self.assertEqual([piece["type"] for piece in pieces], ["black", "content", "black"])
        self.assertEqual([hole["duration_s"] for hole in holes], [2.0, 5.0])
        self.assertEqual(content_seconds, 3.0)
        self.assertEqual(window_seconds, 10.0)
        self.assertAlmostEqual(sum(piece["duration_s"] for piece in pieces), 10.0)

    def test_ffmpeg_invocations_remain_non_interactive(self) -> None:
        with mock.patch.object(pull_oss_videos.subprocess, "run") as run:
            pull_oss_videos._run_ffmpeg(["-version"])

        command = run.call_args.args[0]
        self.assertIn("-nostdin", command)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg/ffprobe")
class FfmpegAlignmentTest(unittest.TestCase):
    @staticmethod
    def _make_video(path: Path, color: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=%s:s=64x48:r=15:d=2" % color,
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
            ],
            check=True,
        )

    @staticmethod
    def _duration(path: Path) -> float:
        output = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
        ])
        return float(output)

    def test_two_cameras_are_padded_to_the_same_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            segs_dir = root / "segs"
            outputs = []
            for camera, start, color in (("cam0", 101.0, "red"), ("cam1", 102.0, "blue")):
                segment = {"camera": camera, "start_ts": start, "end_ts": start + 2.0}
                source = Path(pull_oss_videos._segment_path(str(segs_dir), camera, segment, 0))
                self._make_video(source, color)
                out_path = root / (camera + "_continuous.mp4")
                work = root / (camera + "_work")
                work.mkdir()

                result = pull_oss_videos.align_concat_camera(
                    camera,
                    [segment],
                    str(segs_dir),
                    [(100.0, 105.0)],
                    str(out_path),
                    str(work),
                    "wall_clock",
                )

                self.assertTrue(result["ok"])
                self.assertEqual(result["window_s"], 5.0)
                outputs.append(out_path)

            durations = [self._duration(path) for path in outputs]
            for duration in durations:
                self.assertAlmostEqual(duration, 5.0, delta=1.0 / 15.0 + 0.01)
            self.assertLessEqual(max(durations) - min(durations), 0.1)

    def test_full_cli_writes_an_aligned_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mp4"
            self._make_video(source, "green")
            bundle_dir = root / "meta_task-c_1"
            bundle_dir.mkdir()
            (bundle_dir / "video_segments.json").write_text(
                json.dumps({
                    "task_id": "task-c",
                    "sub_task_id": "task-c_1",
                    "segments": [{
                        "camera": "cam0",
                        "start_ts": 101.0,
                        "end_ts": 103.0,
                        "url": source.as_uri(),
                    }],
                }),
                encoding="utf-8",
            )
            (bundle_dir / "export_meta.json").write_text(
                json.dumps({"keep_windows": [{"from": 100.0, "to": 105.0}]}),
                encoding="utf-8",
            )
            output_dir = root / "output"

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return_code = pull_oss_videos.main([
                    "--in", str(bundle_dir / "video_segments.json"),
                    "--out", str(output_dir),
                ])

            self.assertEqual(return_code, 0)
            manifest = json.loads(
                (output_dir / "videos_task-c_1" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["keep_windows_source"], "export_meta")
            self.assertEqual(manifest["camera_count"], 1)
            self.assertEqual(manifest["concat_ok"], 1)
            self.assertTrue(manifest["concat_complete"])
            self.assertTrue(manifest["align"])
            self.assertEqual(manifest["align_mode"], "wall_clock")

    def test_cli_processes_subtasks_concurrently_with_shared_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mp4"
            self._make_video(source, "yellow")
            input_paths = []
            for index in (1, 2):
                bundle_dir = root / ("meta_task-e_%d" % index)
                bundle_dir.mkdir()
                (bundle_dir / "video_segments.json").write_text(
                    json.dumps({
                        "task_id": "task-e",
                        "sub_task_id": "task-e_%d" % index,
                        "segments": [{
                            "camera": "cam0",
                            "start_ts": 101.0,
                            "end_ts": 103.0,
                            "url": source.as_uri(),
                        }],
                    }),
                    encoding="utf-8",
                )
                (bundle_dir / "export_meta.json").write_text(
                    json.dumps({"keep_windows": [{"from": 100.0, "to": 105.0}]}),
                    encoding="utf-8",
                )
                input_paths.append(bundle_dir / "video_segments.json")

            output_dir = root / "output"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return_code = pull_oss_videos.main([
                    "--in", str(input_paths[0]),
                    "--in", str(input_paths[1]),
                    "--out", str(output_dir),
                    "--jobs", "2",
                ])

            self.assertEqual(return_code, 0)
            for index in (1, 2):
                result_dir = output_dir / ("videos_task-e_%d" % index)
                self.assertTrue((result_dir / "cam0_continuous.mp4").stat().st_size > 0)
                manifest = json.loads((result_dir / "manifest.json").read_text(encoding="utf-8"))
                self.assertTrue(manifest["concat_complete"])


class DownloadTaskCompletionTest(unittest.TestCase):
    def test_completed_aligned_task_can_clean_intermediates_without_redownloading(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            task_id = "task-d"
            task_root = output_root / task_id
            unpacked = task_root / "meta" / "unpacked" / ("meta_" + task_id + "_1")
            result_dir = task_root / "videos" / ("videos_" + task_id + "_1")
            segment_dir = task_root / "videos" / "segs" / "cam0"
            unpacked.mkdir(parents=True)
            result_dir.mkdir(parents=True)
            segment_dir.mkdir(parents=True)
            (unpacked / "video_segments.json").write_text("{}", encoding="utf-8")
            (result_dir / "cam0_continuous.mp4").write_bytes(b"video")
            (segment_dir / "cam0_1.mp4").write_bytes(b"segment")
            zip_path = task_root / "meta" / ("meta_" + task_id + "_all.zip")
            zip_path.write_bytes(b"zip")
            (result_dir / "manifest.json").write_text(
                json.dumps({
                    "sub_task_id": task_id + "_1",
                    "download_ok": 1,
                    "download_skip": 0,
                    "download_fail": 0,
                    "camera_count": 1,
                    "concat_ok": 1,
                    "concat_complete": True,
                    "align": True,
                    "align_mode": "wall_clock",
                }),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    "bash", str(Path(__file__).with_name("download-task.sh")),
                    "--delete-zip", "--delete-segments", task_id, str(output_root),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("仅执行清理", completed.stdout)
            self.assertFalse(zip_path.exists())
            self.assertFalse((task_root / "videos" / "segs").exists())
            self.assertTrue((result_dir / "cam0_continuous.mp4").exists())


if __name__ == "__main__":
    unittest.main()
