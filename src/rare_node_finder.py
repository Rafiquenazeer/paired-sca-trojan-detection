"""
rare_node_finder.py  (v6)
==========================
Finds rare nodes via Monte-Carlo simulation.

v6 updates
----------
* Uses simulate_batch() for fast Monte-Carlo simulation.
* Excludes stuck nodes (P=0 or P=1, or unresolved gates in circuit).
* Excludes synthetic converter artefact nodes from rare-node candidacy.
  This is important for Xilinx LUT/FDRE BENCH files generated from LUT
  expansion, where helper nodes such as __LUTTERM_*, __NOT_*, and __CONST*
  are implementation artefacts, not meaningful benchmark rare nodes.
"""

from __future__ import annotations

import logging
import numpy as np
from tqdm import tqdm

logger = logging.getLogger(__name__)
MIN_STUCK_SAMPLES = 100   # need ≥ this many samples to declare a node "stuck"

# Nodes created by the Xilinx LUT/FDRE-to-BENCH converter begin with "__".
# They must remain in the circuit for correct simulation, but they should not
# be selected as MERS/MERO rare-node targets.
DEFAULT_ARTIFACT_PREFIXES = ("__",)


class RareNodeFinder:
    def __init__(self, circuit, rare_threshold=0.1,
                 num_vectors=10_000, seed=42, exclude_stuck=True,
                 exclude_artifact_nodes=True,
                 artifact_prefixes=None):
        self.circuit                = circuit
        self.rare_threshold         = rare_threshold
        self.num_vectors            = num_vectors
        self.seed                   = seed
        self.exclude_stuck          = exclude_stuck
        self.exclude_artifact_nodes = bool(exclude_artifact_nodes)
        self.artifact_prefixes      = tuple(artifact_prefixes or DEFAULT_ARTIFACT_PREFIXES)

    def _is_artifact_node(self, node_name: str) -> bool:
        """True for synthetic helper nodes created during netlist conversion."""
        if not self.exclude_artifact_nodes:
            return False
        return str(node_name).startswith(self.artifact_prefixes)

    def _cache_key(self) -> str:
        """Versioned cache key so old rare-node checkpoints are not reused."""
        pfx = "none" if not self.exclude_artifact_nodes else "_".join(
            p.replace("_", "u") or "empty" for p in self.artifact_prefixes
        )
        return (f"rare_nodes_v6_T{self.rare_threshold:g}_V{self.num_vectors}"
                f"_stuck{int(self.exclude_stuck)}_art{int(self.exclude_artifact_nodes)}_{pfx}")

    def find_rare_nodes(self, progress_mgr=None, force_recompute=False):
        CACHE_KEY = self._cache_key()
        if progress_mgr and not force_recompute and progress_mgr.exists(CACHE_KEY):
            logger.info("[RareNodeFinder] Loading cached v6 rare-node data ...")
            result = progress_mgr.load(CACHE_KEY)
            logger.info(f"  -> {len(result['rare_nodes'])} rare nodes "
                        f"/ {result['total_nodes']} total")
            return result

        logger.info(f"[RareNodeFinder] Simulating {self.num_vectors:,} random vectors "
                    f"(threshold={self.rare_threshold}) ...")
        if self.exclude_artifact_nodes:
            logger.info("[RareNodeFinder] Artifact-node filter enabled: excluding "
                        f"rare-node candidates with prefixes {self.artifact_prefixes}")

        np.random.seed(self.seed)
        n_pi = self.circuit.n_inputs()
        CHUNK = 5_000
        count_ones  = {}
        total_count = {}

        remaining = self.num_vectors
        pbar = tqdm(total=self.num_vectors, desc="  Monte-Carlo", ncols=80)
        while remaining > 0:
            chunk_size = min(CHUNK, remaining)
            vecs  = np.random.randint(0, 2, size=(chunk_size, n_pi), dtype=np.uint8)
            batch = self.circuit.simulate_batch(vecs)
            for node, vals in batch.items():
                count_ones[node]  = count_ones.get(node, 0)  + int(vals.sum())
                total_count[node] = total_count.get(node, 0) + chunk_size
            remaining -= chunk_size
            pbar.update(chunk_size)
        pbar.close()

        node_probs = {
            node: count_ones[node] / total_count[node]
            for node in total_count if total_count[node] > 0
        }

        from src.circuit_parser import _CONSTANT_NODES
        pi_set       = set(self.circuit.primary_inputs) | set(_CONSTANT_NODES.keys())
        unresolved   = set(getattr(self.circuit, 'unresolved_gates', []))
        rare_nodes   = {}
        stuck_nodes  = {}
        artifact_nodes = {}

        for node, p1 in node_probs.items():
            # Never classify PIs/scan DFF outputs/constants as rare internal targets.
            if node in pi_set:
                continue

            # Keep synthetic helper nodes in simulation, but remove them from
            # rare-node selection.  This avoids inflated rare-node counts such as
            # thousands of __LUTTERM_* minterms in expanded FPGA LUT netlists.
            if self._is_artifact_node(node):
                artifact_nodes[node] = p1
                continue

            is_stuck = (
                self.exclude_stuck and (
                    node in unresolved
                    or (
                        (p1 == 0.0 or p1 == 1.0)
                        and total_count.get(node, 0) >= MIN_STUCK_SAMPLES
                    )
                )
            )
            if is_stuck:
                stuck_nodes[node] = p1
                continue

            if p1 <= self.rare_threshold:
                rare_nodes[node] = {'probability': round(p1, 6),
                                    'rare_value': 1, 'non_rare_value': 0}
            elif (1.0 - p1) <= self.rare_threshold:
                rare_nodes[node] = {'probability': round(1.0 - p1, 6),
                                    'rare_value': 0, 'non_rare_value': 1}

        if artifact_nodes:
            logger.warning(
                f"[RareNodeFinder] EXCLUDED {len(artifact_nodes)} synthetic artefact node(s) "
                f"from rare-node selection. These nodes remain in simulation but are "
                f"not valid MERS targets.\n"
                f"  Artifact sample: {list(artifact_nodes.keys())[:20]}"
            )

        if stuck_nodes:
            logger.warning(
                f"[RareNodeFinder] EXCLUDED {len(stuck_nodes)} 'stuck' nodes "
                f"(P=0 or P=1 — likely constants or unresolved gates).\n"
                f"  Stuck nodes: {list(stuck_nodes.keys())[:20]}\n"
                f"  If using USC benchmarks, get originals from:\n"
                f"  https://people.engr.ncsu.edu/brglez/CBL/software/ISCAS85/"
            )

        r1 = sum(1 for v in rare_nodes.values() if v['rare_value'] == 1)
        r0 = len(rare_nodes) - r1
        logger.info(f"[RareNodeFinder] Found {len(rare_nodes)} rare nodes  "
                    f"(Rare-1: {r1}, Rare-0: {r0})  "
                    f"[excluded {len(stuck_nodes)} stuck, "
                    f"{len(artifact_nodes)} artefact]")

        result = {
            'rare_nodes':             rare_nodes,
            'node_probs':             node_probs,
            'total_nodes':            len(node_probs),
            'stuck_nodes':            stuck_nodes,
            'artifact_nodes':         artifact_nodes,
            'artifact_prefixes':      list(self.artifact_prefixes),
            'exclude_artifact_nodes': self.exclude_artifact_nodes,
            'num_vectors':            self.num_vectors,
            'rare_threshold':         self.rare_threshold,
            'cache_key':              CACHE_KEY,
        }
        if progress_mgr:
            progress_mgr.save(CACHE_KEY, result)
        return result

    @staticmethod
    def sort_vectors_by_rare_coverage(vectors, rare_nodes, circuit):
        if not vectors:
            return []
        rare_names  = list(rare_nodes.keys())
        rare_values = np.array([rare_nodes[n]['rare_value'] for n in rare_names],
                               dtype=np.uint8)
        CHUNK = 2_000
        all_scores = []
        pbar = tqdm(total=len(vectors), desc="  Scoring vectors", ncols=80)
        for start in range(0, len(vectors), CHUNK):
            chunk = vectors[start:start + CHUNK]
            batch = circuit.simulate_batch(chunk)
            rare_mat = np.column_stack(
                [batch.get(n, np.zeros(len(chunk), dtype=np.uint8))
                 for n in rare_names])
            scores = (rare_mat == rare_values[np.newaxis, :]).sum(axis=1)
            all_scores.extend(scores.tolist())
            pbar.update(len(chunk))
        pbar.close()
        scored = sorted(zip(all_scores, range(len(vectors))), key=lambda x: -x[0])
        return [(s, vectors[i]) for s, i in scored]
