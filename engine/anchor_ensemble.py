#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
anchor_ensemble.py  —  Ensemble real-z baseline for the gate anchors.

Runs the pinch stage N times (reseeding the chaos null each run) and aggregates
the per-anchor, per-register chaos z into mean +/- std. This turns the reseed
jitter observed between single runs (Gaia 15<->16, protein 25<->16 trading peaks)
from an alarming anomaly into a measured quantity.

WHY: build_pinch_table's compute_dual_z_scores uses unseeded np.random.uniform,
so each pinch run reseeds the chaos comparison. Scalarize/unify are deterministic,
so we skip them (--from pinch). Figures are wasted here, so we skip them.

Per iteration it runs:
    python run_pipeline.py --from pinch --skip-figures --no-advisory
then reads z_scores_master.json and records the chaos z at each anchor register.

Output: mean, std, min, max of chaos z per anchor register, plus how often each
anchor's register was the PEAK (the co-dominance measure). Writes a JSON artifact.

SAFETY: this rewrites z_scores_master.json each run (that is how the pinch works).
It backs up the current ledger to z_scores_master.json.ensemble_bak before starting
and restores it at the end, so the canonical ledger is preserved.

Usage:  python anchor_ensemble.py --runs 20
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import numpy as np

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(ENGINE_DIR)
CONFIGS_DIR = os.path.join(ROOT, "configs")
LEDGER = os.path.join(ROOT, "lakes", "unified", "z_scores_master.json")
RUN_PIPELINE = os.path.join(ENGINE_DIR, "run_pipeline.py")

# Anchors and the register each is tested at. Add/adjust per Mondy's ruling.
ANCHORS = {
    "stellar_kinematic": 16,
    "biology_backbone":  25,
    "galactic":          21,
    "nuclear_binding":   21,   # optional 4th; remove if Mondy picks galactic only
}


def load_registers():
    with open(os.path.join(CONFIGS_DIR, "harmonic_targets.json"), "r", encoding="utf-8") as f:
        return json.load(f)["registers"]


def read_ledger_z(registers):
    """Return {domain: {'peak_N':N, 'z_at_anchor':z, 'full':[...]}} for the anchors."""
    with open(LEDGER, "r", encoding="utf-8") as f:
        led = json.load(f)
    out = {}
    for dom, anchorN in ANCHORS.items():
        if dom not in led:
            out[dom] = None
            continue
        cz = led[dom]["chaos_z"]
        peak_i = max(range(len(cz)), key=lambda i: cz[i])
        peak_N = registers[peak_i]
        idx = registers.index(anchorN)
        z_at = cz[idx] if idx < len(cz) else None
        out[dom] = {"peak_N": peak_N, "z_at_anchor": z_at}
    return out


def run_pinch_once():
    """Run only the pinch stage, no figures, no advisory. Returns True on success."""
    cmd = [sys.executable, RUN_PIPELINE, "--from", "pinch", "--skip-figures", "--no-advisory"]
    r = subprocess.run(cmd, cwd=ENGINE_DIR, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"    [!] pinch run failed:\n{(r.stderr or r.stdout)[:500]}")
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=20, help="number of pinch iterations")
    args = ap.parse_args()

    registers = load_registers()

    # backup the canonical ledger
    backup = LEDGER + ".ensemble_bak"
    if os.path.exists(LEDGER):
        shutil.copy2(LEDGER, backup)
        print(f"Backed up ledger -> {backup}")

    # collectors
    z_at_anchor = {d: [] for d in ANCHORS}
    peak_hits   = {d: {} for d in ANCHORS}   # register -> count of times it was peak

    print(f"\nRunning {args.runs} pinch iterations (reseed each). "
          f"Skipping scalarize/unify/figures.\n")

    completed = 0
    for i in range(args.runs):
        print(f"[run {i+1}/{args.runs}] pinch ...", end=" ", flush=True)
        if not run_pinch_once():
            print("FAILED — stopping ensemble.")
            break
        snap = read_ledger_z(registers)
        line = []
        for dom, info in snap.items():
            if info is None:
                line.append(f"{dom}:MISSING")
                continue
            z_at_anchor[dom].append(info["z_at_anchor"])
            pk = info["peak_N"]
            peak_hits[dom][pk] = peak_hits[dom].get(pk, 0) + 1
            line.append(f"{dom[:12]}@{ANCHORS[dom]}={info['z_at_anchor']:.1f}(pk{pk})")
        completed += 1
        print("  ".join(line))

    # restore canonical ledger
    if os.path.exists(backup):
        shutil.copy2(backup, LEDGER)
        print(f"\nRestored canonical ledger from {backup}")

    # aggregate
    print("\n" + "=" * 72)
    print(f"   ANCHOR ENSEMBLE  ({completed} runs)")
    print("=" * 72)
    results = {}
    for dom, anchorN in ANCHORS.items():
        zs = [z for z in z_at_anchor[dom] if z is not None]
        if not zs:
            print(f"  {dom:20} @ {anchorN}/pi : no data"); continue
        arr = np.array(zs)
        hits = peak_hits[dom]
        total = sum(hits.values())
        peak_frac = {f"{k}/pi": f"{v}/{total}" for k, v in sorted(hits.items(), key=lambda t:-t[1])}
        # fraction of runs where the ANCHOR register was also the peak
        anchor_peak_frac = hits.get(anchorN, 0) / total if total else 0.0
        print(f"\n  {dom} @ {anchorN}/pi")
        print(f"    real chaos-z: mean={arr.mean():.2f}  std={arr.std(ddof=1):.2f}  "
              f"min={arr.min():.2f}  max={arr.max():.2f}")
        # find the register that is MOST OFTEN the peak, and how dominant it is
        top_peak_N = max(hits, key=hits.get) if hits else None
        top_peak_frac = hits.get(top_peak_N, 0) / total if total else 0.0
        print(f"    anchor register {anchorN}/pi was the peak in {anchor_peak_frac*100:.0f}% of runs")
        print(f"    most-frequent peak: {top_peak_N}/pi ({top_peak_frac*100:.0f}% of runs)")
        print(f"    peak distribution: {peak_frac}")
        # ALIGNMENT is the primary question, THEN strength
        aligned = (top_peak_N == anchorN)
        strong  = (arr.mean() - arr.std(ddof=1) >= 5.0)
        if aligned and strong:
            print(f"    -> CLEAN: anchor register is the stable peak AND stably STRONG.")
        elif (not aligned) and strong:
            print(f"    -> *** MISALIGNED: stably STRONG at {anchorN}/pi, but the PEAK is")
            print(f"       {top_peak_N}/pi in {top_peak_frac*100:.0f}% of runs. Testing the gate at")
            print(f"       {anchorN}/pi would test a non-peak lock. NEEDS MONDY RULING. ***")
        elif aligned and not strong:
            print(f"    -> aligned peak but not stably STRONG (mean-1sd<5).")
        else:
            print(f"    -> WEAK and misaligned. Not a valid anchor at {anchorN}/pi.")
        results[dom] = {
            "anchor_N": anchorN, "mean": float(arr.mean()), "std": float(arr.std(ddof=1)),
            "min": float(arr.min()), "max": float(arr.max()),
            "anchor_peak_fraction": anchor_peak_frac,
            "most_frequent_peak_N": top_peak_N,
            "most_frequent_peak_fraction": top_peak_frac,
            "aligned": bool(top_peak_N == anchorN),
            "peak_distribution": hits,
        }

    out = os.path.join(ENGINE_DIR, "anchor_ensemble_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"runs": completed, "anchors": results}, f, indent=2)
    print(f"\n  Written: {out}")
    print("\n  Take mean +/- std per anchor to Mondy as the gate's real-z baseline.")


if __name__ == "__main__":
    main()