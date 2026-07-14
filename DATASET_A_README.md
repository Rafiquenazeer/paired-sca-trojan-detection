# Dataset A: paired control/reference testset for Dataset B

## Concept

Dataset B (MERS / MERS-h / MERS-s) is engineered to MAXIMIZE switching of
selected rare nodes ("RareSwitch") while controlling total switching --
this makes a Trojan's trigger/payload logic stand out in side-channel
measurements.

**Dataset A** is the mirror image: a same-length, pair-by-pair-matched
testset that

* **minimises** switching of the same selected rare nodes / their local
  "suspicious cone" (RareConeSwitch -> ~0),
* while reproducing B's **BackgroundSwitch** (switching of every other
  gate), **input Hamming distance/weight**, and **output Hamming
  distance/weight**, pair by pair.

Apply both sequences to the same chip: any side-channel difference between
running A and running B should be attributable to the suspicious-cone logic
specifically (the suspected Trojan), since background activity and I/O
statistics are matched out.

## Suspicious cone

`compute_cone(circuit, seed_nodes, fanin_depth, fanout_depth)` (in
`src/dataset_a_generator.py`) returns: the seed rare nodes themselves, plus
`fanin_depth` levels of upstream logic (candidate trigger/comparator logic)
and `fanout_depth` levels of downstream logic (candidate payload logic).
`depth=0` disables that direction (cone = seeds only); `depth=-1` means
unlimited (full transitive cone).

## Algorithm

For each pair `(B[j-1], B[j])` (with `B[-1] = [0]*n_pi`, matching MERS's
init):

1. Record B's target stats: BackgroundSwitch, input HD/HW, output HD/HW.
2. Seed `A[j] = A[j-1] XOR (B[j-1] XOR B[j])` -- XOR-ing by B's own
   transition mask gives **exact** input-HD matching for free.
3. Run up to `--mutation-rounds-a` rounds of single-bit-flip local search
   (same vectorized "try all n_pi flips, take the best" pattern as MERS
   fast mode), maximising:

   ```
   score = -w_rare   * RareConeSwitch  / |cone|
           -w_bg     * |BackgroundSwitch - target_bg|  / |bg|
           -w_in_hd  * |InputHD  - target_in_hd|       / n_pi
           -w_in_hw  * |InputHW  - target_in_hw|       / n_pi
           -w_out_hd * |OutputHD - target_out_hd|      / n_po
           -w_out_hw * |OutputHW - target_out_hw|      / n_po
   ```

   `w_rare` defaults to 10x the other weights, so RareConeSwitch -> 0 is
   the primary objective; the rest are regularisers that keep A close to
   B's profile once RareConeSwitch is already minimal.

## Dynamic weight balancing (phased solver, `--dynamic-weights`)

The flat solver above optimises one fixed-weight objective over all input
bits. With a large suspicious cone (deep fan-in/fan-out expansion), a flat
search can stall: each accepted bit-flip trades rare-cone suppression
against statistical matching, and the matching terms keep nudging bits in
ways that re-disturb the cone. The phased solver removes that conflict:

* **Phase 1 -- rare-cone suppression.** Optimise with a high rare weight
  (`--phase1-w-rare`, default 50) while the matching terms stay ACTIVE at a
  reduced weight, over ALL input bits, until RareConeSwitch <=
  `--rare-threshold` (default 0) or the Phase-1 round budget is exhausted.
  Suppression dominates, but keeping the matching terms on is essential:
  with them zeroed, Phase 1 is free to drag the whole circuit into a
  low-activity state (collapsing Output HD/HW and BackgroundSwitch) as a
  cheap way to cut cone switching -- damage a short Phase 3 cannot repair,
  especially when the seeds are flip-flops whose cones dominate the primary
  outputs (e.g. s38417). With the matching terms active (rare still ~25x
  larger) the cone is quieted WITHOUT wandering from B's activity profile.

* **Phase 2 -- bit locking (`--lock-mode`).** Controls how much freedom
  Phase 3 gets:
  - `soft` (default): Phase 3 may flip ALL inputs, but the rare term stays
    active so re-disturbing the cone is penalised. Robust when the
    cone-input bits also drive the outputs (you need to flip some of them to
    match Output HD/HW). This is what makes s38417-style circuits work.
  - `hard`: Phase 3 flips ONLY background (non-cone-input) inputs --
    strongest guarantee the cone stays quiet, but if the locked cone-input
    PIs also drive the outputs, Phase 3 is starved and Output HD/HW
    collapse. Use only when cone-input PIs are a small, output-irrelevant
    subset.
  - `none`: no locking; Phase 3 is a flat search under the Phase-3 weights.

* **Phase 3 -- statistical matching.** Switch to Phase-3 weights and run
  best-flip (over the bits `lock-mode` allows) to match B's
  BackgroundSwitch / Input HD-HW / Output HD-HW. Budget auto-scales with
  cone size (same as Phase 1) so it can actually REPAIR the profile, not
  just nudge it.

Enable with `--dynamic-weights`. Key knobs:

* `--phase1-rounds` (default: cone-aware, ~0.5 x number-of-lockable-PIs,
  capped at 40) -- the suppression reach. Larger cones need more; the
  default scales automatically.
* `--rare-threshold N` -- accept RareConeSwitch <= N as "suppressed" (for
  cones too large to fully zero, set e.g. 1-2 so Phase 1 stops early and
  hands more budget to Phase 3).
* `--phase1-w-rare`, `--phase3-w-bg`, `--phase3-w-in-hd`, `--phase3-w-out-hd`,
  `--phase3-w-state`.

When the cone is small (e.g. depth 0/0 -- the literal selected rare nodes),
flat and phased both reach ~99.9% zero-RareConeSwitch; the phased solver's
advantage shows on larger cones. Measured on s15850, depth-(1,2) cone (88
cone nodes, 204 lockable PIs), 100 pairs:

| Solver | RareConeSwitch==0 | rare mean | BackgroundSwitch (A / B) |
|---|---:|---:|---:|
| Flat (3 rounds)              | 36% | 1.41 | 858.6 / 912.9 |
| Phased (Phase1=4)            | 4%  | 4.61 | 879.1 / 907.5 |
| Phased (Phase1=8)            | 36% | 1.40 | 890.9 / 907.5 |
| Phased (Phase1=16, default-ish) | 76% | 0.39 | 889.9 / 907.5 |

Two honest caveats: (1) Phase 1's reach is bounded by its round budget --
give it enough rounds (the cone-aware default does this) or it under-
suppresses. (2) Even with unlimited Phase-1 rounds, suppression plateaus
(here at ~76% of pairs hitting exactly 0): for some B-vectors the cone's
input dependencies make RareConeSwitch==0 incompatible with that pair's
required I/O, an inherent structural limit of single-pair matching, not a
solver deficiency. Background switching stays well-matched throughout.

## Validating Dataset A: measure the TRIGGER, not the payload

A subtle but important point when you check Dataset A with a
clean-vs-infected payload simulator (e.g. `trojan_switching_sim_fast.py`):

That simulator's headline **"PARTIAL activation"** counts switching of the
Trojan **payload** net -- the net that exists in the infected netlist but
not the clean one (e.g. `n4448_temp`). A Trust-Hub T1-style payload is
`payload = normal_net XOR trigger`. When the trigger is dormant (never
asserted -- true for BOTH A and B, since FULL activation is ~0%), the
payload net toggles essentially whenever the **normal** net it XORs with
toggles. Dataset A and Dataset B match normal-circuit / background activity
*by design*, so their payload toggling is necessarily about equal. Seeing
"PARTIAL activation ~= for A and B" therefore does NOT mean A failed -- it
is the expected consequence of matching the background.

What Dataset A actually minimises is switching and rare-value activation of
the **trigger** nodes (its cone). That is the differential side-channel
signal. To see it, compare the trigger nodes directly:

```bash
python analyze_ab_trigger_switching.py \
    --bench s38417.bench \
    --pi-order mers_testvectors/s38417_AB/primary_input_order.txt \
    --trigger-spec "Q_i_2__292_n_0:1,Q_i_2__363_n_0:1,Q_i_4__103_n_0:1,Q_i_6__5_n_0:0" \
    --vectors-a mers_testvectors/s38417_AB/s38417_AB_Dataset-A_vectors.txt \
    --vectors-b mers_testvectors/s38417_AB/s38417_AB_Dataset-B_vectors.txt
```

Example output shows, per trigger node, the probability of reaching its
activating value and the toggle rate, together with the aggregate
trigger-cone switching for Dataset A relative to Dataset B. Dataset A's
trigger switching should be a small fraction of Dataset B's, while the
background and input/output profiles stay matched.

The trigger-switching contrast -- not the payload PARTIAL-activation number
-- is the A-vs-B signal that matters for differential Trojan SCA. The
payload figure stays equal on purpose; the trigger figure is what your
control dataset moves.

### Do NOT pass --paired for the exported A/B files

`analyze_ab_trigger_switching.py` (and the payload simulator's `--paired`)
expect ONE layout: each line = `original||generated` concatenated. The
files written by this project's exporter
(`<name>_Dataset-A_vectors.txt`, `<name>_Dataset-B_vectors.txt`) are NOT
that layout -- each line is a single 1×n_pi vector, and the file is an
ordered sequence whose consecutive lines ARE the intended transitions
(`[0..0] -> v0 -> v1 -> ...`, the same convention MERS/MERS-s use). So run
the analyzer WITHOUT `--paired` (the default). `paired=False` in the output
is correct for these files; it counts each consecutive-line transition once,
which is exactly the per-pair switching the generator optimised.

## Targeting the ACTUAL Trojan trigger (important)

Dataset A only suppresses switching on the nodes you name as cone seeds. If
you measure switching/activation on one set of trigger nodes downstream
(e.g. with an external clean-vs-infected simulator) but generated A against
a *different* set, A and B will look nearly identical on your triggers --
because A quieted the wrong cone.

`--num-cone-seeds N` picks N **random** rare nodes. That is fine for a
generic "suppress some rare logic" control, but for a specific known Trojan
you must name its trigger nodes explicitly:

```bash
python generate_dataset_a.py --bench benchmarks/s38417.bench --name s38417_AB \
    --N 100 --mutation-mode fast --mutation-rounds 3 --C 5 --B-method mers_s \
    --cone-seeds "Q_i_2__292_n_0,Q_i_2__363_n_0,Q_i_4__103_n_0,Q_i_6__5_n_0" \
    --cone-fanin-depth 1 --cone-fanout-depth 1 --dynamic-weights
```

`--cone-seeds` accepts ANY gate-output node in the netlist, not only nodes
the finder flagged as "rare". For names not in the rare set, the rare
polarity (which value counts as the rare/suppressed one) is inferred from a
quick Monte-Carlo P(node==1). Measured on s38417 with the four trigger nodes
above as seeds: switching on those exact nodes dropped ~85% (B: 78 total
switches over 300 pairs -> A: 12), versus ~0% contrast when random seeds
were used. Use the SAME node names here that you measure downstream.

## Long runs: checkpoint/resume

Dataset A generation checkpoints every 250 vectors to
`mers_progress/<name>/_partial_<cache_key>.pkl`. If a long run is
interrupted, re-running the same command resumes from the last checkpoint
instead of starting over. The partial file is removed automatically on
successful completion.

## Usage

First produce Dataset B as usual:

```bash
python run_mers.py --bench benchmarks/s15850.bench --name s15850_AB \
    --N 100 --vectors 1500 --trojans 100 --triggers 4 8 \
    --mutation-mode fast --mutation-rounds 3 --skip-mero
```

Then generate Dataset A paired with the MERS-s testset:

```bash
python generate_dataset_a.py --bench benchmarks/s15850.bench --name s15850_AB \
    --N 100 --mutation-mode fast --mutation-rounds 3 --C 5 --B-method mers_s \
    --num-cone-seeds 8 --cone-fanin-depth 0 --cone-fanout-depth 0
```

* `--num-cone-seeds 8` randomly samples 8 of the rare nodes as "selected
  rare nodes" (mimicking an 8-trigger Trojan's footprint). Omit it to use
  ALL rare nodes as seeds, or use `--cone-seeds nodeA,nodeB,...` to name
  specific nodes.
* `--cone-fanin-depth`/`--cone-fanout-depth` (default 0/0 = literal
  selected rare nodes only) expand the cone into local trigger/payload
  logic.
* `--B-method` selects which of B's variants to pair against
  (`mers_s` [default], `mers_h`, `mers`, `mero`, `random`); the
  `--N`/`--mutation-mode`/`--mutation-rounds`/`--C` flags must match the
  original `run_mers.py` invocation so the checkpoint key resolves.

## Output

* `mers_testvectors/<name>/` -- Dataset A's vectors alongside B's
  (`.txt`/`.hex`/Excel), via the existing `TestsetExporter`.
* `mers_results/<name>_dataset_A_vs_B_<tag>.xlsx` -- comparison report:
  - **Summary**: mean/std/min/max for both datasets across all 6 metrics,
    "RareConeSwitch == 0" coverage, and a bar chart.
  - **Per-Pair**: every pair's A vs B values side by side.
  - **Cone Nodes**: the suspicious-cone and background node lists used.

## Measured results (s15850, 8-node cone = literal rare nodes, 1500 pairs)

| Metric | A: mean | B: mean | Diff |
|---|---:|---:|---:|
| RareConeSwitch | 0.001 | 0.628 | **-99.8%** |
| BackgroundSwitch | 881.2 | 883.4 | -0.24% |
| Input HD | 277.19 | 277.24 | -0.02% |
| Input HW | 296.25 | 296.41 | -0.05% |
| Output HD | 212.07 | 210.59 | +0.70% |
| Output HW | 254.75 | 254.60 | +0.06% |

1499/1500 (99.9%) of A's pairs achieve **exactly 0** RareConeSwitch
(vs 49.1% for B), in ~70s for 1500 pairs.

## Caveat: tiny circuits / large cone-to-circuit ratios

The "similar BackgroundSwitch + I/O statistics" goal needs enough
*background* degrees of freedom (gates outside the cone, input bits not
constrained by the HD/HW targets) to be independently satisfiable while
RareConeSwitch -> 0. On `c17` (6 gates total, 4-node cone -> only 2
background gates), minimising RareConeSwitch also suppresses the 2
remaining background gates almost entirely, so BackgroundSwitch/Output
HD/HW end up far from B's (not "similar"). This is an inherent
multi-objective tradeoff, not a bug -- for realistic benchmarks (the cone
is a small fraction of the circuit, as in the s15850 result above), all
six metrics match closely.
