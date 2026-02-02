import tkinter as tk
from tkinter import filedialog, messagebox
import rasterio
from rasterio.windows import Window
from rasterio.transform import Affine
import geopandas as gpd
from shapely.geometry import box
import numpy as np
from PIL import Image, ImageTk
import random
import os
import json
import yaml
import datetime

SQ_METERS_PER_ACRE = 4046.8564224

class ChipLabeler:
    def __init__(self, root, raster_path, chip_size=512, overlap=0.2, classes_path=None, output_filename=None, tiles_filename=None, param_filename=None, plots_filename=None, session_id=None):
        self.root = root
        self.root.title("Geospatial Chip Labeler")
        if classes_path is None:
            classes_path = "/home/aerotract/2software/labeler/detection_classes.yaml"
        self._load_classes(classes_path)
        self._load_config()
        self.session_id = session_id
        # Configuration
        self.chip_size = chip_size
        self.overlap = overlap
        self.raster_path = raster_path
        self.output_filename = output_filename or "labels.geojson"
        if tiles_filename is None:
            tiles_filename = os.path.join(os.path.dirname(self.output_filename) or ".", "tiles.geojson")
        self.tiles_filename = tiles_filename
        self.param_filename = param_filename or os.path.join(os.path.dirname(self.output_filename) or ".", "parameters.json")
        self.plots_filename = plots_filename
        
        # State
        self.windows = []  # List of (Window object, transform)
        self.current_window_idx = 0
        self.current_window = None
        self.current_transform = None
        self.rects = []  # Temporary storage for current chip
        self.rect_ids = []  # canvas id for each rect (same order as self.rects)
        self.chip_labels = {}  # 0-based chip index -> list of (x0,y0,x1,y1,...); persistent when navigating
        self.viewed_chip_indices = set()  # chip indices (tile ids) that have been viewed this session
        self.edit_tile_ids = None  # when set, we're in Edit mode stepping through these tile ids
        self.edit_index = 0
        self.mark_mode_chip_idx = 0  # chip index to return to when leaving Edit mode
        self.existing_labels = []  # Pre-existing labels loaded from output_filename if it exists
        self.all_labels = []  # Built at save time from existing_labels + chip_labels
        self._load_existing_labels()
        
        # UI State
        self.start_x = None
        self.start_y = None
        self.current_rect_id = None
        self.moving_rect_idx = None  # index of rect being moved, or None
        self.move_offset_x = 0
        self.move_offset_y = 0
        self.tile_offset_x = 0
        self.tile_offset_y = 0
        self.tile_width = 0
        self.tile_height = 0
        self.context_width = 0
        self.context_height = 0
        self.tile_in_context_col = 0
        self.tile_in_context_row = 0
        self.image_id = None
        self.tile_boundary_id = None
        self._chip_image_np = None
        self.view_scale = 1.0
        self.zoom_factor = 1.0
        self.pan_x = 0
        self.pan_y = 0
        self.panning = False
        self.pan_start_x = 0
        self.pan_start_y = 0
        self.pan_start_pan_x = 0
        self.pan_start_pan_y = 0
        self.active_class_id = min(self.classes_by_id) if self.classes_by_id else 0
        self.viewport_buffer_pct = 25
        self.show_grid_var = tk.BooleanVar(value=False)
        self.grid_line_ids = []

        # Load Raster Metadata
        with rasterio.open(self.raster_path) as src:
            self.profile = src.profile
            self.width = src.width
            self.height = src.height
            self.crs = src.crs
            print(f"Loaded {raster_path}: {self.width}x{self.height} pixels | CRS: {self.crs}")

        self.plots_boundary = None
        if self.plots_filename and os.path.exists(self.plots_filename):
            try:
                plots_gdf = gpd.read_file(self.plots_filename)
                if not plots_gdf.empty and plots_gdf.crs is not None:
                    plots_gdf = plots_gdf.to_crs(self.crs)
                    self.plots_boundary = plots_gdf.geometry.unary_union
                    if self.plots_boundary is None or self.plots_boundary.is_empty:
                        self.plots_boundary = None
                    else:
                        print(f"Loaded plots boundary from {self.plots_filename}")
            except Exception as e:
                print(f"Could not load plots boundary from {self.plots_filename}: {e}")

        # Pre-calculate sliding windows (deterministic order, optionally clipped to plots)
        self.generate_windows()
        self.tile_state = {}  # stable_tile_id -> {"viewed": bool, "all_black": bool}
        self._load_or_generate_tiles_file()
        self.visit_order = random.sample(range(len(self.windows)), len(self.windows)) if self.windows else []
        self.visit_index = 0
        self.tile_id_to_chip_idx = {}
        self._build_tile_id_to_chip_idx()
        self._restore_chip_labels_from_file()

        # UI Layout: scale bar at top, then main content = sidebar + canvas
        self.scale_frame = tk.Frame(root)
        self.scale_frame.pack(fill=tk.X, pady=4, padx=8)
        tk.Label(self.scale_frame, text="Zoom:").pack(side=tk.LEFT, padx=(0, 8))
        self.scale_var = tk.DoubleVar(value=100)
        self.scale_slider = tk.Scale(
            self.scale_frame, from_=50, to=200, resolution=10, orient=tk.HORIZONTAL,
            variable=self.scale_var, length=200, command=self._on_scale_changed,
        )
        self.scale_slider.pack(side=tk.LEFT)
        tk.Label(self.scale_frame, text="(50%–200%)").pack(side=tk.LEFT, padx=4)

        tk.Label(self.scale_frame, text="Viewport buffer (%):").pack(side=tk.LEFT, padx=(16, 8))
        self.viewport_buffer_entry = tk.Entry(self.scale_frame, width=4)
        self.viewport_buffer_entry.insert(0, "25")
        self.viewport_buffer_entry.pack(side=tk.LEFT)
        self.viewport_buffer_entry.bind("<Return>", self._on_viewport_buffer_commit)
        self.viewport_buffer_entry.bind("<FocusOut>", self._on_viewport_buffer_commit)

        tk.Label(self.scale_frame, text="Show grid").pack(side=tk.LEFT, padx=(16, 4))
        self.show_grid_cb = tk.Checkbutton(
            self.scale_frame, variable=self.show_grid_var, command=self._on_show_grid_changed,
        )
        self.show_grid_cb.pack(side=tk.LEFT)

        self.main_content = tk.Frame(root)
        self.main_content.pack(fill=tk.BOTH, expand=True)

        self.sidebar = tk.Frame(self.main_content, width=220, bg="#f0f0f0", padx=8, pady=8)
        
        self.sidebar.pack(side=tk.LEFT, fill=tk.Y)
        self.sidebar.pack_propagate(False)
        self._build_sidebar()

        self.canvas_frame = tk.Frame(self.main_content)
        self.canvas_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.right_sidebar = tk.Frame(self.main_content, width=280, bg="#f5f5f5")
        self.right_sidebar.pack(side=tk.RIGHT, fill=tk.Y)
        self.right_sidebar.pack_propagate(False)
        self.right_sidebar_canvas = tk.Canvas(self.right_sidebar, bg="#f5f5f5", highlightthickness=0)
        self.right_sidebar_scroll = tk.Scrollbar(self.right_sidebar)
        self.right_sidebar_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6, pady=8)
        self.right_sidebar_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.right_sidebar_canvas.config(yscrollcommand=self.right_sidebar_scroll.set)
        self.right_sidebar_scroll.config(command=self.right_sidebar_canvas.yview)
        self.right_sidebar_inner = tk.Frame(self.right_sidebar_canvas, bg="#f5f5f5")
        self._right_sidebar_window_id = self.right_sidebar_canvas.create_window(0, 0, window=self.right_sidebar_inner, anchor=tk.NW)

        def _on_right_sidebar_inner_configure(event):
            self.right_sidebar_canvas.configure(scrollregion=self.right_sidebar_canvas.bbox("all"))

        def _on_right_sidebar_canvas_configure(event):
            self.right_sidebar_canvas.itemconfig(self._right_sidebar_window_id, width=event.width)

        self.right_sidebar_inner.bind("<Configure>", _on_right_sidebar_inner_configure)
        self.right_sidebar_canvas.bind("<Configure>", _on_right_sidebar_canvas_configure)

        def _scroll_right_sidebar(event):
            delta = getattr(event, "delta", None)
            if delta is not None:
                units = int(-1 * delta / 120)
            else:
                units = -1 if event.num == 4 else 1
            self.right_sidebar_canvas.yview_scroll(units, "units")
        for w in (self.right_sidebar_canvas, self.right_sidebar_inner):
            w.bind("<MouseWheel>", _scroll_right_sidebar)
            w.bind("<Button-4>", _scroll_right_sidebar)
            w.bind("<Button-5>", _scroll_right_sidebar)

        self.canvas = tk.Canvas(self.canvas_frame, cursor="cross", bg="grey")
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.btn_frame = tk.Frame(root)
        self.btn_frame.pack(fill=tk.X, pady=5)
        
        self.lbl_info = tk.Label(self.btn_frame, text="Initializing...")
        self.lbl_info.pack(side=tk.LEFT, padx=10)
        self.lbl_class = tk.Label(self.btn_frame, text="Class: --")
        self.lbl_class.pack(side=tk.LEFT, padx=10)
        
        self.btn_edit = tk.Button(self.btn_frame, text="Edit annotations", command=self._enter_edit_mode)
        self.btn_edit.pack(side=tk.RIGHT, padx=5)
        self.btn_mark = tk.Button(self.btn_frame, text="Mark mode", command=self._leave_edit_mode)
        tk.Button(self.btn_frame, text="Prev (Left)", command=self.prev_chip).pack(side=tk.RIGHT, padx=5)
        tk.Button(self.btn_frame, text="All black (skip)", command=self._all_black_and_next).pack(side=tk.RIGHT, padx=5)
        tk.Button(self.btn_frame, text="Next (Space/Right/Enter)", command=self.next_chip).pack(side=tk.RIGHT, padx=10)
        tk.Button(self.btn_frame, text="Save & Quit", command=self.finish).pack(side=tk.RIGHT, padx=10)
        tk.Button(self.btn_frame, text="Reset tiles/annotations", command=self._reset_tiles_annotations).pack(side=tk.RIGHT, padx=10)
        self._update_edit_buttons()
        
        # Bindings
        self.canvas.bind("<ButtonPress-1>", self.on_button_press)
        self.canvas.bind("<B1-Motion>", self.on_move_press)
        self.canvas.bind("<ButtonRelease-1>", self.on_button_release)
        self.canvas.bind("<ButtonPress-3>", self.on_right_click)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.root.bind("<space>", lambda e: self.next_chip())
        self.root.bind("<Right>", lambda e: self.next_chip())
        self.root.bind("<Return>", lambda e: self.next_chip())
        self.root.bind("<Left>", lambda e: self.prev_chip())
        self.root.bind("<Escape>", self.on_escape)
        
        for i in range(10):
            self.root.bind(f"<KeyPress-KP_{i}>", lambda e, cid=i: self._set_active_class(cid))
            self.root.bind(f"<KeyPress-{i}>", lambda e, cid=i: self._set_active_class(cid))
        if self.visit_order:
            idx = 0
            while idx < len(self.visit_order):
                if self._is_tile_all_black(self.visit_order[idx]):
                    idx += 1
                    continue
                if self._load_chip(self.visit_order[idx], skip_if_visually_black=True):
                    self.visit_index = idx
                    break
                idx += 1
            else:
                self.visit_index = 0
                self._update_info_label()
        else:
            self._update_info_label()

    def _load_classes(self, path):
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        self.classes_by_id = {}
        for val in data.values():
            if isinstance(val, dict) and "class_id" in val:
                cid = val["class_id"]
                self.classes_by_id[cid] = {
                    "class_name": val.get("class_name", ""),
                    "annot_color": val.get("annot_color", "red"),
                    "desc": val.get("desc", ""),
                }
        if not self.classes_by_id:
            self.classes_by_id[0] = {"class_name": "object", "annot_color": "red", "desc": "Object"}

    def _load_config(self):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        self.tile_boundary_color = "yellow"
        self.grid_line_color = "gray"
        if os.path.exists(config_path):
            try:
                with open(config_path) as f:
                    cfg = yaml.safe_load(f) or {}
                self.tile_boundary_color = cfg.get("tile_boundary_color", self.tile_boundary_color)
                self.grid_line_color = cfg.get("grid_line_color", self.grid_line_color)
            except Exception as e:
                print(f"Could not load config from {config_path}: {e}")

    def _active_color(self):
        return self.classes_by_id.get(self.active_class_id, {}).get("annot_color", "red")

    def _active_class_name(self):
        return self.classes_by_id.get(self.active_class_id, {}).get("class_name", "object")

    def _set_active_class(self, class_id):
        if class_id not in self.classes_by_id:
            return
        self.active_class_id = class_id
        if self.current_rect_id is not None:
            self.canvas.itemconfig(self.current_rect_id, outline=self._active_color())
        self._update_class_label()
        self._highlight_sidebar_active()

    def _update_class_label(self):
        name = self._active_class_name()
        keys = " ".join(f"Numpad{k}" for k in sorted(self.classes_by_id)[:5])
        if len(self.classes_by_id) > 5:
            keys += " ..."
        self.lbl_class.config(text=f"Class: {name} (id={self.active_class_id}) | {keys}")

    def _build_sidebar(self):
        """Fill sidebar with pretty-printed class config (desc, color, id, name)."""
        self.sidebar_cards = {}
        tk.Label(
            self.sidebar, text="Detection classes", font=("", 11, "bold"),
            bg="#f0f0f0", fg="#333",
        ).pack(anchor=tk.W, pady=(0, 8))
        for cid in sorted(self.classes_by_id):
            info = self.classes_by_id[cid]
            card = tk.Frame(self.sidebar, bg="#fff", relief=tk.GROOVE, bd=1, padx=6, pady=6)
            card.pack(fill=tk.X, pady=4)
            tk.Label(card, text=f"Numpad {cid}", font=("", 9, "bold"), bg="#fff", fg="#222").pack(anchor=tk.W)
            desc = info.get("desc", "") or "(no description)"
            tk.Label(card, text=desc, wraplength=190, justify=tk.LEFT, bg="#fff", fg="#444").pack(anchor=tk.W)
            row = tk.Frame(card, bg="#fff")
            row.pack(anchor=tk.W, pady=2)
            tk.Label(row, text=f"id: {cid}  name: {info.get('class_name', '')}", bg="#fff", fg="#555", font=("", 8)).pack(side=tk.LEFT)
            color = info.get("annot_color", "red")
            swatch = tk.Label(row, text=" ", bg=color, width=2, font=("", 6))
            swatch.highlight_skip = True
            swatch.pack(side=tk.LEFT, padx=(6, 0))
            tk.Label(row, text=color, bg="#fff", fg="#666", font=("", 8)).pack(side=tk.LEFT, padx=2)
            self.sidebar_cards[cid] = card
        self._highlight_sidebar_active()

    def _highlight_sidebar_active(self):
        """Highlight the active class card in the sidebar; unhighlight others."""
        for cid, card in self.sidebar_cards.items():
            bg = "#d4e8ff" if cid == self.active_class_id else "#fff"
            bd = 2 if cid == self.active_class_id else 1
            card.config(bg=bg, bd=bd, relief=tk.SOLID if cid == self.active_class_id else tk.GROOVE)
            self._set_card_tree_bg(card, bg)

    def _set_card_tree_bg(self, w, bg):
        if getattr(w, "highlight_skip", False):
            return
        try:
            w.config(bg=bg)
        except tk.TclError:
            pass
        for c in w.winfo_children():
            self._set_card_tree_bg(c, bg)

    def _load_existing_labels(self):
        """Load pre-existing labels from output_filename. Labels with tile_id go to chip_labels (restored after generate_windows); others to existing_labels."""
        self.existing_labels = []
        if not os.path.exists(self.output_filename):
            return
        try:
            gdf = gpd.read_file(self.output_filename)
            if gdf.empty:
                return
            has_tile_id = 'tile_id' in gdf.columns
            for _, row in gdf.iterrows():
                try:
                    tile_id = int(row['tile_id']) if has_tile_id else None
                except (ValueError, TypeError):
                    tile_id = None
                if tile_id is not None:
                    continue
                props = {'geometry': row.geometry}
                if 'class_id' in gdf.columns:
                    props['class_id'] = int(row['class_id'])
                else:
                    props['class_id'] = 0
                if 'class_name' in gdf.columns:
                    props['class_name'] = str(row['class_name'])
                else:
                    props['class_name'] = 'object'
                self.existing_labels.append(props)
            print(f"Loaded {len(self.existing_labels)} existing labels (no tile_id) from {self.output_filename}")
        except Exception as e:
            print(f"Could not load existing labels from {self.output_filename}: {e}")

    def _stable_tile_id(self, chip_idx):
        """Stable tile id from window position (same across sessions)."""
        w = self.windows[chip_idx]
        return f"{w.col_off}_{w.row_off}"

    def _is_tile_all_black(self, chip_idx):
        """True if this tile is marked all_black (do not show)."""
        tid = self._stable_tile_id(chip_idx)
        return self.tile_state.get(tid, {}).get("all_black", False)

    def _is_chip_image_all_black(self, img):
        """True if the chip image is visually all black (very low pixel values)."""
        if img is None or img.size == 0:
            return True
        return float(np.mean(img)) < 15 and float(np.max(img)) < 25

    def _build_tile_id_to_chip_idx(self):
        """Map tile ids from tiles file to current-session chip indices. Supports stable ids (col_row) and legacy numeric ids (match by bounds)."""
        self.tile_id_to_chip_idx = {}
        if not self.windows:
            return
        current_stable = {self._stable_tile_id(i): i for i in range(len(self.windows))}
        if not os.path.exists(self.tiles_filename):
            return
        try:
            tiles_gdf = gpd.read_file(self.tiles_filename)
            if tiles_gdf.empty or 'id' not in tiles_gdf.columns:
                return
            tol = 1e-9
            with rasterio.open(self.raster_path) as src:
                for _, tile_row in tiles_gdf.iterrows():
                    raw_id = tile_row['id']
                    chip_idx = None
                    if isinstance(raw_id, str):
                        chip_idx = current_stable.get(raw_id)
                    else:
                        try:
                            file_tile_id = int(raw_id)
                        except (ValueError, TypeError):
                            continue
                        tile_bounds = tile_row.geometry.bounds if tile_row.geometry else None
                        if tile_bounds is None:
                            continue
                        t_left, t_bottom, t_right, t_top = tile_bounds
                        for i, window in enumerate(self.windows):
                            left, bottom, right, top = src.window_bounds(window)
                            if (abs(left - t_left) < tol and abs(bottom - t_bottom) < tol and
                                    abs(right - t_right) < tol and abs(top - t_top) < tol):
                                chip_idx = i
                                break
                    if chip_idx is not None:
                        self.tile_id_to_chip_idx[raw_id] = chip_idx
                        if isinstance(raw_id, (int, float)):
                            self.tile_id_to_chip_idx[int(raw_id)] = chip_idx
        except Exception as e:
            print(f"Could not build tile_id mapping from {self.tiles_filename}: {e}")

    def _restore_chip_labels_from_file(self):
        """Restore chip_labels from labels file using tile_id -> chip_idx mapping. Uses stored tile transform when available, else raster."""
        if not os.path.exists(self.output_filename) or not self.windows:
            return
        try:
            gdf = gpd.read_file(self.output_filename)
            if gdf.empty or 'tile_id' not in gdf.columns:
                return
            tile_id_to_inv_transform = {}
            if os.path.exists(self.tiles_filename):
                try:
                    tiles_gdf = gpd.read_file(self.tiles_filename)
                    if "transform" in tiles_gdf.columns and "id" in tiles_gdf.columns:
                        for _, trow in tiles_gdf.iterrows():
                            tid = trow.get("id")
                            tr = trow.get("transform")
                            if tid is not None and tr is not None and len(tr) == 6:
                                tile_id_to_inv_transform[str(tid)] = ~Affine(*tr)
                                if isinstance(tid, (int, float)):
                                    tile_id_to_inv_transform[str(int(tid))] = ~Affine(*tr)
                except Exception:
                    pass
            for _, row in gdf.iterrows():
                raw_tid = row['tile_id']
                if raw_tid is None or (isinstance(raw_tid, float) and np.isnan(raw_tid)):
                    continue
                chip_idx = self.tile_id_to_chip_idx.get(raw_tid)
                if chip_idx is None and isinstance(raw_tid, (int, float)):
                    chip_idx = self.tile_id_to_chip_idx.get(int(raw_tid))
                if chip_idx is None:
                    continue
                geom = row.geometry
                if geom is None or geom.is_empty:
                    continue
                minx, miny, maxx, maxy = geom.bounds
                str_tid = str(raw_tid)
                inv_transform = tile_id_to_inv_transform.get(str_tid) or tile_id_to_inv_transform.get(str(int(raw_tid)) if isinstance(raw_tid, (int, float)) else None)
                if inv_transform is None and os.path.exists(self.raster_path):
                    with rasterio.open(self.raster_path) as src:
                        window = self.windows[chip_idx]
                        inv_transform = ~src.window_transform(window)
                if inv_transform is None:
                    continue
                col_min, row_min = inv_transform * (minx, miny)
                col_max, row_max = inv_transform * (maxx, maxy)
                x0, x1 = min(col_min, col_max), max(col_min, col_max)
                y0, y1 = min(row_min, row_max), max(row_min, row_max)
                class_id = int(row['class_id']) if 'class_id' in gdf.columns else 0
                class_name = str(row['class_name']) if 'class_name' in gdf.columns else 'object'
                if chip_idx not in self.chip_labels:
                    self.chip_labels[chip_idx] = []
                self.chip_labels[chip_idx].append((x0, y0, x1, y1, class_id, class_name))
            n_restored = sum(len(v) for v in self.chip_labels.values())
            if n_restored:
                print(f"Restored {n_restored} annotations to chip_labels from {self.output_filename}")
        except Exception as e:
            print(f"Could not restore chip_labels from {self.output_filename}: {e}")

    def _load_or_generate_tiles_file(self):
        """Ensure tile_state has one entry per window; load viewed/all_black from tiles file if present. Only write full tiles file if it does not exist."""
        for i in range(len(self.windows)):
            tid = self._stable_tile_id(i)
            self.tile_state[tid] = {"viewed": False, "all_black": False}
        if os.path.exists(self.tiles_filename):
            try:
                gdf = gpd.read_file(self.tiles_filename)
                if gdf.crs is not None:
                    self.crs = gdf.crs
                if not gdf.empty and "id" in gdf.columns:
                    for _, row in gdf.iterrows():
                        tid = row.get("id")
                        if tid is None or tid not in self.tile_state:
                            continue
                        self.tile_state[tid]["viewed"] = row.get("viewed", False) if "viewed" in gdf.columns else False
                        self.tile_state[tid]["all_black"] = row.get("all_black", False) if "all_black" in gdf.columns else False
                    print(f"Loaded tile state from existing {self.tiles_filename} (persistent).")
            except Exception as e:
                print(f"Could not load tile state from {self.tiles_filename}: {e}")
        else:
            self._persist_tiles()
            print(f"Created new {self.tiles_filename}.")

    def _persist_tiles(self):
        """Write full tiles GeoJSON (all windows) with viewed/all_black from tile_state."""
        tiles = self._build_tiles()
        if not tiles:
            return
        gdf_tiles = gpd.GeoDataFrame(tiles, crs=self.crs)
        gdf_tiles.to_file(self.tiles_filename, driver="GeoJSON")

    def _total_annotation_count(self):
        return len(self.existing_labels) + sum(len(v) for v in self.chip_labels.values()) + len(self.rects)

    def _count_by_class(self):
        """Return dict class_id -> count across existing_labels, chip_labels, and current rects."""
        counts = {}
        for label in self.existing_labels:
            cid = label.get("class_id", 0)
            counts[cid] = counts.get(cid, 0) + 1
        for rects in self.chip_labels.values():
            for r in rects:
                cid = r[4] if len(r) >= 6 else 0
                counts[cid] = counts.get(cid, 0) + 1
        for r in self.rects:
            cid = r[4] if len(r) >= 6 else 0
            counts[cid] = counts.get(cid, 0) + 1
        return counts

    def _plot_acres(self):
        """Total acreage of plots boundary (CRS units treated as m²). Returns None if no plots."""
        if self.plots_boundary is None or self.plots_boundary.is_empty:
            return None
        return self.plots_boundary.area / SQ_METERS_PER_ACRE

    def _viewed_tiles_acres(self):
        """Sum acreage of viewed tiles (CRS units treated as m²)."""
        if not self.windows:
            return 0.0
        total_area = 0.0
        with rasterio.open(self.raster_path) as src:
            for i in range(len(self.windows)):
                tid = self._stable_tile_id(i)
                if not self.tile_state.get(tid, {}).get("viewed", False):
                    continue
                left, bottom, right, top = src.window_bounds(self.windows[i])
                total_area += (right - left) * (top - bottom)
        return total_area / SQ_METERS_PER_ACRE

    def _viewed_tiles_count(self):
        """Number of tiles with viewed=True (avoids overlap issues for % viewed)."""
        return sum(1 for i in range(len(self.windows)) if self.tile_state.get(self._stable_tile_id(i), {}).get("viewed", False))

    def _update_right_sidebar(self):
        """Rebuild right sidebar with per-class count cards (with per-acre metrics) and geospatial context."""
        inner = self.right_sidebar_inner
        for w in inner.winfo_children():
            w.destroy()
        counts = self._count_by_class()
        plot_ac = self._plot_acres()
        viewed_ac = self._viewed_tiles_acres()

        tk.Label(inner, text="Label counts", font=("", 10, "bold"), bg="#f5f5f5", fg="#333").pack(anchor=tk.W, pady=(0, 6))
        if not counts:
            tk.Label(inner, text="No annotations yet", bg="#f5f5f5", fg="#666", font=("", 8)).pack(anchor=tk.W)
        else:
            for cid in sorted(counts.keys()):
                n = counts[cid]
                if n <= 0:
                    continue
                info = self.classes_by_id.get(cid, {"class_name": str(cid), "annot_color": "gray", "desc": ""})
                card = tk.Frame(inner, bg="#fff", relief=tk.GROOVE, bd=1, padx=4, pady=3)
                card.pack(fill=tk.X, pady=2)
                row1 = tk.Frame(card, bg="#fff")
                row1.pack(anchor=tk.W)
                tk.Label(row1, text=info.get("desc", "") or f"Class {cid}", wraplength=240, justify=tk.LEFT, bg="#fff", fg="#444", font=("", 7)).pack(anchor=tk.W)
                row2 = tk.Frame(card, bg="#fff")
                row2.pack(anchor=tk.W, pady=1)
                tk.Label(row2, text=f"id: {cid}  {info.get('class_name', '')}", bg="#fff", fg="#555", font=("", 7)).pack(side=tk.LEFT)
                color = info.get("annot_color", "red")
                swatch = tk.Label(row2, text=" ", bg=color, width=1, font=("", 4))
                swatch.pack(side=tk.LEFT, padx=2)
                tk.Label(row2, text=f"  × {n}", bg="#fff", fg="#222", font=("", 8, "bold")).pack(side=tk.LEFT)
                per_viewed_c = f"{n / viewed_ac:.1f}" if viewed_ac > 0 else "—"
                per_plot_c = f"{n / plot_ac:.1f}" if plot_ac is not None and plot_ac > 0 else "—"
                row3 = tk.Frame(card, bg="#fff")
                row3.pack(anchor=tk.W, pady=1)
                tk.Label(row3, text=f"Per viewed ac: {per_viewed_c}   Per plot ac: {per_plot_c}", bg="#fff", fg="#555", font=("", 7)).pack(anchor=tk.W)

        sep = tk.Frame(inner, height=1, bg="gray")
        sep.pack(fill=tk.X, pady=8)
        tk.Label(inner, text="Geospatial context", font=("", 10, "bold"), bg="#f5f5f5", fg="#333").pack(anchor=tk.W, pady=(0, 4))
        tk.Label(inner, text=f"Plot acres: {plot_ac:.2f}" if plot_ac is not None else "Plot acres: N/A", bg="#f5f5f5", fg="#444", font=("", 8)).pack(anchor=tk.W)
        tk.Label(inner, text=f"Viewed acres: {viewed_ac:.2f}", bg="#f5f5f5", fg="#444", font=("", 8)).pack(anchor=tk.W)
        viewed_count = self._viewed_tiles_count()
        total_tiles = len(self.windows)
        if total_tiles > 0:
            pct = 100.0 * viewed_count / total_tiles
            tk.Label(inner, text=f"% tiles viewed: {viewed_count}/{total_tiles} ({pct:.1f}%)", bg="#f5f5f5", fg="#444", font=("", 8)).pack(anchor=tk.W)
        else:
            tk.Label(inner, text="% tiles viewed: N/A", bg="#f5f5f5", fg="#444", font=("", 8)).pack(anchor=tk.W)

    def generate_windows(self):
        """Generates sliding windows with overlap (deterministic row-major order). If plots_boundary is set, only windows intersecting that boundary are kept."""
        step = int(self.chip_size * (1 - self.overlap))
        windows = []
        for row in range(0, self.height, step):
            for col in range(0, self.width, step):
                w = min(self.chip_size, self.width - col)
                h = min(self.chip_size, self.height - row)
                window = Window(col, row, w, h)
                windows.append(window)
        if self.plots_boundary is not None:
            with rasterio.open(self.raster_path) as src:
                filtered = []
                for window in windows:
                    left, bottom, right, top = src.window_bounds(window)
                    tile_box = box(left, bottom, right, top)
                    inter = tile_box.intersection(self.plots_boundary)
                    if inter.is_empty:
                        continue
                    tile_area = tile_box.area
                    if tile_area <= 0:
                        continue
                    overlap_ratio = inter.area / tile_area
                    if overlap_ratio >= 0.5:
                        filtered.append(window)
                windows = filtered
            print(f"Raster grid: {len(windows)} chip windows within plots (>=50% tile overlap, {int(self.overlap*100)}% overlap).")
        else:
            print(f"Raster grid: {len(windows)} chip windows ({int(self.overlap*100)}% overlap).")
        self.windows = windows

    def _rect_bounds(self, r):
        return r[0], r[1], r[2], r[3]

    def _rect_class(self, r):
        if len(r) >= 6:
            return r[4], r[5]
        return (0, self.classes_by_id.get(0, {}).get("class_name", "object"))

    def _color_for_class(self, class_id):
        return self.classes_by_id.get(class_id, {}).get("annot_color", "red")

    def _save_current_to_chip_labels(self):
        """Persist current chip's rects so they restore when navigating back."""
        if self.current_window_idx < 1:
            return
        chip_idx = self.current_window_idx - 1
        self.chip_labels[chip_idx] = [tuple(r) for r in self.rects]

    def _clip_rect(self, x0, y0, x1, y1):
        """Clip rect to tile boundary; return (x0,y0,x1,y1) or None if degenerate."""
        if self.tile_width <= 0 or self.tile_height <= 0:
            return (x0, y0, x1, y1) if x0 < x1 and y0 < y1 else None
        x0 = max(0, min(x0, self.tile_width))
        x1 = max(0, min(x1, self.tile_width))
        y0 = max(0, min(y0, self.tile_height))
        y1 = max(0, min(y1, self.tile_height))
        if x0 >= x1 or y0 >= y1:
            return None
        return (x0, y0, x1, y1)

    def _effective_scale(self):
        """Display scale (view scale * zoom factor)."""
        return self.view_scale * self.zoom_factor

    def _event_to_tile(self, event):
        """Convert event to tile pixel coords (0..tile_width, 0..tile_height)."""
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        s = self._effective_scale()
        if s <= 0:
            return (cx - self.tile_offset_x, cy - self.tile_offset_y)
        return ((cx - self.tile_offset_x) / s, (cy - self.tile_offset_y) / s)

    def _canvas_coords(self, x0, y0, x1, y1):
        """Convert tile coords to canvas coords for drawing (with view scale and zoom)."""
        s = self._effective_scale()
        return (x0 * s + self.tile_offset_x, y0 * s + self.tile_offset_y,
                x1 * s + self.tile_offset_x, y1 * s + self.tile_offset_y)

    def _make_scaled_photo(self):
        """Create a PhotoImage of the context image at effective scale (for display only)."""
        if self._chip_image_np is None:
            return None
        s = self._effective_scale()
        w = max(1, int(self.context_width * s))
        h = max(1, int(self.context_height * s))
        pil_img = Image.fromarray(self._chip_image_np)
        resample = getattr(Image, "Resampling", Image).LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
        pil_img = pil_img.resize((w, h), resample)
        return ImageTk.PhotoImage(image=pil_img)

    def _on_scale_changed(self, value):
        try:
            self.view_scale = float(value) / 100.0
        except (TypeError, ValueError):
            return
        if self.image_id is None:
            return
        self.tk_image = self._make_scaled_photo()
        if self.tk_image is not None:
            self.canvas.itemconfig(self.image_id, image=self.tk_image)
        self._on_canvas_configure()

    def _on_viewport_buffer_commit(self, event=None):
        try:
            raw = self.viewport_buffer_entry.get().strip()
            val = int(raw) if raw else 25
        except ValueError:
            val = 25
        val = max(0, min(100, val))
        self.viewport_buffer_pct = val
        self.viewport_buffer_entry.delete(0, tk.END)
        self.viewport_buffer_entry.insert(0, str(val))
        chip_idx = self.current_window_idx - 1
        if chip_idx >= 0:
            self._load_chip(chip_idx)
        return "break"

    def _on_show_grid_changed(self):
        if self.image_id is None:
            return
        if self.show_grid_var.get():
            self._draw_grid()
        else:
            self._clear_grid()
        self._on_canvas_configure()

    def _on_canvas_configure(self, event=None):
        """Recenter context image, tile offset, rects, and yellow boundary when canvas is resized or scale/zoom/pan changes."""
        if self.image_id is None:
            return
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return
        s = self._effective_scale()
        context_display_w = self.context_width * s
        context_display_h = self.context_height * s
        image_offset_x = (cw - context_display_w) / 2 + self.pan_x
        image_offset_y = (ch - context_display_h) / 2 + self.pan_y
        self.tile_offset_x = image_offset_x + self.tile_in_context_col * s
        self.tile_offset_y = image_offset_y + self.tile_in_context_row * s
        self.canvas.coords(self.image_id, image_offset_x, image_offset_y)
        for i, r in enumerate(self.rects):
            x0, y0, x1, y1 = self._rect_bounds(r)
            self.canvas.coords(self.rect_ids[i], *self._canvas_coords(x0, y0, x1, y1))
        if self.current_rect_id is not None and getattr(self, "_live_rect_bounds", None) is not None:
            self.canvas.coords(self.current_rect_id, *self._canvas_coords(*self._live_rect_bounds))
        if self.tile_boundary_id is not None:
            tx0 = self.tile_offset_x
            ty0 = self.tile_offset_y
            tx1 = tx0 + self.tile_width * s
            ty1 = ty0 + self.tile_height * s
            self.canvas.coords(self.tile_boundary_id, tx0, ty0, tx1, ty1)
        if self.show_grid_var.get() and self.grid_line_ids:
            for lid in self.grid_line_ids:
                self.canvas.delete(lid)
            self.grid_line_ids = []
            self._draw_grid()

    def _draw_grid(self):
        """Draw dot-line grid over entire visible raster at spacing chip_size/10."""
        s = self._effective_scale()
        image_offset_x = self.tile_offset_x - self.tile_in_context_col * s
        image_offset_y = self.tile_offset_y - self.tile_in_context_row * s
        step = self.chip_size / 10.0
        x = 0.0
        while x <= self.context_width:
            cx = image_offset_x + x * s
            self.grid_line_ids.append(
                self.canvas.create_line(
                    cx, image_offset_y,
                    cx, image_offset_y + self.context_height * s,
                    fill=self.grid_line_color, dash=(2, 2),
                )
            )
            x += step
        y = 0.0
        while y <= self.context_height:
            cy = image_offset_y + y * s
            self.grid_line_ids.append(
                self.canvas.create_line(
                    image_offset_x, cy,
                    image_offset_x + self.context_width * s, cy,
                    fill=self.grid_line_color, dash=(2, 2),
                )
            )
            y += step

    def _clear_grid(self):
        for lid in self.grid_line_ids:
            self.canvas.delete(lid)
        self.grid_line_ids = []

    def _load_chip(self, chip_idx, skip_if_visually_black=False):
        """Load chip at 0-based chip_idx; restore annotations from chip_labels if present; mark viewed.
        If skip_if_visually_black and the image is all black, mark tile all_black and return False without showing."""
        self.viewed_chip_indices.add(chip_idx)
        tid = self._stable_tile_id(chip_idx)
        if tid in self.tile_state:
            self.tile_state[tid]["viewed"] = True
        self.current_window = self.windows[chip_idx]
        self.current_window_idx = chip_idx + 1
        self.current_rect_id = None
        self.moving_rect_idx = None
        self.canvas.delete("all")
        self.grid_line_ids = []
        self.rects = []
        self.rect_ids = []
        self.image_id = None
        self.tile_boundary_id = None
        self.zoom_factor = 1.0
        self.pan_x = 0
        self.pan_y = 0

        w = self.current_window.width
        h = self.current_window.height
        col_off = self.current_window.col_off
        row_off = self.current_window.row_off
        self.tile_width = w
        self.tile_height = h
        buf = max(0, min(100, self.viewport_buffer_pct)) / 100.0
        extra_cols = int(buf * w)
        extra_rows = int(buf * h)
        col_off_read = max(0, col_off - extra_cols)
        row_off_read = max(0, row_off - extra_rows)
        width_read = min(w + 2 * extra_cols, self.width - col_off_read)
        height_read = min(h + 2 * extra_rows, self.height - row_off_read)
        if col_off_read + width_read < col_off + w:
            width_read = col_off + w - col_off_read
        if row_off_read + height_read < row_off + h:
            height_read = row_off + h - row_off_read
        context_window = Window(col_off_read, row_off_read, width_read, height_read)
        self.tile_in_context_col = col_off - col_off_read
        self.tile_in_context_row = row_off - row_off_read
        self.context_width = width_read
        self.context_height = height_read

        with rasterio.open(self.raster_path) as src:
            self.current_transform = src.window_transform(self.current_window)
            img_data = src.read(window=context_window)
            if img_data.dtype != np.uint8:
                img_data = img_data.astype(float)
                img_data = ((img_data - np.min(img_data)) / (np.max(img_data) - np.min(img_data) + 1e-5)) * 255
                img_data = img_data.astype(np.uint8)
            if img_data.shape[0] >= 3:
                img = np.moveaxis(img_data[:3], 0, -1)
            else:
                img = np.moveaxis(img_data[0:1], 0, -1)
                img = np.repeat(img, 3, axis=2)
            self._chip_image_np = img
            tile_slice = img[
                self.tile_in_context_row : self.tile_in_context_row + h,
                self.tile_in_context_col : self.tile_in_context_col + w,
            ]

        if skip_if_visually_black and self._is_chip_image_all_black(tile_slice):
            if tid in self.tile_state:
                self.tile_state[tid]["all_black"] = True
            return False

        session_dir = os.path.dirname(self.output_filename)
        tiles_dir = os.path.join(session_dir, "tiles")
        os.makedirs(tiles_dir, exist_ok=True)
        chip_path = os.path.join(tiles_dir, f"{tid}.webp")
        Image.fromarray(tile_slice.copy()).save(chip_path, "WEBP")

        self.canvas.update_idletasks()
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        s = self._effective_scale()
        if cw <= 1:
            cw = int(self.context_width * s)
        if ch <= 1:
            ch = int(self.context_height * s)
        context_display_w = self.context_width * s
        context_display_h = self.context_height * s
        image_offset_x = (cw - context_display_w) / 2 + self.pan_x
        image_offset_y = (ch - context_display_h) / 2 + self.pan_y
        self.tile_offset_x = image_offset_x + self.tile_in_context_col * s
        self.tile_offset_y = image_offset_y + self.tile_in_context_row * s
        self.tk_image = self._make_scaled_photo()
        if self.tk_image is None:
            self.tk_image = ImageTk.PhotoImage(image=Image.fromarray(self._chip_image_np))
        self.image_id = self.canvas.create_image(
            image_offset_x, image_offset_y, image=self.tk_image, anchor=tk.NW)

        if self.show_grid_var.get():
            self._draw_grid()

        if chip_idx in self.chip_labels:
            for r in self.chip_labels[chip_idx]:
                x0, y0, x1, y1 = r[0], r[1], r[2], r[3]
                clipped = self._clip_rect(x0, y0, x1, y1)
                if clipped is None:
                    continue
                x0, y0, x1, y1 = clipped
                class_id, class_name = self._rect_class(r)
                color = self._color_for_class(class_id)
                rid = self.canvas.create_rectangle(*self._canvas_coords(x0, y0, x1, y1), outline=color, width=2)
                self.rect_ids.append(rid)
                self.rects.append((x0, y0, x1, y1, class_id, class_name))

        s = self._effective_scale()
        tx0 = self.tile_offset_x
        ty0 = self.tile_offset_y
        tx1 = tx0 + self.tile_width * s
        ty1 = ty0 + self.tile_height * s
        self.tile_boundary_id = self.canvas.create_rectangle(
            tx0, ty0, tx1, ty1, outline=self.tile_boundary_color, width=2)

        self._update_info_label()
        self._update_class_label()
        self._update_right_sidebar()
        return True

    def _on_zoom(self, event):
        """Mouse wheel: zoom toward cursor (keep point under cursor fixed)."""
        if self.image_id is None:
            return
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        factor = 1.15 if (event.delta > 0 or getattr(event, "num", 0) == 4) else 1.0 / 1.15
        new_zoom = self.zoom_factor * factor
        new_zoom = max(0.2, min(5.0, new_zoom))
        if new_zoom == self.zoom_factor:
            return
        self.pan_x = cx * (1 - new_zoom / self.zoom_factor) + self.pan_x * (new_zoom / self.zoom_factor)
        self.pan_y = cy * (1 - new_zoom / self.zoom_factor) + self.pan_y * (new_zoom / self.zoom_factor)
        self.zoom_factor = new_zoom
        self.tk_image = self._make_scaled_photo()
        if self.tk_image is not None:
            self.canvas.itemconfig(self.image_id, image=self.tk_image)
        self._on_canvas_configure()

    def _on_pan_start(self, event):
        self.panning = True
        self.pan_start_x = event.x
        self.pan_start_y = event.y
        self.pan_start_pan_x = self.pan_x
        self.pan_start_pan_y = self.pan_y

    def _on_pan_motion(self, event):
        if not self.panning:
            return
        self.pan_x = self.pan_start_pan_x + (event.x - self.pan_start_x)
        self.pan_y = self.pan_start_pan_y + (event.y - self.pan_start_y)
        self._on_canvas_configure()

    def _on_pan_end(self, event):
        self.panning = False

    def _update_edit_buttons(self):
        if self.edit_tile_ids is not None:
            self.btn_edit.pack_forget()
            self.btn_mark.pack(side=tk.RIGHT, padx=5)
        else:
            self.btn_mark.pack_forget()
            if os.path.exists(self.tiles_filename):
                self.btn_edit.pack(side=tk.RIGHT, padx=5)
            else:
                self.btn_edit.pack_forget()

    def _enter_edit_mode(self):
        """Step through previously viewed tiles only (Edit = review my labels)."""
        if not os.path.exists(self.tiles_filename):
            self.root.after(10, lambda: messagebox.showwarning("No tiles", f"No tiles file found at {self.tiles_filename}"))
            return
        self._save_current_to_chip_labels()
        chip_idx = self.current_window_idx - 1
        if chip_idx >= 0 and not self.chip_labels.get(chip_idx):
            tid = self._stable_tile_id(chip_idx)
            if tid in self.tile_state:
                self.tile_state[tid]["viewed"] = False
        self._build_tile_id_to_chip_idx()
        viewed_chip_indices = [
            i for i in range(len(self.windows))
            if self.tile_state.get(self._stable_tile_id(i), {}).get("viewed", False)
            and not self.tile_state.get(self._stable_tile_id(i), {}).get("all_black", False)
        ]
        self.edit_tile_ids = sorted(viewed_chip_indices)
        if not self.edit_tile_ids:
            self.root.after(10, lambda: messagebox.showwarning("No viewed tiles", "No viewed tiles to edit. Mark some chips first."))
            return
        self.mark_mode_chip_idx = chip_idx
        self.edit_index = 0
        self._load_chip(self.edit_tile_ids[0])
        self._update_edit_buttons()
        self._update_info_label()

    def _leave_edit_mode(self):
        """Return to Mark mode at the chip we left."""
        self.edit_tile_ids = None
        try:
            self.visit_index = self.visit_order.index(self.mark_mode_chip_idx)
        except ValueError:
            self.visit_index = 0
        self._load_chip(self.mark_mode_chip_idx)
        self._update_edit_buttons()
        self._update_info_label()

    def _update_info_label(self):
        total_annots = self._total_annotation_count()
        if self.edit_tile_ids is not None:
            self.lbl_info.config(text=f"Edit | Tile {self.edit_index + 1}/{len(self.edit_tile_ids)} | Annotations: {total_annots}")
        else:
            if not self.windows:
                self.lbl_info.config(text=f"Annotations: {total_annots}")
                return
            if self.current_window_idx < 1:
                self.lbl_info.config(text=f"No tiles to show (all all-black?)  | Annotations: {total_annots}")
                return
            chip_idx = self.current_window_idx - 1
            tile_id = self._stable_tile_id(chip_idx)
            session_count = self.visit_index + 1
            self.lbl_info.config(text=f"{tile_id} / {len(self.windows)}  session: {session_count}  | Annotations: {total_annots}")

    def next_chip(self):
        """Save current annotations and load the next chip."""
        self._save_current_to_chip_labels()
        if self.edit_tile_ids is not None:
            self.edit_index += 1
            if self.edit_index >= len(self.edit_tile_ids):
                messagebox.showinfo("Edit done", "Reached last tile.")
                self.edit_index = len(self.edit_tile_ids) - 1
                return
            self._load_chip(self.edit_tile_ids[self.edit_index])
            self._update_info_label()
            return
        self._persist_tiles()
        self.visit_index += 1
        while self.visit_index < len(self.visit_order):
            if self._is_tile_all_black(self.visit_order[self.visit_index]):
                self.visit_index += 1
                continue
            if self._load_chip(self.visit_order[self.visit_index], skip_if_visually_black=True):
                self._update_info_label()
                return
            self.visit_index += 1
        messagebox.showinfo("Done", "No more chips!")
        self.finish()

    def _all_black_and_next(self):
        """Mark current tile as all_black, persist tiles, and advance to next (Mark mode only)."""
        if self.edit_tile_ids is not None:
            return
        self._save_current_to_chip_labels()
        chip_idx = self.current_window_idx - 1
        tid = self._stable_tile_id(chip_idx)
        if tid in self.tile_state:
            self.tile_state[tid]["all_black"] = True
        self._persist_tiles()
        self.visit_index += 1
        while self.visit_index < len(self.visit_order):
            if self._is_tile_all_black(self.visit_order[self.visit_index]):
                self.visit_index += 1
                continue
            if self._load_chip(self.visit_order[self.visit_index], skip_if_visually_black=True):
                self._update_info_label()
                return
            self.visit_index += 1
        messagebox.showinfo("Done", "No more chips!")
        self.finish()

    def prev_chip(self):
        """Save current annotations and load the previous chip."""
        self._save_current_to_chip_labels()
        if self.edit_tile_ids is not None:
            if self.edit_index <= 0:
                return
            self.edit_index -= 1
            self._load_chip(self.edit_tile_ids[self.edit_index])
            self._update_info_label()
            return
        self._persist_tiles()
        if self.visit_index <= 0:
            return
        self.visit_index -= 1
        while self.visit_index >= 0:
            if self._is_tile_all_black(self.visit_order[self.visit_index]):
                self.visit_index -= 1
                continue
            if self._load_chip(self.visit_order[self.visit_index], skip_if_visually_black=True):
                self._update_info_label()
                return
            self.visit_index -= 1
        self.visit_index = 0
        self._update_info_label()

    def _rect_at_point(self, x, y):
        """Return index of topmost rect containing (x, y), or None."""
        for i in range(len(self.rects) - 1, -1, -1):
            x0, y0, x1, y1 = self._rect_bounds(self.rects[i])
            if x0 <= x <= x1 and y0 <= y <= y1:
                return i
        return None

    # --- Mouse Events for Drawing Rectangles (center + drag out) ---
    def on_button_press(self, event):
        if event.state & 0x0001:  # Shift: pan
            self._on_pan_start(event)
            return
        x, y = self._event_to_tile(event)
        if event.state & 0x0004:  # Control key: start moving a box
            idx = self._rect_at_point(x, y)
            if idx is not None:
                x0, y0, x1, y1 = self._rect_bounds(self.rects[idx])
                self.moving_rect_idx = idx
                self.move_offset_x = x - x0
                self.move_offset_y = y - y0
            return
        if self.moving_rect_idx is not None:
            return
        self.start_x = x
        self.start_y = y
        self._live_rect_bounds = (x, y, x, y)
        self.current_rect_id = self.canvas.create_rectangle(
            *self._canvas_coords(x, y, x, y), outline=self._active_color(), width=2)

    def on_move_press(self, event):
        if self.panning:
            self._on_pan_motion(event)
            return
        cur_x, cur_y = self._event_to_tile(event)
        if self.moving_rect_idx is not None:
            idx = self.moving_rect_idx
            x0, y0, x1, y1 = self._rect_bounds(self.rects[idx])
            w, h = x1 - x0, y1 - y0
            new_x0 = cur_x - self.move_offset_x
            new_y0 = cur_y - self.move_offset_y
            new_x1 = new_x0 + w
            new_y1 = new_y0 + h
            clipped = self._clip_rect(new_x0, new_y0, new_x1, new_y1)
            if clipped is not None:
                new_x0, new_y0, new_x1, new_y1 = clipped
                self.canvas.coords(self.rect_ids[idx], *self._canvas_coords(new_x0, new_y0, new_x1, new_y1))
                r = self.rects[idx]
                cid = r[4] if len(r) >= 6 else 0
                cname = r[5] if len(r) >= 6 else self._active_class_name()
                self.rects[idx] = (new_x0, new_y0, new_x1, new_y1, cid, cname)
            return
        if self.current_rect_id is None:
            return
        x0 = min(cur_x, 2 * self.start_x - cur_x)
        y0 = min(cur_y, 2 * self.start_y - cur_y)
        x1 = max(cur_x, 2 * self.start_x - cur_x)
        y1 = max(cur_y, 2 * self.start_y - cur_y)
        clipped = self._clip_rect(x0, y0, x1, y1)
        if clipped is not None:
            x0, y0, x1, y1 = clipped
            self._live_rect_bounds = (x0, y0, x1, y1)
            self.canvas.coords(self.current_rect_id, *self._canvas_coords(x0, y0, x1, y1))

    def on_button_release(self, event):
        if self.panning:
            self._on_pan_end(event)
            return
        if self.moving_rect_idx is not None:
            self.moving_rect_idx = None
            return
        if self.current_rect_id is None:
            return
        cur_x, cur_y = self._event_to_tile(event)
        x0 = min(cur_x, 2 * self.start_x - cur_x)
        y0 = min(cur_y, 2 * self.start_y - cur_y)
        x1 = max(cur_x, 2 * self.start_x - cur_x)
        y1 = max(cur_y, 2 * self.start_y - cur_y)
        clipped = self._clip_rect(x0, y0, x1, y1)
        if clipped is not None:
            self.rects.append(clipped + (self.active_class_id, self._active_class_name()))
            self.rect_ids.append(self.current_rect_id)
        else:
            self.canvas.delete(self.current_rect_id)
        self.current_rect_id = None
        self._live_rect_bounds = None
        self._update_right_sidebar()

    def on_escape(self, event=None):
        if self.current_rect_id is not None:
            self.canvas.delete(self.current_rect_id)
            self.current_rect_id = None

    def on_right_click(self, event):
        x, y = self._event_to_tile(event)
        idx = self._rect_at_point(x, y)
        if idx is None:
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Delete box")
        dlg.transient(self.root)
        result = [None]

        def confirm():
            result[0] = True
            dlg.after(1, dlg.destroy)

        def cancel():
            result[0] = False
            dlg.after(1, dlg.destroy)

        tk.Label(dlg, text="Delete this box? (Enter=Yes, Esc=Cancel)").pack(padx=20, pady=10)
        def on_confirm(e):
            confirm()
            return "break"
        def on_cancel(e):
            cancel()
            return "break"
        dlg.bind("<Return>", on_confirm)
        dlg.bind("<Escape>", on_cancel)
        dlg.protocol("WM_DELETE_WINDOW", cancel)
        dlg.update_idletasks()
        dlg.focus_set()
        dlg.after(10, dlg.grab_set)
        dlg.wait_window()
        if result[0]:
            self.canvas.delete(self.rect_ids[idx])
            del self.rect_ids[idx]
            del self.rects[idx]
            self._save_current_to_chip_labels()
            self._update_right_sidebar()
        self._update_info_label()

    def _reset_tiles_annotations(self):
        """Show confirmation modal; on Enter delete tiles, labels, and parameters files."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Reset tiles/annotations")
        dlg.transient(self.root)
        result = [None]

        def confirm():
            result[0] = True
            dlg.destroy()

        def cancel():
            result[0] = False
            dlg.destroy()

        tk.Label(dlg, text="Delete tiles, labels, and parameters files? (Enter=Yes, Esc=Cancel)").pack(padx=20, pady=10)
        dlg.bind("<Return>", lambda e: confirm())
        dlg.bind("<Escape>", lambda e: cancel())
        dlg.protocol("WM_DELETE_WINDOW", cancel)
        dlg.update_idletasks()
        dlg.focus_set()
        dlg.after(10, dlg.grab_set)
        dlg.wait_window()
        if result[0]:
            for path in (self.tiles_filename, self.output_filename, self.param_filename):
                if os.path.exists(path):
                    try:
                        os.remove(path)
                        print(f"Deleted {path}")
                    except OSError as e:
                        messagebox.showerror("Error", f"Could not delete {path}: {e}")
            self.root.quit()

    # --- Geospatial Logic ---
    def _build_all_labels_from_chip_labels(self):
        """Convert all chip_labels (pixel coords) to all_labels (geospatial) using each chip's transform."""
        self.all_labels = []
        with rasterio.open(self.raster_path) as src:
            for chip_idx in sorted(self.chip_labels.keys()):
                window = self.windows[chip_idx]
                transform = src.window_transform(window)
                for r in self.chip_labels[chip_idx]:
                    x1_pix, y1_pix, x2_pix, y2_pix = r[0], r[1], r[2], r[3]
                    class_id, class_name = self._rect_class(r)
                    xmin_pix, xmax_pix = sorted([x1_pix, x2_pix])
                    ymin_pix, ymax_pix = sorted([y1_pix, y2_pix])
                    tl_x, tl_y = transform * (xmin_pix, ymin_pix)
                    br_x, br_y = transform * (xmax_pix, ymax_pix)
                    geom = box(min(tl_x, br_x), min(tl_y, br_y), max(tl_x, br_x), max(tl_y, br_y))
                    self.all_labels.append({
                        'geometry': geom,
                        'class_id': class_id,
                        'class_name': class_name,
                        'tile_id': self._stable_tile_id(chip_idx),
                    })

    def _build_tiles(self):
        """Build list of tile features for all windows: id, viewed, all_black, is_empty, num_annots, transform."""
        tiles = []
        with rasterio.open(self.raster_path) as src:
            for chip_idx in range(len(self.windows)):
                window = self.windows[chip_idx]
                bounds = src.window_bounds(window)
                left, bottom, right, top = bounds
                geom = box(left, bottom, right, top)
                t = src.window_transform(window)
                transform_list = [t.a, t.b, t.c, t.d, t.e, t.f]
                tid = self._stable_tile_id(chip_idx)
                state = self.tile_state.get(tid, {"viewed": False, "all_black": False})
                rects = self.chip_labels.get(chip_idx, [])
                num_annots = len(rects)
                session_dir = os.path.dirname(self.output_filename)
                chip_path = os.path.abspath(os.path.join(session_dir, "tiles", f"{tid}.webp"))
                tiles.append({
                    "geometry": geom,
                    "id": tid,
                    "viewed": state.get("viewed", False),
                    "all_black": state.get("all_black", False),
                    "is_empty": num_annots == 0,
                    "num_annots": num_annots,
                    "chip_path": chip_path,
                    "transform": transform_list,
                })
        return tiles

    def finish(self):
        self._save_current_to_chip_labels()
        self._build_all_labels_from_chip_labels()
        all_labels = self.all_labels
        if not all_labels:
            print("No labels to save.")
        else:
            print(f"Saving {len(all_labels)} labels to {self.output_filename}...")
            for d in all_labels:
                if "tile_id" not in d:
                    d["tile_id"] = None
            gdf = gpd.GeoDataFrame(all_labels, crs=self.crs)
            gdf.to_file(self.output_filename, driver="GeoJSON")
        self._persist_tiles()
        present_classes = sorted(set(d.get("class_id") for d in all_labels if d.get("class_id") is not None))
        if self.param_filename and os.path.exists(self.param_filename):
            try:
                with open(self.param_filename) as f:
                    params = json.load(f)
                timestamps = params.get("TIMESTAMPS", [])
                if timestamps:
                    end_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    timestamps[-1][1] = end_ts
                    params["TIMESTAMPS"] = timestamps
                params["PRESENT_CLASSES"] = present_classes
                with open(self.param_filename, "w") as f:
                    json.dump(params, f)
            except Exception as e:
                print(f"Could not update session timestamps: {e}")
        print("Done!")
        print(self.session_id)
        self.root.quit()

# --- Run the App ---
if __name__ == "__main__":
    import argparse
    import uuid
    import shutil

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default="config.yaml")
    parser.add_argument("--raster_file", type=str, default="/home/aerotract/2software/sample_inputs/seedlings/109_SM31_ortho.tif")
    parser.add_argument("--plots_file", type=str, default="/home/aerotract/2software/sample_inputs/seedlings/plots.geojson")
    parser.add_argument("--chip_size", type=int, default=640)
    parser.add_argument("--overlap", type=float, default=0.20)
    parser.add_argument("--sessions_dir", type=str, default="/home/aerotract/2software/sample_data_lake/sessions/")
    parser.add_argument("--session_id", type=str, default="")
    args = parser.parse_args()

    sessions_dir = os.path.abspath(args.sessions_dir)
    os.makedirs(sessions_dir, exist_ok=True)
    session_id = None

    if not args.session_id:
        session_id = str(uuid.uuid4())
        session_dir = os.path.join(sessions_dir, session_id)
        os.makedirs(session_dir, exist_ok=True)
        LABELS_FILENAME = os.path.abspath(os.path.join(session_dir, "labels.geojson"))
        TILES_FILENAME = os.path.abspath(os.path.join(session_dir, "tiles.geojson"))
        PARAM_FILENAME = os.path.abspath(os.path.join(session_dir, "parameters.json"))
        PLOTS_FILENAME = os.path.abspath(os.path.join(session_dir, "plots.geojson"))
        if os.path.exists(args.plots_file):
            shutil.copy2(args.plots_file, PLOTS_FILENAME)
        start_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        PARAMETERS = {
            "RASTER_FILE": os.path.abspath(args.raster_file),
            "PLOTS_FILE": PLOTS_FILENAME,
            "CHIP_SIZE": args.chip_size,
            "OVERLAP": args.overlap,
            "LABELS_FILENAME": LABELS_FILENAME,
            "TILES_FILENAME": TILES_FILENAME,
            "TIMESTAMPS": [[start_ts, start_ts]],
        }
        with open(PARAM_FILENAME, "w") as f:
            json.dump(PARAMETERS, f)
        RASTER_FILE = PARAMETERS["RASTER_FILE"]
        CHIP_SIZE = args.chip_size
        OVERLAP = args.overlap
    else:
        session_dir = os.path.join(sessions_dir, args.session_id)
        session_id = args.session_id
        if not os.path.isdir(session_dir):
            print(f"Session directory not found: {session_dir}")
            raise SystemExit(1)
        PARAM_FILENAME = os.path.join(session_dir, "parameters.json")
        if not os.path.exists(PARAM_FILENAME):
            print(f"Parameters file not found: {PARAM_FILENAME}")
            raise SystemExit(1)
        with open(PARAM_FILENAME) as f:
            PARAMETERS = json.load(f)
        if "TIMESTAMPS" not in PARAMETERS and "START_TIME" in PARAMETERS:
            st = PARAMETERS["START_TIME"]
            PARAMETERS["TIMESTAMPS"] = [[st, st]]
        timestamps = PARAMETERS.get("TIMESTAMPS", [])
        start_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        timestamps.append([start_ts, start_ts])
        PARAMETERS["TIMESTAMPS"] = timestamps
        with open(PARAM_FILENAME, "w") as f:
            json.dump(PARAMETERS, f)
        RASTER_FILE = PARAMETERS.get("RASTER_FILE")
        PLOTS_FILENAME = PARAMETERS.get("PLOTS_FILE") or os.path.abspath(os.path.join(session_dir, "plots.geojson"))
        LABELS_FILENAME = PARAMETERS.get("LABELS_FILENAME") or os.path.abspath(os.path.join(session_dir, "labels.geojson"))
        TILES_FILENAME = PARAMETERS.get("TILES_FILENAME") or os.path.abspath(os.path.join(session_dir, "tiles.geojson"))
        CHIP_SIZE = PARAMETERS.get("CHIP_SIZE", 640)
        OVERLAP = PARAMETERS.get("OVERLAP", 0.20)

    root = tk.Tk()
    if not os.path.exists(RASTER_FILE):
        # RASTER_FILE = filedialog.askopenfilename(title="Select Raster File")
        RASTER_FILE = "/home/aerotract/2software/sample_inputs/seedlings/109_SM31_ortho.tif"
        PLOTS_FILENAME = "/home/aerotract/2software/sample_inputs/seedlings/plots.geojson"
    if RASTER_FILE:
        app = ChipLabeler(
            root, RASTER_FILE, chip_size=CHIP_SIZE, overlap=OVERLAP,
            output_filename=LABELS_FILENAME, tiles_filename=TILES_FILENAME,
            param_filename=PARAM_FILENAME, plots_filename=PLOTS_FILENAME,
            session_id=session_id,
        )
        root.mainloop()
