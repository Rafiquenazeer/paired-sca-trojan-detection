#!/usr/bin/env python3
"""
generate_dataset_a.py
======================
Generate "Dataset A" - a paired control/reference testset for an existing
Dataset B (a MERS/MERS-h/MERS-s/MERO/Random testset already produced by
run_mers.py and saved under mers_progress/<name>/).

Dataset A reproduces B's overall activity profile (BackgroundSwitch, input
Hamming distance/weight, output Hamming distance/weight) per vector pair,
while minimising switching of the "suspicious" rare-node logic (the
selected rare nodes, optionally expanded into a fan-in/fan-out cone).

Example
-------
First run the normal pipeline to produce B (e.g. MERS-s):

  python run_mers.py --bench benchmarks/s15850.bench --name s15850_AB \\
      --N 100 --vectors 1500 --trojans 100 --triggers 4 8 \\
      --mutation-mode fast --mutation-rounds 3 --skip-mero

Then generate A paired with that MERS-s testset:

  python generate_dataset_a.py --bench benchmarks/s15850.bench --name s15850_AB \\
      --N 100 --mutation-mode fast --mutation-rounds 3 --C 5 --B-method mers_s \\
      --num-cone-seeds 8 --cone-fanin-depth 1 --cone-fanout-depth 2

Output
------
  mers_testvectors/<name>/   - Dataset A vectors (.txt/.hex), alongside B
  mers_results/<name>_dataset_A_vs_B_<tag>.xlsx - comparison report
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def parse_args():
    p = argparse.ArgumentParser(
        description="Generate Dataset A (anti-rare-cone, statistically "
                     "matched) paired with an existing Dataset B testset."
    )
    p.add_argument("--bench", required=True, help="Path to .bench file")
    p.add_argument("--name", required=True, help="Run name / checkpoint folder name")

    # --- Keys needed to locate Dataset B's checkpoint (mirrors run_mers.py) ---
    p.add_argument("--N", type=int, default=1000,
                    help="N used for MERS/MERO when B was generated [default: 1000]")
    p.add_argument("--vectors", type=int, default=10_000,
                    help="Random-vector count used when B was generated "
                         "(only needed if --B-method random) [default: 10000]")
    p.add_argument("--mutation-mode", choices=["paper", "fast"], default="paper",
                    help="Mutation mode used when B was generated [default: paper]")
    p.add_argument("--mutation-rounds", type=int, default=12,
                    help="Mutation rounds used when B was generated (fast mode) [default: 12]")
    p.add_argument("--mero-rounds", type=int, default=8,
                    help="MERO mutation rounds used when B was generated [default: 8]")
    p.add_argument("--C", type=int, default=5, help="MERS-s C value used when B was generated [default: 5]")
    p.add_argument("--B-method", choices=["mers_s", "mers_h", "mers", "mero", "random"],
                    default="mers_s", help="Which testset to use as Dataset B [default: mers_s]")
    p.add_argument("--B-limit", type=int, default=None,
                    help="Use only the first N vectors of B (default: all)")

    # --- Suspicious cone definition ---
    p.add_argument("--cone-fanin-depth", type=int, default=0,
                    help="Fan-in levels to include around each seed rare node "
                         "(0 = seed nodes only, the literal 'selected rare nodes'; "
                         "use -1 for unlimited) [default: 0]")
    p.add_argument("--cone-fanout-depth", type=int, default=0,
                    help="Fan-out levels to include around each seed rare node "
                         "(0 = seed nodes only; -1 for unlimited) [default: 0]")
    p.add_argument("--cone-seeds", type=str, default=None,
                    help="Comma-separated list of specific node names to use as "
                         "the 'selected rare nodes' (must be rare nodes found by "
                         "RareNodeFinder). Default: ALL rare nodes.")
    p.add_argument("--num-cone-seeds", type=int, default=None,
                    help="Instead of --cone-seeds, randomly pick this many rare "
                         "nodes (e.g. to mimic an N-trigger Trojan's footprint). "
                         "Default: use all rare nodes.")
    p.add_argument("--cone-seed-rng-seed", type=int, default=99,
                    help="RNG seed for --num-cone-seeds sampling [default: 99]")
    p.add_argument("--use-regions", action="store_true",
                    help="Use the densest rare-node region(s) as the cone seeds "
                         "(reuses the region selection saved by run_mers.py "
                         "--use-regions, or recomputes it). NOTE: if Dataset B was "
                         "built with regions, Dataset A reuses that selection "
                         "AUTOMATICALLY even without this flag.")
    p.add_argument("--all-rare", action="store_true",
                    help="Force the cone to use ALL rare nodes, overriding the "
                         "automatic reuse of Dataset B's saved region selection.")
    p.add_argument("--region-top-k", type=int, default=1,
                    help="Number of densest regions to use as cone seeds [default: 1].")
    p.add_argument("--region-fanin-depth", type=int, default=3,
                    help="Region cone depth (only if recomputing) [default: 3].")
    p.add_argument("--region-overlap", type=float, default=0.30,
                    help="Region overlap threshold (only if recomputing) [default: 0.30].")
    p.add_argument("--region-mode", choices=["compact", "components"], default="compact",
                    help="Region mode (only if recomputing) [default: compact].")
    p.add_argument("--region-max-size", type=int, default=40,
                    help="Max rare nodes per region (only if recomputing) [default: 40].")
    p.add_argument("--placement", default=None,
                    help="Vivado placement file for PHYSICAL region recompute "
                         "(only used if no saved region_selection checkpoint).")
    p.add_argument("--phys-radius", type=float, default=4.0,
                    help="Physical clustering radius [default: 4.0].")

    # --- Generation objective / search ---
    p.add_argument("--mutation-rounds-a", type=int, default=5,
                    help="Single-bit-flip refinement rounds per vector for "
                         "Dataset A (flat mode) [default: 5]")
    p.add_argument("--w-rare", type=float, default=10.0, help="Weight: minimise RareConeSwitch [default: 10]")
    p.add_argument("--w-bg", type=float, default=1.0, help="Weight: match BackgroundSwitch [default: 1]")
    p.add_argument("--w-in-hd", type=float, default=1.0, help="Weight: match Input Hamming Distance [default: 1]")
    p.add_argument("--w-in-hw", type=float, default=1.0, help="Weight: match Input Hamming Weight [default: 1]")
    p.add_argument("--w-out-hd", type=float, default=1.0, help="Weight: match Output Hamming Distance [default: 1]")
    p.add_argument("--w-out-hw", type=float, default=1.0, help="Weight: match Output Hamming Weight [default: 1]")
    p.add_argument("--w-state", type=float, default=0.0,
                    help="Weight: match scan-state (DFF) Hamming distance as a "
                         "SEPARATE term. Default 0 (off): in full-scan mode the "
                         "DFF next-state already appears among outputs, so "
                         "Output HD covers it. [default: 0]")
    p.add_argument("--rng-seed", type=int, default=7, help="RNG seed for generation [default: 7]")

    # --- Dynamic weight balancing (phased solver) ---
    p.add_argument("--dynamic-weights", action="store_true",
                    help="Enable the 3-phase solver: Phase 1 suppresses "
                         "RareConeSwitch with a high rare weight, Phase 2 locks "
                         "the cone-input PIs, Phase 3 matches Dataset B's "
                         "statistics by flipping only the unlocked background "
                         "inputs (cannot re-disturb the cone).")
    p.add_argument("--phase1-rounds", type=int, default=None,
                    help="Max Phase-1 (suppression) rounds [default: mutation-rounds-a]")
    p.add_argument("--phase3-rounds", type=int, default=None,
                    help="Max Phase-3 (matching) rounds [default: mutation-rounds-a]")
    p.add_argument("--phase1-w-rare", type=float, default=50.0,
                    help="Phase-1 rare-cone weight [default: 50]")
    p.add_argument("--rare-threshold", type=int, default=0,
                    help="RareConeSwitch value at/below which Phase 1 stops and "
                         "bit-locking begins (0 = require exactly zero) [default: 0]")
    p.add_argument("--lock-mode", choices=["soft", "hard", "none"], default="soft",
                    help="Phase 2/3 bit-locking policy. 'soft' (default): Phase 3 "
                         "may flip ALL inputs but the rare term stays active so it "
                         "won't re-disturb the cone -- robust when cone-input bits "
                         "also drive the outputs (e.g. s38417). 'hard': Phase 3 "
                         "flips ONLY background (non-cone-input) inputs -- strongest "
                         "cone guarantee but can starve output matching. 'none': no "
                         "locking. [default: soft]")
    p.add_argument("--max-candidates", type=int, default=None,
                    help="SPEED: evaluate only this many randomly-chosen input-bit "
                         "flips per search round instead of all of them. Cost scales "
                         "~linearly with this, so e.g. 250 on s38417 (1559 inputs) is "
                         "~10x faster with negligible quality loss. Auto-set to 250 "
                         "for circuits with >600 inputs when --dynamic-weights is on; "
                         "pass 0 to force ALL bits every round. [default: auto]")
    p.add_argument("--no-improve-patience", type=int, default=2,
                    help="Stop a phase after this many consecutive non-improving "
                         "rounds. With subsampling >1 is needed since one empty round "
                         "no longer means converged. [default: 2]")
    p.add_argument("--phase3-w-bg", type=float, default=2.0, help="Phase-3 BackgroundSwitch weight [default: 2]")
    p.add_argument("--phase3-w-in-hd", type=float, default=2.0, help="Phase-3 Input HD weight [default: 2]")
    p.add_argument("--phase3-w-out-hd", type=float, default=2.0, help="Phase-3 Output HD weight [default: 2]")
    p.add_argument("--phase3-w-state", type=float, default=0.0, help="Phase-3 scan-state weight [default: 0]")


    p.add_argument("--save-dir", default="mers_progress", help="Checkpoint root [default: mers_progress]")
    p.add_argument("--results-dir", default="mers_results", help="Excel report root [default: mers_results]")
    p.add_argument("--vectors-dir", default="mers_testvectors", help="Vector export root [default: mers_testvectors]")
    p.add_argument("--no-full-scan", action="store_true", help="Do NOT convert DFFs to full-scan mode")
    p.add_argument("--no-export-vectors", action="store_true", help="Skip writing A's vectors to .txt/.xlsx")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def _resolve_max_candidates(val):
    """
    Resolve the --max-candidates CLI value into the generator's parameter.
      None  (flag omitted)  -> None  => generator auto-decides (subsample on
                                        large circuits, off on small ones).
      0     (explicit)      -> a very large cap => effectively ALL bits every
                                        round (disables subsampling).
      N>0                   -> N.
    """
    if val is None:
        return None
    if val == 0:
        return 10**9   # larger than any n_pi -> pool never subsampled
    return val


def _extract_testset(obj):
    if isinstance(obj, dict) and "testset" in obj:
        return obj["testset"]
    return obj


def _find_rare_nodes_key(pm):
    keys = [k for k in pm.list_checkpoints() if k.startswith("rare_nodes_v6")]
    if not keys:
        raise SystemExit(
            "No rare_nodes_v6_* checkpoint found. Run run_mers.py for this "
            "--name first.")
    if len(keys) > 1:
        print(f"  [WARN] Multiple rare_nodes_v6_* checkpoints found; using {keys[0]}")
    return keys[0]


def _b_testset_key(args, mers_tag):
    if args.B_method == "random":
        return f"random_v6_testset_{args.vectors}"
    if args.B_method == "mero":
        return f"mero_v6_testset_N{args.N}_r{args.mero_rounds}"
    if args.B_method == "mers":
        return f"mers_v6_testset_{mers_tag}"
    if args.B_method == "mers_h":
        return f"mers_h_v6_testset_{mers_tag}"
    if args.B_method == "mers_s":
        return f"mers_s_v6_testset_{mers_tag}_C{args.C}"
    raise ValueError(args.B_method)


def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
    )

    from src.circuit_parser import Circuit
    from src.progress_manager import ProgressManager
    from src.dataset_a_generator import DatasetAGenerator
    from src.dataset_a_reporter import DatasetComparisonReporter
    from src.testset_exporter import TestsetExporter

    if not os.path.isfile(args.bench):
        raise SystemExit(f"Bench file not found: {args.bench}")

    print(f"\n[1/5] Parsing circuit: {args.bench}")
    circuit = Circuit()
    circuit.parse_bench(args.bench, full_scan=not args.no_full_scan)

    pm = ProgressManager(args.save_dir, args.name)

    print("[2/5] Loading rare nodes + Dataset B checkpoint ...")
    rare_key = _find_rare_nodes_key(pm)
    rni = pm.load(rare_key)
    rare_nodes = rni['rare_nodes']
    print(f"  Rare nodes: {len(rare_nodes)}  (from '{rare_key}')")

    mers_tag = f"N{args.N}_{args.mutation_mode}"
    if args.mutation_mode == "fast":
        mers_tag += f"_r{args.mutation_rounds}"
    b_key = _b_testset_key(args, mers_tag)
    if not pm.exists(b_key):
        available = "\n".join(f"    {k}" for k in pm.list_checkpoints())
        raise SystemExit(
            f"Dataset B checkpoint '{b_key}' not found under "
            f"{args.save_dir}/{args.name}/.\n"
            f"  Available checkpoints:\n{available}\n"
            f"  Check --B-method/--N/--mutation-mode/--mutation-rounds/--C match "
            f"the original run_mers.py invocation.")
    testset_B = _extract_testset(pm.load(b_key))
    if args.B_limit:
        testset_B = testset_B[:args.B_limit]
    print(f"  Dataset B ('{args.B_method}', '{b_key}'): {len(testset_B):,} vectors")

    # ---- Select cone seed nodes ----
    # Precedence:
    #   1. --cone-seeds / --num-cone-seeds  -> explicit override (you choose)
    #   2. a saved region_selection (Dataset B was built with --use-regions)
    #      -> AUTOMATICALLY reuse the SAME region rare nodes, so A and B target
    #         the identical set without needing to repeat the flag. Pass
    #         --all-rare to override this and use every rare node instead.
    #   3. --use-regions with no saved selection -> (re)compute regions here
    #   4. otherwise -> ALL rare nodes (whole rare-logic cone)
    seed_nodes = rare_nodes
    seed_source = "all rare nodes (default)"
    if args.cone_seeds:
        seed_source = "explicit --cone-seeds"
        names = [n.strip() for n in args.cone_seeds.split(",") if n.strip()]
        # Allow ANY valid circuit gate node as a seed, not only pre-classified
        # rare nodes. This is what lets you target the ACTUAL Trojan trigger
        # nodes you measure downstream (which may not be flagged "rare" by the
        # finder). For names already in the rare set we use their measured
        # polarity; for others we infer polarity from a quick Monte-Carlo
        # P(node==1): rare_value = 1 if the node is mostly 0 (rarer to be 1),
        # else 0. Names that aren't gates at all are a hard error.
        not_gates = [n for n in names if n not in circuit.gates]
        if not_gates:
            raise SystemExit(
                f"--cone-seeds contains names that are not gate outputs in the "
                f"circuit: {not_gates}\n  (Check spelling / netlist naming. "
                f"Primary inputs and constants can't be cone seeds.)")
        seed_nodes = {}
        unknown = [n for n in names if n not in rare_nodes]
        inferred = {}
        if unknown:
            import numpy as _np
            rng = _np.random.RandomState(12345)
            probe = rng.randint(0, 2, size=(2000, circuit.n_inputs())).astype('uint8')
            M, nidx = circuit.simulate_matrix(probe)
            for n in unknown:
                p1 = float(M[nidx[n]].mean())
                rv = 1 if p1 <= 0.5 else 0          # rare value = the less likely one
                inferred[n] = {'rare_value': rv, 'non_rare_value': 1 - rv,
                               'probability': p1 if rv == 1 else 1 - p1}
        for n in names:
            seed_nodes[n] = rare_nodes[n] if n in rare_nodes else inferred[n]
        if unknown:
            print(f"  ({len(unknown)} seed node(s) not in the rare set; polarity "
                  f"inferred from Monte-Carlo: {unknown[:6]}"
                  f"{' ...' if len(unknown) > 6 else ''})")
    elif args.num_cone_seeds:
        seed_source = f"--num-cone-seeds {args.num_cone_seeds} (random rare nodes)"
        import random
        names = list(rare_nodes.keys())
        random.Random(args.cone_seed_rng_seed).shuffle(names)
        chosen = names[:args.num_cone_seeds]
        seed_nodes = {n: rare_nodes[n] for n in chosen}
    elif (not args.all_rare) and (args.use_regions or pm.exists('region_selection')):
        # Reuse the exact region(s) MERS targeted for Dataset B, if available;
        # otherwise recompute regions here with matching parameters. This branch
        # is taken AUTOMATICALLY when Dataset B was built with --use-regions
        # (a region_selection checkpoint exists), so A always matches B.
        sel = None
        if pm.exists('region_selection'):
            rsel = pm.load('region_selection')
            sel = rsel.get('selected_rare_nodes')
            auto = "" if args.use_regions else " [auto: Dataset B used regions]"
            seed_source = f"saved region selection{auto}"
            print(f"  Using saved region selection: {len(sel)} rare nodes "
                  f"from region(s) {rsel.get('chosen_region_ids')} "
                  f"(top-{rsel.get('region_top_k')}).{auto}")
        if not sel:
            seed_source = "--use-regions (recomputed)"
            print(f"  No saved region selection; recomputing top-{args.region_top_k} "
                  f"region(s).")
            if args.placement:
                from src.placement_regions import (load_placement,
                                                   match_rare_to_coords,
                                                   build_physical_regions)
                from src.rare_node_regions import select_top_regions
                coords = load_placement(args.placement)
                located, _ = match_rare_to_coords(rare_nodes, coords)
                rr = build_physical_regions(rare_nodes, located,
                                            radius=args.phys_radius,
                                            mode=args.region_mode,
                                            max_region_size=args.region_max_size)
            else:
                from src.rare_node_regions import build_regions, select_top_regions
                rr = build_regions(circuit, rare_nodes,
                                   fanin_depth=args.region_fanin_depth,
                                   overlap_threshold=args.region_overlap,
                                   mode=args.region_mode,
                                   max_region_size=args.region_max_size)
            sel, _ = select_top_regions(rr, top_k=args.region_top_k)
        seed_nodes = {n: rare_nodes[n] for n in sel if n in rare_nodes}
    elif args.all_rare and pm.exists('region_selection'):
        seed_source = "all rare nodes (--all-rare override; B used regions)"
        print(f"  --all-rare: ignoring the saved region selection; using ALL "
              f"{len(rare_nodes)} rare nodes.")

    print(f"  Suspicious-cone seeds: {len(seed_nodes)} node(s) from {seed_source}.")
    if seed_source.startswith("all rare"):
        print(f"  -> Dataset A will suppress switching across ALL {len(seed_nodes)} "
              f"rare nodes (whole rare-logic cone).")
    print(f"     e.g. {list(seed_nodes.keys())[:10]}"
          f"{' ...' if len(seed_nodes) > 10 else ''}")

    # ---- Generate Dataset A ----
    print(f"\n[3/5] Generating Dataset A ({len(testset_B):,} vector pairs, "
          f"cone fanin={args.cone_fanin_depth} fanout={args.cone_fanout_depth}, "
          f"mutation_rounds={args.mutation_rounds_a}) ...")
    weights = {
        'rare':    args.w_rare,
        'bg':      args.w_bg,
        'in_hd':   args.w_in_hd,
        'in_hw':   args.w_in_hw,
        'out_hd':  args.w_out_hd,
        'out_hw':  args.w_out_hw,
        'state':   args.w_state,
    }
    phase1_weights = {'rare': args.phase1_w_rare}
    phase3_weights = {
        'bg':     args.phase3_w_bg,
        'in_hd':  args.phase3_w_in_hd,
        'out_hd': args.phase3_w_out_hd,
        'state':  args.phase3_w_state,
    }
    gen = DatasetAGenerator(
        circuit, seed_nodes,
        cone_fanin_depth=args.cone_fanin_depth,
        cone_fanout_depth=args.cone_fanout_depth,
        weights=weights,
        mutation_rounds=args.mutation_rounds_a,
        rng_seed=args.rng_seed,
        dynamic_weights=args.dynamic_weights,
        phase1_weights=phase1_weights,
        phase3_weights=phase3_weights,
        phase1_rounds=args.phase1_rounds,
        phase3_rounds=args.phase3_rounds,
        rare_threshold=args.rare_threshold,
        lock_mode=args.lock_mode,
        max_candidates=_resolve_max_candidates(args.max_candidates),
        no_improve_patience=args.no_improve_patience,
    )
    a_tag = (f"{args.B_method}_{mers_tag}"
             f"{'_C'+str(args.C) if args.B_method=='mers_s' else ''}"
             f"_cone_fi{args.cone_fanin_depth}_fo{args.cone_fanout_depth}"
             f"_seeds{len(seed_nodes)}"
             f"{'_dyn' if args.dynamic_weights else ''}")
    cache_key = f"dataset_a_{a_tag}"
    result = gen.generate(testset_B, progress_mgr=pm, cache_key=cache_key)

    # ---- Summary to console ----
    import numpy as np
    print(f"\n[4/5] Summary (A vs B, {len(result['per_pair_A'])} pairs):")
    print(f"  {'metric':<22} {'A: mean':>10} {'A: std':>8} | {'B: mean':>10} {'B: std':>8}")
    for key, label in [("rare_cone_sw", "RareConeSwitch"), ("bg_sw", "BackgroundSwitch"),
                        ("in_hd", "Input HD"), ("in_hw", "Input HW"),
                        ("out_hd", "Output HD"), ("out_hw", "Output HW")]:
        a = np.array([d[key] for d in result['per_pair_A']], dtype=float)
        b = np.array([d[key] for d in result['per_pair_B']], dtype=float)
        print(f"  {label:<22} {a.mean():>10.3f} {a.std():>8.3f} | {b.mean():>10.3f} {b.std():>8.3f}")
    cov_a = sum(1 for d in result['per_pair_A'] if d['rare_cone_sw'] == 0)
    cov_b = sum(1 for d in result['per_pair_B'] if d['rare_cone_sw'] == 0)
    K = len(result['per_pair_A'])
    print(f"\n  RareConeSwitch == 0:  A: {cov_a}/{K} ({cov_a/K*100:.1f}%)   "
          f"B: {cov_b}/{K} ({cov_b/K*100:.1f}%)")
    print(f"  Cone nodes: {len(result['cone_nodes'])}   Background nodes: {len(result['bg_nodes'])}")

    # ---- Reports / exports ----
    print("\n[5/5] Writing report and vectors ...")
    report_path = DatasetComparisonReporter(args.results_dir).generate(
        result, args.name, tag=a_tag)

    if not args.no_export_vectors:
        manifest = TestsetExporter(args.vectors_dir).generate(
            {"Dataset-B": testset_B, "Dataset-A": result['testset']},
            circuit, args.name)
        pm.save(f"vector_export_manifest_{a_tag}", manifest, use_json=True)
        print(f"  Vectors      → {args.vectors_dir}/{args.name}/")

    print(f"  Comparison   → {report_path}")
    print("\n✓ Dataset A generation complete.")


if __name__ == "__main__":
    main()
