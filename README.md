# Forrest Type Classification

You give the program a location: a latitude and a longitude. The program
describes the forest around that location. It lists the tree species that
grow there. It also describes what the trees look like and what properties
they have.

The program reads data from these sources:

- OpenStreetMap (a free world map that anyone can edit), through the
  Overpass API (a free search service for OpenStreetMap data):
  https://wiki.openstreetmap.org/wiki/Overpass_API#Quick_Start_(60_seconds):_for_Developers/Programmers
- Wikipedia, for short descriptions of each species
- GBIF, a free database of species observations
- CORINE Land Cover, a satellite-based land-cover map of Europe

`forrest_type.py` is a proof of concept (a small program that shows what
the data can give). See the Usage section below.

A possible future extension is a Flutter app for Android. This document
does not cover that app.

## Usage

### Setup

1. Create a Python environment (an isolated copy of Python for one
   project):
   ```sh
   python -m venv .venv
   ```
2. Start using that environment:
   ```sh
   source .venv/bin/activate
   ```
3. Install the packages that `forrest_type.py` needs:
   ```sh
   pip install -r requirements.txt
   ```

The offline unit tests use `pytest`. Install it with `pip install pytest`.

### Run

```sh
python forrest_type.py LAT LON [--radius 1000] [--raw] [--no-wiki]
                      [--no-gbif] [--endpoint URL] [--osm-pbf FILE]
                      [--corine FILE]
```

Examples:

```sh
python forrest_type.py 59.3293 18.0686 --radius 2000   # Stockholm (Djurgården)
python forrest_type.py 57.0 14.5                      # rural forest, Sweden
python forrest_type.py 0 0                            # ocean -> empty-result path
python forrest_type.py 59.3293 18.0686 --raw > dump.json
```

The script asks the Overpass API for forest areas (`natural=wood`,
`landuse=forest`), tree rows (`natural=tree_row`) and mapped trees
(`natural=tree`). The radius is the distance around your point. The
default radius is 1000 m. The script prints:

- the number of forest areas and mapped trees
- a forest-type estimate from the `leaf_type` and `leaf_cycle` tags, for
  example "Deciduous broadleaf forest"
- named forests and the `wood=*` and `protect_class=*` tags
- the most common `species`, `genus` and `taxon` values. The script
  splits values that contain `;` into separate names.
- short Wikipedia descriptions of the top species. OSM tags carry no
  appearance data. The script therefore reads the
  [Wikipedia REST API](https://en.wikipedia.org/api/rest_v1/).
- species that people observed within the radius. The
  [GBIF occurrence API](https://www.gbif.org/developer/summary) is an
  independent source. It also works where nobody mapped trees in OSM.
  GBIF lists all plants. Herbs and mosses appear next to the trees.

The flag `--raw` prints the raw Overpass JSON. Study that dump to learn
what data exists around a point. That study is the main purpose of this
proof of concept.

### Offline mode (`--osm-pbf`)

You can read the same OSM data from a local extract instead of the
Overpass API. An extract is a snapshot file of the map data. The extract
gives repeatable results and no HTTP 504 replies.

```sh
mkdir -p data
curl -L -o data/sweden-latest.osm.pbf \
    https://download.geofabrik.de/europe/sweden-latest.osm.pbf
python forrest_type.py 59.3293 18.0686 --radius 2000 \
    --osm-pbf data/sweden-latest.osm.pbf
```

[Geofabrik](https://download.geofabrik.de/) publishes extracts for many
regions. The extract mode needs the `osmium` (pyosmium) package. It is in
`requirements.txt`. With `--osm-pbf`, the flag `--raw` prints the
collected elements as JSON.

Notes:

- pyosmium assembles forests from closed ways and multipolygon
  relations. Tree rows come from open ways. Trees come from nodes.
- The extract mode approximates the `around:` rule. An element counts
  when any vertex lies within the radius, or when your point lies inside
  a forest polygon.
- An extract can lag behind the live OSM database by hours or days.

### CORINE land cover (`--corine`)

OSM tags do not cover every forest. For a forest-type estimate that does
not depend on OSM, the script can sample a CORINE Land Cover raster. A
raster is a grid image where each cell holds a value. CORINE uses 100 m
cells. The forest classes are 311 (broad-leaved), 312 (coniferous),
313 (mixed) and 324 (transitional woodland).

```sh
python forrest_type.py 59.3293 18.0686 --corine data/clc2018_europe.tif
```

The CORINE raster is free. You must create a free account on the
Copernicus Land Monitoring Service to download it:
<https://land.copernicus.eu/pan-european/corine-land-cover/clc2018>.
Put the GeoTIFF file in `data/`. That directory is not tracked by git.
Any class-coded GeoTIFF works. The script measures the distance from
your point to each pixel centre with the haversine formula (a standard
way to measure distance on a sphere). It reads the native EPSG:3035
rasters correctly, and it reads Web Mercator exports correctly too.
This mode needs `rasterio` and `pyproj`. They are in
`requirements.txt`.

The report shows the class mix within the radius and the dominant forest
class. OSM and CORINE are independent sources. They usually agree in
areas that mappers mapped well. Use them to check each other.

## Data-source caveats and limitations

- OSM species tagging is sparse. Many forest areas carry only
  `leaf_type` and `leaf_cycle`. Some carry no forest tags at all. When
  the script finds no species data, it prints that fact. The forest type
  then reads "unspecified". The script does not guess.
- The public Overpass servers limit request rates (HTTP 429) and time
  out (HTTP 504) on large queries. The script retries with backoff. If
  the first server fails, it falls back to the
  [kumi.systems mirror](https://overpass.kumi.systems/). For this
  reason the radius cap is 5000 m.
- The Overpass `around:` rule matches elements that touch the radius. A
  point inside a large forest usually matches. A point close to the
  forest edge does not always match. The `--osm-pbf` mode applies the
  same rule locally. An element counts when any vertex lies within the
  radius, or when your point lies inside a forest polygon.
- Wikipedia titles do not always match the scientific names in OSM
  tags. A failed lookup is not an error. The script skips it.
- GBIF counts observations, not forest composition. People record plants
  where they walk. The counts favour well-surveyed and often urban
  areas. A failed GBIF request prints a warning. The report continues
  without that section.
- The OSM forest-type label is an estimate from the dominant tags. The
  `--corine` section classifies actual land cover from satellite data.
  CORINE works at 100 m resolution and has its own errors. For example,
  an urban park can come out as forest. A clear-cut can come out as
  transitional woodland.

## Data licenses

The sources have their own licenses. This section names each license and
the credit line to use.

OpenStreetMap data uses the Open Database License (ODbL). ODbL requires
this credit line: "© OpenStreetMap contributors". ODbL also has a
share-alike clause. If you build a derived database from OSM data, that
database must use ODbL too. A report that reads OSM and prints findings
is not a derived database. Geofabrik extracts carry the same ODbL terms
as the live data.

Wikipedia text uses the Creative Commons Attribution-ShareAlike license
(CC BY-SA). A report that quotes a description credits Wikipedia. An app
that redistributes the text must also share that text under CC BY-SA.

GBIF brokers data from thousands of publishers. Each occurrence record
carries the license of its dataset. Most records are CC0 or CC BY. Some
are CC BY-NC (non-commercial use only). A commercial product must filter
those records out or avoid redistributing them. GBIF asks for credit to
the underlying datasets.

CORINE Land Cover and other Copernicus data use the Copernicus license.
The license is free and allows commercial use. It has no share-alike
clause. It requires an attribution line, for example: "Land cover: ©
Copernicus - CLC 2018".

The current script only reads data and prints a report. It therefore
needs only the credit lines above. The planned Flutter app will
redistribute data. Check this section again before that work starts.

## License

This project uses the Mozilla Public License 2.0 (MPL-2.0). The LICENSE
file in the repository root holds the full license text.

