# Active Learning System: Data Schemas
This document details the different schemas for piece/step of data involved in the system.

# Step 1) Labeling Raw Data to Disk in Sessions
First step of this system is labeling raw data. The file inputs to this step are:
1) `raster_file`: The `.tif` file to be labeled
2) `plots_file`: The `GeoJSON` file designating which areas of the raster to label
   - Attributes: `plot_id` (integer) unique id of each plot in the file

**CRS:** New session: CRS is inferred from the raster. Resume: CRS is taken from the existing `tiles_file` (file-level CRS) so the session stays consistent without requiring the raster.

The file outputs of this step are:
1) `tiles_file`: `GeoJSON` file of the tiles created by the labeling service, the bounds of each chip.
   - File-level **CRS** (e.g. EPSG:32610).
   - Per-feature attributes:
     - `id`: unique `str` id of the tile (col_row e.g. `"20480_22016"`)
     - `viewed`: `boolean` whether the user has viewed the tile
     - `num_annots`: `integer` number of labels in this tile
     - `all_black`: `boolean` (only set if viewed)
     - `is_empty`: `boolean` (only set if viewed)
     - `chip_path`: absolute path to the WebP chip (optional; defaulted at ingest)
     - `transform`: `[a, b, c, d, e, f]` affine coefficients (tile pixel → CRS) for bbox conversion without the raster
2) `labels_file`: `GeoJSON` file of labels for each tile (same CRS as tiles).
   - Attributes: `class_id`, `class_name`, `tile_id` (str mapping to tile `id` in `tiles_file`), `geometry`
3) `params_file`: `JSON` file of session parameters.
   - Attributes: `RASTER_FILE`, `PLOTS_FILE`, `TILES_FILENAME`, `LABELS_FILENAME`, `CHIP_SIZE`, `OVERLAP`, `TIMESTAMPS` (list of `[start_ts, end_ts]`), `PRESENT_CLASSES` (list of class_ids labeled)
4) `tiles/` directory: `.webp` chips; filename = `tile_id.webp`

Session directory structure:
```bash
|- <session_dir>/
    |- <session_id>/
        |- tiles.geojson
        |- labels.geojson
        |- parameters.json
        |- plots.geojson
        |- tiles/
            |- 20480_22016.webp
            |- 32768_19456.webp
            ...
```

# Step 2) Converting GeoJSON Annotations to PostGIS + Data Lake
Data is stored in a PostGIS database and chip images in session directories (data lake).

**CRS and geometry:** Tiles and labels use **dynamic CRS**. Geometry columns are `GEOMETRY(Polygon, 0)` (any SRID per row). The actual CRS is stored in `tiles.crs` (e.g. `"EPSG:32610"`). Each tile also stores a **transform** (affine pixel→CRS) so downstream steps (e.g. COCO bbox conversion) do not need the raster.

**Tile identity:** Tiles have a global `id` (SERIAL) and a secondary `tile_id` (col_row, e.g. `"35328_11264"`). `tile_id` is unique only per session. Labels reference the tile by `tile_pk` → `tiles(id)`.

PostGIS schema:
```sql
-- 1. Sessions Table (From params.json)
CREATE TABLE sessions (
    id UUID PRIMARY KEY,
    raster_path TEXT,
    plots_file_path TEXT,
    chip_size INTEGER,
    overlap FLOAT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    present_classes INTEGER[]
);

-- 2. Tiles Table (From tiles.geojson)
CREATE TABLE tiles (
    id SERIAL PRIMARY KEY,
    tile_id TEXT NOT NULL,  -- col_row e.g. "35328_11264", unique per session only
    session_id UUID REFERENCES sessions(id),
    is_empty BOOLEAN,
    geometry GEOMETRY(Polygon, 0),  -- dynamic SRID per row
    audit_status TEXT DEFAULT 'pending',
    chip_path TEXT,
    crs TEXT,       -- e.g. "EPSG:32610"
    transform TEXT, -- affine "a,b,c,d,e,f" (pixel -> CRS)
    UNIQUE (session_id, tile_id)
);
CREATE INDEX idx_tiles_geometry ON tiles USING GIST (geometry);

-- 3. Labels Table (From labels.geojson)
CREATE TABLE labels (
    id SERIAL PRIMARY KEY,
    tile_pk INTEGER NOT NULL REFERENCES tiles(id),
    session_id UUID REFERENCES sessions(id),
    class_id INTEGER,
    class_name TEXT,
    geometry GEOMETRY(Polygon, 0),
    source TEXT DEFAULT 'human'
);
CREATE INDEX idx_labels_geometry ON labels USING GIST (geometry);
```

Chip images live under each session directory:
```bash
|- <session_base_directory>/
    |- <session_id>/
        |- tiles/
            |- 20480_22016.webp
            ...
```

# Step 3) PostGIS + Data Lake to COCO
**Virtual dataset** (e.g. `create_virtual_dataset.py`): Query sessions (optional filters: session_ids, chip_sizes, present_classes, etc.). Output JSON keyed by **tile primary key** (tiles.id): `{ tile_pk: { "tile_id": "col_row", "label_ids": [ ... ] } }`. Includes empty tiles.

**COCO dataset** (e.g. `create_coco_dataset.py`): Input = virtual dataset JSON path + output directory. Copies WebP chips into `output_dir/images/` (filenames = tile pk for uniqueness). Builds `output_dir/annotations/instances.json` (COCO format). **Bbox conversion** uses each tile’s stored `transform` (inverse affine: CRS → tile pixel); the raster is **not** required. Labels without a tile transform are skipped (with a warning).