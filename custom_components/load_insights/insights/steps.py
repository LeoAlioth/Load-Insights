"""Steps: what several meters saw of one change, joined into one object.

An **edge** is one meter's own change; a **step** is the edges of several
meters that are one change (AGENTS.md, Terms). Meters are taken from the
innermost outward: an outer meter's edge is CLAIMED by an inner meter's step of
the same direction, size within both meters' noise plus ``slack`` of the size,
and moment within ``reach`` seconds. What is left of an outer edge too big for
the inner one stays a step of its own (an unclaimed edge); an edge nobody
claims is a step with one edge. The owner is the innermost meter in it.

Pure: no Home Assistant, no detector state (steps-and-claims plan, stage 2).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Sequence

METER_CAL_SLACK = 0.05        # as detect.METER_CAL_SLACK: a meter's gain is learned to this much
CLAIM_REACH_S = 5.0           # a claim's timing: both meters' spans overlap within this


@dataclass
class Edge:
    meter: str
    phase: str                 # the system phase it sits on
    since: float
    size: float                # signed W, in the meter's own terms
    noise: float = 0.0
    held: bool = False         # the meter only held its old level through it: it cannot claim


@dataclass
class Step:
    edges: List[Edge] = field(default_factory=list)
    owner: str = ""
    since: float = 0.0
    changes: Dict[str, float] = field(default_factory=dict)    # system phase -> signed W, the owner's, in the grid's terms

    @property
    def size(self) -> float:
        return sum(self.changes.values())


def _fits(inner: float, outer: float, noise: float, slack: float) -> float:
    """Of ``outer`` how much ``inner`` explains: all of it within tolerance, the inner's whole
    size where the outer is bigger than it by more than tolerance, else 0 (the inner is bigger)."""
    tol = noise + slack * max(abs(inner), abs(outer))
    if abs(abs(outer) - abs(inner)) <= tol:
        return outer
    if abs(outer) > abs(inner) + tol:
        return math.copysign(abs(inner), outer)
    return 0.0


def build_steps(edges: Mapping[str, Sequence[Edge]], depth: Mapping[str, int],
                gain: Callable[[str], float] = lambda m: 1.0,
                slack: float = METER_CAL_SLACK, reach: float = CLAIM_REACH_S) -> List[Step]:
    """Every meter's edges as steps, claimed inside-out. ``depth`` is how many meters a
    meter hangs under (the grid 0); the deepest is taken first."""
    steps: List[Step] = []
    for meter in sorted(edges, key=lambda m: -depth.get(m, 0)):
        g = gain(meter)
        for e in sorted(edges[meter], key=lambda x: x.since):
            if e.held:
                continue
            size, left = e.size * g, e.size * g
            for s in steps:
                if s.owner == meter or e.phase not in s.changes and s.changes:
                    continue
                if abs(s.since - e.since) > reach or (s.size > 0) != (size > 0):
                    continue
                took = _fits(s.size, size, math.hypot(e.noise, max((x.noise for x in s.edges), default=0.0)), slack)
                if took:
                    s.edges.append(e)
                    left = size - took
                    break
            if abs(left) > e.noise + slack * abs(size):
                owned = Step([e], meter, e.since, {e.phase: left})
                steps.append(owned)
    return sorted(steps, key=lambda s: s.since)
