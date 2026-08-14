#!/usr/bin/env python3
"""
Comprehensive decode of every documented Sigenergy register. READ-ONLY, stdlib only.

Reads the full register map in regmap.json (generated from
TypQxQ/Sigenergy-Local-Modbus modbusregisterdefinitions.py) and reports, for each
field, what this particular unit actually returns:

  ok           decoded value
  unsupported  device rejected the address (exception 2)
  sentinel     read fine but returned an all-ones "not available / not set" value
  write-only   documented as wo, never read

Read-only ("ro") registers are INPUT registers -> FC4. Holding ("rw") -> FC3.
Reading a holding register does not write anything; nothing here writes.
`gain` is a divisor: value = raw / gain.

Usage: dump.py [host] [--json out.json] [--ac-charger UNIT] [--quiet]
       --quiet prints only the per-group tallies and the derived summary.

The host defaults to config.json's; a positional argument overrides it.
"""
import json
import os
import sys
import time

import config
import lib
from lib import (Modbus, ModbusError, decode, dtype_of, is_absent_marker,
                 is_sentinel, plan_blocks)


ALARM_TABLE_FOR = {
    "plant_general_alarm1": "PCS_ALARM_CODES",
    "plant_general_alarm2": "PCS_ALARM_CODES2",
    "plant_general_alarm3": "ESS_ALARM_CODES",
    "plant_general_alarm4": "GATEWAY_ALARM_CODES",
    "plant_general_alarm6": "PLANT_ALARM_CODES6",
    "plant_general_alarm7": "PLANT_ALARM_CODES7",
    "inverter_alarm1": "PCS_ALARM_CODES",
    "inverter_alarm2": "PCS_ALARM_CODES2",
    "inverter_alarm3": "ESS_ALARM_CODES",
    "inverter_alarm4": "GATEWAY_ALARM_CODES",
    "ac_charger_alarm1": "AC_CHARGER_ALARM_CODES1",
    "ac_charger_alarm2": "AC_CHARGER_ALARM_CODES2",
    "ac_charger_alarm3": "AC_CHARGER_ALARM_CODES3",
    "dc_charger_alarm": "DC_CHARGER_ALARM_CODES",
}


# Registers whose meaning lives in an appendix enum rather than inline in the text.
ENUM_FOR = {
    "plant_running_state": "RunningState",
    "inverter_running_state": "RunningState",
    "plant_ems_work_mode": "EMSWorkMode",
    "plant_remote_ems_control_mode": "RemoteEMSControlMode",
    "inverter_output_type": "OutputType",
    "ac_charger_system_state": "ACChargerSystemState",
    "dc_charger_running_state": "DCChargerRunningState",
}

def enum_map(desc):
    """Pull '0: ongrid, 1: offgrid(auto)' style mappings out of a description."""
    out, i = {}, 0
    while True:
        c = desc.find(":", i)
        if c < 0:
            return out
        j = c - 1
        while j >= 0 and desc[j] == " ":
            j -= 1
        k = j
        while k >= 0 and desc[k].isdigit():
            k -= 1
        if k == j:  # no digits before the colon
            i = c + 1
            continue
        code = int(desc[k + 1:j + 1])
        depth, m, cut = 0, c + 1, None
        while m < len(desc):
            ch = desc[m]
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            elif ch in ",;" and depth == 0:
                break
            elif ch.isdigit() and depth == 0:
                # "0: disabled 1: enabled" — the next code starts here, unseparated
                n = m
                while n < len(desc) and desc[n].isdigit():
                    n += 1
                p = n
                while p < len(desc) and desc[p] == " ":
                    p += 1
                if p < len(desc) and desc[p] == ":":
                    cut = m
                    break
            m += 1
        label = desc[c + 1:cut if cut is not None else m].strip()
        if label:
            out[code] = label
        i = (cut if cut is not None else m + 1)


def annotate(field, raw, val, tables):
    """Human-readable interpretation beyond the scaled number."""
    key = field["key"]
    dt = dtype_of(field)
    notes = []

    if key in lib.TIME_KEYS:
        if raw == 0:
            return "never / not set"
        return time.strftime("%Y-%m-%d %H:%M:%S device-local", time.gmtime(raw))

    tbl = tables["alarm_codes"].get(ALARM_TABLE_FOR.get(key, ""), {})
    if tbl:
        if raw == 0:
            return "no alarms"
        bits = [tbl.get(str(b), f"bit{b}") for b in range(16) if raw >> b & 1]
        return "ALARM: " + "; ".join(bits)

    if key in ENUM_FOR:
        names = tables["enums"].get(ENUM_FOR[key], {})
        label = names.get(str(raw))  # kept verbatim: these are spec constant names
        notes.append(label if label else f"undocumented code {raw}")
    elif dt in ("U16", "S16") and "alarm" not in key:
        m = enum_map(field["desc"])
        if m:
            notes.append(m.get(raw, f"undocumented code {raw}"))

    if is_absent_marker(raw, dt):
        notes.append("raw -1 (0xFF..) — channel not present on this unit")
    if key == "inverter_power_factor":
        notes.append("map says U16; read as S16 (PF is bounded to [-1,1])")
    return " · ".join(n for n in notes if n)


def load_regmap():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "regmap.json")) as fh:
        return json.load(fh)


def group_plan(cfg, ac_unit=None):
    """(group name, unit id, label) for every group worth sweeping on this unit."""
    plant, inv = cfg["plant_unit"], cfg["inverter_unit"]
    plan = [
        ("PLANT_RUNNING_INFO_REGISTERS", plant, "Plant running info (ro, FC4)"),
        ("PLANT_PARAMETER_REGISTERS", plant, "Plant parameters (rw, FC3)"),
        ("PLANT_ESS_PREHEATING_REGISTERS", plant, "Plant ESS preheating (rw, FC3)"),
        ("INVERTER_RUNNING_INFO_REGISTERS", inv, "Inverter running info (ro, FC4)"),
        ("INVERTER_PARAMETER_REGISTERS", inv, "Inverter parameters (rw, FC3)"),
        ("DC_CHARGER_RUNNING_INFO_REGISTERS", inv, "DC charger running info (ro, FC4)"),
        ("DC_CHARGER_PARAMETER_REGISTERS", inv, "DC charger parameters (rw, FC3)"),
    ]
    if ac_unit is not None:
        plan += [
            ("AC_CHARGER_RUNNING_INFO_REGISTERS", ac_unit, "AC charger running info (ro, FC4)"),
            ("AC_CHARGER_PARAMETER_REGISTERS", ac_unit, "AC charger parameters (rw, FC3)"),
        ]
    return plan


def sweep(mb, bundle, cfg, ac_unit=None):
    """Full-map sweep, returning {key: row}. Used by log.py's drift detector."""
    tables = {"alarm_codes": bundle["alarm_codes"], "enums": bundle.get("enums", {})}
    out = {}
    for gname, unit, _ in group_plan(cfg, ac_unit):
        rows = []
        read_group(mb, unit, bundle["groups"][gname], rows, tables)
        for r in rows:
            out[r["key"]] = r
    return out


def read_group(mb, unit, fields, out, tables):
    """Block-read a group, falling back to per-field reads when a block is refused."""
    stats = {"ok": 0, "unsupported": 0, "sentinel": 0, "write_only": 0, "error": 0}
    readable = [f for f in fields if f["rtype"] != "wo"]
    for f in fields:
        if f["rtype"] == "wo":
            out.append(dict(f, status="write-only", value=None, raw=None, note=""))
            stats["write_only"] += 1

    for start, span, group in plan_blocks(readable):
        fc = 4 if group[0]["rtype"] == "ro" else 3
        try:
            buf = mb.read(unit, start, span, fc)
            chunks = [(f, buf[(f["addr"] - start) * 2:
                              (f["addr"] - start + f["count"]) * 2]) for f in group]
        except ModbusError:
            chunks = []
            for f in group:  # isolate: a whole block dies on one bad address
                try:
                    chunks.append((f, mb.read(unit, f["addr"], f["count"],
                                              4 if f["rtype"] == "ro" else 3)))
                except ModbusError as e:
                    out.append(dict(f, status="unsupported", value=None, raw=None,
                                    note=str(e)))
                    stats["unsupported"] += 1
        except Exception as e:
            for f in group:
                out.append(dict(f, status="error", value=None, raw=None, note=str(e)))
                stats["error"] += 1
            continue

        for f, chunk in chunks:
            raw, val = decode(chunk, f)
            dt = dtype_of(f)
            if dt != "STRING" and is_sentinel(raw, dt):
                out.append(dict(f, status="sentinel", value=None, raw=raw,
                                note="all-ones / not available"))
                stats["sentinel"] += 1
            else:
                out.append(dict(f, status="ok", value=val, raw=raw,
                                note=annotate(f, raw, val, tables)))
                stats["ok"] += 1
    return stats


def derive(rows):
    """Turn the raw field dump into statements about this specific installation."""
    by_key = {r["key"]: r for r in rows}

    def v(key, default=None):
        r = by_key.get(key)
        return default if not r or r["status"] != "ok" else r["value"]

    def raw(key):
        r = by_key.get(key)
        return None if not r else r["raw"]

    out = []

    out.append(("Identity", f"{v('inverter_model_type','?')} · "
                            f"SN {v('inverter_serial_number','?')} · "
                            f"FW {v('inverter_machine_firmware_version','?')}"))

    ot = raw("inverter_output_type")
    live_phases = [p for p in ("a", "b", "c")
                   if by_key.get(f"inverter_phase_{p}_voltage", {}).get("status") == "ok"]
    dead_phases = [p for p in ("a", "b", "c") if p not in live_phases]
    out.append(("AC topology",
                f"output type {ot} ({'L/N — single phase' if ot == 0 else 'multi-phase'}), "
                f"phase voltage live: {', '.join(live_phases) or 'none'}"
                + (f"; {'/'.join(dead_phases)} and the A-B/B-C/C-A line voltages "
                   f"return not-present markers" if dead_phases else "")))

    strings, mppt = raw("inverter_pv_string_count"), raw("inverter_mppt_count")
    live_pv = [i for i in range(1, 37)
               if (by_key.get(f"inverter_pv{i}_voltage", {}).get("raw") not in (None, -1))]
    out.append(("PV topology",
                f"{strings} strings / {mppt} MPPTs declared; pv1..pv{max(live_pv or [0])} "
                f"report real values, pv{max(live_pv or [0]) + 1}..pv36 return -1 "
                f"(the map's 36 channels are generic, not this hardware)"))

    out.append(("Battery",
                f"{raw('inverter_pack_count')} pack · "
                f"{v('plant_ess_rated_energy_capacity')} kWh rated · "
                f"SOC {v('plant_ess_soc')}% · SOH {v('plant_ess_soh')}% · "
                f"cell avg {v('inverter_ess_average_cell_temperature')} degC / "
                f"{v('inverter_ess_average_cell_voltage')} V"))

    out.append(("Power now",
                f"grid {v('plant_grid_sensor_active_power')} kW · "
                f"PV {v('plant_sigen_photovoltaic_power')} kW · "
                f"battery {v('plant_ess_power')} kW · "
                f"load {v('plant_general_load_power')} kW"))

    out.append(("Lifetime energy",
                f"PV {v('plant_accumulated_pv_energy')} kWh · "
                f"batt chg {v('plant_accumulated_battery_charge_energy')} / "
                f"dis {v('plant_accumulated_battery_discharge_energy')} kWh · "
                f"grid in {v('plant_accumulated_grid_import_energy')} / "
                f"out {v('plant_accumulated_grid_export_energy')} kWh · "
                f"load {v('plant_accumulated_consumed_energy')} kWh"))

    out.append(("Control posture",
                f"EMS mode {raw('plant_ems_work_mode')} "
                f"({by_key.get('plant_ems_work_mode', {}).get('note', '?')}) · "
                f"remote EMS {'enabled' if raw('plant_remote_ems_enable') else 'disabled'} · "
                f"charge/discharge cut-off {v('plant_charge_cut_off_soc')}% / "
                f"{v('plant_discharge_cut_off_soc')}%"))

    unsupported = [r for r in rows if r["status"] == "unsupported"]
    if unsupported:
        spans = []
        for start, span, _ in plan_blocks(unsupported):
            spans.append(f"{start}..{start + span - 1}")
        out.append(("Refused addresses",
                    f"{len(unsupported)} fields rejected with exception 2 "
                    f"({', '.join(spans)}) — the hardware/firmware has no such "
                    f"subsystem"))

    sentinels = [r for r in rows if r["status"] == "sentinel"]
    if sentinels:
        out.append(("Unimplemented on this firmware",
                    f"{len(sentinels)} registers answer with all-ones: "
                    + ", ".join(r["key"] for r in sentinels[:8])
                    + (f", +{len(sentinels) - 8} more" if len(sentinels) > 8 else "")))

    alarms = [r for r in rows
              if r["key"] in ALARM_TABLE_FOR and r["status"] == "ok" and r["raw"]]
    out.append(("Alarms", "; ".join(f"{r['key']}={r['note']}" for r in alarms)
                if alarms else "all alarm words clear"))

    st, tz = raw("plant_system_time"), v("plant_system_timezone")
    if st and tz is not None:
        # The register holds local time as an epoch count, so back out the device's
        # own offset before comparing to real UTC.
        skew = (st - tz * 60) - time.time()
        out.append(("Clock", f"device {time.strftime('%H:%M:%S', time.gmtime(st))} "
                             f"device-local, tz {tz} min; local-time-as-epoch "
                             f"encoding, skew vs true UTC {skew:+.0f} s"))
    return out


def fmt_value(row):
    v = row["value"]
    if v is None:
        return f"{'--':>16}"
    if isinstance(v, str):
        return f"{v:>16}"
    if isinstance(v, float):
        return f"{v:>16,.3f}"
    return f"{v:>16,}"


def main():
    args = sys.argv[1:]
    json_out, ac_unit, quiet = None, None, False
    rest = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--json":
            json_out = args[i + 1]
            i += 2
        elif a == "--ac-charger":
            ac_unit = int(args[i + 1])
            i += 2
        elif a == "--quiet":
            quiet = True
            i += 1
        else:
            rest.append(a)
            i += 1
    cfg = config.load(overrides={"host": rest[0] if rest else None})
    host, port = cfg["host"], cfg["port"]

    bundle = load_regmap()
    groups = bundle["groups"]
    tables = {"alarm_codes": bundle["alarm_codes"], "enums": bundle.get("enums", {})}
    plan = group_plan(cfg, ac_unit)

    print(f"Sigenergy full register decode @ {host}:{port}   "
          f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    report, totals = {}, {"ok": 0, "unsupported": 0, "sentinel": 0,
                          "write_only": 0, "error": 0}
    t0 = time.perf_counter()
    with Modbus(host, port, cfg["timeout_s"]) as mb:
        for gname, unit, label in plan:
            fields = groups[gname]
            rows = []
            stats = read_group(mb, unit, fields, rows, tables)
            rows.sort(key=lambda r: r["addr"])
            report[gname] = {"unit": unit, "rows": rows, "stats": stats}
            for k in totals:
                totals[k] += stats[k]

            print(f"== {label} — unit {unit} — {len(fields)} documented fields ==")
            print(f"   ok {stats['ok']} · unsupported {stats['unsupported']} · "
                  f"sentinel {stats['sentinel']} · write-only {stats['write_only']}"
                  + (f" · error {stats['error']}" if stats["error"] else ""))
            if not quiet:
                for r in rows:
                    tag = "" if r["status"] == "ok" else f"[{r['status']}] "
                    note = r["note"] and f"  {tag}{r['note']}" or (f"  {tag}".rstrip())
                    print(f"  {r['addr']:>5} {r['key']:<46}{fmt_value(r)} "
                          f"{(r['unit'] or ''):<5}{note}")
            print()

    el = time.perf_counter() - t0
    all_rows = [r for g in report.values() for r in g["rows"]]

    print("== What the map means on this unit ==")
    for label, text in derive(all_rows):
        print(f"  {label:<32}{text}")

    print(f"\n=== {len(all_rows)} documented fields, {el:.1f}s ===")
    print(f"  ok {totals['ok']} · unsupported {totals['unsupported']} · "
          f"sentinel {totals['sentinel']} · write-only {totals['write_only']} · "
          f"error {totals['error']}")

    if json_out:
        with open(json_out, "w") as fh:
            json.dump({"host": host, "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "totals": totals, "groups": report}, fh, indent=1)
        print(f"  -> {json_out}")
        print("  NOTE: this file contains the unit's serial number and your LAN "
              "address. Redact before sharing.")


if __name__ == "__main__":
    main()
