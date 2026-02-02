# Active Learning System: Data Flow
This document details the flow of data from raw data, to labeling session data, to PostGIS + session storage, to ML-ready training datasets (e.g. COCO).

# Step 1) Raw Data --> Session Data
Raw data lives on NAS or client machines. Session data is stored under `<session_base_directory>/<session_id>/`.
- **New sessions:** A new `<session_id>` directory is created. CRS is inferred from the raster. Tiles GeoJSON and labels GeoJSON are written with that CRS; each tile gets a **transform** (affine pixel→CRS) so downstream steps can convert geometry without the raster.
- **Revisiting sessions:** Existing files and tile directory are updated. CRS is taken from the existing tiles file so the session stays consistent even if the raster is missing or moved.

# Step 2) Session Data --> PostGIS + Data Lake
Ingest reads `parameters.json`, `tiles.geojson`, and `labels.geojson` for a session and writes to PostGIS. Chip images remain under the session directory (WebP in `tiles/`).

- **Tiles:** Each tile gets a global `id` (SERIAL). Stored: `tile_id` (col_row, e.g. `"35328_11264"`), `session_id`, geometry (SRID 0; actual CRS in `crs` column), `chip_path`, **crs** (e.g. `"EPSG:32610"`), **transform** (affine `"a,b,c,d,e,f"`). `UNIQUE(session_id, tile_id)` so the same col_row can appear in different sessions.
- **Labels:** Stored with **tile_pk** referencing `tiles(id)` (not `tile_id`). Geometry uses SRID 0; CRS is implied by the tile. Labels whose tile was not in the viewed tiles inserted are dropped.

**Revisiting sessions:** If the session already exists in the DB, it is deleted (labels, tiles, session) and re-submitted so the latest submission is the source of truth. Then:
- `session` entry is created (including `present_classes` from parameters).
- Viewed tiles are inserted (with `crs` and `transform` from tiles GeoJSON).
- Labels are inserted with `tile_pk` resolved from (session_id, tile_id) after tiles are in place.

# Step 3) PostGIS + Data Lake --> Virtual Dataset (or other formats)
- **Virtual dataset:** Query sessions (optional filters). Output JSON Used to define which tiles and labels go into a dataset. Structure of the output is 
    ```JSON
    {
        "metadata": {
            "out_path": ...,
            "query_file": ...,
            "session_id": ...,
            "class_ids": ...,
        },
        "virtual_dataset": {
            tile.pk: {
                "label_ids": [
                    label1.pk,
                    label2.pk
                ]
            },
            ...
        },
    }
    ```

# Step 4) Virtual Dataset --> COCO
- **COCO dataset:** Input = virtual dataset JSON + output dir. Copies WebP chips by tile pk; builds `annotations/instances.json`. 
- **Bbox conversion** uses each tile’s stored **transform** (inverse: CRS → tile pixel); the raster is not required. Labels on tiles without a transform are skipped.
