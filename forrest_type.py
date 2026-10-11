#!/usr/bin/env python3
"""forrest_type.py - proof-of-concept forest type classifier.

Given a latitude/longitude, queries the OpenStreetMap Overpass API for nearby
forest/woodland areas (natural=wood, landuse=forest) and mapped trees
(natural=tree, natural=tree_row). Summarises the forest types (leaf_type /
leaf_cycle), the tree species and genera found in the OSM tags, and - where
possible - adds a short description of each species from the Wikipedia REST
API (OSM carries no appearance data of its own). As a second, independent
species source it lists species actually observed near the point, from the
GBIF occurrence API (Phase 1 of plan/forest-data-sources-plan.md).

Usage:
    python forrest_type.py LAT LON [--radius 1000] [--raw]
                           [--no-wiki] [--no-gbif] [--endpoint URL]
                           [--osm-pbf FILE] [--corine FILE]

Examples:
    python forrest_type.py 59.3293 18.0686 --radius 2000   # Stockholm
    python forrest_type.py 57.0 14.5                       # rural Sweden
    python forrest_type.py 0 0 --raw                       # ocean, raw dump
    python forrest_type.py 59.3293 18.0686 --osm-pbf data/sweden-latest.osm.pbf
    python forrest_type.py 59.3293 18.0686 --corine data/clc2018.tif

Requires: requests (see requirements.txt). osmium (pyosmium) is only
needed for --osm-pbf and rasterio+pyproj only for --corine. Pure
functions (build_query, summarize, build_gbif_geometry,
gbif_facets_to_counts, resolve_gbif_names, distance_m, point_in_ring,
forest_estimate) make no network calls and are unit tested in tests/.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter

import requests

DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"
MIRROR_ENDPOINT = "https://overpass.kumi.systems/api/interpreter"
WIKI_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/"
GBIF_API = "https://api.gbif.org/v1/occurrence/search"
GBIF_SPECIES_URL = "https://api.gbif.org/v1/species/{key}"
PLANTAE_KINGDOM_KEY = 6
MAX_RADIUS = 5000  # metres; keep the public Overpass server happy
DEFAULT_RADIUS = 1000

HEADERS = {
    # overpass-api.de's front server (mod_security) rejects UAs that look like
    # the default python-requests one; a plain product token works.
    "User-Agent": "forrest-type-poc/0.1",
}


# --------------------------------------------------------------------------
# Query building (no network)
# --------------------------------------------------------------------------

def build_query(lat: float, lon: float, radius: int) -> str:
    """Build the Overpass QL query for forests and trees around a point.

    ``out tags center`` skips full geometry so responses stay small.
    """
    return f"""[out:json][timeout:25];
(
  nwr["natural"="wood"](around:{radius},{lat},{lon});
  nwr["landuse"="forest"](around:{radius},{lat},{lon});
  nwr["natural"="tree_row"](around:{radius},{lat},{lon});
  node["natural"="tree"](around:{radius},{lat},{lon});
);
out tags center;"""


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------

def fetch(query: str, endpoint: str = DEFAULT_ENDPOINT) -> dict:
    """POST the query to Overpass and return the parsed JSON dict.

    Retries on 429/504 with backoff, then falls back to the mirror endpoint.
    Any failure prints to stderr and exits with code 1.
    """
    candidates = [endpoint] if endpoint != DEFAULT_ENDPOINT else [endpoint, MIRROR_ENDPOINT]

    for i, url in enumerate(candidates):
        payload = {"data": query}
        for attempt in range(3):  # initial try + 2 retries
            try:
                resp = requests.post(url, data=payload, headers=HEADERS, timeout=30)
            except requests.RequestException as exc:
                print(f"error: request to {url} failed: {exc}", file=sys.stderr)
                break  # fall through to next candidate endpoint
            if resp.status_code != 200:
                if resp.status_code in (429, 504) and attempt < 2:
                    delay = 2 ** (attempt + 1)
                    print(
                        f"warning: {url} returned {resp.status_code}, "
                        f"retrying in {delay}s",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                print(
                    f"error: {url} returned HTTP {resp.status_code} "
                    f"(the public servers can be overloaded)",
                    file=sys.stderr,
                )
                break  # give up on this endpoint, try next
            try:
                body = resp.json()
            except ValueError:
                print(f"error: {url} returned invalid JSON", file=sys.stderr)
                break
            # Overpass can return HTTP 200 with a "remark" such as
            # "runtime error: Query timed out ..." and partial/empty
            # elements. Treat that as a retryable failure, not success.
            # Guard against non-dict JSON roots (HTTP 200 but not an
            # Overpass response object).
            if not isinstance(body, dict):
                print(f"error: {url} returned unexpected JSON (not an object)", file=sys.stderr)
                break
            remark = body.get("remark") or ""
            if "runtime error" in remark:
                if attempt < 2:
                    delay = 2 ** (attempt + 1)
                    print(
                        f"warning: {url} Overpass remark: {remark.strip()} - "
                        f"retrying in {delay}s",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                print(f"error: {url} failed: {remark.strip()}", file=sys.stderr)
                break
            return body
        print(f"warning: endpoint {url} failed", file=sys.stderr)

    print(
        "error: all Overpass endpoints failed. The public servers rate-limit "
        "and can be slow - try again in a minute or a smaller --radius.",
        file=sys.stderr,
    )
    sys.exit(1)


# --------------------------------------------------------------------------
# Summarise (no network)
# --------------------------------------------------------------------------

def _split_multi(value: str) -> list[str]:
    """Split an OSM multi-value string ('a;b' or 'a;b;c') into parts."""
    return [p.strip() for p in value.split(";") if p.strip()]


# Typographic apostrophes/quotes and the multiplication sign get collapsed
# to their ASCII equivalents so the same species written with different
# characters ('Prunus 'Accolade'' vs 'Prunus 'Accolade'') merges into one
# count. Only applied to display names, never to wikidata/wikipedia keys.
_NAME_TRANSLATE = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u2033": '"',
    "\u00d7": "x",  # hybrid sign: 'Aesculus \u00d7 carnea' -> 'Aesculus x carnea'
})


def _normalize_name(value: str) -> str:
    """Normalise a species/genus display name for counting."""
    return value.translate(_NAME_TRANSLATE).strip()


def summarize(elements: list[dict]) -> dict:
    """Aggregate Overpass elements into forest-type and species summaries."""
    forest_areas: list[dict] = []
    trees: list[dict] = []
    tree_rows: list[dict] = []

    for el in elements:
        tags = el.get("tags") or {}
        if tags.get("natural") == "tree":
            trees.append(tags)
        elif tags.get("natural") == "tree_row":
            tree_rows.append(tags)
        elif tags.get("natural") == "wood" or tags.get("landuse") == "forest":
            forest_areas.append(tags)

    leaf_type = Counter(t["leaf_type"] for t in forest_areas if "leaf_type" in t)
    leaf_cycle = Counter(t["leaf_cycle"] for t in forest_areas if "leaf_cycle" in t)

    names = [t["name"] for t in forest_areas if "name" in t]
    protect_class = Counter(t["protect_class"] for t in forest_areas if "protect_class" in t)
    wood = Counter(t["wood"] for t in forest_areas if "wood" in t)

    species: Counter = Counter()
    genera: Counter = Counter()
    species_wikidata: Counter = Counter()
    genus_wikidata: Counter = Counter()
    wikipedia: Counter = Counter()

    for tags in forest_areas + trees:
        for key, counter, normalize in (
            ("species", species, True),
            ("genus", genera, True),
            ("taxon", species, True),
            ("species:wikidata", species_wikidata, False),
            ("genus:wikidata", genus_wikidata, False),
            ("wikipedia", wikipedia, False),
        ):
            if key in tags:
                for part in _split_multi(tags[key]):
                    if normalize:
                        part = _normalize_name(part)
                    counter[part] += 1

    # Approximate forest-type label from leaf_type + leaf_cycle.
    forest_type = "unspecified"
    if forest_areas:
        lt = leaf_type.most_common(1)[0][0] if leaf_type else None
        lc = leaf_cycle.most_common(1)[0][0] if leaf_cycle else None
        forest_type = forest_label(lt, lc)

    return {
        "forest_area_count": len(forest_areas),
        "tree_count": len(trees),
        "tree_row_count": len(tree_rows),
        "leaf_type": leaf_type,
        "leaf_cycle": leaf_cycle,
        "names": names,
        "protect_class": protect_class,
        "wood": wood,
        "species": species,
        "genera": genera,
        "species_wikidata": species_wikidata,
        "genus_wikidata": genus_wikidata,
        "wikipedia": wikipedia,
        "forest_type": forest_type,
    }


def forest_label(leaf_type: str | None, leaf_cycle: str | None) -> str:
    """Turn leaf_type/leaf_cycle tags into a readable forest-type label."""
    type_word = {
        "broadleaved": "broadleaf",
        "needleleaved": "conifer",
        "mixed": "mixed",
    }.get(leaf_type)
    cycle_word = {
        "deciduous": "deciduous",
        "evergreen": "evergreen",
        "semi_deciduous": "semi-deciduous",
        "mixed": "mixed",
    }.get(leaf_cycle)

    if type_word and cycle_word:
        return f"{cycle_word.capitalize()} {type_word} forest"
    if type_word:
        return f"{type_word.capitalize()} forest ({leaf_cycle or 'leaf cycle unspecified'})"
    if cycle_word:
        return f"{cycle_word.capitalize()} forest (leaf type unspecified)"
    return "unspecified"


# --------------------------------------------------------------------------
# Wikipedia descriptions (second data source)
# --------------------------------------------------------------------------

def describe_species(name: str, session: requests.Session, cache: dict) -> str | None:
    """Fetch a short Wikipedia summary for a species/genus name.

    Returns the extract text, or None on any failure (404, network, ...).
    Results are cached across calls.
    """
    if name in cache:
        return cache[name]

    try:
        resp = session.get(WIKI_SUMMARY_URL + requests.utils.quote(name), timeout=10)
    except requests.RequestException:
        cache[name] = None
        return None

    extract = None
    if resp.status_code == 200:
        try:
            extract = resp.json().get("extract")
        except ValueError:
            extract = None

    cache[name] = extract if extract else None
    return cache[name]


# --------------------------------------------------------------------------
# Local OSM extract (Phase 2: same data as Overpass, no network)
# --------------------------------------------------------------------------

def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two points (haversine)."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def point_in_ring(lat: float, lon: float, ring: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test.

    ``ring`` is a list of (lat, lon) tuples describing a closed polygon
    boundary (no explicit repeat of the first vertex is required). Pure
    function; used to detect 'small circle fully inside a large forest'.
    """
    inside = False
    n = len(ring)
    for i in range(n):
        lat1, lon1 = ring[i]
        lat2, lon2 = ring[(i + 1) % n]
        if (lon1 > lon) != (lon2 > lon):
            lat_cross = lat1 + (lon - lon1) * (lat2 - lat1) / (lon2 - lon1)
            if lat > lat_cross:
                inside = not inside
    return inside


def _area_outer_rings(area) -> list[list[tuple[float, float]]]:
    """Collect the outer rings of a pyosmium Area as (lat, lon) point lists."""
    rings: list[list[tuple[float, float]]] = []
    for ring in area.outer_rings():
        rings.append([(location.lat, location.lon) for location in ring])
    return rings


def _way_node_points(way) -> list[tuple[float, float]]:
    """Collect valid (lat, lon) points of a pyosmium Way's nodes."""
    return [(n.location.lat, n.location.lon)
            for n in way.nodes if n.location.valid()]


def read_osm_extract(path: str, lat: float, lon: float, radius: int) -> list[dict]:
    """Read a local OSM extract (.osm.pbf or .osm XML) and return the
    forest/tree elements near (lat, lon), in the loose element schema that
    summarize() consumes ({type, id, tags}).

    Mirrors the Overpass 'around:' semantics as closely as a PoC needs:
    - nodes (natural=tree): included when the node itself is within radius;
    - areas (closed ways and multipolygon relations: natural=wood,
      landuse=forest, natural=tree_row): included when any outer-ring
      vertex is within radius OR the point falls inside the polygon;
    - open ways (natural=tree_row lines): included when any node is
      within radius.

    Requires pyosmium (osmium), imported lazily so the Overpass path
    works without it. Raises SystemExit with a readable message when the
    file cannot be read.
    """
    try:
        import osmium
    except ImportError:
        print("error: --osm-pbf needs the 'osmium' package: pip install osmium",
              file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(path):
        print(f"error: OSM extract not found: {path}", file=sys.stderr)
        sys.exit(1)

    elements: list[dict] = []
    try:
        processor = (osmium.FileProcessor(path)
                     .with_locations()
                     .with_areas()
                     .with_filter(osmium.filter.KeyFilter("natural", "landuse")))
        for obj in processor:
            kind = type(obj).__name__
            tags = dict(obj.tags)
            natural = tags.get("natural")
            landuse = tags.get("landuse")
            is_forest = natural == "wood" or landuse == "forest"

            if kind == "Node":
                if natural == "tree" and obj.location.valid():
                    if distance_m(lat, lon, obj.location.lat, obj.location.lon) <= radius:
                        elements.append({"type": "node", "id": obj.id, "tags": tags})

            elif kind == "Area":
                if not (is_forest or natural == "tree_row"):
                    continue
                rings = _area_outer_rings(obj)
                points = [p for ring in rings for p in ring]
                near = any(distance_m(lat, lon, plat, plon) <= radius
                           for plat, plon in points)
                # 'small circle inside a big forest': no vertex is close,
                # but the point itself lies within the polygon
                inside = (not near and
                          any(point_in_ring(lat, lon, ring) for ring in rings))
                if near or inside:
                    elements.append({"type": "way" if obj.from_way() else "relation",
                                     "id": obj.orig_id(), "tags": tags})

            elif kind == "Way":
                # open tree_row lines; closed ways are handled as Areas,
                # so skip closed ways here to avoid double counting
                node_refs = [n.ref for n in obj.nodes]
                closed = len(node_refs) > 1 and node_refs[0] == node_refs[-1]
                if natural != "tree_row" or closed:
                    continue
                points = _way_node_points(obj)
                if any(distance_m(lat, lon, plat, plon) <= radius for plat, plon in points):
                    elements.append({"type": "way", "id": obj.id, "tags": tags})

            # Relation objects are skipped: forest relations come through
            # as Areas via with_areas().
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"error: could not read OSM extract {path}: {exc}", file=sys.stderr)
        sys.exit(1)

    return elements


# --------------------------------------------------------------------------
# CORINE land cover raster (Phase 3: authoritative forest type, offline)
# --------------------------------------------------------------------------

# CORINE Land Cover (CLC 2018) class codes -> readable names.
CORINE_CLASS_NAMES = {
    111: "Continuous urban fabric", 112: "Discontinuous urban fabric",
    121: "Industrial or commercial units", 122: "Road and rail networks",
    123: "Port areas", 124: "Airports",
    131: "Mineral extraction sites", 132: "Dump sites",
    133: "Construction sites", 141: "Green urban areas",
    142: "Sport and leisure facilities",
    211: "Non-irrigated arable land", 212: "Permanently irrigated land",
    213: "Rice fields", 221: "Vineyards",
    222: "Fruit trees and berry plantations", 223: "Olive groves",
    231: "Pastures", 241: "Annual crops with permanent crops",
    242: "Complex cultivation patterns",
    243: "Agriculture with natural vegetation", 244: "Agro-forestry areas",
    311: "Broad-leaved forest", 312: "Coniferous forest",
    313: "Mixed forest", 321: "Natural grassland",
    322: "Moors and heathland", 323: "Sclerophyllous vegetation",
    324: "Transitional woodland-shrub", 331: "Beaches, dunes, sands",
    332: "Bare rocks", 333: "Sparsely vegetated areas",
    334: "Burnt areas", 335: "Glaciers and perpetual snow",
    411: "Inland marshes", 412: "Peatbogs", 421: "Salt marshes",
    422: "Salines", 423: "Intertidal flats",
    511: "Water courses", 512: "Water bodies", 521: "Coastal lagoons",
    522: "Estuaries", 523: "Sea and ocean",
}

# Classes that describe actual forest / woodland vegetation.
CORINE_FOREST_CODES = frozenset({311, 312, 313, 324})


def corine_class_name(code: int) -> str:
    """Readable name for a CORINE class code."""
    return CORINE_CLASS_NAMES.get(code, f"CORINE class {code}")


def forest_estimate(counts: Counter[int]) -> str | None:
    """Dominant CORINE forest class name from a class -> pixel count map.

    Only genuine forest/woodland codes count (311/312/313/324). Returns
    None when no forest class is present. Pure function.
    """
    forest = {code: n for code, n in counts.items() if code in CORINE_FOREST_CODES}
    if not forest:
        return None
    top_code = max(forest.items(), key=lambda kv: kv[1])[0]
    return corine_class_name(top_code)


def sample_raster(path: str, lat: float, lon: float, radius: int) -> dict:
    """Sample a categorical GeoTIFF (e.g. CORINE land cover) within radius.

    Counts the class codes of all pixel centres whose ground distance to
    (lat, lon) is <= radius. Works with any projected CRS with metre
    units (CORINE is EPSG:3035): pixel centres are reprojected to WGS84
    and measured with the haversine formula, so Web Mercator rasters are
    measured correctly too.

    Returns {total, nodata, counts (Counter), crs, resolution}. Raises
    SystemExit with a readable message on failure. Needs rasterio and
    pyproj (lazy import).
    """
    try:
        import numpy as np
        import rasterio
        from rasterio.windows import Window
        from pyproj import Transformer
    except ImportError as exc:
        print(f"error: --corine needs 'rasterio' and 'pyproj': pip install "
              f"rasterio pyproj ({exc})", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(path):
        print(f"error: CORINE raster not found: {path}", file=sys.stderr)
        sys.exit(1)

    try:
        with rasterio.open(path) as ds:
            if ds.crs is None:
                print(f"error: {path} has no CRS; a georeferenced raster is "
                      f"required", file=sys.stderr)
                sys.exit(1)
            to_crs = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)
            px, py = to_crs.transform(lon, lat)
            row, col = rasterio.transform.rowcol(ds.transform, px, py)
            resx, resy = ds.res
            # pad generously: Web Mercator inflates distances by up to
            # ~2x at northern latitudes, so 2.5x covers it
            pad_c = int(radius / max(abs(resx), 1e-9) * 2.5) + 1
            pad_r = int(radius / max(abs(resy), 1e-9) * 2.5) + 1
            c0 = max(int(col) - pad_c, 0)
            r0 = max(int(row) - pad_r, 0)
            w = min(2 * pad_c + 1, ds.width - c0)
            h = min(2 * pad_r + 1, ds.height - r0)
            result = {"total": 0, "nodata": 0, "counts": Counter(),
                      "crs": str(ds.crs), "resolution": (abs(resx), abs(resy))}
            if w <= 0 or h <= 0:
                # point outside the raster coverage
                return result
            window = Window(c0, r0, w, h)
            data = ds.read(1, window=window)
            wtr = ds.window_transform(window)
            rows_i, cols_i = np.indices(data.shape)
            xs, ys = rasterio.transform.xy(wtr, rows_i, cols_i)
            to_wgs = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True)
            lons, lats = to_wgs.transform(xs, ys)
            lons = np.asarray(lons)
            lats = np.asarray(lats)
            # vectorised haversine
            p1 = math.radians(lat)
            p2 = np.radians(lats)
            a = (np.sin((p2 - p1) / 2) ** 2
                 + math.cos(p1) * np.cos(p2) * np.sin(np.radians(lons - lon) / 2) ** 2)
            dist = 2 * 6_371_000.0 * np.arcsin(np.sqrt(a))
            within = dist <= radius
            nodata = ds.nodata
            counts = result["counts"]
            nodata_count = 0
            for value, is_within in zip(data.flat, within.flat):
                if not is_within:
                    continue
                if nodata is not None and value == nodata:
                    nodata_count += 1
                else:
                    counts[int(value)] += 1
                result["total"] += 1
            result["nodata"] = nodata_count
            return result
    except (rasterio.errors.RasterioIOError, RuntimeError, ValueError) as exc:
        print(f"error: could not read raster {path}: {exc}", file=sys.stderr)
        sys.exit(1)


# --------------------------------------------------------------------------
# GBIF species observations (second, independent data source)
# --------------------------------------------------------------------------

def build_gbif_geometry(lat: float, lon: float, radius_m: int,
                        vertices: int = 24) -> str:
    """Return a WKT polygon approximating a circle of ``radius_m`` metres.

    GBIF's occurrence search accepts a ``geometry`` WKT filter but has no
    circle primitive, so we emit a regular 24-vertex polygon. Every vertex
    sits at distance ~radius from the centre (great-circle approximated
    with flat local scaling), matching the Overpass `around:` semantics
    closely enough for a PoC.
    """
    rlat = radius_m / 111_320.0
    rlon = radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 1e-9))
    points = [
        f"{lon + rlon * math.cos(a):.6f} {lat + rlat * math.sin(a):.6f}"
        for a in (2 * math.pi * k / vertices for k in range(vertices))
    ]
    # A valid WKT polygon repeats the first point at the end.
    return "POLYGON((" + ",".join(points + [points[0]]) + "))"


def gbif_facets_to_counts(body: dict) -> Counter[int]:
    """Extract the speciesKey facet counts from a GBIF search response.

    Returns a Counter mapping taxon key -> occurrence count, or an empty
    Counter when the response carries no facets. Pure function.
    """
    counts: Counter = Counter()
    facets = body.get("facets") or []
    for facet in facets:
        # GBIF calls the field 'SPECIES_KEY' in live responses; normalise
        # by dropping underscores so both spellings match.
        field = (facet.get("field") or "").upper().replace("_", "")
        if field != "SPECIESKEY":
            continue
        for entry in facet.get("counts") or []:
            try:
                counts[int(entry["name"])] = int(entry["count"])
            except (KeyError, TypeError, ValueError):
                continue
    return counts


def gbif_name_for_key(key: int, session: requests.Session,
                      cache: dict) -> str | None:
    """Resolve a GBIF taxon key to a display name (best-effort, cached).

    Prefers the binomial ('Quercus robur') over the full scientific name
    with author ('Quercus robur L.'), and appends the English vernacular
    name when GBIF knows one: 'Quercus robur (Pedunculate Oak)'.
    Returns None on any failure or if the key resolves above species rank.
    """
    if key in cache:
        return cache[key]

    name = None
    try:
        resp = session.get(GBIF_SPECIES_URL.format(key=key), timeout=10)
        if resp.status_code == 200:
            body = resp.json()
            if isinstance(body, dict):
                binomial = body.get("species") or body.get("scientificName")
                if body.get("rank") in ("SPECIES", "SUBSPECIES", "VARIETY") and binomial:
                    vernacular = body.get("vernacularName")
                    name = f"{binomial} ({vernacular})" if vernacular else binomial
    except (requests.RequestException, ValueError):
        name = None

    cache[key] = name
    return name


def resolve_gbif_names(counts: Counter[int], lookup, top: int) -> dict[str, int]:
    """Turn taxon-key counts into a name -> count dict via ``lookup``.

    Walks the counts in descending order and keeps the first ``top`` keys
    that resolve to a name. Unresolvable keys (None) are skipped. Pure
    function; ``lookup`` is injected so tests can run without network.
    """
    names: dict[str, int] = {}
    for key, count in counts.most_common():
        if len(names) >= top:
            break
        name = lookup(key)
        if name and name not in names:
            names[name] = count
    return names


def fetch_gbif_species(lat: float, lon: float, radius: int, top: int = 10,
                       session: requests.Session | None = None,
                       ) -> tuple[int, dict[str, int]] | None:
    """Query GBIF for observed plant species within the radius.

    Returns (total occurrences in radius, {display name: count}) for the
    top observed species, or None on any failure. Best-effort: callers
    treat None as 'GBIF unavailable' and never block the report on it.
    """
    session = session or requests.Session()
    try:
        resp = session.get(
            GBIF_API,
            params={
                "geometry": build_gbif_geometry(lat, lon, radius),
                "kingdom_key": PLANTAE_KINGDOM_KEY,
                "facet": "speciesKey",
                "facetLimit": top * 3,  # headroom for unresolvable keys
                "limit": 0,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"warning: GBIF returned HTTP {resp.status_code} - "
                  f"skipping observed-species section", file=sys.stderr)
            return None
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"warning: GBIF request failed: {exc} - skipping "
              f"observed-species section", file=sys.stderr)
        return None

    if not isinstance(body, dict):
        print("warning: GBIF returned unexpected JSON - skipping "
              "observed-species section", file=sys.stderr)
        return None

    counts = gbif_facets_to_counts(body)
    cache: dict = {}
    species = resolve_gbif_names(
        counts,
        lambda key: gbif_name_for_key(key, session, cache),
        top,
    )
    return body.get("count", 0), species


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def print_report(lat: float, lon: float, radius: int, summary: dict,
                 with_wiki: bool = True, with_gbif: bool = True,
                 source: str | None = None, corine: dict | None = None) -> None:
    """Print a plain-text report of the summarised data."""
    print(f"Location: {lat}, {lon}  (radius {radius} m)")
    if source:
        print(f"Data source: {source}")
    print()

    print("Counts")
    print(f"  Forest areas : {summary['forest_area_count']}")
    print(f"  Mapped trees : {summary['tree_count']}")
    print(f"  Tree rows    : {summary['tree_row_count']}")
    print()

    print("Forest type")
    print(f"  Estimated type: {summary['forest_type']}")
    if summary["leaf_type"]:
        for key, count in summary["leaf_type"].most_common():
            print(f"  leaf_type={key}: {count}")
    else:
        print("  (no leaf_type tags on nearby forest areas)")
    if summary["leaf_cycle"]:
        for key, count in summary["leaf_cycle"].most_common():
            print(f"  leaf_cycle={key}: {count}")
    else:
        print("  (no leaf_cycle tags on nearby forest areas)")
    print()

    if summary["names"]:
        print("Named forests")
        for name in sorted(set(summary["names"])):
            print(f"  {name}")
        print()

    if summary["wood"]:
        print("Wood tags (wood=*)")
        for key, count in summary["wood"].most_common():
            print(f"  {key}: {count}")
        print()

    if summary["protect_class"]:
        print("Protection (protect_class=*)")
        for key, count in summary["protect_class"].most_common():
            print(f"  {key}: {count}")
        print()

    top_species = summary["species"].most_common(10)
    top_genera = summary["genera"].most_common(10)
    if top_species or top_genera:
        print("Top species / taxa")
        for name, count in top_species:
            print(f"  {name}: {count}")
        print()
        if top_genera:
            print("Top genera")
            for name, count in top_genera:
                print(f"  {name}: {count}")
            print()

        if with_wiki:
            top_for_wiki = [n for n, _ in top_species][:5]
            if top_genera and len(top_for_wiki) < 5:
                top_for_wiki += [n for n, _ in top_genera][: 5 - len(top_for_wiki)]

            session = requests.Session()
            session.headers.update(HEADERS)
            cache: dict = {}
            print("Wikipedia descriptions")
            found_any = False
            for name in top_for_wiki:
                text = describe_species(name, session, cache)
                if text:
                    found_any = True
                    print(f"  {name}:")
                    print(f"    {text}")
            if not found_any:
                print("  (no Wikipedia matches found)")
            print()
    else:
        print("Species")
        print("  No species/genus/taxon tags found nearby.")
        print("  Note: OSM species tagging is sparse - a mapped forest area")
        print("  often only carries leaf_type/leaf_cycle, if anything.")
        print()

    if with_gbif:
        gbif = fetch_gbif_species(lat, lon, radius)
        if gbif is not None:
            total, species_counts = gbif
            print("Species observed nearby (GBIF)")
            print(f"  ({total} plant occurrences recorded within the radius)")
            print("  (all plants - herbs, mosses etc. included; GBIF has no "
                  "tree-only filter)")
            if species_counts:
                for name, count in species_counts.items():
                    print(f"  {name}: {count}")
            else:
                print("  (no species observations recorded in GBIF for "
                      "this area)")
            print()

    if corine is not None:
        land = corine["total"] - corine["nodata"]
        resx, resy = corine["resolution"]
        cell = f"~{round((resx + resy) / 2)} m cells"
        print("CORINE land cover (raster)")
        print(f"  {land} land pixels sampled within the radius "
              f"({corine['nodata']} nodata)  [{corine['crs']}, {cell}]")
        if land > 0:
            forest_items = sorted(
                ((code, n) for code, n in corine["counts"].items()
                 if code in CORINE_FOREST_CODES),
                key=lambda kv: -kv[1])
            if forest_items:
                print("  Forest classes:")
                for code, n in forest_items:
                    print(f"    {corine_class_name(code)} ({code}): "
                          f"{n} px ({100 * n / land:.0f}%)")
                print(f"  Dominant forest class: {forest_estimate(corine['counts'])}")
            else:
                print("  (no CORINE forest classes within the radius)")
            other = sorted((kv for kv in corine["counts"].items()
                            if kv[0] not in CORINE_FOREST_CODES),
                           key=lambda kv: -kv[1])[:5]
            if other:
                print("  Other classes (top 5):")
                for code, n in other:
                    print(f"    {corine_class_name(code)} ({code}): "
                          f"{n} px ({100 * n / land:.0f}%)")
        else:
            print("  (the point lies outside the raster coverage)")
        print()

    if (summary["forest_area_count"] == 0 and summary["tree_count"] == 0
            and summary["tree_row_count"] == 0):
        print("Nothing found at this location.")
        print("Hints: increase --radius (max 5000 m), pick a point closer to")
        print("woodland, or run with --raw to inspect what Overpass returns.")
        print("Note: OSM coverage for forests and especially species is sparse")
        print("in many parts of the world.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="forrest_type.py",
        description="Describe forest types and tree species around a lat/lon "
        "using OpenStreetMap (Overpass) plus Wikipedia summaries and GBIF "
        "species observations.",
    )
    parser.add_argument("lat", type=float, help="latitude, -90..90")
    parser.add_argument("lon", type=float, help="longitude, -180..180")
    parser.add_argument("--radius", type=int, default=DEFAULT_RADIUS,
                        help=f"search radius in metres (default {DEFAULT_RADIUS}, max {MAX_RADIUS})")
    parser.add_argument("--raw", action="store_true",
                        help="dump the raw Overpass JSON (for studying the data)")
    parser.add_argument("--no-wiki", action="store_true",
                        help="skip the Wikipedia lookups")
    parser.add_argument("--no-gbif", action="store_true",
                        help="skip the GBIF observed-species lookup")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                        help="Overpass API endpoint URL")
    parser.add_argument("--osm-pbf", metavar="FILE", default=None,
                        help="read a local OSM extract (.osm.pbf or .osm XML) "
                        "instead of querying the Overpass API")
    parser.add_argument("--corine", metavar="FILE", default=None,
                        help="sample a CORINE land cover GeoTIFF for an "
                        "authoritative forest-type estimate")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if not -90 <= args.lat <= 90:
        print(f"error: latitude {args.lat} out of range [-90, 90]", file=sys.stderr)
        sys.exit(1)
    if not -180 <= args.lon <= 180:
        print(f"error: longitude {args.lon} out of range [-180, 180]", file=sys.stderr)
        sys.exit(1)

    radius = min(max(args.radius, 0), MAX_RADIUS)
    if args.radius > MAX_RADIUS:
        print(f"warning: radius capped at {MAX_RADIUS} m", file=sys.stderr)

    if args.osm_pbf:
        elements = read_osm_extract(args.osm_pbf, args.lat, args.lon, radius)
        if args.raw:
            print(json.dumps({"elements": elements}, indent=2, ensure_ascii=False))
        summary = summarize(elements)
        source = f"local OSM extract ({args.osm_pbf})"
    else:
        query = build_query(args.lat, args.lon, radius)
        data = fetch(query, args.endpoint)
        if args.raw:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        elements = data.get("elements", [])
        summary = summarize(elements)
        source = f"Overpass API ({args.endpoint})"

    if args.raw:
        # Keep stdout a pure JSON dump; the readable report goes to stderr.
        report_out = sys.stderr
    else:
        report_out = sys.stdout

    old_stdout = sys.stdout
    sys.stdout = report_out
    try:
        print_report(args.lat, args.lon, radius, summary,
                     with_wiki=not args.no_wiki,
                     with_gbif=not args.no_gbif,
                     source=source,
                     corine=sample_raster(args.corine, args.lat, args.lon, radius)
                     if args.corine else None)
    finally:
        sys.stdout = old_stdout


if __name__ == "__main__":
    main()
