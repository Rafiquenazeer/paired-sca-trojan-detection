# MERS Project v5 Fixed Package

This package applies the fixes discussed for reproducing the Huang et al. MERS results more closely on `s15850`.

## Main fixes included

1. **Paper-gate Trojan evaluator**
   - `src/evaluator.py` now evaluates a vectorised inserted Trojan gate model:
     - inverters for rare-0 trigger inputs,
     - balanced AND-tree trigger logic,
     - XOR-style payload effect.
   - This replaces the old under-counting model `trigger_sw + payload_sw`.

2. **Correct `TotalSwitch` definition everywhere**
   - Evaluation and MERS-s now count only internal golden-circuit gate output switching.
   - Primary input switching is excluded from the denominator.

3. **Correct MERS previous-vector update**
   - `src/mers_algo.py` now follows Algorithm 1: after accepting a vector, `tp = v'j`.
   - The old reset-to-zero behavior was removed.

4. **Paper mutation mode added**
   - Default: `--mutation-mode paper`, exact sequential bit-by-bit Algorithm-1 mutation.
   - Faster development option: `--mutation-mode fast --mutation-rounds 12`.

5. **MERO baseline added**
   - `src/mero_algo.py` implements a MERO-style activation baseline.
   - Reports now include Random, MERO, MERS, MERS-h, and MERS-s unless skipped.

6. **Cache versioning**
   - New v5 cache keys prevent accidental reuse of old v4 results.

7. **s15850 benchmark included**
   - Your uploaded `s15850(1).bench` is included as `benchmarks/s15850.bench`.

## Recommended commands

For the most paper-faithful run:

```bash
python run_mers.py --bench benchmarks/s15850.bench --name s15850_v5 --N 1000 --vectors 10000 --trojans 1000 --triggers 4 8 --mutation-mode paper
```

This can be slow in Python. For faster debugging before the full run:

```bash
python run_mers.py --bench benchmarks/s15850.bench --name s15850_v5_fast --N 1000 --vectors 10000 --trojans 1000 --triggers 4 8 --mutation-mode fast --mutation-rounds 12
```

If MERS-s is too slow, skip it first:

```bash
python run_mers.py --bench benchmarks/s15850.bench --name s15850_v5_fast --N 1000 --vectors 10000 --trojans 1000 --triggers 4 8 --mutation-mode fast --mutation-rounds 12 --skip-mers-s
```

## Important note

The new MERO implementation is a practical MERO-style activation baseline inside this Python framework. It is not guaranteed to be bit-for-bit identical to the original MERO implementation used by the paper, but it gives you the missing activation-based comparison method using the same parser and evaluator.

## Automatic test-vector export added

The project now automatically exports the generated test vectors for every enabled algorithm:

- Random
- MERO
- MERS
- MERS-h
- MERS-s

By default, exports are written to:

```text
mers_testvectors/<run_name>/
```

For example:

```text
mers_testvectors/s15850_v5_fast/
```

Each method gets:

```text
<run_name>_<method>_vectors.txt
<run_name>_<method>_vectors_hex.txt
```

The folder also contains:

```text
<run_name>_all_testsets.xlsx
primary_input_order.txt
vector_export_manifest.json
README_VECTOR_EXPORTS.txt
```

The binary `.txt` files contain one vector per line, with no header. The bit order is exactly the order in `primary_input_order.txt`.

When a full testset has more than 10,000 vectors, the exporter also writes:

```text
<run_name>_<method>_eval10k_vectors.txt
<run_name>_<method>_eval10k_vectors_hex.txt
```

These `eval10k` files are the first 10,000 vectors used for paper-style fair comparison.

To change the export folder:

```bash
python run_mers.py --bench benchmarks/s15850.bench --name s15850_v5_fast --N 1000 --vectors 10000 --trojans 1000 --triggers 4 8 --mutation-mode fast --mutation-rounds 12 --vectors-dir my_vectors
```

To disable vector export:

```bash
python run_mers.py --bench benchmarks/s15850.bench --name s15850_v5_fast --N 1000 --vectors 10000 --trojans 1000 --triggers 4 8 --mutation-mode fast --mutation-rounds 12 --no-export-vectors
```

Important: do not shuffle MERS, MERS-h, or MERS-s vectors. Their order matters because the side-channel switching is created by consecutive-vector transitions.

### Export old checkpointed vectors without rerunning MERS

If you already completed a run before this export update, you do **not** need to rerun the full experiment. Use:

```bash
python export_existing_testsets.py --bench benchmarks/s15850.bench --name s15850_v5_fast --N 1000 --vectors 10000 --mutation-mode fast --mutation-rounds 12 --C 5
```

This reads the `.pkl` checkpoints from:

```text
mers_progress/s15850_v5_fast/
```

and writes readable vectors to:

```text
mers_testvectors/s15850_v5_fast/
```
