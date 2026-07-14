#!/usr/bin/env python3
"""
run_mers.py
===========
Command-line entry point for the MERS Hardware Trojan Detection pipeline.

Examples
--------
# Quickstart with c17 (tiny circuit, instant results):
  python run_mers.py --bench benchmarks/c17.bench --name c17 --N 50 --vectors 500

# Full run matching the paper (c2670):
  python run_mers.py --bench benchmarks/c2670.bench --name c2670 --N 1000

# Skip slow MERS-s reordering:
  python run_mers.py --bench benchmarks/c3540.bench --name c3540 --skip-mers-s

# Trust-Hub AES benchmark (Verilog requires conversion first):
  python run_mers.py --bench benchmarks/AES-T200_gate.bench --name AES-T200 --N 1000

# Check what's already done (no re-computation):
  python run_mers.py --bench benchmarks/c2670.bench --name c2670 --status-only

# Force redo a specific stage (delete its checkpoint):
  python run_mers.py --bench benchmarks/c2670.bench --name c2670 --delete-stage rare_nodes
"""

import sys
import os
import argparse
import logging

# ── Make sure the project root is on PYTHONPATH ──────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    fmt   = "%(asctime)s  %(levelname)-7s  %(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("mers_run.log", mode='a', encoding='utf-8'),
        ]
    )


def parse_args():
    p = argparse.ArgumentParser(
        description="MERS – Statistical Test Generation for Side-Channel "
                    "Analysis based Trojan Detection (Huang et al., CCS 2016)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    # Required
    p.add_argument("--bench", required=True,
                   help="Path to the .bench netlist file")
    p.add_argument("--name",  required=True,
                   help="Short name for this benchmark (used in filenames)")

    # Core algorithm parameters
    p.add_argument("--N",        type=int,   default=1000,
                   help="Rare-switching requirement per node  [default: 1000]")
    p.add_argument("--threshold",type=float, default=0.1,
                   help="Rare-node threshold P(rare)         [default: 0.1]")
    p.add_argument("--vectors",  type=int,   default=10_000,
                   help="Random candidate-pool size for MERS generation "
                        "[default: 10000]")
    p.add_argument("--rare-vectors", type=int, default=None,
                   help="Monte-Carlo vectors used ONLY to DETECT rare nodes "
                        "(decoupled from --vectors). More = more accurate rareness "
                        "estimate. [default: same as --vectors]")
    p.add_argument("--trojans",  type=int,   default=1000,
                   help="Trojan instances for evaluation     [default: 1000]")
    p.add_argument("--triggers", type=int,   nargs='+', default=[4, 8],
                   help="Trigger counts to evaluate          [default: 4 8]")
    p.add_argument("--C",        type=int,   default=5,
                   help="MERS-s weight ratio                 [default: 5]")

    # Optional flags
    p.add_argument("--skip-mero", action="store_true",
                   help="Skip MERO activation-based baseline")
    p.add_argument("--skip-mers-h",  action="store_true",
                   help="Skip Hamming-distance reordering (MERS-h)")
    p.add_argument("--skip-mers-s",  action="store_true",
                   help="Skip simulation-based reordering (MERS-s, slowest)")
    p.add_argument("--mutation-mode", choices=["paper", "fast"], default="paper",
                   help="MERS mutation: exact paper scan or fast vectorised approximation [default: paper]")
    p.add_argument("--use-regions", action="store_true",
                   help="Group rare nodes into structural regions (dense pockets a "
                        "Trojan trigger could be built from) and run MERS on only the "
                        "top-K densest regions instead of all rare nodes.")
    p.add_argument("--region-top-k", type=int, default=1,
                   help="Number of densest regions to target [default: 1].")
    p.add_argument("--region-fanin-depth", type=int, default=3,
                   help="Fan-in cone depth used to define region proximity [default: 3].")
    p.add_argument("--region-overlap", type=float, default=0.30,
                   help="Cone-overlap (Jaccard) threshold for grouping rare nodes "
                        "[default: 0.30].")
    p.add_argument("--region-mode", choices=["compact", "components"], default="compact",
                   help="'compact': many small dense pockets (recommended). "
                        "'components': connected components (fewer, larger) [default: compact].")
    p.add_argument("--region-max-size", type=int, default=40,
                   help="Max rare nodes per region in compact mode [default: 40].")
    p.add_argument("--placement", default=None,
                   help="Vivado placement file (cell,x,y CSV or 'cell SLICE_X#Y#'). "
                        "With --use-regions, clusters rare nodes by PHYSICAL die "
                        "proximity (for SCA) instead of logical fan-in cones.")
    p.add_argument("--phys-radius", type=float, default=4.0,
                   help="Physical clustering radius in placement units [default: 4.0].")
    p.add_argument("--pad-to-requested", action="store_true",
                   help="Top the MERS testset back up to --vectors with unused "
                        "high-coverage vectors (MERS normally keeps only vectors "
                        "that excite an unsatisfied rare node, so the kept count "
                        "is usually < --vectors).")
    p.add_argument("--mutation-rounds", type=int, default=12,
                   help="Best-flip rounds for --mutation-mode fast [default: 12]")
    p.add_argument("--no-full-scan", action="store_true",
                   help="Do NOT convert DFFs to full-scan mode")
    p.add_argument("--include-artifact-nodes", action="store_true",
                   help="Do NOT exclude synthetic converter helper nodes (default excludes nodes starting with __)")
    p.add_argument("--artifact-prefixes", default="__",
                   help="Comma-separated prefixes to exclude as synthetic artefacts [default: __]")

    # Directories
    p.add_argument("--save-dir",    default="mers_progress",
                   help="Checkpoint directory  [default: mers_progress]")
    p.add_argument("--results-dir", default="mers_results",
                   help="Excel output directory [default: mers_results]")
    p.add_argument("--vectors-dir", default="mers_testvectors",
                   help="Automatic test-vector export directory [default: mers_testvectors]")
    p.add_argument("--no-export-vectors", action="store_true",
                   help="Disable automatic .txt and .xlsx export of generated test vectors")

    # Utility modes
    p.add_argument("--status-only",   action="store_true",
                   help="Print checkpoint status then exit (no computation)")
    p.add_argument("--delete-stage",  type=str, default=None,
                   help="Delete a checkpoint stage by key, then exit")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Enable DEBUG logging")

    return p.parse_args()


def main():
    args = parse_args()
    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    # ── Utility modes ──────────────────────────────────────────────────────
    if args.status_only or args.delete_stage:
        from src.progress_manager import ProgressManager
        pm = ProgressManager(args.save_dir, args.name)
        pm.print_status()

        if args.delete_stage:
            pm.delete(args.delete_stage)
            print(f"\nDeleted checkpoint: '{args.delete_stage}'")
        return

    # ── Validate inputs ────────────────────────────────────────────────────
    if not os.path.isfile(args.bench):
        print(f"\nERROR: Bench file not found: {args.bench}")
        print("  Download ISCAS benchmarks first:")
        print("    python download_benchmarks.py")
        sys.exit(1)

    logger.info(f"Starting MERS for '{args.name}'")
    logger.info(f"  Bench  : {args.bench}")
    logger.info(f"  N      : {args.N}")
    logger.info(f"  Thresh : {args.threshold}")
    logger.info(f"  Vectors: {args.vectors:,}")
    logger.info(f"  Trojans: {args.trojans}")
    logger.info(f"  Mutation mode: {args.mutation_mode}")
    logger.info(f"  Mutation rounds: {args.mutation_rounds}")
    logger.info(f"  Artifact rare-node filter: {not args.include_artifact_nodes}")

    # ── Run pipeline ───────────────────────────────────────────────────────
    from src.pipeline import MERSPipeline

    pipe = MERSPipeline(
        bench_file       = args.bench,
        circuit_name     = args.name,
        N                = args.N,
        rare_threshold   = args.threshold,
        num_rand_vectors = args.vectors,
        rare_vectors     = args.rare_vectors,
        trigger_counts   = args.triggers,
        C                = args.C,
        num_trojans      = args.trojans,
        save_dir         = args.save_dir,
        results_dir      = args.results_dir,
        skip_mero        = args.skip_mero,
        skip_mers_h      = args.skip_mers_h,
        skip_mers_s      = args.skip_mers_s,
        mutation_mode    = args.mutation_mode,
        mutation_rounds  = args.mutation_rounds,
        pad_to_requested = args.pad_to_requested,
        use_regions      = args.use_regions,
        region_top_k     = args.region_top_k,
        region_fanin_depth = args.region_fanin_depth,
        region_overlap   = args.region_overlap,
        region_mode      = args.region_mode,
        region_max_size  = args.region_max_size,
        placement_file   = args.placement,
        phys_radius      = args.phys_radius,
        full_scan        = not args.no_full_scan,
        export_vectors   = not args.no_export_vectors,
        vectors_dir      = args.vectors_dir,
        exclude_artifact_nodes = not args.include_artifact_nodes,
        artifact_prefixes = [p for p in args.artifact_prefixes.split(",") if p],
    )

    results = pipe.run()

    print("\n✓ Pipeline complete.")
    print(f"  Excel reports → {args.results_dir}/")
    if not args.no_export_vectors:
        print(f"  Test vectors  → {args.vectors_dir}/{args.name}/")
    print(f"  Checkpoints   → {args.save_dir}/{args.name}/")
    print(f"  Log           → mers_run.log")


if __name__ == "__main__":
    main()
