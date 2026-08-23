#!/usr/bin/env python3
"""
The presentation contract: which fields make up which chart, and which are "live".

Stdlib only -- in fact it imports nothing at all. That is the point of it being its own
module: this is data, and both readers of the archive need the same copy of it.

**Why this is not in the code that draws the charts.** It used to be, in serve.py, back when
a local HTTP server was the only thing that rendered this archive. ingest.py imported it from
there and said so ("the presentation contract comes from serve.py, deliberately") -- which was
true, and was also a workaround for the catalogue having nowhere better to live. When the local
viewer was removed the import would have been the only reason to keep an HTTP server in the
tree, so the data moved here and the server went.

Adding a chart is a dict in PANELS, not new JavaScript: ingest.py copies this list into each
plan's meta.json and web/app.js builds the page from it.

**An `id` is a URL.** It appears in the page's hash as `panels=` and as `#focus=`, and it
reaches a tile query, so renaming one silently drops that panel from any saved link that names
it -- the id no longer matches a panel, and nothing is there to complain. Titles are free to
change; ids are the part someone may have sent to somebody.

Shares are the exception, and deliberately: because ingest.py copies this list into meta.json,
a frozen share carries the ids and titles it was made with and keeps rendering them. See
FINDINGS 35 -- a share holds its own copy of both the data and the page.
"""

# The live strip. plant_* rather than the inverter_* duplicates (FINDINGS 7).
LIVE_FIELDS = [
    "plant_sigen_photovoltaic_power", "plant_general_load_power", "plant_ess_power",
    "plant_grid_sensor_active_power", "plant_ess_soc", "plant_ess_soh",
    "plant_running_state", "plant_on_off_grid_status", "plant_ems_work_mode",
    "plant_ess_average_cell_temperature", "inverter_pcs_internal_temperature",
    "inverter_grid_frequency", "inverter_phase_a_voltage", "inverter_power_factor",
    "plant_pv_total_daily_generation", "plant_ess_available_max_charging_power",
    "plant_ess_available_max_discharging_power", "plant_system_time",
    "plant_general_alarm1", "plant_general_alarm2", "plant_general_alarm3",
    "plant_general_alarm4", "plant_general_alarm6", "plant_general_alarm7",
]

# Which chart is made of which fields. Slots are categorical palette positions, assigned to an
# ENTITY and never re-used for another one, so grid is the same colour on every chart it
# appears on. See the module docstring for why an `id` may not be renamed lightly.
PANELS = [
    {
        "id": "power", "title": "Power flow", "unit": "kW", "kind": "lines",
        "zero": True, "height": 260,
        "note": "Grid > 0 importing, < 0 exporting. Battery > 0 charging. "
                "PV = load + battery charge + export.",
        "series": [
            {"key": "plant_grid_sensor_active_power", "label": "Grid", "slot": 1},
            {"key": "plant_general_load_power", "label": "Load", "slot": 2},
            {"key": "plant_ess_power", "label": "Battery", "slot": 3},
            {"key": "plant_sigen_photovoltaic_power", "label": "PV", "slot": 4},
        ],
    },
    {
        "id": "soc", "title": "Battery state of charge", "unit": "%",
        "kind": "lines", "domain": [0, 100], "height": 150,
        "note": "Its own chart rather than a second axis on the power plot.",
        "series": [{"key": "plant_ess_soc", "label": "SOC", "slot": 3}],
    },
    {
        "id": "panels-amps", "title": "PV panels current", "unit": "A",
        "kind": "lines", "height": 190, "ramp": True,
        "note": "pv1..pv4 are the inverter's four DC inputs, each a group of panels. "
                "Those four only: the other 32 documented channels return the -1 "
                "absent marker on this unit. Power needs V x I per sample, which "
                "bucketed V and I cannot reconstruct -- so current, which is what "
                "shows a shaded group.",
        "series": [
            {"key": "inverter_pv1_current", "label": "pv1", "slot": 1},
            {"key": "inverter_pv2_current", "label": "pv2", "slot": 2},
            {"key": "inverter_pv3_current", "label": "pv3", "slot": 3},
            {"key": "inverter_pv4_current", "label": "pv4", "slot": 4},
        ],
    },
    {
        "id": "panels-volts", "title": "PV panels voltage", "unit": "V",
        "kind": "lines", "height": 190, "ramp": True, "collapsed": True,
        "note": "The four groups differ in how many panels are wired in series, so "
                "their voltages are not comparable to each other -- compare each to "
                "its own settled Voc (FINDINGS 10). Full voltage at zero current is "
                "the MPPT self-check, not a fault.",
        "series": [
            {"key": "inverter_pv1_voltage", "label": "pv1", "slot": 1},
            {"key": "inverter_pv2_voltage", "label": "pv2", "slot": 2},
            {"key": "inverter_pv3_voltage", "label": "pv3", "slot": 3},
            {"key": "inverter_pv4_voltage", "label": "pv4", "slot": 4},
        ],
    },
    {
        "id": "temps", "title": "Temperatures", "unit": "degC", "kind": "lines",
        "height": 150,
        "series": [
            {"key": "inverter_pcs_internal_temperature", "label": "PCS", "slot": 1},
            {"key": "plant_ess_average_cell_temperature", "label": "Cell avg",
             "slot": 2},
        ],
    },
    {
        "id": "freq", "title": "Grid frequency", "unit": "Hz", "kind": "lines",
        "height": 130, "collapsed": True,
        "series": [{"key": "inverter_grid_frequency", "label": "Frequency",
                    "slot": 1}],
    },
    {
        "id": "volts", "title": "Grid voltage (phase A)", "unit": "V",
        "kind": "lines", "height": 130, "collapsed": True,
        "series": [{"key": "inverter_phase_a_voltage", "label": "Voltage",
                    "slot": 1}],
    },
    {
        "id": "pf", "title": "Power factor", "unit": "", "kind": "lines",
        "height": 130, "collapsed": True, "domain": [-1, 1],
        "note": "Typed U16 upstream; read as S16, since a power factor is bounded "
                "to [-1, 1].",
        "series": [{"key": "inverter_power_factor", "label": "Power factor",
                    "slot": 1}],
    },
    {
        "id": "state", "title": "Plant state and alarms", "kind": "state",
        "height": 120,
        "note": "A band is one bucket wide; a bucket that contained a transition "
                "is marked. Alarm words are OR-ed over the bucket, so a bit set "
                "for one sample still shows.",
        # `short` is what fits in the axis gutter, which is as wide as the line
        # charts' so the shared crosshair stays aligned; `label` is the full name,
        # used in the tooltip and as the row's SVG title.
        "series": [
            {"key": "plant_running_state", "label": "Running state", "short": "State"},
            {"key": "plant_on_off_grid_status", "label": "Grid connection",
             "short": "Grid"},
            {"key": "plant_ems_work_mode", "label": "EMS work mode", "short": "EMS"},
        ],
        "alarms": ["plant_general_alarm1", "plant_general_alarm2",
                   "plant_general_alarm3", "plant_general_alarm4",
                   "plant_general_alarm6", "plant_general_alarm7"],
    },
    {
        "id": "health", "title": "Capture health", "unit": "ms", "kind": "latency",
        "height": 170, "collapsed": True,
        "note": "Tick latency over records that returned data. An outage probe's "
                "latency is the socket timeout, not the device's, so those are "
                "counted separately and shaded instead.",
    },
]

# Window totals come from the lifetime counters, not from integrating power: they
# are the independent cross-check, and they agree to about a watt-hour.
ENERGY_TILES = [
    {"key": "plant_accumulated_pv_energy", "label": "PV generated"},
    {"key": "plant_accumulated_grid_import_energy", "label": "Grid imported"},
    {"key": "plant_accumulated_grid_export_energy", "label": "Grid exported"},
    {"key": "plant_accumulated_battery_charge_energy", "label": "Battery charged"},
    {"key": "plant_accumulated_battery_discharge_energy",
     "label": "Battery discharged"},
    {"key": "plant_accumulated_consumed_energy", "label": "Load consumed"},
]


def panel_keys(ids=None):
    """Fields the named panels need, in a stable order."""
    keys = []
    for p in PANELS:
        if ids is not None and p["id"] not in ids:
            continue
        for s in p.get("series", []):
            keys.append(s["key"])
        keys.extend(p.get("alarms", []))
    for t in ENERGY_TILES:
        if ids is None or "energy" in ids:
            keys.append(t["key"])
    return list(dict.fromkeys(keys))
