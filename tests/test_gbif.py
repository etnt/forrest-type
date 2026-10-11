"""Offline unit tests for the GBIF source in forrest_type.

These make no network calls: the geometry builder and facet parsing are
pure, and name resolution is tested with an injected lookup function.
"""

import math
from collections import Counter

from forrest_type import (
    build_gbif_geometry,
    gbif_facets_to_counts,
    resolve_gbif_names,
)


# --------------------------------------------------------------------------
# build_gbif_geometry
# --------------------------------------------------------------------------

def test_geometry_is_valid_wkt_polygon():
    g = build_gbif_geometry(59.3293, 18.0686, 2000)
    assert g.startswith("POLYGON((")
    assert g.endswith("))")
    points = g[len("POLYGON(("):-2].split(",")
    # 24 vertices + closing point
    assert len(points) == 25
    # a valid polygon repeats the first point at the end
    assert points[0] == points[-1]


def test_geometry_points_sit_at_radius():
    lat, lon, r = 59.3293, 18.0686, 2000
    points = build_gbif_geometry(lat, lon, r)[len("POLYGON(("):-2].split(",")
    coords = [tuple(map(float, p.split())) for p in points[:24]]
    for plon, plat in coords:
        # flat local scaling: metres per degree used by the builder
        dlat = (plat - lat) * 111_320.0
        dlon = (plon - lon) * 111_320.0 * math.cos(math.radians(lat))
        # vertices land exactly on the circle under the builder's flat model
        # (allow ~0.5 m for the 6-decimal coordinate rounding)
        assert abs(math.hypot(dlon, dlat) - r) < 0.5


def test_geometry_center_is_inside_polygon():
    # the centre of the circle must fall inside the polygon ring;
    # a simple ray-casting check in (lon, lat) space
    lat, lon, r = 59.0, 15.0, 1000
    ring = build_gbif_geometry(lat, lon, r)[len("POLYGON(("):-2].split(",")
    poly = [tuple(map(float, p.split())) for p in ring]
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:]):
        if (y1 > lat) != (y2 > lat):
            x_cross = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if x_cross > lon:
                inside = not inside
    assert inside


def test_geometry_poles_clamped():
    # near-pole latitudes must not blow up (cos -> 0 division guard)
    g = build_gbif_geometry(89.9, 0.0, 5000)
    assert g.startswith("POLYGON((")
    g = build_gbif_geometry(90.0, 0.0, 5000)
    assert "nan" not in g.lower() and "inf" not in g.lower()


# --------------------------------------------------------------------------
# gbif_facets_to_counts
# --------------------------------------------------------------------------

def make_facets_body(counts, field="SPECIESKEY"):
    return {"count": 42, "results": [],
            "facets": [{"field": field,
                        "counts": [{"name": k, "count": v}
                                   for k, v in counts]}]}


def test_facets_to_counts_parses_specieskey_facet():
    body = make_facets_body([(2878688, 1244), (5376075, 376)])
    assert gbif_facets_to_counts(body) == Counter({2878688: 1244, 5376075: 376})


def test_facets_to_counts_ignores_other_facet_fields():
    body = make_facets_body([(6, 40800876)], field="TAXONKEY")
    assert gbif_facets_to_counts(body) == Counter()


def test_facets_to_counts_handles_missing_and_malformed():
    assert gbif_facets_to_counts({}) == Counter()
    assert gbif_facets_to_counts({"facets": []}) == Counter()
    assert gbif_facets_to_counts({"facets": None}) == Counter()
    body = {"facets": [{"field": "SPECIESKEY",
                        "counts": [{"name": "not-a-number", "count": 5},
                                   {"count": 3},
                                   {"name": 7, "count": "bad"}]}]}
    assert gbif_facets_to_counts(body) == Counter()


def test_facets_to_counts_accepts_lowercase_and_underscored_field():
    # live GBIF responses use the field name 'SPECIES_KEY'; older docs
    # used 'speciesKey' - the parser normalises both spellings
    assert gbif_facets_to_counts(make_facets_body([(1, 2)], field="speciesKey")) == Counter({1: 2})
    assert gbif_facets_to_counts(make_facets_body([(1, 2)], field="SPECIES_KEY")) == Counter({1: 2})


# --------------------------------------------------------------------------
# resolve_gbif_names
# --------------------------------------------------------------------------

def test_resolve_gbif_names_maps_and_limits():
    counts = Counter({1: 50, 2: 40, 3: 30, 4: 20})
    names = resolve_gbif_names(counts, {1: "Quercus robur",
                                        2: "Pinus sylvestris",
                                        3: "Picea abies"}.get, top=3)
    assert names == {"Quercus robur": 50,
                     "Pinus sylvestris": 40,
                     "Picea abies": 30}


def test_resolve_gbif_names_skips_unresolvable_keys():
    counts = Counter({1: 50, 2: 40, 3: 30})
    lookup = {1: "Quercus robur", 2: None, 3: "Picea abies"}.get
    # top=3: key 2 resolves to None, so key 3 fills the slot
    assert resolve_gbif_names(counts, lookup, top=3) == {
        "Quercus robur": 50, "Picea abies": 30,
    }


def test_resolve_gbif_names_deduplicates_names():
    # two keys resolving to the same display name keep the higher count
    counts = Counter({1: 50, 2: 40})
    lookup = {1: "Quercus robur", 2: "Quercus robur"}.get
    assert resolve_gbif_names(counts, lookup, top=5) == {"Quercus robur": 50}


def test_resolve_gbif_names_empty_counts():
    assert resolve_gbif_names(Counter(), {1: "x"}.get, top=5) == {}