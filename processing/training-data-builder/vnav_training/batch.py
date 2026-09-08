from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener

from .pipeline import run_full, write_json


@dataclass(frozen=True)
class TaskScan:
    task_id: str
    task_root: Path
    status: str
    reason: str | None = None
    completed_run: Path | None = None
    warnings: tuple[str, ...] = ()

    def document(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_root": str(self.task_root),
            "status": self.status,
            "reason": self.reason,
            "completed_run": str(self.completed_run) if self.completed_run else None,
            "warnings": list(self.warnings),
        }


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def find_completed_full_run(
    output_root: Path,
    task_id: str,
    pipeline_version: str,
) -> Path | None:
    full_root = output_root / task_id / pipeline_version / "full"
    for run_json in sorted(full_root.glob("run_*/run.json"), reverse=True):
        try:
            document = _read_json(run_json)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not (
            document.get("mode") == "full"
            and document.get("status") == "success"
            and document.get("pipeline_version") == pipeline_version
            and document.get("full_generation_authorized") is True
            and int(document.get("accepted_count") or 0) > 0
            and (run_json.parent / "cases").is_dir()
            and (run_json.parent / "source_manifest.jsonl").is_file()
            and (run_json.parent / "teacher_labels.jsonl").is_file()
            and (run_json.parent / "rgb_history_manifest.jsonl").is_file()
        ):
            continue
        return run_json.parent
    return None


def task_layout_error(task_root: Path, camera_ids: Sequence[str]) -> str | None:
    meta_root = task_root / "meta" / "unpacked"
    video_root = task_root / "videos"
    meta_dirs = sorted(
        path
        for path in meta_root.glob("meta_*")
        if (path / "frames.jsonl").is_file()
    )
    if not meta_dirs:
        return "meta_unpacked_missing"
    if not video_root.is_dir():
        return "video_root_missing"
    for meta_dir in meta_dirs:
        source_sub_task_id = meta_dir.name.removeprefix("meta_")
        video_dir = video_root / f"videos_{source_sub_task_id}"
        manifest_path = video_dir / "manifest.json"
        if not manifest_path.is_file():
            return f"video_manifest_missing:{source_sub_task_id}"
        try:
            manifest = _read_json(manifest_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return f"video_manifest_invalid:{source_sub_task_id}:{type(error).__name__}"
        if not manifest.get("concat_complete"):
            return f"video_manifest_incomplete:{source_sub_task_id}"
        result_by_camera = {
            str(item.get("camera")): item
            for item in manifest.get("results", ())
            if isinstance(item, dict) and item.get("ok") and item.get("camera")
        }
        camera_count = int(manifest.get("camera_count") or len(result_by_camera))
        if (
            camera_count <= 0
            or int(manifest.get("concat_ok") or 0) != camera_count
            or len(result_by_camera) != camera_count
        ):
            return f"video_camera_count_mismatch:{source_sub_task_id}"
        for camera_id, item in result_by_camera.items():
            path = Path(str(item.get("out") or ""))
            if not path.is_file():
                path = video_dir / f"{camera_id}_continuous.mp4"
            if not path.is_file() or path.stat().st_size <= 0:
                return f"video_missing:{source_sub_task_id}/{camera_id}"
    return None


def task_layout_warnings(task_root: Path, camera_ids: Sequence[str]) -> tuple[str, ...]:
    recommended = tuple(str(value) for value in camera_ids)
    warnings = []
    for meta_dir in sorted((task_root / "meta" / "unpacked").glob("meta_*")):
        if not (meta_dir / "frames.jsonl").is_file():
            continue
        source_sub_task_id = meta_dir.name.removeprefix("meta_")
        manifest_path = task_root / "videos" / f"videos_{source_sub_task_id}" / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = _read_json(manifest_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        actual = tuple(
            camera_id
            for camera_id in recommended
            if any(
                isinstance(item, dict)
                and item.get("ok")
                and str(item.get("camera")) == camera_id
                for item in manifest.get("results", ())
            )
        )
        extras = sorted(
            {
                str(item.get("camera"))
                for item in manifest.get("results", ())
                if isinstance(item, dict)
                and item.get("ok")
                and item.get("camera")
                and str(item.get("camera")) not in recommended
            }
        )
        actual += tuple(extras)
        if actual != recommended:
            warnings.append(
                "video_camera_set_nonstandard:%s:actual=%s:recommended=%s"
                % (source_sub_task_id, ",".join(actual), ",".join(recommended))
            )
    return tuple(warnings)


def scan_tasks(
    source_root: Path,
    output_root: Path,
    config: Mapping[str, Any],
    task_ids: Sequence[str] = (),
) -> list[TaskScan]:
    selected = set(task_ids)
    paths = sorted(
        path
        for path in source_root.iterdir()
        if path.is_dir() and (not selected or path.name in selected)
    )
    missing = selected - {path.name for path in paths}
    if missing:
        raise FileNotFoundError(f"requested task directories are missing: {sorted(missing)}")
    scans: list[TaskScan] = []
    for task_root in paths:
        completed = find_completed_full_run(
            output_root, task_root.name, str(config["pipeline_version"])
        )
        if completed is not None:
            scans.append(
                TaskScan(
                    task_id=task_root.name,
                    task_root=task_root,
                    status="already_completed",
                    completed_run=completed,
                )
            )
            continue
        reason = task_layout_error(task_root, config["camera_ids"])
        warnings = task_layout_warnings(task_root, config["camera_ids"])
        scans.append(
            TaskScan(
                task_id=task_root.name,
                task_root=task_root,
                status="incomplete" if reason else "pending",
                reason=reason,
                warnings=warnings,
            )
        )
    return scans


def teacher_health(url: str, timeout_s: float = 1.0) -> dict[str, Any] | None:
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(url.rstrip("/") + "/health", timeout=timeout_s) as response:
            document = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return document if isinstance(document, dict) else None


class TeacherService:
    def __init__(
        self,
        *,
        url: str,
        model_server_root: Path,
        teacher_config: Path,
        python: Path,
        device: str,
        log_path: Path,
        start_timeout_s: float,
        auto_start: bool,
    ) -> None:
        self.url = url.rstrip("/")
        self.model_server_root = model_server_root
        self.teacher_config = teacher_config
        self.python = python
        self.device = device
        self.log_path = log_path
        self.start_timeout_s = start_timeout_s
        self.auto_start = auto_start
        self.process: subprocess.Popen[bytes] | None = None
        self._log_stream = None
        self.reused = False

    def start(self) -> Mapping[str, Any]:
        health = teacher_health(self.url)
        if health and health.get("status") == "ok" and health.get("ready"):
            self.reused = True
            return health
        if not self.auto_start:
            raise RuntimeError(f"teacher service is not ready at {self.url}")
        parsed = urlparse(self.url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("automatic teacher startup requires a local http URL")
        if parsed.path not in {"", "/"} or parsed.port is None:
            raise ValueError(f"teacher URL must include only host and port: {self.url}")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_stream = self.log_path.open("ab")
        environment = dict(os.environ)
        prior_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(self.model_server_root) + (
            os.pathsep + prior_pythonpath if prior_pythonpath else ""
        )
        command = [
            str(self.python),
            "-m",
            "model_server",
            "--config",
            str(self.teacher_config),
            "--host",
            str(parsed.hostname),
            "--port",
            str(parsed.port),
            "--device",
            self.device,
        ]
        self.process = subprocess.Popen(
            command,
            cwd=self.model_server_root,
            env=environment,
            stdout=self._log_stream,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + self.start_timeout_s
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"teacher exited with code {self.process.returncode}; see {self.log_path}"
                )
            health = teacher_health(self.url)
            if health and health.get("status") == "ok" and health.get("ready"):
                return health
            time.sleep(0.5)
        raise TimeoutError(
            f"teacher did not become ready within {self.start_timeout_s}s; "
            f"see {self.log_path}"
        )

    def stop(self) -> None:
        try:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=20.0)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=10.0)
        finally:
            if self._log_stream is not None:
                self._log_stream.close()
                self._log_stream = None


@contextmanager
def batch_lock(output_root: Path) -> Iterator[None]:
    lock_path = output_root / "_batch_runs" / "batch.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"another training batch holds {lock_path}") from error
        stream.seek(0)
        stream.truncate()
        stream.write(f"pid={os.getpid()} started={datetime.now().astimezone().isoformat()}\n")
        stream.flush()
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write_batch_summary(path: Path, document: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    write_json(temporary, document)
    temporary.replace(path)


def run_pending_batch(
    *,
    source_root: Path,
    output_root: Path,
    config: Mapping[str, Any],
    teacher_url: str,
    model_server_root: Path,
    teacher_config: Path,
    static_map_root: Path,
    teacher_python: Path,
    teacher_device: str,
    teacher_start_timeout_s: float,
    auto_start_teacher: bool,
    dry_run: bool,
    task_ids: Sequence[str] = (),
) -> tuple[Path | None, dict[str, Any]]:
    started = time.time()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    scans = scan_tasks(source_root, output_root, config, task_ids)
    pending = [scan for scan in scans if scan.status == "pending"]
    initial_document: dict[str, Any] = {
        "batch_id": f"pending_full:{timestamp}",
        "mode": "pending_full",
        "status": "dry_run" if dry_run else "running",
        "source_root": str(source_root),
        "output_root": str(output_root),
        "pipeline_version": config["pipeline_version"],
        "started_at": datetime.fromtimestamp(started).astimezone().isoformat(),
        "dry_run": dry_run,
        "task_filter": list(task_ids),
        "tasks": [scan.document() for scan in scans],
        "counts": {
            "discovered": len(scans),
            "pending": len(pending),
            "already_completed": sum(scan.status == "already_completed" for scan in scans),
            "incomplete": sum(scan.status == "incomplete" for scan in scans),
            "generated": 0,
            "failed": 0,
        },
    }
    if dry_run:
        initial_document["completed_at"] = datetime.now().astimezone().isoformat()
        initial_document["elapsed_s"] = round(time.time() - started, 3)
        return None, initial_document

    with batch_lock(output_root):
        # Re-scan under the lock so a task completed by a previous invocation
        # between discovery and lock acquisition is not generated twice.
        scans = scan_tasks(source_root, output_root, config, task_ids)
        pending = [scan for scan in scans if scan.status == "pending"]
        batch_dir = output_root / "_batch_runs" / f"run_{timestamp}_{os.getpid()}"
        batch_dir.mkdir(parents=True, exist_ok=False)
        summary_path = batch_dir / "batch_run.json"
        initial_document["tasks"] = [scan.document() for scan in scans]
        initial_document["counts"].update(
            {
                "discovered": len(scans),
                "pending": len(pending),
                "already_completed": sum(
                    scan.status == "already_completed" for scan in scans
                ),
                "incomplete": sum(scan.status == "incomplete" for scan in scans),
            }
        )
        _write_batch_summary(summary_path, initial_document)
        if not pending:
            initial_document["status"] = "success"
            initial_document["completed_at"] = datetime.now().astimezone().isoformat()
            initial_document["elapsed_s"] = round(time.time() - started, 3)
            _write_batch_summary(summary_path, initial_document)
            return batch_dir, initial_document

        service = TeacherService(
            url=teacher_url,
            model_server_root=model_server_root,
            teacher_config=teacher_config,
            python=teacher_python,
            device=teacher_device,
            log_path=batch_dir / "teacher.log",
            start_timeout_s=teacher_start_timeout_s,
            auto_start=auto_start_teacher,
        )
        task_documents = initial_document["tasks"]
        try:
            health = service.start()
            initial_document["teacher"] = {
                "url": teacher_url,
                "reused_existing_service": service.reused,
                "health": health,
                "log": str(service.log_path) if not service.reused else None,
            }
            _write_batch_summary(summary_path, initial_document)
            task_by_id = {item["task_id"]: item for item in task_documents}
            for scan in pending:
                task_document = task_by_id[scan.task_id]
                task_document["status"] = "running"
                task_document["started_at"] = datetime.now().astimezone().isoformat()
                _write_batch_summary(summary_path, initial_document)
                try:
                    run_dir = run_full(
                        task_root=scan.task_root,
                        output_root=output_root,
                        confirmation="SATISFIED",
                        teacher_url=teacher_url,
                        model_server_root=model_server_root,
                        teacher_config=teacher_config,
                        static_map_root=static_map_root,
                        config=config,
                    )
                    run_document = _read_json(run_dir / "run.json")
                    accepted = int(run_document.get("accepted_count") or 0)
                    if accepted <= 0:
                        raise RuntimeError(f"full run produced no accepted samples: {run_dir}")
                    task_document.update(
                        {
                            "status": "generated",
                            "output": str(run_dir),
                            "candidate_count": int(
                                run_document.get("candidate_count") or 0
                            ),
                            "accepted_count": accepted,
                            "rejected_count": int(
                                run_document.get("rejected_count") or 0
                            ),
                            "completed_at": datetime.now().astimezone().isoformat(),
                        }
                    )
                    initial_document["counts"]["generated"] += 1
                except Exception as error:
                    task_document.update(
                        {
                            "status": "failed",
                            "error": f"{type(error).__name__}: {error}",
                            "failed_at": datetime.now().astimezone().isoformat(),
                        }
                    )
                    initial_document["counts"]["failed"] += 1
                _write_batch_summary(summary_path, initial_document)
        except BaseException as error:
            initial_document["status"] = (
                "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
            )
            initial_document["batch_error"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            service.stop()
            initial_document["teacher_stopped"] = not service.reused
            initial_document["completed_at"] = datetime.now().astimezone().isoformat()
            initial_document["elapsed_s"] = round(time.time() - started, 3)
            if initial_document["status"] == "running":
                initial_document["status"] = (
                    "partial_failure"
                    if initial_document["counts"]["failed"]
                    else "success"
                )
            _write_batch_summary(summary_path, initial_document)
        return batch_dir, initial_document
