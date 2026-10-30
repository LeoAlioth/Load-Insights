"""Auto-naming of confidently detected loads - the Home Assistant half (INS-21).

The recogniser, the bar's hysteresis, the numbering and the dashboard's list as
it should read are pure and live in `insights/autoname.py`; this module owns
what needs `hass`: the store, the rename, the words, and the write to the
Energy dashboard (`EnergyManager.async_update`, verified against Home
Assistant's `homeassistant/components/energy/data.py`: it takes
`device_consumption` as a whole list, entries `{stat_consumption, name?,
stat_rate?, included_in_stat?}`, and does not validate - so the whole list is
read back, changed in the pure planner and written once).

Run once an hour from `DetectionRunner._check_named`, never while history is
being re-read. Two options, both on when absent (`CONF_AUTONAME`,
`CONF_AUTONAME_DASHBOARD`). The dashboard option covers EVERY named load, the owner's
names too, and does not depend on auto-naming (2026-10-25).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from homeassistant.components.energy.data import async_get_manager
from homeassistant.helpers import entity_registry as er, translation
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import CONF_AUTONAME, CONF_AUTONAME_DASHBOARD, CONF_BASE_LOAD_PHASES, DOMAIN
from .insights import autoname as A
from .insights.classify import FRIDGE, appliance_from_name
from .insights.model import SiteModel

if TYPE_CHECKING:
    from .detection import DetectionRunner

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1
# English fallbacks; the words are in translations/*.json under "exceptions" (autoname_fridge, autoname_heater)
DEFAULT_PATTERNS = {A.FRIDGE_KIND: "Fridge {n}", A.HEATER_KIND: "Heater {n}"}


def _is_fridge_name(name: str) -> bool:
    return appliance_from_name(name) == FRIDGE


class AutoNamer:
    """What detection has named by itself, and what it put on the Energy dashboard."""

    def __init__(self, runner: "DetectionRunner") -> None:
        self._runner = runner
        self._store: Store = Store(runner.hass, STORAGE_VERSION, f"{DOMAIN}.{runner.entry.entry_id}.autoname")
        self.cands: Dict[str, A.Candidate] = {}
        self.marks: Dict[str, dict] = {}              # OUR names: name -> {kind, descriptor, since}
        self.tombs: List[A.Tombstone] = []
        self.added: List[str] = []                    # statistics we put on the dashboard
        self.declined: List[str] = []                 # ...that the owner took out again

    # ------------------------------------------------------------ options
    @property
    def enabled(self) -> bool:
        return bool(self._runner.config.get(CONF_AUTONAME, True))

    @property
    def dashboard(self) -> bool:
        # every named load goes on the dashboard, the owner's names too (2026-10-25): independent of auto-naming
        return bool(self._runner.config.get(CONF_AUTONAME_DASHBOARD, True))

    # ------------------------------------------------------------ store
    async def async_load(self) -> None:
        raw = await self._store.async_load() or {}
        self.cands = {k: A.Candidate.from_dict(v) for k, v in (raw.get("cands") or {}).items()}
        self.marks = {n: dict(m) for n, m in (raw.get("marks") or {}).items()}
        self.tombs = [A.Tombstone.from_dict(t) for t in raw.get("tombs") or []]
        self.added = list(raw.get("added") or [])
        self.declined = list(raw.get("declined") or [])

    async def _save(self) -> None:
        await self._store.async_save({
            "cands": {k: c.to_dict() for k, c in self.cands.items()}, "marks": self.marks,
            "tombs": [t.to_dict() for t in self.tombs], "added": self.added, "declined": self.declined})

    def reset(self, forget_names: bool) -> None:
        """A detection reset: signature ids mean nothing any more, so the
        candidates go. Our names are carried like any other (orphan names) unless
        ``forget_names``, which drops them and their marks too; tombstones stay
        (they describe loads, not ids)."""
        self.cands = {}
        if forget_names:
            self.marks = {}

    # ------------------------------------------------------------ words
    async def _patterns(self) -> Dict[str, str]:
        out = dict(DEFAULT_PATTERNS)
        try:
            words = await translation.async_get_translations(
                self._runner.hass, self._runner.hass.config.language, "exceptions", [DOMAIN])
        except Exception:  # noqa: BLE001 - a missing language is the English words
            return out
        for kind in out:
            text = words.get(f"component.{DOMAIN}.exceptions.autoname_{kind}.message")
            if text and "{n}" in text:
                out[kind] = text
        return out

    # ------------------------------------------------------------ the hourly check
    async def async_check(self) -> None:
        r = self._runner
        if not r.enabled or r.refiling or not r.caught_up:
            return
        changed = False
        if self.enabled:
            changed = await self._name()
        elif self.marks or self.cands:
            self.cands = {}
        changed |= await self._dashboard()
        if changed:
            await self._save()

    async def async_dashboard(self) -> None:
        """After every pass: a load named a minute ago, or a base load whose entity was just made, is on the
        Energy dashboard at the next pass, not at the hour (the owner, 2026-10-30: an hour's wait reads as
        broken). Cheap when nothing changed - plan_dashboard writes nothing."""
        r = self._runner
        if not r.enabled or r.refiling or not r.caught_up:
            return
        if await self._dashboard():
            await self._save()

    def _owner_names(self) -> List[Tuple[str, Tuple[str, int], object]]:
        """(name, ref, signature) of every named signature."""
        r = self._runner
        return [(sig.name, (meter, sig.id), sig) for meter, det in r.fleet.detectors() for sig in det.signatures if sig.name]

    async def _name(self) -> bool:
        r = self._runner
        changed = False
        # 1. what the owner did to our names since the last check
        present = set(r.fleet.names())
        for _, det in r.fleet.detectors():
            present |= {o.get("name") for o in det.orphan_names}
        marks, buried = A.reconcile(self.marks, present)
        if buried:
            _LOGGER.info("Auto-names taken away by the owner (not made again for a load like them): %s",
                         ", ".join(t.name for t in buried))
            self.tombs += buried
            self.marks, changed = marks, True
        # 2. today's facts: what is offered for naming, and what wears one of our names
        facts: Dict[str, A.Facts] = {}
        from .detection import MIN_COUNT_TO_NAME, NAMING_MIN_WH   # detection imports this module
        rows = r.fleet.namable(r.holds_one_device, MIN_COUNT_TO_NAME, NAMING_MIN_WH)
        for group in rows.values():
            for ref, sig in group:
                facts[A.key_of(ref)] = A.facts_of(ref, sig)
        mine: Dict[str, Tuple[Tuple[str, int], object]] = {}
        for name, ref, sig in self._owner_names():
            if name in self.marks:
                facts[A.key_of(ref)] = A.facts_of(ref, sig)
                mine[A.key_of(ref)] = (ref, sig)
        today = dt_util.now().date().isoformat()
        self.cands = A.tick(self.cands, facts, today)
        # 3. a name whose load has not cleared for a week is taken back (never on one bad day)
        for key, (ref, sig) in list(mine.items()):
            c = self.cands.get(key)
            if c is not None and A.lapsed(c):
                _LOGGER.info("Auto-name %s taken back from %s: the load has not cleared the bar for %d days",
                             sig.name, key, A.DAYS_TO_REVOKE)
                gone = sig.name
                await r.async_rename(ref, None)
                self.cands.pop(key, None)
                if gone not in r.fleet.names():
                    self.marks.pop(gone, None)
                changed = True
        # 4. new names
        owned = [(n, A.descriptor(A.facts_of(ref, sig))) for n, ref, sig in self._owner_names() if n not in self.marks]
        taken = set(r.fleet.names()) | set(self.marks) | {o.get("name") for _, det in r.fleet.detectors() for o in det.orphan_names}
        taken |= set(r.all_submeters) | {t.name for t in self.tombs if t.name}
        taken |= {d.label for d in await self._devices()}
        patterns = await self._patterns()
        todo = A.decide({k: f for k, f in facts.items() if k not in mine}, self.cands, self.marks, self.tombs, patterns,
                        taken, owned, _is_fridge_name)
        for key, name, kind in todo:
            ref = facts[key].ref
            if not await r.async_rename(ref, name):
                continue
            self.marks.setdefault(name, {"kind": kind, "descriptor": self.cands[key].descriptor,
                                         "since": dt_util.utcnow().isoformat()})
            _LOGGER.info("Auto-named %s (%s) as %s", key, kind, name)
            changed = True
        if todo:
            r.async_update_listeners()          # the sensors of the new name appear
        return changed

    async def _devices(self) -> list:
        manager = await async_get_manager(self._runner.hass)
        return SiteModel.from_prefs(manager.data or {}).devices

    async def _dashboard(self) -> bool:
        """Put every named load's energy on the Energy dashboard, and each sub-meter's base load under its meter
        (the owner, 2026-10-30) - or take ours off. Only entries in ``added`` are ever removed, so a rename (a new
        energy sensor) moves the entry and an entry the owner made stays his."""
        r = self._runner
        manager = await async_get_manager(r.hass)
        wanted: Dict[str, str] = {}
        rates: Dict[str, str] = {}
        parents: Dict[str, str] = {}
        if self.dashboard:
            for name in sorted(r.fleet.names()):
                if not r.metered_device(name):
                    entity = r.energy_entity(name)
                    if not entity:
                        return False          # its sensor is not made yet (a reload): decide next hour, remove nothing
                    wanted[entity] = name
                    if rate := r.power_entity(name):
                        rates[entity] = rate
            for entity, name, rate, parent in self._base_loads():
                wanted[entity], parents[entity] = name, parent
                if rate:
                    rates[entity] = rate
        written, added, declined = await A.write_dashboard(manager, wanted, self.added, self.declined, rates, parents)
        moved = (added, declined) != (self.added, self.declined)
        self.added, self.declined = added, declined
        if written:
            _LOGGER.info("Energy dashboard device list written (auto-named loads: %s)",
                         ", ".join(sorted(wanted.values())) or "none")
        return written or moved

    def _base_loads(self) -> List[Tuple[str, str, Optional[str], str]]:
        """(energy entity, name, power entity, the meter's statistic) of each sub-meter's base load - the
        entities sensor._base_loads made, so a meter unticked or with a floor under BASE_LOAD_MIN_W has none.
        Never the grid's: the meters' floors are inside it (the owner, 2026-10-30)."""
        from .detection import base_load_label, base_load_uid      # detection imports this module
        r = self._runner
        registry = er.async_get(r.hass)
        keys, split = r.base_load_keys(), set(r.config.get(CONF_BASE_LOAD_PHASES) or [])
        out = []
        for meter, det in r.fleet.detectors():
            key = (r.submeters.get(meter) or {}).get("energy") if meter else None
            if not key or key not in keys:
                continue
            for p in (sorted(det.phases) if key in split and len(det.phases) > 1 else [None]):
                energy = registry.async_get_entity_id("sensor", DOMAIN, base_load_uid(r.entry.entry_id, key, p, "energy"))
                if energy:
                    power = registry.async_get_entity_id("sensor", DOMAIN, base_load_uid(r.entry.entry_id, key, p, "power"))
                    out.append((energy, base_load_label(r.fleet, meter, p), power, key))
        # the remainder meter's (Delavnica, not read): under it on the dashboard as its own was
        rem = r.remainder_meter()
        if rem is not None and rem[1] in keys:
            energy = registry.async_get_entity_id("sensor", DOMAIN, base_load_uid(r.entry.entry_id, rem[1], None, "energy"))
            if energy:
                power = registry.async_get_entity_id("sensor", DOMAIN, base_load_uid(r.entry.entry_id, rem[1], None, "power"))
                out.append((energy, base_load_label(r.fleet, rem[0], None), power, rem[1]))
        return out
