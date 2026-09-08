#!/usr/bin/env python3
"""Render a review-only PNG from the authoritative arrays in one inputs.npz."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image


def render(archive_path: Path) -> Image.Image:
    with np.load(archive_path, allow_pickle=False) as archive:
        occupancy = np.asarray(archive["static_occupancy"])
        route_mask = np.asarray(archive["forward_route_mask"])
        rollout = np.asarray(archive["teacher_rollout"])

    if occupancy.ndim != 2 or route_mask.shape != occupancy.shape:
        raise ValueError("map_route_shape_mismatch")

    rgb = np.full((*occupancy.shape, 3), (245, 247, 244), dtype=np.uint8)
    rgb[occupancy != 0] = (34, 41, 47)
    rgb[route_mask != 0] = (20, 185, 220)

    resolution_m = 0.05
    size = occupancy.shape[0]
    for state in rollout:
        if len(state) < 2:
            continue
        col = int(round(size / 2.0 + float(state[0]) / resolution_m))
        row = int(round(size / 2.0 - float(state[1]) / resolution_m))
        if 0 <= row < size and 0 <= col < occupancy.shape[1]:
            rgb[max(0, row - 2) : row + 3, max(0, col - 2) : col + 3] = (245, 145, 35)

    center_row = occupancy.shape[0] // 2
    center_col = occupancy.shape[1] // 2
    rgb[center_row - 4 : center_row + 5, center_col - 4 : center_col + 5] = (225, 48, 62)
    return Image.fromarray(np.rot90(rgb, 1), mode="RGB")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: renderTrainingMap.py INPUTS_NPZ", file=sys.stderr)
        return 2
    image = render(Path(sys.argv[1]))
    image.save(sys.stdout.buffer, format="PNG", optimize=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
