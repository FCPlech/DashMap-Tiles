#!/usr/bin/env python3
"""Validate a staged zipped release dir before `gh release create`.

Checks:
  - required assets present (at least one dataset zip + release-manifest.json + SHA256SUMS)
  - release-manifest.json shape (lightweight check, stdlib only — full schema
    is in ../release-manifest.schema.json)
  - every file listed in the manifest exists with matching size + sha256
  - each single zip opens cleanly and its member list matches the manifest's
    `contents` (exact on-device names — no rename needed on install)
  - split zip parts (.zip.part-aa, ...) are a complete sequence, none
    missing/truncated, with no reassembled zip alongside
  - `--deep` additionally reassembles split zips in a temp dir and runs the
    same zip/member checks on them (plus a CRC pass over every member)
  - SHA256SUMS covers every staged file (except itself) and verifies
  - no loose map-data files (.tar/.sqlite) outside the zips — the release
    ships zips only
  - no single asset >= 2 GiB (GitHub hard limit; publish_region.py splits at 1900 MiB)
  - tag naming convention <slug>-vYYYY.MM.DD

Usage:
    ./scripts/verify_release.py --dir dist/brazil-v2026.09.16
    ./scripts/verify_release.py --dir dist/brazil-v2026.09.16 --deep

Stdlib only. Exit 0 = OK, 1 = problems found.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

GITHUB_MAX_ASSET = 2 * 1024 * 1024 * 1024
TAG_RE = re.compile(r"^.+-v\d{4}\.\d{2}\.\d{2}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
# Only these basenames may sit in a release dir: anything else (notably loose
# .tar/.sqlite map data) means the staging leaked unpackaged files.
ALLOWED_RE = re.compile(r"^(release-manifest\.json|SHA256SUMS|.+\.zip|.+\.zip\.part-[a-z]{2})$")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fail(msg: str, errors: list[str]) -> None:
    errors.append(msg)
    print(f"FAIL: {msg}")


def check_zip_members(zip_path: Path, want_contents: list[str], where: str,
                      errors: list[str], crc: bool = False) -> None:
    """Member list (+ optionally CRC of every member) of one reassembled zip."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            have = z.namelist()
            if sorted(have) != sorted(want_contents):
                fail(f"{where}: zip members {have} do not match manifest contents "
                     f"{want_contents}", errors)
            if crc:
                bad = z.testzip()
                if bad is not None:
                    fail(f"{where}: CRC error in member {bad!r}", errors)
    except zipfile.BadZipFile as e:
        fail(f"{where}: not a readable zip: {e}", errors)


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate a staged DashMap-Tiles release dir.")
    ap.add_argument("--dir", required=True, help="staging dir from publish_region.py")
    ap.add_argument("--deep", action="store_true",
                    help="reassemble split zips in a temp dir and CRC-check them too")
    args = ap.parse_args()

    d = Path(args.dir)
    errors: list[str] = []
    if not d.is_dir():
        print(f"FAIL: not a directory: {d}")
        return 1

    for p in sorted(d.iterdir()):
        if p.is_file() and not ALLOWED_RE.match(p.name):
            fail(f"unexpected file {p.name} — releases ship zips + manifest + SHA256SUMS only "
                 f"(no loose .tar/.sqlite)", errors)

    manifest_path = d / "release-manifest.json"
    sums_path = d / "SHA256SUMS"
    if not manifest_path.exists():
        fail("release-manifest.json missing", errors)
    if not sums_path.exists():
        fail("SHA256SUMS missing", errors)
    if errors:
        return 1

    try:
        m = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        fail(f"release-manifest.json is not valid JSON: {e}", errors)
        return 1

    for key in ("schema", "slug", "displayName", "tag", "builtAtMs",
                "source", "valhalla", "streets", "install", "attribution"):
        if key not in m:
            fail(f"manifest missing key: {key}", errors)
    if m.get("schema") != 1:
        fail(f"manifest schema must be 1, got {m.get('schema')!r}", errors)
    if not TAG_RE.match(str(m.get("tag", ""))):
        fail(f"tag {m.get('tag')!r} does not match <slug>-vYYYY.MM.DD", errors)
    if isinstance(m.get("builtAtMs"), bool) or not isinstance(m.get("builtAtMs"), int):
        fail("builtAtMs must be an integer (epoch ms)", errors)

    if not m.get("valhalla", {}).get("present") and not m.get("streets", {}).get("present"):
        fail("release has neither valhalla nor streets data", errors)

    tmpdir = Path(tempfile.mkdtemp(prefix="verify-tiles-")) if args.deep else None
    try:
        # Every manifest-listed file must exist with matching size+hash; zips get
        # their member list checked against `contents`.
        for section in ("valhalla", "streets"):
            sec = m.get(section, {})
            archive = sec.get("archive", "")
            if sec.get("present") and (not archive.endswith(".zip") or "/" in archive):
                fail(f"{section}: archive {archive!r} must be a plain .zip filename", errors)
            contents = sec.get("contents", []) if sec.get("present") else []
            files = sec.get("files", [])
            for entry in files:
                name = entry.get("name", "")
                p = d / name
                if not p.exists():
                    fail(f"manifest lists {name} but it is not staged", errors)
                    continue
                if p.stat().st_size != entry.get("size"):
                    fail(f"{name}: size mismatch (disk {p.stat().st_size} vs manifest {entry.get('size')})", errors)
                if not SHA_RE.match(str(entry.get("sha256", ""))):
                    fail(f"{name}: manifest sha256 is not a 64-hex string", errors)
                elif sha256_of(p) != entry.get("sha256"):
                    fail(f"{name}: sha256 mismatch", errors)
            if not sec.get("present"):
                continue
            if sec.get("packaging") == "single":
                if len(files) != 1 or files[0].get("name") != archive:
                    fail(f"{section}: single packaging must list exactly [{archive}]", errors)
                elif (d / archive).exists():
                    check_zip_members(d / archive, contents, archive, errors, crc=args.deep)
            elif sec.get("packaging") == "split":
                seq = sorted(e.get("name", "").split(".part-")[1]
                             for e in files if ".part-" in e.get("name", ""))
                expected = [f"{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(len(files))]
                if seq != expected or len(files) < 2:
                    fail(f"{section}: split parts not a complete sequence (have {seq})", errors)
                if (d / archive).exists():
                    fail(f"{section}: both {archive}.part-* and reassembled {archive} staged — "
                         f"ship one or the other", errors)
                if args.deep and tmpdir is not None and not any(
                        e.startswith(f"{section}:") for e in errors):
                    reasm = tmpdir / archive
                    with reasm.open("wb") as f:
                        for e in files:
                            with (d / e["name"]).open("rb") as part:
                                shutil.copyfileobj(part, f, 8 * 1024 * 1024)
                    check_zip_members(reasm, contents, f"{archive} (reassembled)", errors, crc=True)
            else:
                fail(f"{section}: packaging must be 'single' or 'split'", errors)

        # SHA256SUMS must cover everything staged (except itself) and verify.
        sums: dict[str, str] = {}
        try:
            for line in sums_path.read_text().splitlines():
                if not line.strip():
                    continue
                h, _, name = line.partition("  ")
                if not SHA_RE.match(h.strip()) or not name.strip():
                    fail(f"SHA256SUMS malformed line: {line!r}", errors)
                    continue
                sums[name.strip()] = h.strip()
        except OSError as e:
            fail(f"cannot read SHA256SUMS: {e}", errors)

        for p in sorted(d.iterdir()):
            if p.name == "SHA256SUMS" or not p.is_file():
                continue
            if p.name not in sums:
                fail(f"{p.name} staged but missing from SHA256SUMS", errors)
            elif sha256_of(p) != sums[p.name]:
                fail(f"{p.name}: SHA256SUMS hash mismatch", errors)
            if p.stat().st_size >= GITHUB_MAX_ASSET:
                fail(f"{p.name} is {p.stat().st_size} bytes — over GitHub's 2 GiB per-asset limit", errors)
    finally:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)

    if errors:
        print(f"\n{len(errors)} problem(s) in {d}")
        return 1
    total = sum(p.stat().st_size for p in d.iterdir() if p.is_file())
    print(f"OK: {d} valid ({total / (1024 * 1024):.1f} MB, tag {m.get('tag')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
