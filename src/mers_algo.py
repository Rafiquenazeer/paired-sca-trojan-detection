"""
mers_algo.py  (v5)
==================
Algorithms 1/2/3 from Huang et al., CCS 2016.

Important v5 fixes
------------------
1. Paper-consistent previous-vector handling:
   after accepting a mutated vector, Algorithm 1 sets tp = v'j.  v4 reset tp
   to all-zero in some cases, which made the rare-switching counters differ
   from the final evaluated vector sequence.  v5 always uses tp = accepted.

2. Mutation options:
   * mutation_mode="paper": exact Algorithm-1 sequential bit scan.  This is
     slow in Python but most faithful to the paper.
   * mutation_mode="fast": vectorised iterative best-flip rounds.  This is
     much faster and useful for development; increase --mutation-rounds for
     better quality.

3. MERS-s TotalSwitch objective now matches the evaluator: internal gate
   outputs only, excluding primary inputs and constants.

4. Cache keys include v5 so old v4 testsets/reorderings are not reused.
"""

from __future__ import annotations

import sys
import logging
import numpy as np
from tqdm import tqdm

logger = logging.getLogger(__name__)

P_ACTIVATABLE = 0.0001
DEFAULT_MUTATION_MODE = "paper"
DEFAULT_MUTATION_ROUNDS = 12


# ======================================================================
# Algorithm 1 – MERS Core
# ======================================================================

class MERS:
    """
    Multiple Excitation of Rare Switching.

    Parameters
    ----------
    circuit         : Circuit object
    N               : rare-switching requirement per node
    rare_threshold  : threshold for rare node classification
    rng_seed        : reproducibility seed
    mutation_mode   : "paper" for exact sequential bit scan, "fast" for
                      vectorised iterative best-flip approximation
    mutation_rounds : rounds used only in fast mode
    """

    def __init__(self, circuit, N: int = 1000,
                 rare_threshold: float = 0.1,
                 rng_seed: int = 42,
                 mutation_mode: str = DEFAULT_MUTATION_MODE,
                 mutation_rounds: int = DEFAULT_MUTATION_ROUNDS):
        self.circuit         = circuit
        self.N               = N
        self.rare_threshold  = rare_threshold
        self.rng_seed        = rng_seed
        self.mutation_mode   = mutation_mode.lower().strip()
        self.mutation_rounds = int(mutation_rounds)
        if self.mutation_mode not in {"paper", "fast"}:
            raise ValueError("mutation_mode must be 'paper' or 'fast'")

    # ------------------------------------------------------------------
    def generate(self, rare_nodes_info: dict,
                 num_random_vectors: int = 10_000,
                 progress_mgr=None,
                 checkpoint_every: int = 200,
                 pad_to_requested: bool = False) -> dict:
        """Run MERS Algorithm 1 and return {testset, switching_counters, size}.

        By design (paper Algorithm 1), a candidate vector is kept ONLY if it
        excites at least one not-yet-satisfied rare node. Once every rare
        node has met its N-switch quota, the remaining candidates contribute
        nothing and are skipped, so the final testset is usually SMALLER than
        num_random_vectors. That is expected MERS behaviour, not a bug.

        Set pad_to_requested=True to top the testset back up to
        num_random_vectors with the highest-coverage unused candidate
        vectors (useful when a downstream comparison wants exactly the
        requested count). Padding vectors are appended after the MERS core,
        so they do not change the rare-node statistics of the core set.
        """
        import random
        from src.rare_node_finder import RareNodeFinder

        tag = f"N{self.N}_{self.mutation_mode}"
        if self.mutation_mode == "fast":
            tag += f"_r{self.mutation_rounds}"
        FINAL_KEY   = f"mers_v6_testset_{tag}"
        PARTIAL_KEY = f"_partial_mers_v6_testset_{tag}"

        if progress_mgr and progress_mgr.exists(FINAL_KEY):
            logger.info(f"[MERS] Loading cached v6 testset ({tag}) ...")
            result = progress_mgr.load(FINAL_KEY)
            logger.info(f"  -> {result['size']} vectors")
            return result

        rare_nodes = rare_nodes_info['rare_nodes']
        n_pi       = self.circuit.n_inputs()
        random.seed(self.rng_seed)
        np.random.seed(self.rng_seed)

        activatable = [n for n, info in rare_nodes.items()
                       if info.get('probability', 0) > P_ACTIVATABLE]
        can_early_stop = len(activatable) == 0
        logger.info(
            f"[MERS] {len(activatable)} activatable rare node(s) — "
            f"early-stop {'enabled (2000 no-growth)' if can_early_stop else 'disabled'}."
        )

        logger.info(f"[MERS] Generating {num_random_vectors:,} candidate test "
                    f"vectors to build the testset from (NOT rare-node detection; "
                    f"using the {len(rare_nodes)} rare nodes already found) ...")
        random_vectors = [
            [random.randint(0, 1) for _ in range(n_pi)]
            for _ in range(num_random_vectors)
        ]

        logger.info("[MERS] Sorting by rare-node coverage ...")
        scored         = RareNodeFinder.sort_vectors_by_rare_coverage(
            random_vectors, rare_nodes, self.circuit)
        sorted_vectors = [v for _, v in scored]

        # Resume from partial checkpoint
        if progress_mgr and progress_mgr.exists(PARTIAL_KEY):
            state              = progress_mgr.load(PARTIAL_KEY)
            testset            = state['testset']
            switching_counters = state['switching_counters']
            prev_vector        = state['prev_vector']
            start_idx          = state['last_completed_idx'] + 1
            no_growth_since    = state.get('no_growth_since', start_idx)
            logger.info(f"[MERS] Resuming from vector {start_idx} "
                        f"(testset: {len(testset)})")
        else:
            testset            = []
            switching_counters = {n: 0 for n in rare_nodes}
            prev_vector        = [0] * n_pi
            start_idx          = 0
            no_growth_since    = 0

        state_prev   = self.circuit.simulate(prev_vector)
        idx          = np.arange(n_pi)
        prev_size    = len(testset)
        interrupted  = False
        last_j       = start_idx - 1

        logger.info(
            f"[MERS] Mutation loop: N={self.N}, rare_nodes={len(rare_nodes)}, "
            f"mode={self.mutation_mode}, rounds={self.mutation_rounds}, start={start_idx} ..."
        )

        if self.mutation_mode == "paper":
            logger.warning(
                "[MERS] Exact paper mutation is slow in Python. For quick tests, "
                "use --mutation-mode fast --mutation-rounds 12."
            )

        pbar = tqdm(total=len(sorted_vectors) - start_idx,
                    initial=0, desc="  MERS vectors", ncols=80)

        try:
            for j in range(start_idx, len(sorted_vectors)):
                last_j = j
                vj = list(sorted_vectors[j])

                if self.mutation_mode == "paper":
                    mutated, state_mut = self._mutate_paper_sequential(
                        vj, state_prev, rare_nodes, switching_counters)
                else:
                    mutated, state_mut = self._mutate_fast_best_rounds(
                        vj, state_prev, rare_nodes, switching_counters, n_pi, idx)

                improved = _check_improvement(
                    state_prev, state_mut, rare_nodes, switching_counters, self.N)

                if improved or len(testset) == 0:
                    testset.append(mutated)
                    _update_counters(state_prev, state_mut,
                                     rare_nodes, switching_counters)

                    # Paper Algorithm 1 line 24: Set tp = v'j.
                    # Do NOT reset to all-zero here.
                    prev_vector = mutated
                    state_prev  = state_mut

                # Early-stop check
                if len(testset) > prev_size:
                    no_growth_since = j
                    prev_size       = len(testset)
                elif can_early_stop and len(testset) <= 1:
                    if (j - no_growth_since) >= 2000:
                        pbar.close()
                        logger.warning(
                            "[MERS] No improvement after 2000 iterations. "
                            "All rare nodes P~0. Stopping.")
                        break

                # Checkpoint
                if progress_mgr and j > start_idx and j % checkpoint_every == 0:
                    progress_mgr.save(PARTIAL_KEY, {
                        'testset':            testset,
                        'switching_counters': switching_counters,
                        'prev_vector':        prev_vector,
                        'last_completed_idx': j,
                        'no_growth_since':    no_growth_since,
                    })

                pbar.update(1)

                # N-detect termination
                if all(v >= self.N for v in switching_counters.values()):
                    logger.info(f"\n[MERS] All nodes satisfied N={self.N} at vector {j}")
                    break

        except KeyboardInterrupt:
            interrupted = True
            print(f"\n[MERS] Interrupted at vector {last_j}.")
        finally:
            if pbar:
                pbar.close()

        if progress_mgr:
            progress_mgr.save(PARTIAL_KEY, {
                'testset': testset,
                'switching_counters': switching_counters,
                'prev_vector': prev_vector,
                'last_completed_idx': last_j,
                'no_growth_since': no_growth_since,
            })
            print(f"[MERS] Checkpoint saved — testset: {len(testset)} vectors")

        if interrupted:
            print("[MERS] Re-run same command to continue.")
            sys.exit(0)

        # Minimum testset guarantee so later stages have enough vectors even
        # on tiny or pathological benchmarks.
        MIN_SIZE = max(100, len(rare_nodes))
        if len(testset) < MIN_SIZE:
            logger.warning("[MERS] Filling testset to %d with fallback vectors.",
                           MIN_SIZE)
            existing = set(map(tuple, testset))
            for vec in sorted_vectors:
                if len(testset) >= MIN_SIZE:
                    break
                key = tuple(vec)
                if key not in existing:
                    testset.append(list(vec))
                    existing.add(key)

        # Explain the (normal) gap between requested and kept counts.
        n_skipped = num_random_vectors - len(testset)
        if n_skipped > 0:
            logger.info(
                "[MERS] Kept %d of %d candidate vectors; %d were skipped because "
                "they excited no still-unsatisfied rare node (every rare node had "
                "already met its N=%d quota). This is expected MERS behaviour.",
                len(testset), num_random_vectors, n_skipped, self.N)
            if pad_to_requested:
                existing = set(map(tuple, testset))
                added = 0
                for vec in sorted_vectors:
                    if len(testset) >= num_random_vectors:
                        break
                    key = tuple(vec)
                    if key not in existing:
                        testset.append(list(vec))
                        existing.add(key)
                        added += 1
                logger.info("[MERS] pad_to_requested: appended %d unused "
                            "high-coverage vectors -> %d total.",
                            added, len(testset))

        result = {
            'testset':            testset,
            'switching_counters': switching_counters,
            'size':               len(testset),
            'mutation_mode':      self.mutation_mode,
            'mutation_rounds':    self.mutation_rounds,
        }
        logger.info(f"[MERS] Done — testset: {len(testset)} vectors")

        if progress_mgr:
            progress_mgr.save(FINAL_KEY, result)
            if progress_mgr.exists(PARTIAL_KEY):
                progress_mgr.delete(PARTIAL_KEY)

        return result

    # ------------------------------------------------------------------
    # Mutation kernels
    # ------------------------------------------------------------------

    def _mutate_paper_sequential(self, vj, state_prev, rare_nodes, counters):
        """Exact Algorithm-1 lines 14–20: scan each bit and accept if RS improves."""
        current = list(vj)
        state_current = self.circuit.simulate(current)
        rs_now = _scalar_rare_sw(state_prev, state_current,
                                 rare_nodes, counters, self.N)

        for bit in range(len(current)):
            candidate = current.copy()
            candidate[bit] ^= 1
            state_candidate = self.circuit.simulate(candidate)
            rs_candidate = _scalar_rare_sw(state_prev, state_candidate,
                                           rare_nodes, counters, self.N)
            if rs_candidate > rs_now:
                current = candidate
                state_current = state_candidate
                rs_now = rs_candidate
        return current, state_current

    def _mutate_fast_best_rounds(self, vj, state_prev, rare_nodes, counters, n_pi, idx):
        """Fast vectorised approximation: best bit flip per round."""
        state_vj  = self.circuit.simulate(vj)
        rs_now    = _scalar_rare_sw(state_prev, state_vj,
                                    rare_nodes, counters, self.N)

        current       = np.array(vj, dtype=np.uint8)
        state_current = state_vj

        for _round in range(max(1, self.mutation_rounds)):
            muts = np.tile(current, (n_pi, 1))
            muts[idx, idx] ^= 1

            batch    = self.circuit.simulate_batch(muts)
            rs_batch = _batch_rare_sw(
                state_prev, batch, rare_nodes, counters, self.N, n_pi)

            best_bit = int(np.argmax(rs_batch))
            if rs_batch[best_bit] > rs_now:
                current[best_bit] ^= 1
                state_current = {node: int(arr[best_bit]) for node, arr in batch.items()}
                rs_now = int(rs_batch[best_bit])
            else:
                break

        return current.tolist(), state_current


# ======================================================================
# Algorithm 2 – MERS-h
# ======================================================================

class MERSHamming:
    """Algorithm 2: Hamming-distance reordering."""

    def __init__(self, circuit):
        self.circuit = circuit

    def reorder(self, testset: list, progress_mgr=None, N_tag: str = '') -> list:
        CACHE_KEY = f"mers_h_v6_testset_{N_tag}"
        if progress_mgr and progress_mgr.exists(CACHE_KEY):
            logger.info("[MERS-h] Loading cached v6 testset ...")
            return progress_mgr.load(CACHE_KEY)

        if not testset:
            return []
        logger.info(f"[MERS-h] Reordering {len(testset):,} vectors ...")

        remaining = np.array(testset, dtype=np.uint8)
        reordered = []
        prev      = np.zeros(remaining.shape[1], dtype=np.uint8)
        alive     = np.ones(len(remaining), dtype=bool)

        pbar = tqdm(total=len(remaining), desc="  MERS-h", ncols=80)
        while alive.any():
            dists       = (remaining[alive] != prev[np.newaxis, :]).sum(axis=1)
            local_best  = int(np.argmin(dists))
            alive_idxs  = np.where(alive)[0]
            global_best = alive_idxs[local_best]
            chosen      = remaining[global_best]
            reordered.append(chosen.tolist())
            prev               = chosen
            alive[global_best] = False
            pbar.update(1)
        pbar.close()

        logger.info(f"[MERS-h] Done — {len(reordered)} vectors")
        if progress_mgr:
            progress_mgr.save(CACHE_KEY, reordered)
        return reordered


# ======================================================================
# Algorithm 3 – MERS-s
# ======================================================================

class MERSSim:
    """
    Algorithm 3: Simulation-based reordering using profit = C*RareSwitch - TotalSwitch.
    v5 fix: TotalSwitch is internal-gate switching only, matching evaluator.py.

    v8 performance fix
    -------------------
    The per-step TotalSwitch/RareSwitch computation used to loop over every
    gate node (and every rare node) individually, issuing one small NumPy
    call each -- O(n_gates) calls PER reordering step, i.e. O(n_gates *
    testset_size) calls overall.  For LUT-expanded benchmarks (tens of
    thousands of gate nodes) and large testsets (thousands of vectors,
    typical for MERS with N=1000), this Python-loop overhead became the
    dominant cost of MERS-s (tens of minutes).

    v8 stacks all gate-node / rare-node columns from `batch` into single
    (n_nodes, K) matrices with one np.stack() call, then computes
    TotalSwitch / RareSwitch with 2-3 vectorised ops total per step
    regardless of n_gates -- typically a ~10x reduction in MERS-s runtime
    for large circuits, with identical results.
    """

    def __init__(self, circuit, rare_nodes: dict):
        self.circuit    = circuit
        self.rare_nodes = rare_nodes
        self.gate_nodes = list(getattr(circuit, 'gates', {}).keys())

        rare_names = list(rare_nodes.keys())
        self._rare_names        = rare_names
        self._rare_rare_val     = np.array(
            [rare_nodes[n]['rare_value'] for n in rare_names], dtype=np.uint8)
        self._rare_non_rare_val = np.array(
            [rare_nodes[n]['non_rare_value'] for n in rare_names], dtype=np.uint8)

    def reorder(self, testset: list, C: int = 5,
                progress_mgr=None, N_tag: str = '',
                checkpoint_every: int = 200) -> list:
        CACHE_KEY = f"mers_s_v6_testset_{N_tag}_C{C}"
        if progress_mgr and progress_mgr.exists(CACHE_KEY):
            logger.info(f"[MERS-s] Loading cached v6 testset (C={C}) ...")
            return progress_mgr.load(CACHE_KEY)

        if not testset:
            return []
        logger.info(f"[MERS-s] Reordering {len(testset):,} vectors (C={C}) ...")

        remaining  = [list(v) for v in testset]
        reordered  = []
        prev       = [0] * len(testset[0])
        state_prev = self.circuit.simulate(prev)

        pbar        = tqdm(total=len(remaining), desc="  MERS-s", ncols=80)
        interrupted = False

        try:
            while remaining:
                batch = self.circuit.simulate_batch(remaining)
                K     = len(remaining)
                zeros = np.zeros(K, dtype=np.uint8)

                # ---- RareSwitch: stack all rare-node columns at once ----
                if self._rare_names:
                    rare_mat  = np.stack(
                        [batch.get(n, zeros) for n in self._rare_names])  # (R,K)
                    rare_prev = np.fromiter(
                        (state_prev.get(n, 0) for n in self._rare_names),
                        dtype=np.uint8, count=len(self._rare_names))       # (R,)
                    eligible = (rare_prev == self._rare_non_rare_val)      # (R,)
                    hits     = (rare_mat == self._rare_rare_val[:, None])  # (R,K)
                    rare_sw  = (hits & eligible[:, None]).sum(axis=0, dtype=np.int32)
                else:
                    rare_sw = np.zeros(K, dtype=np.int32)

                # ---- TotalSwitch: stack all gate-output columns at once ----
                # (internal gate outputs only, matching evaluator.py)
                gate_mat  = np.stack(
                    [batch.get(n, zeros) for n in self.gate_nodes])        # (G,K)
                gate_prev = np.fromiter(
                    (state_prev.get(n, 0) for n in self.gate_nodes),
                    dtype=np.uint8, count=len(self.gate_nodes))            # (G,)
                total_sw = (gate_mat != gate_prev[:, None]).sum(axis=0, dtype=np.int32)

                profits  = C * rare_sw - total_sw
                best_idx = int(np.argmax(profits))

                chosen = remaining.pop(best_idx)
                reordered.append(chosen)
                prev       = chosen
                state_prev = self.circuit.simulate(prev)
                pbar.update(1)

                if progress_mgr and len(reordered) % checkpoint_every == 0:
                    progress_mgr.save(f"_partial_{CACHE_KEY}",
                                      {'reordered': reordered,
                                       'remaining': remaining, 'prev': prev})

        except KeyboardInterrupt:
            interrupted = True
            print("\n[MERS-s] Interrupted.")
        finally:
            pbar.close()

        if interrupted:
            if progress_mgr:
                progress_mgr.save(f"_partial_{CACHE_KEY}",
                                  {'reordered': reordered,
                                   'remaining': remaining, 'prev': prev})
            sys.exit(0)

        logger.info(f"[MERS-s] Done — {len(reordered)} vectors")
        if progress_mgr:
            progress_mgr.save(CACHE_KEY, reordered)
            if progress_mgr.exists(f"_partial_{CACHE_KEY}"):
                progress_mgr.delete(f"_partial_{CACHE_KEY}")
        return reordered


# ======================================================================
# Shared helper functions
# ======================================================================

def _scalar_rare_sw(state_prev: dict, state_curr: dict,
                    rare_nodes: dict, counters: dict, N: int) -> int:
    """Count non_rare→rare transitions for unsatisfied nodes (scalar)."""
    count = 0
    for node, info in rare_nodes.items():
        if counters.get(node, 0) < N:
            if (state_prev.get(node, 0) == info['non_rare_value']
                    and state_curr.get(node, 0) == info['rare_value']):
                count += 1
    return count


def _batch_rare_sw(state_prev: dict, batch: dict,
                   rare_nodes: dict, counters: dict,
                   N: int, K: int) -> np.ndarray:
    """Count non_rare→rare transitions for K batch candidates."""
    counts = np.zeros(K, dtype=np.int32)
    for node, info in rare_nodes.items():
        if counters.get(node, 0) >= N:
            continue
        if node not in batch:
            continue
        if state_prev.get(node, 0) != info['non_rare_value']:
            continue
        counts += (batch[node] == info['rare_value']).astype(np.int32)
    return counts


def _check_improvement(state_prev: dict, state_curr: dict,
                       rare_nodes: dict, counters: dict, N: int) -> bool:
    """True if at least one unsatisfied rare node gets a non_rare→rare transition."""
    for node, info in rare_nodes.items():
        if counters.get(node, 0) < N:
            if (state_prev.get(node, 0) == info['non_rare_value']
                    and state_curr.get(node, 0) == info['rare_value']):
                return True
    return False


def _update_counters(state_prev: dict, state_curr: dict,
                     rare_nodes: dict, counters: dict) -> None:
    """Increment switching counters for accepted transitions."""
    for node, info in rare_nodes.items():
        if (state_prev.get(node, 0) == info['non_rare_value']
                and state_curr.get(node, 0) == info['rare_value']):
            counters[node] = counters.get(node, 0) + 1
