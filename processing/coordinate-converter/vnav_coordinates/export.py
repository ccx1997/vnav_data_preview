"""Read-only adapters and atomic pixel-manifest export; no teacher dependency."""
from collections import Counter
import json
from pathlib import Path
import shutil

import numpy as np

from .geometry import ASSETS, FRAME, HanddrawMapper, digest, validate_map, world_to_cartesian

FIELDS = {
    "reference_pose": "handdraw_pixel_xy",
    "raw_poses": "handdraw_raw_pixel_xy",
    "route": "handdraw_route_pixel_xy",
}
VERSION = "vnav_pixels_v1"


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


class Sources:
    def __init__(self):
        self.hashes = {}

    def track(self, path):
        path = Path(path).resolve()
        value = digest(path)
        if str(path) in self.hashes and self.hashes[str(path)] != value:
            raise ValueError(f"source changed during export: {path}")
        self.hashes[str(path)] = value
        return path

    def document(self, path):
        return json.loads(self.track(path).read_text())

    def rows(self, path):
        path = self.track(path)
        with path.open() as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("expected a JSON object")
                except ValueError as exc:
                    raise ValueError(f"{path}:{number}: {exc}") from exc
                yield row

    def verify(self):
        for name, value in self.hashes.items():
            if digest(name) != value:
                raise ValueError(f"source changed during export: {name}")


class PixelConverter:
    def __init__(self, maps, *, assets=ASSETS, sources=None):
        self.specs = maps
        self.maps = {}
        self.sources = sources if sources is not None else Sources()
        self.assets = Path(assets)
        self.handdraw = None

    def map_metadata(self, name):
        if name not in self.maps:
            if name not in self.specs:
                raise ValueError(f"map not configured: {name}")
            spec = self.specs[name]
            if isinstance(spec, (str, Path)):
                path = self.sources.track(spec)
                with np.load(path, allow_pickle=False) as archive:
                    height, width = archive["occupancy"].shape
                    meta = dict(width=width, height=height,
                                origin_xy=archive["origin_xy"].tolist(),
                                resolution_m=float(archive["resolution_m"]),
                                path=str(path), sha256=self.sources.hashes[str(path)])
            else:
                meta = dict(spec)
            validate_map(meta)
            if name == "P_map":
                for filename in ("metadata.json", "data/roads.json", "original.jpg"):
                    self.sources.track(self.assets / filename)
                self.handdraw = HanddrawMapper(self.assets)
                self.handdraw.validate_pmap(meta)
            self.maps[name] = meta
        return self.maps[name]

    def convert_row(self, row):
        frame = row.get("coordinate_frame")
        if frame not in (None, "world", "world_m") or any(k in row for k in FIELDS.values()):
            raise ValueError("input must be world coordinates; refusing a second pixel conversion")
        if not any(key in row for key in FIELDS):
            raise ValueError("row has no reference_pose, raw_poses or route")
        name = row["map_name"]
        meta = self.map_metadata(name)
        result = dict(row, coordinate_frame=FRAME, pixel_data_version=VERSION,
                      position_resolution_m=meta["resolution_m"],
                      handdraw_map="handdraw_pmap" if name == "P_map" else None)
        distances = []
        for key, output_key in FIELDS.items():
            if key not in row:
                continue
            converted = world_to_cartesian(row[key], meta["origin_xy"], meta["resolution_m"])
            if ((key == "reference_pose" and (converted.ndim != 1 or not converted.size))
                    or (key != "reference_pose" and converted.ndim != 2)):
                raise ValueError(f"{key}: expected {'one pose' if key == 'reference_pose' else 'a point/pose sequence'}")
            result[key] = converted.tolist()
            result[output_key] = None
            if name == "P_map":
                mapped, distance = self.handdraw.map_many(converted[..., :2])
                result[output_key] = mapped[0].tolist() if key == "reference_pose" else mapped.tolist()
                distances.extend(distance.tolist())
        return result, distances


def teacher_rows(run, sources):
    """Adapt accepted current-pipeline cases to the reference pose-history schema.

    Only the three explicit geometry fields are pixelized. All other values are
    retained in their original units or referenced through source_* provenance.
    """
    run = Path(run).resolve()
    info = sources.document(run / "run.json")
    if info.get("status") != "success" or info.get("accepted_count", 0) <= 0:
        raise ValueError("teacher run must be successful with accepted samples")
    count, seen = 0, set()
    for label in sources.rows(run / "teacher_labels.jsonl"):
        if label.get("status") != "accepted":
            continue
        case_id = label.get("case_id")
        if not isinstance(case_id, str) or case_id in ("", ".", "..") or Path(case_id).name != case_id:
            raise ValueError("accepted teacher label has invalid/missing case_id")
        if case_id in seen:
            raise ValueError(f"duplicate accepted case_id: {case_id}")
        seen.add(case_id)
        case = run / "cases" / case_id
        sample = sources.document(case / "sample.json")
        for key in ("case_id", "status", "map_name", "grid_pose", "route_id"):
            if sample.get(key) != label.get(key):
                raise ValueError(f"{case_id}: sample and teacher label disagree on {key}")
        pose = sample["grid_pose"]
        path = sources.track(case / "inputs.npz")
        with np.load(path, allow_pickle=False) as archive:
            history = archive["teacher_history_pose"]
            stamps = archive["teacher_history_stamp_s"]
            route = archive["forward_route"]
        expected = sample["teacher_history"]
        expected_poses, expected_stamps = np.asarray(expected["poses"]), np.asarray(expected["stamps_s"])
        if (history.shape != expected_poses.shape or stamps.shape != expected_stamps.shape
                or history.ndim != 2 or history.shape[1] != 3 or stamps.shape != (len(history),)
                or not np.isfinite(stamps).all()
                or not np.allclose(history, expected_poses, rtol=0, atol=1e-6)
                or not np.allclose(stamps, expected_stamps, rtol=0, atol=1e-6)):
            raise ValueError(f"{case_id}: teacher history JSON/NPZ mismatch")
        count += 1
        yield dict(
            case_id=case_id, map_name=sample["map_name"], route_id=sample["route_id"],
            sub_task_id=sample["sub_task_id"], meta_ts=sample["meta_ts"],
            reference_pose=[pose["x"], pose["y"], pose["yaw_rad"]],
            raw_poses=history.tolist(), raw_pose_stamps_s=stamps.tolist(), route=route.tolist(),
            commands=sample["teacher"]["commands"], initial_state=sample["initial_state"],
            source_run=str(run), source_sample_json=str(case / "sample.json"),
            inputs_npz_path=str(path), source_rgb_history=sample.get("rgb_history"),
            source_artifacts=sample.get("artifacts"),
        )
    if count != info["accepted_count"]:
        raise ValueError(f"accepted_count mismatch: run={info['accepted_count']}, labels={count}")


def export_pixels(output, maps, *, inputs=(), run=None, assets=ASSETS):
    """Export explicit JSONL files OR one completed teacher run into a new directory."""
    if bool(inputs) == (run is not None):
        raise ValueError("specify either input JSONL files or one teacher run")
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("output must be a new directory; refusing overwrite")
    source_roots = [Path(run).resolve()] if run is not None else [Path(p).resolve().parent for p in inputs]
    if any(output.resolve().is_relative_to(root) for root in source_roots + [Path(assets).resolve()]):
        raise ValueError("output must be outside the source and asset directories")
    names = [Path(p).name for p in inputs]
    if len(set(names)) != len(names) or any(not name.endswith(".jsonl") for name in names):
        raise ValueError("input names must be unique .jsonl filenames")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_name(output.name + ".building")
    staging.mkdir(exist_ok=False)
    try:
        sources = Sources()
        converter = PixelConverter(maps, assets=assets, sources=sources)
        streams = [(Path(path).name, sources.rows(path)) for path in inputs]
        if run is not None:
            streams = [("samples.jsonl", teacher_rows(run, sources))]
        counts, distances = {}, []
        for name, rows in streams:
            map_counts = Counter()
            with (staging / name).open("x") as stream:
                for number, row in enumerate(rows, 1):
                    try:
                        converted, distance = converter.convert_row(row)
                    except (ValueError, KeyError, TypeError) as exc:
                        raise ValueError(f"{name}: row {number}: {exc}") from exc
                    stream.write(json.dumps(converted, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
                    map_counts[row["map_name"]] += 1
                    distances.extend(distance)
            counts[name] = dict(total=sum(map_counts.values()), maps=dict(map_counts))
        if not any(item["total"] for item in counts.values()):
            raise ValueError("no coordinate rows to export")
        handdraw = converter.handdraw.prepare_image(staging / "handdraw_pmap") if converter.handdraw else None
        schema = dict(
            version=VERSION, coordinate_frame=FRAME, origin="center of bottom-left pixel",
            axes="x_right_y_up", position_unit="pixel", coordinate_fields=list(FIELDS),
            formula="xy_px=(xy_world-origin_xy)/resolution_m-0.5",
            image_column="x_px", image_row="height-1-y_px",
            yaw="radians in source world frame, unchanged; handdraw fields contain XY only",
            untouched_units="commands m/s, rad/s; timestamps seconds; other fields retain source units",
            source_artifacts="source_* and inputs_npz_path are read-only provenance in original coordinates; not pixel tensors",
            maps=converter.maps, handdraw=handdraw, handdraw_non_pmap=None,
            handdraw_image="handdraw_pmap/aligned.png" if handdraw else None,
            teacher_adapter=dict(reference_pose="sample.grid_pose", raw_poses="inputs.npz.teacher_history_pose",
                                 route="inputs.npz.forward_route") if run is not None else None,
        )
        save_json(staging / "coordinate_schema.json", schema)
        sources.verify()
        stats = dict(count=len(distances), median=None, p95=None, max=None)
        if distances:
            stats.update(zip(("median", "p95", "max"), np.percentile(distances, [50, 95, 100]).tolist()))
        audit = dict(version=VERSION, counts=counts, source_sha256=sources.hashes,
                     source_unchanged=True, projection_distance_m=stats,
                     projection_statistics="all mapped point occurrences, including repeated history/route points",
                     output_sha256={str(p.relative_to(staging)): digest(p) for p in sorted(staging.rglob("*")) if p.is_file()})
        save_json(staging / "pixel_coordinate_audit.json", audit)
        if output.exists() or output.is_symlink():
            raise ValueError("output appeared during export; refusing overwrite")
        staging.rename(output)
        return audit
    except BaseException:
        shutil.rmtree(staging)
        raise
