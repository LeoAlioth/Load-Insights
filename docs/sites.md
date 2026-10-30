# Sites

How the sites Load Insights runs on are wired, which meter readings it can rely on, and operating notes for their Home Assistant instances.

## Site topologies


Load Insights (and Load Juggler) run against three sites. House consumption is
always **the base reading at the point the loads hang off, plus every generation
source attached below that point**. `parallel` / `series` are the two-node cases
of that rule.

The general form, which all three sites fall out of (2026-09-18):

```
house = Σ(inverter load-side readings)          what is behind each inverter
      + Σ(generation attached below those)      an AC array on a load port
      + (grid meter − Σ(inverter grid-port readings))   loads behind neither
```

The last term is what sits BETWEEN the utility meter and the inverter's own
AC input - a non-backed-up panel. A hybrid measures its grid port itself, so
the difference between that and the external CTs is exactly that load, and
it is zero when nothing is there.

| site | load side | generation below | grid term | house |
|---|---|---|---|---|
| **home** ("31b consumption forecast") | none - the PV inverter has no AC input | SolarEdge I1 | the M1 meter in full | `SE − meter` (that meter reads export POSITIVE) |
| **kozolec** (re-added) | MultiPlus AC1 L1 **out** | none - arrays are DC via 2 MPPTs | zero: its AC input is an idle generator port | the output alone |
| **third site** (SolarEdge + Deye, not installed yet) | Deye **load port** | SolarEdge, cabled to that load port | zero: grid meter and Deye grid port agree, nothing between them | `deye + SE` |

Do NOT read the third site as a split house - it is not. `deye + SE + grid`
is the general form with a grid term that happens to be zero.

The difference between a Deye's load port and its grid port is its BATTERY
power (DC-coupled), not a load.

Key facts that are not derivable from the code:

- **Home's M1 meter reports export as POSITIVE** — the opposite of the Energy
  dashboard convention. Verified on 2026-09-14 data: 2177/2177 samples positive
  while the array was over 12 kW. That is why home's house figure is a
  subtraction, not the usual sum.
- **Kozolec is off grid.** Its MultiPlus AC input is a **generator** port, real
  and working, reading zero almost always because the generator is off. Its
  Energy dashboard files that port's energy as `grid_import`.
- **Third site's SolarEdge is on the Deye's LOAD port**, so the Deye's reading
  nets the SolarEdge's production and goes negative when it back-feeds.

Related: 

**Home's sub-meters (2026-09-23).** Directly under the GRID CONNECTION: the
"Hiša" 3EM (the house circuit - "Hiša" means house, so never call the top level
"the house" when talking to the owner; say "the grid connection"), the "Mansarda"
attic 3EM (its phases rotated: its b is the grid's C, its c the grid's A), the
hidrofor plug (it feeds ONLY the pressure pump, the owner 2026-09-28) and the workshop boiler meter (neither is inside a 3EM - the owner
confirmed), plus other plugs. Blaževa Soba is inside Hiša; the NASA station
plug ("Vtičnice - pisarna") is inside Mansarda.

**Home's kiln is wired phase to phase, A-C (measured 2026-09-30).** Off the Hiša 3EM's per-phase
real and apparent power at 256 pulses, its real step is 0.868 / 0.871 of its apparent step on A / C - the
sqrt(3)/2 of one resistive element between two phases, never the 1.0 of two elements to neutral. The owner had
expected phase to neutral. So the kiln's per-phase PF 0.87 is geometry, and its A and C steps are one current.

**Blinds and fans (2026-10-02).** West Blinds (Shelly 2PM Gen4) sits next to the Attic-ac Shelly,
directly on the grid connection; North and East Blinds are behind Mansarda, as is the bathroom fan
(Shelly 1PM Mini G3, ~15 W). The living-room ceiling fan is a CasaFan Eco Neo III 132, RF-controlled
(assumed state), 2.3-27 W over six speeds - no meter.

**Sušilna (2026-10-02).** The Sušilna plug (Shelly PM mini, 60 s reports) feeds a Klarstein
dehumidifier on Tuya Local (`switch.susilna_dehumidifier_power`, mode Low, setpoint 35 %; its
switch drops to `unavailable` ~60 times in ten days for under a second). It runs on a schedule,
20:00 to 16:00 every day, flat ~300 W, on grid phase A - 63 kWh in ten days, Home's biggest
metered device. A 30-60 W load switches on with it at 20:00 ("a separate fan in all likelihood",
The owner). On days it was off (22-23 Sep) another ~270 W load ran 20 h on the same phase from 18:00 -
a timer load, unidentified. Its history is in data/history/home-extra (fetch_history group
`susilna`).

**Attic AC (2026-09-25).** The attic AC is NOT inside Mansarda. One Shelly PM mini
("Attic-ac", `sensor.attic_ac_*`) measures it. MELCloud's
`sensor.wizard_s_attic_ac_energy` is the SAME unit (the owner confirmed), reading about 30%
of the Shelly, so never count both. Site notes are in .

**Unmetered loads found 2026-09-28.** Home's bathroom floor mat (Termostat Kopalnica,
`climate.termostat_kopalnica` hvac_action) is about 635 W on HIŠA phase C, not the attic;
Load Insights calls it "Talno gretje". Kozolec has TWO fridges, one in Apartma and one on the
open bottom floor (effectively outdoors). Both run about 28 min at ~58 W sagging to ~44 W, 20–29
starts a day between them, told apart only by how they start (one has a ~760 W one-reading
surge).

**Kozel (2026-09-29).** The Kozolec Home Assistant also monitors **Kozel**, a nearby rental
house: a 9.9 kWp grid-tied SolarEdge SE10K (`sensor.solaredge_se10k_*`), plus its Vaillant
heating ("Kozel Ogrevanje"). Kozolec's own PV is the two Victron MPPTs (150/85, 150/70).
Never read the SE10K as Kozolec's production.

**EVBox Elvi ("Polnilnica", `sensor.evbox_elvi_power_active_import`, kW, ~10 s) is directly on Home's grid
connection, not in Hiša (2026-10-14).** A charge never shows on the grid meter as one 3-phase step:
the car takes its phases staggered and unequal (12 Oct 19:17 UTC: b +1160 first, a/c +800 4 s later, then
a/c ramp ~600 W over 30-40 s; the rise to 10.5 kW is a joint ~1 kW/phase jump, 20 s ramps on a/c and a
later +1220 on a). So phase votes must come from the sub-meter's steps, not from grid sessions.

**Kozolec battery (2026-10-14):** four JK BMS packs in parallel, 1212 Ah installed, read in HA as one
(sensor.jk_bms_id_512_*; cell ids "address::cell"). By BMS address (JK app names, BLE MACs A4:C1:38:07:..):
00 = "10 kWh pack-00", the older **LF105** cells (2P, 210 Ah; 2C:92) - lowest cell at the top of a charge
(00::15, 00::02); 01 = "LF334-pack-3-01" (2E:5A) - runs highest (01::11, 01::01); 02 = "LF334-pack-2-02"
(2C:59); 03 = "LF334-pack-1--03" (6C:37). All four read 95-96 % on 14 Oct evening (in step).

## Reactive power sources


Home's SolarEdge grid meter publishes signed var per phase (`sensor.solaredge_se17k_m1_ac_var_{a,b,c}`), but its
recorder history only starts 2026-09-30 07:57 UTC, when it was enabled. The local ten-day CSVs have none of it, so
every local replay, bench and graph page built the house's reactive power from the grid meter's V·I, which is
unsigned. That's noise: on 190 of the floor mat's resistive ~630 W steps, the signed var read a median of 0 var
(87 % under 100), while V·I read 152 (37 % under 100).

The Pro 3EMs (Hiša, Mansarda) publish no reactive power and an unsigned PF. Their VAr comes from apparent power,
paired within one update (SAME_UPDATE_S); that is clean (the kiln legs read tan 30° = 0.58, IQR 0.57-0.58).

**Why:** a reactive-power feature for the house's clustering can only be judged on data with signed var, and the
local history lacks it until it is fetched again after ~9 Oct (ten days of signed var).

**How to apply:** before benching anything that reads the house's VAr, fetch the var history and check the
CSVs contain `ac_var`; until ten days exist, judge on today's live data or a sub-meter's borrowed step.


**Update 2026-10-12:** the local history was refetched (2-11 Oct) and holds the signed var; the reactive angle
(EDGE_ANGLE) was benched on it and shipped always-on (LI bd93df0). The third site's SolarEdge meter publishes signed var as
`sensor.solaredge_i2_m1_ac_reactive_power_{a,b,c}` (solaredge_modbus_multi), recorded from 2026-10-08 21:35 UTC and
read by production LI there; fetch_history pulls it since 96cad05 - bench the third site's angle from ~2026-10-18.
Kozolec still has only V x I. Job note: data/scorecard/jobs/angle-2026-10-12.md.


## Shelly reporting


Measured 2026-10-14 from Kozolec recorder data (Pro 4PM fw 2.1.0, 1PM Mini Gen3 fw 2.1.0): a Shelly power reading is pushed early only when it changes by about 5 % of the LAST REPORTED value; otherwise it waits for the device's fixed minute tick (Pro 4PM ~hh:mm:00.7, Gen3 ~:00.0). Undocumented for Switch apower (Pro EM / PM1 docs state 5 % (+10 W / 1 W)); no config key to change it (workaround: an on-device script polling Switch.GetStatus and pushing via MQTT/webhook). Plus Plug S fw 1.7.5 is leakier (~6 s early reports).

Consequence for Load Insights: a step below ~5 % of a loaded Shelly channel's level reaches HA up to 60 s late, e.g. the Kozolec boiler's ~35 W warm-up sag on 1.9 kW. Kozolec's "boiler" is a Pro 4PM channel (with well pump, power strip, washing machine), not a plug; Home's Hidrofor is an IKEA INSPELNING Zigbee plug, not a Shelly.

## Operating notes for the Home Assistant instances

- A HACS dev deploy installs but does NOT restart HA; restart after a check_config.
  The installed build is `update.load_insights_update`'s `installed_version`, stamped in
  UTC (`0.4.1-dev.20260928.1159` = 13:59 local). Diagnostics `integration_manifest.version`
  is the bare manifest version (0.4.1) for every dev build, so confirm a restart by the
  new code's effect. A Load Juggler release bumps the manifest, so there it does tell.
- Naming a Load Insights load after a metered device gives its energy sensor `_2`; rename
  it to `*_detected_energy`.
- Load Insights ignores its own sensors listed in the Energy dashboard (since 56559fa,
  2026-09-28), so detected loads can be listed there.
- Load Insights keys meters by their Energy-dashboard NAME: renaming one splits its
  history, and `reset_detection` re-files it.
- Big history queries through the proxy time out and return an HTML page; fetch one
  entity and a few days per request.

## Remote user site

The remote user site (the third site) is described in Dynamic_OCPP_EVSE docs/live-sites.md. Load Insights has run there as a third data set since 2026-10-03; releases go there only when the owner says so (see the root AGENTS.md).
