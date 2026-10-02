"""The scorecard's own arithmetic - capture, impurity, the floor, the
remainder, the invariance diff - on made-up numbers, no history needed.

    python3 tests/test_energy_bench.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import run_main  # noqa: E402
import energy_bench  # noqa: E402


def test_check():
    energy_bench.check()


if __name__ == "__main__":
    run_main(globals())
