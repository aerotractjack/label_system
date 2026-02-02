"""
Create a virtual dataset: record of which DB entries would be included in a dataset.
Requires query_file (registry key or path to .sql). Filter params required per registry entry.
Query must return (tile_pk, tile_id, label_id); label_id NULL for empty tiles.
Output: JSON { tile_pk: { "tile_id": "col_row", "label_ids": [...] } } keyed by tiles.id.
"""
import json
import os
import sys
from pathlib import Path

import yaml
from sqlalchemy import create_engine, text

DB_URL = "postgresql://user:password@localhost:5432/active_learning"

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent / "filters" / "registry.yaml"


def _load_registry(registry_path):
    with open(registry_path) as f:
        return yaml.safe_load(f)


def _resolve_query_file(query_file_arg, registry_path):
    """
    If query_file_arg is a key in the registry, return (resolved_path, required_arg_names, user_query_file).
    Else treat as path; return (resolved_path, [], user_query_file). Resolved path: absolute for SQL file.
    """
    registry = _load_registry(registry_path)
    registry_dir = registry_path.parent
    user_value = query_file_arg

    if registry and query_file_arg in registry:
        entry = registry[query_file_arg]
        qf = entry.get("query_file")
        if not qf:
            raise ValueError(f"Registry entry '{query_file_arg}' has no query_file")
        path = Path(qf)
        if not path.is_absolute():
            path = (registry_dir / path).resolve()
        else:
            path = Path(qf)
        required = []
        args_spec = entry.get("args")
        if args_spec and isinstance(args_spec, dict):
            required = list(args_spec.keys())
        return str(path), required, user_value

    path = Path(query_file_arg.strip())
    if not path.is_absolute():
        path = path.resolve()
    return str(path), [], user_value


def _resolve_sql(path_or_sql):
    """If path_or_sql is a path to an existing file, return file contents; else treat as direct SQL."""
    if not path_or_sql or not str(path_or_sql).strip():
        return None
    s = str(path_or_sql).strip()
    path = Path(s)
    if s.startswith("~"):
        path = Path(os.path.expanduser(s))
    if path.is_file():
        with open(path) as f:
            return f.read().strip()
    return s


def create_virtual_dataset(engine=None, query_file=None, params=None):
    """
    query_file: path to .sql file or direct SQL. Must return (tile_pk, tile_id, label_id); label_id NULL for empty tiles. params dict passed as bind params.
    Returns: dict { tile_pk: { "tile_id": "col_row", "label_ids": [int, ...] } } keyed by tiles.id.
    """
    if engine is None:
        engine = create_engine(DB_URL)
    if not query_file or not str(query_file).strip():
        raise ValueError("query_file is required")
    sql = _resolve_sql(query_file)
    if not sql:
        raise ValueError(f"query_file did not resolve to SQL: {query_file}")
    params = params or {}
    with engine.connect() as conn:
        r = conn.execute(text(sql), params)
        rows = list(r)
    tiles = {}
    for row in rows:
        tile_pk, tile_id, label_id = row[0], row[1], row[2] if len(row) > 2 else None
        if tile_pk not in tiles:
            tiles[tile_pk] = {"tile_id": tile_id, "label_ids": []}
        if label_id is not None:
            tiles[tile_pk]["label_ids"].append(label_id)
    for pk in tiles:
        tiles[pk]["label_ids"].sort()
    return tiles


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Create virtual dataset from PostGIS. Use --query_file (registry key or path). Params required per registry entry."
    )
    parser.add_argument("--out_path", type=str, required=True, help="Output JSON path")
    parser.add_argument(
        "--query_file",
        type=str,
        required=True,
        help="Registry key (e.g. all, by_session, by_class_ids) or path to .sql file. Must return (tile_pk, tile_id, label_id).",
    )
    parser.add_argument("--registry", type=str, default=None, help="Path to registry.yaml (default: labeler/filters/registry.yaml)")
    parser.add_argument("--session_id", type=str, default=None, help="Required for by_session; bind :session_id")
    parser.add_argument("--class_ids", type=int, nargs="*", default=None, help="Required for by_class_ids; bind :class_ids (list)")
    args = parser.parse_args()

    registry_path = Path(args.registry) if args.registry else DEFAULT_REGISTRY_PATH
    if not registry_path.is_file():
        parser.error(f"Registry not found: {registry_path}")

    try:
        resolved_path, required_args, user_query_file = _resolve_query_file(args.query_file, registry_path)
    except ValueError as e:
        parser.error(str(e))

    params = {}
    for arg_name in required_args:
        val = getattr(args, arg_name, None)
        if val is None:
            parser.error(f"--query_file {args.query_file} requires --{arg_name.replace('_', '-')}")
        if arg_name == "class_ids" and isinstance(val, list) and len(val) == 0:
            parser.error("--class_ids must list at least one class_id")
        params[arg_name] = val
    if args.session_id is not None and "session_id" not in params:
        params["session_id"] = args.session_id
    if args.class_ids is not None and "class_ids" not in params:
        params["class_ids"] = args.class_ids

    result = create_virtual_dataset(query_file=resolved_path, params=params if params else None)
    output_json = {
        "out_path": args.out_path,
        "query_file": user_query_file,
        "session_id": args.session_id,
        "class_ids": args.class_ids,
        "virtual_dataset": result,
    }
    out = json.dumps(output_json, indent=2)
    with open(args.out_path, "w") as f:
        f.write(out)
    print(f"Wrote virtual dataset ({len(result)} tiles) to {args.out_path}")


if __name__ == "__main__":
    main()
