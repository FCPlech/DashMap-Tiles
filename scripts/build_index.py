#!/usr/bin/env python3
"""Add/update one region entry in tiles-index.json after publishing a release.

Usage:
    ./scripts/build_index.py --slug sul --display-name BR-Sul \\
        --country-code BR --country-name Brazil \\
        --tag BR-sul --repo FCPlech/DashMap-Tiles

Reads the tag's release-manifest.json from the local staging dir (to confirm
it exists and matches the tag) or just writes the index entry from flags with
--no-manifest-check. The entry's builtAtMs comes from the manifest when
readable, else the previous entry keeps its value. Keeps regions sorted by
slug, replaces the entry for the same slug in place.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "tiles-index.json"


def main() -> int:
    ap = argparse.ArgumentParser(description="Update tiles-index.json for one region.")
    ap.add_argument("--slug", required=True)
    ap.add_argument("--display-name", required=True)
    ap.add_argument("--country-code", default=None,
                    help="ISO country code (default: display-name prefix before '-', e.g. BR-Sul -> BR)")
    ap.add_argument("--country-name", default="",
                    help="country display name for the group header (e.g. Brazil)")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--repo", required=True, help="GitHub repo, e.g. FCPlech/DashMap-Tiles")
    ap.add_argument("--manifest", default=None,
                    help="local release-manifest.json to cross-check (default: build/<tag>/release-manifest.json if present)")
    ap.add_argument("--no-manifest-check", action="store_true")
    args = ap.parse_args()

    manifest_path = Path(args.manifest) if args.manifest else (ROOT / "build" / args.tag / "release-manifest.json")
    manifest_built_at = 0
    if not args.no_manifest_check:
        if not manifest_path.exists():
            print(f"ERROR: manifest not found at {manifest_path} (pass --no-manifest-check to skip).",
                  file=sys.stderr)
            return 1
        try:
            m = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as e:
            print(f"ERROR: manifest is not valid JSON: {e}", file=sys.stderr)
            return 1
        if m.get("tag") != args.tag or m.get("slug") != args.slug:
            print(f"ERROR: manifest tag/slug ({m.get('slug')}/{m.get('tag')}) "
                  f"does not match flags ({args.slug}/{args.tag}).", file=sys.stderr)
            return 1
        if isinstance(m.get("builtAtMs"), int):
            manifest_built_at = m["builtAtMs"]

    try:
        index = json.loads(INDEX.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: cannot read {INDEX}: {e}", file=sys.stderr)
        return 1

    previous = next((r for r in index.get("regions", []) if r.get("slug") == args.slug), {})
    code = args.country_code
    if code is None:
        prefix = args.display_name.split("-", 1)[0]
        code = prefix if len(prefix) == 2 and prefix.isalpha() and prefix.isupper() else ""
    entry = {
        "slug": args.slug,
        "displayName": args.display_name,
        "countryCode": code,
        "countryName": args.country_name or previous.get("countryName", ""),
        "latest": args.tag,
        "builtAtMs": manifest_built_at or previous.get("builtAtMs", 0),
        "releaseUrl": f"https://github.com/{args.repo}/releases/tag/{args.tag}",
        "manifestUrl": f"https://github.com/{args.repo}/releases/download/{args.tag}/release-manifest.json",
    }
    regions = [r for r in index.get("regions", []) if r.get("slug") != args.slug]
    regions.append(entry)
    regions.sort(key=lambda r: r["slug"])
    index["regions"] = regions
    INDEX.write_text(json.dumps(index, indent=2) + "\n")
    print(f"Updated {INDEX}: {args.slug} → {args.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
