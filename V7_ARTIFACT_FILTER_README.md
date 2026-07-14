# V7 update: synthetic LUT-helper node filter for MERS

This update fixes the issue seen with Xilinx/Vivado LUT-expanded BENCH files such as `s38417_T1.bench`.

## Problem fixed

The LUT-to-BENCH converter expands each Xilinx LUT into helper gates such as:

- `__LUTTERM_*`
- `__NOT_*`
- `__CONST*`

These nodes are required internally for correct simulation, but they are not real benchmark rare-node targets. In the previous version, MERS could classify thousands of these helper/minterm nodes as rare nodes, making the algorithm very slow and scientifically misleading.

## What changed

1. `src/rare_node_finder.py`
   - Added default artefact-node filter.
   - Nodes whose names start with `__` are excluded from rare-node selection.
   - These nodes still remain in simulation, so circuit logic is not broken.
   - Reports how many artefact nodes were excluded.

2. `src/circuit_parser.py`
   - Fixed a major topological-sort performance issue.
   - Large LUT-expanded BENCH files now parse much faster.

3. `src/excel_reporter.py`
   - Summary now reports excluded synthetic artefact nodes.
   - "All Node Probs" sheet now shows whether a node is a rare target, stuck/constant, synthetic artefact, or normal.

4. Cache versioning
   - Rare-node, MERS, MERO, MERS-h, MERS-s, and random-testset cache keys were updated to v6.
   - This prevents old checkpoints with bad helper-node rare targets from being silently reused.

5. Command-line options
   - Default behavior excludes synthetic artefact nodes.
   - To disable this filter only for debugging:

```bat
python run_mers.py --bench benchmarks/s38417_T1.bench --name s38417_T1 --include-artifact-nodes
```

   - To change the excluded prefixes:

```bat
python run_mers.py --bench benchmarks/s38417_T1.bench --name s38417_T1 --artifact-prefixes __,__TEMP_
```

## Recommended command for s38417_T1 quick test

```bat
cd /d C:\Users\i1305\Desktop\mers_v4
python run_mers.py --bench benchmarks/s38417_T1.bench --name s38417_T1 --N 100 --vectors 1000 --trojans 100 --triggers 4 8 --mutation-mode fast --mutation-rounds 3
```

You should now see something closer to a few hundred/few thousand real rare nodes instead of tens of thousands of `__LUTTERM_*` nodes. In the smoke test on your `s38417_T1.bench`, the rare-node count dropped from about `27088` inflated rare nodes to about `275` real candidate rare nodes when using 1000 Monte-Carlo vectors.

## Recommended full command

```bat
cd /d C:\Users\i1305\Desktop\mers_v4
rmdir /s /q mers_progress\s38417_T1
python run_mers.py --bench benchmarks/s38417_T1.bench --name s38417_T1 --N 1000 --vectors 10000 --trojans 1000 --triggers 4 8 --mutation-mode fast --mutation-rounds 3
```

For exact paper-style mutation, omit `--mutation-mode fast --mutation-rounds 3`, but it will be much slower on large circuits.
