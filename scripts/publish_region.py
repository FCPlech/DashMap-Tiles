#!/usr/bin/env python3
"""Stage prepared offline map files into zipped release assets.

Packs one zip per dataset for a single region, plus a generated
`release-manifest.json` and `SHA256SUMS`, ready to upload as GitHub
Release assets.

This script does NOT build maps and knows nothing about whatever tooling
produced the input files: it only packages finished files you hand it.
Each release hosts two independent datasets for one region:

  - Offline Routing (Valhalla tiles)
  - Nearby Streets (SQLite database)

Input files are given explicitly — no directory layout is assumed:

  --valhalla-tar FILE         routing tile tarball (dataset present iff given)
  --valhalla-manifest FILE    routing manifest.json
  --valhalla-config FILE      routing valhalla.json
  --valhalla-admin FILE       routing admin database (optional)
  --valhalla-timezones FILE   routing timezones database (optional)
  --streets-db FILE           streets SQLite database (dataset present iff given)
  --streets-manifest FILE     streets manifest.json

Staged output (<out-dir>/):

    valhalla.zip               (or valhalla.zip.part-aa/ab/... when split)
    streets.zip                (or streets.zip.part-aa/ab/... when split)
    release-manifest.json      (schema: ../release-manifest.schema.json)
    SHA256SUMS

Each zip contains the EXACT on-device filenames, so the app (or a manual
`unzip`) extracts straight into the target dir with no renaming:

    valhalla.zip  →  valhalla_tiles.tar, manifest.json, valhalla.json,
                     admin.sqlite?, timezones.sqlite?
                     (installs into the release manifest's valhallaDir)
    streets.zip   →  streets-brazil.sqlite, manifest.json
                     (installs into the release manifest's streetsDir)

GitHub rejects release assets over 2 GB, so zips >= --split-above are split
into --part-size chunks (`split -b` style naming: .part-aa, .part-ab, ...).
Reassembly is `cat valhalla.zip.part-* > valhalla.zip`, then `unzip`.

Usage:
    ./scripts/publish_region.py \\
        --slug brazil --display-name Brazil --tag brazil-v2026.09.16 \\
        --valhalla-tar valhalla_tiles.tar --valhalla-manifest manifest.json \\
        --valhalla-config valhalla.json --valhalla-admin admin.sqlite \\
        --valhalla-timezones timezones.sqlite \\
        --streets-db streets.sqlite --streets-manifest streets-manifest.json \\
        --built-at-ms 1750000000000 --source-pbf brazil-latest.osm.pbf \\
        --source-url https://download.geofabrik.de/south-america/brazil-latest.osm.pbf \\
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


def need_file(value: str | None, flag: str) -> Path | None:
    """Resolve an optional input-file flag, rejecting missing paths."""
    if value is None:
        return None
    p = Path(value)
    if not p.is_file():
        print(f"ERROR: {flag} points at {value}, which is not a file.", file=sys.stderr)
        raise SystemExit(1)
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--slug", required=True, help="region slug, e.g. brazil")
    ap.add_argument("--display-name", required=True, help='human name, e.g. "Brazil"')
    ap.add_argument("--tag", required=True, help="release tag, e.g. brazil-v2026.09.16")
    ap.add_argument("--out", required=True, help="staging output dir")
    ap.add_argument("--valhalla-tar", default=None,
                    help="routing tile tarball → stored as valhalla_tiles.tar (dataset present iff given)")
    ap.add_argument("--valhalla-manifest", default=None,
                    help="routing manifest file → stored as manifest.json")
    ap.add_argument("--valhalla-config", default=None,
                    help="routing config file → stored as valhalla.json")
    ap.add_argument("--valhalla-admin", default=None,
                    help="routing admin db → stored as admin.sqlite")
    ap.add_argument("--valhalla-timezones", default=None,
                    help="routing timezones db → stored as timezones.sqlite")
    ap.add_argument("--streets-db", default=None,
                    help="streets SQLite database → stored as streets-brazil.sqlite (dataset present iff given)")
    ap.add_argument("--streets-manifest", default=None,
                    help="streets manifest file → stored as manifest.json")
    ap.add_argument("--built-at-ms", type=int, default=None,
                    help="build timestamp (epoch ms); defaults to now — set it to the actual build date")
    ap.add_argument("--source-pbf", default="",
                    help="source extract filename, for provenance (free text)")
    ap.add_argument("--source-url", default="",
                    help="source extract URL, for provenance (free text)")
    ap.add_argument("--valhalla-version", default="",
                    help="routing engine build id, for provenance (free text)")
    ap.add_argument("--split-above", type=int, default=DEFAULT_SPLIT_ABOVE,
                    help="split a dataset zip at/above this many bytes")
    ap.add_argument("--part-size", type=int, default=DEFAULT_PART_SIZE,
                    help="bytes per split part")
    args = ap.parse_args()

    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        print(f"ERROR: --out {out} exists and is not empty (refusing to mix releases).", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)

    # ── Valhalla members (exact on-device names — unzip straight into valhallaDir) ──
    val_inputs = [
        (need_file(args.valhalla_tar, "--valhalla-tar"), "valhalla_tiles.tar"),
        (need_file(args.valhalla_manifest, "--valhalla-manifest"), "manifest.json"),
        (need_file(args.valhalla_config, "--valhalla-config"), "valhalla.json"),
        (need_file(args.valhalla_admin, "--valhalla-admin"), "admin.sqlite"),
        (need_file(args.valhalla_timezones, "--valhalla-timezones"), "timezones.sqlite"),
    ]
    val_members = [(p, arc) for p, arc in val_inputs if p is not None]
    if args.valhalla_tar is None and val_members:
        print("ERROR: routing side files given without --valhalla-tar — "
              "the tile tarball is required for the dataset.", file=sys.stderr)
        return 1

    # ── Streets members (exact on-device names — unzip straight into streetsDir).
    # Whatever the input db filename is, it is stored as streets-brazil.sqlite:
    # that is the filename the app reads, so no rename is needed on install.
    st_inputs = [
        (need_file(args.streets_db, "--streets-db"), "streets-brazil.sqlite"),
        (need_file(args.streets_manifest, "--streets-manifest"), "manifest.json"),
    ]
    st_members = [(p, arc) for p, arc in st_inputs if p is not None]
    if args.streets_db is None and st_members:
        print("ERROR: --streets-manifest given without --streets-db — "
              "the database is required for the dataset.", file=sys.stderr)
        return 1

    if not val_members and not st_members:
        print("ERROR: nothing to stage — give --valhalla-tar and/or --streets-db.", file=sys.stderr)
        return 1

    built_at = args.built_at_ms if args.built_at_ms is not None else int(time.time() * 1000)

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
            "pbfFile": args.source_pbf,
            "sourceUrl": args.source_url,
            "valhallaVersion": args.valhalla_version,
        },
        "valhalla": section("valhalla.zip", val_members),
        "streets": section("streets.zip", st_members),
        "install": {
            # Fixed on-device locations the app extracts each zip into —
            # member names already match, so extraction needs no renaming.
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
