# MERS-Based Side-Channel Hardware-Trojan Detection with Physical FPGA Region Targeting

A Python toolchain for statistical test generation and differential
side-channel analysis for hardware-Trojan detection, based on **MERS**
(Multiple Excitation of Rare Switching), extended with:

- a **paired control test set (Dataset A)** for differential side-channel
  analysis, and
- **physical FPGA region targeting** that focuses test generation on the
  densest pockets of rare nodes on the placed device.

## Overview

Given a gate-level circuit (ISCAS `.bench`, including Xilinx LUT/FDRE netlists
translated to `.bench`), the toolchain:

1. **Detects rare nodes** by Monte-Carlo simulation (the low-controllability
   nets a Trojan trigger is typically built from).
2. **Generates Dataset B** using MERS / MERS-h / MERS-s, which maximises
   switching of the rare/trigger logic (high side-channel sensitivity).
3. **Generates Dataset A**, a paired control set that *minimises* switching of
   a suspect cone while *matching* Dataset B per-pair on background switching
   and input/output Hamming distance and weight.
4. **Targets regions** — optionally restricts test generation to the rare
   nodes in the top-K densest **logical** (fan-in cone) or **physical**
   (Vivado-placement) regions.
5. **Evaluates and reports** — scores test sets against a Trojan population
   (Side-Channel Sensitivity) and exports Excel/PDF reports.

The differential principle: applying Dataset A and Dataset B to the same device
and subtracting the side-channel traces cancels the common background activity
and leaves a signal dominated by the Trojan trigger.

## Installation

```bash
pip install -r requirements.txt
```

Requires Python 3.8+.

## Quick start

```bash
# 1. Generate Dataset B (MERS/MERS-h/MERS-s) on a benchmark
python run_mers.py --bench benchmarks/s15850.bench --name demo \
    --N 100 --vectors 1000 --rare-vectors 10000 \
    --trojans 100 --triggers 4 8 --mutation-mode fast --mutation-rounds 12

# 2. Generate the paired control Dataset A
python generate_dataset_a.py --bench benchmarks/s15850.bench --name demo \
    --N 100 --mutation-mode fast --mutation-rounds 12 --C 5 \
    --B-method mers_s --dynamic-weights

# 3. Measure the A-vs-B differential on the trigger nodes
python analyze_ab_trigger_switching.py --bench benchmarks/s15850.bench \
    --pi-order mers_testvectors/demo/primary_input_order.txt \
    --trigger-spec "nodeA:1,nodeB:1,nodeC:1,nodeD:0" \
    --vectors-a mers_testvectors/demo/demo_Dataset-A_vectors.txt \
    --vectors-b mers_testvectors/demo/demo_Dataset-B_vectors.txt
```

## Region targeting (logical or physical)

```bash
# inspect regions and pick top-K / radius before committing
python analyze_regions.py --bench benchmarks/s38417.bench --region-top-k 8

# physical FPGA regions from Vivado placement (see export_placement.tcl)
python analyze_regions.py --bench benchmarks/s38417.bench \
    --placement placement.csv --phys-radius 6 --region-top-k 5

# run the full pipeline on the top physical regions
python run_mers.py --bench benchmarks/s38417.bench --name s38417_AB \
    --use-regions --region-top-k 5 --placement placement.csv ...
```

`export_placement.tcl` is the Vivado Tcl snippet that exports per-cell slice
coordinates to `placement.csv`. `sweep_radius.py` and `inspect_radius.py` help
choose the clustering radius from the placement.

## Repository layout

```
run_mers.py                  Dataset B (MERS) pipeline entry point
generate_dataset_a.py        Dataset A (paired control) generator
analyze_regions.py           Region inspector + Excel report
analyze_ab_trigger_switching.py   A-vs-B differential on trigger nodes
sweep_radius.py, inspect_radius.py   Physical-region radius selection helpers
export_placement.tcl         Vivado placement export snippet
requirements.txt

src/
  circuit_parser.py          .bench parser + levelized vectorized simulator
  rare_node_finder.py        Monte-Carlo rare-node detection
  mers_algo.py               MERS / MERS-h / MERS-s test generation
  mero_algo.py               MERO baseline
  dataset_a_generator.py     paired-control generation (3-phase solver)
  dataset_a_reporter.py      A-vs-B comparison report
  rare_node_regions.py       logical (fan-in cone) regions
  placement_regions.py       physical (FPGA placement) regions
  evaluator.py               Side-Channel Sensitivity scoring
  excel_reporter.py          Excel reports (rare nodes, regions, comparison)
  progress_manager.py        checkpoint/resume
  testset_exporter.py        vector-file export

benchmarks/                  sample .bench circuits (c17, s15850, ...)
docs (*.md)                  DATASET_A_README, REGIONS_README, GUIDE, ...
```

## Results

Experimental results on the **s38417** benchmark are summarised in
[`results/RESULTS.md`](results/RESULTS.md), covering:

- **Side-channel sensitivity (SCS)** of each MERS variant vs a random
  baseline (MERS-s reaches ~3.3x random on 4-trigger and ~4.1x on 8-trigger
  Trojans);
- **Physical FPGA region formation** at radius 6 (18 regions; top-5 = 186 of
  302 rare nodes, a 38% reduction) with the radius-selection sweep;
- **Differential A-vs-B** results: the paired control suppresses switching on
  the actual trigger nodes by ~96% while background and input/output Hamming
  profiles stay matched.

Full PDF reports are in [`results/`](results/).

## Documentation

- `GUIDE.md` — end-to-end usage guide
- `DATASET_A_README.md` — paired-control algorithm and options
- `REGIONS_README.md` — logical and physical region targeting

## References

- H. Salmani, M. Tehranipoor, and R. Karri, *MERS: Statistical Test Generation
  for Side-Channel Analysis based Trojan Detection*, and the ISCAS'89
  benchmark suite.
- M. Rabozzi et al., *Floorplanning Automation for Partial-Reconfigurable FPGAs
  via Feasible Placements Generation*, IEEE TVLSI, 2017 (physical-region
  formalisation inspiration).

## License

Released under the MIT License — see `LICENSE`.
