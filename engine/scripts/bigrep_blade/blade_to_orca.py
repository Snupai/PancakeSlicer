"""Generate the BigRep STUDIO.2 Orca profiles from BigRep BLADE's resources.

Usage (from the repo root):
    python3 engine/scripts/bigrep_blade/blade_to_orca.py --blade <BLADE resources dir> \
        [--user-profile hi-temp_0.3_support-adhesion_test.curaprofile] \
        [--platform studio.3mf] [--image STUDIO2.png] [--profiles engine/resources/profiles]

<BLADE resources dir> is BLADE's Cura resources folder (definitions/, extruders/, variants/, quality/,
intent/, materials/). The script resolves BLADE's own setting stack (definition -> extruder -> variant ->
material -> quality, plus intent and quality_changes for --user-profile), rewrites
<profiles>/BigRep/{machine,process,filament} and BigRep.json, and then runs orca_profile_tool.py
normalize, update-index and generate-id for the BigRep vendor. --platform writes the bed STL from
BLADE's platform mesh, --image the 240x240 cover (needs Pillow). Existing bed/cover files are kept
when those options are omitted. The nozzle head envelope is not touched.
"""

import argparse
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cura_stack import Blade, cfg_text_values  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
VENDOR = "BigRep"
MODEL = "BigRep STUDIO.2"
MACHINE = "BigRep STUDIO.2 0.6 nozzle"
NOZZLE = "0.6 mm"
REFERENCE = "Pro HT"
LAYERS = ["0.1", "0.2", "0.3", "0.4", "0.5"]
BED_MODEL = "bigrep_studio_2_buildplate_model.stl"
COVER = f"{MODEL}_cover.png"
COVER_SIZE = 240

MATERIALS = {
    "HI-TEMP": ("bigrep_hi_temp_nature", "hi_temp", "HI-TEMP", "PLA"),
    "HI-TEMP-CF": ("bigrep_hi_temp_cf_black", "hi_temp_cf", "HI-TEMP CF", "PLA-CF"),
    "PLA": ("bigrep_pla_white", "pla", "PLA", "PLA"),
    "rPLA": ("bigrep_r_pla_nature", "r_pla", "rPLA", "PLA"),
    "PLX": ("bigrep_plx_white", "plx", "PLX", "PLA"),
    "PETG": ("bigrep_petg_transparent", "petg", "PETG", "PETG"),
    "rPETG": ("bigrep_r_petg_white", "r_petg", "rPETG", "PETG"),
    "Pro HT": ("bigrep_pro_ht_nature", "pro_ht", "PRO HT", "PLA"),
    "ASA": ("bigrep_asa_white", "asa", "ASA", "ASA"),
    "BVOH": ("bigrep_bvoh_nature", "bvoh", "BVOH", "BVOH"),
    "TPU": ("bigrep_tpu_black", "tpu", "TPU", "TPU"),
    "PA6/66": ("bigrep_pa666_black", "pa666", "PA6-66", "PA"),
}

START_GCODE = ["T0", "M140 S[bed_temperature_initial_layer_single]", "M105", "M190 S[bed_temperature_initial_layer_single]",
               "M104 S[nozzle_temperature_initial_layer]", "M105", "M109 S[nozzle_temperature_initial_layer]",
               "M82 ;absolute extrusion mode", "M800", "G28", "G92 E0"]
END_GCODE = ["M140 S0", "M107", "G92 E0", "G1 E-5.0000 F1800", "G91", "G1 Z10 F4000", "G90", "G1 X5 F40000", "G1 Y5 F40000",
             "M104 S0 H0", "M104 S0 H1", "M107 P0", "M107 P1", "M140 S0", "M84", "M801", "M82 ;absolute extrusion mode", "M104 S0"]
CHANGE_GCODE = ["{if previous_extruder >= 0 and idle_temperature[previous_extruder] > 0}"
                "M104 S{idle_temperature[previous_extruder]} H{previous_extruder}",
                "{endif}M104 S{new_filament_temp} H{next_extruder}", "M109 S{new_filament_temp} H{next_extruder}",
                "T{next_extruder}"]

PATTERN = {"zigzag": "zigzag", "grid": "grid", "lines": "line", "triangles": "triangles", "cubic": "cubic", "gyroid": "gyroid"}
SKIN = {"zigzag": "rectilinear", "lines": "monotonicline", "concentric": "concentric"}
SUPPORT_BASE = {"zigzag": "rectilinear", "lines": "rectilinear", "grid": "rectilinear-grid", "triangles": "rectilinear-grid"}
SUPPORT_INTERFACE = {"zigzag": "rectilinear", "lines": "rectilinear", "concentric": "concentric", "grid": "grid"}


class Values:
    def __init__(self, resolver, material_file):
        self.resolver, self.material_file = resolver, material_file

    def __getitem__(self, key):
        return self.resolver.get(key, 0)


def filament_name(label):
    return f"{VENDOR} {label} @STUDIO.2"


def num(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        r = round(v, 4)
        return str(int(r)) if r == int(r) else str(r)
    return str(v)


def two(v):
    return [num(v), num(v)]


def spacing(width, density):
    return 0.0 if density >= 100 else round(width * 100.0 / density - width, 2)


def process_values(r):
    lh = r["layer_height"]
    roofing = bool(r["roofing_layer_count"]) and r["roofing_layer_count"] > 0
    out = {
        "layer_height": num(lh),
        "initial_layer_print_height": num(r["layer_height_0"]),
        "line_width": num(r["line_width"]),
        "outer_wall_line_width": num(r["wall_line_width_0"]),
        "inner_wall_line_width": num(r["wall_line_width_x"]),
        "sparse_infill_line_width": num(r["infill_line_width"]),
        "internal_solid_infill_line_width": num(r["skin_line_width"]),
        "top_surface_line_width": num(r["roofing_line_width"] if roofing else r["skin_line_width"]),
        "support_line_width": num(r["support_line_width"]),
        "initial_layer_line_width": num(r["line_width"] * r["initial_layer_line_width_factor"] / 100.0),
        "wall_loops": num(r["wall_line_count"]),
        "wall_generator": "classic" if r["br_disable_arachne"] else "arachne",
        "top_shell_layers": num(r["top_layers"]),
        "bottom_shell_layers": num(r["bottom_layers"]),
        "top_shell_thickness": num(r["top_thickness"]),
        "bottom_shell_thickness": num(r["bottom_thickness"]),
        "top_surface_pattern": SKIN[r["roofing_pattern"] if r["roofing_layer_count"] else r["top_bottom_pattern"]],
        "internal_solid_infill_pattern": SKIN[r["top_bottom_pattern"]],
        "bottom_surface_pattern": SKIN[r["top_bottom_pattern_0"]],
        "sparse_infill_density": f"{num(r['infill_sparse_density'])}%",
        "sparse_infill_pattern": PATTERN[r["infill_pattern"]],
        "infill_wall_overlap": f"{num(r['infill_overlap'])}%",
        "top_bottom_infill_wall_overlap": f"{num(r['skin_overlap'])}%",
        "seam_position": "aligned",
        "outer_wall_speed": num(r["speed_wall_0"]),
        "inner_wall_speed": num(r["speed_wall_x"]),
        "sparse_infill_speed": num(r["speed_infill"]),
        "internal_solid_infill_speed": num(r["speed_topbottom"]),
        "top_surface_speed": num(r["speed_roofing"] if roofing else r["speed_topbottom"]),
        "support_speed": num(r["speed_support_infill"]),
        "support_interface_speed": num(r["speed_support_roof"]),
        "travel_speed": num(r["speed_travel"]),
        "initial_layer_speed": num(r["speed_print_layer_0"]),
        "initial_layer_infill_speed": num(r["speed_print_layer_0"]),
        "initial_layer_travel_speed": num(r["speed_travel_layer_0"]),
        "skirt_speed": num(r["skirt_brim_speed"]),
        "slow_down_layers": num(r["speed_slowdown_layers"]),
        "bridge_speed": num(r["bridge_skin_speed"] if r["bridge_settings_enabled"] else r["speed_topbottom"]),
        "internal_bridge_speed": "100%",
        "reduce_crossing_wall": "1" if r["retraction_combing"] != "off" else "0",
        "brim_type": ("outer_only" if r["brim_location"] == "outside" else "outer_and_inner") if r["adhesion_type"] == "brim" else "no_brim",
        "brim_width": num(r["brim_width"]),
        "brim_object_gap": num(r["brim_gap"]),
        "skirt_loops": num(r["skirt_line_count"]) if r["adhesion_type"] == "skirt" else "0",
        "skirt_distance": num(r["skirt_gap"]),
        "enable_support": num(r["support_enable"]),
        "support_type": "tree(auto)" if r["support_structure"] == "tree" else "normal(auto)",
        "support_on_build_plate_only": "1" if r["support_type"] == "buildplate" else "0",
        "support_threshold_angle": num(int(round(90 - r["support_angle"]))),
        "support_base_pattern": SUPPORT_BASE[r["support_pattern"]],
        "support_base_pattern_spacing": num(spacing(r["support_line_width"], r["support_infill_rate"])),
        "support_interface_pattern": SUPPORT_INTERFACE[r["support_roof_pattern"]],
        "support_interface_spacing": num(spacing(r["support_interface_line_width"], r["support_roof_density"])),
        "support_interface_top_layers": num(int(round(r["support_roof_height"] / lh)) if r["support_roof_enable"] else 0),
        "support_interface_bottom_layers": num(int(round(r["support_bottom_height"] / lh)) if r["support_bottom_enable"] else 0),
        "support_top_z_distance": num(r["support_top_distance"]),
        "support_bottom_z_distance": num(r["support_bottom_distance"]),
        "support_object_xy_distance": num(r["support_xy_distance"]),
        "enable_prime_tower": num(r["prime_tower_enable"]),
        "prime_tower_width": num(r["prime_tower_size"]),
        "prime_volume": num(r["prime_tower_min_volume"]),
    }
    if not r["acceleration_enabled"]:
        for key in ("default_acceleration", "outer_wall_acceleration", "inner_wall_acceleration", "top_surface_acceleration",
                    "initial_layer_acceleration", "travel_acceleration", "sparse_infill_acceleration",
                    "internal_solid_infill_acceleration", "bridge_acceleration"):
            out[key] = "0"
    if not r["jerk_enabled"]:
        for key in ("default_jerk", "outer_wall_jerk", "inner_wall_jerk", "infill_jerk", "top_surface_jerk", "initial_layer_jerk", "travel_jerk"):
            out[key] = "0"
    return out


def filament_values(r, blade, machine):
    temp = int(round(r["material_print_temperature"]))
    temp0 = int(round(r["material_print_temperature_layer_0"]))
    bed, bed0 = num(r["material_bed_temperature"]), num(r["material_bed_temperature_layer_0"])
    density = ET.parse(blade.material_file(r.material_file)).getroot().find(
        "{http://www.ultimaker.com/material}properties/{http://www.ultimaker.com/material}density").text
    out = {
        "filament_vendor": [VENDOR],
        "filament_diameter": [num(r["material_diameter"])],
        "filament_density": [density],
        "nozzle_temperature": [str(temp)],
        "nozzle_temperature_initial_layer": [str(temp0)],
        "nozzle_temperature_range_low": [str(min(temp, temp0))],
        "nozzle_temperature_range_high": [str(max(temp, temp0))],
        "idle_temperature": [num(int(round(r["material_standby_temperature"])))],
        "filament_flow_ratio": [num(r["material_flow"] / 100.0)],
        "filament_max_volumetric_speed": [num(round(r["br_maximum_extruder_throughput"], 2))],
        "filament_shrink": [f"{num(round(100 * 100 / r['material_shrinkage_percentage_xy'], 2))}%"],
        "filament_shrinkage_compensation_z": [f"{num(round(100 * 100 / r['material_shrinkage_percentage_z'], 2))}%"],
        "fan_min_speed": [num(r["cool_fan_speed_min"] if r["cool_fan_enabled"] else 0)],
        "fan_max_speed": [num(r["cool_fan_speed_max"] if r["cool_fan_enabled"] else 0)],
        "slow_down_for_layer_cooling": ["1"],
        "reduce_fan_stop_start_freq": ["1" if r["cool_fan_enabled"] and r["cool_fan_speed_min"] > 0 else "0"],
        "enable_overhang_bridge_fan": ["1" if r["bridge_settings_enabled"] else "0"],
        "slow_down_layer_time": [num(r["cool_min_layer_time"])],
        "fan_cooling_layer_time": [num(r["cool_min_layer_time_fan_speed_max"])],
        "slow_down_min_speed": [num(r["cool_min_speed"])],
        "full_fan_speed_layer": [num(r["cool_fan_full_layer"])],
        "close_fan_the_first_x_layers": ["1" if r["cool_fan_speed_0"] == 0 else "0"],
    }
    for plate in ("cool_plate_temp", "eng_plate_temp", "hot_plate_temp", "textured_plate_temp"):
        out[plate] = [bed]
        out[plate + "_initial_layer"] = [bed0]
    for key, cura in (("filament_retraction_length", "retraction_amount"), ("filament_retraction_speed", "retraction_retract_speed"),
                      ("filament_deretraction_speed", "retraction_prime_speed"),
                      ("filament_retract_restart_extra", "retraction_extra_prime_amount"), ("filament_z_hop", "retraction_hop"),
                      ("filament_retraction_minimum_travel", "retraction_min_travel")):
        if abs(r[cura] - machine[cura]) > 1e-9:
            out[key] = [num(r[cura])]
    if r["material_is_support_material"] or r.material_file.startswith("bigrep_bvoh"):
        out["filament_soluble"] = ["1"]
        out["filament_is_support"] = ["1"]
    return out


def machine_values(m):
    return {
        "nonplanar_head": "bigrep-studio-2",
        "gcode_flavor": "marlin",
        "use_relative_e_distances": "1" if m["relative_extrusion"] else "0",
        "emit_machine_limits_to_gcode": "0",
        "single_extruder_multi_material": "0",
        "printable_area": ["0x0", f"{num(m['machine_width'])}x0", f"{num(m['machine_width'])}x{num(m['machine_depth'])}",
                           f"0x{num(m['machine_depth'])}"],
        "printable_height": num(m["machine_height"]),
        "extruder_clearance_radius": "113",
        "extruder_clearance_height_to_rod": "345",
        "extruder_clearance_height_to_lid": "345",
        "nozzle_diameter": two(m["machine_nozzle_size"]),
        "extruder_colour": ["#FCE94F", "#3465A4"],
        "extruder_offset": [f"{num(m['machine_nozzle_offset_x'])}x{num(m['machine_nozzle_offset_y'])}"] * 2,
        "min_layer_height": two(float(LAYERS[0])),
        "max_layer_height": two(float(LAYERS[-1])),
        "retraction_length": two(m["retraction_amount"]),
        "retraction_speed": two(m["retraction_retract_speed"]),
        "deretraction_speed": two(m["retraction_prime_speed"]),
        "retract_restart_extra": two(m["retraction_extra_prime_amount"]),
        "retraction_minimum_travel": two(m["retraction_min_travel"]),
        "retract_when_changing_layer": two(m["retract_at_layer_change"]),
        "wipe": two(False),
        "z_hop": two(m["retraction_hop"] if m["retraction_hop_enabled"] else 0),
        "z_hop_types": ["Spiral Lift"] * 2 if m["br_z_hop_type"] == "spiral" else ["Normal Lift"] * 2,
        "retract_lift_above": two(0),
        "retract_lift_below": two(0),
        "retract_length_toolchange": two(m["switch_extruder_retraction_amount"]),
        "retract_restart_extra_toolchange": two(m["switch_extruder_extra_prime_amount"]),
        "machine_max_speed_x": two(m["machine_max_feedrate_x"]),
        "machine_max_speed_y": two(m["machine_max_feedrate_y"]),
        "machine_max_speed_z": two(m["machine_max_feedrate_z"]),
        "machine_max_acceleration_x": two(m["machine_max_acceleration_x"]),
        "machine_max_acceleration_y": two(m["machine_max_acceleration_y"]),
        "machine_max_acceleration_z": two(m["machine_max_acceleration_z"]),
        "machine_max_acceleration_e": two(m["machine_max_acceleration_e"]),
        "machine_max_acceleration_extruding": two(m["machine_acceleration"]),
        "machine_max_acceleration_travel": two(m["machine_acceleration"]),
        "machine_max_acceleration_retracting": two(m["machine_max_acceleration_e"]),
        "machine_max_jerk_x": two(m["machine_max_jerk_xy"]),
        "machine_max_jerk_y": two(m["machine_max_jerk_xy"]),
        "machine_max_jerk_z": two(m["machine_max_jerk_z"]),
        "machine_max_jerk_e": two(m["machine_max_jerk_e"]),
        "machine_tool_change_time": num(m["machine_extruder_start_code_duration"] + m["machine_extruder_end_code_duration"]),
        "machine_start_gcode": "\n".join(START_GCODE),
        "machine_end_gcode": "\n".join(END_GCODE),
        "change_filament_gcode": "\n".join(CHANGE_GCODE),
        "machine_pause_gcode": "M0",
        "default_filament_profile": [filament_name(MATERIALS[REFERENCE][2])],
    }


def read_user_profile(path):
    with zipfile.ZipFile(path) as z:
        parts = [cfg_text_values(z.read(n).decode("utf-8")) + (z.read(n).decode("utf-8"),) for n in z.namelist()]
    qc_global, qc_extruder, name, quality_type, intent = {}, {}, None, None, None
    for values, meta, text in parts:
        name = name or re.search(r"^name\s*=\s*(.+)$", text, re.M).group(1).strip()
        if "position" not in meta:
            qc_global, quality_type = values, meta.get("quality_type")
        elif meta["position"] == "0":
            qc_extruder, intent = values, meta.get("intent_category")
    layer = quality_type.replace("_", ".")
    label = " ".join(w for w in name.split() if w != layer)
    return dict(global_values=qc_global, extruder_values=qc_extruder, layer=layer, intent=None if intent == "default" else intent,
                process=f"{float(layer):.2f}mm {label} @{MODEL}", filament=filament_name(label))


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent="\t", ensure_ascii=False) + "\n")


def platform_stl(blade, platform_path, out_path):
    meta = blade.definition()["metadata"]
    ox, oy, oz = meta.get("platform_offset", [0, 0, 0])
    with zipfile.ZipFile(platform_path) as z:
        text = z.read("3D/3dmodel.model").decode("utf-8")
    vertices = [tuple(map(float, v)) for v in re.findall(r'<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"', text)]
    triangles = [tuple(map(int, t)) for t in re.findall(r'<triangle v1="(\d+)" v2="(\d+)" v3="(\d+)"', text)]
    item = re.search(r'<item[^>]*transform="([^"]+)"', text)
    m = list(map(float, item.group(1).split())) if item else [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]
    rotated = [(x * m[0] + y * m[3] + z * m[6], x * m[1] + y * m[4] + z * m[7], x * m[2] + y * m[5] + z * m[8]) for x, y, z in vertices]
    cura = [(x, z, -y) for x, y, z in rotated]
    centre = [(min(p[i] for p in cura) + max(p[i] for p in cura)) / 2 for i in range(3)]
    printer = [(x - centre[0] + ox, -(z - centre[2] + oz), y - centre[1] + oy) for x, y, z in cura]
    kept = [t for t in triangles if max(printer[i][2] for i in t) <= 1e-6]
    if not kept:
        raise SystemExit("the platform mesh has nothing below the build surface")
    top = max(printer[i][2] for t in kept for i in t)
    buf = io.BytesIO()
    buf.write(b"BigRep STUDIO.2 bed from BLADE studio.3mf".ljust(80, b"\0"))
    buf.write(struct.pack("<I", len(kept)))
    for t in kept:
        a, b, c = (printer[i] for i in t)
        u = [b[k] - a[k] for k in range(3)]
        v = [c[k] - a[k] for k in range(3)]
        n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
        length = sum(k * k for k in n) ** 0.5 or 1.0
        buf.write(struct.pack("<3f", *(k / length for k in n)))
        for p in (a, b, c):
            buf.write(struct.pack("<3f", p[0], p[1], p[2] - top))
        buf.write(b"\0\0")
    with open(out_path, "wb") as f:
        f.write(buf.getvalue())
    return len(kept)


def cover_png(image_path, out_path):
    from PIL import Image
    image = Image.open(image_path).convert("RGBA")
    image.thumbnail((COVER_SIZE, COVER_SIZE), Image.LANCZOS)
    canvas = Image.new("RGBA", (COVER_SIZE, COVER_SIZE), (0, 0, 0, 0))
    canvas.paste(image, ((COVER_SIZE - image.width) // 2, (COVER_SIZE - image.height) // 2), image)
    canvas.save(out_path, optimize=True)


def run_profile_tool(profiles):
    tool = os.path.join(REPO, "engine", "scripts", "orca_profile_tool.py")
    for command in ("normalize", "update-index", "generate-id"):
        subprocess.run([sys.executable, tool, command, "--vendor", VENDOR, "--profiles", profiles], check=True, cwd=os.path.join(REPO, "engine"))


def generate(blade, profiles, user_profile=None, platform=None, image=None, profile_tool=True):
    vendor_dir = os.path.join(profiles, VENDOR)
    for sub in ("machine", "process", "filament"):
        shutil.rmtree(os.path.join(vendor_dir, sub), ignore_errors=True)
    os.makedirs(vendor_dir, exist_ok=True)
    if platform:
        platform_stl(blade, platform, os.path.join(vendor_dir, BED_MODEL))
    if image:
        cover_png(image, os.path.join(vendor_dir, COVER))

    def resolve(material, layer, intent=None, quality_changes=None):
        file, quality = MATERIALS[material][:2]
        resolver, _ = blade.stack(file, layer, NOZZLE, quality, intent, quality_changes)
        return Values(resolver, file)

    machine = resolve(REFERENCE, "0.3")
    processes = [(f"{float(layer):.2f}mm Standard @{MODEL}", layer) for layer in LAYERS]
    filaments = [(filament_name(label), material, ftype) for material, (_, _, label, ftype) in MATERIALS.items()]
    user = read_user_profile(user_profile) if user_profile else None

    process_names = [name for name, _ in processes] + ([user["process"]] if user else [])
    filament_names = [name for name, _, _ in filaments] + ([user["filament"]] if user else [])
    entries = lambda kind, names: [{"name": n, "sub_path": f"{kind}/{n}.json"} for n in names]
    write_json(os.path.join(profiles, f"{VENDOR}.json"), {
        "name": VENDOR, "version": "02.04.00.02", "force_update": "0",
        "description": "BigRep configurations (ported from BigRep BLADE 5.17.4)",
        "machine_model_list": [{"name": MODEL, "sub_path": f"machine/{MODEL}.json"}],
        "machine_list": entries("machine", ["fdm_machine_common", "fdm_bigrep_studio_2_common", MACHINE]),
        "process_list": entries("process", ["fdm_process_common"] + process_names),
        "filament_list": entries("filament", ["fdm_filament_common"] + filament_names),
    })
    write_json(os.path.join(vendor_dir, "machine", f"{MODEL}.json"), {
        "type": "machine_model", "name": MODEL, "model_id": "BigRep-STUDIO-2", "nozzle_diameter": "0.6", "machine_tech": "FFF",
        "family": VENDOR, "bed_model": BED_MODEL if os.path.exists(os.path.join(vendor_dir, BED_MODEL)) else "",
        "bed_texture": "", "hotend_model": "", "default_materials": ";".join(name for name, _, _ in filaments),
    })
    write_json(os.path.join(vendor_dir, "machine", "fdm_machine_common.json"), {
        "type": "machine", "name": "fdm_machine_common", "from": "system", "instantiation": "false", "printer_technology": "FFF",
        "gcode_flavor": "marlin", "printer_settings_id": "", "default_print_profile": "", "thumbnails": [""],
        "before_layer_change_gcode": ";BEFORE_LAYER_CHANGE\n;[layer_z]\n", "layer_change_gcode": ";AFTER_LAYER_CHANGE\n;[layer_z]",
    })
    common = {"type": "machine", "name": "fdm_bigrep_studio_2_common", "inherits": "fdm_machine_common", "from": "system",
              "instantiation": "false"}
    common.update(machine_values(machine))
    write_json(os.path.join(vendor_dir, "machine", "fdm_bigrep_studio_2_common.json"), common)
    write_json(os.path.join(vendor_dir, "machine", f"{MACHINE}.json"), {
        "type": "machine", "name": MACHINE, "inherits": "fdm_bigrep_studio_2_common", "from": "system", "instantiation": "true",
        "printer_model": MODEL, "printer_variant": "0.6", "default_print_profile": f"0.30mm Standard @{MODEL}",
    })

    write_json(os.path.join(vendor_dir, "process", "fdm_process_common.json"), {
        "type": "process", "name": "fdm_process_common", "from": "system", "instantiation": "false",
        "compatible_printers": [], "compatible_printers_condition": "", "print_settings_id": "",
        "filename_format": "{input_filename_base}_{filament_type[initial_tool]}_{print_time}.gcode",
    })

    def process(name, values):
        data = {"type": "process", "name": name, "inherits": "fdm_process_common", "from": "system", "instantiation": "true"}
        data.update(process_values(values))
        data["compatible_printers"] = [MACHINE]
        write_json(os.path.join(vendor_dir, "process", f"{name}.json"), data)

    for name, layer in processes:
        process(name, resolve(REFERENCE, layer))

    write_json(os.path.join(vendor_dir, "filament", "fdm_filament_common.json"), {
        "type": "filament", "name": "fdm_filament_common", "from": "system", "instantiation": "false",
        "filament_settings_id": [""], "compatible_printers": [], "compatible_printers_condition": "",
    })

    def filament(name, values, ftype):
        data = {"type": "filament", "name": name, "inherits": "fdm_filament_common", "from": "system", "instantiation": "true",
                "filament_type": [ftype]}
        data.update(filament_values(values, blade, machine))
        data["compatible_printers"] = [MACHINE]
        write_json(os.path.join(vendor_dir, "filament", f"{name}.json"), data)

    for name, material, ftype in filaments:
        filament(name, resolve(material, "0.3"), ftype)

    if user:
        values = resolve("HI-TEMP", user["layer"], user["intent"], (user["global_values"], user["extruder_values"]))
        process(user["process"], values)
        filament(user["filament"], values, MATERIALS["HI-TEMP"][3])

    if profile_tool:
        run_profile_tool(profiles)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--blade", required=True, help="BLADE Cura resources folder")
    parser.add_argument("--profiles", default=os.path.join(REPO, "engine", "resources", "profiles"))
    parser.add_argument("--user-profile", help="a BLADE .curaprofile for HI-TEMP to add as an extra process and filament")
    parser.add_argument("--platform", help="BLADE's studio.3mf platform mesh")
    parser.add_argument("--image", help="BLADE's STUDIO2.png printer image")
    parser.add_argument("--skip-profile-tool", action="store_true", help="do not run orca_profile_tool.py afterwards")
    args = parser.parse_args()
    generate(Blade(args.blade), os.path.abspath(args.profiles), args.user_profile, args.platform, args.image,
             not args.skip_profile_tool)


if __name__ == "__main__":
    main()
