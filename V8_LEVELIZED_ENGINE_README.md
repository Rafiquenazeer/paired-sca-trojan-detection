# V8 update: levelized vectorized simulation engine (for s38417-scale circuits)

## Why

`src/circuit_parser.py`'s `simulate_batch()`/`simulate()` (v4-v7) issued
**one NumPy call per gate**. For small ISCAS-85 circuits (hundreds to a
few thousand gates) this is fine. For large Xilinx LUT-expanded
benchmarks such as `s38417_T1.bench` (after LUT6 INIT expansion this
produces 10^4-10^5 internal nodes, including `__LUTTERM_*`/`__NOT_*`
helper nodes), per-gate NumPy call overhead dominates:

* `simulate()` (K=1, used by `--mutation-mode paper` and by MERS-s's
  `state_prev` update) costs roughly `gates * ~1us` -- tens of
  milliseconds per call, called **tens of millions of times**.
* `simulate_batch()` (K=n_pi, used by `--mutation-mode fast`) similarly
  pays `gates` NumPy calls every time.
* MERS-s additionally looped over every gate node and every rare node
  **per reordering step** to compute `TotalSwitch`/`RareSwitch` -- another
  `O(gates * testset_size)` Python-level loop.

## What changed

### 1. `src/circuit_parser.py` -- levelized, grouped vectorized engine

At parse time the circuit is **compiled once**:

* `level[node]` = 0 for PIs/constants, `1 + max(level[input])` for gates.
  Gates at the same level can never depend on each other.
* Gates are grouped by `(level, logic_class, invert, arity)` where
  `logic_class ∈ {AND, OR, XOR, BUF}` (NAND/NOR/XNOR/NOT/INV map to the
  base class plus an `invert` flag; unrecognised types fall back to BUF,
  matching the old pass-through fallback).
* `simulate_batch(vectors)` walks the levels in order; each group is
  evaluated with **one** gather -> `np.bitwise_and/or/xor.reduce` ->
  optional invert -> scatter, instead of one call per gate.
* `simulate(vector)` now returns a lightweight `NodeState` (a dict-like
  view with `.get()`, `in`, `[]`) instead of building a Python dict with
  one entry per node -- critical because `--mutation-mode paper` calls
  `simulate()` `n_pi` times per test vector.

This is a **pure performance change**: verified bit-identical to the old
per-gate implementation on `c17`, `demo_rare`, `s15850` (sequential, 513
DFFs, full-scan), and a synthetic LUT-artifact+DFF circuit exercising
`__CONST0`/`__CONST1`/`__LUTTERM_*`/`__NOT_*`/`Q=DFF(D)` together.

Measured on a 39,594-gate / 1,629-PI synthetic benchmark (s38417_T1
scale, compiles to 520 groups across 58 levels -- a 76x reduction in
NumPy calls):

| Operation                     | v7 (per-gate) | v8 (levelized) | Speedup |
|--------------------------------|--------------:|----------------:|--------:|
| `simulate()` (K=1)             | 40.1 ms/call  | 3.5 ms/call     | 11.5x   |
| `simulate_batch(K=n_pi=1629)`  | 0.64 s/call   | 0.38 s/call     | 1.7x    |

### 2. `src/mers_algo.py` -- vectorized MERS-s (Algorithm 3)

`MERSSim.reorder()` used to loop over every gate node and every rare node
individually each step (`O(gates)` NumPy calls per step). v8 stacks all
gate-output / rare-node columns into single `(N, K)` matrices with one
`np.stack()` and computes `TotalSwitch`/`RareSwitch` with 2-3 vectorised
ops total, **independent of gate count**. Verified bit-exact against the
old per-node loop (same `profits`, same `argmax`, same chosen vector,
every step).

On the synthetic 39,594-gate benchmark this brings MERS-s to ~60 ms/step
(~10 minutes for a 10,000-vector testset), down from an estimated ~30+
minutes of pure Python-loop overhead.

## Confirmed: works for sequential AND combinational circuits

* **Combinational** (`c17`, no DFFs): full pipeline runs end-to-end.
* **Sequential** (`s15850`, 513 DFFs, full-scan): `Q=DFF(D)` splits into
  scan PI (`Q`) / scan PO (`D`) exactly as before; full pipeline
  (parse -> rare nodes -> MERO -> MERS -> MERS-h -> MERS-s -> vector
  export -> evaluation) runs end-to-end with sensible results
  (MERS-s ≥ MERS-h ≥ MERS > Random for both 4- and 8-trigger Trojans).
* **LUT-expanded artifacts** (`__CONST0`/`__CONST1` via the
  `__CONST_DRIVER` AND/OR/NOT trick, `__LUTTERM_*`, `__NOT_*`,
  `Q=DFF(D)`): all simulate correctly and are excluded from rare-node
  candidacy by the existing `--artifact-prefixes __` filter, end-to-end.

## Realistic runtime estimates for `s38417_T1.bench`

Based on the measured per-vector/per-step costs above (39,594 gates /
1,629 PIs is a reasonable proxy for s38417_T1's scale; the README for the
artifact filter reports ~275 real rare nodes for the actual file, vs.
~4,000 in this synthetic stress test, so real runtimes should be **at or
below** these numbers):

| Stage                                   | `--vectors 10000` estimate |
|------------------------------------------|----------------------------:|
| Parse + compile                           | < 1 s                       |
| Rare-node finding (Monte Carlo)           | < 1 s                       |
| MERS, `--mutation-mode fast --mutation-rounds 3` | ~55 min               |
| MERS, `--mutation-mode paper` (default)   | ~26 hours                   |
| MERO (default, 8 rounds)                  | ~2.4 hours                  |
| MERS-s (v8 vectorized)                    | ~10 min (testset=10,000)    |
| MERS-h                                    | seconds                     |
| Excel export (rare-node sheet, ~41K nodes)| ~7 s                         |

**Recommended first run** (matches the existing
`V7_ARTIFACT_FILTER_README.md` recommendation, now with the v8 speedups
applied underneath):

```bat
python run_mers.py --bench benchmarks/s38417_T1.bench --name s38417_T1 ^
    --N 1000 --vectors 10000 --trojans 1000 --triggers 4 8 ^
    --mutation-mode fast --mutation-rounds 3 --skip-mero
```

This should complete in roughly **1-1.5 hours**. Add MERO back in
(`--skip-mero` removed) for ~2.4 extra hours, or use `--mutation-mode
paper` only if you can leave it running overnight (~1 day, vs. multiple
days before this update).

For a quick first look, reduce `--vectors` to 1000-2000 (proportionally
faster) before committing to a 10,000-vector run.

## No other changes

`src/rare_node_finder.py`, `src/evaluator.py`, `src/mero_algo.py`,
`src/pipeline.py`, `src/testset_exporter.py`, `run_mers.py`,
`tools/*.py` are unchanged -- `simulate()`/`simulate_batch()` are
drop-in replacements with the same return types (`NodeState` supports
the same `.get()`/`in`/`[]` interface every caller already used).
