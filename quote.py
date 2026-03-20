#!/usr/bin/env python3
"""
3D Print Quoting Tool
Slices a model via PrusaSlicer CLI and outputs print time, weight, and cost info.

Usage:
  python quote.py model.stl --size 100,50,30 --printer "LNL3D D3 (0.4 mm nozzle)" --print-profile "0.20 mm NORMAL" --filament "Standard PLA"
  python quote.py model.stl --scale 150% --filament "Generic PETG @LNL3D"
  python quote.py --list-profiles
  python quote.py --list-profiles --printer "LNL3D D3 (0.4 mm nozzle)"
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PRUSASLICER = "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"

# Filament density fallbacks (g/cm³) if not in profile
DENSITY_DEFAULTS = {
    "pla": 1.24,
    "petg": 1.27,
    "abs": 1.04,
    "asa": 1.07,
    "tpu": 1.21,
    "tpe": 1.20,
    "nylon": 1.15,
    "pa": 1.15,
    "pc": 1.20,
    "pla+": 1.24,
    "pla-cf": 1.30,
    "petg-cf": 1.30,
}


def run_slicer(*args, capture=True):
    cmd = [PRUSASLICER] + list(args)
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
    )
    return result


def list_profiles(printer_profile=None):
    if printer_profile:
        result = run_slicer(
            "--query-print-filament-profiles",
            "--printer-profile", printer_profile,
        )
        try:
            # Strip stderr log lines that appear before the JSON
            json_start = result.stdout.find("{")
            data = json.loads(result.stdout[json_start:])
            print(f"\nPrinter: {data.get('printer_profile')}\n")
            print("Print profiles:")
            for p in data.get("print_profiles", []):
                print(f"  {p['name']}")
            print("\nBundled filament profiles:")
            seen = set()
            for p in data.get("print_profiles", []):
                for f in p.get("filament_profiles", []):
                    if f not in seen:
                        print(f"  {f}")
                        seen.add(f)
            print("\nUser filament profiles:")
            seen = set()
            for p in data.get("print_profiles", []):
                for f in p.get("user_filament_profiles", []):
                    if f not in seen:
                        print(f"  {f}")
                        seen.add(f)
        except Exception as e:
            print(f"Error parsing profiles: {e}")
            print(result.stdout[:500])
    else:
        result = run_slicer("--query-printer-models")
        json_start = result.stdout.find("{")
        try:
            data = json.loads(result.stdout[json_start:])
            print("\nAvailable printer profiles:")
            for model in data.get("printer_models", []):
                for variant in model.get("variants", []):
                    for profile in variant.get("printer_profiles", []):
                        print(f"  {profile['name']}")
        except Exception as e:
            print(f"Error: {e}")
            print(result.stdout[:500])
        print("\nRun with --list-profiles --printer \"<name>\" to see print/filament profiles for a printer.")


def parse_size(size_str):
    """Parse size string: '100,50,30' (mm) or '150%' or '1.5x' or '0.5'"""
    s = size_str.strip()
    if "," in s:
        parts = [float(p) for p in s.split(",")]
        if len(parts) == 2:
            parts.append(0)  # 0 = unconstrained Z
        return ("fit", parts)
    if s.endswith("%"):
        return ("scale", float(s[:-1]))
    if s.lower().endswith("x"):
        return ("scale", float(s[:-1]) * 100)
    # Bare number treated as scale factor (1.5 = 150%)
    try:
        v = float(s)
        if v <= 10:  # Likely a multiplier
            return ("scale", v * 100)
        return ("scale", v)  # Likely a percentage
    except ValueError:
        raise ValueError(f"Cannot parse size: {size_str!r}. Use 'X,Y,Z' (mm), '150%', or '1.5x'")


def parse_gcode_stats(gcode_path):
    """Extract print time, filament usage, and cost from sliced gcode."""
    stats = {
        "time_normal": None,
        "time_silent": None,
        "filament_mm": [],
        "filament_cm3": [],
        "filament_g": [],
        "filament_cost": [],
    }

    time_pattern = re.compile(
        r";\s*estimated printing time \((\w+(?: \w+)?)\s*mode\)\s*=\s*(.+)"
    )
    mm_pattern = re.compile(r";\s*filament used \[mm\]\s*=\s*(.+)")
    cm3_pattern = re.compile(r";\s*filament used \[cm3\]\s*=\s*(.+)")
    g_pattern = re.compile(r";\s*filament used \[g\]\s*=\s*(.+)")
    cost_pattern = re.compile(r";\s*filament cost\s*=\s*(.+)")

    with open(gcode_path) as f:
        for line in f:
            m = time_pattern.match(line)
            if m:
                mode, t = m.group(1).strip(), m.group(2).strip()
                if "normal" in mode.lower():
                    stats["time_normal"] = t
                elif "silent" in mode.lower():
                    stats["time_silent"] = t
                else:
                    stats["time_normal"] = t
                continue
            m = mm_pattern.match(line)
            if m:
                stats["filament_mm"] = [float(x) for x in m.group(1).split(",")]
                continue
            m = cm3_pattern.match(line)
            if m:
                stats["filament_cm3"] = [float(x) for x in m.group(1).split(",")]
                continue
            m = g_pattern.match(line)
            if m:
                stats["filament_g"] = [float(x) for x in m.group(1).split(",")]
                continue
            m = cost_pattern.match(line)
            if m:
                stats["filament_cost"] = [float(x) for x in m.group(1).split(",")]

    return stats


def parse_time_to_minutes(t):
    """Convert '1h 23m 45s' to total minutes."""
    total = 0
    for match in re.finditer(r"(\d+)\s*([hms])", t):
        val, unit = int(match.group(1)), match.group(2)
        if unit == "h":
            total += val * 60
        elif unit == "m":
            total += val
        elif unit == "s":
            total += val / 60
    return total


def format_time(t_str):
    return t_str if t_str else "unknown"


def quote(args):
    input_file = Path(args.file)
    if not input_file.exists():
        print(f"Error: File not found: {input_file}")
        sys.exit(1)

    suffix = input_file.suffix.lower()
    if suffix not in (".stl", ".obj", ".3mf", ".amf"):
        print(f"Warning: Unexpected file type {suffix!r}, proceeding anyway.")

    # Build slicer command
    cmd_args = []

    # Printer profile
    if args.printer:
        cmd_args += ["--printer-profile", args.printer]

    # Print quality profile
    if args.print_profile:
        cmd_args += ["--print-profile", args.print_profile]

    # Filament profile (PrusaSlicer uses --material-profile)
    if args.filament:
        cmd_args += ["--material-profile", args.filament]

    # Extra config overrides
    if args.infill:
        cmd_args += ["--fill-density", str(args.infill)]
    if args.layer_height:
        cmd_args += ["--layer-height", str(args.layer_height)]

    # Scaling / sizing
    if args.size:
        mode, value = parse_size(args.size)
        if mode == "fit":
            x, y, z = value
            if z > 0:
                cmd_args += ["--scale-to-fit", f"{x},{y},{z}"]
            else:
                cmd_args += ["--scale-to-fit", f"{x},{y},9999"]
        else:
            cmd_args += ["--scale", str(value)]

    with tempfile.TemporaryDirectory() as tmpdir:
        out_gcode = os.path.join(tmpdir, "output.gcode")
        cmd_args += ["--export-gcode", "--output", out_gcode]
        cmd_args += [str(input_file)]

        print(f"Slicing {input_file.name}...")
        if args.verbose:
            print("Command:", " ".join([PRUSASLICER] + cmd_args))

        result = run_slicer(*cmd_args)

        if result.returncode != 0 or not os.path.exists(out_gcode):
            print("\nSlicer error:")
            for line in result.stderr.splitlines():
                if "[error]" in line.lower() or "error" in line.lower():
                    print(" ", line)
            if args.verbose:
                print(result.stderr)
            sys.exit(1)

        stats = parse_gcode_stats(out_gcode)

    # --- Output ---
    print()
    print("=" * 50)
    print(f"  QUOTE: {input_file.name}")
    print("=" * 50)

    if args.size:
        mode, value = parse_size(args.size)
        if mode == "fit":
            x, y, z = value
            dim_str = f"{x} x {y}" + (f" x {z}" if z > 0 else "") + " mm"
            print(f"  Size (target):   {dim_str}")
        else:
            print(f"  Scale:           {value:.1f}%")

    if args.printer:
        print(f"  Printer:         {args.printer}")
    if args.print_profile:
        print(f"  Print profile:   {args.print_profile}")
    if args.filament:
        print(f"  Filament:        {args.filament}")
    if args.infill:
        print(f"  Infill:          {args.infill}%")

    print()
    print("  --- Print Stats ---")

    time_str = format_time(stats["time_normal"])
    print(f"  Print time:      {time_str}")

    if stats["time_silent"]:
        print(f"  (silent mode):   {stats['time_silent']}")

    total_g = sum(stats["filament_g"]) if stats["filament_g"] else None
    total_cm3 = sum(stats["filament_cm3"]) if stats["filament_cm3"] else None
    total_cost = sum(stats["filament_cost"]) if stats["filament_cost"] else None

    if total_g is not None:
        print(f"  Filament weight: {total_g:.1f} g")
    if total_cm3 is not None:
        print(f"  Filament volume: {total_cm3:.1f} cm³")
    if stats["filament_mm"]:
        print(f"  Filament length: {sum(stats['filament_mm']):.0f} mm ({sum(stats['filament_mm'])/1000:.2f} m)")

    # Cost estimate
    print()
    print("  --- Cost Estimate ---")

    cost_per_kg = args.cost_per_kg
    if cost_per_kg and total_g:
        material_cost = (total_g / 1000) * cost_per_kg
        print(f"  Material cost:   ${material_cost:.2f}  (@ ${cost_per_kg:.2f}/kg)")
    elif total_cost:
        print(f"  Material cost:   ${total_cost:.2f}  (from filament profile)")
    else:
        print(f"  Material cost:   (set --cost-per-kg to calculate)")

    if args.hourly_rate and stats["time_normal"]:
        minutes = parse_time_to_minutes(stats["time_normal"])
        machine_cost = (minutes / 60) * args.hourly_rate
        print(f"  Machine cost:    ${machine_cost:.2f}  ({minutes:.0f} min @ ${args.hourly_rate:.2f}/hr)")

        if cost_per_kg and total_g:
            total = material_cost + machine_cost
        elif total_cost:
            total = total_cost + machine_cost
        else:
            total = machine_cost

        if args.markup:
            quoted = total * (1 + args.markup / 100)
            print(f"  Subtotal:        ${total:.2f}")
            print(f"  Markup ({args.markup:.0f}%):     ${quoted - total:.2f}")
            print(f"  ─────────────────────────────")
            print(f"  QUOTE PRICE:     ${quoted:.2f}")
        else:
            print(f"  ─────────────────────────────")
            print(f"  TOTAL COST:      ${total:.2f}")

    if args.quantity and args.quantity > 1:
        print(f"\n  Quantity:        {args.quantity}")
        # Recalculate with quantity (time scales, material scales)
        if stats["time_normal"]:
            mins = parse_time_to_minutes(stats["time_normal"]) * args.quantity
            h, m = divmod(int(mins), 60)
            s = int((mins % 1) * 60)
            time_qty = f"{h}h {m}m" if h else f"{m}m {s}s"
            print(f"  Total time:      {time_qty}")
        if total_g:
            print(f"  Total weight:    {total_g * args.quantity:.1f} g")

    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(
        description="3D print quoting tool using PrusaSlicer CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument("file", nargs="?", help="Input STL/OBJ/3MF file")
    parser.add_argument(
        "--size", "-s",
        help="Target size as 'X,Y,Z' mm (fit to box), '150%%' (scale), or '1.5x' (multiplier)",
    )
    parser.add_argument(
        "--scale",
        help="Scale percentage (e.g. 150 for 150%%). Alternative to --size.",
    )
    parser.add_argument(
        "--printer", "-p",
        help='Printer profile name (e.g. "LNL3D D3 (0.4 mm nozzle)")',
    )
    parser.add_argument(
        "--print-profile", "-q",
        help='Print quality profile (e.g. "0.20 mm NORMAL (0.4 mm nozzle) @LNL3D")',
    )
    parser.add_argument(
        "--filament", "-f",
        help='Filament profile name (e.g. "Standard PLA" or "Generic PETG @LNL3D")',
    )
    parser.add_argument(
        "--infill", "-i",
        type=float,
        help="Infill percentage override (e.g. 20)",
    )
    parser.add_argument(
        "--layer-height", "-l",
        type=float,
        help="Layer height override in mm (e.g. 0.2)",
    )
    parser.add_argument(
        "--cost-per-kg", "-c",
        type=float,
        help="Filament cost per kg in dollars (e.g. 25.00)",
    )
    parser.add_argument(
        "--hourly-rate", "-r",
        type=float,
        help="Machine hourly rate in dollars (e.g. 3.50)",
    )
    parser.add_argument(
        "--markup", "-m",
        type=float,
        help="Markup percentage to add to cost (e.g. 30 for 30%%)",
    )
    parser.add_argument(
        "--quantity", "-n",
        type=int,
        default=1,
        help="Number of copies (time and material scale linearly)",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="List available printer/print/filament profiles and exit",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show slicer command and full output",
    )

    args = parser.parse_args()

    # Handle --scale as alias for --size
    if args.scale and not args.size:
        s = str(args.scale)
        if not s.endswith("%"):
            s += "%"
        args.size = s

    if args.list_profiles:
        list_profiles(args.printer)
        return

    if not args.file:
        parser.print_help()
        sys.exit(1)

    quote(args)


if __name__ == "__main__":
    main()
