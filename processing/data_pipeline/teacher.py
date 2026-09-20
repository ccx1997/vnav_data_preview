from contextlib import contextmanager
from pathlib import Path
import os
import socket
import shutil
import subprocess
import time

import yaml
from vnav_training.batch import teacher_health

from .common import PipelineError, digest, write_json


def preflight(args, config):
    for name in ("ffmpeg", "ffprobe"):
        if not shutil.which(name):
            raise PipelineError(f"缺少工具：{name}")
    for path in (args.teacher_config, args.teacher_python,
                 args.model_server_root / "model_server/__init__.py"):
        if not path.is_file():
            raise PipelineError(f"缺少教师依赖：{path}")
    if not os.access(args.teacher_python, os.X_OK):
        raise PipelineError("教师 Python 不可执行")
    service = yaml.safe_load(args.teacher_config.read_text())["model_service"]
    if service.get("inference_backend") != "rule10":
        raise PipelineError("当前流水线要求 Rule-10 教师")
    checkpoint = Path(service["rule10_checkpoint"])
    if not checkpoint.is_absolute():
        checkpoint = args.teacher_config.parent / checkpoint
    checkpoint = checkpoint.resolve()
    maps = {key: args.static_map_root / (value + ".npz") for key, value in config["map_mapping"].items()}
    for path in [checkpoint, *maps.values()]:
        if not path.is_file():
            raise PipelineError(f"缺少 checkpoint/地图：{path}")
    return dict(teacher_config_sha256=digest(args.teacher_config), checkpoint=str(checkpoint),
                checkpoint_sha256=digest(checkpoint), maps={k:str(v) for k,v in maps.items()})


def check_health(health, assets, config):
    batch = health.get("teacher_batch") or {}
    if (health.get("status") != "ok" or not health.get("ready")
            or health.get("inference_backend") != "rule10" or health.get("curvature_only") is not False
            or not batch.get("enabled") or not batch.get("isolated_from_online_history")
            or batch.get("max_batch_size", 0) < config["teacher_batch_size"]
            or batch.get("max_history_frames_per_sample", 0) < config["teacher_history_max_frames"]):
        raise PipelineError("教师服务与独立历史 batch 契约不匹配")
    checkpoint = Path(health.get("checkpoint") or "")
    if not checkpoint.is_file() or digest(checkpoint) != assets["checkpoint_sha256"]:
        raise PipelineError("已运行教师的 checkpoint 与指定配置不匹配，不复用或停止该服务")


def select_device(device, environment):
    if device != "auto":
        return device
    if environment.get("CUDA_VISIBLE_DEVICES"):
        return "cuda:0"
    result = subprocess.check_output(["nvidia-smi", "--query-gpu=uuid,memory.free,utilization.gpu",
                                      "--format=csv,noheader,nounits"], text=True)
    choices = []
    for line in result.splitlines():
        uuid, free, use = [part.strip() for part in line.split(",")]
        if int(free) >= 8192:
            choices.append((int(use), -int(free), uuid))
    if not choices:
        raise PipelineError("没有至少 8192 MiB 空闲显存的 GPU")
    environment["CUDA_VISIBLE_DEVICES"] = min(choices)[2]
    return "cuda:0"


@contextmanager
def teacher_service(args, config, assets, report):
    from urllib.parse import urlparse

    process = None
    log = None
    lifecycle = dict(reused=False, owned_teacher_stopped=False)
    url = args.teacher_url
    try:
        health = teacher_health(url) if url else None
        if health and health.get("ready"):
            check_health(health, assets, config)
            # Health exposes checkpoint identity, but not all YAML execution parameters.
            # For an existing local service verify its actual startup config as well.
            address = urlparse(url)
            if address.hostname not in ("localhost", "127.0.0.1"):
                raise PipelineError("当前仅支持可核验启动配置的本地教师服务")
            try:
                command = Path(f"/proc/{int(health['pid'])}/cmdline").read_bytes().decode().split("\0")
                config_path = Path(command[command.index("--config") + 1])
                if not config_path.is_absolute():
                    config_path = Path(f"/proc/{int(health['pid'])}/cwd").resolve() / config_path
                require_match = digest(config_path) == assets["teacher_config_sha256"]
            except (OSError, KeyError, ValueError, IndexError):
                require_match = False
            if not require_match:
                raise PipelineError("无法核验已有教师启动配置，或其 YAML 与指定配置不同；未停止该服务")
            lifecycle["reused"] = True
        else:
            if args.no_start_teacher:
                raise PipelineError("--no-start-teacher 要求指定已就绪的 --teacher-url")
            if not url:
                for port in range(8103, 8120):
                    with socket.socket() as sock:
                        try:
                            sock.bind(("127.0.0.1", port))
                        except OSError:
                            continue
                        url = f"http://127.0.0.1:{port}"
                        break
                if not url:
                    raise PipelineError("8103..8119 没有空闲本地端口")
            address = urlparse(url)
            if (address.scheme != "http" or address.hostname not in ("localhost", "127.0.0.1")
                    or not address.port or address.path not in ("", "/") or address.query or address.fragment):
                raise PipelineError("自动启动教师仅支持本地 HTTP host:port")
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(args.model_server_root) + os.pathsep + environment.get("PYTHONPATH", "")
            environment["PYTHONUNBUFFERED"] = "1"
            device = select_device(args.teacher_device, environment)
            command = [str(args.teacher_python), "-m", "model_server", "--config", str(args.teacher_config),
                       "--host", address.hostname, "--port", str(address.port), "--device", device, "--batch"]
            log = (report / "teacher_service.log").open("x")
            process = subprocess.Popen(command, cwd=args.model_server_root.parent.parent, env=environment,
                                       stdout=log, stderr=subprocess.STDOUT)
            lifecycle.update(owned_pid=process.pid, command=command, url=url,
                             cuda_visible_devices=environment.get("CUDA_VISIBLE_DEVICES"))
            deadline = time.monotonic() + args.teacher_start_timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise PipelineError(f"教师进程退出：{process.returncode}；见 teacher_service.log")
                health = teacher_health(url)
                if health and health.get("ready"):
                    if health.get("pid") != process.pid:
                        raise PipelineError("端口被其他教师进程占用")
                    check_health(health, assets, config)
                    break
                time.sleep(0.5)
            else:
                raise PipelineError("教师启动超时")
        write_json(report / "teacher_health.json", health)
        yield url
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            lifecycle["owned_teacher_stopped"] = True
        if log:
            log.close()
        write_json(report / "teacher_lifecycle.json", lifecycle)
