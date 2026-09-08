#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec /mnt/wlf/anaconda3/envs/deepseek_train/bin/python \
  "$script_dir/build_pending_training_data.py" \
  --confirm-full SATISFIED \
  "$@"
