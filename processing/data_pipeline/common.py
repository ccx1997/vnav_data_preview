from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from zoneinfo import ZoneInfo


class PipelineError(RuntimeError):
    pass


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def read_json(path):
    return json.loads(Path(path).read_text())


def rows(path):
    with Path(path).open() as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def task_ids(values):
    result = []
    for value in values:
        parts = json.loads(value) if value.strip().startswith("[") else value.split(",")
        if not isinstance(parts, list):
            raise PipelineError("任务列表必须是 JSON 数组或逗号/空格分隔的 ID")
        for task in parts:
            if isinstance(task, str):
                task = task.strip()
            if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", task):
                raise PipelineError(f"非法任务 ID: {task!r}")
            if task not in result:
                result.append(task)
    if not result:
        raise PipelineError("任务 ID 列表不能为空")
    return result


def load_environment(project):
    path = project / ".env"
    if not path.is_file():
        return dict(os.environ)
    # Capture only in memory. Never log environment variables or credentials.
    result = subprocess.run(["bash", "-c", 'set -a\nsource "$1" >&2\nexec env -0',
                             "vnav-env", str(path)], capture_output=True, check=False)
    if result.returncode:
        raise PipelineError("无法加载项目 .env")
    return dict(item.decode().split("=", 1) for item in result.stdout.split(b"\0") if item)


def run_logged(command, path, *, cwd, env=None):
    print(f"执行 {Path(command[0]).name}；日志：{path}", file=sys.stderr, flush=True)
    with Path(path).open("x") as log:
        process = subprocess.Popen([str(x) for x in command], cwd=cwd, env=env,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait()
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
            raise
    if code:
        raise PipelineError(f"命令退出码 {code}；详情见 {path}")


@contextmanager
def pipeline_lock(source_root):
    source_root.mkdir(parents=True, exist_ok=True)
    with (source_root / ".data_pipeline.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PipelineError("同一源目录已有流水线运行") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            item = dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns, inode=stat.st_ino)
            if path.suffix in (".json", ".jsonl"):
                item["sha256"] = digest(path)
            result[str(path.relative_to(root))] = item
    return result
