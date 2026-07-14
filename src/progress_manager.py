"""
progress_manager.py
===================
Checkpoint system that persists all intermediate results to disk.
If the process is killed or the machine reboots, the pipeline resumes
from exactly where it left off – no work is repeated.

Files are stored under  <save_dir>/<circuit_name>/
  * *.pkl  – binary pickle (fast, for numpy arrays / large lists)
  * *.json – JSON (human-readable metadata / counters)
"""

import os
import json
import pickle
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)


class ProgressManager:
    """
    Manages save / load of arbitrary Python objects keyed by string IDs.

    Example
    -------
    pm = ProgressManager('mers_progress', 'c2670')
    pm.save('rare_nodes', rare_nodes_dict)
    ...
    if pm.exists('rare_nodes'):
        rare_nodes = pm.load('rare_nodes')
    """

    def __init__(self, base_dir: str = 'mers_progress', circuit_name: str = 'circuit'):
        self.base_dir = Path(base_dir) / circuit_name
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = self.base_dir / 'progress_log.json'
        self._log: dict = self._load_log()

    # ------------------------------------------------------------------
    # Core save / load
    # ------------------------------------------------------------------

    def save(self, key: str, data, use_json: bool = False) -> None:
        """Persist *data* under *key*."""
        if use_json:
            path = self.base_dir / f"{key}.json"
            with open(path, 'w') as fh:
                json.dump(data, fh, indent=2)
        else:
            path = self.base_dir / f"{key}.pkl"
            with open(path, 'wb') as fh:
                pickle.dump(data, fh, protocol=pickle.HIGHEST_PROTOCOL)

        self._log[key] = {
            'saved_at': datetime.now().isoformat(),
            'type': 'json' if use_json else 'pkl',
            'path': str(path)
        }
        self._save_log()
        logger.debug(f"[Checkpoint] Saved  '{key}'  →  {path.name}")

    def load(self, key: str):
        """Load data that was previously saved under *key*."""
        # Try pkl first, then json
        for ext, mode in [('.pkl', 'rb'), ('.json', 'r')]:
            path = self.base_dir / f"{key}{ext}"
            if path.exists():
                if ext == '.pkl':
                    with open(path, 'rb') as fh:
                        data = pickle.load(fh)
                else:
                    with open(path, 'r') as fh:
                        data = json.load(fh)
                logger.info(f"[Checkpoint] Loaded '{key}'  from  {path.name}")
                return data
        raise FileNotFoundError(f"No checkpoint found for key '{key}'")

    def exists(self, key: str) -> bool:
        """Return True if a checkpoint for *key* exists on disk."""
        for ext in ('.pkl', '.json'):
            if (self.base_dir / f"{key}{ext}").exists():
                return True
        return False

    def delete(self, key: str) -> None:
        """Remove a checkpoint (e.g., to force re-computation)."""
        for ext in ('.pkl', '.json'):
            p = self.base_dir / f"{key}{ext}"
            if p.exists():
                p.unlink()
                logger.info(f"[Checkpoint] Deleted '{key}'")

    def list_checkpoints(self) -> list:
        """Return all saved checkpoint keys."""
        return sorted(self._log.keys())

    def print_status(self) -> None:
        """Print a human-readable summary of all checkpoints."""
        print(f"\n{'='*60}")
        print(f"Checkpoint directory: {self.base_dir}")
        print(f"{'='*60}")
        if not self._log:
            print("  (no checkpoints saved yet)")
        else:
            for key, meta in self._log.items():
                exists = '✓' if self.exists(key) else '✗ (deleted)'
                print(f"  [{exists}] {key:35s}  saved {meta['saved_at'][:19]}")
        print(f"{'='*60}\n")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_log(self) -> dict:
        if self._log_file.exists():
            with open(self._log_file, 'r') as fh:
                return json.load(fh)
        return {}

    def _save_log(self) -> None:
        with open(self._log_file, 'w') as fh:
            json.dump(self._log, fh, indent=2)


class PartialProgress:
    """
    Handles the case where a long loop (e.g., MERS mutation over 10K vectors)
    was interrupted mid-way.  Saves per-iteration state so it can resume.

    Usage
    -----
    pp = PartialProgress(pm, 'mers_mutation')
    if pp.is_complete():
        result = pm.load('mers_mutation')
    else:
        start_idx = pp.resume_from()
        for i in range(start_idx, total):
            ... do work ...
            pp.update(i, partial_state)
        pp.mark_complete(final_result)
    """

    def __init__(self, pm: ProgressManager, stage_key: str):
        self.pm = pm
        self.stage_key = stage_key
        self.partial_key = f"_partial_{stage_key}"

    def is_complete(self) -> bool:
        return self.pm.exists(self.stage_key)

    def resume_from(self) -> int:
        """Return the index to resume from (0 if no partial save)."""
        if self.pm.exists(self.partial_key):
            state = self.pm.load(self.partial_key)
            idx = state.get('last_completed_idx', -1) + 1
            logger.info(f"[PartialProgress] Resuming '{self.stage_key}' from index {idx}")
            return idx
        return 0

    def load_state(self) -> dict:
        """Load the saved partial state (or return empty dict)."""
        if self.pm.exists(self.partial_key):
            return self.pm.load(self.partial_key)
        return {}

    def update(self, idx: int, state: dict) -> None:
        """Save partial state after completing iteration *idx*."""
        state['last_completed_idx'] = idx
        self.pm.save(self.partial_key, state)

    def mark_complete(self, final_result) -> None:
        """Save the final result and clean up partial checkpoint."""
        self.pm.save(self.stage_key, final_result)
        if self.pm.exists(self.partial_key):
            self.pm.delete(self.partial_key)
        logger.info(f"[PartialProgress] Stage '{self.stage_key}' complete.")
