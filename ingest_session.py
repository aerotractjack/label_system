import json
import pandas as pd
import geopandas as gpd
from pathlib import Path
from sqlalchemy import Integer, create_engine, text
from sqlalchemy.dialects.postgresql import ARRAY

# Database connection string
DB_URL = "postgresql://user:password@localhost:5432/active_learning"
engine = create_engine(DB_URL)

def ingest_session(params_path, tiles_path, labels_path):
    # 1. Load Params
    with open(params_path, 'r') as f:
        params = json.load(f)
    
    # Extract session ID from the file path in params
    session_id = params['TILES_FILENAME'].split('/')[-2]

    # 2. If session already exists (revisiting), delete it and its tiles/labels so we re-submit as source of truth
    with engine.connect() as conn:
        r = conn.execute(text("SELECT 1 FROM sessions WHERE id = :sid LIMIT 1"), {"sid": session_id})
        if r.fetchone() is not None:
            conn.execute(text("DELETE FROM labels WHERE session_id = :sid"), {"sid": session_id})
            conn.execute(text("DELETE FROM tiles WHERE session_id = :sid"), {"sid": session_id})
            conn.execute(text("DELETE FROM sessions WHERE id = :sid"), {"sid": session_id})
            conn.commit()
            print(f"Re-submitting existing session {session_id}: removed previous session, tiles, and labels.")

    # 3. Insert Session (sessions table has no geometry; use pandas)
    present_classes = [int(x) for x in params.get('PRESENT_CLASSES', [])]
    session_df = pd.DataFrame([{
        'id': session_id,
        'raster_path': params['RASTER_FILE'],
        'plots_file_path': params['PLOTS_FILE'],
        'chip_size': params['CHIP_SIZE'],
        'overlap': params['OVERLAP'],
        'present_classes': present_classes,
    }])
    session_df.to_sql('sessions', engine, if_exists='append', index=False, dtype={'present_classes': ARRAY(Integer)})
    print(f"Registered Session: {session_id}")

    # 3. Load and Insert Tiles
    tiles_gdf = gpd.read_file(tiles_path)
    # Rename 'id' to 'tile_id' to match our DB schema
    tiles_gdf = tiles_gdf.rename(columns={'id': 'tile_id'})
    tiles_gdf = tiles_gdf[tiles_gdf["viewed"] == True]
    tiles_gdf['session_id'] = session_id
    session_dir = Path(tiles_path).parent
    if 'chip_path' not in tiles_gdf.columns:
        tiles_gdf['chip_path'] = tiles_gdf['tile_id'].apply(
            lambda x: str((session_dir / "tiles" / f"{x}.webp").resolve())
        )
    else:
        tiles_gdf['chip_path'] = tiles_gdf['chip_path'].apply(
            lambda p: str((session_dir / p).resolve()) if p and not Path(str(p)).is_absolute() else p
        )
    # CRS and transform for dynamic CRS / bbox conversion without raster
    tiles_gdf['crs'] = str(tiles_gdf.crs) if tiles_gdf.crs is not None else None
    if 'transform' in tiles_gdf.columns:
        def serialize_transform(t):
            if t is None:
                return None
            try:
                lst = list(t) if hasattr(t, '__iter__') and not isinstance(t, str) else t
                if len(lst) == 6:
                    return ','.join(str(x) for x in lst)
            except (TypeError, ValueError):
                pass
            return None
        tiles_gdf['transform'] = tiles_gdf['transform'].apply(serialize_transform)
    else:
        tiles_gdf['transform'] = None

    # Select only columns that exist in our DB (do not supply id; SERIAL assigns it)
    tiles_to_db = tiles_gdf[['tile_id', 'session_id', 'is_empty', 'geometry', 'chip_path', 'crs', 'transform']].copy()
    tiles_to_db.crs = None  # table has GEOMETRY(Polygon, 0); CRS is stored in crs column
    tiles_to_db.to_postgis('tiles', engine, if_exists='append', index=False)
    print(f"Uploaded {len(tiles_gdf)} tiles.")

    # Build col_row -> tiles.id so labels can reference tile_pk
    with engine.connect() as conn:
        r = conn.execute(
            text("SELECT id, tile_id FROM tiles WHERE session_id = :sid"),
            {"sid": session_id},
        )
        col_row_to_tile_pk = {str(row[1]): row[0] for row in r}

    # 5. Load and Insert Labels (labels reference tiles.id via tile_pk)
    labels_gdf = gpd.read_file(labels_path)
    labels_gdf['session_id'] = session_id
    # Resolve tile_id (col_row from GeoJSON) to tile primary key
    labels_gdf['tile_pk'] = labels_gdf['tile_id'].astype(str).map(col_row_to_tile_pk)
    # Drop labels whose tile was not in the viewed tiles we inserted
    labels_gdf = labels_gdf.dropna(subset=['tile_pk'])
    labels_gdf['tile_pk'] = labels_gdf['tile_pk'].astype(int)
    labels_to_db = labels_gdf[['tile_pk', 'session_id', 'class_id', 'class_name', 'geometry']].copy()
    labels_to_db.crs = None  # table has GEOMETRY(Polygon, 0)
    labels_to_db.to_postgis('labels', engine, if_exists='append', index=False)
    print(f"Uploaded {len(labels_to_db)} labels.")

if __name__ == "__main__":
    # Point these to your local files
    import sys
    from pathlib import Path
    from argparse import ArgumentParser
    parser = ArgumentParser()
    parser.add_argument("--session_id", type=str, required=False)
    parser.add_argument("--params_path", type=str, required=False)
    parser.add_argument("--tiles_path", type=str, required=False)
    parser.add_argument("--labels_path", type=str, required=False)
    args = parser.parse_args()
    if args.params_path:
        params_path = args.params_path
        tiles_path = args.tiles_path
        labels_path = args.labels_path
    else:
        session_id = args.session_id
        session_dir = Path(f'/home/aerotract/2software/sample_data_lake/sessions/{session_id}')
        params_path = session_dir / 'parameters.json'
        tiles_path = session_dir / 'tiles.geojson'
        labels_path = session_dir / 'labels.geojson'
    ingest_session(
        params_path=params_path,
        tiles_path=tiles_path,
        labels_path=labels_path
    )