# Release process: publishing one region

Each region is an independent GitHub Release, tag `<slug>-vYYYY.MM.DD`
(e.g. `brazil-v2026.09.16`). Regions never block each other: re-releasing
Brazil does not touch Argentina.

## Prerequisites

- The finished files for one region, prepared with your own tooling:
  - Routing: a tile tarball (`valhalla_tiles.tar`), its `manifest.json`
    (`{"region","builtAtMs",...}`), the `valhalla.json` config, plus
    `admin.sqlite` / `timezones.sqlite` when produced.
  - Streets: the SQLite database and its `manifest.json`
    (`{"region","builtAtMs",...}`).
  - At least one of the two datasets is required; shipping both is preferred.
- `gh` CLI authenticated with `contents: write` on this repo.
- This repo checked out locally.

## 1. Stage the release

```bash
# from this repo root:
./scripts/publish_region.py \
  --slug brazil \
  --display-name Brazil \
  --tag brazil-v2026.09.16 \
  --valhalla-tar valhalla_tiles.tar \
  --valhalla-manifest manifest.json \
  --valhalla-config valhalla.json \
  --valhalla-admin admin.sqlite \
  --valhalla-timezones timezones.sqlite \
  --streets-db streets.sqlite \
  --streets-manifest streets-manifest.json \
  --built-at-ms 1750000000000 \
  --source-pbf brazil-latest.osm.pbf \
  --source-url https://download.geofabrik.de/south-america/brazil-latest.osm.pbf \
  --out dist/brazil-v2026.09.16
```

This reads (never moves) the input files and packs one zip per dataset:
`valhalla.zip` (with `valhalla_tiles.tar`, `manifest.json`, `valhalla.json`,
`admin.sqlite`/`timezones.sqlite` when given) and `streets.zip` (with the
database stored as `streets-brazil.sqlite`, plus `manifest.json`).
A zip that would exceed GitHub's 2 GiB per-asset limit is split into
`.part-aa/ab/...` chunks. It also writes `release-manifest.json` +
`SHA256SUMS` into the staging dir. `--built-at-ms` should be the actual
build date (defaults to now); `--source-pbf`/`--source-url` are free-text
provenance recorded in the manifest.

## 2. Validate

```bash
./scripts/verify_release.py --dir dist/brazil-v2026.09.16
# for split zips, also run the deep pass (reassembles + CRC-checks):
./scripts/verify_release.py --dir dist/brazil-v2026.09.16 --deep
```

Must print `OK`. Fix anything it flags. Never upload a release that does
not verify (the app trusts the manifest hashes on install).

## 3. Create the GitHub Release and upload

```bash
gh release create brazil-v2026.09.16 \
  --title "Brazil, 16 Sep 2026" \
  --notes "Offline routing + nearby streets for Brazil. OSM data © OpenStreetMap contributors (ODbL), via Geofabrik. See release-manifest.json for build provenance and SHA-256." \
  dist/brazil-v2026.09.16/*
```

Asset list after upload should be exactly the staging dir contents
(`gh release view brazil-v2026.09.16 --json assets --jq '.assets[].name'`).

## 4. Point the index at the new release

```bash
./scripts/build_index.py --slug brazil --display-name Brazil \
  --tag brazil-v2026.09.16 --repo FCPlech/DashMap-Tiles
git add tiles-index.json
git commit -m "index: brazil → brazil-v2026.09.16"
git push
```

The app discovers regions only through `tiles-index.json`. A release that
is not indexed is invisible to it. Double-check the raw URL serves the new
index afterwards.

## 5. Smoke-test (recommended)

- Download `release-manifest.json` from the release URL and re-run
  `verify_release.py --deep` against a fresh download of the assets.
- Confirm the app's future downloader (or manually:
  `cat valhalla.zip.part-* > valhalla.zip` + `sha256sum -c SHA256SUMS` +
  `unzip -t valhalla.zip`) reassembles, verifies and extracts,
  including the split-parts case.

## Notes

- **Re-releases:** same tag is immutable. If a published asset is bad,
  cut a new dated tag (`…-vYYYY.MM.DD` of the fix day), never
  `--clobber` over the old one. The index then moves to the new tag.
- **Partial datasets:** a release may ship only Valhalla or only streets
  (the other section gets `"present": false`), but prefer shipping both.
- **Staging dirs (`dist/`) are git-ignored**. Only `tiles-index.json`,
  scripts and docs are committed. The multi-GB files exist solely as
  Release assets.
- **Never commit** `*.zip`, `*.tar`, `*.sqlite`, `*.pbf` or
  `valhalla_tiles/` trees to this repo. `.gitignore` blocks them, and CI also
  checks.
