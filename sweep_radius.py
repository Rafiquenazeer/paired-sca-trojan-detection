"""Sweep --phys-radius and report region-size distribution, so you can pick
the value that gives a useful number of compact regions (not one blob, not
all singletons). Run:  python sweep_radius.py placement.csv [--name NAME]"""
import sys, argparse, logging
sys.path.insert(0, '.')
logging.basicConfig(level=logging.ERROR)
import numpy as np
from src.placement_regions import (load_placement, match_rare_to_coords,
                                    build_physical_regions)

ap = argparse.ArgumentParser()
ap.add_argument("placement")
ap.add_argument("--name", default=None,
                help="reuse rare_nodes checkpoint under mers_progress/<name>")
ap.add_argument("--bench", default=None,
                help="bench to detect rare nodes if no --name checkpoint")
ap.add_argument("--radii", default="2,3,4,5,6,8",
                help="comma list of radii to try")
ap.add_argument("--max-size", type=int, default=40)
a = ap.parse_args()

coords = load_placement(a.placement)

# Get rare nodes
rare = None
if a.name:
    from src.progress_manager import ProgressManager
    pm = ProgressManager("mers_progress", a.name)
    keys = [k for k in pm.list_checkpoints() if k.startswith("rare_nodes_v6")]
    if keys:
        rare = pm.load(keys[0])["rare_nodes"]
        print(f"Rare nodes from checkpoint '{keys[0]}': {len(rare)}")
if rare is None and a.bench:
    from src.circuit_parser import Circuit
    from src.rare_node_finder import RareNodeFinder
    c = Circuit(); c.parse_bench(a.bench, full_scan=True)
    rare = RareNodeFinder(c, rare_threshold=0.1, num_vectors=2000,
                          seed=42).find_rare_nodes()["rare_nodes"]
    print(f"Rare nodes (freshly detected): {len(rare)}")
if rare is None:
    sys.exit("Provide --name (checkpoint) or --bench (to detect rare nodes).")

located, missing = match_rare_to_coords(rare, coords)
print(f"Placed rare nodes: {len(located)}/{len(rare)} "
      f"({100*len(located)/len(rare):.0f}%)\n")

print(f"{'radius':>6} {'#regions':>9} {'singletons':>11} {'largest':>8} "
      f"{'top5 sizes':>20}")
print("-" * 60)
for r in [float(x) for x in a.radii.split(",")]:
    rr = build_physical_regions(rare, located, radius=r,
                                mode='compact', max_region_size=a.max_size)
    regs = rr["regions"]
    sizes = [x["size"] for x in regs]
    singles = sum(1 for s in sizes if s == 1)
    big = "[" + ",".join(str(s) for s in sizes[:5]) + "]"
    print(f"{r:>6.1f} {len(regs):>9} {singles:>11} "
          f"{(max(sizes) if sizes else 0):>8} {big:>20}")
print("\nPick a radius where the top regions have a useful handful of rare")
print("nodes (say 5-25) and you're not collapsing everything into one blob")
print("or leaving everything as singletons.")
