import copy
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
from PIL import Image
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vnav_coordinates import HanddrawMapper, pmap_pixel_to_handdraw_pixel, world_to_cartesian
from vnav_coordinates.export import PixelConverter, Sources, export_pixels, teacher_rows
from vnav_coordinates.geometry import ASSETS, digest


@pytest.fixture(scope="module")
def mapper():
    return HanddrawMapper()


@pytest.fixture
def maps(mapper):
    return {"P_map": mapper.meta["training_pmap"],
            "B10_map": dict(width=100, height=200, resolution_m=.05, origin_xy=[-1., -2.])}


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_world_centers_shape_and_yaw():
    poses = np.array([[-9.95, 20.05, 4 * np.pi], [-10., 20., -np.pi]])
    original = poses.copy()
    result = world_to_cartesian(poses, [-10., 20.], .1)
    np.testing.assert_allclose(result[:, :2], [[0, 0], [-.5, -.5]], atol=1e-12)
    np.testing.assert_array_equal(result[:, 2], poses[:, 2])
    np.testing.assert_array_equal(poses, original)
    assert world_to_cartesian([], [0, 0], .1).shape == (0, 2)


@pytest.mark.parametrize("values,origin,resolution", [
    ([float("nan"), 0], [0, 0], .1), ([0, 0], [0, float("inf")], .1),
    ([0, 0], [0, 0], 0), ([0, 0], [0, 0], -1),
    ([0, 0], [0, 0], float("nan")), ([0, 0, 0, 0], [0, 0], .1),
    ([0, 0], [0], .1), (4., [0, 0], .1),
])
def test_invalid_world_coordinates(values, origin, resolution):
    with pytest.raises(ValueError):
        world_to_cartesian(values, origin, resolution)


def test_mapper_matches_independent_scalar_projection(mapper):
    rng = np.random.default_rng(918)
    # More than 128 points exercises chunk boundaries; include road nodes and OOB.
    raster = np.vstack((rng.uniform([-100, -100], [1800, 3000], size=(259, 2)),
                        [edge["pmap"][0][0] for edge in mapper.mapper.edges]))
    pixels = raster.copy()
    pixels[:, 1] = mapper.mapper.meta["height"] - 1 - pixels[:, 1]
    result, distances = mapper.map_many(pixels)
    expected = [mapper.mapper.map(*point, input_frame="pixel", details=True) for point in raster]
    aligned = mapper.original_to_aligned([point["jpg_pixel"] for point in expected])
    np.testing.assert_allclose(result, aligned, rtol=0, atol=1e-9)
    np.testing.assert_allclose(distances, [point["distance_m"] for point in expected], rtol=0, atol=1e-9)
    np.testing.assert_allclose(pmap_pixel_to_handdraw_pixel(*pixels[0]), result[0])
    empty, distance = mapper.map_many([])
    assert empty.shape == (0, 2) and distance.shape == (0,)
    for invalid in ([1, 2, 3, 4], [[0, float("inf")]], [[1e308, 1e308]]):
        with pytest.raises(ValueError):
            mapper.map_many(invalid)


def test_junction_plateau_arc_progress_and_tie(mapper):
    # Analytic two-edge fixture: first road goes east, JPG road goes north.
    synthetic = copy.copy(mapper)
    synthetic.mapper = copy.copy(mapper.mapper)
    synthetic.mapper.meta = {"height": 101}
    synthetic.mapper.resolution = .1
    synthetic.mapper.nodes = {1: {"jpg": [10, 20]}, 2: {"jpg": [10, 220]},
                              3: {"jpg": [110, 20]}, 4: {"jpg": [110, 220]}}
    synthetic.mapper.edges = [
        dict(start=1, end=2, clips=[40, 10], length=100, jpg=([(10, 20), (10, 220)], [0, 200])),
        dict(start=3, end=4, clips=[40, 10], length=100, jpg=([(110, 20), (110, 220)], [0, 200])),
    ]
    synthetic.segments = np.asarray([[0, 0, 50, 100, 0, 100, 10000, 0],
                                     [1, 0, 52, 100, 0, 100, 10000, 0]])
    synthetic.matrix = np.array([[1, 0, 0], [0, 1, 0]])
    values, distance = synthetic.map_many([[0, 50], [40, 50], [65, 50], [90, 50], [65, 49]])
    np.testing.assert_allclose(values, [[10, 20], [10, 20], [10, 120], [10, 220], [10, 120]])
    np.testing.assert_allclose(distance, [0, 0, 0, 0, .1])


def test_road_topology_and_snap_radii(mapper):
    # The reference uses JPG IDs; paired P_map IDs are one greater.
    connections = [{edge["start"] - 1, edge["end"] - 1} for edge in mapper.mapper.edges]
    assert {37, 35} in connections and {35, 47} not in connections
    for edge in mapper.mapper.edges:
        for node, clip in zip((edge["start"], edge["end"]), edge["clips"]):
            degree = mapper.mapper.nodes[node]["degree"]
            expected = 4. if degree >= 3 else 1. if degree == 1 else 1.5
            assert clip * mapper.mapper.resolution == pytest.approx(min(expected, .45 * edge["length"] * .1))


def test_prepared_image_exact_alignment(mapper, tmp_path):
    target = tmp_path / "map"
    meta = mapper.prepare_image(target)
    with Image.open(target / "aligned.png") as actual, Image.open(ASSETS / "original.jpg") as original:
        assert actual.size == (1619, 2162)
        # Independent output-raster -> original-raster inverse transform.
        expected = original.convert("RGB").transform(
            actual.size, Image.Transform.AFFINE, (0, -2/3, 1441, 2/3, 0, 0),
            Image.Resampling.BICUBIC, fillcolor="white")
        # Swapping interpolation axes changes rounding at a few sharp edges,
        # but must not introduce a geometric shift or scale difference.
        difference = np.abs(np.asarray(actual, dtype=int) - np.asarray(expected, dtype=int))
        assert np.count_nonzero(difference) / difference.size < 1e-4
        assert difference.max() <= 10
    np.testing.assert_allclose(mapper.original_to_aligned([[0, 0], [1440, 1078]]),
                               [[.25, .75], [1617.25, 2160.75]])
    assert meta["png_sha256"] == digest(target / "aligned.png")
    with pytest.raises(FileExistsError):
        mapper.prepare_image(target)


def test_jsonl_export_and_source_preservation(tmp_path, maps, mapper):
    source = tmp_path / "source" / "train_manifest.jsonl"
    row = dict(map_name="P_map", reference_pose=[0, 0, 4*np.pi], raw_poses=[[1, 2, .3]],
               route=[[0, 0], [1, 2]], commands=[dict(linear_mps=.5, angular_rps=.1)], map_annotation="manual")
    indoor = dict(map_name="B10_map", reference_pose=[-1, -2, .4], raw_poses=[], route=[])
    write_rows(source, [row, indoor])
    source_bytes = source.read_bytes()
    output = tmp_path / "pixels"
    audit = export_pixels(output, maps, inputs=[source])
    converted, converted_indoor = [json.loads(line) for line in (output / source.name).read_text().splitlines()]
    np.testing.assert_allclose(converted["handdraw_pixel_xy"], mapper.map_world(row["reference_pose"])[0][0])
    np.testing.assert_allclose(converted["handdraw_raw_pixel_xy"], mapper.map_world(row["raw_poses"])[0])
    np.testing.assert_allclose(converted["handdraw_route_pixel_xy"], mapper.map_world(row["route"])[0])
    assert converted["reference_pose"][2] == row["reference_pose"][2]
    assert converted["commands"] == row["commands"] and converted["map_annotation"] == "manual"
    assert converted_indoor["reference_pose"] == [-.5, -.5, .4]
    assert converted_indoor["handdraw_pixel_xy"] is None and converted_indoor["raw_poses"] == []
    assert audit["counts"][source.name] == dict(total=2, maps={"P_map": 1, "B10_map": 1})
    assert audit["projection_distance_m"]["count"] == 4
    assert audit["source_unchanged"] and source.read_bytes() == source_bytes
    for name, sha in audit["output_sha256"].items():
        assert digest(output / name) == sha
    with pytest.raises(ValueError, match="new directory"):
        export_pixels(output, maps, inputs=[source])
    with pytest.raises(ValueError, match="outside"):
        export_pixels(source.parent / "pixels", maps, inputs=[source])


def test_non_pmap_export_empty_distance_and_no_image(tmp_path, maps):
    source = tmp_path / "source" / "indoor.jsonl"
    write_rows(source, [dict(map_name="B10_map", route=[])])
    audit = export_pixels(tmp_path / "pixels", maps, inputs=[source])
    assert audit["projection_distance_m"] == dict(count=0, median=None, p95=None, max=None)
    assert not (tmp_path / "pixels/handdraw_pmap").exists()


@pytest.mark.parametrize("row", [
    dict(map_name="missing", route=[[0, 0]]),
    dict(map_name="B10_map", route=[[float("nan"), 0]]),
    dict(map_name="B10_map", route=[[0, 0]], coordinate_frame="cartesian_pixel"),
    dict(map_name="B10_map", reference_pose=[]),
    dict(map_name="B10_map", route=[0, 0]),
    dict(map_name="B10_map"),
])
def test_export_errors_leave_no_partial_output(tmp_path, maps, row):
    source = tmp_path / "source" / "input.jsonl"
    write_rows(source, [dict(map_name="B10_map", route=[[0, 0]]), row])
    before = digest(source)
    with pytest.raises(ValueError):
        export_pixels(tmp_path / "pixels", maps, inputs=[source])
    assert digest(source) == before
    assert not (tmp_path / "pixels").exists() and not (tmp_path / "pixels.building").exists()


def test_mismatched_pmap_rejected(maps):
    maps["P_map"] = dict(maps["P_map"], origin_xy=[0, 0])
    with pytest.raises(ValueError, match="differ"):
        PixelConverter(maps).convert_row(dict(map_name="P_map", route=[[0, 0]]))


def teacher_fixture(tmp_path):
    run = tmp_path / "teacher"
    case = run / "cases/sample_0000001"
    case.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps(dict(status="success", accepted_count=1)))
    sample = dict(case_id=case.name, status="accepted", map_name="B10_map", route_id="r1",
                  sub_task_id="task_1", meta_ts=100., grid_pose=dict(x=1., y=2., yaw_rad=.3),
                  teacher_history=dict(poses=[[.5, 1.5, .1]], stamps_s=[99.]),
                  teacher=dict(commands=[dict(linear_mps=.2, angular_rps=.1, duration_s=.5)]),
                  initial_state=dict(initial_linear_mps=.1), rgb_history=dict(cameras={}))
    (case / "sample.json").write_text(json.dumps(sample))
    write_rows(run / "teacher_labels.jsonl", [sample, dict(status="rejected", reject_reason="collision")])
    np.savez(case / "inputs.npz", teacher_history_pose=[[.5, 1.5, .1]], teacher_history_stamp_s=[99.],
             forward_route=[[1., 2.], [3., 4.]], teacher_rollout=[[0., 0., 0.]])
    return run, sample


def test_teacher_adapter_and_cli(tmp_path, maps):
    run, sample = teacher_fixture(tmp_path)
    config = tmp_path / "maps.json"
    config.write_text(json.dumps(maps))
    sources_before = {str(p): digest(p) for p in run.rglob("*") if p.is_file()}
    output = tmp_path / "pixels"
    result = subprocess.run([sys.executable, str(ROOT / "export_pixels.py"), "--run", str(run),
                             "--output", str(output), "--maps-json", str(config)],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    row = json.loads((output / "samples.jsonl").read_text())
    np.testing.assert_allclose(row["reference_pose"], [39.5, 79.5, .3])
    np.testing.assert_allclose(row["raw_poses"], [[29.5, 69.5, .1]])
    np.testing.assert_allclose(row["route"], [[39.5, 79.5], [79.5, 119.5]])
    assert row["commands"] == sample["teacher"]["commands"]
    assert row["raw_pose_stamps_s"] == [99.] and row["source_sample_json"].endswith("sample.json")
    assert sources_before == {str(p): digest(p) for p in run.rglob("*") if p.is_file()}
    assert json.loads(result.stdout)["status"] == "success"


@pytest.mark.parametrize("damage", ["missing_npz", "wrong_map", "count", "duplicate", "history"])
def test_teacher_corruption_is_reported(tmp_path, maps, damage):
    run, sample = teacher_fixture(tmp_path)
    case = run / "cases" / sample["case_id"]
    if damage == "missing_npz":
        (case / "inputs.npz").unlink()
    elif damage == "wrong_map":
        sample["map_name"] = "P_map"
        (case / "sample.json").write_text(json.dumps(sample))
    elif damage == "count":
        (run / "run.json").write_text(json.dumps(dict(status="success", accepted_count=2)))
    elif damage == "duplicate":
        write_rows(run / "teacher_labels.jsonl", [sample, sample])
    else:
        sample["teacher_history"]["poses"][0][0] = 40.
        (case / "sample.json").write_text(json.dumps(sample))
    with pytest.raises((ValueError, FileNotFoundError)):
        export_pixels(tmp_path / "pixels", maps, run=run)
    assert not (tmp_path / "pixels").exists()


def test_npz_map_metadata(tmp_path):
    path = tmp_path / "map.npz"
    np.savez(path, occupancy=np.zeros((5, 10)), origin_xy=[-1, -2], resolution_m=.2)
    converter = PixelConverter({"custom": path})
    row, _ = converter.convert_row(dict(map_name="custom", reference_pose=[-.9, -1.9, .2]))
    np.testing.assert_allclose(row["reference_pose"], [0, 0, .2], atol=1e-12)
    assert converter.maps["custom"]["width"] == 10 and converter.maps["custom"]["sha256"] == digest(path)
