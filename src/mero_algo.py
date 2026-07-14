"""
mero_algo.py  (v5)
==================
A practical MERO-style baseline for comparison with MERS.

The Huang et al. MERS paper compares against MERO with N=1000.  MERO is
activation-oriented: it tries to make every rare node take its rare value
at least N times.  MERS is switching-oriented: it tries to create
non_rare -> rare transitions at least N times.

This implementation is intentionally simple and reproducible:
  1. Generate the same-size random input pool.
  2. Sort vectors by rare-value coverage.
  3. Greedily mutate each vector to increase rare-value activation count for
     still-unsatisfied rare nodes.
  4. Add a vector if it increases at least one activation counter.

It is not claimed to be a bit-for-bit reproduction of the original MERO C
implementation, but it provides the missing activation-based baseline using
exactly the same circuit/parser/evaluator infrastructure as this project.
"""

from __future__ import annotations

import random
import logging
import numpy as np
from tqdm import tqdm

logger = logging.getLogger(__name__)


class MERO:
    """MERO-style rare-node activation baseline."""

    def __init__(self, circuit, N: int = 1000, rng_seed: int = 123,
                 mutation_rounds: int = 8):
        self.circuit = circuit
        self.N = int(N)
        self.rng_seed = rng_seed
        self.mutation_rounds = int(mutation_rounds)

    def generate(self, rare_nodes_info: dict,
                 num_random_vectors: int = 10_000,
                 progress_mgr=None) -> dict:
        from src.rare_node_finder import RareNodeFinder

        CACHE_KEY = f"mero_v6_testset_N{self.N}_r{self.mutation_rounds}"
        if progress_mgr and progress_mgr.exists(CACHE_KEY):
            logger.info(f"[MERO] Loading cached v6 testset (N={self.N}) ...")
            return progress_mgr.load(CACHE_KEY)

        rare_nodes = rare_nodes_info['rare_nodes']
        n_pi = self.circuit.n_inputs()
        random.seed(self.rng_seed)
        np.random.seed(self.rng_seed)

        logger.info(f"[MERO] Generating {num_random_vectors:,} random vectors ...")
        random_vectors = [
            [random.randint(0, 1) for _ in range(n_pi)]
            for _ in range(num_random_vectors)
        ]

        logger.info("[MERO] Sorting by rare-value activation coverage ...")
        scored = RareNodeFinder.sort_vectors_by_rare_coverage(
            random_vectors, rare_nodes, self.circuit)
        sorted_vectors = [v for _, v in scored]

        counters = {n: 0 for n in rare_nodes}
        testset = []
        idx = np.arange(n_pi)

        pbar = tqdm(total=len(sorted_vectors), desc="  MERO vectors", ncols=80)
        for v in sorted_vectors:
            mutated, state_mut = self._mutate_for_activation(
                v, rare_nodes, counters, n_pi, idx)

            activated = _activated_unsatisfied(state_mut, rare_nodes, counters, self.N)
            if activated or len(testset) == 0:
                testset.append(mutated)
                for node in activated:
                    counters[node] += 1

            pbar.update(1)
            if all(vv >= self.N for vv in counters.values()):
                logger.info("\n[MERO] All nodes satisfied N=%d", self.N)
                break
        pbar.close()

        # Keep at least enough vectors for stable evaluation on small circuits.
        MIN_SIZE = max(100, len(rare_nodes))
        if len(testset) < MIN_SIZE:
            existing = set(map(tuple, testset))
            for vec in sorted_vectors:
                if len(testset) >= MIN_SIZE:
                    break
                key = tuple(vec)
                if key not in existing:
                    testset.append(list(vec))
                    existing.add(key)

        result = {
            'testset': testset,
            'activation_counters': counters,
            'size': len(testset),
            'mutation_rounds': self.mutation_rounds,
        }
        logger.info(f"[MERO] Done — testset: {len(testset)} vectors")
        if progress_mgr:
            progress_mgr.save(CACHE_KEY, result)
        return result

    def _mutate_for_activation(self, v, rare_nodes, counters, n_pi, idx):
        state_v = self.circuit.simulate(v)
        score_now = _activation_score(state_v, rare_nodes, counters, self.N)
        current = np.array(v, dtype=np.uint8)
        state_current = state_v

        for _ in range(max(1, self.mutation_rounds)):
            muts = np.tile(current, (n_pi, 1))
            muts[idx, idx] ^= 1
            batch = self.circuit.simulate_batch(muts)
            scores = _batch_activation_score(batch, rare_nodes, counters, self.N, n_pi)
            best_bit = int(np.argmax(scores))
            if scores[best_bit] > score_now:
                current[best_bit] ^= 1
                state_current = {node: int(arr[best_bit]) for node, arr in batch.items()}
                score_now = int(scores[best_bit])
            else:
                break
        return current.tolist(), state_current


def _activation_score(state, rare_nodes, counters, N):
    count = 0
    for node, info in rare_nodes.items():
        if counters.get(node, 0) < N and state.get(node, 0) == info['rare_value']:
            count += 1
    return count


def _batch_activation_score(batch, rare_nodes, counters, N, K):
    counts = np.zeros(K, dtype=np.int32)
    for node, info in rare_nodes.items():
        if counters.get(node, 0) >= N or node not in batch:
            continue
        counts += (batch[node] == info['rare_value']).astype(np.int32)
    return counts


def _activated_unsatisfied(state, rare_nodes, counters, N):
    return [
        node for node, info in rare_nodes.items()
        if counters.get(node, 0) < N and state.get(node, 0) == info['rare_value']
    ]
