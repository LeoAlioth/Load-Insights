"""Build History-panel links for an export, a day at a time.

The panel can only download what is on screen, and a Shelly reporting every
second makes a day of three-phase readings a file of its own (Anze,
2026-09-18). So this writes one link per day per GROUP of entities, as a
page that can be clicked through in order.

    python3 tests/export_urls.py --days 10 --out data/history/home/links.html

Groups and entity lists live in SITES below; add a site by copying one.
"""
from __future__ import annotations

import argparse
import html
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

HOME = {
    "base": "https://ha.alpacasbarn.com",
    "tz": timezone(timedelta(hours=2)),
    "groups": {
        # the SolarEdge meter and inverter, plus the template phase sensors
        # detection is configured against, plus the single-phase device
        # meters - all of them poll, so a day of these is modest
        "meters-and-devices": (
            [f"sensor.solaredge_se17k_m1_ac_{k}" for k in
             ("power_a", "power_b", "power_c", "current_a", "current_b", "current_c",
              "voltage_an", "voltage_bn", "voltage_cn")]
            + ["sensor.solaredge_se17k_i1_ac_power"]
            + [f"sensor.solaredge_se17k_i1_ac_{k}" for k in
               ("current_a", "current_b", "current_c", "voltage_an", "voltage_bn", "voltage_cn")]
            + [f"sensor.se17k_home_power_phase_{p}" for p in "abc"]
            + ["sensor.shellypmminig3_84fce63c6654_power", "sensor.nasa_station_power",
               "sensor.evbox_elvi_power_active_import", "sensor.hidrofor_power",
               "sensor.workshop_boiler_power", "sensor.attic_ac_power",
               "sensor.server_ups_power", "sensor.shellypmminig3_susilna_power",
               "sensor.shellypmminig3_ecda3bc6b054_power"]),
        # the two three-phase Shellys, which report every second
        "shellys": (
            [f"sensor.shellypro3em_34987a459ae0_phase_{p}_active_power" for p in "abc"]
            + ["sensor.shellypro3em_34987a459ae0_total_active_power"]
            + [f"sensor.attic_phase_{p}_active_power" for p in "abc"]
            + ["sensor.attic_total_active_power"]),
    },
}

# Kozolec is a second Home Assistant, so it needs its own base URL and its
# own token - a long-lived token is issued by one instance for one user and
# means nothing to another (Anze, 2026-09-18). Its LOAD side is the
# MultiPlus AC OUT, whose entity names are still to be confirmed; the AC IN
# it is currently pointed at reads zero, the site being off grid.
KOZOLEC = {
    "base": "https://ha.kozolec.hlevcek.com",
    "tz": timezone(timedelta(hours=2)),
    "groups": {
        # The LOAD side, and it is fully instrumented: power, current AND
        # voltage on the MultiPlus output, which is a coherent triple on the
        # very reading the loads hang off - the one thing home does not have.
        # The AC INPUT is a generator port, real but idle almost always, so
        # it comes along to show what "off" looks like rather than to detect
        # on (Anze, 2026-09-18).
        "inverter": [
            "sensor.multiplus_ii_48_15000_200_100_id_276_output_power_l1",
            "sensor.multiplus_ii_48_15000_200_100_id_276_output_current_l1",
            "sensor.multiplus_ii_48_15000_200_100_id_276_output_voltage_l1",
            "sensor.multiplus_ii_48_15000_200_100_id_276_0_line_l2_output_power",
            "sensor.multiplus_ii_48_15000_200_100_id_276_input_power_l1",
            "sensor.multiplus_ii_48_15000_200_100_id_276_input_current_l1",
            "sensor.multiplus_ii_48_15000_200_100_id_276_input_voltage_l1",
            "sensor.gx_device_consumption_power_l1",
            "sensor.gx_device_consumption_current_l1",
            "sensor.gx_device_critical_loads_on_l1",
        ],
        # DC: the arrays charge the battery directly, so none of this ever
        # reaches the AC side as generation - which is why the load signal
        # here is clean and home's is not.
        "dc": [
            "sensor.mppt_150_70_pv_yield_power",
            "sensor.mppt_150_85_pv_yield_power",
            "sensor.gx_device_pv_power",
            "sensor.gx_device_dc_battery_power",
            "sensor.multiplus_ii_48_15000_200_100_id_276_dc_power",
            "sensor.jk_bms_id_512_charge",
        ],
        "devices": [
            "sensor.shellypro4pm_kozolec_switch_0_power",      # Car charger
            "sensor.shellypro4pm_kozolec_switch_1_power",      # Boiler
            "sensor.shellypro4pm_kozolec_switch_3_power",      # Washing Machine
            "sensor.kotlovnica_well_pump_power",
            "sensor.kozolec_hidrofor_power",                   # Water Pump
            "sensor.shelly_pond_switch_0_power",               # Pond
            "sensor.pond_evse_power",
            "sensor.pastir_staja_power",
            "sensor.bug_lamp_power",
        ],
    },
}

SITES = {"home": HOME, "kozolec": KOZOLEC}


def link(base: str, entities, start: datetime, end: datetime) -> str:
    stamp = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return (f"{base}/history?entity_id={quote(','.join(entities), safe='')}"
            f"&start_date={quote(stamp(start), safe='')}&end_date={quote(stamp(end), safe='')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="home", choices=sorted(SITES))
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--ending", help="last local day to include, YYYY-MM-DD; default yesterday")
    parser.add_argument("--out", default="links.html")
    args = parser.parse_args()

    site = SITES[args.site]
    tz = site["tz"]
    last = (datetime.strptime(args.ending, "%Y-%m-%d").replace(tzinfo=tz) if args.ending
            else datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1))
    days = [last - timedelta(days=n) for n in range(args.days - 1, -1, -1)]

    rows = []
    for day in days:
        cells = []
        for name, entities in site["groups"].items():
            url = link(site["base"], entities, day, day + timedelta(days=1))
            cells.append(f'<a href="{html.escape(url)}" target="_blank">{html.escape(name)}'
                         f' <span class="n">{len(entities)}</span></a>')
        rows.append(f'<tr><th>{day:%a %d %b}</th><td>' + "</td><td>".join(cells) + "</td></tr>")

    page = f"""<!doctype html><meta charset="utf-8">
<title>History exports</title>
<style>
 body {{ font: 15px/1.5 system-ui, sans-serif; margin: 2rem; max-width: 46rem; }}
 table {{ border-collapse: collapse; width: 100%; }}
 th, td {{ padding: .4rem .6rem; border-bottom: 1px solid #8884; text-align: left; }}
 th {{ white-space: nowrap; font-weight: 600; }}
 a {{ display: inline-block; }}
 .n {{ opacity: .55; font-size: .85em; }}
 p {{ color: #666; }}
</style>
<h1>{html.escape(args.site)} &middot; {args.days} days</h1>
<p>One file per cell. Open the link, then <b>Download data</b> from the menu at
   the top right. Save everything into <code>data/history/{html.escape(args.site)}/</code>.</p>
<table>{''.join(rows)}</table>
"""
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(page)
    print(f"{len(days)} days x {len(site['groups'])} groups -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
