#!/usr/bin/env python3
"""Publish one staged region dir as a GitHub Release, creating or replacing.

Tags are stable per region (e.g. BR-sul, see verify_release.py): rebuilding
a region replaces the SAME release instead of adding a new one. Freshness
lives in the manifest's builtAtMs and in the release title, never in the tag.

Given --dir (output of publish_region.py: zips/parts + release-manifest.json
+ SHA256SUMS):

  - release missing -> `gh release create` with every staged file.
  - release exists  -> assets no longer in the staging set are deleted
    (covers a single/single flip leaving stale .part files behind),
    current files are uploaded with --clobber, and title/notes are refreshed.

Usage:
    ./scripts/publish_release.py --dir build/BR-sul

    # then point the index at it (unchanged tag, new build date picked up
    # from the manifest automatically):
    ./scripts/build_index.py --slug sul --display-name BR-Sul --tag BR-sul \\
        --repo FCPlech/DashMap-Tiles

Needs the `gh` CLI, authenticated with contents:write on the repo.
Stdlib only otherwise.
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

DEFAULT_REPO = "FCPlech/DashMap-Tiles"
ATTRIBUTION_SHORT = "Map data (c) OpenStreetMap contributors (ODbL), via Geofabrik."


def run_gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", required=True, help="staged release dir from publish_region.py")
    ap.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repo (default: %(default)s)")
    ap.add_argument("--title", default=None,
                    help='release title (default: "<display>, <today>")')
    ap.add_argument("--notes", default=None, help="release notes (default: built from manifest)")
    args = ap.parse_args()

    d = Path(args.dir)
    try:
        manifest = json.loads((d / "release-manifest.json").read_text())
    except (OSError, ValueError) as e:
        print(f"ERROR: cannot read {d}/release-manifest.json: {e}", file=sys.stderr)
        return 1
    tag = str(manifest.get("tag", ""))
    display = str(manifest.get("displayName", tag))
    if not tag:
        print("ERROR: manifest has no tag.", file=sys.stderr)
        return 1

    staged = sorted(p for p in d.iterdir() if p.is_file())
    if not staged:
        print(f"ERROR: nothing staged in {d}.", file=sys.stderr)
        return 1

    today = datetime.date.today().strftime("%d %b %Y").lstrip("0")
    title = args.title or f"{display}, {today}"
    notes = args.notes or (
        f"Offline routing + nearby streets for {display}. {ATTRIBUTION_SHORT} "
        f"See release-manifest.json for build provenance and SHA-256."
    )

    existing = run_gh(["release", "view", tag, "--repo", args.repo,
                       "--json", "assets", "--jq", ".assets[].name"])
    if existing.returncode != 0:
        print(f"creating release {tag} ...")
        created = subprocess.run(
            ["gh", "release", "create", tag, "--repo", args.repo,
             "--title", title, "--notes", notes,
             *[str(p) for p in staged]])
        if created.returncode != 0:
            print(f"ERROR: could not create release {tag}.", file=sys.stderr)
            return 1
    else:
        live = set(existing.stdout.split())
        want = {p.name for p in staged}
        for stale in sorted(live - want):
            print(f"removing stale asset {stale} ...")
            if run_gh(["release", "delete-asset", tag, stale,
                       "--repo", args.repo, "--yes"]).returncode != 0:
                print(f"ERROR: could not remove stale asset {stale}.", file=sys.stderr)
                return 1
        print(f"uploading {len(staged)} files to {tag} ...")
        if subprocess.run(
            ["gh", "release", "upload", tag, "--repo", args.repo,
             *[str(p) for p in staged], "--clobber"]).returncode != 0:
            print(f"ERROR: upload to {tag} failed.", file=sys.stderr)
            return 1
        if run_gh(["release", "edit", tag, "--repo", args.repo,
                   "--title", title, "--notes", notes]).returncode != 0:
            print(f"ERROR: could not refresh title/notes of {tag}.", file=sys.stderr)
            return 1

    print(f"https://github.com/{args.repo}/releases/tag/{tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
