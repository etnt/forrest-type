"""Offline tests for the CORINE raster sampler (--corine, Phase 3).

Builds a small synthetic class-coded GeoTIFF in EPSG:3035 (the real
CORINE projection) with rasterio, then verifies the sampling pipeline
(CRS transform, windowing, nodata handling) against counts recomputed
independently from the fixture grid. No network, no downloads.
"""

import math
import os
from collections import Counter

import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine
from pyproj import Transformer

from forrest_type import (
    CORINE_FOREST_CODES,
    corine_class_name,
    distance_m,
    forest_estimate,
    sample_raster,
)

POINT = (59.3293, 18.0686)  # Stockholm test point
CELL = 100.0  # real CORINE resolution
RADIUS = 300

# fixture grid layout (60x60 cells, 100 m each, centred on the point):
#   rows  0..44 : 311 left of centre column, 312 right of it
#   rows 45..49 : 313
#   rows 50..59 : nodata (0)
GRID_ROWS = GRID_COLS = 60
BOUNDARY_COL = 30
ROW_313_START, ROW_313_END = 45, 50
ROW_NODATA_START = 50
NODATA = 0


def make_grid() -> np.ndarray:
    grid = np.zeros((GRID_ROWS, GRID_COLS), dtype=np.int32)
    grid[:ROW_313_START, :BOUNDARY_COL] = 311
    grid[:ROW_313_START, BOUNDARY_COL:] = 312
    grid[ROW_313_START:ROW_313_END, :] = 313
    # rows >= ROW_NODATA_START stay 0 (nodata)
    return grid


@pytest.fixture(scope="module")
def corine_tif(tmp_path_factory):
    """Write the synthetic CORINE-style GeoTIFF centred on the point."""
    to_3035 = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)
    px, py = to_3035.transform(POINT[1], POINT[0])
    # point must be the exact centre of pixel (30, 30)
    x0 = px - (BOUNDARY_COL + 0.5) * CELL
    y0 = py + (BOUNDARY_COL + 0.5) * CELL
    transform = Affine(CELL, 0, x0, 0, -CELL, y0)
    path = tmp_path_factory.mktemp("raster") / "corine_probe.tif"
    with rasterio.open(
        path, "w", driver="GTiff", width=GRID_COLS, height=GRID_ROWS,
        count=1, dtype="int32", crs="EPSG:3035", transform=transform,
        nodata=NODATA,
    ) as ds:
        ds.write(make_grid(), 1)
    return str(path), x0, y0


def expected_counts(x0, y0, radius):
    """Recompute the sample independently from the fixture grid."""
    to_wgs = Transformer.from_crs("EPSG:3035", "EPSG:4326", always_xy=True)
    counts = Counter()
    nodata = 0
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            x = x0 + (c + 0.5) * CELL
            y = y0 - (r + 0.5) * CELL
            # always_xy=True -> (lon, lat)
            plon, plat = to_wgs.transform(x, y)
            if distance_m(POINT[0], POINT[1], plat, plon) <= radius:
                v = int(make_grid()[r, c])
                if v == NODATA:
                    nodata += 1
                else:
                    counts[v] += 1
    return counts, nodata


def test_sample_counts_match_independent_recomputation(corine_tif):
    path, x0, y0 = corine_tif
    res = sample_raster(path, *POINT, RADIUS)
    exp_counts, exp_nodata = expected_counts(x0, y0, RADIUS)
    assert res["counts"] == exp_counts
    assert res["nodata"] == exp_nodata
    assert res["total"] == sum(exp_counts.values()) + exp_nodata


def test_sample_returns_both_sides_of_class_boundary(corine_tif):
    path, _, _ = corine_tif
    res = sample_raster(path, *POINT, RADIUS)
    # the class boundary (col 30) runs through the circle, so both
    # broadleaved (311) and coniferous (312) must appear
    assert res["counts"][311] > 0
    assert res["counts"][312] > 0
    assert res["counts"][313] == 0  # 313 rows are outside the radius


def test_sample_crs_and_resolution_reported(corine_tif):
    path, _, _ = corine_tif
    res = sample_raster(path, *POINT, RADIUS)
    assert "3035" in res["crs"]
    assert res["resolution"] == (CELL, CELL)


def test_sample_large_radius_pulls_in_313_and_nodata(corine_tif):
    path, _, _ = corine_tif
    res = sample_raster(path, *POINT, 25_000)
    assert res["counts"][313] > 0
    assert res["nodata"] > 0


def test_sample_point_outside_raster_returns_empty(tmp_path_factory):
    # write a tiny raster in 3035 far from (0, 0) ocean point
    to_3035 = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)
    px, py = to_3035.transform(POINT[1], POINT[0])
    path = tmp_path_factory.mktemp("raster2") / "far.tif"
    with rasterio.open(
        path, "w", driver="GTiff", width=10, height=10, count=1,
        dtype="int32", crs="EPSG:3035",
        transform=Affine(CELL, 0, px - 500, 0, -CELL, py + 500),
    ) as ds:
        ds.write(np.full((10, 10), 311, dtype=np.int32), 1)
    res = sample_raster(str(path), 0.0, 0.0, 1000)
    assert res["total"] == 0
    assert res["counts"] == Counter()


def test_sample_missing_file_exits(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        sample_raster(str(tmp_path / "nope.tif"), *POINT, 500)
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().err


# --------------------------------------------------------------------------
# forest_estimate / names
# --------------------------------------------------------------------------

def test_forest_estimate_dominant_forest_class():
    assert forest_estimate(Counter({311: 5, 313: 7, 112: 100})) == "Mixed forest"
    assert forest_estimate(Counter({312: 9, 311: 2})) == "Coniferous forest"
    assert forest_estimate(Counter({324: 3})) == "Transitional woodland-shrub"


def test_forest_estimate_ignores_non_forest_classes():
    assert forest_estimate(Counter({112: 10, 231: 5})) is None
    assert forest_estimate(Counter()) is None


def test_forest_codes_are_named():
    for code in CORINE_FOREST_CODES:
        assert corine_class_name(code) != f"CORINE class {code}"


def test_corine_class_name_fallback():
    assert corine_class_name(999) == "CORINE class 999"
    assert corine_class_name(311) == "Broad-leaved forest"