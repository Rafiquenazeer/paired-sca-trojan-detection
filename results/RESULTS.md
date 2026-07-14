# Experimental Results — s38417

All results below are on the **s38417** benchmark (Xilinx LUT/FDRE netlist
translated to `.bench`; 1,559 primary inputs, 1,633 primary outputs, 43,001
gates, 1,527 scan flip-flops), placed and routed in Vivado for the
physical-region experiments.

PDF reports with the full write-ups are in this folder:
- `Physical_FPGA_Regions_Results.pdf` — full results report
- `Experimental_Setup.pdf` — configuration used

---

## 1. Test-set quality — Side-Channel Sensitivity (SCS / MaxRelSw)

Higher SCS means the test set drives a hidden trigger harder (more visible in
a side-channel trace). Each MERS variant is compared against an equal-size
random baseline over a population of 100 Trojans per trigger width.

**4-trigger Trojans**

| Method | SCS (MaxRelSw) | vs Random |
|--------|----------------|-----------|
| Random | 0.000664 | baseline |
| MERS   | 0.001073 | 1.62x |
| MERS-h | 0.002150 | 3.24x |
| **MERS-s** | **0.002224** | **3.35x** |

**8-trigger Trojans**

| Method | SCS (MaxRelSw) | vs Random |
|--------|----------------|-----------|
| Random | 0.000866 | baseline |
| MERS   | 0.001542 | 1.78x |
| MERS-h | 0.003519 | 4.06x |
| **MERS-s** | **0.003587** | **4.14x** |

Consistent ordering **MERS-s > MERS-h > MERS > Random**; the gain grows with
trigger count. MERS-s was used as Dataset B in the differential experiment.

---

## 2. Physical FPGA region formation (radius = 6 slices)

Clustering the 302 detected rare nodes by slice-placement proximity produced
**18 physical regions** (no singletons). Five densest:

| Region | Rare nodes | Footprint (slices) | Density (rare/area) |
|--------|-----------|--------------------|---------------------|
| 0 | 40 | 30  | 1.333 |
| 1 | 40 | 49  | 0.816 |
| 2 | 36 | 104 | 0.346 |
| 3 | 35 | 120 | 0.292 |
| 4 | 35 | 132 | 0.265 |

Targeting the **top-5 regions** selects **186 of 302 rare nodes (38%
reduction)** in the set MERS must excite, focused on the densest physical
pockets. The clustering radius of 6 slices was chosen from a sweep as the
knee of the singleton-count curve for this placement.

**Radius sweep (used to choose radius = 6):**

| radius | #regions | singletons | largest | top-5 sizes |
|--------|----------|-----------|---------|-------------|
| 2 | 60 | 20 | 25 | 25, 21, 18, 17, 15 |
| 3 | 36 | 6  | 40 | 40, 38, 21, 20, 20 |
| 4 | 29 | 5  | 40 | 40, 40, 28, 24, 21 |
| 5 | 21 | 2  | 40 | 40, 40, 31, 27, 22 |
| **6** | **18** | **1** | **40** | **40, 40, 36, 35, 35** |
| 8 | 15 | 1  | 40 | 40, 40, 40, 35, 35 |

---

## 3. Differential A-vs-B result (physical regions, radius 6)

Dataset B = MERS-s; Dataset A = paired control. 900 vectors each (899
transitions).

### Whole-cone matched-control table

| Metric | Dataset A (mean ± std) | Dataset B (mean ± std) | Note |
|--------|------------------------|------------------------|------|
| RareConeSwitch    | 249.995 ± 68.352   | 523.670 ± 108.195  | A substantially lower (~52%) |
| BackgroundSwitch  | 2969.025 ± 625.407 | 3029.167 ± 644.782 | matched |
| Input HD  | 755.934 ± 18.767  | 755.934 ± 18.767  | exact |
| Input HW  | 779.042 ± 16.530  | 781.408 ± 20.923  | closely matched |
| Output HD | 556.934 ± 245.006 | 556.916 ± 245.032 | near-exact |
| Output HW | 598.096 ± 265.432 | 599.106 ± 264.493 | closely matched |

The suspect cone is suppressed while background switching and input/output
Hamming profiles stay matched — the paired-control signature that makes the
difference attributable to the suspect logic.

### Aggregate trigger switching (the 4 actual trigger nodes)

| Metric | Dataset A | Dataset B | Interpretation |
|--------|-----------|-----------|----------------|
| Valid trigger-transition pairs | 899 | 899 | same transition window |
| Trigger-cone switches / pair   | 0.0200 | 0.4828 | A is ~4% of B |
| Any-trigger-switch rate        | 2.00%  | 41.71% | strong suppression |

**Dataset A reduces switching on the actual trigger nodes by ~96% relative to
Dataset B**, while the whole-cone view (diluted by many non-trigger cone
nodes) shows ~52%. Both are the same effect measured on different node sets:
the whole-cone table proves A is a matched control; the trigger table proves
A specifically suppresses the trigger.

---

## Reproducing

```bash
# 1. Dataset B (MERS-s) on the top-5 physical regions
python run_mers.py --bench benchmarks/s38417.bench --name s38417_AB \
    --N 100 --vectors 1000 --rare-vectors 100000 \
    --trojans 100 --triggers 4 8 --mutation-mode fast --mutation-rounds 12 \
    --use-regions --region-top-k 5 --placement placement.csv --phys-radius 6

# 2. Dataset A (paired control) — auto-reuses the same region selection
python generate_dataset_a.py --bench benchmarks/s38417.bench --name s38417_AB \
    --N 100 --mutation-mode fast --mutation-rounds 12 --C 5 \
    --B-method mers_s --dynamic-weights

# 3. Differential on the trigger nodes
python analyze_ab_trigger_switching.py --bench benchmarks/s38417.bench \
    --pi-order mers_testvectors/s38417_AB/primary_input_order.txt \
    --trigger-spec "Q_i_2__292_n_0:1,Q_i_2__363_n_0:1,Q_i_4__103_n_0:1,Q_i_6__5_n_0:0" \
    --vectors-a mers_testvectors/s38417_AB/s38417_AB_Dataset-A_vectors.txt \
    --vectors-b mers_testvectors/s38417_AB/s38417_AB_Dataset-B_vectors.txt
```

`placement.csv` is exported from Vivado using `export_placement.tcl`. Absolute
numbers vary slightly with RNG seed and Trojan sampling; the trends
(MERS-s strongest, ~96% trigger suppression with matched background) are
stable.
