import json
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
from PIL import Image
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vnav_coordinates import HanddrawMapper, SourceMapMapper
from vnav_coordinates._paired_roads import _sample
from vnav_coordinates.export import SOURCE_MAP_VERSION, PixelConverter, export_pixels, filter_source_map_row
from vnav_coordinates.geometry import digest
from vnav_coordinates.source_map import SOURCE_MAP_ASSETS
from remap_existing import latest_runs, validate_export
from test_coordinates import teacher_fixture


@pytest.fixture(scope="module")
def mapper():
    return SourceMapMapper()


def world_on_edge(mapper, edge_id, progress):
    edge = next(e for e in mapper.mapper.edges if e["id"] == edge_id)
    x, y = _sample(edge["pmap"], progress)
    meta = mapper.meta["training_pmap"]
    return [(x + .5) * .1 + meta["origin_xy"][0],
            (meta["height"] - .5 - y) * .1 + meta["origin_xy"][1], 7.]


def test_scale_centers_and_no_junction_plateau(mapper):
    np.testing.assert_allclose(mapper.original_to_aligned([[0, 0], [1440, 1078]]),
                               [[(1642/1079-1)/2, (2192/1441-1)/2],
                                [1641-(1642/1079-1)/2, 2191-(2192/1441-1)/2]])
    # Within the legacy 4m junction plateau, different world positions must
    # now retain their progress. Both targets remain on the new gray stroke.
    points = [world_on_edge(mapper, "23-25", t) for t in [.001, .01, .02]]
    new, _ = mapper.map_world(points)
    old, _ = HanddrawMapper().map_world(points)
    np.testing.assert_allclose(old, np.repeat(old[:1], 3, axis=0))
    assert np.all(np.linalg.norm(np.diff(new, axis=0), axis=1) > 1)
    assert mapper.road_validity(new).all()
    assert mapper.map_many([])[0].shape == (0, 2)


def test_main_road_coverage_and_deleted_entrance(mapper):
    points = [world_on_edge(mapper, e["id"], t) for e in mapper.mapper.edges
              if e["id"] not in {"1-23", "3-25", "5-27", "7-29", "9-31", "11-33",
                                 "13-35", "15-41", "17-45", "19-47", "21-43"}
              for t in np.linspace(0, 1, 101)]
    mapped, _ = mapper.map_world(points)
    assert mapper.road_validity(mapped).all()
    entrance, _ = mapper.map_world([world_on_edge(mapper, "1-23", 0)])
    assert not mapper.road_validity(entrance).any()


def test_cached_mapping_preserves_exact_order_values_and_ownership(mapper):
    points = np.array([[843.125, 1546.5], [800.01, 1500.02], [843.125, 1546.5]])
    expected, distances = mapper._map_uncached(points)
    actual, actual_distances = mapper.map_many(points)
    np.testing.assert_array_equal(actual, expected)
    np.testing.assert_array_equal(actual_distances, distances)
    actual[:] = -999
    again, _ = mapper.map_many(points[::-1])
    np.testing.assert_array_equal(again, expected[::-1])


def test_image_and_three_gaps_are_preserved(mapper, tmp_path):
    out = tmp_path / "map"
    meta = mapper.prepare_image(out)
    original = np.array(Image.open(SOURCE_MAP_ASSETS / "source_map.png"))
    actual = np.array(Image.open(out / "aligned.png"))
    np.testing.assert_array_equal(actual, np.rot90(original))
    assert actual.shape[:2] == (2192, 1642)
    np.testing.assert_array_equal(np.array(Image.open(out / "road_mask.png")) > 0,
                                  np.rot90(original[..., 0] == 192))
    assert meta["png_sha256"] == digest(out / "aligned.png")
    # Original PNG raster (u,v) becomes aligned Cartesian (v,u).
    for endpoints in ([[1180, 852], [1335, 852]],
                      [[1529, 1150], [1531, 1215]],
                      [[1479, 600], [1590, 588]]):
        xy = np.asarray(endpoints)[:, ::-1]
        assert mapper.road_validity(xy).all()
        assert mapper.route_segment_validity(xy) == [False]
    assert mapper.route_segment_validity([[852, 1100], [852, 1180]]) == [True]
    assert not mapper.road_validity([[-1, 0], [1642, 0], [0, 2192]]).any()


def test_filter_drops_history_and_truncates_without_reconnecting():
    row = dict(handdraw_point_valid=dict(reference_pose=True, raw_poses=[True], route=[True]*4),
               route=[[i, 0] for i in range(4)], handdraw_route_pixel_xy=[[i, 1] for i in range(4)],
               handdraw_route_segment_valid=[True, False, True])
    assert filter_source_map_row(row) is None
    assert row["route"] == [[0, 0], [1, 0]]
    assert row["handdraw_route_pixel_xy"] == [[0, 1], [1, 1]]
    assert row["handdraw_route_segment_valid"] == [True]
    assert row["source_map_route_trim"] == dict(original_points=4, retained_points=2)
    row["handdraw_point_valid"]["raw_poses"] = [False]
    assert filter_source_map_row(row) == "history_outside_drawn_road"


def test_cli_export_filter_trim_mixed_maps_and_protection(mapper, tmp_path):
    # No B10 metadata is needed: out-of-scope rows must be excluded before loading maps.
    maps = {"P_map": mapper.meta["training_pmap"]}
    on_road = world_on_edge(mapper, "23-25", .1)
    junction = world_on_edge(mapper, "1-23", 1)
    entrance = world_on_edge(mapper, "1-23", 0)
    source = tmp_path / "input" / "world.jsonl"
    source.parent.mkdir()
    common = dict(map_name="P_map", reference_pose=on_road, raw_poses=[on_road],
                  raw_pose_stamps_s=[1], commands=[dict(linear_mps=.5)], route=[junction, entrance])
    rows = [dict(common, case_id="keep"), dict(common, case_id="entrance", reference_pose=entrance),
            dict(common, case_id="history", raw_poses=[entrance]),
            dict(map_name="B10_map", reference_pose=[.05, .05, 7.], route=[])]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))
    before = digest(source)
    config = tmp_path / "maps.json"
    config.write_text(json.dumps(maps))
    output = tmp_path / "new"
    result = subprocess.run([sys.executable, str(ROOT / "export_pixels.py"), "--input", str(source),
                             "--output", str(output), "--maps-json", str(config),
                             "--handdraw-map", "source-map"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    kept = [json.loads(line) for line in (output / source.name).read_text().splitlines()]
    assert len(kept) == 1 and kept[0]["handdraw_map"] == "source_map"
    assert kept[0]["pixel_data_version"] == SOURCE_MAP_VERSION
    assert kept[0]["reference_pose"][2] == 7
    assert kept[0]["commands"] == common["commands"]
    assert kept[0]["raw_pose_stamps_s"] == [1]
    assert len(kept[0]["route"]) == len(kept[0]["handdraw_route_pixel_xy"]) == 1
    schema = json.loads((output / "coordinate_schema.json").read_text())
    assert schema["map_scope"] == ["P_map"] and set(schema["maps"]) == {"P_map"}
    audit = json.loads((output / "pixel_coordinate_audit.json").read_text())
    assert audit["input_counts"][source.name]["total"] == 4
    assert audit["counts"][source.name]["total"] == 1
    assert audit["excluded_rows"] == 3 and audit["trimmed_routes"] == 1
    assert audit["exclusion_reasons"]["map_not_supported_by_source_map"] == 1
    with pytest.raises(ValueError, match="only supports P_map"):
        PixelConverter(maps, handdraw_map="source-map").convert_row(rows[-1])
    assert digest(source) == before and audit["source_unchanged"]
    for relative, sha in audit["output_sha256"].items():
        assert digest(output / relative) == sha
    with pytest.raises(ValueError, match="new directory"):
        export_pixels(output, maps, inputs=[source], handdraw_map="source-map")
    # No valid observations is a valid, auditable empty export, not a source error.
    source.write_text(json.dumps(dict(common, reference_pose=entrance)) + "\n")
    empty_audit = export_pixels(tmp_path / "empty", maps, inputs=[source], handdraw_map="source-map")
    assert empty_audit["excluded_rows"] == 1 and empty_audit["counts"][source.name]["total"] == 0


def test_changed_asset_is_rejected_without_output(mapper, tmp_path):
    assets = tmp_path / "assets"
    shutil.copytree(SOURCE_MAP_ASSETS, assets)
    with (assets / "source_map.png").open("ab") as stream:
        stream.write(b"changed")
    source = tmp_path / "input" / "world.jsonl"
    source.parent.mkdir()
    source.write_text(json.dumps(dict(map_name="P_map", reference_pose=[0, 0, 0])) + "\n")
    with pytest.raises(ValueError, match="checksum"):
        export_pixels(tmp_path / "bad", {"P_map": mapper.meta["training_pmap"]}, inputs=[source],
                      handdraw_map="source-map", source_map_assets=assets)
    assert not (tmp_path / "bad").exists() and not (tmp_path / "bad.building").exists()


def test_batch_selects_latest_successful_run_and_validates(tmp_path):
    run, _ = teacher_fixture(tmp_path)
    task = tmp_path / "training" / "task1" / "vnav_teacher_rule10_history_v2" / "full"
    task.mkdir(parents=True)
    for name in ("run_20260901", "run_20260902", "run_20260903"):
        shutil.copytree(run, task / name)
    (task / "run_20260903" / "run.json").write_text(json.dumps(dict(status="failed", accepted_count=1)))
    selected = latest_runs(tmp_path / "training")
    assert selected == [("task1", task / "run_20260902")]
    output = tmp_path / "pixels"
    maps = {"B10_map": dict(width=100, height=200, resolution_m=.05, origin_xy=[-1., -2.])}
    export_pixels(output, maps, run=selected[0][1], handdraw_map="source-map")
    result = validate_export(output, selected[0][1])
    assert result["passed"] and result["retained"] == 0 and result["excluded"] == 1
    assert result["maps"] == {} and result["gray_points_checked"] == 0
    with (selected[0][1] / "run.json").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="source changed"):
        validate_export(output, selected[0][1])
