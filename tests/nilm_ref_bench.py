"""ha-nilm (open-source, pretrained few-shot NILM) scored on LI's exports and plug truths,
in LI's terms: capture and impurity per measured load, kWh beside the percentages.

    VENV=/private/tmp/claude-1773206459/-Users-anzek-Projects/a1c5cef5-a767-4118-8e59-86b14a1066eb/scratchpad/nilmvenv
    $VENV/bin/python tests/nilm_ref_bench.py [SITE=home] [KEY=value ...]       # ~4 min on this Mac, 2.7 GB
    $VENV/bin/python tests/nilm_ref_bench.py MAINS=phase LABEL=phase           # each load on its best single phase
    $VENV/bin/python tests/nilm_ref_bench.py SITE=home-mansarda                # mains = the circuit the plug sits in
    $VENV/bin/python tests/nilm_ref_bench.py SELFTEST="Hidrofor,Polnilnica"    # an estimate equal to the plug must score ~100 / 0
    $VENV/bin/python tests/nilm_ref_bench.py REPORT=data/nilm_ref/<run>.json   # reprint a saved run
    $VENV/bin/python tests/nilm_ref_bench.py WORKFLOW=1 LABEL=wf               # the practitioner workflow, below
Keys: WORKFLOW VAL(4) CANDS(4) F1_MIN(0.5) IMP_MAX(0.5) SITE TRAIN SCORE LOADS MODES(one,day) REFS(2) MAINS(total,phase) BUDGET(live|preview) BRIDGE MARGIN PARITY LABEL.
Every run first checks that the batch path used here equals the product's live path (PARITY=1: ha-nilm's own
RefQueryDisaggregator.disaggregate_next fed sample by sample; max difference 8e-7 in on-probability, 0.01 W).

Nothing live: reads data/history/*, ha-nilm's code at /Users/anzek/Projects/ha-nilm (imported,
never modified, no bytecode written there), writes only data/nilm_ref/ (embeddings, query-embedding
cache, one results JSON per run) and LI's own parse cache in ~/.cache/load_insights_bench.

SETUP (once; python3.12 venv outside both repos, ~1.5 GB because of TensorFlow):
    uv venv --python /opt/homebrew/bin/python3.12 $VENV
    uv pip install --python $VENV/bin/python numpy ai-edge-litert h5py tensorflow tf_keras
ai-edge-litert is the TFLite runtime ha-nilm imports as tflite_runtime (shimmed in nilm()); TensorFlow
is needed ONLY to train the per-appliance reference embedding (see below), tf_keras because
ha-nilm's head.h5 is a Keras-2 file (TF_USE_LEGACY_KERAS=1, set here).

WHAT HA-NILM IS, as read from the code (not what its README suggests)
  * extractor.tflite: 599 mains samples at 8 s (80 min, ZOH-resampled, window minimum subtracted,
    (x-258.7)/682.8) -> a 128-d query embedding. One extraction per 8 s step.
  * head.tflite: (reference embedding of ONE appliance, query embedding) -> (power, on-probability).
  * The reference embedding is NOT the extractor's output for a labelled window. It is a 128-d vector
    LEARNED by gradient descent (Adam, lr 0.01, <=200 epochs, patience 5, seed 0) through the FROZEN
    head (nilm_trainer/app/trainer.py, Keras head.h5), against labelled windows of a training range
    (<= 7 days in the product): on/off BCE, plus power MSE with a plug (supervision "sensor") or a
    weak pseudo-power with ON intervals only (supervision "intervals", two stages). Every window of
    the range that is not inside a labelled ON interval is an OFF example, so "one reference
    interval" means one range with that interval labelled and nothing else ON in it.
  * the edge then picks the on/off threshold by best F1 of the TFLite head over the training windows
    (edge_replay_worker) and publishes power (0 below the threshold, capped at mains - window min).
  Training here = their own prepare_training_data.build_embeddings_training_payload + trainer.
  train_ref_embedding, imported unchanged. No training server, no Home Assistant. TensorFlow 2.21 /
  tf_keras 2.21 instead of their pinned 2.15 (no 2.15 wheel for python3.12 arm64): same maths, last
  digits of the learned vector may differ.

WHAT IS SCORED
  mains   MAINS=total (default): the three phases of LI's production house reading (bench._prod_house,
          the series the LI card replays), summed, ZOH onto an 8 s grid. MAINS=total,phase adds one
          run where each load uses the single phase whose 5-min mean correlates best with its plug on
          the REFERENCE days (the product's advice: one model per mains sensor, trained on the
          sensor that measures its circuit).
  truth   energy_bench.measured(): the plugs LI scores as "devices" (_tables), their floor and
          energy above it (_truth) - LI's own functions, called not copied.
  train   TRAIN=data/history/home-older (6 Sep - 2 Oct; a comma list pools folders) vs score
          data/history/home (3-13 Oct, the span the LI card scores): reference days all lie before
          the scored span. (15-25 Oct is no use for the EV charger: it did not charge.)
  modes   one: ONE labelled ON interval (the plug's highest-energy run on the rank-th best day, from
                ha-nilm's own sensor_intervals rule: compute_on_mask 20 W / 60 s / 300 s) with +-MARGIN
                hours around it as the range (ON intervals that fall inside are labelled too).
          day: the whole best day, plug as "sensor" supervision (power + on/off). Upper bound; what
                ha-nilm's sensor mode gives a user who owns the plug.
          REFS=2 trains on the two best days per load (rank 0, 1): run-to-run spread of the reference.
  Two definitions of capture / impurity, both with kWh (truth / attributed / right / wrong):
    LI   LI's session attribution applied to ha-nilm's output: ha-nilm's ON runs (gaps <= BRIDGE s
         bridged) are the sessions of that model, which "owns" them all; a session is the load's only
         if the plug rose with it (energy_bench._started_with) and then it is credited min(its energy,
         what the plug drew above its floor over its span), every plug moment once, shortest session
         first (energy_bench._credit). capture = right / truth, impurity = (attributed - right) /
         attributed. Same functions, same rules, as the card.
    OVL  power overlap on the 8 s grid: right = sum(min(est W, plug W above floor)). No timing rule;
         an upper bound of LI's for the same estimate.
  WORKFLOW=1 (what the README tells a person: label a range completely, train, look at the preview, retrain)
          The preview has no threshold or power control (the on/off threshold is chosen by best F1 inside training), so a
          person's levers are the training range, the labels and which models to keep. Emulated without touching the scored span:
          * week: per load, CANDS contiguous 7-day ranges of the reference folder (plug as "sensor" supervision), plus the
            load's best single day ("d"); days with a data gap (house reading silent > 10 min) are never used.
          * preview: each candidate is scored on a VALIDATION window = the last VAL clean days of the reference folder, held out
            of every candidate's training (training pool ends before it). The candidate with the best validation energy-F1
            (2 * right / (truth + attributed), overlap definition) is kept - the person watching the preview and retraining.
            It is then scored unchanged on the scored span. Candidates are also scored on the span, for the SPREAD only.
          * kept models: a load is dropped when its kept candidate's validation energy-F1 < F1_MIN (0.5) or overlap impurity
            > IMP_MAX (50 %), the person rejecting a model whose preview looks wrong; pooled numbers for the kept set are
            printed beside the all-loads numbers, with the LI card restricted to the same loads.
          Guesses: the choice metric (energy-F1), the rejection rule, the candidate starts (evenly spread week starts), 4 days of
          validation, "best single day" as the d candidate.
  BUDGET=live (default) caps each load at mains - window minimum, as the live addon does;
  BUDGET=preview scales all loads of a mains feed to that budget, as the UI preview does.
  Side figure: on/off F1 against ha-nilm's own plug-derived ON mask.

GUESSES (not in either code base): BRIDGE=120 s joins ha-nilm's ON flicker into one session;
a session's "starting watts" for _started_with is the mean estimate over its first 4 steps;
MARGIN=2 h; which day / interval is "the" reference (highest plug energy); the card compared
against is LI_CARD (hidden mode, defaults, same 3-13 Oct data, a different checkout's folder).
"""
from __future__ import annotations

import asyncio
import bisect
import datetime as dt
import hashlib
import importlib.util
import json
import os
import resource
import sys
import time
import types
from pathlib import Path
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True
os.environ.setdefault("TF_USE_LEGACY_KERAS", "1")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
import numpy as np  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import bench as B  # noqa: E402
import energy_bench as EB  # noqa: E402

R = B.R
NILM = Path("/Users/anzek/Projects/ha-nilm")
BUNDLE = NILM / "nilm_edge/inference/bundles/online_v1"
HEAD_H5 = NILM / "nilm_trainer/bundles/online_v1/head.h5"
OUT = ROOT / "data" / "nilm_ref"
TZ = ZoneInfo("Europe/Ljubljana")
DT = 8.0
T_WIN = 599
LI_CARD = ROOT / "data/scorecard/0eaf24f-hint_default-20261026T122343Z.json"
# ha-nilm's trainer config (nilm_trainer/app/main.py SERVER_TRAINING), used as given
TRAINING = dict(epochs=200, lr=0.01, batch_size=1024, seed=0, patience=5, min_delta=1e-4)
DEFAULTS = {"SITE": "home", "TRAIN": "", "SCORE": "", "LOADS": "", "MODES": "one,day", "REFS": "2", "MAINS": "total",
            "BUDGET": "live", "BRIDGE": "120", "MARGIN": "2", "PARITY": "1", "LABEL": "", "REPORT": "", "SELFTEST": "", "WORKFLOW": "0", "VAL": "4", "CANDS": "4", "F1_MIN": "0.5", "IMP_MAX": "0.5"}
TRAIN_DEFAULT = {"home": "data/history/home-older", "kozolec": "data/history/kozolec-older"}


def nilm():
    """ha-nilm's modules, imported from their repo, with the TFLite runtime shimmed under its old name."""
    from ai_edge_litert import interpreter as it
    pkg = types.ModuleType("tflite_runtime")
    pkg.interpreter = it
    sys.modules["tflite_runtime"], sys.modules["tflite_runtime.interpreter"] = pkg, it
    sys.path.insert(0, str(NILM / "nilm_edge/src"))
    import edge_preview_worker as ew
    import edge_replay_worker as er
    import embedding_store as es
    import prepare_training_data as ptd
    import refquery as rq
    spec = importlib.util.spec_from_file_location("nilm_trainer_trainer", NILM / "nilm_trainer/app/trainer.py")
    tr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tr)
    return types.SimpleNamespace(ew=ew, er=er, es=es, ptd=ptd, rq=rq, tr=tr)


# ------------------------------------------------------------------ data
def load(folder: str, site: str):
    """(phase -> house rows, load -> plug rows) of a folder, exactly as the LI card builds them."""
    B._apply(["SUBS=none"] + (["HOUSE=prod"] if site in B.PROD_HOUSE else []))
    dev, _ = EB.measured(folder, site)
    s = R.read_csv([folder], False, say=lambda *a, **k: None)
    if site in B.PROD_HOUSE:
        B._prod_house(s, site=site)
    return {p: s[e] for p, e in B.SITES[site]["main"].items() if s.get(e)}, dev


def grid_of(nl, phases: dict):
    """The 8 s grid (integer seconds, so ha-nilm's ms round trip is exact) and each phase + 'total' on it."""
    t0 = float(np.ceil(max(r[0][0] for r in phases.values())))
    t1 = min(r[-1][0] for r in phases.values())
    grid = nl.ptd.build_uniform_grid(t0, t1, DT, align="start")
    y = {p: nl.ptd.zoh_resample_to_grid(r, grid, max_hold_s=None, fill_value=0.0).astype(np.float64) for p, r in phases.items()}
    y["total"] = sum(y[p] for p in phases)
    return grid, y


def gaps_of(phases: dict, min_s: float = 600.0):
    """[(start, end)] where any phase of the house reading is silent for more than min_s: ZOH holds a stale value over them,
    so a reference range must not contain one (the first run trained on 17 Sep with a 2.3 h hole, 07:59-10:17)."""
    out = []
    for r in phases.values():
        t = np.array([x[0] for x in r])
        out += [(float(t[i]), float(t[i + 1])) for i in np.flatnonzero(np.diff(t) > min_s)]
    return out


def clean_days(tgrid, gaps):
    """Local days fully inside the grid and free of gaps."""
    return [(a, b) for a, b in local_days(tgrid[0], tgrid[-1]) if not any(g0 < b and g1 > a for g0, g1 in gaps)]


def queries(nl, grid, y, tag: str):
    """Query embeddings of every 599-step window of y (ha-nilm's preview path), cached on disk.
    -> Q (N,128), label time (N), mains at label (N), window minimum (N); N = len(grid) - 598."""
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "cache").mkdir(exist_ok=True)
    key = hashlib.sha1(f"{tag}|{grid[0]}|{len(grid)}|{y[:5000].sum():.3f}|{y[-5000:].sum():.3f}".encode()).hexdigest()[:16]
    path = OUT / "cache" / f"q-{key}.npz"
    if path.exists():
        z = np.load(path)
        return z["Q"], z["t"], z["m"], z["b"]
    ctx = nl.ew.build_offline_preview_context(list(zip(grid.tolist(), y.tolist())), str(BUNDLE), max_hold_s=None)
    ex = nl.ptd.QueryExtractor(str(BUNDLE / "extractor.tflite"), num_threads=2)
    Q, T, M, Bl = [], [], [], []
    for b in nl.ew.iter_preview_batches(ctx, 1024):
        emb, ok = ex.extract_embeddings(ex.build_input_batch(b["windows"]), return_mask=True)
        Q.append(emb), T.append(b["label_times_ms"][ok] / 1000.0), M.append(b["mains_at_label"][ok]), Bl.append(b["baseload_at_label"][ok])
    Q, T, M, Bl = (np.concatenate(a) for a in (Q, T, M, Bl))
    np.savez(path, Q=Q, t=T, m=M, b=Bl)
    return Q, T, M, Bl


def local_days(t0: float, t1: float):
    d = dt.datetime.fromtimestamp(t0, TZ).date() + dt.timedelta(days=1)
    while True:
        a = dt.datetime.combine(d, dt.time(), TZ).timestamp()
        b = dt.datetime.combine(d + dt.timedelta(days=1), dt.time(), TZ).timestamp()
        if b > t1:
            return
        yield a, b
        d += dt.timedelta(days=1)


# ------------------------------------------------------------------ reference selection + training
def runs_of(mask):
    """[(i, j)] inclusive index runs of 1s."""
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1) - 1))


def references(nl, TR, name: str, mode: str, rank: int, margin_h: float):
    """The training range of one load: (folder index, i0, i1 exclusive, intervals or None, note); TR is a list of
    (grid, mains by key, plugs) per reference folder, days pooled across them."""
    days = []
    for fi, (tgrid, _, tdev, gaps) in enumerate(TR):
        if name not in tdev:
            continue
        floor = EB._truth(tdev[name])[1]
        pg = np.maximum(nl.ptd.zoh_resample_to_grid(tdev[name], tgrid, max_hold_s=None, fill_value=0.0), 0.0)
        for a, b in clean_days(tgrid, gaps):
            i0, i1 = np.searchsorted(tgrid, [a, b])
            days.append((float(np.maximum(pg[i0:i1] - floor, 0).sum() * DT / 3600.0), fi, i0, i1))
    days = sorted((d for d in days if d[0] > 0), reverse=True)
    if len(days) <= rank:
        raise RuntimeError(f"no day {rank} with {name} running in the reference folders")
    kwh, fi, i0, i1 = days[rank]
    tgrid, _, tdev, _ = TR[fi]
    rows, floor = tdev[name], EB._truth(tdev[name])[1]
    pg = np.maximum(nl.ptd.zoh_resample_to_grid(rows, tgrid, max_hold_s=None, fill_value=0.0), 0.0)
    mask = lambda s: nl.ptd.compute_on_mask(s, sample_period_s=DT, threshold_watts=20.0, window_hours=24.0, min_on_s=60.0, min_off_s=300.0)  # noqa: E731
    if mode == "day":
        return fi, i0, i1, None, f"day {dt.datetime.fromtimestamp(tgrid[i0], TZ):%m-%d}, plug {kwh / 1000:.2f} kWh above floor"
    runs = runs_of(mask(pg[i0:i1]))
    if not runs:
        raise RuntimeError(f"{name}: no ON interval on the reference day")
    best = max(runs, key=lambda r: float(np.maximum(pg[i0 + r[0]:i0 + r[1] + 1] - floor, 0).sum()))
    m = int(margin_h * 3600 / DT)
    a, b = max(i0, i0 + best[0] - m), min(i1, i0 + best[1] + 1 + m)
    b = max(b, a + T_WIN + 50)
    labelled = [(a + i, a + j) for i, j in runs_of(mask(pg[a:b]))]
    note = (f"1 interval {dt.datetime.fromtimestamp(tgrid[i0 + best[0]], TZ):%m-%d %H:%M}"
            f"+{(best[1] - best[0] + 1) * DT / 60:.0f} min, range {(b - a) * DT / 3600:.1f} h, {len(labelled)} labelled ON")
    return fi, a, b, [{"start": tgrid[i] * 1000.0, "end": (tgrid[j] + DT) * 1000.0} for i, j in labelled], note


def train_one(nl, name, mode, TR, key, rng, edir: Path):
    """Train + deploy one reference embedding exactly as the product chain does; returns its meta."""
    fi, a, b, intervals, note = rng
    tgrid, tys, tdev, _ = TR[fi]
    ty, rows = tys[key], tdev[name]
    pts = [{"x": float(t) * 1000.0, "y": float(v)} for t, v in zip(tgrid[a:b], ty[a:b])]
    kw = dict(fullSensorHistoryData=pts, appliance_name=name, inference_dir=str(BUNDLE), embeddings_only=True, num_threads=2,
              batch_size=1024, align_grid="start", max_hold_s=None, appliance_sensor_id="plug" if mode == "day" else None)
    if mode == "day":
        t_a = float(tgrid[a])
        first = nl.ptd.zoh_resample_to_grid(rows, np.array([t_a]), max_hold_s=None, fill_value=0.0)[0]
        plug = [{"x": t_a * 1000.0, "y": float(first)}] + [{"x": t * 1000.0, "y": float(v)} for t, v in rows if t_a < t <= tgrid[b - 1] + DT]
        kw.update(supervision_mode="sensor", applianceSensorHistoryData=plug)
    else:
        kw.update(supervision_mode="intervals", selectedWindows=intervals)
    t0 = time.time()
    pay = nl.ptd.build_embeddings_training_payload(**kw)
    t1 = time.time()
    emb, stats, params = nl.tr.train_ref_embedding(
        query_embeddings=pay["embeddings"], targets_on=pay["targets_on"], targets_power=pay.get("targets_power"),
        weak_mains=pay.get("weak_mains"), settings=pay["settings"], head_model_path=str(HEAD_H5), **TRAINING)
    t2 = time.time()
    edir.mkdir(parents=True, exist_ok=True)
    nl.es.save_embedding_npy(str(edir), name, emb)
    dis = nl.rq.RefQueryDisaggregator(inference_dir=str(BUNDLE), embeddings_dir=str(edir), num_threads=2, history_fetcher=None, top_k=None)
    safe = nl.es.sanitize_name(name)
    ref = dis._load_embedding(safe)
    E = np.asarray(pay["embeddings"], dtype=np.float32)
    scores = np.array([dis._run_head(ref, E[i:i + 1])[1] for i in range(len(E))], dtype=np.float32)
    thr, f1 = nl.er._best_prob_threshold(np.asarray(pay["targets_on"]), scores)     # the edge's deployment threshold
    nl.es.save_embedding_metadata(str(edir), name, {"onoff_threshold": float(thr), "power_threshold": float(params.get("power_threshold", 0.0)),
                                                    "onoff_f1": float(f1), "trainer": stats, "range_note": note})
    return {"note": note, "n_windows": len(E), "on_fraction": pay["stats"]["on_fraction"], "epochs_ran": stats["epochs_ran"],
            "final_loss": stats["final_loss"], "onoff_threshold": float(thr), "train_f1": float(f1),
            "prepare_s": t1 - t0, "train_s": t2 - t1, "safe": safe}


# ------------------------------------------------------------------ inference + scoring
def heads(nl, edir: Path, safes: list, Q):
    dis = nl.rq.RefQueryDisaggregator(inference_dir=str(BUNDLE), embeddings_dir=str(edir), num_threads=2, history_fetcher=None, top_k=None)
    refs = [dis._load_embedding(s) for s in safes]
    P, O = np.empty((len(safes), len(Q)), np.float32), np.empty((len(safes), len(Q)), np.float32)
    for i in range(len(Q)):
        q = Q[i:i + 1]
        for k, ref in enumerate(refs):
            P[k, i], O[k, i], _ = dis._run_head(ref, q)
    return dis, P, O


def deploy(P, O, thr, avail, rule):
    p = np.where(O >= np.asarray(thr)[:, None], np.maximum(P, 0.0), 0.0)
    if rule == "live":
        return np.minimum(p, avail[None, :])
    tot = p.sum(0)
    return p * np.minimum(1.0, avail / np.maximum(tot, 1e-9))[None, :]


TIMES: dict = {}


class Sess:
    def __init__(self, start, end, wh, first_w):
        self.start, self.end, self.energy_wh, self.levels = start, end, wh, {"x": [(start, first_w)]}


def sessions(t, p, on, bridge_s):
    out = []
    runs = runs_of(on)
    merged = []
    for i, j in runs:
        if merged and t[i] - (t[merged[-1][1]] + DT) <= bridge_s:
            merged[-1] = (merged[-1][0], j)
        else:
            merged.append((i, j))
    for i, j in merged:
        out.append(Sess(float(t[i]), float(t[j]) + DT, float(p[i:j + 1].sum() * DT / 3600.0), float(p[i:min(i + 4, j + 1)].mean())))
    return out


def score(nl, name, rows, t, p, on, bridge_s):
    """Everything about one load: kWh and rates in LI's two definitions, F1."""
    floor = EB._truth(rows)[1]
    times = TIMES.setdefault(id(rows), [r[0] for r in rows])
    a, b = float(t[0]), float(t[-1]) + DT
    truth_wh = EB._above(rows, times, a, b, floor)
    raw = np.maximum(nl.ptd.zoh_resample_to_grid(rows, t, max_hold_s=None, fill_value=0.0).astype(np.float64), 0.0)
    tr = np.maximum(raw - floor, 0.0)
    gap = np.diff(t) <= 300.0
    est_wh = float((((p[:-1] + p[1:]) / 2.0) * DT * gap).sum() / 3600.0)         # the trapezoid of ha-nilm's energy entity
    ovl_right = float(np.minimum(p, tr).sum() * DT / 3600.0)
    ovl_att = float(p.sum() * DT / 3600.0)
    res = {"truth_kwh": truth_wh / 1000.0, "est_kwh": est_wh / 1000.0, "floor_w": floor,
           "ovl": _rates(truth_wh, ovl_att, ovl_right)}
    for br in sorted({0.0, bridge_s, 600.0}):
        left, att, right = [], 0.0, 0.0
        for s in sorted(sessions(t, p, on, br), key=lambda s: (s.end - s.start, s.start)):
            if s.energy_wh <= 0:
                continue
            att += s.energy_wh
            if EB._started_with(rows, times, s):
                right += EB._credit(left, rows, times, floor, s.start, s.end, s.energy_wh)
        res["li" if br == bridge_s else f"li_bridge{br:g}"] = _rates(truth_wh, att, right)
    truth_on = nl.ptd.compute_on_mask(raw, sample_period_s=DT, threshold_watts=20.0, window_hours=24.0,
                                      min_on_s=60.0, min_off_s=300.0).astype(bool)
    res["f1"] = nl.er._binary_f1_score(truth_on, on)
    return res


def _rates(truth_wh, att_wh, right_wh):
    return {"truth_kwh": truth_wh / 1000.0, "attributed_kwh": att_wh / 1000.0, "right_kwh": right_wh / 1000.0,
            "wrong_kwh": (att_wh - right_wh) / 1000.0, "capture": right_wh / truth_wh if truth_wh else None,
            "impurity": (att_wh - right_wh) / att_wh if att_wh else None}


def parity(nl, edir, safes, grid, y, Q):
    """The product's own live path (RefQueryDisaggregator.disaggregate_next, sample by sample) against the batch
    path used here, on the first 1200 steps: raw head outputs must agree (live labels a window one step late)."""
    dis = nl.rq.RefQueryDisaggregator(inference_dir=str(BUNDLE), embeddings_dir=str(edir), num_threads=2, history_fetcher=None, top_k=None)
    _, P, O = heads(nl, edir, safes, Q[:600])

    async def feed():
        got = {}
        for k in range(0, 2 * T_WIN + 1):
            r = await dis.disaggregate_next(float(y[k]), float(grid[k]), appliances=safes)
            if r is not None:
                got[k] = r["appliances"]
        return got
    got = asyncio.run(feed())
    d_on = d_pw = 0.0
    n = 0
    for k, ap in got.items():
        i = k - 599
        if not 0 <= i < 600:
            continue
        for s, name in enumerate(safes):
            if name in ap:
                d_on = max(d_on, abs(ap[name]["onoff"] - float(O[s, i])))
                d_pw = max(d_pw, abs(ap[name]["power_norm"] * dis.settings.power_std + dis.settings.power_mean - float(P[s, i])))
                n += 1
    return {"compared": n, "max_onoff_diff": d_on, "max_power_diff_w": d_pw}


# ------------------------------------------------------------------ main
def main(argv):
    cfg = dict(DEFAULTS)
    for a in argv:
        k, _, v = a.partition("=")
        if k not in cfg:
            raise SystemExit(f"unknown {k}; keys: {', '.join(cfg)}")
        cfg[k] = v
    if cfg["REPORT"]:                                    # reprint a saved run, nothing recomputed
        rep = json.loads(Path(cfg["REPORT"]).read_text(encoding="utf-8"))
        if rep.get("workflow"):
            return report_workflow(rep)
        first = next(iter(rep["results"].values()))
        return report(rep, [n for n in first if not n.startswith("_")])
    site = cfg["SITE"]
    score_folder = str(ROOT / cfg["SCORE"]) if cfg["SCORE"] else EB._folder(site)      # home-mansarda scores on home's folder, as LI does
    if cfg["SELFTEST"]:                                  # the scoring wiring: an estimate equal to the plug must score ~100 % / ~0 %
        nl = nilm()
        sph, sdev = load(score_folder, site)
        grid, _ = grid_of(nl, sph)
        t = grid[T_WIN - 1:]
        for n in cfg["SELFTEST"].split(","):
            floor = EB._truth(sdev[n])[1]
            p = np.maximum(nl.ptd.zoh_resample_to_grid(sdev[n], t, max_hold_s=None, fill_value=0.0).astype(np.float64) - floor, 0.0)
            sc = score(nl, n, sdev[n], t, p, p > 0, 120.0)
            print(f"selftest {n}: truth {sc['truth_kwh']:.2f} kWh, overlap capture {sc['ovl']['capture']:.3f} impurity {sc['ovl']['impurity']:.3f}, "
                  f"LI-session capture {sc['li']['capture']:.3f} impurity {sc['li']['impurity']:.3f}")
            assert sc["ovl"]["capture"] > 0.97 and sc["ovl"]["impurity"] < 0.03, "overlap scoring is off"
        return
    train_folders = [str(ROOT / f) for f in (cfg["TRAIN"] or TRAIN_DEFAULT[site.split("-")[0]]).split(",")]
    modes, mains_kinds, refs = cfg["MODES"].split(","), cfg["MAINS"].split(","), int(cfg["REFS"])
    bridge, margin = float(cfg["BRIDGE"]), float(cfg["MARGIN"])
    T0 = time.time()
    nl = nilm()
    print(f"ha-nilm imported ({time.time() - T0:.0f} s); loading LI data ...", flush=True)
    sph, sdev = load(score_folder, site)
    TR = []
    for f in train_folders:
        tph, tdev = load(f, site)
        tgrid, ty = grid_of(nl, tph)
        TR.append((tgrid, ty, tdev, gaps_of(tph)))
    # the card's devices; the EXTRA= meters (fan, blinds, EcoFlow: under 0.1 kWh or not plugs of an appliance) stay out unless LOADS= names them
    names = [n for n in EB._tables(site)["devices"] if n in sdev and any(n in tr[2] for tr in TR) and n not in B.EXTRA_SUBS.get(site, {})]
    names = [n for n in names if EB._truth(sdev[n])[0] >= 50.0] if not cfg["LOADS"] else cfg["LOADS"].split(",")
    sgrid, sy = grid_of(nl, sph)
    t_load = time.time() - T0
    days_scored = (sgrid[-1] - sgrid[0]) / 86400.0
    print(f"{site}: scoring {days_scored:.1f} days ({time.strftime('%m-%d %H:%M', time.localtime(sgrid[0]))} - "
          f"{time.strftime('%m-%d %H:%M', time.localtime(sgrid[-1]))}), loads {names}; reference days from {[Path(f).name for f in train_folders]}", flush=True)
    # which phase each load sits on: the one whose 5-min mean follows its plug best, on the REFERENCE days only
    phase_of = {}
    sgrid_phases = [p for p in sy if p != "total"]
    blk = 37
    for n in names:
        parts = {p: [] for p in sgrid_phases}
        pgs = []
        for tgrid, ty, tdev, _ in TR:
            if n in tdev:
                m = len(tgrid) // blk * blk
                pgs.append(nl.ptd.zoh_resample_to_grid(tdev[n], tgrid, max_hold_s=None, fill_value=0.0)[:m].reshape(-1, blk).mean(1))
                for p in sgrid_phases:
                    parts[p].append(ty[p][:m].reshape(-1, blk).mean(1))
        pg = np.concatenate(pgs)
        c = {p: float(np.nan_to_num(np.corrcoef(np.concatenate(parts[p]), pg)[0, 1], nan=-1.0)) for p in sgrid_phases}
        phase_of[n] = max(c, key=c.get)
    QS = {}
    tq = time.time()
    for k in sorted({"total"} | ({phase_of[n] for n in names} if "phase" in mains_kinds else set())):
        QS[k] = queries(nl, sgrid, sy[k], f"score|{score_folder}|{k}")
    t_query_s = time.time() - tq
    n_steps = len(QS["total"][1])
    t_eval = QS["total"][1]
    half = len(t_eval) // 2
    mains_kwh = float(QS["total"][2].sum() * DT / 3600.0 / 1000.0)                     # the house's energy over the scored steps
    budget_kwh = float(np.maximum(0.0, QS["total"][2] - QS["total"][3]).sum() * DT / 3600.0 / 1000.0)   # what is above the window minimum
    if cfg["WORKFLOW"] == "1":
        return workflow(nl, cfg, site, names, TR, sdev, sgrid, QS, bridge, T0, t_load, days_scored, n_steps, mains_kwh, budget_kwh)
    results, schemes = {}, []
    for mk in mains_kinds:
        for mode in modes:
            for rank in range(refs):
                schemes.append((mk, mode, rank))
    t_train = t_infer = 0.0
    for mk, mode, rank in schemes:
        tag = f"{mk}-{mode}-r{rank}"
        edir = OUT / "emb" / site / tag
        series = {n: ("total" if mk == "total" else phase_of[n]) for n in names}
        meta = {}
        for n in names:
            tt = time.time()
            try:
                meta[n] = train_one(nl, n, mode, TR, series[n], references(nl, TR, n, mode, rank, margin), edir)
            except Exception as e:                                        # a load that cannot train is reported, not hidden
                meta[n] = {"error": f"{type(e).__name__}: {e}"[:300]}
            t_train += time.time() - tt
            print(f"  [{tag}] trained {n}: {meta[n].get('note') or meta[n].get('error')}", flush=True)
        ok = [n for n in names if "error" not in meta[n]]
        res = {n: {"train": meta[n]} for n in names}
        ti = time.time()
        for key in sorted({series[n] for n in ok}):
            grp = [n for n in ok if series[n] == key]
            Q, t, m, bl = QS[key]
            safes = [meta[n]["safe"] for n in grp]
            dis, P, O = heads(nl, edir, safes, Q)
            if cfg["PARITY"] == "1" and not results and key == "total":
                res["_parity"] = parity(nl, edir, safes, sgrid, sy[key], Q)
                print("  parity (live disaggregate_next vs batch):", res["_parity"], flush=True)
            thr = [meta[n]["onoff_threshold"] for n in grp]
            avail = np.maximum(0.0, m - bl)
            for rule in ("live", "preview"):
                p = deploy(P, O, thr, avail, rule)
                for g, n in enumerate(grp):
                    on = O[g] >= thr[g]
                    sc = score(nl, n, sdev[n], t, p[g].astype(np.float64), on, bridge)
                    if rule == cfg["BUDGET"]:
                        res[n]["score"] = sc
                        res[n]["mains"] = key
                        h = {}
                        for nm, sl in (("h1", slice(0, half)), ("h2", slice(half, None))):
                            h[nm] = score(nl, n, sdev[n], t[sl], p[g][sl].astype(np.float64), on[sl], bridge)
                        res[n]["halves"] = {k: {"li": v["li"], "ovl": v["ovl"]} for k, v in h.items()}
                    else:
                        res[n]["other_budget"] = {"li": sc["li"], "ovl": sc["ovl"], "est_kwh": sc["est_kwh"]}
        t_infer += time.time() - ti
        results[tag] = res
    wall = time.time() - T0
    rep = {"site": site, "utc": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), "config": cfg, "score_folder": score_folder,
           "train_folders": train_folders, "scored_days": days_scored, "scored_steps": n_steps, "mains_kwh": mains_kwh, "above_window_min_kwh": budget_kwh, "phase_of": phase_of,
           "training": TRAINING, "results": results,
           "cost": {"wall_s": wall, "load_li_data_s": t_load, "query_embeddings_s": t_query_s, "train_s": t_train, "inference_s": t_infer,
                    "peak_rss_gb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9,
                    "query_s_per_day": t_query_s / days_scored / len(QS)}}
    report(rep, names)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{site}-{cfg['LABEL'] + '-' if cfg['LABEL'] else ''}{rep['utc']}.json"
    path.write_text(json.dumps(rep, indent=1, default=float), encoding="utf-8")
    print(f"written {path}")


def workflow(nl, cfg, site, names, TR, sdev, sgrid, QS, bridge, T0, t_load, days_scored, n_steps, mains_kwh, budget_kwh):
    """The practitioner workflow: what ha-nilm's README asks of a person, with nothing from the scored span in any choice.
    Per load: candidate reference ranges (CANDS contiguous 7-day weeks of the reference folder, clean of data gaps, plus the
    load's best single day), each trained with the plug as sensor supervision; each candidate is scored on a VALIDATION window
    (the last VAL clean days of the reference folder, after every candidate's training days, so held out from all of them) and
    the candidate with the best validation energy-F1 (overlap: 2 right / (truth + attributed)) is kept - standing for the person
    who looks at the preview and retrains (the preview has no threshold or power control: the on/off threshold is chosen by best F1
    inside training, so range and labels are the only levers). The kept model is then scored unchanged on the scored span.
    "Kept models" view: a load whose validation result a person would reject (energy-F1 < F1_MIN or overlap impurity > IMP_MAX)
    is dropped. Every candidate is ALSO scored on the scored span, for the spread only - never for a choice."""
    tgrid, ty, tdev, gaps = TR[0]
    days = clean_days(tgrid, gaps)
    nval, ncand = int(cfg["VAL"]), int(cfg["CANDS"])
    f1_min, imp_max = float(cfg["F1_MIN"]), float(cfg["IMP_MAX"])
    vdays, pool = days[-nval:], days[:-nval]
    assert all(abs(vdays[i][1] - vdays[i + 1][0]) < 7200 for i in range(len(vdays) - 1)), "validation days not contiguous"
    starts = [i for i in range(len(pool) - 6) if all(abs(pool[j][1] - pool[j + 1][0]) < 7200 for j in range(i, i + 6))]
    starts = [starts[k] for k in sorted({int(round(x)) for x in np.linspace(0, len(starts) - 1, min(ncand, len(starts)))})]
    fmt = lambda t: dt.datetime.fromtimestamp(t, TZ).strftime("%m-%d")  # noqa: E731
    iv0, iv1 = (int(x) for x in np.searchsorted(tgrid, [vdays[0][0], vdays[-1][1]]))
    vgrid, vy = tgrid[iv0:iv1], ty["total"][iv0:iv1]
    Qv = queries(nl, vgrid, vy, f"val|{vgrid[0]}")
    print(f"workflow: weeks starting {[fmt(pool[i][0]) for i in starts]}, validation {fmt(vdays[0][0])}..{fmt(vdays[-1][0])} "
          f"({len(vgrid) * DT / 86400:.1f} d), pool {fmt(pool[0][0])}..{fmt(pool[-1][0])}", flush=True)
    cands = [(f"w{k}", [i for i in (starts[k],)][0]) for k in range(len(starts))] + [("d", None)]
    t_train = t_infer = 0.0
    out = {}          # tag -> {load: {"train", "val", "score", "halves"}}
    for tag, st in cands:
        edir = OUT / "emb" / site / f"wf-{tag}"
        res = {}
        meta = {}
        for n in names:
            tt = time.time()
            try:
                if st is not None:
                    a, b = (int(x) for x in np.searchsorted(tgrid, [pool[st][0], pool[st + 6][1]]))
                    rng = (0, a, b, None, f"week {fmt(pool[st][0])}..{fmt(pool[st + 6][0])}")
                    pg = nl.ptd.zoh_resample_to_grid(tdev[n], tgrid[a:b], max_hold_s=None, fill_value=0.0)
                    if float(np.maximum(pg - EB._truth(tdev[n])[1], 0).sum()) <= 0:
                        raise RuntimeError(f"{n} never runs in that week")
                else:
                    rng = references(nl, [(tgrid, ty, tdev, gaps + [(vgrid[0], vgrid[-1] + DT)])], n, "day", 0, 0.0)
                meta[n] = train_one(nl, n, "day", TR, "total", rng, edir)
            except Exception as e:
                meta[n] = {"error": f"{type(e).__name__}: {e}"[:300]}
            t_train += time.time() - tt
            print(f"  [{tag}] {n}: {meta[n].get('note') or meta[n].get('error')}  ({time.time() - tt:.0f} s)", flush=True)
        ok = [n for n in names if "error" not in meta[n]]
        res = {n: {"train": meta[n]} for n in names}
        ti = time.time()
        safes = [meta[n]["safe"] for n in ok]
        thr = [meta[n]["onoff_threshold"] for n in ok]
        if ok:
            for which, Q, tdata in (("val", Qv, None), ("score", QS["total"], None)):
                q, t, m, bl = Q
                dis, P, O = heads(nl, edir, safes, q)
                p = deploy(P, O, thr, np.maximum(0.0, m - bl), "live")
                for g, n in enumerate(ok):
                    on = O[g] >= thr[g]
                    rows = tdev[n] if which == "val" else sdev[n]
                    sc = score(nl, n, rows, t, p[g].astype(np.float64), on, bridge)
                    if which == "val":
                        o = sc["ovl"]
                        sc["f1e"] = 2 * o["right_kwh"] / (o["truth_kwh"] + o["attributed_kwh"]) if o["truth_kwh"] + o["attributed_kwh"] else 0.0
                        res[n]["val"] = sc
                    else:
                        half = len(t) // 2
                        res[n]["score"] = sc
                        res[n]["halves"] = {nm: {k: v[k] for k in ("li", "ovl")} for nm, v in
                                            ((nm, score(nl, n, rows, t[sl], p[g][sl].astype(np.float64), on[sl], bridge))
                                             for nm, sl in (("h1", slice(0, half)), ("h2", slice(half, None))))}
        t_infer += time.time() - ti
        out[tag] = res
    chosen = {}
    for n in names:
        have = [(out[t][n]["val"]["f1e"], -(out[t][n]["val"]["ovl"]["impurity"] or 1.0), t) for t in out if "val" in out[t][n]]
        if have:
            best = max(have)[2]
            v = out[best][n]["val"]
            chosen[n] = {"cand": best, "val_f1e": v["f1e"], "val_impurity": v["ovl"]["impurity"], "val_capture": v["ovl"]["capture"],
                         "kept": v["f1e"] >= f1_min and (v["ovl"]["impurity"] or 1.0) <= imp_max}
    rep = {"site": site, "workflow": True, "utc": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), "config": cfg, "scored_days": days_scored,
           "scored_steps": n_steps, "mains_kwh": mains_kwh, "above_window_min_kwh": budget_kwh, "training": TRAINING, "names": names,
           "validation_days": [fmt(vdays[0][0]), fmt(vdays[-1][0])], "week_starts": [fmt(pool[i][0]) for i in starts],
           "candidates": out, "chosen": chosen, "rule": {"f1e_min": f1_min, "impurity_max": imp_max},
           "cost": {"wall_s": time.time() - T0, "load_li_data_s": t_load, "train_s": t_train, "inference_s": t_infer,
                    "peak_rss_gb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9}}
    report_workflow(rep)
    path = OUT / f"{site}-{cfg['LABEL'] + '-' if cfg['LABEL'] else ''}wf-{rep['utc']}.json"
    path.write_text(json.dumps(rep, indent=1, default=float), encoding="utf-8")
    print(f"written {path}")


def li_card_rows(site):
    if not LI_CARD.exists():
        return {}
    run = json.loads(LI_CARD.read_text(encoding="utf-8"))["runs"]
    return {m: {n: {"truth_kwh": g["truth_kwh"], "attributed_kwh": g["attributed_kwh"], "right_kwh": g["correct_kwh"],
                    "wrong_kwh": g["attributed_kwh"] - g["correct_kwh"]} for n, g in run[f"{site}|{m}|6"]["tables"]["devices"]["loads"].items()}
            for m in ("hidden", "fed") if f"{site}|{m}|6" in run}


def report_workflow(rep):
    names, cands, chosen = rep["names"], rep["candidates"], rep["chosen"]
    kept = [n for n in names if chosen.get(n, {}).get("kept")]
    print(f"\n=== practitioner workflow, {rep['site']}: scored {rep['scored_steps']} steps, validation days {rep['validation_days'][0]}..{rep['validation_days'][1]} "
          f"(held out of every candidate), week candidates starting {rep['week_starts']} + best single day (d)")
    print(f"selection: best validation energy-F1 = 2*right/(truth+attributed) of the overlap score; kept-models rule: validation F1 >= {rep['rule']['f1e_min']} "
          f"and overlap impurity <= {100 * rep['rule']['impurity_max']:.0f} %")
    cols = f"{'right':>7s} {'wrong':>8s} {'capt%':>5s} {'impur%':>6s}"
    print(f"\n  CHOSEN model per load, scored on the span (kWh)\n  {'load':20s} {'cand':>4s} {'valF1':>5s} {'valImp%':>7s} {'kept':>4s} {'truth':>7s} {'est':>8s} | OVERLAP {cols} | LI-SESSION {cols}")
    rows_o, rows_l, est = {}, {}, {}
    for n in names:
        c = chosen.get(n)
        if not c:
            print(f"  {n:20s} no candidate trained")
            continue
        sc = cands[c["cand"]][n]["score"]
        o, l = sc["ovl"], sc["li"]
        rows_o[n], rows_l[n], est[n] = o, l, sc["est_kwh"]
        print(f"  {n:20s} {c['cand']:>4s} {c['val_f1e']:5.2f} {pct(c['val_impurity']):>7s} {'yes' if c['kept'] else 'NO':>4s} {o['truth_kwh']:7.2f} {sc['est_kwh']:8.2f} | "
              f"{'':7s} {o['right_kwh']:7.2f} {o['wrong_kwh']:8.2f} {pct(o['capture'])} {pct(o['impurity'])} | {'':10s} {l['right_kwh']:7.2f} {l['wrong_kwh']:8.2f} {pct(l['capture'])} {pct(l['impurity'])}")
    card = li_card_rows(rep["site"])
    print(f"\n  SUMMARY pooled (kWh: truth / attributed / right / wrong; capture % / impurity %)")
    print(f"  {'':34s} {'truth':>7s} {'attrib':>8s} {'right':>7s} {'wrong':>8s} {'capt%':>5s} {'impur%':>6s}   LI-session: right / wrong, capture / impurity")
    def row(label, ro, rl):
        o, l = pooled(ro), pooled(rl)
        extra = f"   {l['right_kwh']:.2f} / {l['wrong_kwh']:.2f}, {pct(l['capture'])} / {pct(l['impurity'])}" if rl else ""
        print(f"  {label:34s} {o['truth_kwh']:7.2f} {o['attributed_kwh']:8.2f} {o['right_kwh']:7.2f} {o['wrong_kwh']:8.2f} {pct(o['capture'])} {pct(o['impurity'])}{extra}")
    row(f"ha-nilm CHOSEN, all {len(rows_o)} loads", list(rows_o.values()), list(rows_l.values()))
    if kept:
        row(f"ha-nilm CHOSEN, KEPT {len(kept)} loads", [rows_o[n] for n in kept], [rows_l[n] for n in kept])
        print(f"      kept: {kept}; dropped: {[n for n in rows_o if n not in kept]}")
    for m, d in card.items():
        row(f"LI card {m}, same {len(rows_o)} loads", [d[n] for n in rows_o if n in d], [])
        if kept:
            row(f"LI card {m}, same KEPT {len(kept)}", [d[n] for n in kept if n in d], [])
    for tag in cands:
        got = [n for n in names if "score" in cands[tag][n]]
        if got:
            row(f"candidate {tag}, {len(got)} loads (spread only)", [cands[tag][n]["score"]["ovl"] for n in got], [cands[tag][n]["score"]["li"] for n in got])
    caps = [pooled([cands[t][n]["score"]["ovl"] for n in names if "score" in cands[t][n]]) for t in cands]
    print(f"  spread across candidates, pooled overlap: capture {pct(min(c['capture'] for c in caps))}-{pct(max(c['capture'] for c in caps))} %, "
          f"impurity {pct(min(c['impurity'] for c in caps))}-{pct(max(c['impurity'] for c in caps))} %")
    print(f"\n  per load, spread of the scored overlap capture / impurity % over candidates (min-max)")
    for n in names:
        v = [(cands[t][n]["score"]["ovl"]["capture"], cands[t][n]["score"]["ovl"]["impurity"] or 0) for t in cands if "score" in cands[t][n]]
        if v:
            print(f"  {n:20s} capture {pct(min(x[0] for x in v))}-{pct(max(x[0] for x in v))}   impurity {pct(min(x[1] for x in v))}-{pct(max(x[1] for x in v))}   ({len(v)} candidates)")
    prev = sorted(OUT.glob(f"{rep['site']}-final*-*.json"), key=lambda f: f.stat().st_mtime)
    if prev:
        old = json.loads(prev[-1].read_text(encoding="utf-8"))
        print(f"\n  earlier single-reference runs ({prev[-1].name}), pooled overlap over its 7 loads")
        for tag, res in old["results"].items():
            got = [res[n]["score"]["ovl"] for n in res if not n.startswith("_") and res[n].get("score")]
            o = pooled(got)
            print(f"  {'ha-nilm ' + tag:34s} {o['truth_kwh']:7.2f} {o['attributed_kwh']:8.2f} {o['right_kwh']:7.2f} {o['wrong_kwh']:8.2f} {pct(o['capture'])} {pct(o['impurity'])}")
    c = rep["cost"]
    print(f"\ncost: wall {c['wall_s']:.0f} s (LI data load {c['load_li_data_s']:.0f} s, training {c['train_s']:.0f} s for {sum(1 for t in cands for n in names if 'train' in cands[t][n] and 'error' not in cands[t][n]['train'])} embeddings, "
          f"validation + scored inference {c['inference_s']:.0f} s), peak RSS {c['peak_rss_gb']:.1f} GB")


def pooled(rows):
    """LI's site figure: capture weighted by energy, impurity pooled."""
    t = sum(r["truth_kwh"] for r in rows)
    a = sum(r["attributed_kwh"] for r in rows)
    ok = sum(r["right_kwh"] for r in rows)
    return {"truth_kwh": t, "attributed_kwh": a, "right_kwh": ok, "wrong_kwh": a - ok, "capture": ok / t if t else None, "impurity": (a - ok) / a if a else None}


def pct(v):
    return "   -" if v is None else f"{100 * v:5.1f}"


def line(name, r, f1=None, extra=""):
    return (f"  {name:20s} {r['truth_kwh']:8.2f} {r['attributed_kwh']:8.2f} {r['right_kwh']:8.2f} {r['wrong_kwh']:8.2f} "
            f"{pct(r['capture'])} {pct(r['impurity'])}" + ("" if f1 is None else f"  F1 {f1:.2f}") + extra)


def report(rep, names):
    card = json.loads(LI_CARD.read_text(encoding="utf-8")) if LI_CARD.exists() else None
    li = {}
    if card:
        for mode in ("hidden", "fed"):
            run = card["runs"].get(f"{rep['site']}|{mode}|6")
            if run:
                li[mode] = {n: {"truth_kwh": g["truth_kwh"], "attributed_kwh": g["attributed_kwh"], "right_kwh": g["correct_kwh"],
                                "wrong_kwh": g["attributed_kwh"] - g["correct_kwh"], "capture": g["capture"], "impurity": g["impurity"]}
                            for n, g in run["tables"]["devices"]["loads"].items()}
    cols = f"{'right':>7s} {'wrong':>8s} {'capt%':>5s} {'impur%':>6s}"
    summary = []
    for tag, res in rep["results"].items():
        print(f"\n=== ha-nilm {tag}  (mains = {'each load on its best phase' if tag.startswith('phase') else 'sum of the 3 phases'}, "
              f"BUDGET={rep['config']['BUDGET']}, {rep['scored_steps']} steps of 8 s; kWh)")
        print(f"  {'load':20s} {'truth':>7s} {'est':>8s} | OVERLAP {cols} | LI-SESSION {cols} | F1  reference")
        got = [(n, res[n]["score"]) for n in names if res[n].get("score")]
        for n in names:
            sc = res[n].get("score")
            if not sc:
                print(f"  {n:20s} {res[n]['train'].get('error', 'not run')}")
                continue
            o, l = sc["ovl"], sc["li"]
            print(f"  {n:20s} {sc['truth_kwh']:7.2f} {sc['est_kwh']:8.2f} | {'':7s} {o['right_kwh']:7.2f} {o['wrong_kwh']:8.2f} {pct(o['capture'])} {pct(o['impurity'])} "
                  f"| {'':10s} {l['right_kwh']:7.2f} {l['wrong_kwh']:8.2f} {pct(l['capture'])} {pct(l['impurity'])} | {sc['f1']:.2f}  {res[n]['train']['note']}")
        if got:
            po, pl = pooled([g["ovl"] for _, g in got]), pooled([g["li"] for _, g in got])
            print(f"  {'POOLED':20s} {po['truth_kwh']:7.2f} {sum(g['est_kwh'] for _, g in got):8.2f} | {'':7s} {po['right_kwh']:7.2f} {po['wrong_kwh']:8.2f} {pct(po['capture'])} {pct(po['impurity'])} "
                  f"| {'':10s} {pl['right_kwh']:7.2f} {pl['wrong_kwh']:8.2f} {pct(pl['capture'])} {pct(pl['impurity'])} |")
            hv = {}
            for h in ("h1", "h2"):
                hv[h] = [pooled([res[n]["halves"][h][d] for n, _ in got]) for d in ("ovl", "li")]
            print("  halves of the span (first / second 5.3 d), pooled capture / impurity %: "
                  f"overlap {pct(hv['h1'][0]['capture'])}/{pct(hv['h1'][0]['impurity'])} | {pct(hv['h2'][0]['capture'])}/{pct(hv['h2'][0]['impurity'])}"
                  f"   LI-session {pct(hv['h1'][1]['capture'])}/{pct(hv['h1'][1]['impurity'])} | {pct(hv['h2'][1]['capture'])}/{pct(hv['h2'][1]['impurity'])}")
            for k in ("li_bridge0", "li_bridge600"):
                if k in got[0][1]:
                    pb = pooled([g[k] for _, g in got])
                    print(f"  LI-session with sessions bridged at {k[9:]} s instead of {rep['config']['BRIDGE']}: right {pb['right_kwh']:.2f} wrong {pb['wrong_kwh']:.2f} kWh, "
                          f"capture {pct(pb['capture'])} impurity {pct(pb['impurity'])}")
            ob = [res[n]["other_budget"]["ovl"] for n, _ in got if "other_budget" in res[n]]
            if ob:
                p2 = pooled(ob)
                print(f"  other budget rule ({'preview' if rep['config']['BUDGET'] == 'live' else 'live'}): overlap right {p2['right_kwh']:.2f} wrong {p2['wrong_kwh']:.2f} kWh, "
                      f"capture {pct(p2['capture'])} impurity {pct(p2['impurity'])}")
            summary.append((tag, po, pl))
    for mode, d in li.items():
        print(f"\n=== LI card, {mode} ({LI_CARD.name}: defaults, same 3-13 Oct data, its whole span; kWh)")
        print(f"  {'load':20s} {'truth':>7s} {'attrib':>8s} | {cols}")
        for n in names:
            if n in d:
                r = d[n]
                print(f"  {n:20s} {r['truth_kwh']:7.2f} {r['attributed_kwh']:8.2f} | {r['right_kwh']:7.2f} {r['wrong_kwh']:8.2f} {pct(r['capture'])} {pct(r['impurity'])}")
        r = pooled([d[n] for n in names if n in d])
        print(f"  {'POOLED':20s} {r['truth_kwh']:7.2f} {r['attributed_kwh']:8.2f} | {r['right_kwh']:7.2f} {r['wrong_kwh']:8.2f} {pct(r['capture'])} {pct(r['impurity'])}")
        summary.append((f"LI card {mode}", r, r))
    if "mains_kwh" in rep:
        print(f"\nmains over the scored steps: {rep['mains_kwh']:.0f} kWh in all, {rep['above_window_min_kwh']:.0f} kWh of it above the 80-min window minimum "
              f"(the most all models together can be handed under BUDGET=preview)")
    print(f"=== SUMMARY, pooled over {len(names)} loads (kWh: truth / attributed / right / wrong; capture % / impurity %)")
    print(f"  {'':22s} {'truth':>7s} {'attrib':>8s} {'right':>7s} {'wrong':>8s} {'capt%':>5s} {'impur%':>6s}   (LI-session figures follow for ha-nilm)")
    for tag, o, l in summary:
        print(f"  {tag:22s} {o['truth_kwh']:7.2f} {o['attributed_kwh']:8.2f} {o['right_kwh']:7.2f} {o['wrong_kwh']:8.2f} {pct(o['capture'])} {pct(o['impurity'])}"
              + ("" if tag.startswith("LI card") else f"   LI-session: right {l['right_kwh']:.2f} wrong {l['wrong_kwh']:.2f} capture {pct(l['capture'])} impurity {pct(l['impurity'])}"))
    c = rep["cost"]
    print(f"\ncost: wall {c['wall_s']:.0f} s for {rep['scored_days']:.1f} days scored + reference days; LI data load {c['load_li_data_s']:.0f} s (LI parse cache), "
          f"query embeddings {c['query_embeddings_s']:.0f} s ({c['query_s_per_day']:.1f} s per scored day per mains series), "
          f"training {c['train_s']:.0f} s, heads+scoring {c['inference_s']:.0f} s, peak RSS {c['peak_rss_gb']:.1f} GB")


if __name__ == "__main__":
    main(sys.argv[1:])
