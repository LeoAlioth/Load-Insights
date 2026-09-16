"""strings.json and every translation must parse and carry the same keys.

A translation file that does not parse makes Home Assistant fall back to raw
keys for every label in the integration, and nothing in the pure tier would
have noticed - which is exactly how 0.1.0-dev.20260916.0749 shipped with a
stray literal after the closing brace. Parity is checked as the full key
tree, not just the top level, so a label added in one language cannot be
missed in another.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import run_main  # noqa: E402

BASE = Path(__file__).resolve().parents[1] / "custom_components" / "load_insights"
FILES = ("strings.json", "translations/en.json", "translations/sl.json")


def _load(name):
    return json.loads((BASE / name).read_text(encoding="utf-8"))


def _keys(d, prefix=""):
    out = set()
    for k, v in d.items():
        path = f"{prefix}/{k}"
        out.add(path)
        if isinstance(v, dict):
            out |= _keys(v, path)
    return out


def test_every_translation_file_parses():
    for name in FILES:
        _load(name)


def test_strings_and_en_are_identical():
    assert (BASE / "strings.json").read_text(encoding="utf-8") == (BASE / "translations/en.json").read_text(encoding="utf-8")


def test_every_language_has_the_same_keys():
    ref = _keys(_load("strings.json"))
    for name in FILES[1:]:
        assert _keys(_load(name)) == ref, (name, _keys(_load(name)) ^ ref)


def test_no_label_is_empty():
    def walk(d, path=""):
        for k, v in d.items():
            if isinstance(v, dict):
                walk(v, f"{path}/{k}")
            else:
                assert isinstance(v, str) and v.strip(), f"{path}/{k}"
    for name in FILES:
        walk(_load(name))


def test_every_sensor_translation_key_in_code_has_a_name():
    src = (BASE / "sensor.py").read_text(encoding="utf-8")
    import re
    used = set(re.findall(r'"(consumption_forecast|consumption_today|consumption_tomorrow|remainder_forecast|device_forecast|consumption_error_day_ahead|consumption_bias_day_ahead|consumption_error_hour_ahead|consumption_day_ahead_kwh_error|remainder_error_day_ahead|remainder_bias_day_ahead|detected_loads|unknown_load_power)"', src))
    names = set(_load("strings.json")["entity"]["sensor"])
    assert used <= names, used - names


if __name__ == "__main__":
    run_main(globals())
