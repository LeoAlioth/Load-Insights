"""The setup pages' headings (custom_components/load_insights/flowgroups.py).

Every grouped field must be labelled under its section in both languages, every
section must have a heading, no grouped label may be left behind at the top of
its step (it would not show), and no field may sit in two sections.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

BASE = Path(__file__).resolve().parents[1] / "custom_components" / "load_insights"
GROUPS = load("flowgroups")
PARTS = (("config", GROUPS.CONFIG), ("options", GROUPS.OPTIONS))


def _steps():
    for lang in ("en", "sl"):
        d = json.loads((BASE / "translations" / f"{lang}.json").read_text(encoding="utf-8"))
        for part, steps in PARTS:
            for step, groups in steps.items():
                yield lang, part, step, groups, d[part]["step"][step]


def test_every_section_has_a_heading_and_its_labels():
    for lang, part, step, groups, obj in _steps():
        shown = set()
        for name, keys, _ in groups:
            sec = obj.get("sections", {}).get(name)
            if sec is None:
                continue                      # a section with no field in the form's labels
            assert sec["name"].strip(), (lang, part, step, name)
            assert set(sec["data"]) <= set(keys), (lang, step, name)
            shown |= set(sec["data"])
        every = {k for _, keys, _ in groups for k in keys}
        assert not every & set(obj.get("data", {})), (lang, part, step, every & set(obj["data"]))
        assert not every & set(obj.get("data_description", {})), (lang, part, step)
        assert shown, (lang, part, step)


def test_no_field_is_in_two_sections():
    for part, steps in PARTS:
        for step, groups in steps.items():
            keys = [k for _, ks, _ in groups for k in ks]
            assert len(keys) == len(set(keys)), (part, step)
            names = [n for n, _, _ in groups]
            assert len(names) == len(set(names)) and not set(names) & set(keys), (part, step)


def test_the_form_code_uses_the_groups():
    src = (BASE / "config_flow.py").read_text(encoding="utf-8")
    assert "GROUPS = OPTIONS_GROUPS" in src and "GROUPS = CONFIG_GROUPS" in src


if __name__ == "__main__":
    run_main(globals())
