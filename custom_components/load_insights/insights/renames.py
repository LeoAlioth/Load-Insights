"""Following renamed entities through the settings and the Energy dashboard.
Pure: stdlib only.
"""
from __future__ import annotations

from typing import Optional


def follow_renames(value, renames: dict):
    """``value`` - options, data, anything JSON-shaped - with every string
    that IS a renamed entity id, key or value, swapped for its new id. Home
    Assistant moves an entity's history on a rename but not the settings that
    name it: after the owner's renames of 2026-09-28 every input, link and meter
    here had to be picked again by hand."""
    if isinstance(value, str):
        return renames.get(value, value)
    if isinstance(value, list):
        return [follow_renames(v, renames) for v in value]
    if isinstance(value, tuple):
        return tuple(follow_renames(v, renames) for v in value)
    if isinstance(value, dict):
        return {follow_renames(k, renames): follow_renames(v, renames) for k, v in value.items()}
    return value


# The Energy dashboard's lists that name statistics - what
# EnergyManager.async_update takes.
ENERGY_PREF_KEYS = ("energy_sources", "device_consumption", "device_consumption_water")


def dashboard_renames(prefs: Optional[dict], renames: dict) -> tuple:
    """(the renames the Energy dashboard still names, each of its lists as
    following them leaves it - only the lists that change). Home Assistant
    moves a statistic on a rename but not the dashboard entry naming it, so
    the dashboard goes blank for that device (2026-09-29)."""
    lists = {k: (prefs or {})[k] for k in ENERGY_PREF_KEYS if k in (prefs or {})}
    named = {o: n for o, n in renames.items() if follow_renames(lists, {o: n}) != lists}
    moved = {k: follow_renames(v, named) for k, v in lists.items()}
    return named, {k: v for k, v in moved.items() if v != lists[k]}
