"""
Create a virtual dataset: record of which DB entries would be included in a dataset.
Input: query on sessions (optional sets of values per session field; omit = no filter).
Output: JSON { tile_pk: { "tile_id": "col_row", "label_ids": [...] } } keyed by tiles.id (include empty tiles).
"""
import json
import sys

from sqlalchemy import create_engine, text

DB_URL = "postgresql://user:password@localhost:5432/active_learning"


def build_session_where_and_params(query):
    """Build WHERE clause and params for sessions query. query keys: session_ids, raster_paths, chip_sizes, overlaps, present_classes (all optional). Omit = no filter; empty list = match none."""
    conditions = []
    params = {}
    if query.get("session_ids") is not None:
        vals = list(query["session_ids"])
        if vals:
            conditions.append("id::text = ANY(:session_ids)")
            params["session_ids"] = [str(v) for v in vals]
        else:
            conditions.append("1 = 0")
    if query.get("raster_paths") is not None:
        vals = list(query["raster_paths"])
        if vals:
            conditions.append("raster_path = ANY(:raster_paths)")
            params["raster_paths"] = vals
        else:
            conditions.append("1 = 0")
    if query.get("chip_sizes") is not None:
        vals = list(query["chip_sizes"])
        if vals:
            conditions.append("chip_size = ANY(:chip_sizes)")
            params["chip_sizes"] = vals
        else:
            conditions.append("1 = 0")
    if query.get("overlaps") is not None:
        vals = list(query["overlaps"])
        if vals:
            conditions.append("overlap = ANY(:overlaps)")
            params["overlaps"] = vals
        else:
            conditions.append("1 = 0")
    if query.get("present_classes") is not None:
        vals = [int(x) for x in query["present_classes"]]
        if vals:
            conditions.append("present_classes && :present_classes")
            params["present_classes"] = vals
        else:
            conditions.append("1 = 0")
    where = " AND ".join(conditions) if conditions else "TRUE"
    return where, params


def create_virtual_dataset(query, engine=None, skip_negatives=False ):
    """
    query: dict with optional keys session_ids, raster_paths, chip_sizes, overlaps, present_classes (sets of values; omit = all).
    Returns: dict { tile_pk: { "tile_id": "col_row", "label_ids": [int, ...] } } keyed by tiles.id (empty tiles included).
    """
    if engine is None:
        engine = create_engine(DB_URL)
    where, params = build_session_where_and_params(query)
    sql_sessions = text(f"SELECT id FROM sessions WHERE {where}")
    with engine.connect() as conn:
        r = conn.execute(sql_sessions, params)
        session_ids = [row[0] for row in r]
    if not session_ids:
        return {}
    session_ids_str = [str(s) for s in session_ids]
    tile_filter = ""
    if skip_negatives:
        tile_filter = " AND id IN (SELECT DISTINCT tile_pk FROM labels WHERE session_id::text = ANY(:sids))"
    tiles_sql = f"SELECT id, tile_id FROM tiles WHERE session_id::text = ANY(:sids){tile_filter}"
    with engine.connect() as conn:
        r = conn.execute(text(tiles_sql), {"sids": session_ids_str})
        tiles = {row[0]: {"tile_id": row[1], "label_ids": []} for row in r}
        r = conn.execute(
            text("SELECT id, tile_pk FROM labels WHERE session_id::text = ANY(:sids)"),
            {"sids": session_ids_str},
        )
        for row in r:
            lid, tile_pk = row[0], row[1]
            if tile_pk in tiles:
                tiles[tile_pk]["label_ids"].append(lid)
    for pk in tiles:
        tiles[pk]["label_ids"].sort()
    return tiles


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--query_path", type=str, required=False)
    parser.add_argument("--out_path", type=str, required=False)
    parser.add_argument("--skip_negatives", action="store_true")
    args = parser.parse_args()
    query_path = args.query_path
    out_path = args.out_path
    if query_path is None:
        query = {}
    else:
        with open(query_path) as f:
            query = json.load(f)
    result = create_virtual_dataset(query, skip_negatives=args.skip_negatives)
    output_json = {
        "query_path": query_path,
        "query": query,
        "out_path": out_path,
        "skip_negatives": args.skip_negatives,
        "virtual_dataset": result,
    }
    out = json.dumps(output_json, indent=2)
    if out_path:
        with open(out_path, "w") as f:
            f.write(out)
        print(f"Wrote virtual dataset ({len(result)} tiles) to {out_path}")
    else:
        print(out)


if __name__ == "__main__":
    main()
