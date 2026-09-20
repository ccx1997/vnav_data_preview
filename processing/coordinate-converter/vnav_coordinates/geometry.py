"""Geometry ported from visual_navigation_e2e.pixel_coordinates.

Public pixels index the bottom-left pixel center, X right, Y up. Yaw is kept
in the source world frame; a road projection does not define an orientation.
"""
from functools import lru_cache
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

from ._paired_roads import PmapJpgMapper, _sample

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "handdraw_pmap"
FRAME = "cartesian_pixel"


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def coordinates(values, dimensions=(2, 3)):
    result = np.asarray(values, dtype=np.float64)
    if result.shape == (0,):
        result = result.reshape(0, dimensions[0])
    if result.ndim not in (1, 2) or result.shape[-1] not in dimensions:
        raise ValueError(f"expected XY/pose vector or matrix with columns {dimensions}")
    if not np.isfinite(result).all():
        raise ValueError("coordinates must be finite")
    return result


def validate_map(meta):
    origin = np.asarray(meta["origin_xy"], dtype=np.float64)
    resolution = float(meta["resolution_m"])
    if origin.shape != (2,) or not np.isfinite(origin).all():
        raise ValueError("map origin_xy must contain two finite numbers")
    if not np.isfinite(resolution) or resolution <= 0:
        raise ValueError("map resolution_m must be finite and positive")
    for key in ("width", "height"):
        if int(meta[key]) != meta[key] or meta[key] <= 0:
            raise ValueError(f"map {key} must be a positive integer")


def world_to_cartesian(values, origin_xy, resolution_m):
    """World XY metres -> pixel centers; preserve an optional yaw verbatim."""
    validate_map(dict(origin_xy=origin_xy, resolution_m=resolution_m, width=1, height=1))
    result = coordinates(values).copy()
    with np.errstate(over="ignore", invalid="ignore"):
        result[..., :2] = (result[..., :2] - origin_xy) / resolution_m - 0.5
    if not np.isfinite(result).all():
        raise ValueError("converted coordinates must be finite")
    return result


class HanddrawMapper:
    """P_map Cartesian pixels -> aligned hand-drawn JPG road pixels.

    Paired centerlines, 4 m junction / 1.5 m ordinary / 1 m entrance attraction,
    capped at 45% of edge length; then paired arc-length progress and image
    alignment. The vectorized selection preserves the reference's tie rule.
    """

    def __init__(self, assets=ASSETS):
        self.assets = Path(assets)
        self.meta = json.loads((self.assets / "metadata.json").read_text())
        for name, key in (("data/roads.json", "roads_sha256"), ("original.jpg", "original_sha256")):
            if digest(self.assets / name) != self.meta[key]:
                raise ValueError(f"handdraw asset checksum mismatch: {name}")
        self.mapper = PmapJpgMapper(self.assets / "data/roads.json", junction_m=4.)
        self.segments = np.asarray(self.mapper.segments)
        self.matrix = np.asarray(self.meta["original_raster_to_aligned_cartesian"], dtype=np.float64)
        self.validate_pmap(self.meta["training_pmap"])
        self._validate_image_geometry()

    def validate_pmap(self, meta):
        validate_map(meta)
        roads = self.mapper.meta
        if (meta["width"] != roads["width"] or meta["height"] != roads["height"]
                or not np.isclose(meta["resolution_m"], roads["resolution"], rtol=0, atol=1e-12)
                or not np.allclose(meta["origin_xy"], [roads["x_min"], roads["y_min"]], rtol=0, atol=1e-10)):
            raise ValueError("P_map dimensions/origin/resolution differ from paired road annotations")

    def _validate_image_geometry(self):
        transform = self.mapper.transform
        if transform["angle"] != 270 or transform["sx"] != transform["sy"] or transform["sx"] != 1.5:
            raise ValueError("handdraw image requires reference rotation 90° CCW and exact scale 1.5")
        with Image.open(self.assets / "original.jpg") as original:
            w, h = original.size
        scale = float(transform["sx"])
        width, height = int(np.ceil(h * scale)), int(np.ceil(w * scale))
        expected = [[0, scale, (scale - 1) / 2], [scale, 0, height - .5 - scale * (w - .5)]]
        if (self.matrix.shape != (2, 3) or not np.allclose(self.matrix, expected, rtol=0, atol=1e-10)
                or self.meta["width"] != width or self.meta["height"] != height
                or self.meta["original_size"] != [w, h]):
            raise ValueError("handdraw metadata and image alignment differ")

    def original_to_aligned(self, xy):
        return coordinates(xy, (2,)) @ self.matrix[:, :2].T + self.matrix[:, 2]

    def map_many(self, points):
        xy = coordinates(points, (2,)).reshape(-1, 2).copy()
        xy[:, 1] = self.mapper.meta["height"] - 1 - xy[:, 1]
        seg = self.segments
        out, distances = [], []
        for batch_start in range(0, len(xy), 128):
            p = xy[batch_start:batch_start + 128]
            with np.errstate(over="ignore", invalid="ignore"):
                u = np.clip(((p[:, None, :] - seg[None, :, 1:3]) * seg[None, :, 3:5]).sum(-1) / seg[:, 6], 0., 1.)
                near = seg[None, :, 1:3] + u[..., None] * seg[None, :, 3:5]
                d2 = ((p[:, None, :] - near) ** 2).sum(-1)
            if not np.isfinite(d2).all():
                raise ValueError("coordinates exceed projection numeric range")
            minimum = d2.min(axis=1)
            chosen = (d2 <= minimum[:, None] + 1e-9).argmax(axis=1)
            for i, k in enumerate(chosen):
                edge = self.mapper.edges[int(seg[k, 0])]
                s = seg[k, 7] + u[i, k] * seg[k, 5]
                a, b = edge["clips"]
                if s <= a:
                    jpg = self.mapper.nodes[edge["start"]]["jpg"]
                elif s >= edge["length"] - b:
                    jpg = self.mapper.nodes[edge["end"]]["jpg"]
                else:
                    jpg = _sample(edge["jpg"], (s - a) / (edge["length"] - a - b))
                out.append(jpg)
                distances.append(float(np.sqrt(d2[i, k]) * self.mapper.resolution))
        return self.original_to_aligned(np.asarray(out).reshape(-1, 2)), np.asarray(distances)

    def map_world(self, points):
        """World XY/poses -> handdraw XY and projection distances in metres."""
        meta = self.meta["training_pmap"]
        pixels = world_to_cartesian(points, meta["origin_xy"], meta["resolution_m"])
        return self.map_many(pixels[..., :2])

    def __call__(self, x_px, y_px):
        return tuple(self.map_many([[x_px, y_px]])[0][0])

    def prepare_image(self, output):
        """Write aligned images and portable metadata to a NEW directory."""
        output = Path(output)
        output.mkdir(parents=True, exist_ok=False)
        with Image.open(self.assets / "original.jpg") as original:
            rotated = original.convert("RGB").transpose(Image.Transpose.ROTATE_90)
        scale = float(self.mapper.transform["sx"])
        aligned = rotated.transform(
            (self.meta["width"], self.meta["height"]), Image.Transform.AFFINE,
            (1 / scale, 0, 0, 0, 1 / scale, 0), Image.Resampling.BICUBIC, fillcolor="white",
        )
        aligned.save(output / "aligned.jpg", quality=98, subsampling=0)
        aligned.save(output / "aligned.png")
        meta = dict(self.meta, jpg_sha256=digest(output / "aligned.jpg"),
                    png_sha256=digest(output / "aligned.png"))
        (output / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
        return meta


@lru_cache(maxsize=1)
def _mapper():
    return HanddrawMapper()


def pmap_pixel_to_handdraw_pixel(x_px, y_px):
    """Both input/output use bottom-left pixel centers, X right, Y up."""
    return _mapper()(x_px, y_px)
