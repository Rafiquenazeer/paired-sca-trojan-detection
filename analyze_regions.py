#!/usr/bin/env python3
"""
analyze_regions.py
==================
Inspect the rare-node "regions" of a circuit WITHOUT running the full MERS
pipeline, so you can see how many dense pockets exist and how big each is
before choosing --region-top-k.

A region = a tight cluster of rare nodes whose fan-in cones overlap, i.e.
rare nodes an attacker could realistically wire into one Trojan trigger.
Regions are ranked densest-first. See src/rare_node_regions.py for details.

USAGE
-----
  python analyze_regions.py --bench benchmarks/s38417.bench \
      --threshold 0.1 --region-mode compact --region-top-k 5

  # reuse a cached rare-node checkpoint instead of re-finding rare nodes:
  python analyze_regions.py --bench benchmarks/s38417.bench --name s38417_AB
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", required=True)
    ap.add_argument("--name", default=None,
                    help="If given, reuse the rare-node checkpoint under "
                         "mers_progress/<name>/ instead of recomputing.")
    ap.add_argument("--threshold", type=float, default=0.1)
    ap.add_argument("--vectors", "--rare-vectors", dest="vectors",
                    type=int, default=100_000,
                    help="Monte-Carlo vectors for rare-node detection before "
                         "building regions [default: 100000]. Ignored when --name "
                         "reuses an existing rare-node checkpoint.")
    ap.add_argument("--no-full-scan", action="store_true")
    ap.add_argument("--region-mode", choices=["compact", "components"], default="compact")
    ap.add_argument("--region-fanin-depth", type=int, default=3)
    ap.add_argument("--region-overlap", type=float, default=0.30)
    ap.add_argument("--region-max-size", type=int, default=40)
    ap.add_argument("--region-top-k", type=int, default=10,
                    help="How many top regions to print [default: 10]")
    ap.add_argument("--save-dir", default="mers_progress")
    ap.add_argument("--results-dir", default="mers_results",
                    help="Where to write the Excel report [default: mers_results]")
    ap.add_argument("--no-excel", action="store_true",
                    help="Skip writing the Excel region report.")
    ap.add_argument("--region-sheets", type=int, default=10,
                    help="Number of per-region detail sheets to include [default: 10]")
    # --- physical (FPGA placement-aware) region mode ---
    ap.add_argument("--placement", default=None,
                    help="Path to a Vivado placement file (cell,x,y CSV or "
                         "'cell SLICE_X#Y#' rows). When given, regions are "
                         "clustered by PHYSICAL proximity on the die instead of "
                         "logical fan-in cones -- the correct notion for "
                         "side-channel analysis. See src/placement_regions.py "
                         "for the Vivado export snippet.")
    ap.add_argument("--phys-radius", type=float, default=4.0,
                    help="Physical clustering radius in placement-grid units "
                         "(two rare nodes group if their slice-distance <= this) "
                         "[default: 4.0]")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    from src.circuit_parser import Circuit
    from src.rare_node_regions import build_regions, select_top_regions

    circuit = Circuit()
    circuit.parse_bench(a.bench, full_scan=not a.no_full_scan)

    rare_nodes = None
    if a.name:
        from src.progress_manager import ProgressManager
        pm = ProgressManager(a.save_dir, a.name)
        keys = [k for k in pm.list_checkpoints() if k.startswith("rare_nodes_v6")]
        if keys:
            rni = pm.load(keys[0])
            rare_nodes = rni["rare_nodes"]
            # The checkpoint key encodes the detection sample size as _V<N>.
            import re as _re
            m = _re.search(r"_V(\d+)", keys[0])
            nv = int(m.group(1)) if m else None
            print(f"Loaded {len(rare_nodes)} rare nodes from checkpoint "
                  f"'{keys[0]}'" + (f" (detected with {nv:,} vectors)." if nv else "."))
            if nv and nv < 10_000:
                print(f"  [note] that checkpoint used only {nv:,} detection "
                      f"vectors. For cleaner regions, re-run run_mers.py with "
                      f"--rare-vectors 100000, or drop --name here to detect "
                      f"fresh at the 100k default.")
    if rare_nodes is None:
        from src.rare_node_finder import RareNodeFinder
        print(f"Detecting rare nodes with {a.vectors:,} Monte-Carlo vectors "
              f"(threshold={a.threshold}) before building regions ...")
        rni = RareNodeFinder(circuit, rare_threshold=a.threshold,
                             num_vectors=a.vectors, seed=42).find_rare_nodes()
        rare_nodes = rni["rare_nodes"]

    R = len(rare_nodes)
    print(f"\nTotal rare nodes: {R}")

    if a.placement:
        # ---- Physical (FPGA placement-aware) regions ----
        from src.placement_regions import (load_placement, match_rare_to_coords,
                                           build_physical_regions)
        print(f"Region kind: PHYSICAL (FPGA placement)  radius: {a.phys_radius}  "
              f"mode: {a.region_mode}\n")
        coords = load_placement(a.placement)
        located, missing = match_rare_to_coords(rare_nodes, coords)
        print(f"Placement match: {len(located)}/{R} rare nodes located on the die "
              f"({100*len(located)/max(1,R):.0f}%).")
        if missing:
            print(f"  {len(missing)} rare node(s) had no placement match "
                  f"(e.g. {missing[:4]}). They are omitted from physical regions.")
        rr = build_physical_regions(
            rare_nodes, located, radius=a.phys_radius,
            mode=a.region_mode, max_region_size=a.region_max_size)
        col_label = "slices"
    else:
        # ---- Logical (shared fan-in cone) regions ----
        print(f"Region kind: LOGICAL (fan-in cone)  mode: {a.region_mode}  "
              f"fan-in depth: {a.region_fanin_depth}  overlap: {a.region_overlap}  "
              f"max-size: {a.region_max_size}\n")
        rr = build_regions(circuit, rare_nodes,
                           fanin_depth=a.region_fanin_depth,
                           overlap_threshold=a.region_overlap,
                           mode=a.region_mode,
                           max_region_size=a.region_max_size)
        col_label = "cone"
    regs = rr["regions"]

    print(f"{'rank':>4} {'rare':>5} {col_label:>6} {'density':>8}  sample rare nodes")
    print("-" * 72)
    for reg in regs[:a.region_top_k]:
        sample = ", ".join(reg["rare_nodes"][:3])
        print(f"{reg['id']:>4} {reg['size']:>5} {reg['cone_size']:>6} "
              f"{reg['density']:>8.3f}  {sample}")

    print(f"\nTotal regions: {len(regs)}  (+{len(rr['singletons'])} singletons)")
    for k in (1, 2, 3, 5):
        if k <= len(regs):
            sel, _ = select_top_regions(rr, top_k=k)
            print(f"  top-{k}: {len(sel):>4} rare nodes "
                  f"({100*len(sel)/R:>4.0f}% of all)  "
                  f"=> MERS targets {100*(1-len(sel)/R):.0f}% fewer nodes")

    print("\nPick --region-top-k so the union covers the pocket(s) you want to "
          "probe.\nDenser regions (higher density = rare nodes per cone gate) are "
          "the\nmore probable Trojan hiding spots.")

    # ---- Excel report ----
    if not a.no_excel:
        from src.excel_reporter import RegionReporter
        cname = a.name or os.path.splitext(os.path.basename(a.bench))[0]
        path = RegionReporter(a.results_dir).generate(
            rr, rare_nodes, cname, per_region_sheets=a.region_sheets)
        print(f"\nExcel region report written to: {path}")
        print("  Sheets: Summary (overview + chart + coverage), "
              "All Rare Nodes (with region id), one sheet per top region.")


if __name__ == "__main__":
    main()
