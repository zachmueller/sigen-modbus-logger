#!/usr/bin/env python3
"""
What the live stream carries, and how each of it is drawn. Stdlib only; imports panels.

Seven metrics, chosen for a quick glance rather than for diagnosis -- the hourly archive is
still where everything else lives. Their names are the register keys, exactly as regmap.json,
decode.py, the tiles and panels.py spell them, so a live point and an archive record name a
quantity the same way and nothing needs a mapping table to join them later.

Labels and colour slots are NOT restated here. They are looked up in panels.PANELS, which is
the presentation contract the archive viewer already draws from, so grid is the same blue and
load the same orange on both pages, and renaming a label in one place renames it in both.

Three readers: live.py on the Pi (which keys to decode and how finely to round them), the
live Lambda (which ships SERIES to the page with the seed), and the tests.
"""
import panels

# Display order on the live page. Power first, because "what is the house doing" is the
# question; then the battery's state, then the grid's.
KEYS = (
    "plant_general_load_power",
    "plant_sigen_photovoltaic_power",
    "plant_grid_sensor_active_power",
    "plant_ess_power",
    "plant_ess_soc",
    "inverter_grid_frequency",
    "inverter_phase_a_voltage",
)

# Decimal places = the register's own resolution (log10 of its gain). Rounding to it means
# a message never carries digits the device did not produce -- 1.2340000000000002 kW is a
# float artefact, not a measurement. tests/test_live.py checks these against regmap.json.
DECIMALS = {
    "plant_general_load_power": 3,
    "plant_sigen_photovoltaic_power": 3,
    "plant_grid_sensor_active_power": 3,
    "plant_ess_power": 3,
    "plant_ess_soc": 1,
    "inverter_grid_frequency": 2,
    "inverter_phase_a_voltage": 2,
}

# Which way is which, for the two signed powers. The same words the archive viewer's live
# strip uses (web/app.js), and the conventions are the register descriptions': 30005 ">0 buy
# from grid; <0 sell to grid", 30037 "<0: discharging, >0: charging".
SIGNS = {
    "plant_grid_sensor_active_power": ["importing", "exporting"],
    "plant_ess_power": ["charging", "discharging"],
}

# The smallest y-span a chart may autoscale to. Without a floor, a steady 50.00 Hz fills the
# chart's height with 0.01 Hz of quantisation noise and looks like a grid in distress.
MIN_SPAN = {
    "plant_general_load_power": 0.5,
    "plant_sigen_photovoltaic_power": 0.5,
    "plant_grid_sensor_active_power": 0.5,
    "plant_ess_power": 0.5,
    "inverter_grid_frequency": 0.2,
    "inverter_phase_a_voltage": 4.0,
}

# Which panel each metric is drawn from in the archive viewer. Its title is used for the
# multi-series power panel only as a fallback; a live chart is one metric, so it is titled
# by the series label.
_TITLES = {
    "plant_general_load_power": "House load",
    "plant_sigen_photovoltaic_power": "Solar (PV)",
    "plant_grid_sensor_active_power": "Grid",
    "plant_ess_power": "Battery power",
}


def _lookup(key):
    for p in panels.PANELS:
        for s in p.get("series", []):
            if s["key"] == key:
                return p, s
    raise KeyError(f"{key} is in livefields.KEYS but in no panels.PANELS series")


def series():
    """One dict per live metric, in display order: everything the page needs to draw it."""
    out = []
    for key in KEYS:
        panel, ser = _lookup(key)
        item = {
            "key": key,
            "label": ser["label"],
            "title": _TITLES.get(key, panel["title"]),
            "unit": panel.get("unit", ""),
            "slot": ser["slot"],
            "dec": DECIMALS[key],
            "zero": bool(panel.get("zero")),
        }
        if panel.get("domain"):
            item["domain"] = list(panel["domain"])
        if key in MIN_SPAN:
            item["min_span"] = MIN_SPAN[key]
        if key in SIGNS:
            item["sign"] = list(SIGNS[key])
        out.append(item)
    return out
