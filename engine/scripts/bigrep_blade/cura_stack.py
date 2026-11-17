"""Resolve BigRep BLADE (Cura-based) settings for one machine/nozzle/material/layer stack.

Usage:
    python3 cura_stack.py --blade <BLADE resources dir> --material bigrep_pro_ht_nature --layer 0.3 \
        [--quality pro_ht] [--intent visual] [--nozzle "0.6 mm"] key [key ...]

<BLADE resources dir> is BLADE's Cura resources folder (definitions/, extruders/, variants/, quality/,
intent/, materials/). Nothing from BLADE is shipped with this script.
"""

import argparse
import configparser
import json
import math
import os
import xml.etree.ElementTree as ET

MACHINE = "bigrep_studio_2"
NS = "{http://www.ultimaker.com/material}"

XML_MAP = {
    "print temperature": "default_material_print_temperature",
    "heated bed temperature": "default_material_bed_temperature",
    "standby temperature": "material_standby_temperature",
    "print cooling": "cool_fan_speed",
    "retraction amount": "retraction_amount",
    "retraction speed": "retraction_speed",
    "retraction retraction speed": "retraction_retract_speed",
    "retraction prime speed": "retraction_prime_speed",
    "retraction extra prime amount": "retraction_extra_prime_amount",
    "retraction hop": "retraction_hop",
    "adhesion tendency": "material_adhesion_tendency",
    "surface energy": "material_surface_energy",
    "build volume temperature": "build_volume_temperature",
    "max material feedrate": "br_max_material_feedrate",
    "support interface desity": "support_interface_density",
    "support top distance": "support_top_distance",
    "support bottom distance": "support_bottom_distance",
    "brim line count": "brim_line_count",
    "support angle": "support_angle",
    "adhesion type": "adhesion_type",
    "status": "br_status",
    "material flow": "material_flow",
    "material flow layer 0": "material_flow_layer_0",
    "support infill rate": "support_infill_rate",
    "support interface pattern": "support_interface_pattern",
}
XML_SKIP = {"hardware compatible"}


def _flatten(tree, out):
    for key, value in tree.items():
        out[key] = value
        if "children" in value:
            _flatten(value["children"], out)
    return out


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def overrides(path):
    out = {}
    for key, value in _load_json(path).get("overrides", {}).items():
        if "value" in value:
            out[key] = "=" + str(value["value"])
        elif "default_value" in value:
            out[key] = value["default_value"]
    return out


def cfg_text_values(text):
    c = configparser.ConfigParser(interpolation=None)
    c.optionxform = str
    c.read_string(text)
    return dict(c["values"]) if c.has_section("values") else {}, dict(c["metadata"]) if c.has_section("metadata") else {}


def cfg_values(path):
    with open(path, encoding="utf-8") as f:
        return cfg_text_values(f.read())


class Blade:
    def __init__(self, root):
        self.root = root
        self.defs = _flatten(_load_json(self.path("definitions", "fdmprinter.def.json"))["settings"], {})
        _flatten(_load_json(self.path("definitions", "fdmextruder.def.json"))["settings"], self.defs)

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def definition(self):
        return _load_json(self.path("definitions", f"{MACHINE}.def.json"))

    def cast(self, key, value):
        if not isinstance(value, str) or value.startswith("="):
            return value
        kind = self.defs.get(key, {}).get("type")
        try:
            if kind == "float":
                return float(value)
            if kind == "int":
                return int(float(value))
            if kind == "bool":
                return value.strip() in ("True", "true", "1", "yes")
            if kind in ("optional_extruder", "extruder"):
                return int(value)
        except ValueError:
            return value
        return value

    def material_file(self, name):
        return self.path("materials", f"{name}.xml.fdm_material")

    def material(self, name, hotend="0.6 mm"):
        root = ET.parse(self.material_file(name)).getroot()
        values, unmapped = {}, set()

        def take(node):
            for s in node.findall(f"{NS}setting"):
                key = s.get("key")
                if key in XML_SKIP:
                    continue
                if key not in XML_MAP:
                    unmapped.add(key)
                    continue
                values[XML_MAP[key]] = (s.text or "").strip()

        settings = root.find(f"{NS}settings")
        take(settings)
        matched = False
        for machine in settings.findall(f"{NS}machine"):
            if any(mi.get("product") == MACHINE for mi in machine.findall(f"{NS}machine_identifier")):
                matched = True
                take(machine)
                for h in machine.findall(f"{NS}hotend"):
                    if h.get("id") == hotend:
                        take(h)
        props = root.find(f"{NS}properties")
        values["material_diameter"] = props.find(f"{NS}diameter").text
        values["material_density"] = props.find(f"{NS}density").text
        meta = root.find(f"{NS}metadata/{NS}name")
        info = {t: (meta.find(NS + t).text if meta.find(NS + t) is not None else "") for t in ("brand", "material", "color")}
        return values, matched, unmapped, info

    def quality_layers(self, quality, nozzle="0.6 mm"):
        prefix = f"{MACHINE}_{nozzle.split()[0]}_mm_{quality}_"
        layers = []
        folder = self.path("quality", MACHINE)
        for name in sorted(os.listdir(folder)):
            if name.startswith(prefix) and name.endswith("_polyimide.inst.cfg"):
                layer = name[len(prefix):-len("_polyimide.inst.cfg")]
                if layer.replace(".", "", 1).isdigit():
                    layers.append(layer)
        return layers

    def stack(self, material, layer, nozzle="0.6 mm", quality=None, intent=None, quality_changes=None):
        tag = nozzle.split()[0]
        defs = overrides(self.path("definitions", f"{MACHINE}.def.json"))
        ext_defs = [overrides(self.path("extruders", f"{MACHINE}_extruder_{i}.def.json")) for i in (0, 1)]
        variant = cfg_values(self.path("variants", f"{MACHINE}_dual_extruder_{tag.replace('.', '')}.inst.cfg"))[0]
        plate = cfg_values(self.path("variants", f"{MACHINE}_polyimide.inst.cfg"))[0]
        global_quality = cfg_values(self.path("quality", MACHINE, f"{MACHINE}_global_{layer}.inst.cfg"))[0]
        mat, matched, unmapped, info = self.material(material, hotend=nozzle)
        mq, it = {}, {}
        if quality:
            p = self.path("quality", MACHINE, f"{MACHINE}_{tag}_mm_{quality}_{layer}_polyimide.inst.cfg")
            if os.path.exists(p):
                mq = cfg_values(p)[0]
            if intent:
                p = self.path("intent", MACHINE, f"{MACHINE}_{tag}_mm_{quality}_{intent}_{layer}_polyimide.inst.cfg")
                if os.path.exists(p):
                    it = cfg_values(p)[0]
        qc_global, qc_extruder = quality_changes or ({}, {})
        extruder = lambda i: [qc_extruder if i == 0 else {}, it, mq, mat, variant, ext_defs[i]]
        resolver = Resolver(self, [qc_global, global_quality, plate, defs], [extruder(0), extruder(1)])
        return resolver, dict(matched=matched, unmapped=unmapped, info=info, quality=bool(mq), intent=bool(it))


class Resolver:
    def __init__(self, blade, global_stack, extruder_stacks):
        self.blade = blade
        self.defs = blade.defs
        self.g = [{k: blade.cast(k, v) for k, v in c.items()} for c in global_stack]
        self.e = [[{k: blade.cast(k, v) for k, v in c.items()} for c in s] for s in extruder_stacks]
        self.cache, self.busy, self.errors, self.resolving = {}, set(), {}, set()

    def default(self, key):
        d = self.defs[key]
        if "value" in d:
            return "=" + str(d["value"])
        return self.blade.cast(key, d.get("default_value"))

    def raw(self, key, ext):
        for container in (self.e[ext] if ext is not None else []) + self.g:
            if key in container:
                return container[key]
        return self.default(key)

    def get(self, key, ext=None):
        if key not in self.defs:
            raise KeyError(key)
        d = self.defs[key]
        if ext is not None and not d.get("settable_per_extruder", True):
            ext = None
        if ext is not None and "limit_to_extruder" in d:
            limit = self.evaluate(str(d["limit_to_extruder"]), ext, key + "#limit")
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = -1
            if limit not in (-1, ext) and 0 <= limit < len(self.e):
                ext = limit
        cache_key = (key, ext, key in self.resolving)
        if cache_key in self.cache:
            return self.cache[cache_key]
        if cache_key in self.busy:
            self.errors.setdefault(key + "#cycle", "cycle, used literal default")
            return self.blade.cast(key, d.get("default_value"))
        self.busy.add(cache_key)
        try:
            if ext is None and "resolve" in d and key not in self.resolving and not any(key in c for c in self.g[:1]):
                self.resolving.add(key)
                try:
                    value = self.evaluate(d["resolve"], None, key)
                finally:
                    self.resolving.discard(key)
            else:
                value = self.raw(key, ext)
                if isinstance(value, str) and value.startswith("="):
                    value = self.evaluate(value[1:], ext, key)
            if isinstance(value, str) and not value.startswith("="):
                value = self.blade.cast(key, value)
            if d.get("type") == "float" and isinstance(value, (int, float)) and not isinstance(value, bool):
                value = float(value)
        finally:
            self.busy.discard(cache_key)
        self.cache[cache_key] = value
        return value

    def evaluate(self, expr, ext, key):
        r = self

        def first_extruder_with(k):
            for i in range(len(r.e)):
                if r.get(k, i):
                    return i
            return 0

        funcs = {
            "extruderValue": lambda e, k: r.get(k, None if int(e) < 0 else int(e)),
            "extruderValues": lambda k: [r.get(k, i) for i in range(len(r.e))],
            "resolveOrValue": lambda k: r.get(k, None),
            "defaultExtruderPosition": lambda: "0",
            "anyExtruderWithMaterial": first_extruder_with,
            "anyExtruderNrWithOrDefault": first_extruder_with,
            "valueFromContainer": lambda k, idx: r.get(k, ext),
            "extruderValueFromContainer": lambda e, k, idx: r.get(k, int(e)),
        }

        class Scope(dict):
            def __missing__(self, name):
                if name in funcs:
                    return funcs[name]
                if name in r.defs:
                    return r.get(name, ext)
                raise KeyError(name)

        try:
            return eval(expr, {"__builtins__": __builtins__, "math": math}, Scope())
        except Exception as err:
            self.errors[key] = f"{type(err).__name__}: {err} in {expr[:120]}"
            return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--blade", required=True)
    parser.add_argument("--material", required=True)
    parser.add_argument("--layer", required=True)
    parser.add_argument("--quality")
    parser.add_argument("--intent")
    parser.add_argument("--nozzle", default="0.6 mm")
    parser.add_argument("keys", nargs="+")
    args = parser.parse_args()
    resolver, meta = Blade(args.blade).stack(args.material, args.layer, args.nozzle, args.quality, args.intent)
    for key in args.keys:
        print(f"{key} = {resolver.get(key, 0)!r}")
    for key, err in resolver.errors.items():
        if not key.endswith("#cycle"):
            print(f"error {key}: {err}")
    if meta["unmapped"]:
        print("unmapped material keys:", ", ".join(sorted(meta["unmapped"])))


if __name__ == "__main__":
    main()
