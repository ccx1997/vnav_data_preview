"""The 2026-09-24 centerline map, registered to the existing paired JPG roads.

The user confirmed that this image preserves the old drawing's layout. This
transfers that calibration, not a new metric survey or a route planner.
"""
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .geometry import ASSETS, HanddrawMapper, coordinates, digest

SOURCE_MAP_ASSETS = ASSETS.parent / "source_map"


class SourceMapMapper(HanddrawMapper):
    """P_map pixels -> rotated source_map pixels, without endpoint attraction."""

    def __init__(self, assets=ASSETS, source_assets=SOURCE_MAP_ASSETS):
        super().__init__(assets)
        self.source_assets = Path(source_assets)
        registration = json.loads((self.source_assets / "registration.json").read_text())
        image_path = self.source_assets / "source_map.png"
        if digest(image_path) != registration["source_sha256"]:
            raise ValueError("source-map checksum mismatch; a changed map needs a reviewed registration")
        if (registration["reference_sha256"] != self.meta["original_sha256"]
                or registration["reference_size"] != self.meta["original_size"]
                or registration["paired_roads_sha256"] != self.meta["roads_sha256"]):
            raise ValueError("source-map registration does not match the paired reference assets")
        with Image.open(image_path) as image:
            w, h = image.size
            rgba = np.asarray(image.convert("RGBA"))
        if registration["source_size"] != [w, h]:
            raise ValueError("source-map dimensions differ from registration")
        if (not np.all(rgba[..., 3] == 255)
                or not np.all(rgba[..., :3] == rgba[..., :1])
                or not np.isin(rgba[..., 0], [0, 192, 255]).all()):
            raise ValueError("source-map must be opaque with palette 0/192/255")
        # np.rot90 and PIL ROTATE_90 preserve every categorical pixel and gap.
        self.road_mask = np.rot90(rgba[..., 0] == 192).copy()
        sx, sy = np.asarray([w, h]) / np.asarray(self.meta["original_size"])
        self.matrix = np.array([[0, sy, (sy - 1) / 2], [sx, 0, (sx - 1) / 2]])
        for edge in self.mapper.edges:
            edge["clips"] = [0., 0.]
        self.meta = dict(
            coordinate_frame="cartesian_pixel", origin="center of bottom-left pixel",
            axes="x_right_y_up", position_unit="pixel", width=h, height=w,
            original_size=[w, h], rotation_ccw_degrees=90, exact_scale=1,
            source_raster_to_aligned_cartesian=[[0, 1, 0], [1, 0, 0]],
            original_raster_to_aligned_cartesian=self.matrix.tolist(),
            original_raster="legacy paired JPG raster; source_raster is the new PNG raster",
            training_pmap=self.meta["training_pmap"], registration=registration,
            resolution_m=None, nominal_resolution_m_xy=[.15 / sy, .15 / sx],
            metric_note="nominal scales only; paired-road arc transfer is not a global metric transform",
            junction_snap_m=0, ordinary_snap_m=0, entrance_snap_m=0,
            mapping="nearest existing P_map paired road, unsnapped arc progress, pixel-center resize, CCW 90",
            road_value=192, building_value=0, background_value=255,
            registration_correction_max_px=2,
            road_validity="nearest pixel center must be gray; at most 2 px local registration correction, no dilation or gap bridging",
            route_validity="each consecutive straight segment sampled at <=0.5 pixel in either axis must stay on gray",
            coverage="existing paired roads only; unpaired new roads have no world-coordinate calibration",
            yaw="source world radians unchanged; not a road tangent or a warped-map heading",
        )
        self._point_cache = {}

    def map_many(self, points):
        xy = coordinates(points, (2,)).reshape(-1, 2)
        keys = [tuple(point) for point in xy]
        # Histories and repeated teacher initial states share exact coordinates.
        # Cache without quantization; bound memory for long recordings.
        if len(self._point_cache) > 32768:
            self._point_cache.clear()
        missing = list(dict.fromkeys(key for key in keys if key not in self._point_cache))
        if missing:
            mapped, distances = self._map_uncached(missing)
            for key, point, distance in zip(missing, mapped, distances):
                self._point_cache[key] = (tuple(point), float(distance))
        return (np.asarray([self._point_cache[key][0] for key in keys]).reshape(-1, 2),
                np.asarray([self._point_cache[key][1] for key in keys]))

    def _map_uncached(self, points):
        mapped, distances = super().map_many(points)
        # The inherited centerline falls just outside the new stroke at a few
        # rasterized corners (<=2 px). Correct only those local discrepancies;
        # a deleted entrance/gap remains invalid, never snapped to another road.
        offsets = np.array([(x, y) for y in range(-2, 3) for x in range(-2, 3)])
        for i in np.flatnonzero(~self.road_validity(mapped)):
            candidates = np.floor(mapped[i] + .5) + offsets
            squared = ((candidates - mapped[i]) ** 2).sum(axis=1)
            eligible = self.road_validity(candidates) & (squared <= 4)
            if eligible.any():
                best = np.where(eligible, squared, np.inf).argmin()
                mapped[i] = candidates[best]
        return mapped, distances

    def road_validity(self, points):
        xy = coordinates(points, (2,)).reshape(-1, 2)
        height, width = self.road_mask.shape
        inside = ((xy[:, 0] >= -.5) & (xy[:, 0] < width - .5)
                  & (xy[:, 1] >= -.5) & (xy[:, 1] < height - .5))
        valid = np.zeros(len(xy), dtype=bool)
        indices = np.floor(xy[inside] + .5).astype(int)
        valid[inside] = self.road_mask[height - 1 - indices[:, 1], indices[:, 0]]
        return valid

    def route_segment_validity(self, points):
        """Conservative raster check, not graph routing or gap repair."""
        xy = coordinates(points, (2,)).reshape(-1, 2)
        endpoints = self.road_validity(xy)
        valid = []
        for i, (a, b) in enumerate(zip(xy, xy[1:])):
            if not (endpoints[i] and endpoints[i + 1]):
                valid.append(False)
                continue
            count = max(2, int(np.ceil(np.max(np.abs(b - a)) * 2)) + 1)
            valid.append(bool(self.road_validity(np.linspace(a, b, count)).all()))
        return valid

    def prepare_image(self, output):
        output = Path(output)
        output.mkdir(parents=True, exist_ok=False)
        with Image.open(self.source_assets / "source_map.png") as original:
            original.transpose(Image.Transpose.ROTATE_90).save(output / "aligned.png")
        Image.fromarray(self.road_mask.astype(np.uint8) * 255).save(output / "road_mask.png")
        meta = dict(self.meta, png_sha256=digest(output / "aligned.png"),
                    road_mask_sha256=digest(output / "road_mask.png"))
        (output / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
        return meta
