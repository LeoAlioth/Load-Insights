"""Any sensor as an explanatory input: labelled, projected, fitted."""
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

I = load("insights.inputs")
CAL = load("insights.calendars")
P = load("insights.profile")
TZ = ZoneInfo("Europe/Ljubljana")
NOW = datetime(2026, 9, 16, 10, 30, tzinfo=TZ)


def keys(hours):
    return [h.timestamp() for h in hours]


def test_a_sensor_with_few_values_is_used_as_it_is():
    raw = {float(i) * 3600: ["2", "3", "4"][i % 3] for i in range(300)}
    labels, kind = I.label_history(raw)
    assert kind == "categorical"
    assert set(labels.values()) == {"2", "3", "4"} and len(labels) == 300


def test_a_numeric_sensor_is_cut_into_bands():
    raw = {float(i) * 3600: float(i) for i in range(400)}
    labels, kind = I.label_history(raw)
    assert kind == "banded"
    assert set(labels.values()) == {"band 1", "band 2", "band 3", "band 4"}
    counts = {b: sum(1 for v in labels.values() if v == b) for b in set(labels.values())}
    assert max(counts.values()) - min(counts.values()) <= 2, counts


def test_free_text_with_many_values_is_unusable_rather_than_guessed_at():
    raw = {float(i) * 3600: "note %d" % i for i in range(300)}
    labels, kind = I.label_history(raw)
    assert kind == "unusable" and labels == {}
    assert I.label_history({}) == ({}, "empty")


def _tariff(t):
    if t.weekday() >= 5:
        return "3"
    return "2" if 7 <= t.hour < 14 or 16 <= t.hour < 20 else "3"


def _projected_tariff(now):
    start = P.floor_hour(now) - timedelta(weeks=8)
    labels = {h.timestamp(): _tariff(h) for h in P.hour_buckets(start, 8 * 168)}
    horizon = P.hour_buckets(P.floor_hour(now), 168)
    return horizon, I.project(labels, keys(horizon), TZ)


def test_a_weekday_and_hour_schedule_projects_perfectly():
    """A tariff is a function of weekday and hour, so its future is knowable
    from its past - the point of projecting by hour-of-week slot."""
    horizon, out = _projected_tariff(NOW)
    assert len(out) == 168
    wrong = [(h, out[h.timestamp()], _tariff(h)) for h in horizon if out[h.timestamp()] != _tariff(h)]
    assert not wrong, wrong[:4]


def test_a_schedule_projects_across_a_clock_change():
    """Slots are local hours either side of a clock change: the 25 Oct 2026
    change inside the horizon (22 Oct) or the history (20 Nov), the 29 Mar
    one in the history (8 Apr). Read at the offset of 'now' alone, the hours
    across the change landed an hour off: 155, 148 and 148 of 168 right."""
    for now in (datetime(2026, 10, 22, 10, 30, tzinfo=TZ), datetime(2026, 11, 20, 23, 59, tzinfo=TZ),
                datetime(2026, 4, 8, 0, 10, tzinfo=TZ)):
        horizon, out = _projected_tariff(now)
        right = sum(out.get(h.timestamp()) == _tariff(h) for h in horizon)
        assert right == 168, (now, right)


def test_an_unprojectable_input_is_held_briefly_and_then_absent():
    """Alternating weeks are not an hour-of-week schedule, so no slot agrees
    with itself: the current label carries a few hours and then stops."""
    start = P.floor_hour(NOW) - timedelta(weeks=8)
    hist = P.hour_buckets(start, 8 * 168)
    labels = {h.timestamp(): ("high" if h.isocalendar()[1] % 2 == 0 else "low") for h in hist}
    horizon = P.hour_buckets(P.floor_hour(NOW), 168)
    out = I.project(labels, keys(horizon), TZ, current="high")
    assert len(out) == I.HOLD_HOURS, len(out)
    assert set(out.values()) == {"high"}
    assert I.project(labels, keys(horizon), TZ) == {}, "nothing to hold, nothing projected"


def test_one_label_is_refused():
    assert not I.usable({float(i) * 3600: "x" for i in range(200)}), "one label explains nothing"
    assert I.usable({float(i) * 3600: ["a", "b"][i % 2] for i in range(100)}), "how much is enough is the evidence rule's"
    assert I.usable({float(i) * 3600: ["a", "b"][i % 2] for i in range(200)})


def test_an_attached_sensor_is_fitted_exactly_like_a_calendar():
    """The input must explain something the WEEKLY PROFILE cannot see, so it
    alternates by week rather than by weekday - a tariff, which is a function
    of weekday and hour, is already absorbed by the profile (the test below)."""
    start = P.floor_hour(NOW) - timedelta(weeks=10)
    hist = P.hour_buckets(start, 10 * 168)
    horizon = P.hour_buckets(P.floor_hour(NOW), 168)
    level = lambda t: "high" if t.isocalendar()[1] % 2 == 0 else "low"   # noqa: E731

    labels = {h.timestamp(): level(h) for h in hist + horizon}
    sig = CAL.CalendarSignals.from_labels("sensor.thing", labels)
    assert sig.kind == "sensor" and set(sig.titles) == {"high", "low"}

    base = lambda t: 0.8 if 7 <= t.hour < 22 else 0.3          # noqa: E731
    samples = [(t, base(t) * (1.6 if level(t) == "high" else 1.0)) for t in hist]
    fc = P.forecast(samples, NOW, calendars=[sig])
    m = fc.calendars[0]
    assert m.engaged, m
    assert "high" in m.titles and m.titles["high"].on > 1.15, m.titles
    assert m.titles["high"].on > m.titles.get("low", m.existence).on, m.titles


def test_an_input_the_weekly_profile_already_knows_explains_nothing():
    """A tariff is a function of weekday and hour - exactly what the 168-slot
    profile is. Attaching it is harmless and adds nothing, which is the
    honest outcome, not a bug."""
    start = P.floor_hour(NOW) - timedelta(weeks=8)
    hist = P.hour_buckets(start, 8 * 168)
    horizon = P.hour_buckets(P.floor_hour(NOW), 168)
    tariff = lambda t: "2" if (t.weekday() < 5 and 7 <= t.hour < 14) else "3"   # noqa: E731
    labels = {h.timestamp(): tariff(h) for h in hist + horizon}
    sig = CAL.CalendarSignals.from_labels("sensor.tariff", labels)
    base = lambda t: 0.8 if 7 <= t.hour < 22 else 0.3          # noqa: E731
    samples = [(t, base(t) * (1.6 if tariff(t) == "2" else 1.0)) for t in hist]
    fc = P.forecast(samples, NOW, calendars=[sig])
    assert not fc.calendars[0].engaged, fc.calendars[0].titles
    assert [round(v, 6) for _, v in fc.hourly] == [round(v, 6) for _, v in P.forecast(samples, NOW).hourly]


def test_an_input_that_explains_nothing_changes_nothing():
    import random
    rnd = random.Random(5)
    start = P.floor_hour(NOW) - timedelta(weeks=8)
    hist = P.hour_buckets(start, 8 * 168)
    horizon = P.hour_buckets(P.floor_hour(NOW), 168)
    labels = {h.timestamp(): rnd.choice(["x", "y"]) for h in hist + horizon}
    sig = CAL.CalendarSignals.from_labels("sensor.noise", labels)
    base = lambda t: 0.8 if 7 <= t.hour < 22 else 0.3          # noqa: E731
    samples = [(t, base(t)) for t in hist]
    with_input = P.forecast(samples, NOW, calendars=[sig])
    without = P.forecast(samples, NOW)
    assert not with_input.calendars[0].engaged
    assert [round(v, 6) for _, v in with_input.hourly] == [round(v, 6) for _, v in without.hourly]


def test_own_scoped_takes_the_unlinked_and_those_linked_to_the_meter_itself():
    class G:
        def __init__(self, e): self.entity = e  # noqa: E704
    sig = [G("free"), G("on_c"), G("on_o")]
    links = {"on_c": ["c"], "on_o": ["o"]}
    within = {"c": ["c", "w"], "o": ["o", "c", "w"]}.get
    got = lambda stat: [g.entity for g in I.own_scoped(sig, links, within, stat)]  # noqa: E731
    assert got(None) == ["free"] and got("c") == ["free", "on_c"] and got("w") == ["free"]


def test_each_devices_own_inputs_become_links_on_one_list():
    """One list of inputs, each with what it is linked to (2026-09-28:
    "a single storage with the tags only deciding to which things it
    connects to"); a link follows its load's name."""
    migrate_inputs, relink = I.migrate_inputs, I.relink
    old = {"input_entities": ["sensor.occupancy"], "interval": 5,
           "device_state_sensors": {"sensor.talno_gretje_energy": ["sensor.floor", "sensor.room"],
                                    "sensor.boiler_energy": "sensor.floor"}}
    new = migrate_inputs(old)
    assert "device_state_sensors" not in new and new["interval"] == 5
    assert new["input_entities"] == ["sensor.occupancy", "sensor.floor", "sensor.room"]
    assert new["input_links"] == {"sensor.floor": ["sensor.talno_gretje_energy", "sensor.boiler_energy"],
                                  "sensor.room": ["sensor.talno_gretje_energy"]}
    assert migrate_inputs(new) is new                  # nothing left to move
    named = {**new, "input_links": {"sensor.floor": ["load:Mat", "sensor.boiler_energy"]}}
    assert relink(named, "Mat", "Talno gretje")["input_links"]["sensor.floor"] == ["load:Talno gretje", "sensor.boiler_energy"]
    assert relink(named, "Mat", None)["input_links"]["sensor.floor"] == ["sensor.boiler_energy"]


def test_sensors_in_a_devices_area_are_suggested_as_its_inputs():
    targets = {"sensor.fan_energy": "bathroom", "load:Kiln": "workshop", "sensor.evse_energy": None}
    candidates = {"binary_sensor.bathroom_presence": "bathroom", "sensor.bathroom_humidity": "bathroom",
                  "sensor.workshop_t": "workshop", "sensor.garage_t": "garage", "binary_sensor.nowhere": None}
    got = I.suggest_inputs(targets, candidates, ["sensor.bathroom_humidity"])
    assert got == [("binary_sensor.bathroom_presence", "sensor.fan_energy"), ("sensor.workshop_t", "load:Kiln")]
    options = {"interval": 5, "input_entities": ["sensor.bathroom_humidity"],
               "input_links": {"sensor.bathroom_humidity": ["sensor.fan_energy"]}}
    out = I.add_inputs(options, got + [("sensor.bathroom_humidity", "load:Kiln")])
    assert out["interval"] == 5
    assert out["input_entities"] == ["sensor.bathroom_humidity", "binary_sensor.bathroom_presence", "sensor.workshop_t"]
    assert out["input_links"] == {"sensor.bathroom_humidity": ["sensor.fan_energy", "load:Kiln"],
                                  "binary_sensor.bathroom_presence": ["sensor.fan_energy"],
                                  "sensor.workshop_t": ["load:Kiln"]}
    assert options["input_links"] == {"sensor.bathroom_humidity": ["sensor.fan_energy"]}   # not mutated
    assert I.add_inputs(options, []) is options



def test_a_metering_devices_own_temperature_goes_to_that_device_only():
    targets = {"sensor.boiler_energy": "workshop", "sensor.charger_energy": "workshop"}
    candidates = {"sensor.boiler_tank": "workshop", "sensor.bms_cells": "workshop", "sensor.room_t": "workshop"}
    own = {"sensor.boiler_tank": "dev-boiler", "sensor.bms_cells": "dev-bms"}
    devices = {"sensor.boiler_energy": "dev-boiler", "sensor.charger_energy": "dev-charger"}
    got = sorted(I.suggest_inputs(targets, candidates, [], own, devices))
    assert got == [("sensor.boiler_tank", "sensor.boiler_energy"),
                   ("sensor.room_t", "sensor.boiler_energy"), ("sensor.room_t", "sensor.charger_energy")], got


def test_a_linked_input_acts_on_its_device_the_meters_around_it_and_the_site():
    """Home's links (3 Oct): the workshop boiler's tank temperature acts on
    the boiler, on Delavnica it sits in and on the site - no longer on
    Blaz's PC; a link to a named load follows it to its meter; the motion
    sensor and the calendar, linked to nothing, act everywhere; the
    unmetered remainder takes only those."""
    parent = {"sensor.blaz_pc_energy": "sensor.hisa_total_active_energy",
              "sensor.pec_za_glino_energy": "sensor.hisa_total_active_energy",
              "sensor.workshop_boiler_energy": "sensor.workshop_total_energy",
              "sensor.bathroom_fan_switch_0_energy": "sensor.mansarda_total_active_energy"}
    named = {"load:Kiln": "sensor.pec_za_glino_energy"}

    def within(target):
        cur, out = named.get(target, target), []
        while cur and cur not in out:
            out.append(cur)
            cur = parent.get(cur)
        return out
    links = {"sensor.workshop_boiler_temperature_1": ["sensor.workshop_boiler_energy"],
             "binary_sensor.bathroom_presence_occupancy": ["sensor.bathroom_fan_switch_0_energy"],
             "sensor.kiln_thermocouple": ["load:Kiln"],
             "binary_sensor.entrance_sensor_occupancy": []}
    signals = [types.SimpleNamespace(entity=e) for e in (
        "calendar.workday_sensor_calendar", "binary_sensor.living_room_living_area_motionaware_area",
        "binary_sensor.entrance_sensor_occupancy", "sensor.workshop_boiler_temperature_1",
        "binary_sensor.bathroom_presence_occupancy", "sensor.kiln_thermocouple")]
    everywhere = ["calendar.workday_sensor_calendar", "binary_sensor.living_room_living_area_motionaware_area",
                  "binary_sensor.entrance_sensor_occupancy"]

    def takes(stat=None):
        return [g.entity for g in I.scoped(signals, links, within, stat)]
    assert takes() == [g.entity for g in signals]
    assert takes("sensor.blaz_pc_energy") == everywhere
    assert takes("remainder") == everywhere
    assert takes("sensor.workshop_boiler_energy") == everywhere + ["sensor.workshop_boiler_temperature_1"]
    assert takes("sensor.workshop_total_energy") == everywhere + ["sensor.workshop_boiler_temperature_1"]
    assert takes("sensor.mansarda_total_active_energy") == everywhere + ["binary_sensor.bathroom_presence_occupancy"]
    assert takes("sensor.hisa_total_active_energy") == everywhere + ["sensor.kiln_thermocouple"]
    assert takes("sensor.pec_za_glino_energy") == everywhere + ["sensor.kiln_thermocouple"]


if __name__ == "__main__":
    run_main(globals())
