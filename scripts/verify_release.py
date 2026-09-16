#!/usr/bin/env python3
"""Validate a staged release dir before `gh release create`.

Checks:
  - required assets present (at least one dataset + release-manifest.json + SHA256SUMS)
  - release-manifest.json matches release-manifest.schema.json (lightweight check, stdlib only)
  - every file listed in the manifest exists with matching size + sha256
  - SHA256SUMS covers every staged file (except itself) and verifies
  - split tar parts (.part-aa, ...) are a complete sequence, none missing/truncated
  - no single asset >= 2 GiB (GitHub hard limit; publish_region.py splits at 1900 MiB)
  - tag/slug naming convention <slug>-vYYYY.MM.DD

Usage:
    ./scripts/verify_release.py --dir dist/brazil-v2026.09.16

Stdlib only. Exit 0 = OK, 1 = problems found.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

GITHUB_MAX_ASSET = 2 * 1024 * 1024 * 1024
TAG_RE = re.compile(r"^.+-v\d{4}\.\d{2}\.\d{2}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fail(msg: str, errors: list[str]) -> None:
    errors.append(msg)
    print(f"FAIL: {msg}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate a staged DashMap-Tiles release dir.")
    ap.add_argument("--dir", required=True, help="staging dir from publish_region.py")
    args = ap.parse_args()

    d = Path(args.dir)
    errors: list[str] = []
    if not d.is_dir():
        print(f"FAIL: not a directory: {d}")
        return 1

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

    # Every manifest-listed file must exist with matching size+hash.
    listed: set[str] = set()
    for section in ("valhalla", "streets"):
        files = m.get(section, {}).get("files", [])
        for entry in files:
            name = entry.get("name", "")
            listed.add(name)
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

    # Split-tar completeness: parts must form an unbroken aa,ab,... sequence.
    parts = sorted(p for p in d.iterdir() if ".part-" in p.name)
    if parts:
        stems = {p.name.split(".part-")[0] for p in parts}
        for stem in stems:
            seq = sorted(p.name.split(".part-")[1] for p in parts if p.name.startswith(stem + ".part-"))
            expected = [f"{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(len(seq))]
            if seq != expected:
                fail(f"{stem}: split parts not a complete sequence (have {seq}, want {expected})", errors)
        # A split tar must NOT also ship the reassembled single file.
        for stem in stems:
            if (d / stem).exists():
                fail(f"{stem}: both split parts and reassembled single file staged — ship one or the other", errors)

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

    if errors:
        print(f"\n{len(errors)} problem(s) in {d}")
        return 1
    total = sum(p.stat().st_size for p in d.iterdir() if p.is_file())
    print(f"OK: {d} valid ({total / (1024 * 1024):.1f} MB, tag {m.get('tag')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
