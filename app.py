#!/usr/bin/env python3
"""
3D Print Quoting GUI — local web app
Run: python3 app.py
Then open: http://localhost:5111
"""

import configparser
import datetime
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import traceback
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_file

PRUSASLICER  = os.environ.get("PRUSASLICER_PATH",  "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer")
ORCASLICER   = os.environ.get("ORCASLICER_PATH",   "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer")
PRUSA_VENDOR_DIR = os.environ.get("PRUSA_VENDOR_DIR", "/Applications/PrusaSlicer.app/Contents/Resources/profiles")
PRUSA_USER_DIR   = os.environ.get("PRUSA_USER_DIR",   os.path.expanduser("~/Library/Application Support/PrusaSlicer"))
PORT = int(os.environ.get("PORT", 5111))

# Persistent scratch dir for 3MF exports (survives request lifecycle)
EXPORT_DIR = os.path.join(tempfile.gettempdir(), "prusaquoting_exports")
os.makedirs(EXPORT_DIR, exist_ok=True)

# Last-job state for 3MF export (in-memory, single-user tool)
_last_job = {}   # {input_path, printer, print_prof, filament, infill, layer_h, walls, supports, size_val, size_mode, filename}
_jobs     = {}   # job_id -> {gcode_paths, filename}  (last 50 jobs)

# In-memory error log (last 100 entries)
_error_log = []

# In-memory progress tracking: pid -> list of {label, status, detail}
_progress = {}

def _emit(pid, label, status, detail=None):
    """Emit a progress step. If a step with the same label exists, update it in-place."""
    if not pid:
        return
    steps = _progress.setdefault(pid, [])
    for s in reversed(steps):
        if s["label"] == label:
            s["status"] = status
            s["detail"] = detail or ""
            return
    steps.append({"label": label, "status": status, "detail": detail or ""})

def _log_error(endpoint, error, tb=None, params=None):
    _error_log.append({
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "endpoint":  endpoint,
        "error":     str(error),
        "type":      type(error).__name__,
        "traceback": tb or traceback.format_exc(),
        "params":    params,
    })
    if len(_error_log) > 100:
        _error_log.pop(0)

app = Flask(__name__)

@app.errorhandler(Exception)
def handle_unhandled_exception(e):
    """Catch all unhandled exceptions, log them, and return JSON instead of HTML."""
    tb = traceback.format_exc()
    _log_error(request.path, e, tb, {"method": request.method})
    return jsonify({"error": str(e), "type": type(e).__name__}), 500


# ── Slicer helpers ────────────────────────────────────────────────────────────

def run_slicer(*args):
    return subprocess.run([PRUSASLICER] + list(args), capture_output=True, text=True)

def run_orca(*args):
    return subprocess.run([ORCASLICER] + list(args), capture_output=True, text=True)


def get_printer_profiles():
    """Return list of {name, path} dicts. Bundled profiles have path=None."""
    result = run_slicer("--query-printer-models")
    idx = result.stdout.find("{")
    profiles = []
    if idx != -1:
        data = json.loads(result.stdout[idx:])
        for model in data.get("printer_models", []):
            for variant in model.get("variants", []):
                for p in variant.get("printer_profiles", []):
                    profiles.append({"name": p["name"], "path": None})

    # Also include user printer .ini files (e.g. Klipper profiles with real machine limits)
    user_printer_dir = os.path.join(PRUSA_USER_DIR, "printer")
    bundled_names = {p["name"] for p in profiles}
    for ini_path in sorted(glob.glob(os.path.join(user_printer_dir, "*.ini"))):
        name = Path(ini_path).stem
        if name not in bundled_names:
            profiles.append({"name": name, "path": ini_path})

    return profiles


# Map profile name → file path (for user profiles)
_USER_PRINTER_PATHS = {}

def _build_user_printer_map():
    user_printer_dir = os.path.join(PRUSA_USER_DIR, "printer")
    for ini_path in glob.glob(os.path.join(user_printer_dir, "*.ini")):
        name = Path(ini_path).stem
        _USER_PRINTER_PATHS[name] = ini_path

_build_user_printer_map()


def get_print_filament_profiles(printer_profile):
    result = run_slicer("--query-print-filament-profiles", "--printer-profile", printer_profile)
    idx = result.stdout.find("{")
    if idx == -1:
        return [], []
    data = json.loads(result.stdout[idx:])
    print_profiles = [p["name"] for p in data.get("print_profiles", [])]
    filament_set, seen = [], set()
    for p in data.get("print_profiles", []):
        for f in p.get("user_filament_profiles", []) + p.get("filament_profiles", []):
            if f not in seen:
                filament_set.append(f)
                seen.add(f)
    return print_profiles, filament_set


# ── Build volume detection ─────────────────────────────────────────────────────

def _load_ini_sections(path):
    """Return dict of {section_name: {key: value}} from a PrusaSlicer .ini file."""
    sections = {}
    current = None
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    current = line[1:-1]
                    sections[current] = {}
                elif current and "=" in line and not line.startswith(";"):
                    k, _, v = line.partition("=")
                    sections[current][k.strip()] = v.strip()
    except OSError:
        pass
    return sections


def _all_printer_sections():
    """Collect all [printer:Name] sections across bundled + user INI files."""
    all_sections = {}
    paths = (
        glob.glob(os.path.join(PRUSA_VENDOR_DIR, "*.ini")) +
        glob.glob(os.path.join(PRUSA_USER_DIR, "printer", "*.ini"))
    )
    for path in paths:
        for sec, kv in _load_ini_sections(path).items():
            all_sections[sec] = kv
    return all_sections


def _resolve_key(sections, section_name, key, depth=0):
    """Walk the inherits chain to find a config key value."""
    if depth > 8:
        return None
    kv = sections.get(section_name, {})
    if key in kv:
        return kv[key]
    inherits_str = kv.get("inherits", "")
    for parent in re.split(r"[;,]", inherits_str):
        parent = parent.strip().strip("*")
        if not parent:
            continue
        # Try progressively with/without printer: prefix and asterisk-wrapped names
        for candidate in [
            parent,
            f"printer:{parent}",
            f"*{parent}*",
            f"printer:*{parent}*",
        ]:
            val = _resolve_key(sections, candidate, key, depth + 1)
            if val is not None:
                return val
    return None


def get_build_volume(printer_profile_name):
    """Return {'x': mm, 'y': mm, 'z': mm} for the printer's build volume."""
    sections = _all_printer_sections()
    section_name = f"printer:{printer_profile_name}"

    bed_shape_str = _resolve_key(sections, section_name, "bed_shape")
    max_z_str     = _resolve_key(sections, section_name, "max_print_height")

    # Parse bed_shape: "0x0,300x0,300x300,0x300"
    bed_x, bed_y = 300, 300  # safe defaults
    if bed_shape_str:
        coords = []
        for pt in bed_shape_str.split(","):
            pt = pt.strip()
            if "x" in pt:
                px, py = pt.split("x", 1)
                try:
                    coords.append((float(px), float(py)))
                except ValueError:
                    pass
        if coords:
            xs = [c[0] for c in coords]
            ys = [c[1] for c in coords]
            bed_x = max(xs) - min(xs)
            bed_y = max(ys) - min(ys)

    max_z = 300
    if max_z_str:
        try:
            max_z = float(max_z_str)
        except ValueError:
            pass

    return {"x": bed_x, "y": bed_y, "z": max_z}


# ── Auto-orient (OrcaSlicer) ──────────────────────────────────────────────────

def auto_orient(input_path, tmpdir):
    """
    Use OrcaSlicer CLI to auto-orient the model.
    Returns (oriented_stl_path, orientation_note).
    Falls back to input_path on failure.
    """
    out_root = os.path.join(tmpdir, "oriented")
    os.makedirs(os.path.join(out_root, "stl"), exist_ok=True)

    result = run_orca(
        "--orient", "1",
        "--export-stl",
        "--outputdir", out_root,
        input_path,
    )

    # Parse best orientation from stdout
    best_line = ""
    for line in result.stdout.splitlines():
        if line.startswith("best:"):
            best_line = line.strip()
            break

    stl_files = glob.glob(os.path.join(out_root, "stl", "*.stl"))
    if stl_files:
        note = best_line if best_line else "auto-oriented"
        return stl_files[0], note
    return input_path, "orient failed (using original)"


# ── Build-volume overflow detection & splitting (trimesh) ─────────────────────

def get_mesh_bounds(stl_path):
    """Return (size_x, size_y, size_z) of the model.
    Tries PrusaSlicer --info first, falls back to trimesh for formats it can't parse."""
    result = run_slicer("--info", stl_path)
    sx = sy = sz = None
    for line in result.stdout.splitlines():
        if line.startswith("size_x"):
            try: sx = float(line.split("=")[1])
            except ValueError: pass
        elif line.startswith("size_y"):
            try: sy = float(line.split("=")[1])
            except ValueError: pass
        elif line.startswith("size_z"):
            try: sz = float(line.split("=")[1])
            except ValueError: pass
    if sx is not None:
        return sx, sy, sz
    # PrusaSlicer --info failed (e.g. OBJ with excessive line length) — try trimesh
    try:
        import trimesh as tm
        mesh = tm.load(stl_path, force="mesh")
        ext = mesh.extents
        return float(ext[0]), float(ext[1]), float(ext[2])
    except Exception:
        pass
    return None, None, None


def _split_mesh_along_axis(mesh, axis, cut_pos, piece_dir, base_name, piece_index):
    """
    Cut mesh at cut_pos along axis (0=X, 1=Y, 2=Z).
    Returns list of (trimesh, label) tuples for each non-empty half.
    """
    import trimesh as tm
    normal_pos = [0, 0, 0]
    normal_neg = [0, 0, 0]
    origin     = [0, 0, 0]
    normal_pos[axis] =  1
    normal_neg[axis] = -1
    origin[axis] = cut_pos

    axis_name = ["X", "Y", "Z"][axis]
    results = []
    for normal, label in [(normal_pos, f"pos{axis_name}"), (normal_neg, f"neg{axis_name}")]:
        try:
            half = tm.intersections.slice_mesh_plane(mesh, normal, origin, cap=True)
            if half is not None and len(half.faces) > 0:
                results.append((half, label))
        except Exception:
            pass
    return results


def _translate_to_origin(mesh):
    """Translate mesh so its bounding box minimum sits at the origin (Z=0 on bed)."""
    import numpy as np
    mesh.apply_translation(-mesh.bounds[0])


def split_if_needed(stl_path, build_vol, tmpdir, do_orient=False):
    """
    Split model into pieces that fit the build volume.
    Cuts at the CENTER of the model along the largest overflowing axis, recursively.
    If do_orient is True, auto-orients each final piece with OrcaSlicer.
    Returns list of (stl_path, label) tuples.
    """
    try:
        import trimesh as tm
    except ImportError:
        return [(stl_path, "whole")]

    try:
        mesh = tm.load(stl_path, force="mesh")
    except Exception:
        return [(stl_path, "whole")]
    bv   = build_vol

    def mesh_fits(m):
        e = m.extents
        return e[0] <= bv["x"] and e[1] <= bv["y"] and e[2] <= bv["z"]

    if mesh_fits(mesh):
        return [(stl_path, "whole")]

    piece_dir = os.path.join(tmpdir, "pieces")
    os.makedirs(piece_dir, exist_ok=True)
    base_name = Path(stl_path).stem

    def try_split(mesh_in, depth=0):
        if mesh_fits(mesh_in) or depth > 6:
            out_path = os.path.join(piece_dir, f"{base_name}_d{depth}_{id(mesh_in)}.stl")
            _translate_to_origin(mesh_in)
            mesh_in.export(out_path)
            return [out_path]

        # Find the largest overflowing axis and cut at its center
        ext = mesh_in.extents
        overflow = sorted(
            [(ext[ax] - lim, ax) for ax, lim in [(0, bv["x"]), (1, bv["y"]), (2, bv["z"])]
             if ext[ax] > lim + 0.5],
            reverse=True
        )
        if not overflow:
            out_path = os.path.join(piece_dir, f"{base_name}_d{depth}_{id(mesh_in)}.stl")
            _translate_to_origin(mesh_in)
            mesh_in.export(out_path)
            return [out_path]

        _, ax = overflow[0]
        # Cut at the geometric center of this mesh along the overflowing axis
        cut_pos = (mesh_in.bounds[0][ax] + mesh_in.bounds[1][ax]) / 2.0

        halves = _split_mesh_along_axis(mesh_in, ax, cut_pos, piece_dir, base_name, depth)
        if len(halves) < 2:
            out_path = os.path.join(piece_dir, f"{base_name}_d{depth}_{id(mesh_in)}.stl")
            _translate_to_origin(mesh_in)
            mesh_in.export(out_path)
            return [out_path]

        results = []
        for half_mesh, _ in halves:
            _translate_to_origin(half_mesh)
            results.extend(try_split(half_mesh, depth + 1))
        return results

    raw_paths = try_split(mesh)
    n = len(raw_paths)

    # Auto-orient each piece if requested
    final_pieces = []
    for i, path in enumerate(raw_paths):
        label = f"piece {i+1} of {n}"
        if do_orient:
            oriented_path, _ = auto_orient(path, tmpdir)
            final_pieces.append((oriented_path, label))
        else:
            final_pieces.append((path, label))

    return final_pieces


# ── Sizing args ───────────────────────────────────────────────────────────────

def parse_size(size_str, mode):
    if mode == "fit":
        parts = [float(x) for x in size_str.replace(" ", "").split(",")]
        if len(parts) == 2:
            parts.append(9999)
        return ["--scale-to-fit", ",".join(str(p) for p in parts)]
    else:
        s = str(size_str).strip().rstrip("%")
        return ["--scale", s]


# ── Gcode parsing ─────────────────────────────────────────────────────────────

def parse_gcode_stats(gcode_path):
    stats = {
        "time_normal": None, "time_silent": None,
        "filament_mm": [], "filament_cm3": [], "filament_g": [], "filament_cost": [],
    }
    time_re = re.compile(r";\s*estimated printing time \((\w+(?: \w+)?)\s*mode\)\s*=\s*(.+)")
    mm_re   = re.compile(r";\s*filament used \[mm\]\s*=\s*(.+)")
    cm3_re  = re.compile(r";\s*filament used \[cm3\]\s*=\s*(.+)")
    g_re    = re.compile(r";\s*filament used \[g\]\s*=\s*(.+)")
    cost_re = re.compile(r";\s*filament cost\s*=\s*(.+)")
    with open(gcode_path) as f:
        for line in f:
            m = time_re.match(line)
            if m:
                mode_name, t = m.group(1).strip().lower(), m.group(2).strip()
                if "normal" in mode_name:
                    stats["time_normal"] = t
                elif "silent" in mode_name:
                    stats["time_silent"] = t
                else:
                    stats["time_normal"] = t
                continue
            for pattern, key in [(mm_re,"filament_mm"),(cm3_re,"filament_cm3"),
                                  (g_re,"filament_g"),(cost_re,"filament_cost")]:
                m = pattern.match(line)
                if m:
                    stats[key] = [float(x) for x in m.group(1).split(",")]
                    break
    return stats


def time_to_minutes(t):
    total = 0.0
    for val, unit in re.findall(r"(\d+)\s*([dhms])", t):
        val = int(val)
        if unit == "d":   total += val * 1440
        elif unit == "h": total += val * 60
        elif unit == "m": total += val
        elif unit == "s": total += val / 60
    return total


def mins_to_str(mins):
    total_s = int(mins * 60)
    d, rem = divmod(total_s, 86400)
    h, rem = divmod(rem, 3600)
    m, s   = divmod(rem, 60)
    if d:   return f"{d}d {h}h {m}m"
    if h:   return f"{h}h {m}m"
    return f"{m}m {s}s"


# ── Slice one piece ───────────────────────────────────────────────────────────

def _closest_bundled_profile(user_name, bundled_names):
    """Find the best-matching bundled profile for a user profile name."""
    # Try progressively broader token matching
    tokens = re.findall(r'[A-Za-z0-9]+', user_name)
    best, best_score = None, 0
    for name in bundled_names:
        score = sum(1 for t in tokens if t.lower() in name.lower())
        if score > best_score:
            best, best_score = name, score
    return best


_BUNDLED_PRINTER_NAMES = None

def _get_bundled_names():
    global _BUNDLED_PRINTER_NAMES
    if _BUNDLED_PRINTER_NAMES is None:
        result = run_slicer("--query-printer-models")
        idx = result.stdout.find("{")
        names = []
        if idx != -1:
            data = json.loads(result.stdout[idx:])
            for model in data.get("printer_models", []):
                for variant in model.get("variants", []):
                    for p in variant.get("printer_profiles", []):
                        names.append(p["name"])
        _BUNDLED_PRINTER_NAMES = names
    return _BUNDLED_PRINTER_NAMES


def _profile_flags(printer, print_prof, filament):
    """Return CLI flags for named profiles.
    For user printer inis: uses closest bundled profile + --load to override machine limits.
    """
    cmd = []
    if printer:
        user_path = _USER_PRINTER_PATHS.get(printer)
        if user_path and os.path.exists(user_path):
            # Need a bundled --printer-profile to satisfy PrusaSlicer,
            # then --load the user ini to override machine limits on top
            bundled = _closest_bundled_profile(printer, _get_bundled_names())
            if bundled:
                cmd += ["--printer-profile", bundled]
            cmd += ["--load", user_path]
        else:
            cmd += ["--printer-profile", printer]
    if print_prof: cmd += ["--print-profile",   print_prof]
    if filament:   cmd += ["--material-profile", filament]
    return cmd


def _override_flags(infill, layer_h, walls, supports):
    """Return CLI flags for print overrides."""
    cmd = []
    if infill:  cmd += ["--fill-density", infill if infill.endswith("%") else infill + "%"]
    if layer_h: cmd += ["--layer-height", layer_h]
    if walls:   cmd += ["--perimeters",   walls]
    if supports and supports != "none":
        cmd += ["--support-material", "--support-material-auto",
                "--support-material-style", supports]
    elif supports == "none":
        cmd += ["--no-support-material"]
    return cmd


MACHINE_LIMIT_KEYS = [
    "machine_limits_usage",
    "machine_max_acceleration_e", "machine_max_acceleration_extruding",
    "machine_max_acceleration_retracting", "machine_max_acceleration_travel",
    "machine_max_acceleration_x", "machine_max_acceleration_y", "machine_max_acceleration_z",
    "machine_max_feedrate_e", "machine_max_feedrate_x",
    "machine_max_feedrate_y", "machine_max_feedrate_z",
    "machine_min_extruding_rate", "machine_min_travel_rate",
]


def _patch_machine_limits(config_path, user_printer_path):
    """Overwrite machine limit keys in config_path with values from user_printer_path."""
    # Read user printer ini
    user_limits = {}
    with open(user_printer_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "=" in line and not line.startswith(";") and not line.startswith("["):
                k, _, v = line.partition("=")
                k = k.strip()
                if k in MACHINE_LIMIT_KEYS:
                    user_limits[k] = v.strip()
    if not user_limits:
        return

    # Read resolved config, replace matching keys
    with open(config_path, encoding="utf-8") as f:
        lines = f.readlines()

    patched = []
    replaced = set()
    for line in lines:
        if "=" in line and not line.startswith(";"):
            k = line.split("=", 1)[0].strip()
            if k in user_limits:
                patched.append(f"{k} = {user_limits[k]}\n")
                replaced.add(k)
                continue
        patched.append(line)

    # Append any keys that weren't already in the resolved config
    for k, v in user_limits.items():
        if k not in replaced:
            patched.append(f"{k} = {v}\n")

    with open(config_path, "w", encoding="utf-8") as f:
        f.writelines(patched)


def build_machine_limits_ini(tmpdir, printer):
    """
    If the selected printer is a user profile (e.g. Klipper), write a minimal
    .ini containing only the machine limit keys from that profile.
    Returns path to the ini, or None if not applicable.
    """
    user_path = _USER_PRINTER_PATHS.get(printer)
    if not user_path or not os.path.exists(user_path):
        return None

    user_limits = {}
    with open(user_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "=" in line and not line.startswith(";") and not line.startswith("["):
                k, _, v = line.partition("=")
                k = k.strip()
                if k in MACHINE_LIMIT_KEYS:
                    user_limits[k] = v.strip()

    if not user_limits:
        return None

    out_path = os.path.join(tmpdir, "machine_limits.ini")
    with open(out_path, "w", encoding="utf-8") as f:
        for k, v in user_limits.items():
            f.write(f"{k} = {v}\n")
    return out_path


def slice_piece(stl_path, tmpdir, piece_idx, printer, print_prof, filament,
                size_val, size_mode, infill, layer_h, walls, supports,
                machine_limits_ini=None, persistent_gcode_path=None):
    """Slice a single STL and return stats dict or raise RuntimeError."""
    out_gcode = os.path.join(tmpdir, f"piece_{piece_idx}.gcode")

    # Always use named profile flags so PrusaSlicer resolves inheritance correctly
    # (layer_height, speeds, etc. come from the profile, not a pre-saved flat config)
    cmd = _profile_flags(printer, print_prof, filament)
    cmd += _override_flags(infill, layer_h, walls, supports)

    # Load machine limits ini last so it overrides the named profile's limits
    if machine_limits_ini and os.path.exists(machine_limits_ini):
        cmd += ["--load", machine_limits_ini]

    if size_val:
        cmd += parse_size(size_val, size_mode)
    cmd += ["--export-gcode", "--output", out_gcode, stl_path]

    result = run_slicer(*cmd)
    if result.returncode != 0 or not os.path.exists(out_gcode):
        errors = [l for l in result.stderr.splitlines() if "error" in l.lower()]
        raise RuntimeError("\n".join(errors) or result.stderr[:400])

    # Persist gcode for download if requested (piece 0 only, for single-piece prints)
    if persistent_gcode_path:
        import shutil as _shutil
        _shutil.copy2(out_gcode, persistent_gcode_path)

    return parse_gcode_stats(out_gcode)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/printers")
def api_printers():
    return jsonify([p["name"] for p in get_printer_profiles()])


@app.route("/api/profiles")
def api_profiles():
    printer = request.args.get("printer", "")
    if not printer:
        return jsonify({"print_profiles": [], "filament_profiles": []})
    pp, fp = get_print_filament_profiles(printer)
    return jsonify({"print_profiles": pp, "filament_profiles": fp})


PRESETS_FILE = os.environ.get("PRESETS_FILE", os.path.expanduser("~/.prusaquoting_presets.json"))
PRESET_KEYS  = ["printer", "print_profile", "filament", "infill",
                "layer_height", "walls", "supports",
                "cost_per_kg", "hourly_rate", "markup", "farm_size"]

def _load_presets():
    try:
        with open(PRESETS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

def _save_presets(data):
    with open(PRESETS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


@app.route("/api/presets", methods=["GET"])
def api_presets_get():
    return jsonify(_load_presets())

@app.route("/api/presets", methods=["POST"])
def api_presets_save():
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    presets = _load_presets()
    presets[name] = {k: body.get(k, "") for k in PRESET_KEYS}
    _save_presets(presets)
    return jsonify({"ok": True, "name": name})

@app.route("/api/presets/<name>", methods=["DELETE"])
def api_presets_delete(name):
    presets = _load_presets()
    presets.pop(name, None)
    _save_presets(presets)
    return jsonify({"ok": True})


@app.route("/api/debug_log", methods=["GET"])
def api_debug_log_get():
    return jsonify(list(reversed(_error_log)))

@app.route("/api/debug_log", methods=["DELETE"])
def api_debug_log_clear():
    _error_log.clear()
    return jsonify({"ok": True})


@app.route("/api/progress/<pid>", methods=["GET"])
def api_progress_get(pid):
    return jsonify(_progress.get(pid, []))

@app.route("/api/progress/<pid>", methods=["DELETE"])
def api_progress_delete(pid):
    _progress.pop(pid, None)
    return jsonify({"ok": True})


@app.route("/api/check_size", methods=["POST"])
def api_check_size():
    """Quick bounding-box + mesh health check — no slicing."""
    import shutil, tempfile
    file    = request.files.get("file")
    printer = request.form.get("printer", "")
    if not file:
        return jsonify({"error": "No file"}), 400
    suffix = Path(file.filename).suffix.lower()
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, "check" + suffix)
        file.save(input_path)
        sx, sy, sz = get_mesh_bounds(input_path)

        # Mesh health checks via trimesh (best-effort)
        mesh_warnings = []
        file_size = os.path.getsize(input_path)
        if file_size > 100 * 1024 * 1024:
            mesh_warnings.append(f"Large file ({file_size / 1024 / 1024:.0f} MB) — upload and slicing will be slow.")
        try:
            import trimesh as tm
            import numpy as np
            mesh = tm.load(input_path, force="mesh")
            if mesh.is_empty or len(mesh.faces) == 0:
                mesh_warnings.append("Empty mesh — file may be corrupted or contain no geometry.")
            else:
                # Watertightness
                if not mesh.is_watertight:
                    mesh_warnings.append("Mesh is not watertight (open holes detected) — may cause slicing errors or missing surfaces.")

                # Winding / normals consistency
                if not mesh.is_winding_consistent:
                    mesh_warnings.append("Inconsistent face normals — some faces may be inverted, causing surfaces to appear hollow or missing.")

                # Geometry below build plate
                if mesh.bounds is not None and mesh.bounds[0][2] < -0.01:
                    mesh_warnings.append(f"Geometry below Z=0 ({mesh.bounds[0][2]:.1f} mm) — part of the model will be cut off by the build plate.")

                # Very small model (possible wrong units)
                max_ext = float(mesh.extents.max())
                if max_ext < 5.0:
                    mesh_warnings.append(f"Model is very small (largest dimension: {max_ext:.2f} mm) — check units (may need to scale from inches or cm).")

                # Thin features
                min_ext = float(mesh.extents.min())
                if min_ext < 0.5:
                    mesh_warnings.append(f"Extremely thin in one axis ({min_ext:.2f} mm) — likely unprintable at this scale.")
                elif min_ext < 2.0:
                    mesh_warnings.append(f"Very thin wall ({min_ext:.1f} mm) — check wall thickness against nozzle diameter.")

                # High aspect ratio (tipping risk)
                if min_ext > 0:
                    ratio = max_ext / min_ext
                    if ratio > 15:
                        mesh_warnings.append(f"High aspect ratio ({ratio:.0f}:1) — tall or narrow models risk tipping mid-print; consider a brim or supports.")

                # Multiple separate bodies
                try:
                    bodies = mesh.split(only_watertight=False)
                    n = len(bodies)
                    if n > 1:
                        mesh_warnings.append(f"{n} separate bodies in file — each will be placed individually; verify all fit on the build plate.")
                except Exception:
                    pass

                # Degenerate (zero-area) faces
                try:
                    degenerate = int((mesh.area_faces < 1e-6).sum())
                    if degenerate > 100:
                        mesh_warnings.append(f"{degenerate:,} degenerate (zero-area) faces — mesh may be corrupted; consider repair before slicing.")
                except Exception:
                    pass

                # High polygon count
                face_count = len(mesh.faces)
                if face_count > 2_000_000:
                    mesh_warnings.append(f"Very high polygon count ({face_count:,} faces) — consider decimating before slicing.")
                elif face_count > 500_000:
                    mesh_warnings.append(f"High polygon count ({face_count:,} faces) — slicing may be slow.")

        except Exception:
            pass  # trimesh unavailable or can't parse — skip health checks

    result = {"filename": file.filename, "size": None, "build_vol": None,
              "overflow_warning": None, "mesh_warnings": mesh_warnings}
    if sx is None:
        result["overflow_warning"] = "Could not read model dimensions — size check skipped. Verify the file fits before slicing."
        result["parse_failed"] = True
        return jsonify(result)
    result["size"] = {"x": round(sx, 1), "y": round(sy, 1), "z": round(sz, 1)}
    if printer:
        bv = get_build_volume(printer)
        result["build_vol"] = bv
        over = []
        if sx > bv["x"]: over.append(f"X {sx:.0f} > {bv['x']:.0f} mm")
        if sy > bv["y"]: over.append(f"Y {sy:.0f} > {bv['y']:.0f} mm")
        if sz > bv["z"]: over.append(f"Z {sz:.0f} > {bv['z']:.0f} mm")
        if over:
            result["overflow_warning"] = f"Exceeds build volume ({', '.join(over)})"
    return jsonify(result)


@app.route("/api/quote", methods=["POST"])
def api_quote():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400

    form = request.form
    printer      = form.get("printer", "")
    print_prof   = form.get("print_profile", "")
    filament     = form.get("filament", "")
    size_mode    = form.get("size_mode", "scale")
    size_val     = form.get("size_val", "").strip()
    infill       = form.get("infill", "").strip()
    layer_h      = form.get("layer_height", "").strip()
    walls        = form.get("walls", "").strip()
    supports     = form.get("supports", "")          # "none", "grid", "snug", "organic"
    cost_per_kg  = form.get("cost_per_kg", "").strip()
    hourly_rate  = form.get("hourly_rate", "").strip()
    markup       = form.get("markup", "").strip()
    quantity     = int(form.get("quantity", 1) or 1)
    time_factor  = float(form.get("time_factor", 1.0) or 1.0)
    farm_size    = max(1, int(form.get("farm_size", 1) or 1))
    do_orient     = form.get("auto_orient")    == "true"
    do_split      = form.get("auto_split")     == "true"
    do_scale_fit  = form.get("auto_scale_fit") == "true"
    pid           = form.get("progress_id", "")

    suffix = Path(file.filename).suffix.lower()
    _emit(pid, "Saving file", "done", file.filename)

    # Save a persistent copy for 3MF export
    import shutil
    persistent_input = os.path.join(EXPORT_DIR, "last_input" + suffix)
    file.seek(0)
    file.save(persistent_input)
    _last_job.update({
        "input_path": persistent_input,
        "filename":   file.filename,
        "printer":    printer,
        "print_prof": print_prof,
        "filament":   filament,
        "infill":     infill,
        "layer_h":    layer_h,
        "walls":      walls,
        "supports":   supports,
        "size_val":   size_val,
        "size_mode":  size_mode,
    })

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, "input" + suffix)
        shutil.copy2(persistent_input, input_path)

        orient_note = None
        work_path   = input_path

        # ── Determine pieces to slice ─────────────────────────────────────────
        pieces = []   # list of (stl_path, label)

        overflow_warning = None   # set if model exceeds build volume

        if printer:
            _emit(pid, "Loading printer profile", "done", printer)
            build_vol = get_build_volume(printer)

            # Apply size transform before checking fit
            if (do_split or do_scale_fit) and size_val:
                scaled_path = os.path.join(tmpdir, "scaled.stl")
                scale_args  = parse_size(size_val, size_mode)
                r = run_slicer(*scale_args, "--export-stl", "--output", scaled_path, work_path)
                if r.returncode == 0 and os.path.exists(scaled_path):
                    work_path = scaled_path
                    size_val  = ""  # already applied, don't re-apply during slice

            # Always check overflow so we can warn even when auto-split is off
            _emit(pid, "Checking mesh bounds", "running")
            sx, sy, sz = get_mesh_bounds(work_path)
            _emit(pid, "Checking mesh bounds", "done",
                  f"{sx:.0f}×{sy:.0f}×{sz:.0f} mm" if sx else "could not read")
            if sx is not None:
                over = []
                if sx > build_vol["x"]: over.append(f"X {sx:.0f} > {build_vol['x']:.0f} mm")
                if sy > build_vol["y"]: over.append(f"Y {sy:.0f} > {build_vol['y']:.0f} mm")
                if sz > build_vol["z"]: over.append(f"Z {sz:.0f} > {build_vol['z']:.0f} mm")
                if over:
                    if do_scale_fit:
                        fit_x = build_vol["x"] * 0.95
                        fit_y = build_vol["y"] * 0.95
                        fit_z = build_vol["z"] * 0.95
                        overflow_warning = f"Model scaled to fit build volume ({', '.join(over)})."
                        if do_orient:
                            _emit(pid, "Auto-orienting", "running")
                            work_path, orient_note = auto_orient(work_path, tmpdir)
                            _emit(pid, "Auto-orienting", "done", orient_note)
                        _emit(pid, "Scaling to fit build volume", "done", f"target {fit_x:.0f}×{fit_y:.0f}×{fit_z:.0f} mm")
                        pieces = [(work_path, "whole")]
                        size_val  = f"{fit_x:.0f},{fit_y:.0f},{fit_z:.0f}"
                        size_mode = "fit"
                    elif do_split:
                        overflow_warning = f"Model too large ({', '.join(over)}), auto-split into pieces."
                        _emit(pid, "Splitting model", "running", ', '.join(over))
                        pieces = split_if_needed(work_path, build_vol, tmpdir, do_orient=do_orient)
                        _emit(pid, "Splitting model", "done", f"{len(pieces)} piece{'s' if len(pieces)!=1 else ''}")
                        if do_orient:
                            orient_note = "each piece auto-oriented"
                    else:
                        overflow_warning = f"Model exceeds build volume ({', '.join(over)}). Enable Auto-Split or Scale to Fit."
                        if do_orient:
                            _emit(pid, "Auto-orienting", "running")
                            work_path, orient_note = auto_orient(work_path, tmpdir)
                            _emit(pid, "Auto-orienting", "done", orient_note)
                        pieces = [(work_path, "whole")]
                else:
                    if do_orient:
                        _emit(pid, "Auto-orienting", "running")
                        work_path, orient_note = auto_orient(work_path, tmpdir)
                        _emit(pid, "Auto-orienting", "done", orient_note)
                    pieces = [(work_path, "whole")]
            elif do_split:
                _emit(pid, "Splitting model", "running")
                pieces = split_if_needed(work_path, build_vol, tmpdir, do_orient=do_orient)
                _emit(pid, "Splitting model", "done", f"{len(pieces)} piece{'s' if len(pieces)!=1 else ''}")
                if do_orient:
                    orient_note = "each piece auto-oriented"
            else:
                if do_orient:
                    _emit(pid, "Auto-orienting", "running")
                    work_path, orient_note = auto_orient(work_path, tmpdir)
                    _emit(pid, "Auto-orienting", "done", orient_note)
                pieces = [(work_path, "whole")]
        else:
            if do_orient:
                _emit(pid, "Auto-orienting", "running")
                work_path, orient_note = auto_orient(work_path, tmpdir)
                _emit(pid, "Auto-orienting", "done", orient_note)
            pieces = [(work_path, "whole")]

        # ── Build machine-limits-only ini for user printer profiles (e.g. Klipper) ─
        machine_limits_ini = build_machine_limits_ini(tmpdir, printer)

        # Persist the work_path (post-orient/scale) for 3MF export
        work_suffix = Path(work_path).suffix or ".stl"
        persistent_work = os.path.join(EXPORT_DIR, "last_work" + work_suffix)
        shutil.copy2(work_path, persistent_work)
        _last_job["work_path"] = persistent_work
        if machine_limits_ini and os.path.exists(machine_limits_ini):
            persistent_cfg = os.path.join(EXPORT_DIR, "last_machine_limits.ini")
            shutil.copy2(machine_limits_ini, persistent_cfg)
            _last_job["resolved_config"] = persistent_cfg
        else:
            _last_job["resolved_config"] = None

        # ── Slice each piece ──────────────────────────────────────────────────
        import uuid as _uuid
        job_id = str(_uuid.uuid4())[:8]

        piece_results = []
        errors_out    = []

        gcode_paths = []   # (label, path) for all successfully sliced pieces
        n_pieces = len(pieces)
        for idx, (piece_path, piece_label) in enumerate(pieces):
            persistent_gcode = os.path.join(EXPORT_DIR, f"{job_id}_piece_{idx}.gcode")
            step_label = f"Slicing {piece_label}" if n_pieces > 1 else "Slicing"
            counter    = f"{idx+1} of {n_pieces}" if n_pieces > 1 else ""
            _emit(pid, step_label, "running", counter)
            try:
                stats = slice_piece(
                    piece_path, tmpdir, idx,
                    printer, print_prof, filament,
                    size_val, size_mode, infill, layer_h, walls, supports,
                    machine_limits_ini=machine_limits_ini,
                    persistent_gcode_path=persistent_gcode,
                )
                t = stats.get("time_normal", "")
                g = sum(stats.get("filament_g", [])) or None
                detail = " · ".join(filter(None, [t, f"{g:.1f} g" if g else ""]))
                _emit(pid, step_label, "done", detail)
                piece_results.append({"label": piece_label, "stats": stats})
                gcode_paths.append((piece_label, persistent_gcode))
            except RuntimeError as e:
                _emit(pid, step_label, "error", str(e))
                errors_out.append(f"{piece_label}: {e}")

        _last_job["gcode_paths"] = gcode_paths

        if not piece_results:
            return jsonify({
                "error": "\n".join(errors_out) or "Slicing failed for all pieces",
                "overflow_warning": overflow_warning,
            }), 500

    _emit(pid, "Calculating quote", "running")
    # ── Aggregate stats ───────────────────────────────────────────────────────
    total_mins   = 0.0
    total_g      = 0.0
    total_cm3    = 0.0
    total_mm     = 0.0
    total_cost_p = 0.0

    piece_summaries = []
    for pr in piece_results:
        s = pr["stats"]
        piece_mins = time_to_minutes(s["time_normal"]) if s["time_normal"] else 0
        piece_g    = sum(s["filament_g"])    if s["filament_g"]    else 0
        piece_cm3  = sum(s["filament_cm3"])  if s["filament_cm3"]  else 0
        piece_mm   = sum(s["filament_mm"])   if s["filament_mm"]   else 0
        piece_cp   = sum(s["filament_cost"]) if s["filament_cost"] else 0
        total_mins   += piece_mins
        total_g      += piece_g
        total_cm3    += piece_cm3
        total_mm     += piece_mm
        total_cost_p += piece_cp
        piece_summaries.append({
            "label":   pr["label"],
            "time":    s["time_normal"] or "—",
            "weight_g": round(piece_g, 1),
            "volume_cm3": round(piece_cm3, 1),
        })

    raw_mins    = total_mins
    total_mins  = total_mins * time_factor   # apply calibration factor
    time_str    = mins_to_str(total_mins)
    split_count = len(piece_results)

    material_cost = None
    if cost_per_kg and total_g:
        material_cost = (total_g / 1000) * float(cost_per_kg)
    elif total_cost_p:
        material_cost = total_cost_p

    machine_cost = None
    if hourly_rate and total_mins:
        machine_cost = (total_mins / 60) * float(hourly_rate)

    subtotal    = (material_cost or 0) + (machine_cost or 0)
    quote_price = subtotal * (1 + float(markup) / 100) if markup and subtotal else None

    qty_time   = mins_to_str(total_mins * quantity) if quantity > 1 else None
    qty_weight = round(total_g * quantity, 1)       if quantity > 1 else None

    import math as _math
    farm_time = None
    if farm_size > 1 and total_mins > 0:
        batches   = _math.ceil(quantity / farm_size)
        farm_time = mins_to_str(total_mins * batches)

    price_str = f"${quote_price:.2f}" if quote_price else (f"${subtotal:.2f}" if subtotal else "")
    _emit(pid, "Calculating quote", "done", price_str)

    _jobs[job_id] = {"gcode_paths": gcode_paths, "filename": file.filename}
    if len(_jobs) > 50:
        del _jobs[next(iter(_jobs))]

    return jsonify({
        "filename":       file.filename,
        "job_id":         job_id,
        "printer":        printer,
        "print_profile":  print_prof,
        "filament":       filament,
        "size_mode":      size_mode,
        "size_val":       size_val,
        "orient_note":    orient_note,
        "split_count":    split_count,
        "supports":       supports,
        "walls":          walls,
        "time_factor":    time_factor,
        "raw_time":       mins_to_str(raw_mins) if time_factor != 1.0 else None,
        "pieces":         piece_summaries,
        "time":           time_str,
        "weight_g":       round(total_g, 1)   if total_g   else None,
        "volume_cm3":     round(total_cm3, 1) if total_cm3 else None,
        "length_mm":      round(total_mm, 0)  if total_mm  else None,
        "material_cost":  round(material_cost, 2) if material_cost else None,
        "machine_cost":   round(machine_cost,  2) if machine_cost  else None,
        "subtotal":       round(subtotal, 2)   if subtotal  else None,
        "markup_pct":     float(markup)        if markup    else None,
        "markup_amt":     round(subtotal * float(markup) / 100, 2) if markup and subtotal else None,
        "quote_price":    round(quote_price, 2) if quote_price else None,
        "quantity":       quantity,
        "qty_time":       qty_time,
        "qty_weight_g":   qty_weight,
        "cost_per_kg":    float(cost_per_kg)  if cost_per_kg  else None,
        "hourly_rate":    float(hourly_rate)  if hourly_rate  else None,
        "errors":           errors_out,
        "overflow_warning": overflow_warning,
        "farm_size":        farm_size,
        "farm_time":        farm_time,
    })


@app.route("/api/export_3mf", methods=["POST"])
def api_export_3mf():
    """Export last job as a 3MF project file using the exact model and resolved config used for quoting."""
    # Use post-orient/scale work_path if available, otherwise fall back to original input
    model_path = _last_job.get("work_path") or _last_job.get("input_path")
    if not model_path or not os.path.exists(model_path):
        return jsonify({"error": "No job to export — run a quote first"}), 400

    j = _last_job
    out_3mf = os.path.join(EXPORT_DIR, "export.3mf")

    if os.path.exists(out_3mf):
        os.remove(out_3mf)

    # Use the already-resolved config (with machine limits patched) if available
    # Use direct profile flags (same as slicing) so settings are correct
    cmd = _profile_flags(j["printer"], j["print_prof"], j["filament"])
    cmd += _override_flags(j["infill"], j["layer_h"], j["walls"], j["supports"])
    machine_limits = j.get("resolved_config")
    if machine_limits and os.path.exists(machine_limits):
        cmd += ["--load", machine_limits]

    if j.get("size_val"):
        cmd += parse_size(j["size_val"], j["size_mode"])

    cmd += ["--export-3mf", "--output", out_3mf, model_path]
    result = run_slicer(*cmd)

    if not os.path.exists(out_3mf):
        errors = [l for l in result.stderr.splitlines() if "error" in l.lower()]
        return jsonify({"error": "\n".join(errors) or result.stderr[:400]}), 500

    stem = Path(j["filename"]).stem
    return send_file(out_3mf, as_attachment=True, download_name=f"{stem}_quote.3mf",
                     mimetype="application/vnd.ms-package.3dmanufacturing-3dmodel+xml")


@app.route("/api/export_ini")
def api_export_ini():
    """Download the machine limits ini used for the last quote (for debugging)."""
    cfg = _last_job.get("resolved_config")
    if not cfg or not os.path.exists(cfg):
        return jsonify({"error": "No machine limits config — run a quote with a user printer profile first"}), 400
    return send_file(cfg, as_attachment=True, download_name="machine_limits.ini",
                     mimetype="text/plain")


@app.route("/api/export_gcode")
def api_export_gcode():
    """Download sliced gcode(s). Pass ?job_id=xxx for a specific quote, or omit for the last one."""
    import zipfile, io
    job_id = request.args.get("job_id")
    job = (_jobs.get(job_id) if job_id else None) or _last_job
    gcode_paths = job.get("gcode_paths", [])
    gcode_paths = [(lbl, p) for lbl, p in gcode_paths if os.path.exists(p)]
    if not gcode_paths:
        return jsonify({"error": "No gcode — run a quote first"}), 400

    stem = Path(job.get("filename", "quote")).stem

    if len(gcode_paths) == 1:
        lbl, path = gcode_paths[0]
        return send_file(path, as_attachment=True, download_name=f"{stem}_quote.gcode",
                         mimetype="text/plain")

    # Multiple pieces — zip them
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, (lbl, path) in enumerate(gcode_paths, 1):
            zf.write(path, f"{stem}_piece{i}_{lbl}.gcode")
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{stem}_quote.zip",
                     mimetype="application/zip")


# ── HTML / CSS / JS ───────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>3D Print Quoting</title>
<style>
  :root {
    --bg:       #0f1117;
    --surface:  #1a1d27;
    --border:   #2a2d3e;
    --accent:   #6c63ff;
    --accent2:  #4ecdc4;
    --text:     #e8eaf0;
    --muted:    #7b7f9e;
    --danger:   #ff6b6b;
    --success:  #51cf66;
    --input-bg: #12141e;
    --warn:     #ffa94d;
    --card-bg:  #1e2130;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding: 2rem 1rem;
  }
  h1 { font-size: 1.5rem; font-weight: 700; letter-spacing: -0.02em; color: #fff; margin-bottom: 0.25rem; }
  .subtitle { color: var(--muted); font-size: 0.85rem; margin-bottom: 2rem; }
  .layout {
    display: grid;
    grid-template-columns: 400px 1fr;
    gap: 1.5rem;
    max-width: 1100px;
    margin: 0 auto;
    align-items: start;
  }
  @media (max-width: 800px) { .layout { grid-template-columns: 1fr; } }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.5rem;
  }
  .card-title {
    font-size: 0.7rem; font-weight: 700; letter-spacing: 0.1em;
    text-transform: uppercase; color: var(--muted); margin-bottom: 1rem;
  }
  /* Drop zone */
  #drop-zone {
    border: 2px dashed var(--border); border-radius: 10px; padding: 2rem 1rem;
    text-align: center; cursor: pointer; transition: border-color .2s, background .2s;
    margin-bottom: 1.25rem; position: relative;
  }
  #drop-zone:hover, #drop-zone.dragover { border-color: var(--accent); background: rgba(108,99,255,.06); }
  #drop-zone.has-file { border-color: var(--accent2); background: rgba(78,205,196,.05); }
  #drop-zone input[type=file] { position: absolute; inset: 0; opacity: 0; width: 100%; height: 100%; pointer-events: none; }
  .drop-icon { font-size: 2rem; margin-bottom: 0.5rem; }
  .drop-label { font-size: 0.9rem; color: var(--muted); }
  .drop-label span { color: var(--accent); font-weight: 600; }
  #file-name { font-size: 0.8rem; color: var(--accent2); margin-top: 0.4rem; font-weight: 500; }
  /* Form */
  .form-group { margin-bottom: 1rem; }
  .form-group label { display: block; font-size: 0.75rem; font-weight: 600; color: var(--muted); margin-bottom: 0.35rem; letter-spacing: 0.05em; text-transform: uppercase; }
  select, input[type=text], input[type=number] {
    width: 100%; background: var(--input-bg); border: 1px solid var(--border);
    border-radius: 8px; color: var(--text); font-size: 0.875rem;
    padding: 0.55rem 0.75rem; outline: none; transition: border-color .2s;
    appearance: none; -webkit-appearance: none;
  }
  select { background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%237b7f9e' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 0.75rem center; padding-right: 2rem; }
  select:focus, input:focus { border-color: var(--accent); }
  select option { background: #1a1d27; }
  .row  { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }
  /* Size / support toggles */
  .size-row, .support-row { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
  .toggle-btn {
    background: var(--input-bg); border: 1px solid var(--border); color: var(--muted);
    border-radius: 6px; padding: 0.5rem 0.6rem; font-size: 0.7rem; font-weight: 700;
    cursor: pointer; white-space: nowrap; transition: all .15s; flex-shrink: 0;
  }
  .toggle-btn.active { background: var(--accent); border-color: var(--accent); color: #fff; }
  /* Feature toggles */
  .feature-row {
    display: flex; align-items: center; gap: 0.75rem;
    background: var(--input-bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 0.6rem 0.85rem; margin-bottom: 0.6rem;
    cursor: pointer; user-select: none;
  }
  .feature-row:hover { border-color: var(--accent); }
  .feature-row.on { border-color: var(--accent2); background: rgba(78,205,196,.05); }
  .feature-row input[type=checkbox] { accent-color: var(--accent2); width: 15px; height: 15px; cursor: pointer; }
  .feature-label { font-size: 0.82rem; font-weight: 600; flex: 1; }
  .feature-sub { font-size: 0.7rem; color: var(--muted); }
  .feature-badge { font-size: 0.65rem; font-weight: 700; letter-spacing: .05em; padding: 0.15rem 0.45rem; border-radius: 4px; }
  .badge-orca { background: rgba(255,169,77,.12); color: var(--warn); border: 1px solid rgba(255,169,77,.25); }
  .badge-tri  { background: rgba(108,99,255,.12); color: var(--accent); border: 1px solid rgba(108,99,255,.25); }
  /* Divider */
  .divider { border: none; border-top: 1px solid var(--border); margin: 1.1rem 0; }
  /* Run button */
  #run-btn {
    width: 100%; padding: 0.8rem; background: var(--accent); color: #fff; border: none;
    border-radius: 9px; font-size: 0.95rem; font-weight: 700; cursor: pointer;
    transition: opacity .2s, transform .1s; letter-spacing: 0.02em; margin-top: 0.25rem;
  }
  #run-btn:hover:not(:disabled) { opacity: .88; }
  #run-btn:active:not(:disabled) { transform: scale(.98); }
  #run-btn:disabled { opacity: .45; cursor: not-allowed; }
  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(255,255,255,.3); border-top-color: #fff; border-radius: 50%; animation: spin .7s linear infinite; vertical-align: middle; margin-right: 6px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  /* Results */
  #results { display: none; }
  .result-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.25rem; }
  .result-filename { font-size: 1rem; font-weight: 700; color: #fff; }
  .result-sub { font-size: 0.78rem; color: var(--muted); margin-top: 2px; }
  .copy-btn { background: transparent; border: 1px solid var(--border); color: var(--muted); border-radius: 6px; padding: 0.35rem 0.7rem; font-size: 0.75rem; cursor: pointer; transition: all .15s; }
  .copy-btn:hover { border-color: var(--accent); color: var(--accent); }
  /* Applied tags */
  .applied-row { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem; }
  .applied-tag { font-size: 0.7rem; font-weight: 700; padding: 0.2rem 0.55rem; border-radius: 5px; letter-spacing: 0.04em; }
  .tag-orient { background: rgba(255,169,77,.1); color: var(--warn); border: 1px solid rgba(255,169,77,.25); }
  .tag-split  { background: rgba(108,99,255,.1); color: var(--accent); border: 1px solid rgba(108,99,255,.25); }
  /* Stat grid */
  .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; margin-bottom: 1rem; }
  .stat-box { background: var(--input-bg); border: 1px solid var(--border); border-radius: 10px; padding: 0.85rem 1rem; }
  .stat-label { font-size: 0.68rem; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 0.3rem; }
  .stat-value { font-size: 1.2rem; font-weight: 700; color: #fff; }
  .stat-value.accent { color: var(--accent2); }
  .stat-sub { font-size: 0.72rem; color: var(--muted); margin-top: 0.15rem; }
  /* Pieces table */
  #pieces-section { display: none; margin-bottom: 1rem; }
  .pieces-title { font-size: 0.7rem; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted); margin-bottom: 0.6rem; }
  .piece-row {
    display: grid; grid-template-columns: 1fr auto auto;
    gap: 0.5rem; align-items: center;
    background: var(--input-bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 0.55rem 0.85rem; margin-bottom: 0.4rem;
    font-size: 0.82rem;
  }
  .piece-label { color: var(--text); font-weight: 600; text-transform: capitalize; }
  .piece-time  { color: var(--muted); font-size: 0.75rem; }
  .piece-weight { color: var(--accent2); font-weight: 700; }
  /* Cost table */
  .cost-table { width: 100%; border-collapse: collapse; }
  .cost-table tr td { padding: 0.45rem 0; font-size: 0.875rem; }
  .cost-table tr td:last-child { text-align: right; font-weight: 600; }
  .cost-table .muted { color: var(--muted); font-size: 0.78rem; }
  .cost-table .total-row td { border-top: 1px solid var(--border); padding-top: 0.65rem; font-size: 1rem; font-weight: 700; color: var(--accent2); }
  /* Qty row */
  .qty-row { background: rgba(78,205,196,.05); border: 1px solid rgba(78,205,196,.2); border-radius: 8px; padding: 0.65rem 0.85rem; margin-top: 0.75rem; font-size: 0.82rem; display: flex; justify-content: space-between; color: var(--text); }
  .qty-row span { color: var(--accent2); font-weight: 700; }
  /* Error box */
  #error-box { display: none; background: rgba(255,107,107,.08); border: 1px solid rgba(255,107,107,.3); border-radius: 10px; padding: 1rem 1.2rem; color: var(--danger); font-size: 0.82rem; font-family: monospace; white-space: pre-wrap; word-break: break-all; margin-top: 1rem; }
  .warn-row { background: rgba(255,169,77,.07); border: 1px solid rgba(255,169,77,.25); border-radius: 8px; padding: 0.55rem 0.85rem; color: var(--warn); font-size: 0.78rem; margin-bottom: 0.75rem; }
  .preset-bar { display:flex; gap:0.5rem; align-items:center; margin-bottom:0.75rem; flex-wrap:wrap; }
  .preset-bar select { flex:1; min-width:0; }
  .preset-btn { background:var(--input-bg); border:1px solid var(--border); color:var(--muted); border-radius:6px; padding:0.4rem 0.65rem; font-size:0.75rem; font-weight:700; cursor:pointer; white-space:nowrap; transition:all .15s; }
  .preset-btn:hover { border-color:var(--accent); color:var(--accent); }
  .preset-btn.del-btn:hover { border-color:#ff6b6b; color:#ff6b6b; }
  .preset-btn.save-btn { border-color:rgba(78,205,196,.4); color:var(--accent2); width:100%; padding:0.5rem; margin-bottom:0.75rem; }
  .preset-btn.save-btn:hover { background:rgba(78,205,196,.08); }
  #file-queue { margin-top:0.75rem; display:none; }
  .queue-item { display:flex; align-items:center; gap:0.5rem; background:var(--input-bg); border:1px solid var(--border); border-radius:7px; padding:0.4rem 0.75rem; margin-bottom:0.3rem; font-size:0.8rem; cursor:pointer; transition:border-color .15s, background .15s; }
  .queue-item:hover        { background:rgba(108,99,255,.07); }
  .queue-item.selected     { border-color:var(--accent); background:rgba(108,99,255,.1); }
  .queue-item.st-slicing   { border-color:var(--accent); }
  .queue-item.st-done      { border-color:#51cf66; }
  .queue-item.st-error     { border-color:#ff6b6b; }
  .queue-fname  { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--text); }
  .queue-status { font-size:0.7rem; font-weight:700; letter-spacing:.04em; color:var(--muted); flex-shrink:0; }
  .queue-status.st-pending  { color:var(--muted); }
  .queue-status.st-slicing  { color:var(--accent); }
  .queue-status.st-done     { color:#51cf66; }
  .queue-status.st-error    { color:#ff6b6b; }
  .queue-badge  { font-size:0.65rem; color:var(--accent); flex-shrink:0; }
  .queue-del    { background:none; border:none; color:var(--muted); cursor:pointer; font-size:0.85rem; padding:0 0.2rem; line-height:1; flex-shrink:0; }
  .queue-del:hover { color:#ff6b6b; }
  .queue-clear { background:none; border:none; color:var(--muted); cursor:pointer; font-size:0.75rem; padding:0.1rem 0.3rem; border-radius:4px; }
  .queue-clear:hover { color:#ff6b6b; }
  .batch-table { width:100%; border-collapse:collapse; font-size:0.82rem; margin-top:0.5rem; }
  .batch-table th { text-align:left; padding:0.35rem 0.5rem; font-size:0.68rem; font-weight:700; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); border-bottom:1px solid var(--border); }
  .batch-table td { padding:0.4rem 0.5rem; border-bottom:1px solid rgba(42,45,62,.5); }
  .batch-table tr:last-child td { border-bottom:none; }
  .batch-table tbody tr.clickable { cursor:pointer; }
  .batch-table tbody tr.clickable:hover td { background:rgba(108,99,255,.07); }
  .batch-totals td { border-top:1px solid var(--border); font-weight:700; color:var(--accent2); padding-top:0.55rem; }
  .batch-err { color:#ff6b6b; font-size:0.75rem; }
  /* Pre-flight size check panel */
  #preflight-panel { display:none; margin-top:1rem; }
  .preflight-title { font-size:0.7rem; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:var(--warn); margin-bottom:0.6rem; }
  .preflight-file { display:grid; grid-template-columns:1fr auto; align-items:center; gap:0.5rem; background:rgba(255,169,77,.05); border:1px solid rgba(255,169,77,.25); border-radius:8px; padding:0.65rem 0.85rem; margin-bottom:0.4rem; }
  .preflight-info { font-size:0.8rem; color:var(--text); }
  .preflight-info strong { color:var(--warn); }
  .preflight-info small { display:block; color:var(--muted); font-size:0.72rem; margin-top:0.15rem; }
  .preflight-actions { display:flex; gap:0.35rem; flex-shrink:0; }
  .pf-btn { font-size:0.72rem; font-weight:700; padding:0.3rem 0.6rem; border-radius:5px; border:1px solid; cursor:pointer; background:transparent; transition:all .15s; white-space:nowrap; }
  .pf-btn.active { background:rgba(78,205,196,.15); border-color:var(--accent2); color:var(--accent2); }
  .pf-btn:not(.active) { border-color:var(--border); color:var(--muted); }
  .pf-btn:hover:not(.active) { border-color:var(--accent); color:var(--accent); }
  .preflight-confirm { width:100%; margin-top:0.75rem; padding:0.65rem; background:var(--accent); color:#fff; border:none; border-radius:8px; font-size:0.9rem; font-weight:700; cursor:pointer; }
  .preflight-confirm:hover { opacity:.88; }
  .pf-mesh-warns { margin-top:0.4rem; display:flex; flex-direction:column; gap:0.2rem; }
  .pf-mesh-warn { font-size:0.72rem; color:var(--warn); opacity:.9; }
  /* Progress panel */
  #progress-panel { display:none; }
  .progress-header { display:flex; align-items:baseline; gap:0.5rem; margin-bottom:0.75rem; }
  .progress-title { font-size:0.8rem; font-weight:700; color:var(--text); flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .progress-subtitle { font-size:0.72rem; color:var(--muted); }
  .progress-bar-track { height:4px; background:var(--border); border-radius:2px; margin-bottom:1rem; overflow:hidden; }
  .progress-bar-fill { height:100%; width:100%; background:linear-gradient(90deg,var(--accent),var(--accent2)); border-radius:2px; animation:progress-sweep 1.8s ease-in-out infinite; transform-origin:left; }
  @keyframes progress-sweep { 0%{transform:scaleX(0.05) translateX(0%)} 50%{transform:scaleX(0.6) translateX(60%)} 100%{transform:scaleX(0.05) translateX(1800%)} }
  .progress-steps { display:flex; flex-direction:column; gap:0.3rem; }
  .progress-step { display:flex; align-items:baseline; gap:0.5rem; font-size:0.78rem; }
  .progress-step-icon { flex-shrink:0; width:1rem; text-align:center; }
  .progress-step-running .progress-step-icon { animation:spin .8s linear infinite; display:inline-block; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .progress-step-done .progress-step-icon { color:var(--success); }
  .progress-step-error .progress-step-icon { color:var(--danger); }
  .progress-step-running { color:var(--text); }
  .progress-step-done { color:var(--muted); }
  .progress-step-error { color:var(--danger); }
  .progress-step-label { flex:1; }
  .progress-step-detail { color:var(--muted); font-size:0.72rem; margin-left:0.25rem; }
  /* Right-column tabs */
  .rc-tabs { display:flex; gap:0.4rem; margin-bottom:1rem; }
  .rc-tab { font-size:0.7rem; font-weight:700; letter-spacing:.08em; text-transform:uppercase; padding:0.3rem 0.75rem; border-radius:6px; border:1px solid var(--border); background:transparent; color:var(--muted); cursor:pointer; transition:all .15s; }
  .rc-tab.active { background:var(--accent); border-color:var(--accent); color:#fff; }
  .rc-tab.has-warn { border-color:var(--warn); color:var(--warn); }
  .rc-tab.active.has-warn { background:var(--warn); border-color:var(--warn); color:#000; }
  /* Health tab */
  #health-panel { display:none; }
  .health-file { margin-bottom:1rem; padding:0.75rem; background:var(--input-bg); border-radius:8px; border:1px solid var(--border); }
  .health-file.has-issues { border-color:rgba(255,169,77,.35); }
  .health-filename { font-size:0.82rem; font-weight:700; color:var(--text); margin-bottom:0.35rem; display:flex; align-items:center; gap:0.4rem; }
  .health-dims { font-size:0.72rem; color:var(--muted); margin-bottom:0.35rem; }
  .health-ok { font-size:0.75rem; color:var(--success); }
  .health-warn { font-size:0.75rem; color:var(--warn); margin-bottom:0.15rem; }
  .health-err  { font-size:0.75rem; color:var(--danger); margin-bottom:0.15rem; }
  .health-empty { color:var(--muted); font-size:0.8rem; text-align:center; padding:2rem; }
  /* Debug panel */
  #debug-btn { position:fixed; bottom:1.2rem; right:1.2rem; z-index:900; background:var(--surface); border:1px solid var(--border); color:var(--muted); font-size:0.72rem; font-weight:700; padding:0.4rem 0.75rem; border-radius:20px; cursor:pointer; letter-spacing:.05em; transition:all .15s; }
  #debug-btn:hover { border-color:var(--accent); color:var(--accent); }
  #debug-btn.has-errors { border-color:var(--danger); color:var(--danger); }
  #debug-panel { display:none; position:fixed; bottom:0; left:0; right:0; z-index:950; background:#0a0c12; border-top:2px solid var(--border); max-height:55vh; overflow-y:auto; font-family:ui-monospace,monospace; font-size:0.75rem; }
  #debug-panel.open { display:block; }
  .debug-header { display:flex; align-items:center; gap:0.75rem; padding:0.6rem 1rem; background:#0f1117; border-bottom:1px solid var(--border); position:sticky; top:0; }
  .debug-header-title { font-weight:700; color:var(--text); flex:1; letter-spacing:.05em; font-size:0.72rem; text-transform:uppercase; }
  .debug-clear-btn { font-size:0.7rem; padding:0.25rem 0.6rem; border:1px solid var(--border); border-radius:4px; background:transparent; color:var(--muted); cursor:pointer; }
  .debug-clear-btn:hover { border-color:var(--danger); color:var(--danger); }
  .debug-close-btn { background:none; border:none; color:var(--muted); font-size:1.1rem; cursor:pointer; line-height:1; padding:0 0.25rem; }
  .debug-close-btn:hover { color:#fff; }
  .debug-empty { padding:1.5rem; color:var(--muted); text-align:center; }
  .debug-entry { border-bottom:1px solid #1a1d27; }
  .debug-entry-head { display:grid; grid-template-columns:auto auto 1fr auto; gap:0.6rem; align-items:center; padding:0.55rem 1rem; cursor:pointer; }
  .debug-entry-head:hover { background:rgba(255,255,255,.03); }
  .debug-ts { color:var(--muted); white-space:nowrap; }
  .debug-ep { color:var(--accent2); white-space:nowrap; }
  .debug-msg { color:var(--danger); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .debug-toggle { color:var(--muted); font-size:0.65rem; }
  .debug-entry-body { display:none; padding:0.5rem 1rem 0.75rem; background:#070910; }
  .debug-entry-body.open { display:block; }
  .debug-tb { white-space:pre-wrap; color:#a0a8c0; font-size:0.7rem; line-height:1.5; margin:0; }
  /* Quote modal */
  #quote-modal { display:none; position:fixed; inset:0; background:rgba(0,0,0,.7); z-index:1000; align-items:center; justify-content:center; padding:1rem; }
  #quote-modal.open { display:flex; }
  #quote-modal-inner { background:var(--card-bg); border:1px solid var(--border); border-radius:16px; padding:1.5rem; max-width:480px; width:100%; max-height:90vh; overflow-y:auto; position:relative; }
  #quote-modal-close { position:absolute; top:0.75rem; right:0.9rem; background:none; border:none; color:var(--muted); font-size:1.3rem; cursor:pointer; line-height:1; }
  #quote-modal-close:hover { color:#fff; }
  /* Placeholder */
  .placeholder { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 4rem 2rem; color: var(--muted); text-align: center; gap: 0.75rem; }
  .placeholder-icon { font-size: 3rem; opacity: .3; }
  .placeholder-text { font-size: 0.85rem; opacity: .6; }
</style>
</head>
<body>
<div style="max-width:1100px;margin:0 auto">
  <h1>3D Print Quoting</h1>
  <p class="subtitle">Drop a model, configure settings, get an instant quote.</p>

  <div class="layout">
    <!-- Left: Controls -->
    <div>
      <div class="card">
        <div class="card-title">Presets</div>
          <div class="preset-bar">
            <select id="preset-sel"><option value="">— saved presets —</option></select>
            <button class="preset-btn" onclick="loadPreset()">Load</button>
            <button class="preset-btn del-btn" onclick="deletePreset()">×</button>
          </div>
          <button class="preset-btn save-btn" onclick="savePreset()">Save Current Settings as Preset…</button>
          <div class="card-title">Model File</div>
        <div id="drop-zone">
          <input type="file" id="file-input" accept=".stl,.obj,.3mf,.amf,.step,.stp" multiple>
          <div class="drop-icon">📦</div>
          <div class="drop-label"><span>Click to browse</span> or drag & drop</div>
          <div class="drop-label" style="margin-top:.25rem;font-size:.75rem">STL · OBJ · 3MF · AMF · STEP</div>
          <div id="file-queue">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.3rem">
              <span style="font-size:.7rem;font-weight:700;color:var(--muted);letter-spacing:.08em;text-transform:uppercase">Queue (<span id="queue-count">0</span>)</span>
              <button class="queue-clear" onclick="clearQueue()">Clear all</button>
            </div>
            <div id="queue-list"></div>
          </div>
          <div id="file-name"></div>
        </div>

        <div class="card-title">Processing</div>
        <label class="feature-row" id="orient-row">
          <input type="checkbox" id="auto-orient" onchange="toggleFeature('orient-row','auto-orient')">
          <div>
            <div class="feature-label">Auto-orient</div>
            <div class="feature-sub">Rotate model to optimal print position</div>
          </div>
          <span class="feature-badge badge-orca">OrcaSlicer</span>
        </label>
        <label class="feature-row" id="split-row">
          <input type="checkbox" id="auto-split" onchange="toggleFeature('split-row','auto-split');if(this.checked)document.getElementById('auto-scale-fit').checked=false;">
          <div>
            <div class="feature-label">Auto-split if too large</div>
            <div class="feature-sub">Cut oversized models to fit build volume</div>
          </div>
          <span class="feature-badge badge-tri">trimesh</span>
        </label>
        <label class="feature-row" id="scale-fit-row">
          <input type="checkbox" id="auto-scale-fit" onchange="toggleFeature('scale-fit-row','auto-scale-fit');if(this.checked)document.getElementById('auto-split').checked=false;">
          <div>
            <div class="feature-label">Scale to fit build volume</div>
            <div class="feature-sub">Shrink oversized models to fit, keeping proportions</div>
          </div>
        </label>

        <hr class="divider">
        <div class="card-title">Size</div>
        <div class="form-group">
          <div class="size-row">
            <button class="toggle-btn active" id="btn-scale" onclick="setSizeMode('scale')">Scale %</button>
            <button class="toggle-btn" id="btn-fit" onclick="setSizeMode('fit')">Fit to box</button>
            <input type="text" id="size-val" placeholder="100%">
          </div>
          <div id="size-hint" style="font-size:.72rem;color:var(--muted);margin-top:.35rem">
            e.g. 150% or 0.5x — leave blank to use model as-is
          </div>
        </div>

        <hr class="divider">
        <div class="card-title">Printer & Profiles</div>
        <div class="form-group">
          <label>Printer</label>
          <select id="printer-sel" onchange="loadProfiles()">
            <option value="">Loading…</option>
          </select>
        </div>
        <div class="form-group">
          <label>Print Profile (Quality)</label>
          <select id="print-profile-sel"><option value="">— select printer first —</option></select>
        </div>
        <div class="form-group">
          <label>Filament</label>
          <select id="filament-sel"><option value="">— select printer first —</option></select>
        </div>

        <hr class="divider">
        <div class="card-title">Overrides (optional)</div>
        <div class="row">
          <div class="form-group">
            <label>Infill %</label>
            <input type="number" id="infill" placeholder="e.g. 20" min="0" max="100" step="5">
          </div>
          <div class="form-group">
            <label>Layer Height mm</label>
            <input type="number" id="layer-height" placeholder="e.g. 0.20" step="0.01" min="0.05" max="1.0">
          </div>
        </div>
        <div class="form-group">
          <label>Wall Count (perimeters)</label>
          <input type="number" id="walls" placeholder="e.g. 3" min="1" max="20" step="1">
        </div>
        <div class="form-group">
          <label>Supports</label>
          <div class="support-row">
            <button class="toggle-btn active" id="btn-sup-none"   onclick="setSupportMode('none')">None</button>
            <button class="toggle-btn"         id="btn-sup-grid"   onclick="setSupportMode('grid')">Standard</button>
            <button class="toggle-btn"         id="btn-sup-snug"   onclick="setSupportMode('snug')">Snug</button>
            <button class="toggle-btn"         id="btn-sup-organic" onclick="setSupportMode('organic')">Organic</button>
          </div>
        </div>

        <hr class="divider">
        <div class="card-title">Pricing</div>
        <div class="row">
          <div class="form-group">
            <label>Filament $/kg</label>
            <input type="number" id="cost-per-kg" placeholder="e.g. 22.00" step="0.5" min="0">
          </div>
          <div class="form-group">
            <label>Machine $/hr</label>
            <input type="number" id="hourly-rate" placeholder="e.g. 3.50" step="0.25" min="0">
          </div>
        </div>
        <div class="row">
          <div class="form-group">
            <label>Markup %</label>
            <input type="number" id="markup" placeholder="e.g. 40" step="5" min="0">
          </div>
          <div class="form-group">
            <label>Quantity</label>
            <input type="number" id="quantity" value="1" min="1" step="1">
          </div>
        </div>
        <div class="form-group">
          <label>Time correction factor
            <span style="font-weight:400;text-transform:none;letter-spacing:0;color:var(--muted);font-size:.7rem">&nbsp;— calibrate vs actual prints</span>
          </label>
          <input type="number" id="time-factor" value="1.0" step="0.05" min="0.1" max="3.0"
            placeholder="1.0 = no adjustment">
        </div>

          <div class="row" style="margin-bottom:0.75rem">
            <div class="form-group">
              <label>Farm size <span style="font-weight:400;text-transform:none;letter-spacing:0;color:var(--muted);font-size:.7rem">printers</span></label>
              <input type="number" id="farm-size" value="1" min="1" step="1">
            </div>
          </div>
          <button id="run-btn" onclick="runQuote()" disabled>Generate Quote</button>
      </div>
    </div>

    <!-- Right: Results -->
    <div class="card" id="results-card">
      <div class="rc-tabs" id="rc-tabs" style="display:none">
        <button class="rc-tab active" id="tab-quote" onclick="switchRcTab('quote')">Quote</button>
        <button class="rc-tab" id="tab-health" onclick="switchRcTab('health')">Health</button>
      </div>

      <!-- Progress panel -->
      <div id="progress-panel">
        <div class="progress-header">
          <div class="progress-title" id="progress-title">Quoting…</div>
          <div class="progress-subtitle" id="progress-subtitle"></div>
        </div>
        <div class="progress-bar-track"><div class="progress-bar-fill"></div></div>
        <div class="progress-steps" id="progress-steps"></div>
      </div>

      <div id="placeholder" class="placeholder">
        <div class="placeholder-icon">📐</div>
        <div class="placeholder-text">Upload a model and hit <strong>Generate Quote</strong></div>
      </div>
      <div id="preflight-panel">
        <div class="preflight-title">⚠ Oversize Files Detected</div>
        <div id="preflight-list"></div>
        <button class="preflight-confirm" onclick="confirmPreflight()">Continue &amp; Quote</button>
      </div>

      <div id="batch-results" style="display:none;margin-top:1rem">
        <div class="card-title" style="margin-bottom:0.5rem">Batch Summary</div>
        <div id="batch-warnings"></div>
        <table class="batch-table">
          <thead><tr><th>File</th><th>Time</th><th>Weight</th><th>Cost</th><th>GCode</th></tr></thead>
          <tbody id="batch-tbody"></tbody>
        </table>
      </div>

      <!-- Health tab -->
      <div id="health-panel">
        <div class="card-title" style="margin-bottom:0.75rem">File Health Report</div>
        <div id="health-list"><div class="health-empty">Run a pre-flight check to see results.</div></div>
      </div>

      <div id="results">
        <div class="result-header">
          <div>
            <div class="result-filename" id="res-filename">—</div>
            <div class="result-sub" id="res-profiles">—</div>
          </div>
          <button class="copy-btn" onclick="copyQuote()">Copy Quote</button>
          <button class="copy-btn" id="dl-3mf-btn" onclick="download3mf()" style="margin-left:0.4rem">Download 3MF</button>
          <a class="copy-btn" href="/api/export_gcode" style="margin-left:0.4rem;text-decoration:none;padding:0.35rem 0.7rem">Download GCode</a>
        </div>

        <div id="farm-row" class="qty-row" style="display:none;border-color:rgba(108,99,255,.2);background:rgba(108,99,255,.04);margin-bottom:0.75rem"></div>
        <div id="applied-row" class="applied-row"></div>
        <div id="warn-row" class="warn-row" style="display:none"></div>

        <div class="stat-grid">
          <div class="stat-box">
            <div class="stat-label">Print Time</div>
            <div class="stat-value" id="res-time">—</div>
            <div class="stat-sub" id="res-size-sub"></div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Weight</div>
            <div class="stat-value accent" id="res-weight">—</div>
            <div class="stat-sub" id="res-volume"></div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Filament Length</div>
            <div class="stat-value" id="res-length">—</div>
            <div class="stat-sub" id="res-length-m"></div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Pieces</div>
            <div class="stat-value" id="res-pieces">1</div>
            <div class="stat-sub" id="res-pieces-sub">single print</div>
          </div>
        </div>

        <div id="pieces-section">
          <div class="pieces-title">Piece Breakdown</div>
          <div id="pieces-list"></div>
        </div>

        <hr class="divider">
        <div class="card-title">Cost Breakdown</div>
        <table class="cost-table"><tbody id="cost-tbody"></tbody></table>

        <div id="qty-box" class="qty-row" style="display:none">
          <div>Qty <span id="qty-n">—</span> · Total time: <span id="qty-time">—</span></div>
          <div>Total weight: <span id="qty-weight">—</span></div>
        </div>
      </div>

      <div id="error-box"></div>
    </div>
  </div>
</div>

<script>
let sizeMode    = 'scale';
let supportMode = 'none';
let selectedFile = null;

async function init() {
  try {
    const [printersRes] = await Promise.all([fetch('/api/printers'), loadPresetsList()]);
    if (!printersRes.ok) throw new Error('Server returned ' + printersRes.status);
    const printers = await printersRes.json();
    const sel = document.getElementById('printer-sel');
    sel.innerHTML = '<option value="">— select printer —</option>' +
      printers.map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
    const last = localStorage.getItem('lastPrinter');
    if (last && printers.includes(last)) { sel.value = last; await loadProfiles(); }
    restoreInputs();
  } catch(e) {
    document.getElementById('printer-sel').innerHTML = '<option value="">— server unreachable —</option>';
    console.error('init() failed:', e);
  }
}

async function loadProfiles() {
  const printer = document.getElementById('printer-sel').value;
  if (!printer) return;
  localStorage.setItem('lastPrinter', printer);
  const res  = await fetch('/api/profiles?printer=' + encodeURIComponent(printer));
  const data = await res.json();
  const pp = document.getElementById('print-profile-sel');
  pp.innerHTML = '<option value="">— any —</option>' +
    data.print_profiles.map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
  const fp = document.getElementById('filament-sel');
  fp.innerHTML = '<option value="">— any —</option>' +
    data.filament_profiles.map(f => `<option value="${esc(f)}">${esc(f)}</option>`).join('');
  const lastPP = localStorage.getItem('lastPrintProfile');
  const lastFP = localStorage.getItem('lastFilament');
  if (lastPP) pp.value = lastPP;
  if (lastFP) fp.value = lastFP;
  updateRunBtn();
}

function setSizeMode(mode) {
  sizeMode = mode;
  document.getElementById('btn-scale').classList.toggle('active', mode === 'scale');
  document.getElementById('btn-fit').classList.toggle('active', mode === 'fit');
  const hint  = document.getElementById('size-hint');
  const input = document.getElementById('size-val');
  if (mode === 'fit') {
    hint.textContent  = 'e.g. 100,80 or 100,80,60 (X,Y or X,Y,Z in mm)';
    input.placeholder = '100,80,60';
  } else {
    hint.textContent  = 'e.g. 150% or 0.5x — leave blank for original size';
    input.placeholder = '100%';
  }
}

function setSupportMode(mode) {
  supportMode = mode;
  for (const m of ['none', 'grid', 'snug', 'organic']) {
    document.getElementById('btn-sup-' + m).classList.toggle('active', m === mode);
  }
  localStorage.setItem('supportMode', mode);
}

function toggleFeature(rowId, checkId) {
  const row = document.getElementById(rowId);
  const chk = document.getElementById(checkId);
  row.classList.toggle('on', chk.checked);
}

const dropZone  = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');
dropZone.addEventListener('click', e => {
    if (!e.target.closest('.queue-clear, .queue-item')) fileInput.click();
  });
dropZone.addEventListener('dragover',  e => { e.preventDefault(); dropZone.classList.add('dragover'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop', e => {
    e.preventDefault(); dropZone.classList.remove('dragover');
    const files = Array.from(e.dataTransfer.files).filter(f => /\\.(stl|obj|3mf|amf|step|stp)$/i.test(f.name));
    if (files.length) { addFilesToQueue(files); dropZone.classList.add('has-file'); document.getElementById('file-name').textContent = files.length === 1 ? '✓ ' + files[0].name : `✓ ${files.length} files queued`; }
  });
fileInput.addEventListener('change', () => {
    const files = Array.from(fileInput.files);
    if (files.length) { addFilesToQueue(files); dropZone.classList.add('has-file'); document.getElementById('file-name').textContent = files.length === 1 ? '✓ ' + files[0].name : `✓ ${files.length} files queued`; }
    fileInput.value = '';
  });

function setFile(f) {
  selectedFile = f;
  dropZone.classList.add('has-file');
  document.getElementById('file-name').textContent = '✓ ' + f.name;
  updateRunBtn();
}
function updateRunBtn() {
  document.getElementById('run-btn').disabled = !selectedFile;
}

let fileQueue = [];        // {file, status, result, settings:null|{...}}
let selectedQueueIdx = -1; // which queue item is active in the settings panel

function getFormSettings() {
  return {
    printer:       document.getElementById('printer-sel').value,
    print_profile: document.getElementById('print-profile-sel').value,
    filament:      document.getElementById('filament-sel').value,
    size_mode:     sizeMode,
    size_val:      document.getElementById('size-val').value,
    infill:        document.getElementById('infill').value,
    layer_height:  document.getElementById('layer-height').value,
    walls:         document.getElementById('walls').value,
    supports:      supportMode,
    auto_orient:     document.getElementById('auto-orient').checked,
    auto_split:      document.getElementById('auto-split').checked,
    auto_scale_fit:  document.getElementById('auto-scale-fit').checked,
    time_factor:   document.getElementById('time-factor').value || '1.0',
    cost_per_kg:   document.getElementById('cost-per-kg').value,
    hourly_rate:   document.getElementById('hourly-rate').value,
    markup:        document.getElementById('markup').value,
    quantity:      document.getElementById('quantity').value,
    farm_size:     document.getElementById('farm-size').value || '1',
  };
}

async function applyFormSettings(s) {
  const printerSel = document.getElementById('printer-sel');
  if (printerSel.value !== s.printer) {
    printerSel.value = s.printer;
    await loadProfiles();
  }
  document.getElementById('print-profile-sel').value = s.print_profile;
  document.getElementById('filament-sel').value      = s.filament;
  setSizeMode(s.size_mode);
  document.getElementById('size-val').value      = s.size_val;
  document.getElementById('infill').value        = s.infill;
  document.getElementById('layer-height').value  = s.layer_height;
  document.getElementById('walls').value         = s.walls;
  setSupportMode(s.supports);
  document.getElementById('auto-orient').checked    = s.auto_orient;
  document.getElementById('auto-split').checked     = s.auto_split;
  document.getElementById('auto-scale-fit').checked = s.auto_scale_fit || false;
  toggleFeature('orient-row',    'auto-orient');
  toggleFeature('split-row',     'auto-split');
  toggleFeature('scale-fit-row', 'auto-scale-fit');
  document.getElementById('time-factor').value  = s.time_factor;
  document.getElementById('cost-per-kg').value  = s.cost_per_kg;
  document.getElementById('hourly-rate').value  = s.hourly_rate;
  document.getElementById('markup').value       = s.markup;
  document.getElementById('quantity').value     = s.quantity;
  document.getElementById('farm-size').value    = s.farm_size;
}

async function selectQueueItem(i) {
  // Save current form into the previously selected item
  if (selectedQueueIdx >= 0 && selectedQueueIdx < fileQueue.length) {
    fileQueue[selectedQueueIdx].settings = getFormSettings();
  }
  selectedQueueIdx = i;
  const item = fileQueue[i];
  if (item.settings) await applyFormSettings(item.settings);
  renderQueue();
}

function deleteQueueItem(i) {
  fileQueue.splice(i, 1);
  if (selectedQueueIdx === i)      selectedQueueIdx = -1;
  else if (selectedQueueIdx > i)   selectedQueueIdx--;
  if (fileQueue.length === 0) { clearQueue(); return; }
  selectedFile = fileQueue[0]?.file || null;
  renderQueue();
  const btn = document.getElementById('run-btn');
  btn.disabled    = false;
  btn.textContent = fileQueue.length > 1 ? 'Quote All' : 'Generate Quote';
  if (fileQueue.length > 1) {
    // Refresh the batch summary table to remove the deleted row
    showBatchResults();
  } else if (fileQueue[0].status === 'done') {
    // One done item — show its individual result
    showResults(fileQueue[0].result);
  } else {
    // One pending/error item — show placeholder
    document.getElementById('batch-results').style.display = 'none';
    document.getElementById('results').style.display = 'none';
    document.getElementById('placeholder').style.display = 'block';
  }
}

function makePid() { return Math.random().toString(36).slice(2,12); }

// ── Progress panel ────────────────────────────────────────────────────────────
let _pollTimer = null;
let _activePid = null;

function showProgressPanel(title, subtitle) {
  document.getElementById('progress-title').textContent   = title;
  document.getElementById('progress-subtitle').textContent = subtitle || '';
  document.getElementById('progress-steps').innerHTML = '';
  document.getElementById('progress-panel').style.display  = 'block';
  document.getElementById('results').style.display         = 'none';
  document.getElementById('batch-results').style.display   = 'none';
  document.getElementById('placeholder').style.display     = 'none';
  document.getElementById('preflight-panel').style.display = 'none';
  document.getElementById('health-panel').style.display    = 'none';
  document.getElementById('error-box').style.display       = 'none';
  document.getElementById('rc-tabs').style.display         = 'none';
}

function hideProgressPanel() {
  document.getElementById('progress-panel').style.display = 'none';
  stopProgressPoll();
}

function startProgressPoll(pid) {
  _activePid = pid;
  _pollTimer = setInterval(async () => {
    try {
      const res  = await fetch(`/api/progress/${pid}`);
      const steps = await res.json();
      renderServerSteps(steps);
    } catch(e) {}
  }, 350);
}

function stopProgressPoll() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  if (_activePid) {
    const pid = _activePid; _activePid = null;
    setTimeout(() => fetch(`/api/progress/${pid}`, {method:'DELETE'}).catch(()=>{}), 3000);
  }
}

let _clientSteps = [];

function addProgressStep(label, status, detail) {
  const existing = _clientSteps.find(s => s.label === label);
  if (existing) { existing.status = status; existing.detail = detail || ''; }
  else _clientSteps.push({label, status, detail: detail || ''});
  renderServerSteps([]);  // re-render with current server steps
}

function renderServerSteps(serverSteps) {
  const all = [..._clientSteps, ...serverSteps];
  document.getElementById('progress-steps').innerHTML = all.map(s => {
    const icon   = s.status === 'done' ? '✓' : s.status === 'error' ? '✗' : s.status === 'running' ? '↻' : '○';
    const detail = s.detail ? `<span class="progress-step-detail">${esc(s.detail)}</span>` : '';
    return `<div class="progress-step progress-step-${s.status}">
      <span class="progress-step-icon">${icon}</span>
      <span class="progress-step-label">${esc(s.label)}</span>${detail}
    </div>`;
  }).join('');
}

// ── Health tab ────────────────────────────────────────────────────────────────
function switchRcTab(tab) {
  document.getElementById('tab-quote').classList.toggle('active', tab === 'quote');
  document.getElementById('tab-health').classList.toggle('active', tab === 'health');
  const showQuote  = tab === 'quote';
  const quoteVis   = window._lastQuote || fileQueue.some(i => i.status === 'done');
  document.getElementById('results').style.display       = (showQuote && window._lastQuote && fileQueue.length <= 1) ? 'block' : 'none';
  document.getElementById('batch-results').style.display = (showQuote && fileQueue.length > 1 && fileQueue.some(i => i.status === 'done')) ? 'block' : 'none';
  document.getElementById('health-panel').style.display  = tab === 'health' ? 'block' : 'none';
}

function updateHealthTab() {
  const files = fileQueue.filter(i => i.sizeCheck);
  const tab   = document.getElementById('tab-health');
  const hasWarn = files.some(i => i.sizeCheck.overflow_warning || (i.sizeCheck.mesh_warnings||[]).length > 0);
  tab.classList.toggle('has-warn', hasWarn);

  if (!files.length) {
    document.getElementById('health-list').innerHTML = '<div class="health-empty">Run a pre-flight check to see results.</div>';
    return;
  }
  document.getElementById('health-list').innerHTML = files.map(item => {
    const sc   = item.sizeCheck;
    const sz   = sc.size ? `${sc.size.x} × ${sc.size.y} × ${sc.size.z} mm` : null;
    const bv   = sc.build_vol ? `Build volume: ${sc.build_vol.x} × ${sc.build_vol.y} × ${sc.build_vol.z} mm` : null;
    const warns = sc.mesh_warnings || [];
    const hasIssues = sc.overflow_warning || warns.length;
    const statusIcon = hasIssues ? '⚠' : '✓';
    const statusCls  = hasIssues ? 'has-issues' : '';
    const overflowHtml = sc.overflow_warning
      ? `<div class="health-warn">⚠ ${esc(sc.overflow_warning)}</div>` : '';
    const warnHtml = warns.map(w => `<div class="health-warn">⚠ ${esc(w)}</div>`).join('');
    const okHtml   = (!hasIssues) ? `<div class="health-ok">✓ No issues detected</div>` : '';
    return `<div class="health-file ${statusCls}">
      <div class="health-filename">${statusIcon} ${esc(item.file.name)}</div>
      ${sz ? `<div class="health-dims">${sz}${bv ? ' &nbsp;·&nbsp; ' + bv : ''}</div>` : ''}
      ${overflowHtml}${warnHtml}${okHtml}
    </div>`;
  }).join('');
}

function buildFormData(file, settings, sizeOverride, pid) {
  const s  = settings || getFormSettings();
  const fd = new FormData();
  fd.append('file',          file);
  fd.append('printer',       s.printer);
  fd.append('print_profile', s.print_profile);
  fd.append('filament',      s.filament);
  fd.append('size_mode',     s.size_mode);
  fd.append('size_val',      s.size_val);
  fd.append('infill',        s.infill);
  fd.append('layer_height',  s.layer_height);
  fd.append('walls',         s.walls);
  fd.append('supports',      s.supports);
  fd.append('cost_per_kg',   s.cost_per_kg);
  fd.append('hourly_rate',   s.hourly_rate);
  fd.append('markup',        s.markup);
  fd.append('quantity',      s.quantity);
  fd.append('farm_size',     s.farm_size);
  fd.append('time_factor',   s.time_factor);
  if (pid) fd.append('progress_id', pid);
  // sizeOverride from pre-flight check takes precedence over form checkboxes
  const scaleOverride = sizeOverride === 'scale_fit';
  const splitOverride = sizeOverride === 'split';
  fd.append('auto_orient',    s.auto_orient                               ? 'true' : 'false');
  fd.append('auto_split',     (splitOverride || (!scaleOverride && s.auto_split))  ? 'true' : 'false');
  fd.append('auto_scale_fit', (scaleOverride || (!splitOverride && s.auto_scale_fit)) ? 'true' : 'false');
  return fd;
}

function addFilesToQueue(files) {
  for (const f of files) fileQueue.push({file: f, status: 'pending', result: null});
  selectedFile = fileQueue[0]?.file || null;
  renderQueue();
  const btn = document.getElementById('run-btn');
  btn.disabled = false;
  btn.textContent = fileQueue.length > 1 ? 'Quote All' : 'Generate Quote';
}

function renderQueue() {
  const qDiv = document.getElementById('file-queue');
  if (fileQueue.length <= 1) { qDiv.style.display = 'none'; return; }
  qDiv.style.display = 'block';
  document.getElementById('queue-count').textContent = fileQueue.length;
  document.getElementById('queue-list').innerHTML = fileQueue.map((item, i) => {
    const dlBtn = (item.status === 'done' && item.result?.job_id)
      ? `<a class="queue-del" href="/api/export_gcode?job_id=${item.result.job_id}" onclick="event.stopPropagation()" title="Download GCode" style="color:var(--accent2);text-decoration:none">↓</a>`
      : '';
    return `<div class="queue-item st-${item.status}${selectedQueueIdx===i?' selected':''}" onclick="selectQueueItem(${i})">
       <span class="queue-fname" title="${esc(item.file.name)}">${esc(item.file.name)}</span>
       ${item.settings ? '<span class="queue-badge" title="Custom settings">⚙</span>' : ''}
       <span class="queue-status st-${item.status}">${item.status}</span>
       ${dlBtn}
       <button class="queue-del" title="Remove" onclick="event.stopPropagation();deleteQueueItem(${i})">×</button>
     </div>`;
  }).join('');
}

function clearQueue() {
  fileQueue = []; selectedFile = null; selectedQueueIdx = -1;
  window._preflightOversized = [];
  renderQueue();
  const btn = document.getElementById('run-btn');
  btn.disabled = true;
  btn.textContent = 'Generate Quote';
  document.getElementById('file-name').textContent = '';
  document.getElementById('drop-zone').classList.remove('has-file');
  document.getElementById('preflight-panel').style.display = 'none';
}

function minsFromStr(s) {
  if (!s) return 0;
  let t = 0;
  for (const [,v,u] of s.matchAll(/(\\d+)\\s*([dhms])/g)) {
    const n = parseInt(v);
    if (u==='d') t += n*1440; else if (u==='h') t += n*60;
    else if (u==='m') t += n; else if (u==='s') t += n/60;
  }
  return t;
}

function minsToStr(m) {
  const d = Math.floor(m/1440), h = Math.floor((m%1440)/60), mn = Math.floor(m%60);
  if (d) return `${d}d ${h}h ${mn}m`;
  if (h) return `${h}h ${mn}m`;
  return `${mn}m`;
}

async function runQuote() {
  if (fileQueue.length > 1) { await runBatch(); return; }
  // Single file already quoted — show cached result, no re-slice
  if (fileQueue.length === 1 && fileQueue[0].status === 'done') {
    showResults(fileQueue[0].result); return;
  }
  if (!selectedFile) return;
  const btn = document.getElementById('run-btn');
  btn.disabled = true;
  document.getElementById('error-box').style.display    = 'none';
  document.getElementById('results').style.display      = 'none';
  document.getElementById('placeholder').style.display  = 'none';
  document.getElementById('batch-results').style.display = 'none';
  document.getElementById('preflight-panel').style.display = 'none';
  saveInputs();

  // Pre-flight check
  const s = getFormSettings();
  if (s.printer) {
    btn.innerHTML = `<span class="spinner"></span> Checking…`;
    try {
      const fd = new FormData();
      fd.append('file', selectedFile);
      fd.append('printer', s.printer);
      const res  = await fetch('/api/check_size', {method:'POST', body:fd});
      const data = await res.json();
      // Store on queue item for health tab
      if (fileQueue.length === 1) fileQueue[0].sizeCheck = data;
      const hasIssues = data.overflow_warning || (data.mesh_warnings && data.mesh_warnings.length > 0);
      updateHealthTab();
      if (hasIssues) {
        const tempItem = {file: selectedFile, sizeCheck: data, sizeOverride: 'warn', settings: s};
        window._preflightOversized = [tempItem];
        window._preflightCallback  = async () => { await _doSingleQuote(tempItem.sizeOverride); };
        _renderPreflightList([tempItem]);
        document.getElementById('preflight-panel').style.display = 'block';
        document.getElementById('rc-tabs').style.display = 'flex';
        btn.disabled    = false;
        btn.textContent = 'Generate Quote';
        return;
      }
    } catch(e) { console.warn('Pre-flight check failed', e); }
  }

  await _doSingleQuote();
}

async function _doSingleQuote(sizeOverride) {
  const btn = document.getElementById('run-btn');
  btn.disabled = true;
  const pp = document.getElementById('print-profile-sel').value;
  const fp = document.getElementById('filament-sel').value;
  if (pp) localStorage.setItem('lastPrintProfile', pp);
  if (fp) localStorage.setItem('lastFilament', fp);

  _clientSteps = [];
  const pid = makePid();
  showProgressPanel(selectedFile ? selectedFile.name : 'Quoting…');
  btn.innerHTML = `<span class="spinner"></span> Quoting…`;
  startProgressPoll(pid);

  try {
    const res  = await fetch('/api/quote', { method: 'POST', body: buildFormData(selectedFile, null, sizeOverride, pid) });
    stopProgressPoll();
    const data = await res.json();
    hideProgressPanel();
    if (!res.ok) {
      const errMsg = (data.overflow_warning ? data.overflow_warning + '\\n\\n' : '') + (data.error || 'Slicing failed');
      showError(errMsg);
    } else {
      if (fileQueue.length === 1) { fileQueue[0].status = 'done'; fileQueue[0].result = data; renderQueue(); }
      showResults(data);
    }
  } catch (e) {
    stopProgressPoll();
    hideProgressPanel();
    showError(String(e));
  }
  btn.disabled    = false;
  btn.textContent = 'Generate Quote';
}

async function runBatch() {
  const btn = document.getElementById('run-btn');
  btn.disabled = true;
  document.getElementById('error-box').style.display    = 'none';
  document.getElementById('results').style.display      = 'none';
  document.getElementById('placeholder').style.display  = 'none';
  document.getElementById('batch-results').style.display = 'none';
  document.getElementById('preflight-panel').style.display = 'none';
  saveInputs();
  if (selectedQueueIdx >= 0 && selectedQueueIdx < fileQueue.length) {
    fileQueue[selectedQueueIdx].settings = getFormSettings();
  }

  // ── Pre-flight: check sizes for pending files before slicing ──────────────
  const pendingItems = fileQueue.filter(item => item.status !== 'done');
  if (pendingItems.length > 0) {
    _clientSteps = [];
    showProgressPanel('Pre-flight Check', `${pendingItems.length} file${pendingItems.length>1?'s':''}`);
    btn.innerHTML = `<span class="spinner"></span> Checking…`;
    const oversized = [];
    for (const item of pendingItems) {
      const s = item.settings || getFormSettings();
      addProgressStep(item.file.name, 'running', 'checking…');
      if (!s.printer) { addProgressStep(item.file.name, 'done', 'no printer selected'); continue; }
      const fd = new FormData();
      fd.append('file', item.file);
      fd.append('printer', s.printer);
      try {
        const res  = await fetch('/api/check_size', {method:'POST', body:fd});
        const data = await res.json();
        item.sizeCheck = data;
        const issues = [data.overflow_warning, ...(data.mesh_warnings||[])].filter(Boolean);
        const detail = issues.length ? `${issues.length} issue${issues.length>1?'s':''}` : (data.size ? `${data.size.x}×${data.size.y}×${data.size.z} mm` : 'ok');
        addProgressStep(item.file.name, issues.length ? 'error' : 'done', detail);
        if (issues.length) oversized.push(item);
      } catch(e) {
        addProgressStep(item.file.name, 'error', 'check failed');
        console.warn('Size check failed for', item.file.name, e);
      }
    }
    updateHealthTab();
    if (oversized.length > 0) {
      hideProgressPanel();
      window._preflightOversized = oversized;
      window._preflightCallback  = async () => { await _runBatchSlice(); };
      _renderPreflightList(oversized);
      document.getElementById('preflight-panel').style.display = 'block';
      document.getElementById('rc-tabs').style.display = 'flex';
      btn.disabled    = false;
      btn.textContent = 'Quote All';
      return;  // wait for user to click "Continue & Quote"
    }
    hideProgressPanel();
  }

  await _runBatchSlice();
}

function _renderPreflightList(items) {
  document.getElementById('preflight-list').innerHTML = items.map((item, idx) => {
    const sc = item.sizeCheck;
    const sz = sc.size ? `${sc.size.x}×${sc.size.y}×${sc.size.z} mm` : '';
    const bv = sc.build_vol ? `build vol: ${sc.build_vol.x}×${sc.build_vol.y}×${sc.build_vol.z} mm` : '';
    const ov = item.sizeOverride || 'warn';
    const parseFailed = sc.parse_failed === true;
    const hasOverflow = !!sc.overflow_warning;
    const meshWarns   = sc.mesh_warnings || [];
    const meshWarnHtml = meshWarns.length
      ? `<div class="pf-mesh-warns">${meshWarns.map(w => `<div class="pf-mesh-warn">⚠ ${esc(w)}</div>`).join('')}</div>`
      : '';
    const sizeActions = parseFailed
      ? `<button class="pf-btn active" onclick="setPfOverride(${idx},'warn')">Quote Anyway</button>`
      : `<button class="pf-btn${ov==='scale_fit'?' active':''}" onclick="setPfOverride(${idx},'scale_fit')">Scale to Fit</button>
         <button class="pf-btn${ov==='split'?' active':''}" onclick="setPfOverride(${idx},'split')">Auto-Split</button>
         <button class="pf-btn${ov==='warn'?' active':''}" onclick="setPfOverride(${idx},'warn')">Quote Anyway</button>`;
    const actions = (hasOverflow || parseFailed) ? sizeActions
      : `<button class="pf-btn active" onclick="setPfOverride(${idx},'warn')">Quote Anyway</button>`;
    return `<div class="preflight-file" id="pf-${idx}">
      <div class="preflight-info">
        <strong>${esc(item.file.name)}</strong>${sc.overflow_warning ? ' — ' + esc(sc.overflow_warning) : ''}
        <small>${sz}${bv ? ' · ' + bv : ''}</small>
        ${meshWarnHtml}
      </div>
      <div class="preflight-actions">${actions}</div>
    </div>`;
  }).join('');
}

function setPfOverride(idx, val) {
  const item = window._preflightOversized[idx];
  item.sizeOverride = val;
  const row = document.getElementById(`pf-${idx}`);
  row.querySelectorAll('.pf-btn').forEach(b => b.classList.remove('active'));
  row.querySelectorAll('.pf-btn').forEach(b => {
    if (b.textContent.trim() === ({scale_fit:'Scale to Fit', split:'Auto-Split', warn:'Quote Anyway'}[val])) b.classList.add('active');
  });
}

async function confirmPreflight() {
  document.getElementById('preflight-panel').style.display = 'none';
  const cb = window._preflightCallback;
  window._preflightCallback = null;
  if (cb) await cb();
  else await _runBatchSlice();
}

async function _runBatchSlice() {
  const btn = document.getElementById('run-btn');
  btn.disabled = true;
  document.getElementById('batch-results').style.display = 'none';

  const total = fileQueue.filter(i => i.status !== 'done').length;
  let done = 0;

  for (let i = 0; i < fileQueue.length; i++) {
    const item = fileQueue[i];
    if (item.status === 'done') continue;
    done++;
    item.status = 'slicing'; renderQueue();

    _clientSteps = [];
    const pid = makePid();
    showProgressPanel(item.file.name, `File ${done} of ${total}`);
    btn.innerHTML = `<span class="spinner"></span> ${done}/${total} Quoting…`;
    startProgressPoll(pid);

    try {
      const res  = await fetch('/api/quote', {method:'POST', body: buildFormData(item.file, item.settings, item.sizeOverride, pid)});
      stopProgressPoll();
      const data = await res.json();
      item.status = res.ok ? 'done' : 'error';
      item.result = data;
    } catch(e) {
      stopProgressPoll();
      item.status = 'error';
      item.result = {error: String(e), filename: item.file.name};
    }
    hideProgressPanel();
    renderQueue();
  }

  btn.disabled = false;
  btn.textContent = 'Quote All';
  document.getElementById('rc-tabs').style.display = 'flex';
  showBatchResults();
}

function showBatchResults() {
  let totalMins = 0, totalG = 0, totalCost = 0, hasCost = false;
  const rows = fileQueue.map((item, i) => {
    const d = item.result;
    const fname = d?.filename || item.file.name;
    const warnBadge = d?.overflow_warning ? ` <span style="color:#f59e0b;font-size:0.85rem" title="${esc(d.overflow_warning)}">⚠</span>` : '';
    const fnCell = `<td style="max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(fname)}">${esc(fname)}${warnBadge}</td>`;
    if (!d || d.error) {
      return `<tr class="clickable" onclick="openQuoteModal(${i})">${fnCell}<td colspan="3" class="batch-err">${esc(d?.error||'error')}</td><td onclick="event.stopPropagation()">${d?.job_id?`<a href="/api/export_gcode?job_id=${d.job_id}" style="color:var(--accent2);text-decoration:none">↓</a>`:'—'}</td></tr>`;
    }
    const mins = minsFromStr(d.time);
    totalMins += mins;
    totalG    += d.weight_g || 0;
    const cost = d.quote_price ?? d.subtotal ?? null;
    if (cost != null) { totalCost += cost; hasCost = true; }
    const dlLink = d.job_id ? `<a href="/api/export_gcode?job_id=${d.job_id}" title="Download GCode" style="color:var(--accent2);text-decoration:none;font-size:1rem" onclick="event.stopPropagation()">↓</a>` : '—';
    return `<tr class="clickable" onclick="openQuoteModal(${i})">${fnCell}<td>${esc(d.time||'—')}</td><td>${d.weight_g!=null?d.weight_g+' g':'—'}</td><td>${cost!=null?'$'+cost.toFixed(2):'—'}</td><td onclick="event.stopPropagation()">${dlLink}</td></tr>`;
  });
  rows.push(`<tr class="batch-totals"><td>TOTAL (${fileQueue.length})</td><td>${minsToStr(totalMins)}</td><td>${totalG.toFixed(1)} g</td><td>${hasCost?'$'+totalCost.toFixed(2):'—'}</td><td></td></tr>`);
  document.getElementById('batch-tbody').innerHTML = rows.join('');
  // Collect and display overflow warnings prominently
  const warnings = fileQueue
    .filter(item => item.result?.overflow_warning)
    .map(item => `<div class="warn-row" style="margin-bottom:0.4rem">⚠ <strong>${esc(item.file.name)}:</strong> ${esc(item.result.overflow_warning)}</div>`);
  document.getElementById('batch-warnings').innerHTML = warnings.join('');
  document.getElementById('batch-results').style.display = 'block';
}

let _modalQuote = null;
function openQuoteModal(i) {
  const item = fileQueue[i];
  if (!item?.result) return;
  const d = item.result;
  _modalQuote = d;
  document.getElementById('modal-filename').textContent = d.filename || item.file.name;
  const parts = [d.printer, d.print_profile, d.filament].filter(Boolean);
  const extras = [];
  if (d.walls)                             extras.push(`${d.walls} walls`);
  if (d.supports && d.supports !== 'none') extras.push(`${d.supports} supports`);
  document.getElementById('modal-profiles').textContent = [...parts, ...extras].join(' · ') || 'Default settings';

  const warnEl = document.getElementById('modal-warn');
  if (d.overflow_warning) { warnEl.style.display='block'; warnEl.textContent='⚠ '+d.overflow_warning; }
  else                    { warnEl.style.display='none'; }

  const applied = document.getElementById('modal-applied');
  applied.innerHTML = '';
  if (d.orient_note)   applied.innerHTML += `<span class="applied-tag tag-orient">↻ auto-oriented</span>`;
  if (d.split_count>1) applied.innerHTML += `<span class="applied-tag tag-split">✂ split into ${d.split_count} pieces</span>`;

  document.getElementById('modal-time').textContent   = d.time    || '—';
  document.getElementById('modal-weight').textContent = d.weight_g!=null ? d.weight_g+' g' : '—';
  document.getElementById('modal-volume').textContent = d.volume_cm3!=null ? d.volume_cm3+' cm³' : '—';
  document.getElementById('modal-pieces').textContent = d.split_count || 1;

  // Cost table
  const rows = [];
  if (d.material_cost!=null) rows.push(`<tr><td>Material</td><td>$${d.material_cost.toFixed(2)}</td></tr>`);
  if (d.machine_cost!=null)  rows.push(`<tr><td>Machine time</td><td>$${d.machine_cost.toFixed(2)}</td></tr>`);
  if (d.subtotal!=null)      rows.push(`<tr><td>Subtotal</td><td>$${d.subtotal.toFixed(2)}</td></tr>`);
  if (d.markup_amt!=null)    rows.push(`<tr><td>Markup (${d.markup_pct}%)</td><td>$${d.markup_amt.toFixed(2)}</td></tr>`);
  if (d.quote_price!=null)   rows.push(`<tr class="total-row"><td>Quote Price</td><td>$${d.quote_price.toFixed(2)}</td></tr>`);
  document.getElementById('modal-cost-tbody').innerHTML = rows.join('');

  const dlBtn = document.getElementById('modal-dl-gcode');
  dlBtn.href = d.job_id ? `/api/export_gcode?job_id=${d.job_id}` : '/api/export_gcode';

  document.getElementById('quote-modal').classList.add('open');
}
function closeQuoteModal() {
  document.getElementById('quote-modal').classList.remove('open');
}
function copyModalQuote() {
  if (!_modalQuote) return;
  const d = _modalQuote;
  const lines = [
    `File: ${d.filename}`,
    `Printer: ${[d.printer,d.print_profile,d.filament].filter(Boolean).join(' / ')}`,
    `Time: ${d.time||'—'}`,
    `Weight: ${d.weight_g!=null?d.weight_g+' g':'—'}`,
  ];
  if (d.quote_price!=null) lines.push(`Quote: $${d.quote_price.toFixed(2)}`);
  else if (d.subtotal!=null) lines.push(`Subtotal: $${d.subtotal.toFixed(2)}`);
  navigator.clipboard.writeText(lines.join('\\n')).catch(()=>{});
}

function showResults(d) {
  document.getElementById('placeholder').style.display  = 'none';
  document.getElementById('health-panel').style.display = 'none';
  document.getElementById('results').style.display      = 'block';
  document.getElementById('rc-tabs').style.display      = 'flex';
  document.getElementById('tab-quote').classList.add('active');
  document.getElementById('tab-health').classList.remove('active');
  updateHealthTab();

  document.getElementById('res-filename').textContent = d.filename;
  const parts = [d.printer, d.print_profile, d.filament].filter(Boolean);
  const extras = [];
  if (d.walls)                           extras.push(`${d.walls} walls`);
  if (d.supports && d.supports !== 'none') extras.push(`${d.supports} supports`);
  const subtitle = [...parts, ...extras].join(' · ') || 'Default settings';
  document.getElementById('res-profiles').textContent = subtitle;

  // Applied tags
  const appliedRow = document.getElementById('applied-row');
  appliedRow.innerHTML = '';
  if (d.orient_note) {
    appliedRow.innerHTML += `<span class="applied-tag tag-orient">↻ auto-oriented</span>`;
  }
  if (d.split_count > 1) {
    appliedRow.innerHTML += `<span class="applied-tag tag-split">✂ split into ${d.split_count} pieces</span>`;
  }

  // Warnings (overflow, partial errors)
  const warnRow = document.getElementById('warn-row');
  if (d.overflow_warning) {
    warnRow.style.display = 'block';
    warnRow.textContent = '⚠ ' + d.overflow_warning;
  } else if (d.errors && d.errors.length > 0) {
    warnRow.style.display = 'block';
    warnRow.textContent = '⚠ Some pieces failed to slice: ' + d.errors.join('; ');
  } else {
    warnRow.style.display = 'none';
  }

  document.getElementById('res-time').textContent = d.time || '—';
  document.getElementById('res-size-sub').textContent =
    d.raw_time ? `slicer est: ${d.raw_time} × ${d.time_factor}` : (d.size_val ? d.size_val + ' (' + d.size_mode + ')' : '');

  document.getElementById('res-weight').textContent = d.weight_g != null ? d.weight_g + ' g' : '—';
  document.getElementById('res-volume').textContent  = d.volume_cm3 != null ? d.volume_cm3 + ' cm³' : '';

  if (d.length_mm != null) {
    document.getElementById('res-length').textContent   = Math.round(d.length_mm) + ' mm';
    document.getElementById('res-length-m').textContent = (d.length_mm / 1000).toFixed(2) + ' m';
  }

  document.getElementById('res-pieces').textContent = d.split_count || 1;
  document.getElementById('res-pieces-sub').textContent = d.split_count > 1
    ? 'print separately' : 'single print';

  // Pieces breakdown
  const piecesSection = document.getElementById('pieces-section');
  const piecesList    = document.getElementById('pieces-list');
  if (d.split_count > 1 && d.pieces && d.pieces.length > 0) {
    piecesSection.style.display = 'block';
    piecesList.innerHTML = d.pieces.map(p => `
      <div class="piece-row">
        <div class="piece-label">${esc(p.label)}</div>
        <div class="piece-time">${esc(p.time)}</div>
        <div class="piece-weight">${p.weight_g} g</div>
      </div>`).join('');
  } else {
    piecesSection.style.display = 'none';
  }

  // Cost table
  const tbody = document.getElementById('cost-tbody');
  tbody.innerHTML = '';
  const rows = [];
  if (d.material_cost != null) {
    const note = d.cost_per_kg ? `@ $${d.cost_per_kg}/kg` : 'from profile';
    rows.push(['Material', `$${d.material_cost.toFixed(2)}`, note]);
  }
  if (d.machine_cost != null) {
    const mins = Math.round(parseTimeToMins(d.time));
    rows.push(['Machine time', `$${d.machine_cost.toFixed(2)}`, `${mins} min @ $${d.hourly_rate}/hr`]);
  }
  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td style="color:var(--muted);font-size:.82rem;padding:.5rem 0" colspan="2">Add $/kg and $/hr above to see cost breakdown.</td></tr>';
  } else {
    for (const [label, val, note] of rows) {
      tbody.innerHTML += `<tr><td>${label} <span class="muted">${note}</span></td><td>${val}</td></tr>`;
    }
    if (d.markup_pct && d.markup_amt != null) {
      tbody.innerHTML += `<tr><td>Markup (${d.markup_pct}%)</td><td>+$${d.markup_amt.toFixed(2)}</td></tr>`;
    }
    if (d.quote_price != null) {
      tbody.innerHTML += `<tr class="total-row"><td>Quote Price</td><td>$${d.quote_price.toFixed(2)}</td></tr>`;
    } else if (d.subtotal != null) {
      tbody.innerHTML += `<tr class="total-row"><td>Total Cost</td><td>$${d.subtotal.toFixed(2)}</td></tr>`;
    }
  }

  // Farm time
  const farmRow = document.getElementById('farm-row');
  if (d.farm_size > 1 && d.farm_time) {
    const batches = Math.ceil((d.quantity||1) / d.farm_size);
    farmRow.textContent = '';
    farmRow.innerHTML = `<span>Farm · ${d.farm_size} printers · ${batches} batch${batches!==1?'es':''}</span><span>Wall-clock: <strong>${esc(d.farm_time)}</strong></span>`;
    farmRow.style.display = 'flex';
    farmRow.style.justifyContent = 'space-between';
  } else {
    farmRow.style.display = 'none';
  }

  // Qty row
  const qtyBox = document.getElementById('qty-box');
  if (d.quantity > 1) {
    document.getElementById('qty-n').textContent      = d.quantity;
    document.getElementById('qty-time').textContent   = d.qty_time || '—';
    document.getElementById('qty-weight').textContent = d.qty_weight_g != null ? d.qty_weight_g + ' g' : '—';
    qtyBox.style.display = 'flex';
  } else {
    qtyBox.style.display = 'none';
  }

  window._lastQuote = d;
}

function showError(msg) {
  document.getElementById('placeholder').style.display = 'none';
  const box = document.getElementById('error-box');
  box.style.display = 'block';
  box.textContent = '⚠ ' + msg;
}

function copyQuote() {
  const d = window._lastQuote;
  if (!d) return;
  const lines = [`3D Print Quote — ${d.filename}`, `─────────────────────────────`];
  if (d.printer)       lines.push(`Printer:   ${d.printer}`);
  if (d.print_profile) lines.push(`Profile:   ${d.print_profile}`);
  if (d.filament)      lines.push(`Filament:  ${d.filament}`);
  if (d.orient_note)   lines.push(`Orient:    applied (${d.orient_note})`);
  if (d.split_count > 1) lines.push(`Pieces:    ${d.split_count} (split to fit build volume)`);
  lines.push('');
  lines.push(`Print time:  ${d.time}`);
  if (d.weight_g != null)      lines.push(`Weight:      ${d.weight_g} g`);
  if (d.volume_cm3 != null)    lines.push(`Volume:      ${d.volume_cm3} cm³`);
  if (d.material_cost != null) lines.push(`Material:    $${d.material_cost.toFixed(2)}`);
  if (d.machine_cost  != null) lines.push(`Machine:     $${d.machine_cost.toFixed(2)}`);
  if (d.markup_pct)            lines.push(`Markup ${d.markup_pct}%: +$${d.markup_amt.toFixed(2)}`);
  if (d.quote_price != null)   lines.push(`\\nQUOTE PRICE: $${d.quote_price.toFixed(2)}`);
  else if (d.subtotal != null) lines.push(`\\nTOTAL COST:  $${d.subtotal.toFixed(2)}`);
  if (d.farm_size > 1 && d.farm_time) {
    lines.push(`Farm (${d.farm_size} printers): ${d.farm_time} wall-clock`);
  }
  if (d.quantity > 1) {
    lines.push('');
    lines.push(`Qty ${d.quantity} — Total time: ${d.qty_time}, Total weight: ${d.qty_weight_g} g`);
  }
  if (d.split_count > 1 && d.pieces) {
    lines.push('\\nPieces:');
    for (const p of d.pieces) lines.push(`  ${p.label}: ${p.time}, ${p.weight_g} g`);
  }
  navigator.clipboard.writeText(lines.join('\\n')).then(() => {
    const btn = document.querySelector('.copy-btn');
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy Quote', 1500);
  });
}

async function download3mf() {
  const btn = document.getElementById('dl-3mf-btn');
  btn.textContent = 'Exporting…';
  btn.disabled = true;
  try {
    const res = await fetch('/api/export_3mf', { method: 'POST' });
    if (!res.ok) {
      const data = await res.json();
      alert('Export failed: ' + (data.error || res.statusText));
      return;
    }
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    const cd   = res.headers.get('Content-Disposition') || '';
    const m    = cd.match(/filename="?([^"]+)"?/);
    a.download = m ? m[1] : 'quote.3mf';
    a.href = url;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert('Export error: ' + e);
  } finally {
    btn.textContent = 'Download 3MF';
    btn.disabled = false;
  }
}

// ── Presets ──────────────────────────────────────────────────────────────────
let presetsCache = {};

async function loadPresetsList() {
  try {
    const res = await fetch('/api/presets');
    presetsCache = await res.json();
  } catch { presetsCache = {}; }
  const sel = document.getElementById('preset-sel');
  sel.innerHTML = '<option value="">— saved presets —</option>' +
    Object.keys(presetsCache).sort().map(n =>
      `<option value="${esc(n)}">${esc(n)}</option>`).join('');
}

function loadPreset() {
  const name = document.getElementById('preset-sel').value;
  if (!name || !presetsCache[name]) return;
  const s = presetsCache[name];
  const printerSel = document.getElementById('printer-sel');
  const applyProfiles = () => {
    if (s.print_profile) document.getElementById('print-profile-sel').value = s.print_profile;
    if (s.filament)      document.getElementById('filament-sel').value = s.filament;
  };
  if (s.printer && printerSel.value !== s.printer) {
    printerSel.value = s.printer;
    loadProfiles().then(applyProfiles);
  } else { applyProfiles(); }
  if (s.infill)       document.getElementById('infill').value = s.infill;
  if (s.layer_height) document.getElementById('layer-height').value = s.layer_height;
  if (s.walls)        document.getElementById('walls').value = s.walls;
  if (s.supports)     setSupportMode(s.supports);
  if (s.cost_per_kg)  document.getElementById('cost-per-kg').value = s.cost_per_kg;
  if (s.hourly_rate)  document.getElementById('hourly-rate').value = s.hourly_rate;
  if (s.markup)       document.getElementById('markup').value = s.markup;
  if (s.farm_size)    document.getElementById('farm-size').value = s.farm_size;
}

async function savePreset() {
  const name = prompt('Preset name:');
  if (!name || !name.trim()) return;
  const body = {
    name:          name.trim(),
    printer:       document.getElementById('printer-sel').value,
    print_profile: document.getElementById('print-profile-sel').value,
    filament:      document.getElementById('filament-sel').value,
    infill:        document.getElementById('infill').value,
    layer_height:  document.getElementById('layer-height').value,
    walls:         document.getElementById('walls').value,
    supports:      supportMode,
    cost_per_kg:   document.getElementById('cost-per-kg').value,
    hourly_rate:   document.getElementById('hourly-rate').value,
    markup:        document.getElementById('markup').value,
    farm_size:     document.getElementById('farm-size').value,
  };
  try {
    const res = await fetch('/api/presets', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    if (!res.ok) throw new Error('Server returned ' + res.status);
    await loadPresetsList();
    document.getElementById('preset-sel').value = body.name;
  } catch(e) {
    alert('Failed to save preset: ' + e.message);
  }
}

async function deletePreset() {
  const name = document.getElementById('preset-sel').value;
  if (!name) return;
  if (!confirm(`Delete preset "${name}"?`)) return;
  try {
    const res = await fetch('/api/presets/' + encodeURIComponent(name), {method:'DELETE'});
    if (!res.ok) throw new Error('Server returned ' + res.status);
    await loadPresetsList();
  } catch(e) {
    alert('Failed to delete preset: ' + e.message);
  }
}

const PERSIST_IDS = ['cost-per-kg', 'hourly-rate', 'markup', 'quantity', 'infill', 'layer-height', 'walls', 'time-factor', 'farm-size'];
function saveInputs() {
  for (const id of PERSIST_IDS) { const el = document.getElementById(id); if (el && el.value) localStorage.setItem('input_' + id, el.value); }
}
function restoreInputs() {
  for (const id of PERSIST_IDS) { const val = localStorage.getItem('input_' + id); if (val) document.getElementById(id).value = val; }
  // Restore support mode
  const savedSupport = localStorage.getItem('supportMode');
  if (savedSupport) setSupportMode(savedSupport);
  // Restore checkbox states
  for (const id of ['auto-orient', 'auto-split']) {
    const saved = localStorage.getItem('chk_' + id);
    if (saved === 'true') {
      document.getElementById(id).checked = true;
      toggleFeature(id === 'auto-orient' ? 'orient-row' : 'split-row', id);
    }
  }
}

// Save checkbox states on change
document.getElementById('auto-orient').addEventListener('change', function() {
  localStorage.setItem('chk_auto-orient', this.checked);
});
document.getElementById('auto-split').addEventListener('change', function() {
  localStorage.setItem('chk_auto-split', this.checked);
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeQuoteModal(); });

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function parseTimeToMins(t) {
  if (!t) return 0;
  let total = 0;
  for (const m of String(t).matchAll(/(\\d+)\\s*([dhms])/g)) {
    const v = parseInt(m[1]);
    if (m[2]==='d') total+=v*1440; else if (m[2]==='h') total+=v*60; else if (m[2]==='m') total+=v; else if (m[2]==='s') total+=v/60;
  }
  return total;
}

init();
</script>

<!-- Individual quote modal -->
<div id="quote-modal" onclick="if(event.target===this)closeQuoteModal()">
  <div id="quote-modal-inner">
    <button id="quote-modal-close" onclick="closeQuoteModal()">×</button>
    <div id="modal-filename" class="result-filename" style="margin-bottom:0.25rem">—</div>
    <div id="modal-profiles" class="result-sub" style="margin-bottom:0.75rem">—</div>
    <div id="modal-warn" class="warn-row" style="display:none;margin-bottom:0.75rem"></div>
    <div id="modal-applied" class="applied-row" style="margin-bottom:0.75rem"></div>
    <div class="stat-grid" style="margin-bottom:0.75rem">
      <div class="stat-box"><div class="stat-label">Print Time</div><div class="stat-value" id="modal-time">—</div></div>
      <div class="stat-box"><div class="stat-label">Weight</div><div class="stat-value accent" id="modal-weight">—</div></div>
      <div class="stat-box"><div class="stat-label">Volume</div><div class="stat-value" id="modal-volume">—</div></div>
      <div class="stat-box"><div class="stat-label">Pieces</div><div class="stat-value" id="modal-pieces">1</div></div>
    </div>
    <hr class="divider" style="margin:0.75rem 0">
    <div class="card-title" style="margin-bottom:0.5rem">Cost Breakdown</div>
    <table class="cost-table"><tbody id="modal-cost-tbody"></tbody></table>
    <div style="display:flex;gap:0.5rem;margin-top:1rem;flex-wrap:wrap">
      <a id="modal-dl-gcode" class="copy-btn" href="#" style="text-decoration:none;padding:0.35rem 0.7rem">Download GCode</a>
      <button class="copy-btn" onclick="copyModalQuote()">Copy Quote</button>
    </div>
  </div>
</div>
<!-- Debug panel -->
<button id="debug-btn" onclick="toggleDebug()" title="Error log">🐛 Debug</button>
<div id="debug-panel">
  <div class="debug-header">
    <div class="debug-header-title">Error Log</div>
    <button class="debug-clear-btn" onclick="clearDebugLog()">Clear</button>
    <button class="debug-close-btn" onclick="toggleDebug()">×</button>
  </div>
  <div id="debug-entries"></div>
</div>

<script>
let _debugOpen = false;

async function loadDebugLog() {
  try {
    const res = await fetch('/api/debug_log');
    const entries = await res.json();
    const btn = document.getElementById('debug-btn');
    btn.classList.toggle('has-errors', entries.length > 0);
    btn.textContent = entries.length > 0 ? `🐛 Debug (${entries.length})` : '🐛 Debug';
    const el = document.getElementById('debug-entries');
    if (!entries.length) {
      el.innerHTML = '<div class="debug-empty">No errors logged.</div>';
      return;
    }
    el.innerHTML = entries.map((e, i) => `
      <div class="debug-entry">
        <div class="debug-entry-head" onclick="toggleDebugEntry(${i})">
          <span class="debug-ts">${esc(e.timestamp)}</span>
          <span class="debug-ep">${esc(e.endpoint)}</span>
          <span class="debug-msg">${esc(e.type)}: ${esc(e.error)}</span>
          <span class="debug-toggle" id="dbg-tog-${i}">▼</span>
        </div>
        <div class="debug-entry-body" id="dbg-body-${i}">
          <pre class="debug-tb">${esc(e.traceback||'')}</pre>
        </div>
      </div>`).join('');
  } catch(err) {
    document.getElementById('debug-entries').innerHTML = '<div class="debug-empty">Could not load log.</div>';
  }
}

function toggleDebugEntry(i) {
  const body = document.getElementById(`dbg-body-${i}`);
  const tog  = document.getElementById(`dbg-tog-${i}`);
  const open = body.classList.toggle('open');
  tog.textContent = open ? '▲' : '▼';
}

function toggleDebug() {
  _debugOpen = !_debugOpen;
  document.getElementById('debug-panel').classList.toggle('open', _debugOpen);
  if (_debugOpen) loadDebugLog();
}

async function clearDebugLog() {
  await fetch('/api/debug_log', {method:'DELETE'});
  loadDebugLog();
}

// Poll for new errors every 10s so the button badge stays current
setInterval(() => {
  fetch('/api/debug_log').then(r => r.json()).then(entries => {
    const btn = document.getElementById('debug-btn');
    btn.classList.toggle('has-errors', entries.length > 0);
    btn.textContent = entries.length > 0 ? `🐛 Debug (${entries.length})` : '🐛 Debug';
    if (_debugOpen) loadDebugLog();
  }).catch(()=>{});
}, 10000);
</script>
</body>
</html>
"""


def open_browser():
    import time
    time.sleep(0.8)
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    print(f"Starting 3D Print Quoting GUI at http://localhost:{PORT}")
    print("Press Ctrl+C to stop.\n")
    threading.Thread(target=open_browser, daemon=True).start()
    host = "0.0.0.0" if os.environ.get("DOCKER") else "127.0.0.1"
    app.run(host=host, port=PORT, debug=False, threaded=True)
