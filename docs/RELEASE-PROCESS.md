# Release process — publishing one region

Each region is an independent GitHub Release, tag `<slug>-vYYYY.MM.DD`
(e.g. `brazil-v2026.09.16`). Regions never block each other: re-releasing
Brazil does not touch Argentina.

## Prerequisites

- A finished local build in the DashMap repo:
  `tools/map_build/regions/<slug>/{valhalla,streets}/`
  (built with `tools/update_offline_data.py` — Docker + osmium).
- `gh` CLI authenticated with `contents: write` on this repo.
- This repo checked out locally.

## 1. Stage the release

```bash
# from this repo root:
./scripts/publish_region.py \
  --region-dir ~/DashMap/tools/map_build/regions/brazil \
  --slug brazil \
  --display-name Brazil \
  --tag brazil-v2026.09.16 \
  --geofabrik-url https://download.geofabrik.de/south-america/brazil-latest.osm.pbf \
  --out dist/brazil-v2026.09.16
```

This copies (never moves) the build output, renames
`streets-brazil.sqlite` → `streets.sqlite`, splits the Valhalla tar if it
would exceed GitHub's 2 GiB per-asset limit, and writes
`release-manifest.json` + `SHA256SUMS` into the staging dir.

## 2. Validate

```bash
./scripts/verify_release.py --dir dist/brazil-v2026.09.16
```

Must print `OK`. Fix anything it flags — never upload a release that does
not verify (the app trusts the manifest hashes on install).

## 3. Create the GitHub Release and upload

```bash
gh release create brazil-v2026.09.16 \
  --title "Brazil — 16 Sep 2026" \
  --notes "Offline routing + nearby streets for Brazil. OSM data © OpenStreetMap contributors (ODbL), via Geofabrik. See release-manifest.json for build provenance and SHA-256." \
  dist/brazil-v2026.09.16/*
```

Asset list after upload should be exactly the staging dir contents
(`gh release view brazil-v2026.09.16 --json assets --jq '.assets[].name'`).

## 4. Point the index at the new release

```bash
./scripts/build_index.py --slug brazil --display-name Brazil \
  --tag brazil-v2026.09.16 --repo <org>/DashMap-Tiles
git add tiles-index.json
git commit -m "index: brazil → brazil-v2026.09.16"
git push
```

The app discovers regions only through `tiles-index.json` — a release that
is not indexed is invisible to it. Double-check the raw URL serves the new
index afterwards.

## 5. Smoke-test (recommended)

- Download `release-manifest.json` from the release URL and re-run
  `verify_release.py` against a fresh download of the assets.
- Confirm the app's future downloader (or a manual `cat` + `sha256sum -c`)
  reassembles and verifies the tar, including the split-parts case.

## Notes

- **Re-releases:** same tag is immutable — if a published asset is bad,
  cut a new dated tag (`…-vYYYY.MM.DD` of the fix day), never
  `--clobber` over the old one. The index then moves to the new tag.
- **Partial datasets:** a release may ship only Valhalla or only streets
  (the other section gets `"present": false`), but prefer shipping both.
- **Staging dirs (`dist/`) are git-ignored** — only `tiles-index.json`,
  scripts and docs are committed. The multi-GB files exist solely as
  Release assets.
- **Never commit** `*.tar`, `*.sqlite`, `*.pbf` or `valhalla_tiles/` trees
  to this repo — `.gitignore` blocks them, CI also checks.
