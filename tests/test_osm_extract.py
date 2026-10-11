"""Offline tests for the local OSM extract reader (--osm-pbf, Phase 2).

pyosmium reads .osm XML as well as .osm.pbf, so the fixture
tests/data/probe.osm lets us exercise the full read path with no
network and no large downloads.
"""

import math
import os

import pytest

from forrest_type import distance_m, point_in_ring, read_osm_extract

FIXTURE = os.path.join(os.path.dirname(__file__), "data", "probe.osm")
POINT = (59.3293, 18.0686)  # inside the fixture's big forest square


# --------------------------------------------------------------------------
# distance_m
# --------------------------------------------------------------------------

def test_distance_m_one_degree_on_equator():
    # 1 degree of longitude on the equator on a R=6371 km sphere is
    # R * pi / 180 (~111.19 km; the WGS84 geodesic is ~111.32 km, but
    # distance_m is a haversine on a sphere)
    d = distance_m(0.0, 0.0, 0.0, 1.0)
    assert abs(d - math.pi * 6_371_000.0 / 180.0) < 1.0


def test_distance_m_zero_and_symmetry():
    a = (59.3293, 18.0686)
    b = (59.3295, 18.0690)
    assert distance_m(*a, *a) == 0.0
    assert abs(distance_m(*a, *b) - distance_m(*b, *a)) < 1e-6


def test_distance_m_short_range_plausible():
    # node 1 of the fixture: ~22 m north, ~23 m east of the test point
    d = distance_m(POINT[0], POINT[1], 59.3295, 18.0690)
    assert 25 < d < 45


# --------------------------------------------------------------------------
# point_in_ring
# --------------------------------------------------------------------------

def test_point_in_ring_square():
    square = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)]
    assert point_in_ring(0.5, 0.5, square)
    assert not point_in_ring(1.5, 0.5, square)
    assert not point_in_ring(-0.1, 0.5, square)


def test_point_in_ring_concave():
    # U shape (lat, lon); the notch is at lat 0..0.5, lon 0.4..0.6
    u = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0),
         (0.0, 0.6), (0.5, 0.6), (0.5, 0.4), (0.0, 0.4)]
    assert point_in_ring(0.8, 0.5, u)     # solid part of the U
    assert not point_in_ring(0.25, 0.5, u)  # inside the notch


def test_point_in_ring_on_vertex_is_tolerated():
    square = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)]
    # on the boundary the result is implementation-defined, but must not
    # raise and must be consistent for repeated calls
    a = point_in_ring(0.0, 0.0, square)
    assert a == point_in_ring(0.0, 0.0, square)


# --------------------------------------------------------------------------
# read_osm_extract
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def elements_near():
    return read_osm_extract(FIXTURE, *POINT, 500)


def test_read_includes_nearby_tree_only(elements_near):
    trees = [e for e in elements_near if e["tags"].get("natural") == "tree"]
    assert len(trees) == 1
    assert trees[0]["id"] == 1
    assert trees[0]["type"] == "node"


def test_read_counts_far_tree_out(elements_near):
    # node 2 sits ~34 km away and must not leak in at radius 500
    assert all(e["id"] != 2 for e in elements_near if e["type"] == "node")


def test_read_counts_tree_rows(elements_near):
    rows = [e for e in elements_near if e["tags"].get("natural") == "tree_row"]
    # way 11 is nearby, way 12 is far outside the radius
    assert [e["id"] for e in rows] == [11]


def test_read_includes_forest_relation_via_area(elements_near):
    forests = [e for e in elements_near
               if e["tags"].get("natural") == "wood"
               or e["tags"].get("landuse") == "forest"]
    # relation 20 (nearby multipolygon) + way 30 (big square containing
    # the point). Relation 21 is far outside and must be excluded.
    ids = sorted(e["id"] for e in forests)
    assert ids == [20, 30]
    by_id = {e["id"]: e for e in forests}
    assert by_id[20]["type"] == "relation"
    assert by_id[20]["tags"]["name"] == "Probe Relation Wood"
    assert by_id[30]["type"] == "way"
    assert by_id[30]["tags"]["name"] == "Big Probe Wood"


def test_read_elements_feed_summarize(elements_near):
    from forrest_type import summarize
    s = summarize(elements_near)
    assert s["forest_area_count"] == 2
    assert s["tree_count"] == 1
    assert s["tree_row_count"] == 1
    assert s["forest_type"] != "unspecified"
    assert "Big Probe Wood" in s["names"]


def test_read_larger_radius_pulls_in_far_elements():
    # 50 km takes in the far tree (node 2), the far forest square (way 31)
    # and its multipolygon relation (21). The fixture deliberately tags
    # both 21 and its member way 31, so both come through - real OSM data
    # usually tags only one of the two.
    els = read_osm_extract(FIXTURE, *POINT, 50_000)
    assert {e["id"] for e in els if e["tags"].get("natural") == "tree"} == {1, 2}
    forests = [e for e in els
               if e["tags"].get("natural") == "wood"
               or e["tags"].get("landuse") == "forest"]
    assert {e["id"] for e in forests} == {20, 30, 21, 31}


def test_read_missing_file_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        read_osm_extract("/nonexistent/file.osm.pbf", *POINT, 500)
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().err


def test_read_garbage_file_exits(tmp_path, capsys):
    bad = tmp_path / "bad.osm"
    bad.write_text("this is not an OSM file")
    with pytest.raises(SystemExit) as exc:
        read_osm_extract(str(bad), *POINT, 500)
    assert exc.value.code == 1
    assert "could not read" in capsys.readouterr().err