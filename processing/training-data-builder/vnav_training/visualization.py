from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image, ImageDraw, ImageFont


RECOMMENDED_CAMERA_ORDER = ("cam0", "cam1", "cam2", "cam3", "cam5", "cam6")


def _camera_order(sample: Mapping[str, Any]) -> tuple[str, ...]:
    available = {str(value) for value in (sample.get("camera_matches") or {})}
    return tuple(value for value in RECOMMENDED_CAMERA_ORDER if value in available) + tuple(
        sorted(value for value in available if value not in RECOMMENDED_CAMERA_ORDER)
    )


def _body_preview_rgb(
    static_occupancy: np.ndarray,
    route_mask: np.ndarray,
    rollout: np.ndarray,
    *,
    resolution_m: float,
) -> np.ndarray:
    occupancy = np.asarray(static_occupancy)
    rgb = np.full((*occupancy.shape, 3), 245, dtype=np.uint8)
    rgb[occupancy != 0] = (35, 40, 48)
    rgb[np.asarray(route_mask) != 0] = (20, 185, 220)
    size = occupancy.shape[0]
    for x, y, _yaw, _elapsed in np.asarray(rollout):
        col = int(round(size / 2.0 + float(x) / resolution_m))
        row = int(round(size / 2.0 - float(y) / resolution_m))
        if 0 <= row < size and 0 <= col < size:
            row0, row1 = max(0, row - 2), min(size, row + 3)
            col0, col1 = max(0, col - 2), min(size, col + 3)
            rgb[row0:row1, col0:col1] = (255, 145, 30)
    center = size // 2
    rgb[max(0, center - 4) : center + 5, max(0, center - 4) : center + 5] = (
        225,
        40,
        55,
    )
    return np.rot90(rgb, 1)


def _grid_preview(
    raw_local_occupancy: np.ndarray,
    fused_local_occupancy: np.ndarray,
) -> np.ndarray:
    raw = np.asarray(raw_local_occupancy)
    fused = np.asarray(fused_local_occupancy)
    rgb = np.full((*raw.shape, 3), 255, dtype=np.uint8)
    rgb[raw != 0] = (30, 35, 42)
    rgb[(raw == 0) & (fused != 0)] = (245, 145, 35)
    size = rgb.shape[0]
    center = size // 2
    rgb[max(0, center - 3) : center + 4, max(0, center - 3) : center + 4] = (
        225,
        40,
        55,
    )
    return np.rot90(rgb, 1)


def render_case(
    case_dir: Path,
    sample: Mapping[str, Any],
    *,
    camera_names: Mapping[str, str],
    raw_local_occupancy: np.ndarray,
    fused_local_occupancy: np.ndarray,
    static_occupancy: np.ndarray,
    route_mask: np.ndarray,
    rollout: np.ndarray,
    resolution_m: float,
) -> Path:
    camera_ids = _camera_order(sample)
    if not camera_ids:
        raise ValueError("preview has no camera matches")
    camera_rows = int(np.ceil(len(camera_ids) / 3))
    total_rows = camera_rows + 2
    figure = plt.figure(
        figsize=(18, 4.0 * camera_rows + 8.0), constrained_layout=False
    )
    grid = figure.add_gridspec(
        total_rows,
        3,
        height_ratios=tuple([1.0] * camera_rows + [1.08, 0.62]),
        left=0.035,
        right=0.98,
        bottom=0.045,
        top=0.93,
        hspace=0.27,
        wspace=0.15,
    )
    for index, camera_id in enumerate(camera_ids):
        axis = figure.add_subplot(grid[index // 3, index % 3])
        image = Image.open(case_dir / f"{camera_id}.png").convert("RGB")
        axis.imshow(image)
        match = sample["camera_matches"][camera_id]
        axis.set_title(
            f"{camera_id} / {camera_names.get(camera_id, camera_id)}  "
            f"dt={match['delta_ms']:+.1f} ms  frame={match['frame_index']}",
            fontsize=10,
            pad=5,
        )
        axis.axis("off")

    occupancy_axis = figure.add_subplot(grid[camera_rows, 0])
    occupancy_axis.imshow(
        _grid_preview(raw_local_occupancy, fused_local_occupancy)
    )
    occupancy_axis.set_title(
        "Fused local occupancy (orange=static-only, forward is up)",
        fontsize=11,
    )
    occupancy_axis.axis("off")

    route_axis = figure.add_subplot(grid[camera_rows, 1])
    route_axis.imshow(
        _body_preview_rgb(
            static_occupancy,
            route_mask,
            rollout,
            resolution_m=resolution_m,
        )
    )
    route_axis.set_title("Static map + forward route + teacher rollout", fontsize=11)
    route_axis.axis("off")

    command_axis = figure.add_subplot(grid[camera_rows, 2])
    commands = sample["teacher"]["commands"]
    indices = np.arange(1, len(commands) + 1)
    linear = [float(item["linear_mps"]) for item in commands]
    angular = [float(item["angular_rps"]) for item in commands]
    command_axis.plot(indices, linear, "o-", color="#1579c5", label="linear m/s")
    command_axis.plot(indices, angular, "s-", color="#d84a32", label="angular rad/s")
    command_axis.axhline(0.0, color="#777777", linewidth=0.8)
    command_axis.set_xticks(indices)
    command_axis.set_xlabel("teacher action step")
    command_axis.grid(True, alpha=0.25)
    command_axis.legend(loc="best")
    command_axis.set_title("Teacher action chunk", fontsize=11)

    info_axis = figure.add_subplot(grid[camera_rows + 1, :])
    info_axis.axis("off")
    state = sample["initial_state"]
    projection = sample["route_projection"]
    diagnostics = sample["teacher"]["diagnostics"]
    compact_commands = [
        f"{index + 1}:({float(item['linear_mps']):.3f},"
        f"{float(item['angular_rps']):.3f},{float(item['duration_s']):.2f}s)"
        for index, item in enumerate(commands)
    ]
    info_lines = [
        f"case={sample['case_id']}  subtask={sample['sub_task_id']}  ts={sample['meta_ts']:.3f}  "
        f"map={sample['map_name']} -> {sample['graph_name']}",
        f"grid_pose=({sample['grid_pose']['x']:.3f}, {sample['grid_pose']['y']:.3f}, "
        f"yaw={sample['grid_pose']['yaw_rad']:.3f} rad)  route_distance={projection['distance_m']:.3f} m  "
        f"remaining={projection['remaining_m']:.2f} m",
        f"initial v/w/kappa={state['initial_linear_mps']:.3f} / "
        f"{state['initial_angular_rps']:.3f} / {state['initial_curvature']:.3f}  "
        f"profile={'indoor' if state['indoor_profile'] else 'outdoor'}  "
        f"collision={sample['rollout']['collision']}",
        f"teacher={sample['teacher']['model_id']}  inference={diagnostics.get('inference_ms')} ms  "
        "actions=(linear m/s, angular rad/s, duration)",
        "  ".join(compact_commands),
    ]
    info_axis.text(
        0.01,
        0.96,
        "\n".join(info_lines),
        va="top",
        ha="left",
        fontsize=10,
        family="monospace",
    )
    figure.suptitle(
        f"VNav teacher pilot · {sample['case_id']}", fontsize=18, y=0.975
    )
    output = case_dir / "preview.png"
    figure.savefig(output, dpi=120)
    plt.close(figure)
    return output


def _font(size: int) -> ImageFont.ImageFont:
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def render_overview(
    review_dir: Path,
    cases: Sequence[Mapping[str, Any]],
    *,
    columns: int = 3,
) -> Path:
    thumbnail_width, thumbnail_height = 620, 570
    label_height = 52
    rows = int(np.ceil(len(cases) / columns))
    canvas = Image.new(
        "RGB",
        (columns * thumbnail_width, rows * (thumbnail_height + label_height)),
        "white",
    )
    draw = ImageDraw.Draw(canvas)
    font = _font(22)
    small_font = _font(17)
    for index, sample in enumerate(cases):
        row, col = divmod(index, columns)
        x = col * thumbnail_width
        y = row * (thumbnail_height + label_height)
        source = Image.open(review_dir / "cases" / sample["case_id"] / "preview.png").convert("RGB")
        source.thumbnail((thumbnail_width, thumbnail_height), Image.Resampling.LANCZOS)
        image_x = x + (thumbnail_width - source.width) // 2
        image_y = y + (thumbnail_height - source.height) // 2
        canvas.paste(source, (image_x, image_y))
        draw.text(
            (x + 8, y + thumbnail_height + 3),
            f"{sample['case_id']}  {sample['sub_task_id']}  {sample['map_name']}",
            fill=(20, 20, 20),
            font=font,
        )
        draw.text(
            (x + 8, y + thumbnail_height + 29),
            f"ts={sample['meta_ts']:.3f}  collision={sample['rollout']['collision']}",
            fill=(75, 75, 75),
            font=small_font,
        )
    output = review_dir / "overview.png"
    canvas.save(output, format="PNG", optimize=True)
    return output
