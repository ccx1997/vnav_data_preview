#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "${VNAV_PIPELINE_PYTHON:-/mnt/wlf/anaconda3/envs/deepseek_train/bin/python}" \
  "$script_dir/run_data_pipeline.py" "$@"
