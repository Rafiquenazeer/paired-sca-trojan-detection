"""
dataset_a_generator.py
=======================
Generates "Dataset A": a paired control/reference testset for Dataset B
(the MERS-family testset).

Where Dataset B (MERS / MERS-h / MERS-s) is engineered to MAXIMIZE
switching in rare-node trigger logic (high RareSwitch, controlled
TotalSwitch -> high relative switching -> good Trojan SCA sensitivity),
Dataset A is engineered to do the OPPOSITE on the SAME suspect logic while
reproducing B's overall activity profile:

  * RareSwitch  -- switching of the selected rare nodes AND their local
                   "suspicious cones" (fan-in/fan-out neighbourhood,
                   representing candidate Trojan trigger/payload logic)
                   -- MINIMIZED (target ~ 0).

  * BackgroundSwitch -- switching of every other internal gate (the
                   normal circuit, excluding the rare cones) -- matched
                   to B's per-pair BackgroundSwitch.

  * Input Hamming distance / weight -- HD(prev,curr) and HW(curr) of the
                   primary-input vectors -- matched to B's per-pair values.

  * Output Hamming distance / weight -- HD/HW of circuit.primary_outputs
                   -- matched to B's per-pair values.

Use case: apply A and B as two separate test sequences to the same chip.
Because background switching and I/O activity are matched pair-by-pair,
any side-channel DIFFERENCE between running A vs running B should be
attributable to the rare-cone logic specifically (i.e. the suspected
Trojan), not to overall activity-level differences between the two
testsets.

Algorithm
---------
For each consecutive pair (B[j-1], B[j]) in dataset B (with B[-1] taken as
the all-zero vector, matching MERS's initial `tp`):

  1. Record B's per-pair "target" statistics: BackgroundSwitch, input
     HD/HW, output HD/HW.

  2. Seed A[j] = A[j-1] XOR (B[j-1] XOR B[j]).  XOR-ing by B's own
     transition mask gives A[j] EXACTLY B's input Hamming distance from
     A[j-1] for free (mask popcount is preserved regardless of base
     vector), and a Hamming weight close to B[j]'s in expectation.

  3. Locally refine A[j] with a multi-round single-bit-flip search (the
     same "best-flip" pattern as MERS's fast mode), scoring each
     candidate by a weighted, scale-normalised objective:

       score = -W_rare  * RareConeSwitch / |cone_nodes|
               -W_bg    * |BackgroundSwitch - target_bg|   / |bg_nodes|
               -W_in_hd * |InputHD  - target_in_hd|        / n_pi
               -W_in_hw * |InputHW  - target_in_hw|        / n_pi
               -W_out_hd* |OutputHD - target_out_hd|       / n_po
               -W_out_hw* |OutputHW - target_out_hw|       / n_po

     W_rare dominates (default 10x the others), so the search drives
     RareConeSwitch to 0 first; the remaining terms act as regularisers
     that keep A's activity profile close to B's once RareConeSwitch
     is already minimal.

Everything is vectorised via Circuit.simulate_matrix(): each round
evaluates all n_pi single-bit-flip neighbours of the current candidate in
one call.

v9 performance fix
-------------------
The original implementation called Circuit.simulate_batch() (which builds
a dict with one entry per circuit node -- e.g. ~44,600 entries for
s38417_T1) and then re-stacked thousands of those entries (the entire
background-node set, e.g. ~43,000 nodes) into a matrix via np.stack() --
TWICE per round (once to read, the dict-build itself is O(n_total_nodes)
too). Measured on s15850 this was ~50% of the per-round cost; the fraction
grows with background-node count.

v9 uses Circuit.simulate_matrix() instead, which returns the raw (n_rows,
K) value matrix plus the node->row index directly. cone/bg/po row indices
are precomputed ONCE in __init__ as integer arrays; each round slices
M[idx_array] -- a single vectorised gather -- instead of a Python list
comprehension + np.stack over thousands of dict entries. Verified to
produce IDENTICAL per-pair statistics to the v8 dict-based implementation
on s15850 (8-node cone, 1500-pair B). Measured ~1.9x per-round speedup on
s15850 (2907 gates); the speedup is larger for bigger circuits since the
bypassed dict/stack cost scales with total node count (~44,600 for
s38417_T1 vs ~3,500 for s15850).
"""

from __future__ import annotations

import logging
from collections import deque

import numpy as np
from tqdm import tqdm

logger = logging.getLogger(__name__)

# Flat-mode weights (used when dynamic_weights=False -- the original
# single-phase solver). 'state' defaults to 0 (off): in full-scan mode the
# DFF next-state bits already appear among the primary outputs, so Output
# HD/HW covers them; set w_state > 0 only to weight scan-state matching as
# a separate term.
DEFAULT_WEIGHTS = {
    'rare':    10.0,
    'bg':       1.0,
    'in_hd':    1.0,
    'in_hw':    1.0,
    'out_hd':   1.0,
    'out_hw':   1.0,
    'state':    0.0,
}

# Dynamic-weight schedule (used when dynamic_weights=True).
# Phase 1 -- rare-cone suppression: rare DOMINATES (high weight) so the
#            search spends most of its effort driving RareConeSwitch -> 0,
#            but the matching terms stay ACTIVE at a reduced weight. This is
#            critical: with the matching terms zeroed, Phase 1 is free to
#            drag the whole circuit into a low-activity state (collapsing
#            Output HD/HW and BackgroundSwitch) as a cheap way to reduce cone
#            switching -- damage that the short Phase 3 cannot then repair,
#            especially when the seeds are flip-flops whose cones dominate
#            the primary outputs (e.g. s38417). Keeping the matching terms on
#            (rare still ~25x larger) suppresses the cone WITHOUT wandering
#            away from B's activity profile.
# Phase 3 -- statistical matching: rare drops to a small "keep it pinned"
#            value and the matching terms take over, flipping only unlocked
#            background inputs.
PHASE1_WEIGHTS = {
    'rare':    50.0,
    'bg':       2.0,
    'in_hd':    2.0,
    'in_hw':    1.0,
    'out_hd':   2.0,
    'out_hw':   1.0,
    'state':    0.0,
}
PHASE3_WEIGHTS = {
    'rare':     2.0,
    'bg':       2.0,
    'in_hd':    2.0,
    'in_hw':    1.0,
    'out_hd':   2.0,
    'out_hw':   1.0,
    'state':    0.0,
}


# ======================================================================
# Cone computation
# ======================================================================

def compute_cone(circuit, seed_nodes, fanin_depth=2, fanout_depth=2) -> set:
    """
    Return the set of GATE-OUTPUT node names forming the "suspicious cone"
    around `seed_nodes` (typically the selected rare nodes): the seeds
    themselves, plus nodes within `fanin_depth` levels upstream (the logic
    that DRIVES the rare condition -- candidate trigger comparator/AND
    logic) and `fanout_depth` levels downstream (logic the rare node could
    influence -- candidate payload logic).

    A depth of 0 disables that direction. A depth of None/-1 means
    "unlimited" (full transitive cone in that direction).

    Only names present in `circuit.gates` are returned (primary inputs
    and constants are excluded -- they have no "switching" of their own
    that MERS-s/the evaluator count as TotalSwitch).
    """
    gate_set = set(circuit.gates.keys())
    seeds = [n for n in seed_nodes if n in gate_set]
    cone = set(seeds)

    def _bfs(start, depth, neighbours_fn):
        if depth == 0:
            return
        unlimited = depth is None or depth < 0
        frontier = set(start)
        visited = set(start)
        d = 0
        while frontier and (unlimited or d < depth):
            next_frontier = set()
            for node in frontier:
                for nb in neighbours_fn(node):
                    if nb in gate_set and nb not in visited:
                        visited.add(nb)
                        next_frontier.add(nb)
            cone.update(next_frontier)
            frontier = next_frontier
            d += 1

    # Fan-in: walk gate.inputs backwards
    _bfs(seeds, fanin_depth,
         lambda n: circuit.gates[n].inputs if n in circuit.gates else [])
    # Fan-out: walk circuit.dependents forwards
    _bfs(seeds, fanout_depth,
         lambda n: circuit.dependents.get(n, []))

    return cone


def compute_cone_input_pis(circuit, cone_nodes) -> set:
    """
    Return the set of PRIMARY-INPUT names that feed (transitively) into the
    cone -- i.e. the input bits whose values can affect any node in
    `cone_nodes`. These are the "bits that caused the suppression": the
    dynamic-weight solver locks them after Phase 1 so that Phase 2/3 moves
    (which flip only UNLOCKED background inputs) cannot re-disturb the rare
    cone.

    Implementation: full transitive fan-in from every cone node, collecting
    any node that is a declared primary input. In full-scan mode this
    includes scan PIs (DFF Q outputs) that feed the cone, which is correct
    -- a flip there could change a cone node's value just like a real PI.
    """
    pi_set = set(circuit.primary_inputs)
    if not pi_set or not cone_nodes:
        return set()

    locked = set()
    visited = set()
    frontier = set(cone_nodes)
    while frontier:
        nxt = set()
        for node in frontier:
            if node in visited:
                continue
            visited.add(node)
            if node in pi_set:
                locked.add(node)
                continue  # PIs have no further fan-in to walk
            gate = circuit.gates.get(node)
            if gate is None:
                continue
            for inp in gate.inputs:
                if inp not in visited:
                    nxt.add(inp)
        frontier = nxt
    return locked


# ======================================================================
# Dataset A generator
# ======================================================================

class DatasetAGenerator:
    """
    Parameters
    ----------
    circuit          : parsed Circuit
    rare_nodes       : dict from RareNodeFinder (the "selected rare nodes")
    cone_fanin_depth : fan-in levels to include in the suspicious cone
                       (default 2; 0 = rare nodes only)
    cone_fanout_depth: fan-out levels to include in the suspicious cone
                       (default 2)
    weights          : objective weights, see DEFAULT_WEIGHTS
    mutation_rounds  : max single-bit-flip refinement rounds per vector
    rng_seed         : reproducibility (only used for tie-breaking)
    """

    def __init__(self, circuit, rare_nodes: dict,
                 cone_fanin_depth=2, cone_fanout_depth=2,
                 weights: dict = None, mutation_rounds: int = 5,
                 rng_seed: int = 7,
                 dynamic_weights: bool = False,
                 phase1_weights: dict = None,
                 phase3_weights: dict = None,
                 phase1_rounds: int = None,
                 phase3_rounds: int = None,
                 rare_threshold: int = 0,
                 lock_mode: str = 'soft',
                 max_candidates: int = None,
                 no_improve_patience: int = 2):
        self.circuit = circuit
        self.rare_nodes = rare_nodes
        self.weights = {**DEFAULT_WEIGHTS, **(weights or {})}
        self.mutation_rounds = max(1, int(mutation_rounds))
        self.rng = np.random.RandomState(rng_seed)

        # ---- Dynamic weight balancing (phased solver) ----
        self.dynamic_weights = bool(dynamic_weights)
        self.phase1_weights = {**PHASE1_WEIGHTS, **(phase1_weights or {})}
        self.phase3_weights = {**PHASE3_WEIGHTS, **(phase3_weights or {})}
        # lock_mode controls Phase 2 / Phase 3 freedom:
        #   'hard' -- Phase 3 may flip ONLY background (non-cone-input) PIs.
        #             Strongest guarantee the cone stays quiet, but if the
        #             locked cone-input PIs also drive the primary outputs
        #             (common when seeds are flip-flops, e.g. s38417), Phase 3
        #             has too little freedom to match Output HD/HW -> outputs
        #             collapse. Use only when cone-input PIs are a small,
        #             output-irrelevant subset.
        #   'soft' -- Phase 3 may flip ALL PIs, but the rare term stays active
        #             (phase3 w_rare) so re-disturbing the cone is penalised.
        #             Cone stays quiet in practice AND outputs can be matched.
        #             DEFAULT -- robust across circuit structures.
        #   'none' -- no locking; Phase 3 == flat search with phase3 weights.
        self.lock_mode = lock_mode if lock_mode in ('hard', 'soft', 'none') else 'soft'
        # Speed levers (large circuits):
        #   max_candidates -- if set, each best-flip round evaluates a random
        #     subset of this many input positions instead of all n_pi. Cost
        #     of simulate_matrix scales ~linearly with candidate count, so
        #     this is a near-proportional speedup. Different subset each round
        #     means all bits still get chances over multiple rounds.
        #   no_improve_patience -- stop a phase after this many CONSECUTIVE
        #     non-improving rounds (with subsampling a single empty round no
        #     longer reliably means "converged", so we need patience > 1).
        self.max_candidates = int(max_candidates) if max_candidates else None
        self.no_improve_patience = max(1, int(no_improve_patience))
        # Per-phase round budgets (finalised after cone is built; see below).
        self._phase1_rounds_arg = phase1_rounds
        self._phase3_rounds_arg = phase3_rounds
        # RareConeSwitch value at/below which Phase 1 is considered "done"
        # and bit-locking kicks in (0 = require exactly zero).
        self.rare_threshold = max(0, int(rare_threshold))

        self.cone_nodes = compute_cone(
            circuit, rare_nodes.keys(), cone_fanin_depth, cone_fanout_depth)
        all_gates = set(circuit.gates.keys())
        self.bg_nodes = all_gates - self.cone_nodes

        self.cone_list = sorted(self.cone_nodes)
        self.bg_list   = sorted(self.bg_nodes)
        # Dedupe POs while preserving order (a node can appear twice if a
        # gate output is also listed as a scan PO, for example).
        seen = set()
        self.po_list = []
        for po in circuit.primary_outputs:
            if po not in seen:
                seen.add(po)
                self.po_list.append(po)

        # Precompute row indices into Circuit.simulate_matrix()'s value
        # matrix M, once. Unknown names (shouldn't happen for cone/bg/PO
        # gate-output lists, but POs can occasionally be phantom) fall back
        # to row 0 (the permanent zero row) -- same convention M itself
        # uses for undefined gate inputs.
        node_index = circuit._node_index
        zero_row   = getattr(circuit, '_zero_row', 0)
        self.cone_idx = np.array([node_index.get(n, zero_row) for n in self.cone_list], dtype=np.int64)
        self.bg_idx   = np.array([node_index.get(n, zero_row) for n in self.bg_list],   dtype=np.int64)
        self.po_idx   = np.array([node_index.get(n, zero_row) for n in self.po_list],   dtype=np.int64)

        # Scan-state (DFF next-state) nodes for the optional w_state term.
        # In full-scan mode, each DFF Q became a scan PI and its D-driver
        # became a scan PO; the next-state value of flip-flop Q lives at the
        # node that drives it. circuit.dff_nodes holds the Q (scan-PI) names;
        # their drivers were appended to primary_outputs at parse time. We
        # match each dff_node to its driver via the parse-time pairing: the
        # D node is whatever the DFF gate's input was. We don't retain the
        # gate (it's removed in full-scan), so we approximate scan-state by
        # the dff_node columns themselves (Q at time t vs t-1 across the
        # pair) -- i.e. how many flip-flops change state between vectors.
        dff_nodes = list(getattr(circuit, 'dff_nodes', []))
        self._n_state = max(1, len(dff_nodes))
        if dff_nodes:
            self._state_idx = np.array(
                [node_index.get(n, zero_row) for n in dff_nodes], dtype=np.int64)
        else:
            self._state_idx = None

        self.n_pi = circuit.n_inputs()

        # Auto-enable candidate subsampling on large circuits: evaluating all
        # n_pi single-bit flips per round is the dominant cost when n_pi is in
        # the thousands (e.g. s38417: 1559 inputs). If the caller didn't set
        # max_candidates and n_pi is large, cap candidates per round at 250 --
        # ~10x faster with negligible quality loss (verified on s38417-scale).
        if self.max_candidates is None and self.dynamic_weights and self.n_pi > 600:
            self.max_candidates = 250
            self._auto_subsample = True
        else:
            self._auto_subsample = False

        # ---- Cone-input PIs = the lockable bits (Phase 2) ----
        # These are the primary-input positions that can affect the cone.
        # Phase 3 is forbidden from flipping them, guaranteeing it cannot
        # re-disturb a cone that Phase 1 already quieted.
        pi_pos = {name: i for i, name in enumerate(circuit.primary_inputs)}
        cone_input_pis = compute_cone_input_pis(circuit, self.cone_nodes)
        self.cone_input_pi_names = cone_input_pis
        self.cone_input_pi_idx = np.array(
            sorted(pi_pos[n] for n in cone_input_pis if n in pi_pos),
            dtype=np.int64)
        # Boolean mask over the n_pi input positions: True == cone-relevant
        # (locked in Phase 3), False == background (free to flip in Phase 3).
        self.cone_input_mask = np.zeros(self.n_pi, dtype=bool)
        if self.cone_input_pi_idx.size:
            self.cone_input_mask[self.cone_input_pi_idx] = True
        self.bg_input_idx = np.where(~self.cone_input_mask)[0].astype(np.int64)

        # Finalise per-phase round budgets now that cone size is known.
        # Phase 1 default: enough flips to plausibly quiet the cone -- scale
        # with the number of cone-input PIs, capped so it stays practical.
        # (Empirically, ~min(n_cone_input_pis, ~3x) rounds reaches the
        # suppression plateau; we cap at 40 to bound per-vector cost.)
        n_lockable = int(self.cone_input_pi_idx.size)
        if self._phase1_rounds_arg is None:
            self.phase1_rounds = int(min(40, max(self.mutation_rounds,
                                                  int(np.ceil(n_lockable * 0.5)))))
        else:
            self.phase1_rounds = int(self._phase1_rounds_arg)
        if self._phase3_rounds_arg is None:
            # Phase 3 must be able to REPAIR the activity profile after
            # suppression, not just nudge it. Give it a budget comparable to
            # Phase 1 (the matching problem is at least as hard), capped.
            self.phase3_rounds = int(min(40, max(self.mutation_rounds,
                                                  self.phase1_rounds)))
        else:
            self.phase3_rounds = int(self._phase3_rounds_arg)

        logger.info(
            f"[DatasetA] Suspicious cone: {len(self.cone_nodes)} node(s) "
            f"(fanin_depth={cone_fanin_depth}, fanout_depth={cone_fanout_depth}) "
            f"out of {len(all_gates)} gates ({len(rare_nodes)} seed rare node(s)). "
            f"Background: {len(self.bg_nodes)} node(s)."
        )
        if self.dynamic_weights:
            sub = (f"subsample={self.max_candidates}/round"
                   + (" (auto)" if self._auto_subsample else "")
                   if self.max_candidates else "no subsampling (all bits/round)")
            logger.info(
                f"[DatasetA] Dynamic weight balancing ENABLED. "
                f"Cone-input PIs (lockable): {len(self.cone_input_pi_idx)} / "
                f"{self.n_pi} inputs; background PIs (free in Phase 3): "
                f"{len(self.bg_input_idx)}. "
                f"Phase1 rounds={self.phase1_rounds} (w_rare={self.phase1_weights['rare']}), "
                f"Phase3 rounds={self.phase3_rounds}, rare_threshold={self.rare_threshold}, "
                f"lock_mode={self.lock_mode}, {sub}."
            )


    # ------------------------------------------------------------------
    # Reference statistics + transition masks from Dataset B
    # ------------------------------------------------------------------

    def reference_stats(self, testset_B: list) -> dict:
        """
        Compute per-pair target statistics and transition masks from
        Dataset B.  B[-1] is treated as the all-zero vector (matching
        MERS's `tp` initialisation), so pair 0 is ([0...0], B[0]).

        Returns dict with parallel lists (length len(testset_B)):
          masks    : XOR(prev_B, curr_B) for each pair, as uint8 arrays
          target_bg, target_in_hd, target_in_hw, target_out_hd, target_out_hw,
          target_rare_cone_sw (B's own RareConeSwitch -- for reporting only)
        """
        n_pi = self.n_pi
        K = len(testset_B)
        vectors_full = [[0] * n_pi] + [list(v) for v in testset_B]
        M, _ = self.circuit.simulate_matrix(vectors_full)

        cone_full = M[self.cone_idx]   # (|cone|, K+1)
        bg_full   = M[self.bg_idx]     # (|bg|,   K+1)
        po_full   = M[self.po_idx]     # (|po|,   K+1)

        cone_prev, cone_curr = cone_full[:, :-1], cone_full[:, 1:]
        bg_prev,   bg_curr   = bg_full[:, :-1],   bg_full[:, 1:]
        po_prev,   po_curr   = po_full[:, :-1],   po_full[:, 1:]

        rare_cone_sw = (cone_curr != cone_prev).sum(axis=0).astype(np.int32)
        bg_sw        = (bg_curr   != bg_prev).sum(axis=0).astype(np.int32)
        out_hd       = (po_curr   != po_prev).sum(axis=0).astype(np.int32)
        out_hw       = po_curr.sum(axis=0).astype(np.int32)

        arr = np.asarray(vectors_full, dtype=np.uint8)
        in_prev, in_curr = arr[:-1], arr[1:]
        masks  = (in_prev ^ in_curr)
        in_hd  = masks.sum(axis=1).astype(np.int32)
        in_hw  = in_curr.sum(axis=1).astype(np.int32)

        return {
            'masks':              masks,         # (K, n_pi)
            'target_bg':          bg_sw,          # (K,)
            'target_in_hd':       in_hd,
            'target_in_hw':       in_hw,
            'target_out_hd':      out_hd,
            'target_out_hw':      out_hw,
            'B_rare_cone_sw':     rare_cone_sw,
        }


    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    def generate(self, testset_B: list, progress_mgr=None,
                  cache_key: str = 'dataset_a') -> dict:
        """
        Generate Dataset A, paired with Dataset B.

        Returns dict:
          testset          : list of A vectors, len == len(testset_B)
          per_pair_A        : per-pair stats for A (rare_cone_sw, bg_sw,
                               in_hd, in_hw, out_hd, out_hw)
          per_pair_B        : B's corresponding stats (same keys), for
                               side-by-side comparison/reporting
          cone_nodes, bg_nodes : the node sets used
        """
        if progress_mgr and progress_mgr.exists(cache_key):
            logger.info("[DatasetA] Loading cached Dataset A ...")
            return progress_mgr.load(cache_key)

        if not testset_B:
            return {'testset': [], 'per_pair_A': [], 'per_pair_B': [],
                    'cone_nodes': sorted(self.cone_nodes),
                    'bg_nodes':   sorted(self.bg_nodes)}

        ref = self.reference_stats(testset_B)
        n_pi = self.n_pi
        K = len(testset_B)
        idx = np.arange(n_pi)
        w = self.weights

        n_cone = max(1, len(self.cone_list))
        n_bg   = max(1, len(self.bg_list))
        n_po   = max(1, len(self.po_list))

        A = [np.zeros(n_pi, dtype=np.uint8)]  # A[0] index is "prev"; A vectors start at 1
        per_pair_A = []
        per_pair_B = []
        start_j = 0

        # Resume from a partial checkpoint if present.
        partial_key = f"_partial_{cache_key}"
        if progress_mgr and progress_mgr.exists(partial_key):
            st = progress_mgr.load(partial_key)
            if st.get('n_pi') == n_pi and st.get('K') == K:
                A = [np.zeros(n_pi, dtype=np.uint8)] + \
                    [np.asarray(v, dtype=np.uint8) for v in st['testset']]
                per_pair_A = st['per_pair_A']
                per_pair_B = st['per_pair_B']
                start_j = len(per_pair_A)
                logger.info(f"[DatasetA] Resuming from partial checkpoint at "
                            f"vector {start_j}/{K}")

        pbar = tqdm(total=K, initial=start_j, desc="  Dataset A", ncols=80)
        for j in range(start_j, K):
            prev_vec = A[-1]
            target = {
                'bg':     int(ref['target_bg'][j]),
                'in_hd':  int(ref['target_in_hd'][j]),
                'in_hw':  int(ref['target_in_hw'][j]),
                'out_hd': int(ref['target_out_hd'][j]),
                'out_hw': int(ref['target_out_hw'][j]),
            }
            per_pair_B.append({
                'rare_cone_sw': int(ref['B_rare_cone_sw'][j]),
                'bg_sw':        target['bg'],
                'in_hd':        target['in_hd'],
                'in_hw':        target['in_hw'],
                'out_hd':       target['out_hd'],
                'out_hw':       target['out_hw'],
            })

            # ---- Seed: apply B's own transition mask ----
            # Preserves input HD exactly (popcount of an XOR mask is
            # invariant to the base vector it's applied to).
            seed = (prev_vec ^ ref['masks'][j]).astype(np.uint8)

            if self.dynamic_weights:
                curr, best_stats = self._optimize_phased(
                    prev_vec, seed, target, n_cone, n_bg, n_po)
            else:
                curr, best_stats = self._optimize_flat(
                    prev_vec, seed, target, n_cone, n_bg, n_po, idx)

            A.append(curr)
            per_pair_A.append(best_stats)
            pbar.update(1)

            # Periodic checkpoint (every 250 vectors) so a long run can resume.
            if progress_mgr and (j + 1) % 250 == 0 and (j + 1) < K:
                progress_mgr.save(partial_key, {
                    'testset':    [v.tolist() for v in A[1:]],
                    'per_pair_A': per_pair_A,
                    'per_pair_B': per_pair_B,
                    'n_pi': n_pi, 'K': K,
                })
        pbar.close()

        result = {
            'testset':    [v.tolist() for v in A[1:]],
            'per_pair_A': per_pair_A,
            'per_pair_B': per_pair_B,
            'cone_nodes': sorted(self.cone_nodes),
            'bg_nodes':   sorted(self.bg_nodes),
            'weights':    dict(self.weights),
            'dynamic_weights': self.dynamic_weights,
            'solver_info': ({
                'mode': 'phased',
                'phase1_weights': dict(self.phase1_weights),
                'phase3_weights': dict(self.phase3_weights),
                'phase1_rounds': self.phase1_rounds,
                'phase3_rounds': self.phase3_rounds,
                'rare_threshold': self.rare_threshold,
                'n_cone_input_pis_locked': int(self.cone_input_pi_idx.size),
                'n_background_pis_free': int(self.bg_input_idx.size),
            } if self.dynamic_weights else {
                'mode': 'flat',
                'mutation_rounds': self.mutation_rounds,
            }),
        }
        logger.info(f"[DatasetA] Done — {len(result['testset'])} vectors")

        if progress_mgr:
            progress_mgr.save(cache_key, result)
            if progress_mgr.exists(partial_key):
                progress_mgr.delete(partial_key)

        return result

    # ------------------------------------------------------------------
    # Candidate evaluation + per-vector solvers
    # ------------------------------------------------------------------

    def _eval_candidates(self, prev_vec, cand_mat):
        """
        Simulate `cand_mat` (C, n_pi) candidate vectors against the fixed
        `prev_vec`, and return per-candidate raw statistics as a dict of
        (C,) int arrays: rare_cone_sw, bg_sw, in_hd, in_hw, out_hd, out_hw,
        state_hd.

        One simulate_matrix() call over [prev_vec; cand_mat], then vectorised
        slices via the precomputed cone/bg/po row-index arrays.
        """
        C = cand_mat.shape[0]
        vectors_full = np.vstack([prev_vec[None, :], cand_mat])
        M, _ = self.circuit.simulate_matrix(vectors_full)

        cone_full = M[self.cone_idx]
        bg_full   = M[self.bg_idx]
        po_full   = M[self.po_idx]

        cone_prev, cone_cand = cone_full[:, 0], cone_full[:, 1:]
        bg_prev,   bg_cand   = bg_full[:, 0],   bg_full[:, 1:]
        po_prev,   po_cand   = po_full[:, 0],   po_full[:, 1:]

        rare_cone_sw = (cone_cand != cone_prev[:, None]).sum(axis=0).astype(np.int32)
        bg_sw        = (bg_cand   != bg_prev[:, None]).sum(axis=0).astype(np.int32)
        out_hd       = (po_cand   != po_prev[:, None]).sum(axis=0).astype(np.int32)
        out_hw       = po_cand.sum(axis=0).astype(np.int32)

        in_masks = (prev_vec[None, :] ^ cand_mat)
        in_hd    = in_masks.sum(axis=1).astype(np.int32)
        in_hw    = cand_mat.sum(axis=1).astype(np.int32)

        # state_hd: switching of scan-state (DFF) outputs. In full-scan mode
        # these are the dff_nodes (which are also scan PIs). We measure the
        # NEXT-state side via the gate driving each D, i.e. the scan-PO; but
        # since dff next-state already lives in po_list, we approximate
        # state_hd by the switching of dff_nodes' driving logic captured in
        # out_hd. We expose a dedicated term only when w_state>0 by counting
        # the dff next-state POs specifically.
        if self._state_idx is not None and self._state_idx.size:
            st_full = M[self._state_idx]
            st_prev, st_cand = st_full[:, 0], st_full[:, 1:]
            state_hd = (st_cand != st_prev[:, None]).sum(axis=0).astype(np.int32)
        else:
            state_hd = np.zeros(C, dtype=np.int32)

        return {
            'rare_cone_sw': rare_cone_sw, 'bg_sw': bg_sw,
            'in_hd': in_hd, 'in_hw': in_hw,
            'out_hd': out_hd, 'out_hw': out_hw, 'state_hd': state_hd,
        }

    def _score_array(self, stats, target, n_cone, n_bg, n_po, w):
        """Weighted objective over arrays of candidate stats (higher=better)."""
        n_pi = max(1, self.n_pi)
        n_st = max(1, self._n_state)
        return (
            -w['rare']   * stats['rare_cone_sw'] / n_cone
            -w['bg']     * np.abs(stats['bg_sw']  - target['bg'])    / n_bg
            -w['in_hd']  * np.abs(stats['in_hd']  - target['in_hd']) / n_pi
            -w['in_hw']  * np.abs(stats['in_hw']  - target['in_hw']) / n_pi
            -w['out_hd'] * np.abs(stats['out_hd'] - target['out_hd'])/ n_po
            -w['out_hw'] * np.abs(stats['out_hw'] - target['out_hw'])/ n_po
            -w.get('state', 0.0) * np.abs(stats['state_hd'] - target.get('state', 0)) / n_st
        )

    def _stats_at(self, stats, i):
        """Extract one candidate's stats (drop state_hd unless used)."""
        out = {k: int(stats[k][i]) for k in
               ('rare_cone_sw', 'bg_sw', 'in_hd', 'in_hw', 'out_hd', 'out_hw')}
        return out

    def _optimize_flat(self, prev_vec, seed, target, n_cone, n_bg, n_po, idx):
        """Original single-phase best-flip search over ALL input bits."""
        w = self.weights
        curr = seed.copy()
        st0 = self._eval_candidates(prev_vec, curr[None, :])
        best_score = float(self._score_array(st0, target, n_cone, n_bg, n_po, w)[0])
        best_stats = self._stats_at(st0, 0)
        n_pi = self.n_pi

        for _round in range(self.mutation_rounds):
            muts = np.tile(curr, (n_pi, 1))
            muts[idx, idx] ^= 1
            st = self._eval_candidates(prev_vec, muts)
            scores = self._score_array(st, target, n_cone, n_bg, n_po, w)
            best_bit = int(np.argmax(scores))
            if scores[best_bit] > best_score:
                best_score = float(scores[best_bit])
                curr = muts[best_bit]
                best_stats = self._stats_at(st, best_bit)
            else:
                break
        return curr, best_stats

    def _best_flip_round(self, prev_vec, curr, cand_idx, target,
                          n_cone, n_bg, n_po, w, best_score, best_stats):
        """
        One best-flip round restricted to candidate input positions
        `cand_idx`. Returns (curr, best_score, best_stats, improved, rare_now).
        Evaluating only a subset of bit positions per round (instead of all
        n_pi) is the main speed lever on large circuits: simulate_matrix cost
        scales ~linearly with the candidate count.
        """
        F = cand_idx.size
        muts = np.tile(curr, (F, 1))
        muts[np.arange(F), cand_idx] ^= 1
        st = self._eval_candidates(prev_vec, muts)
        scores = self._score_array(st, target, n_cone, n_bg, n_po, w)
        best_bit = int(np.argmax(scores))
        if scores[best_bit] > best_score:
            return (muts[best_bit], float(scores[best_bit]),
                    self._stats_at(st, best_bit), True,
                    int(st['rare_cone_sw'][best_bit]))
        return curr, best_score, best_stats, False, None

    def _sample_candidates(self, pool):
        """
        Pick which input positions to try this round. If max_candidates is
        set and smaller than the pool, draw a random subset (different each
        round, so over several rounds all bits get chances); otherwise use
        the whole pool.
        """
        if self.max_candidates and pool.size > self.max_candidates:
            return self.rng.choice(pool, size=self.max_candidates, replace=False)
        return pool

    def _optimize_phased(self, prev_vec, seed, target, n_cone, n_bg, n_po):
        """
        Dynamic weight balancing solver.

        Phase 1 (rare-cone suppression): best-flip over input bits with
            phase1_weights (rare dominates BUT matching terms stay active),
            until RareConeSwitch <= rare_threshold or phase1_rounds exhausted.
        Phase 2 (bit locking): governed by self.lock_mode --
            'hard' : Phase 3 flips only background (non-cone-input) PIs.
            'soft' : Phase 3 flips ALL PIs, rare term active to keep the cone
                     quiet (default; robust when cone-input PIs also drive
                     the outputs, as on s38417).
            'none' : Phase 3 flips all PIs, behaves like flat with w3.
        Phase 3 (statistical matching): best-flip with phase3_weights.

        Speed: when max_candidates is set, each round evaluates a random
        subset of that many bit positions rather than all of them -- a
        proportional speedup on large circuits (n_pi in the thousands).
        no_improve_patience stops a phase after that many consecutive
        non-improving rounds (with subsampling, one bad round no longer
        means "done").
        """
        w1, w3 = self.phase1_weights, self.phase3_weights
        n_pi = self.n_pi
        all_idx = np.arange(n_pi)

        # ---------- Phase 1 ----------
        curr = seed.copy()
        st0 = self._eval_candidates(prev_vec, curr[None, :])
        best_score = float(self._score_array(st0, target, n_cone, n_bg, n_po, w1)[0])
        best_stats = self._stats_at(st0, 0)
        cur_rare = int(st0['rare_cone_sw'][0])

        stale = 0
        for _round in range(self.phase1_rounds):
            if cur_rare <= self.rare_threshold:
                break
            cand = self._sample_candidates(all_idx)
            curr, best_score, best_stats, improved, rare_now = self._best_flip_round(
                prev_vec, curr, cand, target, n_cone, n_bg, n_po, w1,
                best_score, best_stats)
            if improved:
                cur_rare = rare_now
                stale = 0
            else:
                stale += 1
                if stale >= self.no_improve_patience:
                    break

        # ---------- Phase 2: choose which bits Phase 3 may flip ----------
        if self.lock_mode == 'hard':
            free_pool = self.bg_input_idx
        else:
            free_pool = all_idx
        if free_pool.size == 0:
            return curr, best_stats

        # ---------- Phase 3 ----------
        st_c = self._eval_candidates(prev_vec, curr[None, :])
        best_score = float(self._score_array(st_c, target, n_cone, n_bg, n_po, w3)[0])
        best_stats = self._stats_at(st_c, 0)

        stale = 0
        for _round in range(self.phase3_rounds):
            cand = self._sample_candidates(free_pool)
            curr, best_score, best_stats, improved, _ = self._best_flip_round(
                prev_vec, curr, cand, target, n_cone, n_bg, n_po, w3,
                best_score, best_stats)
            if improved:
                stale = 0
            else:
                stale += 1
                if stale >= self.no_improve_patience:
                    break
        return curr, best_stats

    # ------------------------------------------------------------------
    def _score(self, prev_vec, curr_vec, target, n_cone, n_bg, n_po, w):
        """Score a single (prev,curr) pair (kept for backward compat/tests)."""
        st = self._eval_candidates(prev_vec, np.asarray(curr_vec, dtype=np.uint8)[None, :])
        score = float(self._score_array(st, target, n_cone, n_bg, n_po, w)[0])
        return score, self._stats_at(st, 0)
