"""
Create a COCO-format dataset from a virtual dataset JSON (outfile from create_virtual_dataset).
Input: virtual dataset JSON path, output directory (created if not exists).
Output: output_dir/images/ (copied chip images), output_dir/annotations/instances.json (COCO format).
Uses DB to resolve tile pk (tiles.id) -> chip_path, session_id and label geometry -> pixel bbox.
"""
import json
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import yaml
from PIL import Image
from rasterio.transform import Affine
from sqlalchemy import create_engine, text

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False

DB_URL = "postgresql://user:password@localhost:5432/active_learning"


def load_virtual_dataset(path):
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict) and "virtual_dataset" in data:
        return data["virtual_dataset"]
    return data


def bbox_crs_to_tile_pixel(minx, miny, maxx, maxy, transform_str):
    """Convert bbox in CRS to [x, y, width, height] in tile pixel coords using stored tile transform.
    transform_str: 'a,b,c,d,e,f' (Affine: pixel -> CRS). COCO bbox is [x, y, w, h] top-left."""
    if not transform_str or not transform_str.strip():
        return None
    try:
        parts = [float(x) for x in transform_str.strip().split(",")]
        if len(parts) != 6:
            return None
        t = Affine(*parts)
        inv = ~t
    except (ValueError, TypeError):
        return None
    col_min, row_min = inv * (minx, miny)
    col_max, row_max = inv * (maxx, maxy)
    col_lo, row_lo = min(col_min, col_max), min(row_min, row_max)
    col_hi, row_hi = max(col_min, col_max), max(row_min, row_max)
    x = col_lo
    y = row_lo
    w = col_hi - col_lo
    h = row_hi - row_lo
    x = max(0, x)
    y = max(0, y)
    return [round(x, 2), round(y, 2), round(w, 2), round(h, 2)]


def load_class_colors_from_yaml(path):
    """Load detection_classes.yaml and return {class_id: color_name} for annot_color."""
    if not path or not os.path.isfile(path):
        return {}
    with open(path) as f:
        data = yaml.safe_load(f)
    if not data:
        return {}
    out = {}
    for entry in data.values() if isinstance(data, dict) else []:
        if isinstance(entry, dict) and "class_id" in entry and "annot_color" in entry:
            out[int(entry["class_id"])] = entry["annot_color"]
    return out


def _plot_sample_verification(images_dir, coco, output_dir, n=3, class_colors=None):
    """Plot n sample images with bounding boxes to verify COCO reconstruction.
    class_colors: optional dict category_id -> color name (e.g. from detection_classes.yaml)."""
    images = coco["images"]
    annotations = coco["annotations"]
    categories = {c["id"]: c["name"] for c in coco["categories"]}
    ann_by_image = {}
    for a in annotations:
        iid = a["image_id"]
        ann_by_image.setdefault(iid, []).append(a)
    # Prefer images that have annotations
    with_ann = [img for img in images if img["id"] in ann_by_image]
    without_ann = [img for img in images if img["id"] not in ann_by_image]
    sample_images = (with_ann[:n] if len(with_ann) >= n else with_ann) + (
        without_ann[: max(0, n - len(with_ann))]
    )
    sample_images = sample_images[:n]
    if not sample_images:
        return
    fig, axes = plt.subplots(1, len(sample_images), figsize=(5 * len(sample_images), 5))
    if len(sample_images) == 1:
        axes = [axes]
    if class_colors:
        cat_to_color = {cid: class_colors.get(cid, "lime") for cid in categories}
    else:
        colors = plt.cm.tab10(np.linspace(0, 1, max(len(categories), 1)))
        cat_to_color = {cid: colors[i % len(colors)] for i, cid in enumerate(sorted(categories.keys()))}
    for ax, img_info in zip(axes, sample_images):
        path = os.path.join(images_dir, img_info["file_name"])
        if not os.path.exists(path):
            ax.text(0.5, 0.5, f"Missing: {img_info['file_name']}", ha="center", va="center")
            ax.set_axis_off()
            continue
        arr = np.array(Image.open(path).convert("RGB"))
        ax.imshow(arr)
        ann_count = 0
        for a in ann_by_image.get(img_info["id"], []):
            x, y, w, h = a["bbox"]
            cat_id = a["category_id"]
            color = cat_to_color.get(cat_id, "lime")
            rect = mpatches.Rectangle((x, y), w, h, linewidth=2, edgecolor=color, facecolor="none")
            ax.add_patch(rect)
            name = categories.get(cat_id, str(cat_id))
            txt = f"{cat_id}:{name}"
            ax.text(x, max(0, y - 4), txt, color="white", fontsize=8, bbox=dict(boxstyle="round", facecolor=color, alpha=0.8))
            ann_count += 1
        ax.set_title(f"{img_info['file_name']} ({ann_count} annotations)", fontsize=9)
        ax.axis("off")
    plt.tight_layout()
    out_path = os.path.join(output_dir, "sample_verification.png")
    plt.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"Sample verification plot saved to {out_path}")


def create_coco_dataset(virtual_dataset_path, output_dir, engine=None, raster_path_override=None):
    """
    virtual_dataset_path: path to JSON { tile_pk: { "tile_id": "col_row", "label_ids": [...] } } (keys = tiles.id).
    output_dir: directory to create; will contain images/ and annotations/instances.json.
    raster_path_override: if set, use this path for all sessions instead of DB raster_path (use when DB path is wrong or missing).
    """
    if engine is None:
        engine = create_engine(DB_URL)
    vd = load_virtual_dataset(virtual_dataset_path)
    tile_pks = [int(k) for k in vd.keys()]
    if not tile_pks:
        os.makedirs(output_dir, exist_ok=True)
        images_dir = os.path.join(output_dir, "images")
        ann_dir = os.path.join(output_dir, "annotations")
        os.makedirs(images_dir, exist_ok=True)
        os.makedirs(ann_dir, exist_ok=True)
        coco = {"info": {}, "licenses": [], "images": [], "annotations": [], "categories": []}
        with open(os.path.join(ann_dir, "instances.json"), "w") as f:
            json.dump(coco, f, indent=2)
        print(f"Empty virtual dataset: wrote {output_dir}")
        return

    with engine.connect() as conn:
        r = conn.execute(
            text(
                "SELECT id, tile_id, chip_path, session_id, crs, transform FROM tiles WHERE id = ANY(:ids)"
            ),
            {"ids": tile_pks},
        )
        rows = list(r)
    tile_to_chip = {}
    tile_to_session = {}
    tile_to_transform = {}
    for row in rows:
        pk, col_row, chip_path, session_id = row[0], row[1], row[2], row[3]
        transform_val = row[5] if len(row) > 5 else None
        tile_to_chip[pk] = chip_path
        tile_to_session[pk] = str(session_id)
        tile_to_transform[pk] = transform_val
    session_ids = list(set(tile_to_session.values()))
    with engine.connect() as conn:
        r = conn.execute(
            text(
                "SELECT id, chip_size FROM sessions WHERE id::text = ANY(:sids)"
            ),
            {"sids": session_ids},
        )
        session_info = {str(row[0]): {"chip_size": row[1]} for row in r}
    all_label_ids = []
    for pk, rec in vd.items():
        all_label_ids.extend(rec.get("label_ids", []))
    if all_label_ids:
        with engine.connect() as conn:
            r = conn.execute(
                text(
                    """
                    SELECT id, tile_pk, class_id, class_name,
                           ST_XMin(geometry) AS minx, ST_YMin(geometry) AS miny,
                           ST_XMax(geometry) AS maxx, ST_YMax(geometry) AS maxy
                    FROM labels WHERE id = ANY(:lids)
                    """
                ),
                {"lids": all_label_ids},
            )
            label_rows = list(r)
    else:
        label_rows = []

    if all_label_ids and not label_rows:
        print("Warning: requested label_ids not found in DB. Check that the virtual dataset was built from the same DB.")

    images_dir = os.path.join(output_dir, "images")
    ann_dir = os.path.join(output_dir, "annotations")
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(ann_dir, exist_ok=True)

    image_id_by_tile = {}
    images = []
    for i, pk in enumerate(sorted(tile_pks)):
        image_id = i + 1
        image_id_by_tile[pk] = image_id
        chip_path = tile_to_chip.get(pk)
        session_id = tile_to_session.get(pk)
        chip_size = session_info.get(session_id, {}).get("chip_size", 640) if session_id else 640
        if chip_path and os.path.exists(chip_path):
            ext = Path(chip_path).suffix or ".webp"
            dest_name = f"{pk}{ext}"
            dest_path = os.path.join(images_dir, dest_name)
            shutil.copy2(chip_path, dest_path)
            with Image.open(dest_path) as im:
                w, h = im.size
        else:
            dest_name = f"{pk}.webp"
            w = h = chip_size
        images.append({
            "id": image_id,
            "file_name": dest_name,
            "width": w,
            "height": h,
        })

    categories_seen = {}
    categories = []
    annotations = []
    skipped_no_transform = 0
    for row in label_rows:
        lid, tile_pk, class_id, class_name, minx, miny, maxx, maxy = row
        if tile_pk not in image_id_by_tile:
            continue
        session_id = tile_to_session.get(tile_pk)
        session_id = str(session_id).strip() if session_id else None
        if not session_id or session_id not in session_info:
            continue
        transform_str = tile_to_transform.get(tile_pk)
        if not transform_str:
            skipped_no_transform += 1
            continue
        if minx is None or miny is None or maxx is None or maxy is None:
            continue
        bbox = bbox_crs_to_tile_pixel(minx, miny, maxx, maxy, transform_str)
        if bbox is None:
            skipped_no_transform += 1
            continue
        area = bbox[2] * bbox[3]
        if class_id not in categories_seen:
            categories_seen[class_id] = class_name
        annotations.append({
            "id": lid,
            "image_id": image_id_by_tile[tile_pk],
            "category_id": class_id,
            "bbox": bbox,
            "area": round(area, 2),
            "iscrowd": 0,
        })
    if skipped_no_transform:
        print(f"Warning: {skipped_no_transform} label(s) skipped (tile has no transform or bbox conversion failed).")
    for cid, name in sorted(categories_seen.items()):
        categories.append({"id": cid, "name": str(name), "supercategory": "object"})

    coco = {
        "info": {"description": "Virtual dataset export", "version": "1.0"},
        "licenses": [],
        "images": images,
        "annotations": annotations,
        "categories": categories,
    }
    with open(os.path.join(ann_dir, "instances.json"), "w") as f:
        json.dump(coco, f, indent=2)
    print(f"Wrote COCO dataset to {output_dir}: {len(images)} images, {len(annotations)} annotations, {len(categories)} categories.")

    if _HAS_MPL and images:
        detection_classes_path = Path(__file__).resolve().parent / "detection_classes.yaml"
        class_colors = load_class_colors_from_yaml(str(detection_classes_path))
        _plot_sample_verification(images_dir, coco, output_dir, n=3, class_colors=class_colors or None)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Create COCO dataset from virtual dataset JSON.")
    parser.add_argument("virtual_dataset", help="Path to virtual dataset JSON (e.g. out.json)")
    parser.add_argument("output_dir", help="Output directory (created if not exists)")
    parser.add_argument("--raster_path", "-r", default=None, help="Override raster path for all sessions (use when DB path is missing or wrong)")
    args = parser.parse_args()
    create_coco_dataset(
        args.virtual_dataset,
        args.output_dir,
        raster_path_override=args.raster_path,
    )
    shutil.copy(args.virtual_dataset, os.path.join(args.output_dir, "virt.json"))


if __name__ == "__main__":
    main()
