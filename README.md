# Forrest Type Classification

Based on a location (Lat/Long), describe what type of forrest there are in
the surrounding area. What are the typical species of trees that can be found.
What do they typical look like, what properties has the tree got and other
kinds of information would be nice to read about.

We could make use of the Overpass API to obtain this information, see:

https://wiki.openstreetmap.org/wiki/Overpass_API#Quick_Start_(60_seconds):_for_Developers/Programmers

As a proof-of-concept we could perhaps implement a simple Python program to
extract and study what information we can gather.

That proof-of-concept is `forrest_type.py` (see Usage below).

A future extension could be to create a fun and informative Android first Flutter app.

## Usage

### Setup

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`pytest` is used for the offline unit tests (`pip install pytest`).

### Run

```sh
python forrest_type.py LAT LON [--radius 1000] [--raw] [--no-wiki] [--endpoint URL]
```

Examples:

```sh
python forrest_type.py 59.3293 18.0686 --radius 2000   # Stockholm (Djurgården)
python forrest_type.py 57.0 14.5                      # rural forest, Sweden
python forrest_type.py 0 0                            # ocean -> empty-result path
python forrest_type.py 59.3293 18.0686 --raw > dump.json
```

The script queries the Overpass API for forest areas (`natural=wood`,
`landuse=forest`), tree rows (`natural=tree_row`) and mapped trees
(`natural=tree`) within the radius, then prints:

- counts of forest areas and mapped trees,
- a forest-type estimate built from `leaf_type` + `leaf_cycle`
  (e.g. "Deciduous broadleaf forest"),
- named forests, `wood=*` and `protect_class=*` tags,
- the most common `species` / `genus` / `taxon` values (split on `;`),
- short Wikipedia descriptions of the top species (from the
  [Wikipedia REST API](https://en.wikipedia.org/api/rest_v1/)),
  since OSM tags carry no appearance data.

`--raw` dumps the raw Overpass JSON — this is the main purpose of the PoC:
studying what data actually exists around a point.

## Data-source caveats and limitations

- **OSM species tagging is sparse.** Many forest areas only carry
  `leaf_type`/`leaf_cycle`, and plenty carry no forest tags at all. When no
  species data is found the report says so and the forest type falls back to
  "unspecified" rather than guessing.
- **The public Overpass servers rate-limit (HTTP 429) and time out (HTTP 504)**
  on large queries. The script retries with backoff and falls back to the
  [kumi.systems mirror](https://overpass.kumi.systems/). The radius is capped
  at 5000 m for this reason.
- **`around` matches elements touching the radius**, so a point inside a large
  forest usually matches; a point near but outside it may not.
- **Wikipedia titles may not match** the scientific names in OSM tags; failed
  lookups are non-fatal and simply skipped.
- The forest-type label is a rough estimate from the dominant tags, not a
  proper classification of the vegetation.
