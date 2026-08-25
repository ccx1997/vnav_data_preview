#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "用法: $0 [--jobs N] [--remerge] [--delete-zip] [--delete-segments] <task_id> [output_root]"
  echo "  --jobs N           子任务与相机共享的总并发数（默认 4）"
  echo "  --remerge          复用已有片段重新合并并覆盖旧结果；片段缺失时只补下载缺失部分"
  echo "  --delete-zip       解压并完成下载后删除 meta_..._all.zip"
  echo "  --delete-segments  合并成功后删除 videos/segs 原始视频段"
  echo "  默认保留 ZIP 和视频 segments"
  echo "  已完成任务带删除选项重跑时，只执行清理，不重复下载或合并"
}

DELETE_ZIP=false
DELETE_SEGMENTS=false
REMERGE=false
JOBS=4
POSITIONAL=()

while (($#)); do
  case "$1" in
    --jobs)
      if (($# < 2)); then
        echo "--jobs 缺少数值" >&2
        exit 2
      fi
      JOBS="$2"
      shift
      ;;
    --remerge)
      REMERGE=true
      ;;
    --delete-zip)
      DELETE_ZIP=true
      ;;
    --delete-segments)
      DELETE_SEGMENTS=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      POSITIONAL+=("$@")
      break
      ;;
    -*)
      echo "未知选项: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      POSITIONAL+=("$1")
      ;;
  esac
  shift
done

if ((${#POSITIONAL[@]} < 1 || ${#POSITIONAL[@]} > 2)); then
  usage >&2
  exit 2
fi
if [[ ! "${JOBS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "--jobs 必须是大于 0 的整数: ${JOBS}" >&2
  exit 2
fi

TASK_ID="${POSITIONAL[0]}"
OUTPUT_ROOT="${POSITIONAL[1]:-/mnt/chengchangxu/data/visual_nav_mv}"
if [[ ! "${TASK_ID}" =~ ^[0-9A-Za-z][0-9A-Za-z._-]*$ ]]; then
  echo "非法 task_id: ${TASK_ID}" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TASK_DIR="${OUTPUT_ROOT}/${TASK_ID}"
META_DIR="${TASK_DIR}/meta"
UNPACK_DIR="${META_DIR}/unpacked"
VIDEO_DIR="${TASK_DIR}/videos"
ZIP_PATH="${META_DIR}/meta_${TASK_ID}_all.zip"

collect_segment_files() {
  SEGMENT_FILES=()
  if [[ -d "${UNPACK_DIR}" ]]; then
    mapfile -d '' SEGMENT_FILES < <(
      find "${UNPACK_DIR}" -type f -name video_segments.json -print0 | sort -z
    )
  fi
}

segments_are_complete() {
  collect_segment_files
  ((${#SEGMENT_FILES[@]} > 0)) || return 1
  python3 - "${VIDEO_DIR}/segs" "${SEGMENT_FILES[@]}" <<'PY'
import json
import os
import sys


def as_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


segs_dir = sys.argv[1]
for json_path in sys.argv[2:]:
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    segments = data.get("segments") or []
    if not segments:
        raise SystemExit(1)
    grouped = {}
    for segment in segments:
        camera = str(segment.get("camera") or "unknown")
        grouped.setdefault(camera, []).append(segment)
    for camera, camera_segments in grouped.items():
        camera_segments.sort(key=lambda segment: (
            as_float(segment.get("clip_from_ts"))
            if as_float(segment.get("clip_from_ts")) is not None
            else (as_float(segment.get("start_ts")) or 0.0)
        ))
        for index, segment in enumerate(camera_segments):
            start = as_float(segment.get("start_ts"))
            token = int(start) if start is not None else index
            path = os.path.join(segs_dir, camera, "%s_%s.mp4" % (camera, token))
            if not os.path.isfile(path) or os.path.getsize(path) <= 0:
                raise SystemExit(1)
PY
}

task_is_complete() {
  collect_segment_files
  ((${#SEGMENT_FILES[@]} > 0)) || return 1

  local segments_json bundle_name sub_task_id result_dir manifest expected_camera_count continuous_count
  for segments_json in "${SEGMENT_FILES[@]}"; do
    bundle_name="$(basename -- "$(dirname -- "${segments_json}")")"
    [[ "${bundle_name}" == meta_* ]] || return 1
    sub_task_id="${bundle_name#meta_}"
    result_dir="${VIDEO_DIR}/videos_${sub_task_id}"
    manifest="${result_dir}/manifest.json"
    [[ -s "${manifest}" ]] || return 1
    expected_camera_count="$(python3 -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    data = json.load(f)
ok = (
    data.get("sub_task_id") == sys.argv[2]
    and data.get("download_ok", 0) + data.get("download_reused", 0) > 0
    and data.get("download_skip", 0) == 0
    and data.get("download_fail", 0) == 0
    and data.get("camera_count", 0) > 0
    and data.get("concat_ok") == data.get("camera_count")
    and data.get("concat_complete") is True
    and data.get("align") is True
    and data.get("align_mode") in {"hw_ts", "wall_clock"}
)
if not ok:
    raise SystemExit(1)
print(data["camera_count"])
' "${manifest}" "${sub_task_id}")" || return 1
    continuous_count="$(find "${result_dir}" -maxdepth 1 -type f -name 'cam*_continuous.mp4' -size +0c | wc -l)"
    ((continuous_count == expected_camera_count)) || return 1
  done
}

cleanup_outputs() {
  if [[ "${DELETE_ZIP}" == true ]]; then
    if [[ -f "${ZIP_PATH}" ]]; then
      rm -- "${ZIP_PATH}"
      echo "已删除 ZIP: ${ZIP_PATH}"
    else
      echo "ZIP 已不存在: ${ZIP_PATH}"
    fi
  fi

  if [[ "${DELETE_SEGMENTS}" == true ]]; then
    if [[ -d "${VIDEO_DIR}/segs" ]]; then
      rm -rf -- "${VIDEO_DIR}/segs"
      echo "已删除视频 segments: ${VIDEO_DIR}/segs"
    else
      echo "视频 segments 已不存在: ${VIDEO_DIR}/segs"
    fi
  fi
}

download_metadata() {
  python3 "${SCRIPT_DIR}/pull-task-export.py" \
    --task "${TASK_ID}" \
    --out "${META_DIR}"
  unzip -q -o "${ZIP_PATH}" -d "${UNPACK_DIR}"
}

if [[ "${REMERGE}" == false ]] \
  && [[ "${DELETE_ZIP}" == true || "${DELETE_SEGMENTS}" == true ]] \
  && task_is_complete; then
  echo "检测到任务已完整处理，仅执行清理，不重复下载或合并。"
  cleanup_outputs
  echo "清理完成: ${TASK_DIR}"
  exit 0
fi

mkdir -p "${META_DIR}" "${UNPACK_DIR}" "${VIDEO_DIR}"

if [[ "${REMERGE}" == true ]]; then
  if segments_are_complete; then
    echo "检测到视频 segments 完整，直接重新合并，不重复下载。"
  else
    echo "视频 segments 缺失或不完整，刷新任务导出并补下载缺失片段。"
    download_metadata
  fi
else
  download_metadata
fi

collect_segment_files
if ((${#SEGMENT_FILES[@]} == 0)); then
  echo "未找到 video_segments.json，不执行清理" >&2
  exit 1
fi

pull_args=(--out "${VIDEO_DIR}" --jobs "${JOBS}")
for segments_json in "${SEGMENT_FILES[@]}"; do
  pull_args+=(--in "${segments_json}")
done
if [[ "${REMERGE}" == true ]]; then
  pull_args+=(--reuse-segments)
fi
python3 "${SCRIPT_DIR}/pull-oss-videos.py" "${pull_args[@]}"

if ! task_is_complete; then
  echo "任务输出未通过完整性检查，不执行清理" >&2
  exit 1
fi

cleanup_outputs

if [[ "${REMERGE}" == true ]]; then
  echo "重新合并完成: ${TASK_DIR}"
else
  echo "下载完成: ${TASK_DIR}"
fi
