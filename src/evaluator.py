"""
evaluator.py  (v5 paper-gate Trojan model)
===========================================
Evaluates testsets against randomly placed Hardware Trojans using the
side-channel switching metrics from Huang et al., CCS 2016.

Important v5 fixes
------------------
1. DeltaSwitch is no longer approximated as
       trigger_node_switches + final_payload_switch.
   That approximation under-counted the extra switching caused by the
   inserted Trojan circuit.

   v5 builds a vectorised gate-level Trojan model for each random Trojan:
     * optional inverters for trigger nodes whose rare value is 0;
     * a balanced AND tree for the trigger condition;
     * one XOR-style payload effect node.

   DeltaSwitch is computed from the switching of these inserted Trojan
   gates plus the payload-output switching difference relative to the
   golden circuit.

2. TotalSwitch remains the paper-style golden-circuit switching count:
   internal gate outputs only. Primary input transitions are not counted
   in the denominator.

3. Evaluation cache keys include "v5" so old v4 results are not reused.
"""

from __future__ import annotations

import random
import logging
import numpy as np
from tqdm import tqdm

logger = logging.getLogger(__name__)


class TrojanEvaluator:
    """
    Parameters
    ----------
    circuit     : Circuit (golden, no Trojan)
    rare_nodes  : dict from RareNodeFinder
    num_trojans : number of random Trojan instances to sample
    rng_seed    : reproducibility
    """

    def __init__(self, circuit, rare_nodes: dict,
                 num_trojans: int = 1000, rng_seed: int = 99):
        self.circuit     = circuit
        self.rare_nodes  = rare_nodes
        self.num_trojans = num_trojans
        self.rng_seed    = rng_seed

        # Existing internal nodes are useful payload attachment candidates.
        self.gate_nodes = list(getattr(circuit, 'gates', {}).keys())
        if not self.gate_nodes:
            self.gate_nodes = [n for n in getattr(circuit, 'eval_order', [])]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, testset: list, num_triggers: int = 8,
                 progress_mgr=None, label: str = '') -> dict:
        """
        Evaluate testset against num_trojans random Trojans.
        Returns dict with avg_max_relative (SCS), avg_avg_relative, etc.
        """
        CACHE_KEY = f"eval_v5_papergates_{label}_{num_triggers}trig"
        if progress_mgr and progress_mgr.exists(CACHE_KEY):
            logger.info(f"[Eval] Loading cached v5 evaluation '{label}' ...")
            return progress_mgr.load(CACHE_KEY)

        logger.info(f"[Eval] '{label}': {len(testset):,} vectors x "
                    f"{self.num_trojans} Trojans ({num_triggers}-trigger) ...")

        random.seed(self.rng_seed)
        rare_list = list(self.rare_nodes.keys())

        if len(rare_list) < num_triggers:
            logger.warning(f"  Only {len(rare_list)} rare nodes; "
                           f"cannot form {num_triggers}-trigger Trojan. Skipping.")
            return {}

        # ---- Precompute all testset states in one batch ----
        n_pi = self.circuit.n_inputs()
        logger.info("[Eval] Pre-computing batch simulation for testset ...")
        vectors_full = [[0] * n_pi] + [list(v) for v in testset]
        batch_full   = self.circuit.simulate_batch(vectors_full)
        K = len(testset)

        batch_prev = {n: arr[:-1] for n, arr in batch_full.items()}
        batch_curr = {n: arr[1:]  for n, arr in batch_full.items()}

        # ---- Precompute TotalSwitch per pair (GATE OUTPUTS ONLY) ----
        total_sw_per_pair = self._golden_total_switch(batch_prev, batch_curr, K)

        logger.info("[Eval] Batch ready. Evaluating Trojans with v5 paper-gate model ...")

        per_trojan = []
        for _ in tqdm(range(self.num_trojans), desc=f"  {label}", ncols=80):
            trojan  = self._sample_trojan(rare_list, num_triggers)
            metrics = self._evaluate_one_trojan(
                batch_prev, batch_curr, total_sw_per_pair, trojan, K)
            per_trojan.append(metrics)

        result = self._aggregate(per_trojan, len(testset), num_triggers)

        if progress_mgr:
            progress_mgr.save(CACHE_KEY, result)
        return result

    # ------------------------------------------------------------------
    # Golden circuit switching
    # ------------------------------------------------------------------

    def _golden_total_switch(self, batch_prev, batch_curr, K: int) -> np.ndarray:
        """Paper-style TotalSwitch: internal gate outputs only."""
        total = np.zeros(K, dtype=np.int32)
        gate_nodes = set(getattr(self.circuit, 'gates', {}).keys())
        if not gate_nodes:
            # Fallback: count non-PI, non-constant nodes.
            try:
                from src.circuit_parser import _CONSTANT_NODES
                excluded = set(self.circuit.primary_inputs) | set(_CONSTANT_NODES.keys())
            except Exception:
                excluded = set(self.circuit.primary_inputs)
            gate_nodes = set(batch_curr.keys()) - excluded

        for node in gate_nodes:
            curr = batch_curr.get(node)
            prev = batch_prev.get(node)
            if curr is None or prev is None:
                continue
            total += (curr != prev).astype(np.int32)
        return total

    # ------------------------------------------------------------------
    # Trojan model
    # ------------------------------------------------------------------

    def _sample_trojan(self, rare_list: list, num_triggers: int) -> dict:
        trigger_nodes = random.sample(rare_list, num_triggers)
        excluded = set(trigger_nodes)
        payload_candidates = [n for n in self.gate_nodes if n not in excluded]
        payload_node = random.choice(payload_candidates or self.gate_nodes or trigger_nodes)
        return {
            'trigger_nodes':  trigger_nodes,
            'trigger_values': [self.rare_nodes[n]['rare_value']
                               for n in trigger_nodes],
            'payload_node':   payload_node,
        }

    def _evaluate_one_trojan(self, batch_prev, batch_curr,
                              total_sw, trojan, K) -> dict:
        """
        Vectorised per-Trojan evaluation.

        Inserted Trojan gate model:
          trigger_i = node_i                  if rare_value_i == 1
                    = NOT(node_i)             if rare_value_i == 0
          trigger   = AND(trigger_1, ..., trigger_k)  using a balanced tree
          payload*  = payload XOR trigger

        DeltaSwitch is the inserted trigger-network switching plus the
        payload-output switching difference against the golden payload node.
        """
        tnodes = trojan['trigger_nodes']
        tvals  = trojan['trigger_values']
        zeros  = np.zeros(K, dtype=np.uint8)

        extra_sw = np.zeros(K, dtype=np.int32)
        prev_signals = []
        curr_signals = []

        for n, rare_value in zip(tnodes, tvals):
            pv = batch_prev.get(n, zeros)
            cv = batch_curr.get(n, zeros)

            if rare_value == 1:
                # Direct input to the AND tree. No inserted inverter gate.
                tp = pv
                tc = cv
            else:
                # A rare-0 trigger requires an inserted inverter gate.
                tp = 1 - pv
                tc = 1 - cv
                extra_sw += (tp != tc).astype(np.int32)

            prev_signals.append(tp.astype(np.uint8, copy=False))
            curr_signals.append(tc.astype(np.uint8, copy=False))

        # Balanced AND tree. Every AND output is an inserted Trojan gate.
        trig_prev, trig_curr, and_sw = self._balanced_and_tree_switch(
            prev_signals, curr_signals, K)
        extra_sw += and_sw

        # Payload effect. Count only the switching DIFFERENCE relative to the
        # golden payload node; this avoids charging the Trojan for normal
        # payload switching when trigger is constantly 0.
        pnode = trojan['payload_node']
        pprev = batch_prev.get(pnode, zeros)
        pcurr = batch_curr.get(pnode, zeros)
        golden_payload_sw   = (pcurr != pprev)
        infected_payload_sw = ((pcurr ^ trig_curr) != (pprev ^ trig_prev))
        payload_delta_sw = (infected_payload_sw != golden_payload_sw).astype(np.int32)

        delta_sw = extra_sw + payload_delta_sw
        rel_sw   = delta_sw.astype(np.float64) / np.maximum(total_sw, 1)

        return {
            'max_delta':    int(delta_sw.max()),
            'avg_delta':    float(delta_sw.mean()),
            'max_relative': float(rel_sw.max()),
            'avg_relative': float(rel_sw.mean()),
        }

    @staticmethod
    def _balanced_and_tree_switch(prev_signals, curr_signals, K: int):
        """Return final trigger prev/curr and per-pair switching of AND gates."""
        if not curr_signals:
            z = np.zeros(K, dtype=np.uint8)
            return z, z, np.zeros(K, dtype=np.int32)

        prev_level = list(prev_signals)
        curr_level = list(curr_signals)
        and_sw = np.zeros(K, dtype=np.int32)

        while len(curr_level) > 1:
            next_prev = []
            next_curr = []
            i = 0
            while i < len(curr_level):
                if i + 1 < len(curr_level):
                    po = (prev_level[i] & prev_level[i + 1]).astype(np.uint8)
                    co = (curr_level[i] & curr_level[i + 1]).astype(np.uint8)
                    and_sw += (po != co).astype(np.int32)
                    next_prev.append(po)
                    next_curr.append(co)
                    i += 2
                else:
                    # Odd input passes to next tree level without a new gate.
                    next_prev.append(prev_level[i])
                    next_curr.append(curr_level[i])
                    i += 1
            prev_level = next_prev
            curr_level = next_curr

        return prev_level[0], curr_level[0], and_sw

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    @staticmethod
    def _aggregate(per_trojan, testset_size, num_triggers):
        if not per_trojan:
            return {}
        arr = {k: np.array([d[k] for d in per_trojan]) for k in per_trojan[0]}
        return {
            'avg_max_delta':    float(arr['max_delta'].mean()),
            'avg_avg_delta':    float(arr['avg_delta'].mean()),
            'avg_max_relative': float(arr['max_relative'].mean()),
            'avg_avg_relative': float(arr['avg_relative'].mean()),
            'num_trojans':      len(per_trojan),
            'testset_size':     testset_size,
            'num_triggers':     num_triggers,
            'trojan_model':     'v5_paper_gates',
            'per_trojan':       per_trojan,
        }
