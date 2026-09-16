#!/usr/bin/env python3
"""Stage a locally-built DashMap offline region into GitHub Release assets.

Reads the per-region archive produced by the DashMap desktop tool
(`tools/update_offline_data.py` → `tools/map_build/regions/<slug>/`) and
copies it into a clean staging dir with the STABLE asset names the app
expects, plus a generated `release-manifest.json` and `SHA256SUMS`.

Source layout (DashMap repo, never modified, only read):

    tools/map_build/regions/<slug>/
        valhalla/valhalla_tiles.tar
        valhalla/manifest.json          ({"region","builtAtMs","valhallaVersion","sourceFile"})
        valhalla/valhalla.json
        valhalla/admin.sqlite          (optional)
        valhalla/timezones.sqlite      (optional)
        streets/streets-brazil.sqlite  (builder filename is fixed even for other regions)
        streets/streets-manifest.json  ({"region","builtAtMs","sourceFile"})

Staged output (<out-dir>/):

    valhalla_tiles.tar  (or valhalla_tiles.tar.part-aa/ab/... when split)
    valhalla_manifest.json
    valhalla.json
    admin.sqlite               (if present in source)
    timezones.sqlite           (if present in source)
    streets.sqlite             (renamed from streets-brazil.sqlite)
    streets_manifest.json
    release-manifest.json      (schema: ../release-manifest.schema.json)
    SHA256SUMS

GitHub rejects release assets over 2 GB, so tars >= --split-above are split
into --part-size chunks with `split -b` naming (.part-aa, .part-ab, ...).
Reassembly is `cat valhalla_tiles.tar.part-* > valhalla_tiles.tar`.

Usage:
    ./scripts/publish_region.py --region-dir ~/DashMap/tools/map_build/regions/brazil \\
        --slug brazil --display-name Brazil --tag brazil-v2026.09.16 \\
        --geofabrik-url https://download.geofabrik.de/south-america/brazil-latest.osm.pbf \\
        --out dist/brazil-v2026.09.16

    # then validate + upload:
    ./scripts/verify_release.py --dir dist/brazil-v2026.09.16
    gh release create brazil-v2026.09.16 --title brazil-v2026.09.16 --notes "..." dist/brazil-v2026.09.16/*

Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

ATTRIBUTION = "Map data © OpenStreetMap contributors · Open Database License (ODbL)."

# Stay comfortably below GitHub's 2 GiB per-asset hard limit.
DEFAULT_SPLIT_ABOVE = 1900 * 1024 * 1024
DEFAULT_PART_SIZE = 1500 * 1024 * 1024

VALHALLA_FILES = ("valhalla_tiles.tar", "manifest.json", "valhalla.json",
                  "admin.sqlite", "timezones.sqlite")
STREETS_DB_NAMES = ("streets-brazil.sqlite", "streets.sqlite")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def split_file(path: Path, part_size: int, log=print) -> list[Path]:
    """Split path into part_size chunks named <name>.part-aa, .part-ab, ... (GNU split style)."""
    data_size = path.stat().st_size
    parts: list[Path] = []
    with path.open("rb") as f:
        idx = 0
        while True:
            buf = f.read(part_size)
            if not buf:
                break
            # aa, ab, ..., az, ba, ... (enough for any realistic tile tar)
            suffix = chr(ord("a") + idx // 26) + chr(ord("a") + idx % 26)
            part = path.parent / f"{path.name}.part-{suffix}"
            part.write_bytes(buf)
            parts.append(part)
            idx += 1
    log(f"split {path.name} ({data_size} bytes) into {len(parts)} parts")
    return parts


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--region-dir", required=True, help="tools/map_build/regions/<slug>/ from DashMap")
    ap.add_argument("--slug", required=True, help="region slug, e.g. brazil")
    ap.add_argument("--display-name", required=True, help='human name, e.g. "Brazil"')
    ap.add_argument("--tag", required=True, help="release tag, e.g. brazil-v2026.09.16")
    ap.add_argument("--geofabrik-url", default="", help="source .osm.pbf URL for provenance")
    ap.add_argument("--out", required=True, help="staging output dir")
    ap.add_argument("--split-above", type=int, default=DEFAULT_SPLIT_ABOVE,
                    help="split valhalla tar at/above this many bytes")
    ap.add_argument("--part-size", type=int, default=DEFAULT_PART_SIZE,
                    help="bytes per split part")
    args = ap.parse_args()

    src = Path(args.region_dir)
    val_dir = src / "valhalla"
    st_dir = src / "streets"
    if not val_dir.is_dir() and not st_dir.is_dir():
        print(f"ERROR: {src} has neither valhalla/ nor streets/ — wrong --region-dir?", file=sys.stderr)
        return 1

    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        print(f"ERROR: --out {out} exists and is not empty (refusing to mix releases).", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)

    val_manifest = read_json(val_dir / "manifest.json") or {}
    st_manifest = read_json(st_dir / "streets-manifest.json") or {}
    built_at = int(val_manifest.get("builtAtMs") or st_manifest.get("builtAtMs") or time.time() * 1000)
    pbf_file = str(val_manifest.get("sourceFile") or st_manifest.get("sourceFile") or "")
    valhalla_version = str(val_manifest.get("valhallaVersion") or "")

    staged_valhalla: list[Path] = []
    staged_streets: list[Path] = []

    # ── Valhalla ──
    tar = val_dir / "valhalla_tiles.tar"
    if tar.exists():
        shutil.copy2(tar, out / "valhalla_tiles.tar")
        if (out / "valhalla_tiles.tar").stat().st_size >= args.split_above:
            parts = split_file(out / "valhalla_tiles.tar", args.part_size)
            (out / "valhalla_tiles.tar").unlink()
            staged_valhalla.extend(parts)
            packaging = "split"
        else:
            staged_valhalla.append(out / "valhalla_tiles.tar")
            packaging = "single"
    else:
        packaging = "single"

    for name, asset in (("manifest.json", "valhalla_manifest.json"),
                        ("valhalla.json", "valhalla.json"),
                        ("admin.sqlite", "admin.sqlite"),
                        ("timezones.sqlite", "timezones.sqlite")):
        p = val_dir / name
        if p.exists():
            shutil.copy2(p, out / asset)
            staged_valhalla.append(out / asset)

    # ── Streets (builder filename is fixed; publish under the stable name) ──
    db_src = next((st_dir / n for n in STREETS_DB_NAMES if (st_dir / n).exists()), None)
    if db_src is not None:
        shutil.copy2(db_src, out / "streets.sqlite")
        staged_streets.append(out / "streets.sqlite")
    st_man = st_dir / "streets-manifest.json"
    if st_man.exists():
        shutil.copy2(st_man, out / "streets_manifest.json")
        staged_streets.append(out / "streets_manifest.json")

    if not staged_valhalla and not staged_streets:
        print("ERROR: nothing staged — source region dir has no recognizable dataset files.", file=sys.stderr)
        return 1

    def entry(p: Path) -> dict:
        return {"name": p.name, "size": p.stat().st_size, "sha256": sha256_of(p)}

    manifest = {
        "schema": 1,
        "slug": args.slug,
        "displayName": args.display_name,
        "tag": args.tag,
        "builtAtMs": built_at,
        "source": {
            "pbfFile": pbf_file,
            "geofabrikUrl": args.geofabrik_url,
            "valhallaVersion": valhalla_version,
        },
        "valhalla": {
            "present": bool(staged_valhalla),
            "packaging": packaging,
            "files": [entry(p) for p in staged_valhalla],
        },
        "streets": {
            "present": bool(staged_streets),
            "files": [entry(p) for p in staged_streets],
        },
        "install": {
            # Mirrors update_offline_data.py DATASETS device paths. Note the device-side
            # streets filename stays streets-brazil.sqlite (legacy name the app already
            # reads) even though the release asset is streets.sqlite.
            "valhallaDir": "/sdcard/Android/data/com.dashmap.app/files/valhalla",
            "streetsDir": "/sdcard/Android/data/com.dashmap.app/files/offline_streets",
        },
        "attribution": ATTRIBUTION,
    }
    (out / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    with (out / "SHA256SUMS").open("w") as f:
        for p in sorted(out.iterdir()):
            if p.name in ("SHA256SUMS",):
                continue
            f.write(f"{sha256_of(p)}  {p.name}\n")

    print(f"Staged {args.tag} in {out}:")
    for p in sorted(out.iterdir()):
        print(f"  {p.name}  {p.stat().st_size / (1024 * 1024):.1f} MB")
    print("Next: scripts/verify_release.py --dir", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
