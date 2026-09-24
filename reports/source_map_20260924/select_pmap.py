"""Correct this batch's scope by selecting existing P_map rows without remapping."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import sys

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "processing/coordinate-converter"))
from vnav_coordinates.export import Sources, save_json
from vnav_coordinates.geometry import digest

SOURCE = Path("/mnt/chengchangxu/data/visual_nav_training/_source_map_exports/20260924_source_map")
OUTPUT = SOURCE.with_name("20260924_pmap_only")
STAGING = OUTPUT.with_name(OUTPUT.name + ".building")


def main():
    if OUTPUT.exists() or OUTPUT.is_symlink():
        raise ValueError("output already exists; refusing overwrite")
    STAGING.mkdir(exist_ok=False)
    sources = Sources()
    parent = sources.document(SOURCE / "summary.json")
    assert parent["status"] == "success"
    tasks, skipped, counts = [], [], Counter()
    try:
        for task in parent["tasks"]:
            source = Path(task["output"])
            audit = sources.document(source / "pixel_coordinate_audit.json")
            for relative, sha in audit["output_sha256"].items():
                path = source / relative
                assert path.resolve().is_relative_to(source.resolve())
                assert digest(sources.track(path)) == sha, f"damaged existing export: {path}"
            old_schema = sources.document(source / "coordinate_schema.json")
            scope_input = audit["input_counts"]["samples.jsonl"]["maps"].get("P_map", 0)
            counts["input_count"] += scope_input
            ignored = audit["input_counts"]["samples.jsonl"]["total"] - scope_input
            counts["out_of_scope_count"] += ignored
            if not scope_input:
                skipped.append(dict(task=task["task"], reason="no_P_map_samples", samples=ignored))
                continue
            relative = Path(task["task"]) / Path(task["run"]).name
            target, published = STAGING / relative, OUTPUT / relative
            target.mkdir(parents=True)
            excluded = list(sources.rows(source / "excluded_rows.jsonl"))
            retained, scoped_out, trimmed = 0, 0, 0
            expected_bytes = hashlib.sha256()
            with (source / "samples.jsonl").open("rb") as reader, (target / "samples.jsonl").open("xb") as writer:
                for number, line in enumerate(reader, 1):
                    row = json.loads(line)
                    if row["map_name"] != "P_map":
                        scoped_out += 1
                        excluded.append(dict(input=str(source / "samples.jsonl"), row=number,
                                             case_id=row["case_id"], map_name=row["map_name"],
                                             reason="map_not_supported_by_source_map"))
                        continue
                    assert row["handdraw_map"] == "source_map"
                    assert row["handdraw_point_valid"]["reference_pose"]
                    assert all(row["handdraw_point_valid"]["raw_poses"])
                    assert all(row["handdraw_point_valid"]["route"])
                    assert all(row["handdraw_route_segment_valid"])
                    writer.write(line)
                    expected_bytes.update(line)
                    retained += 1
                    trim = row["source_map_route_trim"]
                    trimmed += trim["retained_points"] < trim["original_points"]
            assert scoped_out == ignored
            assert digest(target / "samples.jsonl") == expected_bytes.hexdigest()
            assert retained == task["validation"]["maps"].get("P_map", 0)
            shutil.copytree(source / "source_map", target / "source_map")
            schema = dict(old_schema, map_scope=["P_map"], maps={"P_map": old_schema["maps"]["P_map"]},
                          scope_note="P_map only; reused existing mapped rows byte-for-byte; no coordinate recomputation",
                          invalid_policy="exclude non-P_map; " + old_schema["invalid_policy"])
            save_json(target / "coordinate_schema.json", schema)
            with (target / "excluded_rows.jsonl").open("x") as stream:
                for row in excluded:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            new_audit = {key: value for key, value in audit.items()
                         if key not in {"source_sha256", "output_sha256", "counts", "excluded_rows", "exclusion_reasons"}}
            new_audit.update(
                operation="select_P_map_from_existing_pixel_export", map_scope=["P_map"],
                counts={"samples.jsonl": dict(total=retained, maps={"P_map": retained})},
                excluded_rows=len(excluded), exclusion_reasons=dict(Counter(row["reason"] for row in excluded)),
                upstream_source_audit=str(source / "pixel_coordinate_audit.json"),
                source_sha256={str(path): sources.hashes[str(path.resolve())] for path in
                               [SOURCE / "summary.json", source / "pixel_coordinate_audit.json"]
                               + [source / name for name in audit["output_sha256"]]},
                source_unchanged=True, retained_manifest_bytes_unchanged=True,
                output_sha256={str(path.relative_to(target)): digest(path)
                               for path in target.rglob("*") if path.is_file()})
            assert retained + len(excluded) == audit["input_counts"]["samples.jsonl"]["total"]
            save_json(target / "pixel_coordinate_audit.json", new_audit)
            pmap_excluded = scope_input - retained
            counts.update(retained=retained, excluded=pmap_excluded, trimmed_routes=trimmed)
            tasks.append(dict(task=task["task"], run=task["run"], output=str(published), status="success",
                              input_count=scope_input, retained=retained, excluded=pmap_excluded,
                              out_of_scope_count=ignored, trimmed_routes=trimmed,
                              samples=str(published / "samples.jsonl"), schema=str(published / "coordinate_schema.json"),
                              validation=dict(passed=True, maps={"P_map": retained},
                                              method="all P_map manifest lines reused byte-for-byte; upstream export hashes verified",
                                              upstream_audit=str(source / "pixel_coordinate_audit.json"),
                                              gray_points_checked=task["validation"]["gray_points_checked"],
                                              gray_segments_checked=task["validation"]["gray_segments_checked"])))
        sources.verify()
        assert counts["retained"] == 10245 and counts["out_of_scope_count"] == 1184
        summary = dict(status="success", version=parent["version"], map_scope=["P_map"],
                       output=str(OUTPUT), reused_source_batch=str(SOURCE),
                       operation="scope selection only; no remapping, downloads, teacher runs or source edits",
                       totals=dict(counts), retained_maps={"P_map": counts["retained"]},
                       tasks=tasks, skipped_tasks=skipped, source_unchanged=True,
                       retained_manifest_bytes_unchanged=True)
        save_json(STAGING / "summary.json", summary)
        save_json(STAGING / "source_export_protection.json", dict(source_unchanged=True, sha256=sources.hashes))
        STAGING.rename(OUTPUT)
        save_json(Path(__file__).with_name("pmap_only_result.json"), summary)
        print(json.dumps(dict(output=str(OUTPUT), tasks=len(tasks), skipped_tasks=len(skipped),
                              totals=dict(counts), source_files_checked=len(sources.hashes)), ensure_ascii=False))
    except BaseException:
        shutil.rmtree(STAGING)
        raise


if __name__ == "__main__":
    main()
