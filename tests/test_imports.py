"""Every name a module uses is one it actually has.

py_compile does not catch this and the pure suite never imports the Home
Assistant layer, so a NameError in a page nobody opens during a test ships
happily. It shipped twice in one afternoon: a coordinator reading self.entry
when its attribute is config_entry, and the Inverters page using
LAYOUT_PARALLEL after an edit took that import out with a neighbouring one
(Anze, 2026-09-18: "the inverters setup page just shows an error").

This reads the source rather than importing it, so it needs no Home
Assistant installed - which is the whole premise of the pure tier.
"""
import ast
import pathlib
import builtins
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import run_main  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "load_insights"
# what a Home Assistant base class provides that a subclass may read
INHERITED = {
    "hass", "config_entry", "data", "async_write_ha_state", "coordinator", "entity_id",
    "async_on_remove", "platform", "logger", "name", "available", "async_request_refresh",
    "last_update_success", "update_interval", "async_set_updated_data", "async_added_to_hass",
    "async_update_ha_state", "registry_entry", "device_entry", "should_poll",
    "async_abort", "async_create_entry", "async_show_form", "async_show_menu",
    "async_set_unique_id", "_abort_if_unique_id_configured",
}


def _scope(tree):
    out = set(dir(builtins))
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            out |= {(a.asname or a.name).split(".")[0] for a in n.names}
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            out.add(n.id)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.alias) and n.asname:
            out.add(n.asname)
    return out


def test_no_module_uses_a_name_it_does_not_have():
    bad = {}
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        missing = sorted(used - _scope(tree))
        if missing:
            bad[path.name] = missing
    assert not bad, bad


def test_no_class_reads_an_attribute_it_never_sets():
    bad = []
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            assigned, read, defined = set(), set(), set()
            for n in ast.walk(cls):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defined.add(n.name)
                elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                    assigned.add(n.target.id)            # a dataclass field
                elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self":
                    (assigned if isinstance(n.ctx, ast.Store) else read).add(n.attr)
            for attr in sorted(read - assigned - defined - INHERITED):
                if not attr.startswith("__"):
                    bad.append(f"{path.name}:{cls.name}.self.{attr}")
    # subclasses legitimately read what their own base sets in __init__
    bad = [b for b in bad if not b.startswith("sensor.py:")]
    assert not bad, bad


def test_no_test_is_written_where_nothing_will_run_it():
    """Two ways a test can sit in the file and never execute, both of which
    happened here on the same afternoon: a second ``def`` of a name silently
    replaces the first, and anything below ``run_main(globals())`` is defined
    after the collection that would have found it. Neither fails, neither
    warns, and the only symptom is a test that cannot be made to fail
    (2026-09-19 - test_detect.py had 27 of the first kind and then, while
    they were being removed, gained three of the second)."""
    dead = []
    for path in sorted(pathlib.Path(__file__).parent.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        main = next((n.lineno for n in tree.body if isinstance(n, ast.If)
                     and ast.dump(n.test).find("__main__") >= 0), None)
        seen = set()
        for n in tree.body:
            if not isinstance(n, ast.FunctionDef) or not n.name.startswith("test_"):
                continue
            if n.name in seen:
                dead.append(f"{path.name}:{n.lineno} {n.name} shadows an earlier one")
            if main is not None and n.lineno > main:
                dead.append(f"{path.name}:{n.lineno} {n.name} is below run_main")
            seen.add(n.name)
    assert not dead, dead


def test_every_way_the_library_is_thrown_away_carries_the_names():
    """Two paths discard the signature library - the reset the user asks for
    and the generation bump they never see - and both must carry names and
    their meter readings across. A third path added later would take a user's
    named devices, their energy sensors and their place on the Energy
    dashboard with it, silently, on every installation at once. This fails
    when one appears (Anze, 2026-09-22)."""
    root = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "load_insights"
    tree = ast.parse((root / "detection.py").read_text(encoding="utf-8"))
    bad = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Attribute) and t.attr == "fleet" for t in n.targets)]
        if not assigns or fn.name == "__init__":     # the constructor's placeholder
            continue
        carries = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and n.func.attr == "carry_names" for n in ast.walk(fn))
        if not carries:
            bad.append(f"{fn.name} (line {fn.lineno}) replaces self.fleet without carry_names")
    assert not bad, bad


if __name__ == "__main__":
    run_main(globals())
