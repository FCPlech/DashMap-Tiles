#!/usr/bin/env python3
"""Stage a locally-built DashMap offline region into zipped GitHub Release assets.

Reads the per-region archive produced by the DashMap desktop tool
(`tools/update_offline_data.py` → `tools/map_build/regions/<slug>/`) and
packs it into a clean staging dir as one zip per dataset, plus a generated
`release-manifest.json` and `SHA256SUMS`.

This script does NOT build maps — building happens in the DashMap repo
(Docker + osmium via its offline-maps tool). It only packages a region that
is already built.

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

    valhalla.zip               (or valhalla.zip.part-aa/ab/... when split)
    streets.zip                (or streets.zip.part-aa/ab/... when split)
    release-manifest.json      (schema: ../release-manifest.schema.json)
    SHA256SUMS

Each zip contains the EXACT on-device filenames, so the app (or a manual
`unzip`) extracts straight into the target dir with no renaming:

    valhalla.zip  →  valhalla_tiles.tar, manifest.json, valhalla.json,
                     admin.sqlite?, timezones.sqlite?
                     (installs into …/files/valhalla/)
    streets.zip   →  streets-brazil.sqlite, manifest.json
                     (installs into …/files/offline_streets/)

GitHub rejects release assets over 2 GB, so zips >= --split-above are split
into --part-size chunks (`split -b` style naming: .part-aa, .part-ab, ...).
Reassembly is `cat valhalla.zip.part-* > valhalla.zip`, then `unzip`.

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
import sys
import time
import zipfile
from pathlib import Path

ATTRIBUTION = "Map data © OpenStreetMap contributors · Open Database License (ODbL)."

# Stay comfortably below GitHub's 2 GiB per-asset hard limit.
DEFAULT_SPLIT_ABOVE = 1900 * 1024 * 1024
DEFAULT_PART_SIZE = 1500 * 1024 * 1024

STREETS_DB_NAMES = ("streets-brazil.sqlite", "streets.sqlite")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def make_zip(members: list[tuple[Path, str]], zip_path: Path, log=print) -> None:
    """Pack (source path, archive name) pairs into zip_path (deflated, Zip64)."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6,
                         allowZip64=True) as z:
        for src, arc in members:
            z.write(src, arc)
    log(f"packed {zip_path.name} ({zip_path.stat().st_size} bytes, "
        f"{len(members)} files)")


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
            # aa, ab, ..., az, ba, ... (enough for any realistic region zip)
            suffix = chr(ord("a") + idx // 26) + chr(ord("a") + idx % 26)
            part = path.parent / f"{path.name}.part-{suffix}"
            part.write_bytes(buf)
            parts.append(part)
            idx += 1
    log(f"split {path.name} ({data_size} bytes) into {len(parts)} parts")
    return parts


def pack_dataset(members: list[tuple[Path, str]], archive_name: str, out: Path,
                 split_above: int, part_size: int) -> tuple[list[Path], str, list[str]]:
    """Zip members as out/<archive_name>, splitting when over the limit.

    Returns (staged files, packaging, zip member names)."""
    zip_path = out / archive_name
    make_zip(members, zip_path)
    contents = [arc for _, arc in members]
    if zip_path.stat().st_size >= split_above:
        parts = split_file(zip_path, part_size)
        zip_path.unlink()
        return parts, "split", contents
    return [zip_path], "single", contents


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
                    help="split a dataset zip at/above this many bytes")
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

    # ── Valhalla members (exact on-device names — unzip straight into valhallaDir) ──
    val_members: list[tuple[Path, str]] = []
    for name in ("valhalla_tiles.tar", "manifest.json", "valhalla.json",
                 "admin.sqlite", "timezones.sqlite"):
        p = val_dir / name
        if p.exists():
            val_members.append((p, name))

    # ── Streets members (exact on-device names — unzip straight into streetsDir).
    # The builder's db filename is fixed (streets-brazil.sqlite) even for other
    # regions, and that legacy name is what the app already reads — kept as-is
    # inside the zip so no rename is needed on install.
    st_members: list[tuple[Path, str]] = []
    db_src = next((st_dir / n for n in STREETS_DB_NAMES if (st_dir / n).exists()), None)
    if db_src is not None:
        st_members.append((db_src, "streets-brazil.sqlite"))
    st_man = st_dir / "streets-manifest.json"
    if st_man.exists():
        st_members.append((st_man, "manifest.json"))

    if not val_members and not st_members:
        print("ERROR: nothing staged — source region dir has no recognizable dataset files.", file=sys.stderr)
        return 1

    def entry(p: Path) -> dict:
        return {"name": p.name, "size": p.stat().st_size, "sha256": sha256_of(p)}

    def section(archive: str, members: list[tuple[Path, str]]) -> dict:
        if not members:
            return {"present": False, "packaging": "single", "archive": archive,
                    "files": [], "contents": []}
        staged, packaging, contents = pack_dataset(
            members, archive, out, args.split_above, args.part_size)
        return {"present": True, "packaging": packaging, "archive": archive,
                "files": [entry(p) for p in staged], "contents": contents}

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
        "valhalla": section("valhalla.zip", val_members),
        "streets": section("streets.zip", st_members),
        "install": {
            # Mirrors update_offline_data.py DATASETS device paths. Each zip
            # extracts directly into its dir — member names already match.
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
