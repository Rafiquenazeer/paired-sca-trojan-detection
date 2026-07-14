# Rare-node region targeting (focus MERS on likely Trojan spots)

## Idea

MERS normally excites **all** rare nodes, which is heavy and time-consuming
(e.g. 274 rare nodes on s38417). But an attacker can only hide a Trojan
trigger where several rare nodes are **structurally close enough to feed one
small gate** -- a dense pocket of rare nodes whose fan-in cones overlap. Those
dense pockets are both the most probable Trojan locations and a far smaller
set to target.

Region targeting groups rare nodes into such pockets ("regions"), ranks them
by density, and lets MERS (Dataset B) and the Dataset-A cone focus on the
top-K densest regions instead of every rare node.

## How a region is defined

`src/rare_node_regions.py` defines a region by **structural proximity via
shared fan-in cones** -- the right notion for this threat model, because rare
nodes an attacker could combine into one trigger are exactly the rare nodes
whose cones converge:

1. For each rare node, compute its depth-d fan-in cone (the gates that drive
   it).
2. Two rare nodes are linked if their cones overlap (Jaccard >=
   `--region-overlap`) or one lies in the other's cone.
3. Two clustering modes:
   * **compact** (default, recommended): grow size-capped, tight clusters
     around the densest rare-node seeds (`--region-max-size`, default 40).
     Produces many small dense pockets -- the realistic "wire these into one
     trigger" unit.
   * **components**: connected components of the overlap graph. Fewer, larger
     regions; a coarse "where is rare logic concentrated" map. (On real
     circuits this tends to merge a large fraction of rare nodes into one
     component because they share a logic backbone, which is why compact is
     the default.)
4. Regions are ranked by rare-node count (density). The **top-K densest** are
   the recommended targeting set.

## Step 0: inspect regions before committing (optional)

```bash
python analyze_regions.py --bench benchmarks/s38417.bench --threshold 0.1 \
    --region-mode compact --region-top-k 8
```

By default this **detects rare nodes with 100,000 Monte-Carlo vectors** before
building regions, so the rare-node set is accurate (a thin sample misclassifies
borderline nodes as stuck/rare). Override with `--rare-vectors N` (alias
`--vectors N`). If you pass `--name <run>`, it instead **reuses the rare-node
checkpoint** that `run_mers.py` already saved for that run (so the same
`--rare-vectors` you used there applies, and no re-detection happens):

```bash
# reuse the rare nodes already detected by a prior run_mers.py --name s38417_AB
python analyze_regions.py --bench benchmarks/s38417.bench --name s38417_AB \
    --region-top-k 8
```

It prints each region's size, cone-gate count, and density, plus a coverage
table:

```
rank  rare   cone  density  sample rare nodes
   0    40     79    0.506   n2281, n2294, n2302
   1    40    229    0.175   n2140, n2145, n2303
   ...
  top-1:   40 rare nodes (  8% of all)  => MERS targets 92% fewer nodes
  top-3:  119 rare nodes ( 23% of all)  => MERS targets 77% fewer nodes
```

Pick `--region-top-k` so the union covers the pocket(s) you want to probe.
Higher density = rare nodes per cone gate = tighter, more probable hiding spot.

`analyze_regions.py` also writes an **Excel report**
(`mers_results/<name>_rare_node_regions.xlsx`) with:
* **Summary** — run parameters, a top-K coverage table (rare nodes covered /
  % of all / MERS reduction), the full per-region overview (size, cone gates,
  density, sample nodes), and a bar chart of region sizes;
* **All Rare Nodes** — every rare node with its probability, rare value, and
  assigned region id;
* **Region &lt;id&gt;** — one sheet per top region listing that region's rare
  nodes.

Pass `--no-excel` to skip it, `--results-dir DIR` to change where it goes, or
`--region-sheets N` to control how many per-region detail sheets are written.

## Step 1: Dataset B on the top regions

```bash
python run_mers.py --bench benchmarks/s38417.bench --name s38417_AB \
    --N 100 --vectors 10000 --trojans 100 --triggers 4 8 \
    --mutation-mode fast --mutation-rounds 12 --skip-mero \
    --use-regions --region-top-k 2
```

MERS now excites only the rare nodes in the top-K densest regions. Selecting
a small number of regions typically reduces the targeted rare-node set by a
large fraction -- proportionally faster generation, focused on the densest
pockets. The selection is saved to `mers_progress/<name>/region_selection.pkl`.

## Step 2: Dataset A aligned to the same regions

```bash
python generate_dataset_a.py --bench benchmarks/s38417.bench --name s38417_AB \
    --N 100 --mutation-mode fast --mutation-rounds 12 --C 5 --B-method mers_s \
    --cone-fanin-depth 1 --cone-fanout-depth 1 --dynamic-weights
```

**If Dataset B was built with `--use-regions`, Dataset A reuses that exact
region selection AUTOMATICALLY** — you do not need to repeat `--use-regions`.
On start-up Dataset A checks for the `region_selection` checkpoint that
run_mers.py saved; if present it seeds the cone from only those region rare
nodes, so A and B always target the identical set. The log shows:

```
Using saved region selection: 34 rare nodes from region(s) [0, 1] (top-2).
   [auto: Dataset B used regions]
Suspicious-cone seeds: 34 node(s) from saved region selection [auto: ...].
```

Passing `--use-regions` explicitly still works (and forces a recompute if no
checkpoint exists). To deliberately ignore B's region selection and suppress
**all** rare nodes instead, pass `--all-rare`.

This guarantees the rule: *MERS generates vectors only for the rare nodes in
the selected regions, and Dataset A uses only those same rare nodes.*

## Options

Shared by `run_mers.py`, `generate_dataset_a.py`, and `analyze_regions.py`:

| Flag | Meaning | Default |
|---|---|---|
| `--use-regions` | enable region targeting (B); force/recompute on A | off |
| `--all-rare` | (Dataset A) ignore B's region selection, use all rare nodes | off |
| `--region-top-k` | number of densest regions to target | 1 |
| `--region-mode` | `compact` or `components` | compact |
| `--region-fanin-depth` | cone depth defining proximity | 3 |
| `--region-overlap` | Jaccard overlap to link two rare nodes | 0.30 |
| `--region-max-size` | max rare nodes per region (compact) | 40 |

## Notes

* Region targeting is **opt-in** on Dataset B (`--use-regions`). Once B used
  it, Dataset A follows automatically so the two never drift apart. Without
  regions anywhere, everything behaves as before (all rare nodes).
* Because B and A both target the same region(s), their RareConeSwitch
  comparison and the trigger-switching differential are measured on the same
  focused node set -- the A-vs-B contrast stays valid.
* If you already know the actual Trojan trigger nodes, `--cone-seeds
  "nodeA,nodeB,..."` (on `generate_dataset_a.py`) still overrides everything
  and targets those exact nodes; region targeting is for the realistic case
  where you don't yet know where the Trojan is and want to probe the most
  likely pockets.

---

## Physical (FPGA placement-aware) regions — for side-channel analysis

The logical regions above group rare nodes by **shared fan-in cone**
(proximity in the Boolean netlist). For side-channel analysis on a real FPGA,
what matters is **physical proximity on the die**: rare logic placed in nearby
slices shares a power/EM signature, so a Trojan hidden among physically
clustered rare logic is what a differential SCA can localise. A `.bench`
netlist contains no placement, so logical regions and the FPGA's physical
regions are two different partitions and generally will not match. (On s38417,
four known trigger nodes scatter across four *logical* regions because they
combine flip-flops that are logically far apart — a stealthy trigger.)

To cluster by physical placement, export coordinates from Vivado and pass
`--placement`.

### Step A — export placement from Vivado

After `place_design` (or with a routed `.dcp` open), in the Tcl console:

```tcl
source export_placement.tcl     ;# writes placement.csv  (cell,x,y)
```

`export_placement.tcl` (included) reads each leaf cell's SLICE column/row.
Accepted row formats:

```
cell,x,y                              # CSV with header
design_1/Q_i_2__292_n_0_reg,12,34
Q_i_6__5_n_0  SLICE_X11Y33            # whitespace + site string
Q_i_4__103_n_0  X80Y90
```

Cell names are normalised (hierarchy prefixes and `_reg` suffixes stripped)
so `.bench` names like `Q_i_2__292_n_0` match Vivado's
`design_1_i/u_core/Q_i_2__292_n_0_reg`. The match rate is reported.

### Step B — build physical regions

```bash
# inspect physical regions + Excel report
python analyze_regions.py --bench benchmarks/s38417.bench \
    --placement placement.csv --phys-radius 4 --region-top-k 8

# Dataset B on the densest PHYSICAL region(s)
python run_mers.py --bench benchmarks/s38417.bench --name s38417_AB \
    --N 100 --vectors 10000 --trojans 100 --triggers 4 8 \
    --mutation-mode fast --mutation-rounds 12 --skip-mero \
    --use-regions --region-top-k 2 --placement placement.csv

# Dataset A automatically reuses the same physical selection
python generate_dataset_a.py --bench benchmarks/s38417.bench --name s38417_AB \
    --N 100 --mutation-mode fast --mutation-rounds 12 --C 5 --B-method mers_s \
    --use-regions --dynamic-weights
```

`--phys-radius` sets the clustering distance in placement-grid units (two rare
nodes group if their slice-distance <= radius). In physical mode the
report's footprint column is the slice bounding-box area and density is rare
nodes per slice-area; each region also records a centroid and bounding box.

### Which mode to use

| | Logical regions | Physical regions (`--placement`) |
|---|---|---|
| Proximity by | shared fan-in cone | slice distance on die |
| Right for | logic-level trigger reasoning | **side-channel analysis** |
| Needs | only the `.bench` | Vivado placement export |

The differential A/B method itself is electrical and works on the FPGA
regardless of region mode (or none — naming the actual trigger nodes with
`--cone-seeds` always works). Region mode only changes the heuristic for
guessing *where* an unknown Trojan hides; for SCA on real silicon, physical
mode is the correct heuristic.
