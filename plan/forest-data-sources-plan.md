# Plan: Alternative Forest Data Sources

Adding redundancy and richer data to `forrest_type.py`, ordered by
"lowest hanging fruit" first. Each step is small, self-contained, and
leaves the tool working on its own. Nothing blocks the OSM path we
already have — each new source slots in as an *additional* source.

## Why

The Overpass API is free but flaky (504s under load, unpredictable
latency). OSM is also sparse in rural areas and carries no appearance
data. The alternatives below give us:

1. a second, independent server for species evidence (GBIF),
2. full OSM detail with zero network flakiness (local extract),
3. an authoritative, server-independent forest *type* (rasters).

Design principle: sources are **complementary**. OSM/rasters tell us
*where the forest is and what type*; OSM/GBIF tell us *which species*.

## Status of current sources

| Source | Gives us | Status |
|---|---|---|
| Overpass API (OSM) | forest areas, leaf_type/leaf_cycle, tree genus/species, named forests | ✅ implemented (retries + mirror fallback) |
| Wikipedia REST | species descriptions | ✅ implemented (best-effort) |

---

## Phase 1 — GBIF species observations  🍒 lowest hanging fruit

**Goal:** list tree species *actually observed* near the point, from a
second free server (no account, no key).

- API: `https://api.gbif.org/v1/occurrence/search?lat=..&lon=..&radius=..&limit=..`
  (returns real occurrences: iNaturalist, herbaria, field records).
- Implementation: new function `fetch_gbif(lat, lon, radius)` using the
  existing `requests` pattern; aggregate `species` (and `genus` when
  species is blank) with a `Counter`, same as we do for OSM tags.
- Report: new section "Species observed nearby (GBIF)".
- Failure handling: GBIF is also a free service — best-effort only,
  never blocks the report (same policy as Wikipedia lookups).
- Flag: `--no-gbif` to skip; automatic skip if the call fails.
- Tests: offline test for the aggregation function with a stubbed
  response.

**Effort:** ~half a day. **Risk:** low — same shape as existing code.
**Outcome:** species evidence even where nobody mapped trees in OSM.

## Phase 2 — Local OSM extract (no Overpass server at all)

**Goal:** same OSM data we get from Overpass, but read locally —
deterministic, fast, repeatable.

- Download a Geofabrik country/region extract once, e.g.
  `sweden-latest.osm.pbf` (~1 GB), into a `data/` dir (gitignored).
- Parse with `pyosmium` (pip-installable) or shell out to `osmium`
  (pyosmium keeps us pure-Python).
- New flag: `--osm-pbf data/sweden-latest.osm.pbf` — when given, skip
  Overpass entirely and filter the extract for the same tags
  (`natural=wood`, `landuse=forest`, `natural=tree`, `natural=tree_row`)
  within the radius (great-circle distance check on coordinates).
- The output feeds the *same* `summarize()` function — no changes to
  the report code.
- Document the one-off download step in the README.

**Effort:** ~1 day (bbox filtering and distance math are the only new
logic). **Risk:** low-medium — file size and parse time; mitigate by
suggesting smaller regional extracts.
**Outcome:** offline mode; immune to Overpass 504s.

## Phase 3 — CORINE Land Cover raster (authoritative forest type, offline)

**Goal:** a classified, pan-European forest type that never depends on
a live server.

- Download the CORINE 2018/2021 raster (100 m, classes 311 broadleaf /
  312 conifer / 313 mixed) once from land.copernicus.eu (free account)
  or an EEA mirror; store in `data/` (gitignored, ~1-2 GB GeoTIFF).
- Read with `rasterio` (pip-installable): sample the raster in the
  radius, report the class mix as a third "Forest type" view.
- New flag: `--corine data/corine.tif`; silently skipped when absent.
- Report: "CORINE forest classes (% of sampled cells)".

**Effort:** ~1 day. **Risk:** low — read-only raster sampling.
**Outcome:** an "official" forest type next to the OSM-tag estimate;
the two often agree, which builds trust in both.

## Phase 4 — Copernicus HRL Forest Type via CDSE/openEO

**Goal:** 10 m resolution forest type (FTY) + dominant leaf type (DLT)
— the modern replacement for CORINE.

- Requires a free account on the Copernicus Data Space Ecosystem.
- Use the `openeo` Python client: a small process graph that loads the
  HRL_FTY/DLT collection, applies the spatial filter around our point,
  and returns aggregated class counts.
- New flag: `--cdse` (credentials read from env vars / config file).
- Report: "Copernicus HRL forest type (10 m)".

**Effort:** ~1-2 days (account setup + openEO learning curve).
**Risk:** medium — service conventions change; needs internet and an
account. Keep behind a flag so the core tool never depends on it.
**Outcome:** finer-grained, Sentinel-based forest type; good demo of
the "future Flutter app" data pipeline.

## Phase 5 — Google Earth Engine modes (optional, heaviest)

**Goal:** global coverage and a managed platform, if we outgrow
Europe-focused rasters.

- Requires a Google account + `earthengine-api` + project setup.
- Candidate datasets (all verified present in the GEE catalog):
  - `COPERNICUS/CORINE/V20/100m` — same classes as Phase 3, but
    sampled server-side (no local download).
  - `ESA/WorldCover/v200` — 10 m global land cover.
  - `UMD/hansen/global_forest_change` — tree canopy density %.
- New flag: `--gee <dataset>`, lazy-importing `ee` only when used.
- Note: GEE is *not* a home for the Copernicus HRL FTY/DLT layers —
  those are only on CDSE (verified 404 in the GEE catalog).

**Effort:** ~1-2 days. **Risk:** medium — account/auth friction and a
heavy dependency; deliberately last.

---

## Suggested order and milestones

| Phase | Deliverable | Needs account? | Effort |
|---|---|---|---|
| 1 | GBIF species section in report | no | ½ day |
| 2 | `--osm-pbf` offline mode | no | 1 day |
| 3 | `--corine` raster forest type | no (free download) | 1 day |
| 4 | `--cdse` HRL 10 m forest type | yes (free) | 1-2 days |
| 5 | `--gee` cloud modes | yes (Google) | 1-2 days |

Phases 1-3 need no new accounts and can be tested fully offline once
their data files are fetched — that is why they come first.

## Cross-cutting notes (apply to every phase)

- Every new source is **best-effort and optional**: a failure prints a
  short warning and never blocks the report.
- All downloaded data lives in `data/` (gitignored); keep
  `forrest_type.py` a single-file CLI for as long as practical — if the
  file grows past ~600 lines, split into `sources/` modules then.
- Keep every pure function (aggregation, label building, distance math)
  unit tested offline; network code stays behind thin stubs.
- README gains a short "Data sources" table as each phase lands.

## Explicitly out of scope (for now)

- The Flutter/Android app (README's future extension) — this plan
  builds the data pipeline it would consume.
- National forest inventories (Swedish NFI etc.) — country-specific
  formats; revisit if GBIF coverage proves thin for a target region.