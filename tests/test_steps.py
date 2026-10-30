"""Steps: what several meters saw of one change, claimed inside-out (insights/steps.py).

The numbers are the Home dishwasher start of 22 Oct 10:40: Mansarda's C rose 1940 W
at 10:40:46 while the grid's phase X (Mansarda C = grid A) moved 1925 W at 10:40:44.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

S = load("insights.steps")
E = S.Edge
DEPTH = {"Mansarda": 1, "grid": 0}


def test_an_inner_edge_claims_the_grids_and_owns_the_step():
    steps = S.build_steps({"grid": [E("grid", "X", 0.0, 1925.0, 25.0)],
                           "Mansarda": [E("Mansarda", "X", 2.0, 1940.0, 5.0)]}, DEPTH)
    assert len(steps) == 1 and steps[0].owner == "Mansarda" and len(steps[0].edges) == 2
    assert abs(steps[0].changes["X"] - 1940.0) < 1e-9


def test_an_edge_nobody_claims_is_a_step_with_one_edge():
    steps = S.build_steps({"grid": [E("grid", "Z", 0.0, 2100.0, 25.0)],
                           "Mansarda": [E("Mansarda", "X", 2.0, 1940.0, 5.0)]}, DEPTH)
    assert sorted((s.owner, len(s.edges), tuple(s.changes)) for s in steps) == [("Mansarda", 1, ("X",)), ("grid", 1, ("Z",))]


def test_a_bigger_outer_edge_leaves_the_rest_unclaimed():
    steps = S.build_steps({"grid": [E("grid", "X", 0.0, 3000.0, 25.0)],
                           "Mansarda": [E("Mansarda", "X", 0.0, 1940.0, 5.0)]}, DEPTH)
    rest = [s for s in steps if s.owner == "grid"]
    assert len(steps) == 2 and len(rest) == 1 and abs(rest[0].changes["X"] - 1060.0) < 1e-9


def test_a_meter_that_held_its_value_claims_nothing():
    steps = S.build_steps({"grid": [E("grid", "X", 0.0, 1925.0, 25.0)],
                           "Mansarda": [E("Mansarda", "X", 0.0, 1940.0, 5.0, held=True)]}, DEPTH)
    assert [(s.owner, len(s.edges)) for s in steps] == [("grid", 1)]


def test_a_fall_is_claimed_like_a_rise():
    steps = S.build_steps({"grid": [E("grid", "X", 0.0, -1925.0, 25.0)],
                           "Mansarda": [E("Mansarda", "X", 1.0, -1940.0, 5.0)]}, DEPTH)
    assert len(steps) == 1 and steps[0].owner == "Mansarda" and steps[0].size < 0


def test_two_equal_edges_on_two_phases_claim_their_own_phase():
    steps = S.build_steps({"grid": [E("grid", "X", 0.0, 1000.0, 25.0), E("grid", "Z", 0.0, 1000.0, 25.0)],
                           "Mansarda": [E("Mansarda", "X", 0.0, 1000.0, 5.0), E("Mansarda", "Z", 0.0, 1000.0, 5.0)]}, DEPTH)
    assert sorted((tuple(s.changes), tuple(sorted(e.meter for e in s.edges))) for s in steps) == \
        [(("X",), ("Mansarda", "grid")), (("Z",), ("Mansarda", "grid"))]


def test_a_slow_meters_edge_is_claimed_once_its_latency_is_in_the_reach():
    edges = {"grid": [E("grid", "X", 0.0, 1925.0, 25.0)], "Mansarda": [E("Mansarda", "X", 40.0, 1940.0, 5.0)]}
    assert len(S.build_steps(edges, DEPTH)) == 2
    assert len(S.build_steps(edges, DEPTH, reach=60.0)) == 1


if __name__ == "__main__":
    run_main(globals())
