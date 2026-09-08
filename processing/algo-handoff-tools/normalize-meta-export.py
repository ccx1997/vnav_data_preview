#!/usr/bin/env python3
"""Normalize one annotation export ZIP into unpacked/meta_<sub_task_id>/ bundles."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SUB_TASK_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")
REQUIRED_FILES = ("task.json", "export_meta.json", "video_segments.json", "frames.jsonl")


def _safe_name(raw: str) -> str:
    normalized = str(raw or "").replace("\\", "/").lstrip("./")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe ZIP member: %r" % raw)
    return str(path)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def _read_json(zf: zipfile.ZipFile, member: str) -> Dict[str, Any]:
    try:
        value = json.loads(zf.read(member).decode("utf-8"))
    except KeyError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object: %s" % member)
    return value


def _nested_id(document: Mapping[str, Any]) -> str:
    value = document.get("sub_task")
    if isinstance(value, Mapping):
        return str(value.get("sub_task_id") or "")
    return ""


def _bundle_sub_task_id(
    zf: zipfile.ZipFile,
    prefix: str,
    task_id: str,
) -> str:
    def member(filename: str) -> str:
        return prefix + filename

    task = _read_json(zf, member("task.json"))
    export_meta = _read_json(zf, member("export_meta.json"))
    video_meta = _read_json(zf, member("video_segments.json"))
    prefix_name = prefix.rstrip("/").rsplit("/", 1)[-1] if prefix else ""
    prefix_sub_task_id = prefix_name[5:] if prefix_name.startswith("meta_") else prefix_name
    candidates = {
        value
        for value in (
            prefix_sub_task_id,
            str(video_meta.get("sub_task_id") or ""),
            _nested_id(task),
            _nested_id(export_meta),
        )
        if value
    }
    if len(candidates) != 1:
        raise ValueError(
            "subtask ownership is missing or inconsistent for prefix %r: %s"
            % (prefix, sorted(candidates))
        )
    sub_task_id = candidates.pop()
    if not SUB_TASK_RE.fullmatch(sub_task_id):
        raise ValueError("invalid sub_task_id: %r" % sub_task_id)
    if not sub_task_id.startswith(task_id + "_"):
        raise ValueError(
            "subtask %r does not belong to task %r" % (sub_task_id, task_id)
        )
    declared_task_ids = {
        str(value)
        for value in (task.get("task_id"), video_meta.get("task_id"))
        if value
    }
    if declared_task_ids and declared_task_ids != {task_id}:
        raise ValueError(
            "task ownership mismatch for %s: %s" % (sub_task_id, sorted(declared_task_ids))
        )
    return sub_task_id


def _bundle_prefixes(names: Sequence[str]) -> Tuple[List[str], bool]:
    frame_members = sorted(name for name in names if name.rsplit("/", 1)[-1] == "frames.jsonl")
    if not frame_members:
        raise ValueError("ZIP contains no frames.jsonl")
    root_layout = "frames.jsonl" in frame_members
    nested = sorted(
        {name.rsplit("/", 1)[0] + "/" for name in frame_members if "/" in name}
    )
    if root_layout and nested:
        raise ValueError("ZIP mixes root-level and nested Meta bundles")
    return ([""] if root_layout else nested), root_layout


def _bundle_members(names: Sequence[str], prefix: str) -> Iterable[Tuple[str, str]]:
    for name in names:
        if prefix:
            if not name.startswith(prefix):
                continue
            relative = name[len(prefix) :]
        else:
            relative = name
        if not relative or relative.endswith("/"):
            continue
        yield name, relative


def normalize_archive(archive: Path, unpack_root: Path, task_id: str) -> Dict[str, Any]:
    archive = archive.resolve()
    unpack_root = unpack_root.resolve()
    unpack_root.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix=".normalizing-meta-", dir=str(unpack_root)))
    warnings: List[str] = []
    normalized: List[str] = []
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = list(zf.infolist())
            names = [_safe_name(info.filename) for info in infos]
            if len(names) != len(set(names)):
                raise ValueError("ZIP contains duplicate member names")
            for info in infos:
                if _is_symlink(info):
                    raise ValueError("ZIP symlink is not allowed: %s" % info.filename)
            info_by_name = {_safe_name(info.filename): info for info in infos}
            prefixes, root_layout = _bundle_prefixes(names)
            if root_layout:
                warnings.append(
                    "Meta ZIP uses root-level single-subtask layout; normalized into meta_<sub_task_id>/"
                )
            for prefix in prefixes:
                sub_task_id = _bundle_sub_task_id(zf, prefix, task_id)
                target_name = "meta_" + sub_task_id
                target_stage = stage_root / target_name
                target_stage.mkdir()
                seen_relative = set()
                for source_name, relative in _bundle_members(names, prefix):
                    relative_path = PurePosixPath(relative)
                    if relative_path.is_absolute() or ".." in relative_path.parts:
                        raise ValueError("unsafe bundle member: %s" % source_name)
                    if relative in seen_relative:
                        raise ValueError("duplicate bundle member: %s" % relative)
                    seen_relative.add(relative)
                    destination = target_stage.joinpath(*relative_path.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info_by_name[source_name]) as source, destination.open("wb") as output:
                        shutil.copyfileobj(source, output)
                missing = [name for name in REQUIRED_FILES if not (target_stage / name).is_file()]
                if missing:
                    raise ValueError("bundle %s is missing %s" % (sub_task_id, missing))
                if (target_stage / "frames.jsonl").stat().st_size <= 0:
                    raise ValueError("bundle %s has empty frames.jsonl" % sub_task_id)
                normalized.append(sub_task_id)

        for sub_task_id in normalized:
            staged = stage_root / ("meta_" + sub_task_id)
            target = unpack_root / staged.name
            backup: Optional[Path] = None
            try:
                if target.exists():
                    backup = unpack_root / (".previous-%s-%s" % (uuid.uuid4().hex, staged.name))
                    target.replace(backup)
                staged.replace(target)
            except BaseException:
                if backup is not None and backup.exists() and not target.exists():
                    backup.replace(target)
                raise
            else:
                if backup is not None:
                    shutil.rmtree(backup)
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)

    return {
        "archive": str(archive),
        "unpack_root": str(unpack_root),
        "task_id": task_id,
        "sub_task_ids": normalized,
        "warnings": warnings,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="校验 Meta ZIP 归属并规范为 unpacked/meta_<sub_task_id>/"
    )
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--unpack-root", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args(argv)
    result = normalize_archive(args.archive, args.unpack_root, str(args.task_id))
    print("Meta 规范化完成：%s" % ", ".join(result["sub_task_ids"]))
    for warning in result["warnings"]:
        print("NORMALIZE_WARNING=" + str(warning))
    print("NORMALIZE_RESULT=" + json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
