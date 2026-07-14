# MERS: Complete Step-by-Step Implementation Guide
## Multiple Excitation of Rare Switching for Hardware Trojan Detection
### Based on: Huang, Bhunia & Mishra — CCS 2016

---

## ▶ Quick Reference Card

```
python download_benchmarks.py          # Step 1: setup & instructions
python run_mers.py --bench benchmarks/demo_rare.bench --name demo_rare \
                   --N 100 --vectors 1000 --trojans 50 --triggers 4   # Step 2: run
python run_mers.py --bench benchmarks/c2670.bench --name c2670 \
                   --status-only                                        # Check progress
```

---

## Part 1 — Trust-Hub Benchmark Recommendation

### Best Benchmark for Power SCA + MERS: **AES-T200** (starter) or **AES-T1300** (publication)

| Benchmark     | Triggers      | Why Good for MERS + Power SCA                            |
|---------------|---------------|----------------------------------------------------------|
| **AES-T200**  | Combinational | Rare data-bus condition → perfect rare-node coverage     |
| **AES-T1300** | Sequential    | Most cited in 2022–2025 power SCA literature             |
| **AES-T400**  | Counter-based | Tests sequential Trojan path in MERS                     |
| **s38417-T**  | Various       | Largest ISCAS-89 circuit → best for scalability section  |

**Why AES-T over ISCAS for Power SCA?**
- AES encryption has deterministic, high-amplitude switching → clear power traces
- Trojan trigger conditions are built around rare key/data byte combinations → exactly what MERS optimises
- IEEE DataPort has pre-measured power datasets for AES-T benchmarks (save months of lab time)
- Most reviewers expect AES-T benchmarks in a Power SCA paper

**For matching the original MERS paper exactly:** Use ISCAS-85 circuits  
(c2670, c3540, c5315, c6288, c7552) + ISCAS-89 (s13207, s15850, s35932)

---

## Part 2 — Installation (One Time Only)

### Requirements
- Python 3.8 or newer
- ~100 MB disk space per large benchmark
- Windows / Linux / macOS all work

### Install Python packages
```bash
pip install numpy pandas openpyxl tqdm matplotlib
```

### Project structure (already created)
```
mers_project/
├── run_mers.py              ← main entry point (run this)
├── download_benchmarks.py   ← setup helper (run first)
├── requirements.txt
├── src/
│   ├── circuit_parser.py    ← .bench parser + logic simulator
│   ├── rare_node_finder.py  ← Monte-Carlo rare node detection
│   ├── mers_algo.py         ← Algorithms 1, 2, 3
│   ├── evaluator.py         ← Trojan insertion + switching metrics
│   ├── excel_reporter.py    ← Excel workbook generation
│   ├── progress_manager.py  ← checkpoint save/load system
│   └── pipeline.py          ← orchestrates all stages
├── benchmarks/              ← put .bench files here
│   ├── c17.bench            ← included automatically
│   └── demo_rare.bench      ← included automatically
├── mers_progress/           ← auto-created: checkpoints per circuit
│   └── c2670/
│       ├── rare_nodes.pkl
│       ├── mers_testset_N1000.pkl
│       └── ...
└── mers_results/            ← auto-created: Excel outputs
    ├── c2670_rare_nodes.xlsx
    └── c2670_8trig_sensitivity.xlsx
```

---

## Part 3 — Get Benchmark Files

### Step 3a: ISCAS Benchmarks (FREE, no account needed)

Download the circuits used in the paper from one of these sources:

**Option 1 — GitHub (easiest)**
```
https://github.com/cad-polito-it/benchmarks/tree/master/ISCAS85
```
Download: c2670.bench, c3540.bench, c5315.bench, c6288.bench, c7552.bench  
And ISCAS-89: s13207.bench, s15850.bench, s35932.bench  
→ Place all .bench files in your `benchmarks/` folder.

**Option 2 — NCSU benchmark collection**
```
https://people.engr.ncsu.edu/brglez/CBL/software/ISCAS85/
```

### Step 3b: Trust-Hub AES Benchmarks (free account required)

1. Register at https://trust-hub.org (free)
2. Go to Benchmarks → Chip-Level Trojan
3. Download **AES-T200** (Verilog, ~50 KB)
4. **Convert Verilog → .bench** using Yosys (see Part 5 below)

---

## Part 4 — Running MERS Step by Step

### Stage 1: Verify setup (always runs instantly)
```bash
python run_mers.py --bench benchmarks/demo_rare.bench \
                   --name demo_rare_test \
                   --N 50 --vectors 500 --trojans 20 --triggers 4 \
                   --skip-mers-s
```
Expected output: `✓ Pipeline complete` with Excel files in `mers_results/`

---

### Stage 2: Full paper replication (c2670 — ~3 hours on a laptop)
```bash
python run_mers.py \
  --bench      benchmarks/c2670.bench \
  --name       c2670 \
  --N          1000 \
  --threshold  0.1 \
  --vectors    10000 \
  --trojans    1000 \
  --triggers   4 8 \
  --C          5
```

**If interrupted** → just run the same command again.  
All completed stages are loaded from disk; only unfinished stages run.

---

### Stage 3: Check what's done
```bash
python run_mers.py --bench benchmarks/c2670.bench --name c2670 --status-only
```
Output shows every checkpoint: ✓ (done), ✗ (missing/deleted)

---

### Stage 4: Re-do a specific stage
```bash
# Example: re-run rare node finding with a different threshold
python run_mers.py --bench benchmarks/c2670.bench --name c2670 \
                   --delete-stage rare_nodes

python run_mers.py --bench benchmarks/c2670.bench --name c2670 \
                   --N 1000 --threshold 0.05   # now uses 5% threshold
```

---

### Stage 5: Run on Trust-Hub AES (after Verilog conversion)
```bash
python run_mers.py \
  --bench      benchmarks/AES-T200.bench \
  --name       AES-T200 \
  --N          1000 \
  --threshold  0.1 \
  --vectors    10000 \
  --trojans    1000 \
  --triggers   4 8
```

---

## Part 5 — Convert Trust-Hub Verilog to .bench

### Using Yosys (FREE, open-source)

**Install Yosys**
- Linux:   `sudo apt install yosys`
- Windows: Download from https://github.com/YosysHQ/yosys/releases
- Mac:     `brew install yosys`

**Convert AES-T200.v → AES-T200.bench**
```tcl
# Open Yosys (type "yosys" in terminal)
yosys

# Inside Yosys shell:
read_verilog AES-T200.v
hierarchy -check -top aes_T200      # replace with actual top module name
proc; opt; flatten
synth -flatten
write_bench -noattr benchmarks/AES-T200.bench
exit
```

**Find the top module name:**
```bash
grep -i "^module" AES-T200.v
```

---

## Part 6 — Understanding the Output

### Excel File 1: `<circuit>_rare_nodes.xlsx`

| Sheet              | Contents                                                    |
|--------------------|-------------------------------------------------------------|
| **Rare Nodes**     | All rare nodes sorted by probability, colour-coded by rarity |
| **All Node Probs** | P(node=1) for every node; rare nodes highlighted yellow     |
| **Summary**        | Circuit statistics (gates, PIs, POs, rare counts)           |
| **Probability Chart** | Bar chart of the 50 rarest nodes                        |

**Colour key in Rare Nodes sheet:**
- 🔴 Red   → P(rare value) ≤ 5%   (extremely rare, high Trojan risk)
- 🟠 Orange → P(rare value) ≤ 10%  (rare, typical Trojan trigger)
- 🟡 Yellow → P(rare value) ≤ threshold (barely rare)

### Excel File 2: `<circuit>_<N>trig_sensitivity.xlsx`

| Sheet                   | Contents                                           |
|-------------------------|----------------------------------------------------|
| **Side Channel Sensitivity** | SCS (MaxRelativeSwitch) comparison per method |
| **Delta Switch**        | MaxDeltaSwitch and AvgDeltaSwitch per method       |
| **Chart**               | Bar chart comparing all four methods               |

**Key metrics (Section 5.2 of paper):**
- `SCS = avg_max_relative_switch`  — THE primary metric (higher = better)
- `avg_max_delta_switch`           — absolute switching amplitude
- `testset_size`                   — smaller = more efficient

---

## Part 7 — Parameter Tuning Guide

### N (rare-switching requirement, Algorithm 1)
| N value | Testset size      | Quality    | Runtime      |
|---------|-------------------|------------|--------------|
| 10      | ~100–500 vectors  | Low        | Seconds      |
| 100     | ~500–2000 vectors | Medium     | Minutes      |
| 1000    | ~5K–15K vectors   | High (paper)| 1–10 hours  |
| 10000   | ~50K+ vectors     | Very high  | Days         |

**Recommendation:** Start with N=100 to verify your setup, then N=1000 for publication.

### --threshold (rare node selection)
- **0.1** (default, paper value) — use for standard ISCAS circuits
- **0.05** — if circuit has few rare nodes at 10%
- **0.2** — for very small circuits (c17, toy examples)

### --C (MERS-s weight ratio, Algorithm 3)
- **5** (paper default) — good balance
- **1–2** — maximises suppression of background switching
- **10–50** — approaches MERS without reordering
- The paper shows C=5 is optimal (Figure 6)

### --triggers (Trojan trigger count for evaluation)
- **4** → evaluates 4-trigger Trojans (Table 4 of paper)
- **8** → evaluates 8-trigger Trojans (Table 5 of paper)
- **--triggers 2 4 6 8** → run all four types

---

## Part 8 — Extending to Real Power SCA Measurements

Once MERS generates the testset, to use it with real power traces:

### 1. Program the testset into your test equipment
```python
# Export testset as CSV for FPGA/ASIC test equipment
import pandas as pd, pickle

with open("mers_progress/AES-T200/mers_testset_N1000.pkl", "rb") as f:
    result = pickle.load(f)

testset = result['testset']
df = pd.DataFrame(testset)
df.to_csv("mers_testset_for_measurement.csv", index=False, header=False)
```

### 2. Apply testset to DUT and capture power traces
- Use oscilloscope / ChipWhisperer / Langer probes
- Measure transient current between each consecutive vector pair (prev → curr)
- Record: `TotalSwitch_measured`, `DeltaSwitch_measured` (difference from golden)

### 3. Compute RelativeSwitch from measurements
```python
relative_switch = delta_current / total_current
# If relative_switch > threshold → Trojan suspected
```

### 4. Process calibration (Section 5.8 of paper)
- Use golden chips at process corners to establish threshold
- MERS improves SNR so threshold detection is more reliable

---

## Part 9 — Troubleshooting

| Problem                          | Fix                                                          |
|----------------------------------|--------------------------------------------------------------|
| "No rare nodes found"            | Lower `--threshold` (try 0.2 or 0.3)                        |
| Pipeline killed / system restart | Just re-run same command — checkpoints load automatically    |
| Out of memory on large circuit   | Use `--skip-mers-s` (MERS-s holds all testset in RAM)       |
| MERS takes forever               | Lower N to 100–200; skip-mers-s; use fewer vectors           |
| .bench parse errors              | Check file uses `=` assignments, not `;` semicolons          |
| Verilog won't parse in Yosys     | Try `read_verilog -sv` for SystemVerilog syntax              |
| All SCS values equal             | Need more rare nodes; try smaller trigger count (2 instead of 8)|

---

## Part 10 — Expected Results (matching Table 4 & 5 of paper)

For c2670 with N=1000 (8-trigger Trojans):

| Method   | Avg MaxRelSw (SCS) | vs Random | vs MERO  |
|----------|--------------------|-----------|----------|
| Random   | 0.02469            | —         | —        |
| MERO     | 0.03204            | +29.8%    | —        |
| MERS     | 0.03108            | +25.9%    | -3.0%    |
| **MERS-h**| **0.03729**       | **+51.1%**| **+16.4%**|
| **MERS-s**| **0.03984**       | **+61.4%**| **+24.4%**|

*Your Python results will be directionally correct but may differ slightly from 
the paper (which used C simulation and different Trojan sampling).  
The key trend — MERS-h and MERS-s outperform both Random and MERO — should hold.*

---

*MERS Implementation | Huang, Bhunia & Mishra, CCS 2016 | Python Implementation Guide*

## Test vector exports

This version automatically exports all generated testsets in both text and Excel form. After a run, check:

```text
mers_testvectors/<run_name>/
```

The `.txt` files are plain binary vectors, one vector per line. The Excel workbook contains compact sheets with index, bitstring, hex, and width. Always use `primary_input_order.txt` to map vector bit positions to circuit primary inputs / scan inputs.

Use `--no-export-vectors` only when you want to skip these files.

---

## V7 note for Vivado/Xilinx LUT-expanded BENCH files

For BENCH files generated from Vivado/Xilinx LUT/FDRE netlists, the project now excludes synthetic converter helper nodes from rare-node selection by default. These helper nodes normally start with `__`, for example `__LUTTERM_*`, `__NOT_*`, and `__CONST*`.

They are still simulated internally, but they are not used as MERS/MERO rare-node targets. This prevents inflated rare-node counts and very slow runs on FPGA LUT netlists such as `s38417_T1.bench`.

Default behavior:

```bat
python run_mers.py --bench benchmarks/s38417_T1.bench --name s38417_T1 --N 100 --vectors 1000 --trojans 100 --triggers 4 8
```

Debug-only behavior to include helper nodes again:

```bat
python run_mers.py --bench benchmarks/s38417_T1.bench --name s38417_T1 --include-artifact-nodes
```
