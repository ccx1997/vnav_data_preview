#!/usr/bin/env python3
"""Fourth processing step: export existing world coordinates as pixel manifests."""
import argparse
import json
from pathlib import Path

from vnav_coordinates.export import export_pixels
from vnav_coordinates.geometry import ASSETS
from vnav_coordinates.source_map import SOURCE_MAP_ASSETS

ROOT = Path(__file__).resolve().parent
DEFAULT_MAP_ROOT = Path("/mnt/chengchangxu/projects/navi_sys_odo/dev/model_server/model_server/maps")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run", type=Path, help="one successful existing teacher run")
    source.add_argument("--input", type=Path, nargs="+", help="world-coordinate pose-history JSONL manifests")
    parser.add_argument("--output", required=True, type=Path, help="new directory outside source directories")
    parser.add_argument("--maps-json", type=Path, help="map_name -> NPZ path or dimensions/origin/resolution metadata")
    parser.add_argument("--static-map-root", type=Path, default=DEFAULT_MAP_ROOT)
    parser.add_argument("--handdraw-assets", type=Path, default=ASSETS)
    parser.add_argument("--handdraw-map", choices=("legacy", "source-map"), default="legacy",
                        help="legacy JPG (default, all maps) or registered centerline PNG (P_map rows only)")
    parser.add_argument("--source-map-assets", type=Path, default=SOURCE_MAP_ASSETS,
                        help="registered source_map.png + registration.json; used only by source-map mode")
    args = parser.parse_args()
    try:
        if args.maps_json:
            maps = json.loads(args.maps_json.read_text())
            maps = {key: str((args.maps_json.resolve().parent / value).resolve()) if isinstance(value, str) else value
                    for key, value in maps.items()}
        else:
            config = json.loads((ROOT.parent / "training-data-builder/config.json").read_text())
            maps = {name: args.static_map_root / f"{graph}.npz" for name, graph in config["map_mapping"].items()}
        audit = export_pixels(args.output, maps, inputs=args.input or (), run=args.run, assets=args.handdraw_assets,
                              handdraw_map=args.handdraw_map, source_map_assets=args.source_map_assets)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(1, f"pixel export failed: {exc}\n")
    print(json.dumps(dict(status="success", output=str(args.output.resolve()), counts=audit["counts"]), ensure_ascii=False))


if __name__ == "__main__":
    main()
