"""tests/truth_bench.py's own arithmetic on made-up edges: the truths'
labelling and the scores (no replay).

    python3 tests/test_truth_bench.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import run_main  # noqa: E402
import truth_bench as T  # noqa: E402


def st(meter, ch, t, dp, phase="a", noise=10.0):
    return {"meter": meter, "ch": ch, "t": t, "a": t - 6.0, "b": t + 1.0, "dp": dp, "dp0": dp, "g": dp,
            "noise": noise, "floor": noise, "phase": phase}


def edge(i, t, dp, legs, place="", pattern="a", **kw):
    return {"id": i, "t": t, "place": place, "dir": 1 if dp > 0 else -1, "pattern": pattern, "size": abs(dp),
            "g": abs(dp), "err": 10.0, "phases": ["a"], "legs": [k for k, _ in legs],
            "entries": {k: {"t": lt, "dp": dp} for k, lt in legs}, **kw}


def test_dominated_counts_the_unlabelled_and_a_tie_against_the_class():
    groups = [0, 0, 0, 1, 1, 2]
    lab = {0: "x", 1: "x", 3: "y", 5: "y"}
    assert T.dominated(groups, lab) == {"x": 0.0, "y": 0.5}
    assert T.dominated(groups, lab, unknown=frozenset({4})) == {"x": 0.0, "y": 0.0}


def test_nn_is_leave_one_out_on_one_property():
    items = [{"p": v, "cls": c} for v, c in ((0.0, "a"), (0.1, "a"), (5.0, "b"), (5.2, "b"), (2.6, "a"))]
    r = T.nn(items, "p")
    assert r["acc"] == 0.8 and r["by"] == {"a": [2, 3], "b": [2, 2]} and abs(r["bal"] - (2 / 3 + 1) / 2) < 1e-12


def test_kettle_runs_and_the_press():
    steps = [st("Mansarda", "b", 1000.0, 1500.0), st("Mansarda", "b", 1150.0, -1480.0),     # a kettle run
             st("Mansarda", "b", 2000.0, 1500.0), st("Mansarda", "b", 2500.0, -1500.0),     # too long
             st("Mansarda", "b", T.PRESS[0] + 2.0, 1480.0), st("Mansarda", "b", T.PRESS[1] - 3.0, -1470.0),
             st("Mansarda", "c", 1000.0, 1500.0)]
    assert T.kettle_steps(steps) == {0: "kettle", 1: "kettle", 4: "press", 5: "press"}


def test_carried_and_plug_truth():
    steps = [st("Plug", "a", 1000.0, 200.0), st("Plug", "a", 2000.0, 15.0), st("Plug", "a", 3000.0, -200.0)]
    hidden = {"chans": {"": {"a": "a"}}, "floor": {"|a": 20.0},
              "edges": [edge(1, 1003.0, 205.0, [("|a", 1003.0)]), edge(2, 3020.0, -200.0, [("|a", 3020.0)])]}
    truth, lab, found = T.plug_truth("home", [dict(s, meter="Hidrofor") for s in steps], hidden, {"Hidrofor": 6.0})
    # the 15 W step is under the grid's idle noise and its own; the fall's edge is past the plug's latency
    assert truth == {0: "Hidrofor", 2: "Hidrofor"} and lab == {0: "Hidrofor"} and found == {0: True, 2: False}
    lab, found = T.carried(hidden["edges"], [st("", "a", 3020.0, -200.0)], {0: "x"})
    assert lab == {1: "x"} and found == {0: True}


if __name__ == "__main__":
    run_main(globals())
