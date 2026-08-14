#!/usr/bin/env python3
"""
Regenerate regmap.json from the upstream register definitions.

Parses TypQxQ/Sigenergy-Local-Modbus's modbusregisterdefinitions.py with `ast`
(it imports homeassistant, so it can't just be imported) and flattens the
register dataclasses, alarm-code appendices and IntEnums into plain JSON.

Usage:
  regmap_gen.py [path-to-modbusregisterdefinitions.py]

With no argument it fetches the file from GitHub main into a temp path via curl.
"""
import ast
import json
import os
import subprocess
import sys
import tempfile

RAW_URL = ("https://raw.githubusercontent.com/TypQxQ/Sigenergy-Local-Modbus/main/"
           "custom_components/sigen/modbusregisterdefinitions.py")

GROUPS = ["PLANT_RUNNING_INFO_REGISTERS", "PLANT_PARAMETER_REGISTERS",
          "PLANT_ESS_PREHEATING_REGISTERS", "INVERTER_RUNNING_INFO_REGISTERS",
          "INVERTER_PARAMETER_REGISTERS", "AC_CHARGER_RUNNING_INFO_REGISTERS",
          "AC_CHARGER_PARAMETER_REGISTERS", "DC_CHARGER_RUNNING_INFO_REGISTERS",
          "DC_CHARGER_PARAMETER_REGISTERS"]

ENUM_CLASSES = ["RunningState", "EMSWorkMode", "RemoteEMSControlMode", "OutputType",
                "ACChargerSystemState", "DCChargerRunningState"]

# Home Assistant unit constants -> plain strings
UNITS = {
    "UnitOfPower.KILO_WATT": "kW", "UnitOfPower.WATT": "W",
    "UnitOfEnergy.KILO_WATT_HOUR": "kWh", "UnitOfEnergy.WATT_HOUR": "Wh",
    "UnitOfElectricCurrent.AMPERE": "A", "UnitOfElectricCurrent.MILLIAMPERE": "mA",
    "UnitOfElectricPotential.VOLT": "V", "UnitOfFrequency.HERTZ": "Hz",
    "UnitOfTemperature.CELSIUS": "degC", "PERCENTAGE": "%",
}
RTYPE = {"READ_ONLY": "ro", "HOLDING": "rw", "WRITE_ONLY": "wo"}


def lit(node):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute):
        name = ast.unparse(node)
        if name.startswith(("RegisterType.", "DataType.")):
            return name.split(".", 1)[1]
        return UNITS.get(name, name)
    if isinstance(node, ast.Name):
        return UNITS.get(node.id, node.id)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -lit(node.operand)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [lit(e) for e in node.elts]
    return ast.unparse(node)


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        path = os.path.join(tempfile.gettempdir(), "sigen_regdefs.py")
        subprocess.run(["curl", "-sfL", "-o", path, RAW_URL], check=True)
        print(f"fetched {RAW_URL}\n     -> {path}")

    with open(path) as fh:
        tree = ast.parse(fh.read())
    groups, alarms, enums = {}, {}, {}

    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef) and stmt.name in ENUM_CLASSES:
            enums[stmt.name] = {
                str(lit(s.value)): s.targets[0].id
                for s in stmt.body
                if isinstance(s, ast.Assign) and isinstance(s.targets[0], ast.Name)
            }
            continue
        if not (isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name)):
            continue
        name = stmt.targets[0].id
        if name == "ALARM_CODES":
            for k, v in zip(stmt.value.keys, stmt.value.values):
                alarms[k.value] = {str(lit(a)): lit(b)
                                   for a, b in zip(v.keys, v.values)}
        elif name in GROUPS:
            regs = []
            for k, v in zip(stmt.value.keys, stmt.value.values):
                kw = {a.arg: lit(a.value) for a in v.keywords}
                regs.append({
                    "key": k.value,
                    "addr": kw["address"],
                    "count": kw["count"],
                    "rtype": RTYPE.get(kw["register_type"], kw["register_type"]),
                    "dtype": kw["data_type"],
                    "gain": kw.get("gain", 1),
                    "unit": kw.get("unit") or "",
                    "desc": kw.get("description") or "",
                })
            groups[name] = regs

    missing = [g for g in GROUPS if g not in groups]
    if missing:
        sys.exit(f"upstream file no longer defines: {', '.join(missing)}")

    bundle = {
        "_meta": {
            "source": "TypQxQ/Sigenergy-Local-Modbus @ main "
                      "custom_components/sigen/modbusregisterdefinitions.py",
            "generated_by": "regmap_gen.py",
            "conventions": "gain is a DIVISOR: value = raw / gain. "
                           "'ro' = input registers (FC4); 'rw' = holding (FC3); "
                           "'wo' = write-only, never read.",
        },
        "alarm_codes": alarms,
        "enums": enums,
        "groups": groups,
    }
    # Must match dump.load_regmap(), which reads regmap.json beside itself. Writing
    # any other name here regenerates a file nothing loads.
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regmap.json")
    with open(out, "w") as fh:
        json.dump(bundle, fh, indent=1)

    for g, regs in groups.items():
        lo = min(r["addr"] for r in regs)
        hi = max(r["addr"] + r["count"] - 1 for r in regs)
        kinds = {k: sum(1 for r in regs if r["rtype"] == k) for k in ("ro", "rw", "wo")}
        print(f"  {g:38} {len(regs):4} fields  {lo}..{hi}  "
              f"ro={kinds['ro']} rw={kinds['rw']} wo={kinds['wo']}")
    print(f"\nwrote {out}: {sum(len(v) for v in groups.values())} fields, "
          f"{len(alarms)} alarm tables, {len(enums)} enums")


if __name__ == "__main__":
    main()
