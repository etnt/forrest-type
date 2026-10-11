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

Examples:
    python forrest_type.py 59.3293 18.0686 --radius 2000   # Stockholm
    python forrest_type.py 57.0 14.5                       # rural Sweden
    python forrest_type.py 0 0 --raw                       # ocean, raw dump

Requires: requests (see requirements.txt). Pure functions (build_query,
summarize, build_gbif_geometry, gbif_facets_to_counts, resolve_gbif_names)
make no network calls and are unit tested in tests/.
"""

from __future__ import annotations

import argparse
import json
import math
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
                 with_wiki: bool = True, with_gbif: bool = True) -> None:
    """Print a plain-text report of the summarised data."""
    print(f"Location: {lat}, {lon}  (radius {radius} m)")
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

    query = build_query(args.lat, args.lon, radius)
    data = fetch(query, args.endpoint)

    if args.raw:
        print(json.dumps(data, indent=2, ensure_ascii=False))

    elements = data.get("elements", [])
    summary = summarize(elements)
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
                     with_gbif=not args.no_gbif)
    finally:
        sys.stdout = old_stdout


if __name__ == "__main__":
    main()
