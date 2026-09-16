"""Import the pure tier without executing the component package.

``custom_components/load_insights/__init__.py`` imports Home Assistant, which
the pure tier must never need. Register stub parent packages whose only job
is to carry ``__path__``, then import the pure modules normally through them.
"""
import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    """``load("insights.profile")`` -> that module, HA-free."""
    for pkg, path in (
        ("custom_components", ROOT / "custom_components"),
        ("custom_components.load_insights", ROOT / "custom_components" / "load_insights"),
    ):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = [str(path)]
            sys.modules[pkg] = m
    return importlib.import_module(f"custom_components.load_insights.{name}")


def run_main(namespace: dict) -> None:
    """LJ-style runner: every ``test_*`` in the module, pytest not required."""
    failed = []
    for name, fn in sorted(namespace.items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failed.append(name)
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print()
    print(f"FAILED - {len(failed)} failure(s)" if failed else "OK - 0 failure(s)")
    sys.exit(1 if failed else 0)
