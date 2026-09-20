#!/usr/bin/env python3
"""Map pmap coordinates onto JPG roads, then align axes, scale and origin.

Standard-library only. Keep data/roads.json next to this file. World input/output
(default) uses metres, X right and Y up. Pixel input/output uses pmap pixel units,
X right and Y down. In either mode, that input frame's (0, 0) is projected onto
the road network and mapped to JPG; this JPG road point defines output (0, 0).
The returned coordinates are not bounded to [0, 1]. Original JPG road pixels are
available with details=True under ``jpg_pixel``.

    from dev.pmap_overlay.pmap_to_jpg import pmap_to_jpg, PmapJpgMapper
    x_jpg, y_jpg = pmap_to_jpg(0.0, 0.0)  # world metres -> aligned metres
    info = pmap_to_jpg(843.0, 1360.0, input_frame='pixel', details=True)

The default attraction is 4 m at junctions, 1.5 m at ordinary road nodes and 1 m
at entrances. Each end is capped at 45% of its edge length. Edge progress outside
these endpoint plateaus is re-normalized continuously before transfer to JPG.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path

DEFAULT_ROADS_PATH = Path(__file__).resolve().parent / 'data' / 'roads.json'


def _polyline(xy):
    points = [tuple(map(float, p)) for p in xy]
    if len(points) < 2 or any(not math.isfinite(v) for p in points for v in p):
        raise ValueError('Each road must have at least two finite points')
    cumulative = [0.0]
    for a, b in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + math.hypot(b[0]-a[0], b[1]-a[1]))
    if cumulative[-1] <= 0:
        raise ValueError('Road length must be positive')
    return points, cumulative


def _sample(polyline, progress):
    points, cumulative = polyline
    target = max(0.0, min(1.0, progress)) * cumulative[-1]
    if target >= cumulative[-1]:
        return points[-1]
    i = min(bisect_right(cumulative, target)-1, len(points)-2)
    a, b = points[i], points[i+1]
    fraction = (target-cumulative[i]) / (cumulative[i+1]-cumulative[i])
    return a[0]+fraction*(b[0]-a[0]), a[1]+fraction*(b[1]-a[1])


class PmapJpgMapper:
    """Load once and reuse for repeated pmap -> JPG road coordinate queries.

    ``map(..., input_frame='world')`` returns aligned metres with origin defined
    by mapping pmap world (0,0). ``input_frame='pixel'`` returns aligned pmap-pixel
    units with origin defined by mapping pmap image pixel (0,0). ``jpg_pixel`` in
    detailed results is always the actual point on the unrotated JPG centerline.
    """

    def __init__(self, roads_path=DEFAULT_ROADS_PATH, *, junction_m=4.0,
                 ordinary_m=1.5, entrance_m=1.0):
        self.parameters = dict(junction_m=float(junction_m), ordinary_m=float(ordinary_m),
                               entrance_m=float(entrance_m))
        if any(not math.isfinite(v) or v < 0 for v in self.parameters.values()):
            raise ValueError('Attraction lengths must be finite and non-negative')
        self.roads_path = Path(roads_path).expanduser().resolve()
        raw = self.roads_path.read_bytes()
        self.roads_sha256 = hashlib.sha256(raw).hexdigest()
        document = json.loads(raw)
        self.meta = document['alignment']['map']
        self.transform = document['alignment']['transform']
        self.resolution = float(self.meta['resolution'])
        if not math.isfinite(self.resolution) or self.resolution <= 0:
            raise ValueError('Map resolution must be finite and positive')
        if any(not math.isfinite(float(self.transform[k])) for k in ('sx', 'sy', 'angle')):
            raise ValueError('Alignment transform must be finite')
        if self.transform['sx'] <= 0 or self.transform['sy'] <= 0:
            raise ValueError('Alignment scale must be positive')
        jpg_nodes = {n['annotation_id']: tuple(n['xy']) for n in document['layers']['jpg']['nodes']}
        self.nodes = {n['annotation_id']: dict(jpg_id=n['annotation_id']-1,
                     jpg=jpg_nodes[n['annotation_id']-1], degree=0)
                     for n in document['layers']['pmap']['nodes']}
        jpg_routes = {e['id']: e for e in document['layers']['jpg']['routes']}
        self.edges = []
        self.segments = []
        for edge_index, route in enumerate(document['layers']['pmap']['routes']):
            a, b = route['annotation_ids']
            jpg_route = jpg_routes[route['id']]
            if jpg_route['annotation_ids'] != [a-1, b-1]:
                raise ValueError('JPG and pmap edge directions or IDs differ')
            self.nodes[a]['degree'] += 1
            self.nodes[b]['degree'] += 1
            pmap, jpg = _polyline(route['xy']), _polyline(jpg_route['xy'])
            self.edges.append(dict(id=route['id'], start=a, end=b, pmap=pmap, jpg=jpg,
                                   length=pmap[1][-1]))
            for i, (p, q) in enumerate(zip(pmap[0], pmap[0][1:])):
                dx, dy = q[0]-p[0], q[1]-p[1]
                length = math.hypot(dx, dy)
                if length > 1e-10:
                    self.segments.append((edge_index, p[0], p[1], dx, dy, length,
                                          length*length, pmap[1][i]))
        if not self.edges:
            raise ValueError('No road edges')
        for edge in self.edges:
            clips = []
            for node_id in (edge['start'], edge['end']):
                degree = self.nodes[node_id]['degree']
                radius = junction_m if degree >= 3 else entrance_m if degree == 1 else ordinary_m
                clips.append(min(float(radius)/self.resolution, .45*edge['length']))
            edge['clips'] = clips
        angle = math.radians(float(self.transform['angle']))
        # Exact quarter turns avoid tiny axis leakage due to floating-point cos.
        cosine, sine = math.cos(angle), math.sin(angle)
        if abs(cosine) < 1e-12:
            cosine = 0.0
        if abs(sine) < 1e-12:
            sine = 0.0
        sx, sy = float(self.transform['sx']), float(self.transform['sy'])
        self.pixel_matrix = ((cosine*sx, -sine*sy), (sine*sx, cosine*sy))
        self.world_matrix = ((self.resolution*self.pixel_matrix[0][0],
                              self.resolution*self.pixel_matrix[0][1]),
                             (-self.resolution*self.pixel_matrix[1][0],
                              -self.resolution*self.pixel_matrix[1][1]))
        self._origin_jpg = {
            'world': self._map_pixel(*self.world_to_pixel(0.0, 0.0))['jpg_pixel'],
            'pixel': self._map_pixel(0.0, 0.0)['jpg_pixel'],
        }

    def world_to_pixel(self, x, y):
        """Pmap world metres -> raster pixel centers (top-left, y down)."""
        return ((x-self.meta['x_min'])/self.resolution,
                self.meta['height']-1-(y-self.meta['y_min'])/self.resolution)

    def _map_pixel(self, x, y):
        best = math.inf
        selected = None
        for edge_index, ax, ay, dx, dy, length, squared, start_s in self.segments:
            u = max(0.0, min(1.0, ((x-ax)*dx+(y-ay)*dy)/squared))
            px, py = ax+u*dx, ay+u*dy
            d2 = (x-px)**2+(y-py)**2
            # Same tolerance/order as the interactive mapping kernel.
            if d2 < best-1e-9:
                best = d2
                selected = edge_index, start_s+u*length, (px, py)
        edge_index, s, nearest = selected
        edge = self.edges[edge_index]
        a, b = edge['clips']
        node = None
        if s <= a:
            node, t = edge['start'], 0.0
        elif s >= edge['length']-b:
            node, t = edge['end'], 1.0
        else:
            t = max(0.0, min(1.0, (s-a)/(edge['length']-a-b)))
        jpg = self.nodes[node]['jpg'] if node is not None else _sample(edge['jpg'], t)
        return dict(jpg_pixel=tuple(jpg), mapping_type='node' if node is not None else 'edge',
                    node_id=node, jpg_node_id=node-1 if node is not None else None,
                    edge_id=edge['id'], pmap_edge=(edge['start'], edge['end']),
                    jpg_edge=(edge['start']-1, edge['end']-1),
                    progress=t, raw_progress=s/edge['length'],
                    nearest_pmap_pixel=nearest, distance_m=math.sqrt(best)*self.resolution,
                    start_snap_m=a*self.resolution, end_snap_m=b*self.resolution)

    def normalize_jpg_pixel(self, x, y, *, frame='world'):
        """Express a JPG pixel in the selected aligned frame, without projecting.

        For current alignment (270 degrees, scale 1.5), world normalization is
        X = 0.15*(jpg_y-origin_y), Y = 0.15*(jpg_x-origin_x).
        """
        if frame not in ('world', 'pixel'):
            raise ValueError("frame must be 'world' or 'pixel'")
        if not math.isfinite(float(x)) or not math.isfinite(float(y)):
            raise ValueError('Coordinates must be finite')
        origin = self._origin_jpg[frame]
        dx, dy = float(x)-origin[0], float(y)-origin[1]
        matrix = self.world_matrix if frame == 'world' else self.pixel_matrix
        return matrix[0][0]*dx+matrix[0][1]*dy, matrix[1][0]*dx+matrix[1][1]*dy

    def map(self, x, y, *, input_frame='world', details=False):
        """Return (normalized_x, normalized_y), or a detailed dict.

        world: input/output in metres, X right/Y up, mapped world origin.
        pixel: input/output in pmap pixel units, X right/Y down, mapped image origin.
        Coordinates outside the pmap raster are also projected geometrically.
        details=True additionally returns raw JPG road pixels, edge/node IDs,
        original/adjusted progress and the exact normalization matrix/origin.
        """
        if input_frame not in ('world', 'pixel'):
            raise ValueError("input_frame must be 'world' or 'pixel'")
        x, y = float(x), float(y)
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError('Coordinates must be finite')
        p = self.world_to_pixel(x, y) if input_frame == 'world' else (x, y)
        result = self._map_pixel(*p)
        normalized = self.normalize_jpg_pixel(*result['jpg_pixel'], frame=input_frame)
        if not details:
            return normalized
        return dict(result, jpg_normalized=normalized,
                    normalized_unit='m' if input_frame == 'world' else 'pmap_pixel',
                    axes='x_right_y_up' if input_frame == 'world' else 'x_right_y_down',
                    input_frame=input_frame, input_xy=(x, y), input_pmap_pixel=p,
                    origin_pmap=(0.0, 0.0), origin_jpg_pixel=self._origin_jpg[input_frame],
                    jpg_to_normalized_matrix=self.world_matrix if input_frame == 'world' else self.pixel_matrix,
                    parameters=self.parameters.copy(), roads_sha256=self.roads_sha256)


@lru_cache(maxsize=1)
def _default_mapper():
    return PmapJpgMapper()


def pmap_to_jpg(x, y, *, input_frame='world', details=False):
    """Convenience function: 4 m junction attraction, cached read-only road data.

    Default: pmap world metres -> JPG road coordinates normalized in metres.
    Set input_frame='pixel' for pmap raster pixels -> aligned pmap-pixel units.
    Set details=True to also obtain the actual unrotated JPG road pixel.
    """
    return _default_mapper().map(x, y, input_frame=input_frame, details=details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('x', type=float)
    parser.add_argument('y', type=float)
    parser.add_argument('--input-frame', choices=['world', 'pixel'], default='world')
    parser.add_argument('--roads', type=Path, default=DEFAULT_ROADS_PATH)
    parser.add_argument('--details', action='store_true')
    args = parser.parse_args()
    mapper = PmapJpgMapper(args.roads)
    print(json.dumps(mapper.map(args.x, args.y, input_frame=args.input_frame, details=args.details),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
