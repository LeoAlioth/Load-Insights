"""Every attribute a sensor publishes is either recorded on purpose or not at all.

Home Assistant stores a state's attributes in the recorder database unless the
entity names them in ``_unrecorded_attributes``. There is a 16 KB ceiling; over
it, HA logs a warning saying the database will suffer and stores nothing. So a
sensor whose attributes carry a whole library has to exclude them - and the
exclusion list drifted out of date the moment it was a HAND-WRITTEN SUBSET,
which is what happened to Detected loads: three keys were named, ten more were
added over time, and Anze's house logged the warning 989 times (2026-09-22).

This reads the dict each ``extra_state_attributes`` returns and checks the keys
against what the class excludes, so the two cannot drift apart again. Sensors
whose attributes are small and worth keeping opt out by name below.
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import run_main  # noqa: E402

SENSOR = Path(__file__).resolve().parents[1] / "custom_components" / "load_insights" / "sensor.py"

# Classes whose attributes are deliberately RECORDED - a handful of small
# scalars each, worth having a history of.
RECORDS_ITS_ATTRIBUTES = {
    "UnknownLoadPowerSensor", "BaseLoadSensor", "_Base", "_DetectionBase", "_GridBase",
}


def _classes(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            yield node


def _excluded(cls):
    """The frozenset({...}) assigned to _unrecorded_attributes, as a set."""
    for node in cls.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "_unrecorded_attributes" for t in node.targets):
            continue
        out = set()
        for s in ast.walk(node.value):
            if isinstance(s, ast.Constant) and isinstance(s.value, str):
                out.add(s.value)
        return out
    return None


def _published(cls):
    """Attribute keys whose value is BUILT - a comprehension or a literal
    collection. A scalar (a count, a timestamp, a flag) is small and may well
    be worth a history; a list or dict assembled per update is the shape that
    grows with the site until it hits the ceiling, and those are the ones a
    sensor has to make a decision about."""
    for node in cls.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "extra_state_attributes":
            continue
        built = set()
        for ret in [n for n in ast.walk(node) if isinstance(n, ast.Return)]:
            for d in [n for n in ast.walk(ret) if isinstance(n, ast.Dict)]:
                for k, v in zip(d.keys, d.values):
                    if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                        continue
                    if isinstance(v, (ast.ListComp, ast.DictComp, ast.SetComp,
                                      ast.List, ast.Dict)):
                        built.add(k.value)
                break                      # the outermost dict only
        return built
    return None


def test_an_attribute_assembled_per_update_is_kept_out_of_the_database():
    """What a plain reading of the source can see: a value built by a
    comprehension grows with the site. It cannot see one returned by a method
    call - ``named_loads`` is a list too - so this is a floor, not a proof.
    """
    tree = ast.parse(SENSOR.read_text(encoding="utf-8"))
    checked = 0
    for cls in _classes(tree):
        if cls.name in RECORDS_ITS_ATTRIBUTES:
            continue
        built = _published(cls)
        if built is None:
            continue                       # publishes no attributes of its own
        excluded = _excluded(cls) or set()
        missing = sorted(built - excluded)
        assert not missing, (
            f"{cls.name} assembles {missing} on every update and lets the "
            f"recorder store them - add them to _unrecorded_attributes")
        checked += 1
    assert checked >= 3, f"only {checked} sensors examined - did the parse work?"


def test_detected_loads_keeps_nothing_it_publishes():
    """The one the ceiling was actually breached by. It excludes every key it
    returns, built or not - a signature library, the per-submeter breakdown
    and the last forty sessions have no business in the database, and the
    state (how many unexplained loads are on) is the recordable part."""
    tree = ast.parse(SENSOR.read_text(encoding="utf-8"))
    cls = next(c for c in _classes(tree) if c.name == "DetectedLoadsSensor")
    excluded = _excluded(cls)
    built = _published(cls)
    assert built, "could not read the attribute keys"
    assert built <= excluded, sorted(built - excluded)
    for key in ("meters", "signatures", "recent_sessions", "named_loads",
                "meter_hierarchy", "looks_like_one_device", "caught_up"):
        assert key in excluded, key


if __name__ == "__main__":
    run_main(globals())
