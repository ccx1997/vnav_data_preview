"""Read-only adapters and atomic pixel-manifest export; no teacher dependency."""
from collections import Counter
import json
from pathlib import Path
import shutil

import numpy as np

from .geometry import ASSETS, FRAME, HanddrawMapper, digest, validate_map, world_to_cartesian
from .source_map import SOURCE_MAP_ASSETS, SourceMapMapper

FIELDS = {
    "reference_pose": "handdraw_pixel_xy",
    "raw_poses": "handdraw_raw_pixel_xy",
    "route": "handdraw_route_pixel_xy",
}
VERSION = "vnav_pixels_v1"
SOURCE_MAP_VERSION = "vnav_pixels_source_map_v2"


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
    def __init__(self, maps, *, assets=ASSETS, sources=None, handdraw_map="legacy",
                 source_map_assets=SOURCE_MAP_ASSETS):
        if handdraw_map not in ("legacy", "source-map"):
            raise ValueError("handdraw_map must be legacy or source-map")
        self.specs = maps
        self.maps = {}
        self.sources = sources if sources is not None else Sources()
        self.assets = Path(assets)
        self.handdraw = None
        self.handdraw_mode = handdraw_map
        self.handdraw_name = "source_map" if handdraw_map == "source-map" else "handdraw_pmap"
        self.version = SOURCE_MAP_VERSION if handdraw_map == "source-map" else VERSION
        self.source_map_assets = Path(source_map_assets)

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
                if self.handdraw_mode == "source-map":
                    for filename in ("registration.json", "source_map.png"):
                        self.sources.track(self.source_map_assets / filename)
                    self.handdraw = SourceMapMapper(self.assets, self.source_map_assets)
                else:
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
        if self.handdraw_mode == "source-map" and name != "P_map":
            raise ValueError("source-map only supports P_map; no registration exists for this map")
        meta = self.map_metadata(name)
        result = dict(row, coordinate_frame=FRAME, pixel_data_version=self.version,
                      position_resolution_m=meta["resolution_m"],
                      handdraw_map=self.handdraw_name if name == "P_map" else None)
        if self.handdraw_mode == "source-map":
            result["handdraw_point_valid"] = {} if name == "P_map" else None
            result["handdraw_route_segment_valid"] = None
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
                if self.handdraw_mode == "source-map":
                    valid = self.handdraw.road_validity(mapped).tolist()
                    result["handdraw_point_valid"][key] = valid[0] if key == "reference_pose" else valid
                    if key == "route":
                        result["handdraw_route_segment_valid"] = self.handdraw.route_segment_validity(mapped)
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


def filter_source_map_row(row):
    """Drop unsupported observations; truncate the route at its first invalidity.

    Keep the history/time/command contract by dropping the entire sample if any
    history pose is unsupported. Only a route prefix is cropped, never rerouted.
    """
    valid = row.get("handdraw_point_valid")
    if valid is None:
        return None
    if not valid.get("reference_pose", True):
        return "reference_outside_drawn_road"
    if not all(valid.get("raw_poses", [])):
        return "history_outside_drawn_road"
    if "route" in valid:
        count = len(valid["route"])
        keep = next((i for i, ok in enumerate(valid["route"]) if not ok), count)
        for i, ok in enumerate(row["handdraw_route_segment_valid"]):
            if not ok:
                keep = min(keep, i + 1)
                break
        if count and not keep:
            return "route_starts_outside_drawn_road"
        row["source_map_route_trim"] = dict(original_points=count, retained_points=keep)
        row["route"] = row["route"][:keep]
        row["handdraw_route_pixel_xy"] = row["handdraw_route_pixel_xy"][:keep]
        valid["route"] = valid["route"][:keep]
        row["handdraw_route_segment_valid"] = row["handdraw_route_segment_valid"][:max(0, keep - 1)]
    return None


def export_pixels(output, maps, *, inputs=(), run=None, assets=ASSETS,
                  handdraw_map="legacy", source_map_assets=SOURCE_MAP_ASSETS):
    """Export explicit JSONL files OR one completed teacher run into a new directory."""
    if bool(inputs) == (run is not None):
        raise ValueError("specify either input JSONL files or one teacher run")
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("output must be a new directory; refusing overwrite")
    source_roots = [Path(run).resolve()] if run is not None else [Path(p).resolve().parent for p in inputs]
    asset_roots = [Path(assets).resolve(), Path(source_map_assets).resolve()]
    if any(output.resolve().is_relative_to(root) for root in source_roots + asset_roots):
        raise ValueError("output must be outside the source and asset directories")
    names = [Path(p).name for p in inputs]
    if len(set(names)) != len(names) or any(not name.endswith(".jsonl") for name in names):
        raise ValueError("input names must be unique .jsonl filenames")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_name(output.name + ".building")
    staging.mkdir(exist_ok=False)
    try:
        sources = Sources()
        converter = PixelConverter(maps, assets=assets, sources=sources, handdraw_map=handdraw_map,
                                   source_map_assets=source_map_assets)
        streams = [(Path(path).name, sources.rows(path)) for path in inputs]
        if run is not None:
            streams = [("samples.jsonl", teacher_rows(run, sources))]
        counts, distances = {}, []
        road_counts = Counter()
        input_counts, exclusions, exclusion_reasons = {}, [], Counter()
        trimmed_routes = 0
        for name, rows in streams:
            map_counts = Counter()
            input_map_counts = Counter()
            with (staging / name).open("x") as stream:
                for number, row in enumerate(rows, 1):
                    input_map_counts[row["map_name"]] += 1
                    if handdraw_map == "source-map" and row["map_name"] != "P_map":
                        reason = "map_not_supported_by_source_map"
                        exclusions.append(dict(input=name, row=number, case_id=row.get("case_id"),
                                               map_name=row["map_name"], reason=reason))
                        exclusion_reasons[reason] += 1
                        continue
                    try:
                        converted, distance = converter.convert_row(row)
                    except (ValueError, KeyError, TypeError) as exc:
                        raise ValueError(f"{name}: row {number}: {exc}") from exc
                    distances.extend(distance)
                    if converted.get("handdraw_point_valid") is not None:
                        for field, valid in converted["handdraw_point_valid"].items():
                            valid = [valid] if field == "reference_pose" else valid
                            road_counts["points"] += len(valid)
                            road_counts["invalid_points"] += sum(not value for value in valid)
                        valid = converted["handdraw_route_segment_valid"] or []
                        road_counts["route_segments"] += len(valid)
                        road_counts["invalid_route_segments"] += sum(not value for value in valid)
                    if handdraw_map == "source-map":
                        reason = filter_source_map_row(converted)
                        if reason:
                            exclusions.append(dict(input=name, row=number, case_id=row.get("case_id"),
                                                   map_name=row["map_name"], reason=reason))
                            exclusion_reasons[reason] += 1
                            continue
                        trim = converted.get("source_map_route_trim", {})
                        trimmed_routes += trim.get("retained_points", 0) < trim.get("original_points", 0)
                    stream.write(json.dumps(converted, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
                    map_counts[row["map_name"]] += 1
            counts[name] = dict(total=sum(map_counts.values()), maps=dict(map_counts))
            input_counts[name] = dict(total=sum(input_map_counts.values()), maps=dict(input_map_counts))
        if not any(item["total"] for item in input_counts.values()):
            raise ValueError("no coordinate rows to export")
        handdraw = converter.handdraw.prepare_image(staging / converter.handdraw_name) if converter.handdraw else None
        schema = dict(
            version=converter.version, coordinate_frame=FRAME, origin="center of bottom-left pixel",
            axes="x_right_y_up", position_unit="pixel", coordinate_fields=list(FIELDS),
            formula="xy_px=(xy_world-origin_xy)/resolution_m-0.5",
            image_column="x_px", image_row="height-1-y_px",
            yaw="radians in source world frame, unchanged; handdraw fields contain XY only",
            untouched_units="commands m/s, rad/s; timestamps seconds; other fields retain source units",
            source_artifacts="source_* and inputs_npz_path are read-only provenance in original coordinates; not pixel tensors",
            maps=converter.maps, handdraw=handdraw, handdraw_non_pmap=None,
            handdraw_image=f"{converter.handdraw_name}/aligned.png" if handdraw else None,
            teacher_adapter=dict(reference_pose="sample.grid_pose", raw_poses="inputs.npz.teacher_history_pose",
                                 route="inputs.npz.forward_route") if run is not None else None,
        )
        if handdraw_map == "source-map":
            schema.update(handdraw_mode=handdraw_map, map_scope=["P_map"],
                          handdraw_road_mask="source_map/road_mask.png" if handdraw else None,
                          handdraw_point_valid="per present input field: bool for reference_pose, list for raw_poses/route; null for non-P_map",
                          handdraw_route_segment_valid="N-1 booleans for consecutive mapped route points; [] for N<2; null when absent/non-P_map",
                          invalid_policy="exclude non-P_map before conversion; drop sample if reference/history is off-road or nonempty route starts off-road; trim route at first off-road point/segment; retain temporal history and commands verbatim; no rerouting",
                          action_labels="source teacher commands remain unchanged; route trimming does not revalidate future actions against source-map topology")
            with (staging / "excluded_rows.jsonl").open("x") as stream:
                for excluded in exclusions:
                    stream.write(json.dumps(excluded, ensure_ascii=False) + "\n")
        save_json(staging / "coordinate_schema.json", schema)
        sources.verify()
        stats = dict(count=len(distances), median=None, p95=None, max=None)
        if distances:
            stats.update(zip(("median", "p95", "max"), np.percentile(distances, [50, 95, 100]).tolist()))
        audit = dict(version=converter.version, counts=counts, source_sha256=sources.hashes,
                     source_unchanged=True, projection_distance_m=stats,
                     projection_statistics="all mapped point occurrences, including repeated history/route points",
                     output_sha256={str(p.relative_to(staging)): digest(p) for p in sorted(staging.rglob("*")) if p.is_file()})
        if handdraw_map == "source-map":
            audit["road_validity"] = {key: road_counts[key] for key in (
                "points", "invalid_points", "route_segments", "invalid_route_segments")}
            audit["road_validity"]["scope"] = "all input mapped points before filtering/trimming"
            audit.update(input_counts=input_counts, excluded_rows=len(exclusions),
                         exclusion_reasons=dict(exclusion_reasons), trimmed_routes=trimmed_routes)
        save_json(staging / "pixel_coordinate_audit.json", audit)
        if output.exists() or output.is_symlink():
            raise ValueError("output appeared during export; refusing overwrite")
        staging.rename(output)
        return audit
    except BaseException:
        shutil.rmtree(staging)
        raise
