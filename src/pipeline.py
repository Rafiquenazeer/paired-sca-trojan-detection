"""
pipeline.py
===========
Full end-to-end MERS pipeline.  Runs each stage in order and checkpoints
after every stage so that restarting the script resumes from wherever
it was interrupted.

Pipeline stages
---------------
  1. Parse circuit netlist
  2. Find rare nodes  (cached → rare_nodes.pkl)
  3. Export rare-node Excel report
  4. Generate random baseline testset  (cached → random_testset.pkl)
  5. Generate MERO baseline (optional)
  6. Run MERS core (Algorithm 1)
  7. MERS-h reordering (Algorithm 2)
  8. MERS-s reordering (Algorithm 3)
  9. Export generated test vectors (.txt and .xlsx)
 10. Evaluate Random / MERO / MERS / MERS-h / MERS-s
 11. Export sensitivity Excel report

Usage
-----
  from src.pipeline import MERSPipeline

  pipe = MERSPipeline(
      bench_file   = "benchmarks/c2670.bench",
      circuit_name = "c2670",
      N            = 1000,
  )
  pipe.run()
"""

import random
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class MERSPipeline:
    """
    Parameters
    ----------
    bench_file       : path to the .bench netlist
    circuit_name     : short name (used for filenames and cache keys)
    N                : rare-switching requirement per node (default 1000)
    rare_threshold   : node rarity threshold (default 0.1)
    num_rand_vectors : size of initial random pool (default 10 000)
    trigger_counts   : list of trigger-count sizes to evaluate (default [4, 8])
    C                : weight ratio for MERS-s (default 5)
    num_trojans      : Trojans sampled per evaluation (default 1000)
    save_dir         : root directory for checkpoints
    results_dir      : directory for Excel outputs
    skip_mero        : skip MERO baseline (default False)
    skip_mers_h      : skip Hamming reordering (default False)
    skip_mers_s      : skip simulation reordering (default False)
    mutation_mode    : "paper" or "fast" for MERS Algorithm-1 mutation
    mutation_rounds  : fast-mode best-flip rounds
    full_scan        : treat DFFs as scan PIs (default True)
    export_vectors   : automatically export test vectors to .txt and .xlsx
    vectors_dir      : root directory for exported test vectors
    exclude_artifact_nodes : exclude converter helper nodes from rare targets
    artifact_prefixes      : prefixes for helper nodes to exclude
    """

    def __init__(self,
                 bench_file:       str,
                 circuit_name:     str   = 'circuit',
                 N:                int   = 1000,
                 rare_threshold:   float = 0.1,
                 num_rand_vectors: int   = 10_000,
                 rare_vectors:     int   = None,
                 trigger_counts:   list  = None,
                 C:                int   = 5,
                 num_trojans:      int   = 1000,
                 save_dir:         str   = 'mers_progress',
                 results_dir:      str   = 'mers_results',
                 skip_mero:        bool  = False,
                 skip_mers_h:      bool  = False,
                 skip_mers_s:      bool  = False,
                 mutation_mode:    str   = 'paper',
                 mutation_rounds:  int   = 12,
                 pad_to_requested: bool  = False,
                 use_regions:      bool  = False,
                 region_top_k:     int   = 1,
                 region_fanin_depth: int = 3,
                 region_overlap:   float = 0.30,
                 region_mode:      str   = 'compact',
                 region_max_size:  int   = 40,
                 placement_file:   str   = None,
                 phys_radius:      float = 4.0,
                 full_scan:        bool  = True,
                 export_vectors:   bool  = True,
                 vectors_dir:      str   = 'mers_testvectors',
                 exclude_artifact_nodes: bool = True,
                 artifact_prefixes: list = None):

        self.bench_file       = bench_file
        self.circuit_name     = circuit_name
        self.N                = N
        self.rare_threshold   = rare_threshold
        self.num_rand_vectors = num_rand_vectors
        # Sample size for rare-node DETECTION (Monte-Carlo). Decoupled from the
        # generation pool size: more samples give a more accurate rareness
        # estimate without changing how many test vectors are generated.
        # Defaults to num_rand_vectors so existing behaviour is unchanged.
        self.rare_vectors     = rare_vectors if rare_vectors else num_rand_vectors
        self.trigger_counts   = trigger_counts or [4, 8]
        self.C                = C
        self.num_trojans      = num_trojans
        self.save_dir         = save_dir
        self.results_dir      = results_dir
        self.skip_mero        = skip_mero
        self.skip_mers_h      = skip_mers_h
        self.skip_mers_s      = skip_mers_s
        self.mutation_mode    = mutation_mode
        self.mutation_rounds  = mutation_rounds
        self.pad_to_requested = pad_to_requested
        self.use_regions      = use_regions
        self.region_top_k     = region_top_k
        self.region_fanin_depth = region_fanin_depth
        self.region_overlap   = region_overlap
        self.region_mode      = region_mode
        self.region_max_size  = region_max_size
        self.placement_file   = placement_file
        self.phys_radius      = phys_radius
        self.full_scan        = full_scan
        self.export_vectors   = export_vectors
        self.vectors_dir      = vectors_dir
        self.exclude_artifact_nodes = exclude_artifact_nodes
        self.artifact_prefixes = artifact_prefixes

    # ------------------------------------------------------------------
    def run(self) -> dict:
        """Execute the full pipeline and return a summary dict."""

        from src.circuit_parser    import Circuit
        from src.progress_manager  import ProgressManager
        from src.rare_node_finder  import RareNodeFinder
        from src.mers_algo         import MERS, MERSHamming, MERSSim
        from src.mero_algo         import MERO
        from src.evaluator         import TrojanEvaluator
        from src.excel_reporter    import RareNodeReporter, SensitivityReporter
        from src.testset_exporter  import TestsetExporter

        t0 = time.time()
        _banner(f"MERS Pipeline  –  {self.circuit_name}")

        # ----------------------------------------------------------------
        # Stage 1: Parse netlist
        # ----------------------------------------------------------------
        _step(1, "Parse circuit netlist")
        circuit = Circuit()
        circuit.parse_bench(self.bench_file, full_scan=self.full_scan)

        # ----------------------------------------------------------------
        # Initialise progress manager
        # ----------------------------------------------------------------
        pm = ProgressManager(self.save_dir, self.circuit_name)
        pm.print_status()

        # ----------------------------------------------------------------
        # Stage 2: Find rare nodes
        # ----------------------------------------------------------------
        _step(2, "Find rare nodes")
        if self.rare_vectors != self.num_rand_vectors:
            logger.info("[Stage 2] Using %d Monte-Carlo vectors for rare-node "
                        "detection (generation pool is %d).",
                        self.rare_vectors, self.num_rand_vectors)
        finder = RareNodeFinder(
            circuit,
            rare_threshold         = self.rare_threshold,
            num_vectors            = self.rare_vectors,
            exclude_artifact_nodes = self.exclude_artifact_nodes,
            artifact_prefixes      = self.artifact_prefixes,
        )
        rni = finder.find_rare_nodes(progress_mgr=pm)
        rare_nodes = rni['rare_nodes']

        if not rare_nodes:
            logger.warning("No rare nodes found!  Try lowering rare_threshold.")
            return {}

        # ----------------------------------------------------------------
        # Stage 2b: Region targeting (optional)
        # ----------------------------------------------------------------
        # Group rare nodes into structural regions (dense pockets a Trojan
        # trigger could be built from) and restrict MERS to the top-K densest
        # regions. Targets the most probable Trojan locations and makes MERS
        # much lighter than exciting all rare nodes.
        self.region_info = None
        if self.use_regions:
            from src.rare_node_regions import (build_regions, select_top_regions,
                                               summarize_regions)
            if self.placement_file:
                from src.placement_regions import (load_placement,
                                                   match_rare_to_coords,
                                                   build_physical_regions)
                logger.info("[Stage 2b] PHYSICAL region targeting (top-%d, "
                            "placement=%s, radius=%.1f)",
                            self.region_top_k, self.placement_file, self.phys_radius)
                coords = load_placement(self.placement_file)
                located, missing = match_rare_to_coords(rare_nodes, coords)
                logger.info("[Stage 2b] Placement match: %d/%d rare nodes located "
                            "(%.0f%%).", len(located), len(rare_nodes),
                            100 * len(located) / max(1, len(rare_nodes)))
                region_result = build_physical_regions(
                    rare_nodes, located, radius=self.phys_radius,
                    mode=self.region_mode, max_region_size=self.region_max_size)
            else:
                logger.info("[Stage 2b] LOGICAL region targeting (top-%d densest region(s))",
                            self.region_top_k)
                region_result = build_regions(
                    circuit, rare_nodes,
                    fanin_depth       = self.region_fanin_depth,
                    overlap_threshold = self.region_overlap,
                    mode              = self.region_mode,
                    max_region_size   = self.region_max_size,
                )
            for row in summarize_regions(region_result, top_n=self.region_top_k):
                logger.info("  region %d: %d rare nodes, %d footprint, density=%.3f",
                            row['region_id'], row['rare_nodes'],
                            row['cone_gates'], row['density'])
            selected, chosen = select_top_regions(region_result, top_k=self.region_top_k)
            if selected:
                full_count = len(rare_nodes)
                rare_nodes = {r: rare_nodes[r] for r in selected if r in rare_nodes}
                rni = dict(rni)
                rni['rare_nodes'] = rare_nodes
                rni['region_selected'] = selected
                rni['region_result'] = {
                    'n_regions': len(region_result['regions']),
                    'chosen_ids': [c['id'] for c in chosen],
                    'params': region_result['params'],
                }
                self.region_info = rni['region_result']
                # Persist the selection so generate_dataset_a.py can reuse the
                # identical region(s) as Dataset A's cone seeds.
                pm.save('region_selection', {
                    'selected_rare_nodes': selected,
                    'chosen_region_ids': [c['id'] for c in chosen],
                    'region_top_k': self.region_top_k,
                    'params': region_result['params'],
                    'regions_summary': [
                        {'id': c['id'], 'rare_nodes': c['rare_nodes'],
                         'size': c['size'], 'cone_size': c['cone_size'],
                         'density': c['density']}
                        for c in chosen
                    ],
                })
                logger.info("[Stage 2b] Targeting %d rare node(s) from top-%d region(s) "
                            "(was %d; %.0f%% reduction).",
                            len(rare_nodes), self.region_top_k, full_count,
                            100 * (1 - len(rare_nodes) / max(1, full_count)))
            else:
                logger.warning("[Stage 2b] No regions selected; using all rare nodes.")

        # ----------------------------------------------------------------
        # Stage 3: Excel – rare nodes
        # ----------------------------------------------------------------
        _step(3, "Export rare-node Excel report")
        RareNodeReporter(self.results_dir).generate(rni, circuit, self.circuit_name)

        # ----------------------------------------------------------------
        # Stage 4: Random baseline testset
        # ----------------------------------------------------------------
        _step(4, "Generate random baseline testset")
        RAND_KEY = f"random_v6_testset_{self.num_rand_vectors}"
        if pm.exists(RAND_KEY):
            random_testset = pm.load(RAND_KEY)
        else:
            random.seed(0)
            n_pi = circuit.n_inputs()
            random_testset = [
                [random.randint(0, 1) for _ in range(n_pi)]
                for _ in range(self.num_rand_vectors)
            ]
            pm.save(RAND_KEY, random_testset)
        logger.info(f"  Random testset size: {len(random_testset):,}")

        # ----------------------------------------------------------------
        # Stage 5: MERO baseline
        # ----------------------------------------------------------------
        mero_testset = None
        if not self.skip_mero:
            _step(5, f"Generate MERO baseline (N={self.N})")
            mero = MERO(circuit, N=self.N)
            mero_result = mero.generate(
                rni,
                num_random_vectors = self.num_rand_vectors,
                progress_mgr       = pm,
            )
            mero_testset = mero_result['testset']
            logger.info(f"  MERO testset size: {len(mero_testset):,}")

        # ----------------------------------------------------------------
        # Stage 6: MERS core
        # ----------------------------------------------------------------
        _step(6, f"Run MERS core (N={self.N}, mode={self.mutation_mode})")
        mers = MERS(circuit, N=self.N, rare_threshold=self.rare_threshold,
                    mutation_mode=self.mutation_mode,
                    mutation_rounds=self.mutation_rounds)
        mers_result = mers.generate(
            rni,
            num_random_vectors = self.num_rand_vectors,
            progress_mgr       = pm,
            pad_to_requested   = self.pad_to_requested,
        )
        mers_testset = mers_result['testset']
        logger.info(f"  MERS testset size: {len(mers_testset):,}")

        # Trim to 10K for fair comparison (as in paper)
        mers_eval_testset = mers_testset[:10_000]

        # ----------------------------------------------------------------
        # Stage 7: MERS-h
        # ----------------------------------------------------------------
        mers_h_testset = mers_testset
        mers_tag = f"N{self.N}_{self.mutation_mode}"
        if self.mutation_mode == 'fast':
            mers_tag += f"_r{self.mutation_rounds}"
        if not self.skip_mers_h:
            _step(7, "MERS-h reordering (Hamming Distance)")
            mers_h = MERSHamming(circuit)
            mers_h_testset = mers_h.reorder(
                mers_testset, progress_mgr=pm, N_tag=mers_tag)

        # ----------------------------------------------------------------
        # Stage 8: MERS-s
        # ----------------------------------------------------------------
        mers_s_testset = mers_testset
        if not self.skip_mers_s:
            _step(8, f"MERS-s reordering (Simulation, C={self.C})")
            mers_s = MERSSim(circuit, rare_nodes)
            mers_s_testset = mers_s.reorder(
                mers_testset, C=self.C, progress_mgr=pm, N_tag=mers_tag)

        # ----------------------------------------------------------------
        # Stage 9: Export generated test vectors
        # ----------------------------------------------------------------
        if self.export_vectors:
            _step(9, "Export generated test vectors (.txt and .xlsx)")
            export_sets = {
                'Random': random_testset,
                'MERS':   mers_testset,
            }
            if mero_testset is not None:
                export_sets['MERO'] = mero_testset
            if not self.skip_mers_h:
                export_sets['MERS-h'] = mers_h_testset
            if not self.skip_mers_s:
                export_sets['MERS-s'] = mers_s_testset

            vector_manifest = TestsetExporter(self.vectors_dir).generate(
                export_sets, circuit, self.circuit_name)
            pm.save('vector_export_manifest', vector_manifest, use_json=True)

        # ----------------------------------------------------------------
        # Stage 10: Evaluation
        # ----------------------------------------------------------------
        _step(10, "Evaluate all testsets")
        evaluator = TrojanEvaluator(circuit, rare_nodes, num_trojans=self.num_trojans)

        all_results = {}

        for num_trig in self.trigger_counts:
            logger.info(f"\n  ── {num_trig}-trigger Trojans ──")

            methods = {
                'Random': (random_testset[:10_000], f"rand_{self.circuit_name}"),
            }
            if mero_testset is not None:
                methods['MERO'] = (
                    mero_testset[:10_000],
                    f"mero_{self.circuit_name}_N{self.N}"
                )
            methods['MERS'] = (
                mers_eval_testset,
                f"mers_{self.circuit_name}_{mers_tag}"
            )
            if not self.skip_mers_h:
                methods['MERS-h'] = (
                    mers_h_testset[:10_000],
                    f"mers_h_{self.circuit_name}_{mers_tag}"
                )
            if not self.skip_mers_s:
                methods['MERS-s'] = (
                    mers_s_testset[:10_000],
                    f"mers_s_{self.circuit_name}_{mers_tag}_C{self.C}"
                )

            trig_results = {}
            for method_name, (ts, lbl) in methods.items():
                res = evaluator.evaluate(ts, num_triggers=num_trig,
                                         progress_mgr=pm, label=lbl)
                res['testset_size'] = len(ts)
                trig_results[method_name] = res

            all_results[num_trig] = trig_results

            # ----------------------------------------------------------------
            # Stage 11: Export sensitivity Excel
            # ----------------------------------------------------------------
            SensitivityReporter(self.results_dir).generate(
                trig_results, self.circuit_name, num_trig)

        # ----------------------------------------------------------------
        # Print summary table
        # ----------------------------------------------------------------
        _banner("Results Summary")
        for num_trig, methods in all_results.items():
            print(f"\n  {num_trig}-trigger Trojans:")
            print(f"  {'Method':<12} {'SCS (MaxRelSw)':<20} {'Size':>8}")
            print(f"  {'─'*12} {'─'*20} {'─'*8}")
            for method, m in methods.items():
                scs = m.get('avg_max_relative', 0)
                sz  = m.get('testset_size', 0)
                print(f"  {method:<12} {scs:<20.6f} {sz:>8,}")

        elapsed = time.time() - t0
        print(f"\n  Total time: {elapsed/60:.1f} min")

        pm.print_status()
        return all_results


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _banner(text: str):
    print(f"\n{'='*62}")
    print(f"  {text}")
    print(f"{'='*62}")


def _step(n: int, text: str):
    print(f"\n[Stage {n}] {text}")
    logger.info(f"[Stage {n}] {text}")
