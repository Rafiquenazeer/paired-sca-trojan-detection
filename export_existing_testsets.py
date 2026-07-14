#!/usr/bin/env python3
"""
export_existing_testsets.py
===========================
Export already-generated checkpointed testsets to .txt and .xlsx without
rerunning the full MERS pipeline.

Example for your fast s15850 run:

  python export_existing_testsets.py \
    --bench benchmarks/s15850.bench \
    --name s15850_v5_fast \
    --N 1000 \
    --vectors 10000 \
    --mutation-mode fast \
    --mutation-rounds 12 \
    --C 5

Output:

  mers_testvectors/<name>/
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def parse_args():
    p = argparse.ArgumentParser(
        description="Export existing MERS/MERO/Random testset checkpoints to text and Excel."
    )
    p.add_argument("--bench", required=True, help="Path to .bench file")
    p.add_argument("--name", required=True, help="Run name / checkpoint folder name")
    p.add_argument("--N", type=int, default=1000, help="N used for MERS/MERO [default: 1000]")
    p.add_argument("--vectors", type=int, default=10_000, help="Random vector count used [default: 10000]")
    p.add_argument("--mutation-mode", choices=["paper", "fast"], default="paper")
    p.add_argument("--mutation-rounds", type=int, default=12)
    p.add_argument("--C", type=int, default=5, help="MERS-s C value [default: 5]")
    p.add_argument("--save-dir", default="mers_progress", help="Checkpoint root [default: mers_progress]")
    p.add_argument("--vectors-dir", default="mers_testvectors", help="Export root [default: mers_testvectors]")
    p.add_argument("--no-full-scan", action="store_true", help="Do NOT convert DFFs to full-scan mode")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def _extract_testset(obj):
    if isinstance(obj, dict) and "testset" in obj:
        return obj["testset"]
    return obj


def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
    )
    logger = logging.getLogger("export_existing_testsets")

    from src.circuit_parser import Circuit
    from src.progress_manager import ProgressManager
    from src.testset_exporter import TestsetExporter

    if not os.path.isfile(args.bench):
        raise SystemExit(f"Bench file not found: {args.bench}")

    circuit = Circuit()
    circuit.parse_bench(args.bench, full_scan=not args.no_full_scan)

    pm = ProgressManager(args.save_dir, args.name)

    mers_tag = f"N{args.N}_{args.mutation_mode}"
    if args.mutation_mode == "fast":
        mers_tag += f"_r{args.mutation_rounds}"

    candidates = {
        "Random": f"random_v5_testset_{args.vectors}",
        "MERO":   "mero_v5_testset_N{}_r8".format(args.N),
        "MERS":   f"mers_v5_testset_{mers_tag}",
        "MERS-h": f"mers_h_v5_testset_{mers_tag}",
        "MERS-s": f"mers_s_v5_testset_{mers_tag}_C{args.C}",
    }

    testsets = {}
    print(f"\nSearching checkpoints in: {args.save_dir}/{args.name}/\n")
    for method, key in candidates.items():
        if pm.exists(key):
            obj = pm.load(key)
            ts = _extract_testset(obj)
            testsets[method] = ts
            print(f"  [FOUND] {method:<8} {key:<40} vectors={len(ts):,}")
        else:
            print(f"  [MISS ] {method:<8} {key}")

    if not testsets:
        raise SystemExit("\nNo testset checkpoints found. Check --name, --N, --vectors, and mutation options.")

    manifest = TestsetExporter(args.vectors_dir).generate(testsets, circuit, args.name)
    pm.save("vector_export_manifest", manifest, use_json=True)

    print("\n✓ Export complete.")
    print(f"  Test vectors → {args.vectors_dir}/{args.name}/")
    print(f"  Excel file   → {manifest['excel_workbook']}")
    print(f"  Manifest     → {manifest['manifest_file']}")


if __name__ == "__main__":
    main()
