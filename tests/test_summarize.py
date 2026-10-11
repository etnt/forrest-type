"""Offline unit tests for forrest_type.build_query and forrest_type.summarize.

These make no network calls.
"""

import pytest

from forrest_type import build_query, forest_label, summarize


def make_node(el_id, tags, lat=0.0, lon=0.0):
    return {"type": "node", "id": el_id, "lat": lat, "lon": lon, "tags": tags}


def make_area(el_id, tags):
    return {"type": "way", "id": el_id, "tags": tags}


# --------------------------------------------------------------------------
# build_query
# --------------------------------------------------------------------------

def test_build_query_contains_selectors():
    q = build_query(59.3293, 18.0686, 2000)
    assert 'nwr["natural"="wood"](around:2000,59.3293,18.0686);' in q
    assert 'nwr["landuse"="forest"](around:2000,59.3293,18.0686);' in q
    assert 'node["natural"="tree"](around:2000,59.3293,18.0686);' in q
    assert 'nwr["natural"="tree_row"](around:2000,59.3293,18.0686);' in q


def test_build_query_header_and_output():
    q = build_query(0, 0, 1000)
    assert q.startswith("[out:json][timeout:25];")
    assert q.rstrip().endswith("out tags center;")


# --------------------------------------------------------------------------
# summarize
# --------------------------------------------------------------------------

@pytest.fixture
def small_elements():
    return [
        make_area(1, {
            "name": "Djurgården",
            "natural": "wood",
            "leaf_type": "broadleaved",
            "leaf_cycle": "deciduous",
            "species": "Quercus robur;Betula pendula",
        }),
        make_area(2, {
            "landuse": "forest",
            "leaf_type": "needleleaved",
            "leaf_cycle": "evergreen",
            "genus": "Pinus",
        }),
        make_node(3, {"natural": "tree", "species": "Quercus robur"}),
        make_node(4, {"natural": "tree", "genus": "Betula;Fagus"}),
        make_node(5, {"natural": "tree", "species": "Quercus robur",
                      "wikipedia": "en:Quercus robur"}),
    ]


def test_summarize_counts_areas_and_trees(small_elements):
    s = summarize(small_elements)
    assert s["forest_area_count"] == 2
    assert s["tree_count"] == 3


def test_summarize_leaf_counters(small_elements):
    s = summarize(small_elements)
    assert s["leaf_type"] == {"broadleaved": 1, "needleleaved": 1}
    assert s["leaf_cycle"] == {"deciduous": 1, "evergreen": 1}


def test_summarize_species_counter(small_elements):
    s = summarize(small_elements)
    assert s["species"]["Quercus robur"] == 3  # area + tree + wikipedia node
    assert "Betula pendula" in s["species"]
    assert s["genera"]["Pinus"] == 1
    assert s["genera"]["Betula"] == 1  # split on ';'
    assert s["genera"]["Fagus"] == 1


def test_summarize_names_and_wikipedia(small_elements):
    s = summarize(small_elements)
    assert s["names"] == ["Djurgården"]
    assert s["wikipedia"]["en:Quercus robur"] == 1


def test_summarize_forest_type_label(small_elements):
    # 2 areas with different types -> dominant leaf_type is a tie; Counter
    # picks the first-inserted, so just assert it is not 'unspecified'.
    s = summarize(small_elements)
    assert s["forest_type"] != "unspecified"


def test_summarize_counts_tree_rows():
    # tree_row elements are fetched by build_query and must be counted,
    # not silently dropped.
    s = summarize([make_node(7, {"natural": "tree_row"})])
    assert s["tree_row_count"] == 1


def test_summarize_normalizes_species_names():
    # The same species written with straight vs typographic apostrophes,
    # and with 'x' vs the multiplication sign for hybrids, must merge
    # into one count (seen live: Prunus 'Accolade' split 28/26).
    s = summarize([
        make_node(1, {"natural": "tree", "species": "Prunus ‘Accolade’"}),
        make_node(2, {"natural": "tree", "species": "Prunus ‘Accolade’"}),
        make_node(3, {"natural": "tree", "species": "Aesculus × carnea"}),
        make_node(4, {"natural": "tree", "species": "Aesculus x carnea"}),
    ])
    assert s["species"]["Prunus 'Accolade'"] == 2
    assert s["species"]["Aesculus x carnea"] == 2


def test_summarize_normalizes_genus_names():
    s = summarize([
        make_node(1, {"natural": "tree", "genus": "Tí­lia"}),
        make_node(2, {"natural": "tree", "genus": "Tilia"}),
    ])
    # accented characters are NOT folded - only quotes and ×/x - so these
    # stay separate (documented behaviour)
    assert s["genera"]["Tí­lia"] == 1
    assert s["genera"]["Tilia"] == 1


def test_summarize_normalization_does_not_touch_wikidata():
    s = summarize([
        make_node(1, {"natural": "tree", "species:wikidata": "Q1234"}),
    ])
    assert s["species_wikidata"]["Q1234"] == 1


def test_summarize_empty():
    s = summarize([])
    assert s["forest_area_count"] == 0
    assert s["tree_count"] == 0
    assert s["forest_type"] == "unspecified"
    assert not s["species"] and not s["genera"]


# --------------------------------------------------------------------------
# forest_label
# --------------------------------------------------------------------------

def test_forest_label_combinations():
    assert forest_label("broadleaved", "deciduous") == "Deciduous broadleaf forest"
    assert forest_label("needleleaved", "evergreen") == "Evergreen conifer forest"
    assert forest_label(None, "evergreen") == "Evergreen forest (leaf type unspecified)"
    assert forest_label("mixed", None) == "Mixed forest (leaf cycle unspecified)"
    assert forest_label(None, None) == "unspecified"
