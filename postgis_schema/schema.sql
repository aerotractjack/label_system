-- Enable PostGIS if not already enabled
CREATE EXTENSION IF NOT EXISTS postgis;

-- 1. Sessions Table (From params.json)
CREATE TABLE sessions (
    id UUID PRIMARY KEY,
    raster_path TEXT,
    plots_file_path TEXT,
    chip_size INTEGER,
    overlap FLOAT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    present_classes INTEGER[]  -- unique class_ids labeled in this session; query: WHERE 5 = ANY(present_classes)
);
-- For existing DBs: ALTER TABLE sessions ADD COLUMN IF NOT EXISTS present_classes INTEGER[];

-- 2. Tiles Table (From tiles.geojson)
CREATE TABLE tiles (
    id SERIAL PRIMARY KEY,
    tile_id TEXT NOT NULL,  -- col_row e.g. "35328_11264", unique per session only
    session_id UUID REFERENCES sessions(id),
    is_empty BOOLEAN,
    geometry GEOMETRY(Polygon, 0),  -- dynamic SRID per row
    audit_status TEXT DEFAULT 'pending',
    chip_path TEXT,  -- absolute path to WebP chip
    crs TEXT,  -- e.g. "EPSG:32610"
    transform TEXT,  -- affine "a,b,c,d,e,f" (pixel -> CRS)
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
    geometry GEOMETRY(Polygon, 0),  -- dynamic SRID per row
    source TEXT DEFAULT 'human'
);
CREATE INDEX idx_labels_geometry ON labels USING GIST (geometry);