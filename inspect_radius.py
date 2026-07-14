# Quick helper: report the coordinate ranges in YOUR placement.csv so you can
# pick a sensible --phys-radius. Run: python /tmp/inspect_radius.py placement.csv
import sys
sys.path.insert(0,'.')
from src.placement_regions import load_placement
import numpy as np

path = sys.argv[1] if len(sys.argv) > 1 else "placement.csv"
coords = load_placement(path)
xs = np.array([c[0] for c in coords.values()])
ys = np.array([c[1] for c in coords.values()])
print(f"\nCells placed: {len(coords)}")
print(f"  X range: {xs.min():.0f} .. {xs.max():.0f}   (span {np.ptp(xs):.0f} slices)")
print(f"  Y range: {ys.min():.0f} .. {ys.max():.0f}   (span {np.ptp(ys):.0f} slices)")
diag = float(np.hypot(np.ptp(xs), np.ptp(ys)))
print(f"  Full-die diagonal: {diag:.0f} slices")
print(f"\nRule of thumb for --phys-radius:")
print(f"  tight  pockets : {max(2, diag*0.01):.0f}  (~1% of die)")
print(f"  medium pockets : {max(3, diag*0.03):.0f}  (~3% of die)")
print(f"  loose  pockets : {max(5, diag*0.06):.0f}  (~6% of die)")
print(f"\nStart around the 'medium' value, then check the region sizes in the")
print(f"output: if you get one big blob, lower it; if all singletons, raise it.")
