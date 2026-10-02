"""Pull the history the replay harness needs straight from Home Assistant.

Clicking twenty exports through the History panel is the slow way (Anze,
2026-09-18). The REST API returns the same states, so this walks a day at a
time over each group of entities and writes one CSV per day per group, in
exactly the shape the panel downloads - entity_id, state, last_changed - so
replay.py reads either without knowing the difference.

    python3 tests/fetch_history.py --days 10
    python3 tests/fetch_history.py --days 1 --ending 2026-09-14 --chunk-hours 6

It needs a long-lived access token, which it reads from a FILE and never
takes on the command line, where it would land in a shell history:

    Home Assistant, your profile, Security, Long-lived access tokens.
    Save it as data/<hostname> - so data/ha.alpacasbarn.com holds the token
    that instance issued. The whole data/ folder is gitignored.

A token belongs to ONE instance, so each site needs its own, and naming the
file after the host is what keeps them straight (Anze, 2026-09-18).

Days already downloaded are skipped, so an interrupted run resumes.
"""
from __future__ import annotations

import argparse
import csv
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Each site's base URL, zone, and the entities fetched for it, a CSV per day
# per group; add a site by copying one.
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
              "voltage_an", "voltage_bn", "voltage_cn",
              # recorded from 2026-09-30: the meter's own, signed reactive power
              "var_a", "var_b", "var_c", "va_a", "va_b", "va_c", "pf_a", "pf_b", "pf_c")]
            + ["sensor.solaredge_se17k_i1_ac_power"]
            + [f"sensor.solaredge_se17k_i1_ac_{k}" for k in
               ("current_a", "current_b", "current_c", "voltage_an", "voltage_bn", "voltage_cn")]
            + [f"sensor.se17k_home_power_phase_{p}" for p in "abc"]
            + ["sensor.blaz_pc_power", "sensor.attic_office_power",
               "sensor.evbox_elvi_power_active_import", "sensor.hidrofor_power",
               "sensor.workshop_boiler_power", "sensor.attic_ac_power",
               "sensor.server_ups_power", "sensor.shellypmminig3_susilna_power",
               "sensor.workshop_charger_power"]),
        # the two three-phase Shellys, which report every second
        "shellys": (
            [f"sensor.hisa_phase_{p}_active_power" for p in "abc"]
            + ["sensor.hisa_total_active_power"]
            + [f"sensor.mansarda_phase_{p}_active_power" for p in "abc"]
            + ["sensor.mansarda_total_active_power"]),
        # ...and their power factors and apparent power per phase, so the
        # replay can give their steps a reactive power the way production
        # does (their steps sat at "no power factor" on the graphs, 2026-09-30)
        "shellys-pf": (
            [f"sensor.{m}_phase_{p}_{k}" for m in ("hisa", "mansarda") for p in "abc"
             for k in ("power_factor", "apparent_power")]),
        # small loads energy_bench's worth scores (2026-10-02): the bathroom
        # fan's Shelly (behind Mansarda, from 22 Sep 12:58 UTC), the RF ceiling
        # fan's assumed speed, and the blinds' Shelly 2PMs - West on the grid
        # connection, North and East behind Mansarda. Their first days were
        # fetched into data/history/home-extra.
        "fans": ["sensor.bathroom_fan_switch_0_power", "switch.bathroom_fan_switch_0",
                 "sensor.living_room_ceiling_fan_speed", "fan.ceiling_fan"],
        "blinds": ["sensor.west_blinds_power", "sensor.north_blinds_power",
                   "sensor.living_room_east_blinds_power"],
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
            "sensor.power_strip_power",      # Car charger
            "sensor.boiler_power",      # Boiler
            "sensor.washing_machine_power",      # Washing Machine
            "sensor.well_pump_power",
            "sensor.kozolec_hidrofor_power",                   # Water Pump
            "sensor.pond_filter_power",               # Pond
            "sensor.pond_evse_power",
            "sensor.pastir_staja_power",
            "sensor.bug_lamp_power",
            "sensor.bathroom_ir_panel_switch_0_power",       # Bathroom IR Panel
        ],
    },
}

SITES = {"home": HOME, "kozolec": KOZOLEC}

# Entities renamed on 2026-09-28 (HA_Configs/<site>/rename-plan-2026-09-28*.json).
# History fetched before then is under the old id and after under the new;
# the recorder moved the old days to the new id too, so a fetch by the old id
# came back empty from 23 Sep on - Home's house and attic meters, the office
# plug, Kozolec's well pump and boiler all missing from the bench for those
# days. replay.read_csv reads an old id as its new one.
RENAMED = {
    "sensor.attic_phase_a_active_power": "sensor.mansarda_phase_a_active_power",
    "sensor.attic_phase_b_active_power": "sensor.mansarda_phase_b_active_power",
    "sensor.attic_phase_c_active_power": "sensor.mansarda_phase_c_active_power",
    "sensor.attic_total_active_power": "sensor.mansarda_total_active_power",
    "sensor.kotlovnica_well_pump_power": "sensor.well_pump_power",
    "sensor.nasa_station_power": "sensor.attic_office_power",
    "sensor.shelly_pond_switch_0_power": "sensor.pond_filter_power",
    "sensor.shellypmminig3_84fce63c6654_power": "sensor.blaz_pc_power",
    "sensor.shellypmminig3_ecda3bc6b054_power": "sensor.workshop_charger_power",
    "sensor.shellypro3em_34987a459ae0_phase_a_active_power": "sensor.hisa_phase_a_active_power",
    "sensor.shellypro3em_34987a459ae0_phase_b_active_power": "sensor.hisa_phase_b_active_power",
    "sensor.shellypro3em_34987a459ae0_phase_c_active_power": "sensor.hisa_phase_c_active_power",
    "sensor.shellypro3em_34987a459ae0_total_active_power": "sensor.hisa_total_active_power",
    "sensor.shellypro4pm_kozolec_switch_0_power": "sensor.power_strip_power",
    "sensor.shellypro4pm_kozolec_switch_1_power": "sensor.boiler_power",
    "sensor.shellypro4pm_kozolec_switch_3_power": "sensor.washing_machine_power",
}


def read_token(path: Path) -> str:
    if not path.exists():
        raise SystemExit(
            f"no token at {path}\n"
            "  Home Assistant -> your profile -> Security -> Long-lived access tokens,\n"
            "  then save it at that path - the file is named after the host it\n"
            "  came from. One per instance; data/ is gitignored."
        )
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"{path} is empty")
    return token


def fetch(base: str, token: str, entities, start: datetime, end: datetime, timeout: int):
    """The API's history for one window, as (entity_id, state, last_changed)."""
    query = urllib.parse.urlencode({
        "filter_entity_id": ",".join(entities),
        "end_time": end.astimezone(timezone.utc).isoformat(),
        "minimal_response": "",
        "no_attributes": "",
    })
    stamp = start.astimezone(timezone.utc).isoformat()
    url = f"{base}/api/history/period/{urllib.parse.quote(stamp)}?{query}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    rows = []
    for series in payload:
        # with minimal_response only the first entry carries the entity id
        entity_id = series[0].get("entity_id") if series else None
        for entry in series:
            entity_id = entry.get("entity_id") or entity_id
            when = entry.get("last_changed") or entry.get("last_updated")
            if entity_id and when is not None:
                rows.append((entity_id, entry.get("state"), when))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="home", choices=sorted(SITES))
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--ending", help="last local day, YYYY-MM-DD; default yesterday")
    parser.add_argument("--token", help="default data/<hostname of the site>")
    parser.add_argument("--out", help="default data/history/<site>")
    parser.add_argument("--chunk-hours", type=int, default=24,
                        help="split each day, for entities that report every second")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true", help="say what it would fetch")
    args = parser.parse_args()

    site = SITES[args.site]
    tz = site["tz"]
    out = Path(args.out) if args.out else ROOT / "data" / "history" / args.site
    out.mkdir(parents=True, exist_ok=True)
    if args.token:
        token_path = Path(args.token) if Path(args.token).is_absolute() else ROOT / args.token
    else:
        token_path = ROOT / "data" / urllib.parse.urlsplit(site["base"]).hostname
    token = "" if args.dry_run else read_token(token_path)

    last = (datetime.strptime(args.ending, "%Y-%m-%d").replace(tzinfo=tz) if args.ending
            else datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1))
    days = [last - timedelta(days=n) for n in range(args.days - 1, -1, -1)]

    total = 0
    for day in days:
        for group, entities in site["groups"].items():
            target = out / f"{day:%Y-%m-%d}_{group}.csv"
            if target.exists():
                print(f"  {target.name}: already here")
                continue
            rows = []
            edge = day
            while edge < day + timedelta(days=1):
                stop = min(edge + timedelta(hours=args.chunk_hours), day + timedelta(days=1))
                if args.dry_run:
                    print(f"  would fetch {len(entities)} entities "
                          f"{edge:%Y-%m-%d %H:%M} -> {stop:%H:%M}")
                else:
                    try:
                        rows += fetch(site["base"], token, entities, edge, stop, args.timeout)
                    except urllib.error.HTTPError as exc:
                        print(f"  {target.name}: HTTP {exc.code} {exc.reason}")
                        return 1
                    except Exception as exc:  # noqa: BLE001
                        print(f"  {target.name}: {type(exc).__name__}: {exc}")
                        return 1
                edge = stop
            if args.dry_run:
                continue
            with target.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["entity_id", "state", "last_changed"])
                writer.writerows(rows)
            total += len(rows)
            print(f"  {target.name}: {len(rows)} rows")
    if not args.dry_run:
        print(f"{total} rows into {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
