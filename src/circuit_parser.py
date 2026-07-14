"""
circuit_parser.py  (v8 — levelized vectorized simulation engine)
==================================================================
Parses ISCAS-85 / ISCAS-89 .bench netlists, including LUT-expanded
Xilinx/Vivado .bench files produced by tools/xilinx_lut_fdre_to_bench.py
(helper nodes such as __LUTTERM_*, __NOT_*, __CONST0/__CONST1, and
Q = DFF(D) scan flip-flops).

Carried forward from v4/v7
---------------------------
* Constant nodes ('0','1','vss','vdd','gnd','vcc', ...) pre-resolved.
* 'gat' prefix normalisation (USC sportlab format).
* WIRE/IBUF/OBUF recognised as buffers.
* Phantom-PO and unresolved-gate diagnostics.
* Sequential circuits: DFF -> full-scan PI/PO splitting (Q becomes a
  scan primary input, D becomes a scan primary output) -- unchanged,
  works identically for combinational (no DFFs) and sequential netlists.

NEW in v8 — why
----------------
The v4/v7 simulate_batch() issued ONE NumPy call PER GATE.  For small
ISCAS-85 circuits (hundreds to a few thousand gates) this is fine.  For
large Xilinx LUT-expanded benchmarks such as s38417 (after LUT6 INIT
expansion this can be 10^4-10^5 internal nodes), per-gate NumPy call
overhead (~1-5 microseconds each) dominates and a single simulate_batch()
call can take hundreds of milliseconds -- and MERS/MERO/MERS-s call
simulate_batch()/simulate() tens of thousands of times.

NEW in v8 — what
-----------------
At parse time, the circuit is *compiled* once into "levels": level 0 is
primary inputs + constants, level L is every gate whose inputs are all at
level < L.  Gates at the SAME level can never depend on each other (that
is the definition of level = longest path from a PI), so within a level
they can be evaluated in any order.  Gates are further grouped by
(level, logic-class, invert, arity) so that an entire group -- e.g. "all
247 6-input AND terms at level 13" -- is evaluated with ONE vectorised
gather -> reduce -> scatter operation instead of 247 separate calls.

This reduces the NumPy-call count from O(gates) to O(levels x type/arity
combinations) -- typically a 50-300x reduction for LUT-expanded
benchmarks -- while producing BIT-IDENTICAL results to the old per-gate
loop (verified against it for c17, demo_rare, and s15850).

Public API is unchanged:
  * simulate_batch(vectors) -> dict[node_name -> np.ndarray(K, uint8)]
  * simulate(vector)        -> NodeState  (drop-in dict-like: .get(), in, [])
"""

import re
import logging
import numpy as np
from collections import defaultdict, deque

logger = logging.getLogger(__name__)

# Gate types that are simple pass-through buffers
_BUF_TYPES = frozenset({'BUFF', 'BUF', 'WIRE', 'IBUF', 'OBUF'})
_NOT_TYPES = frozenset({'NOT', 'INV'})
_AND_TYPES = frozenset({'AND', 'NAND'})
_OR_TYPES  = frozenset({'OR', 'NOR'})
_XOR_TYPES = frozenset({'XOR', 'XNOR'})

# Constant node names -> their fixed logic value.
# These are NEVER defined as gates; they act like hard-wired PIs.
_CONSTANT_NODES: dict = {
    '0':   0, '1':   1,
    'vss': 0, 'VSS': 0, 'gnd': 0, 'GND': 0,
    'vdd': 1, 'VDD': 1, 'vcc': 1, 'VCC': 1,
}


def _normalise_name(name: str) -> str:
    """
    Normalise a node name.
    - Strips 'gat' prefix used in USC sportlab .bench files
      (e.g. 'gat1656' -> '1656').
    - Does NOT modify constant names ('0', '1', 'vss', etc.).
    """
    s = name.strip()
    if s in _CONSTANT_NODES:
        return s
    if s.lower().startswith('gat') and len(s) > 3 and s[3:].isdigit():
        return s[3:]
    return s


class Gate:
    __slots__ = ('name', 'gate_type', 'inputs', 'output')

    def __init__(self, name: str, gate_type: str, inputs: list, output: str):
        self.name      = name
        self.gate_type = gate_type.upper()
        self.inputs    = inputs
        self.output    = output

    def evaluate(self, values: dict) -> int:
        """Scalar evaluation of one gate (kept for reference/diagnostics)."""
        iv = [values.get(i, 0) for i in self.inputs]
        gt = self.gate_type
        if gt == 'AND':  return 1 if all(v == 1 for v in iv) else 0
        if gt == 'OR':   return 1 if any(v == 1 for v in iv) else 0
        if gt in _NOT_TYPES: return 1 - iv[0]
        if gt == 'NAND': return 0 if all(v == 1 for v in iv) else 1
        if gt == 'NOR':  return 0 if any(v == 1 for v in iv) else 1
        if gt == 'XOR':
            r = 0
            for v in iv: r ^= v
            return r
        if gt == 'XNOR':
            r = 0
            for v in iv: r ^= v
            return 1 - r
        if gt in _BUF_TYPES: return iv[0] if iv else 0
        return iv[0] if iv else 0

    def __repr__(self):
        return f"Gate({self.output}={self.gate_type}({','.join(self.inputs)}))"


class NodeState:
    """
    Lightweight dict-like view over a single simulated vector.

    Returned by Circuit.simulate(vector).  Avoids materialising a Python
    dict with one entry per circuit node (which, for LUT-expanded
    benchmarks with 10^4-10^5 nodes, would dominate runtime when
    simulate() is called tens of thousands of times -- e.g. once per bit
    in --mutation-mode paper).

    Supports the subset of the dict API used throughout this project:
    .get(name, default), `name in state`, state[name], .items(), .keys().
    """
    __slots__ = ('_col', '_index')

    def __init__(self, col: np.ndarray, index: dict):
        self._col   = col     # 1D array, one entry per compiled node row
        self._index = index   # shared dict: node name -> row index

    def get(self, name, default=0):
        idx = self._index.get(name)
        if idx is None:
            return default
        return int(self._col[idx])

    def __contains__(self, name):
        return name in self._index

    def __getitem__(self, name):
        return int(self._col[self._index[name]])

    def items(self):
        for name, idx in self._index.items():
            yield name, int(self._col[idx])

    def keys(self):
        return self._index.keys()


class Circuit:
    """
    Parsed logic circuit.

    Attributes
    ----------
    primary_inputs  : list[str]
    primary_outputs : list[str]
    gates           : dict[str, Gate]
    eval_order      : list[str]   - topological order of gate outputs
    all_nodes       : list[str]   - PIs + gate outputs
    dff_nodes       : list[str]   - DFF outputs (scan PIs in full-scan mode)
    unresolved_gates: list[str]   - gates left after topo sort (bugs)
    """

    def __init__(self):
        self.primary_inputs:   list = []
        self.primary_outputs:  list = []
        self.gates:            dict = {}
        self.eval_order:       list = []
        self.all_nodes:        list = []
        self.dff_nodes:        list = []
        self.unresolved_gates: list = []
        self._filename:        str  = ''
        self._compiled:        bool = False

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_bench(self, filename: str, full_scan: bool = True) -> 'Circuit':
        self._filename = filename
        logger.info(f"Parsing: {filename}")

        with open(filename, 'r', encoding='utf-8', errors='replace') as fh:
            lines = fh.readlines()

        for raw in lines:
            line = raw.strip()
            if '#' in line:
                line = line[:line.index('#')].strip()
            if not line:
                continue

            if line.upper().startswith('INPUT'):
                m = re.search(r'\((.+)\)', line)
                if m:
                    self.primary_inputs.append(_normalise_name(m.group(1)))
                continue

            if line.upper().startswith('OUTPUT'):
                m = re.search(r'\((.+)\)', line)
                if m:
                    self.primary_outputs.append(_normalise_name(m.group(1)))
                continue

            if '=' in line:
                m = re.match(r'(\S+)\s*=\s*(\w+)\s*\((.+)\)', line)
                if not m:
                    continue
                output   = _normalise_name(m.group(1))
                gtype    = m.group(2).strip().upper()
                inputs_s = m.group(3).strip()
                inputs   = [_normalise_name(x) for x in inputs_s.split(',')]

                if gtype == 'DFF' and full_scan:
                    self.dff_nodes.append(output)
                    self.primary_inputs.append(output)
                    self.primary_outputs.append(inputs[0])
                else:
                    self.gates[output] = Gate(f'g_{output}', gtype, inputs, output)

        # Warn about phantom POs (OUTPUT declared but no gate drives them)
        pi_set   = set(self.primary_inputs)
        gate_set = set(self.gates.keys())
        phantom  = [po for po in self.primary_outputs
                    if po not in gate_set and po not in pi_set
                    and po not in _CONSTANT_NODES]
        if phantom:
            logger.warning(
                f"  {len(phantom)} OUTPUT node(s) have no gate definition "
                f"(will always be 0): {phantom[:8]}")

        self._topological_sort()

        pi_set2 = set(self.primary_inputs)
        self.all_nodes = list(self.primary_inputs) + [
            n for n in self.eval_order if n not in pi_set2
        ]

        logger.info(
            f"  PIs={len(self.primary_inputs)}  POs={len(self.primary_outputs)}  "
            f"Gates={len(self.gates)}  DFFs(scan)={len(self.dff_nodes)}  "
            f"Eval-order={len(self.eval_order)}"
        )
        if self.unresolved_gates:
            logger.warning(
                f"  *** {len(self.unresolved_gates)} unresolved gate(s) - "
                f"inputs reference undefined nodes.  "
                f"Sample: {self.unresolved_gates[:5]}.  "
                f"These gates default to 0 and their descendants may be wrong."
            )

        self._compile()
        return self

    # ------------------------------------------------------------------
    # Topological sort
    # ------------------------------------------------------------------

    def _topological_sort(self):
        """
        Kahn's algorithm.

        Pre-resolved nodes: all declared primary inputs (incl. DFF outputs
        in full-scan mode) AND all constant nodes ('0','1','vss','vdd', ...).
        This ensures BUF(0), AND(x,1) etc. are handled correctly regardless
        of where they appear in the file.
        """
        resolved   = set(self.primary_inputs) | set(_CONSTANT_NODES.keys())
        dependents = defaultdict(list)

        for out, gate in self.gates.items():
            for inp in gate.inputs:
                dependents[inp].append(out)

        in_degree = {
            out: sum(1 for i in gate.inputs if i not in resolved)
            for out, gate in self.gates.items()
        }

        queue = deque([out for out, d in in_degree.items() if d == 0])
        order = []

        while queue:
            node = queue.popleft()
            order.append(node)
            resolved.add(node)
            for dep in dependents[node]:
                if dep in in_degree:
                    in_degree[dep] -= 1
                    if in_degree[dep] == 0:
                        queue.append(dep)

        order_set = set(order)
        remaining = [n for n in self.gates if n not in order_set]
        self.unresolved_gates = remaining
        order.extend(remaining)
        self.eval_order = order

    # ------------------------------------------------------------------
    # v8: compile into levelized, grouped vectorised form
    # ------------------------------------------------------------------

    def _classify(self, gate: 'Gate'):
        """
        Map a gate to (logic_class, invert, input_list_for_grouping).

        logic_class is one of 'AND', 'OR', 'XOR', 'BUF'.  AND/OR/XOR with
        arity 1 naturally reduce to the identity, which matches
        Gate.evaluate()'s behaviour for single-input AND/OR/XOR gates.
        Unknown gate types fall back to BUF (pass-through of input 0),
        exactly like the final `return iv[0] if iv else 0` in
        Gate.evaluate().
        """
        gt = gate.gate_type
        inputs = gate.inputs if gate.inputs else ['']
        if gt in _AND_TYPES:
            return 'AND', (gt == 'NAND'), inputs
        if gt in _OR_TYPES:
            return 'OR', (gt == 'NOR'), inputs
        if gt in _XOR_TYPES:
            return 'XOR', (gt == 'XNOR'), inputs
        if gt in _NOT_TYPES:
            return 'BUF', True, inputs[:1]
        # BUF/BUFF/WIRE/IBUF/OBUF and any unrecognised type
        return 'BUF', False, inputs[:1]

    def _compile(self):
        """
        Build the levelized, grouped representation used by
        _simulate_matrix().  Runs once at parse time.

        Row layout of the value matrix M (n_rows, K):
          row 0    -> permanently 0; the fallback row for any gate-input
                      name that is not itself a PI, constant, or another
                      gate's output (matches old `result.get(inp, zeros)`)
          rows 1.. -> one row per constant name, one row per primary
                      input, one row per gate output (insertion order:
                      constants, PIs, gates -- matches the key set
                      returned by the v4 per-gate simulate_batch exactly)
        """
        node_index: dict = {}
        const_one_rows = []
        row = 1  # row 0 reserved as the universal zero/"missing input" row

        for cname, cval in _CONSTANT_NODES.items():
            node_index[cname] = row
            if cval == 1:
                const_one_rows.append(row)
            row += 1

        for pi in self.primary_inputs:
            if pi not in node_index:
                node_index[pi] = row
                row += 1

        for g in self.gates.keys():
            if g not in node_index:
                node_index[g] = row
                row += 1

        self._node_index     = node_index
        self._n_rows         = row
        self._const_one_rows = np.array(const_one_rows, dtype=np.int64)
        self._zero_row       = 0

        # Forward fanout map: node -> list of gate outputs that consume it
        # as an input. Used by dataset_a_generator.compute_cone() to walk
        # "downstream" from a rare node without recomputing this each time.
        dependents: dict = defaultdict(list)
        for out, gate in self.gates.items():
            for inp in gate.inputs:
                dependents[inp].append(out)
        self.dependents = dict(dependents)

        # ---- Levels: level 0 = PI/constant, level L = 1+max(input levels) ----
        unresolved_set = set(self.unresolved_gates)
        level: dict = {}
        for pi in self.primary_inputs:
            level[pi] = 0
        for c in _CONSTANT_NODES:
            level[c] = 0

        for node in self.eval_order:
            if node in unresolved_set:
                continue
            gate = self.gates[node]
            inp_levels = [level.get(i, 0) for i in gate.inputs] or [0]
            level[node] = max(inp_levels) + 1

        # ---- Group resolved gates by (level, logic_class, invert, arity) ----
        groups: dict = {}
        for node in self.eval_order:
            if node in unresolved_set:
                continue
            gate = self.gates[node]
            logic_class, invert, in_names = self._classify(gate)
            arity = max(1, len(in_names))
            key = (level[node], logic_class, invert, arity)
            out_rows, in_rows = groups.setdefault(key, ([], []))
            out_rows.append(node_index[node])
            in_rows.append([node_index.get(n, self._zero_row) for n in in_names])

        compiled = []
        for (lvl, logic_class, invert, arity), (out_rows, in_rows) in groups.items():
            compiled.append((
                lvl, logic_class, invert,
                np.array(out_rows, dtype=np.int64),
                np.array(in_rows,  dtype=np.int64),   # shape (G, arity)
            ))
        compiled.sort(key=lambda t: t[0])
        self._compiled_groups = compiled

        # ---- Unresolved gates: small fallback list, processed afterwards ----
        # Evaluated one-by-one in `self.unresolved_gates` order, replicating
        # the old per-gate loop's left-to-right semantics for this edge case
        # (malformed .bench files only -- empty for well-formed netlists).
        unresolved_compiled = []
        for node in self.unresolved_gates:
            gate = self.gates[node]
            logic_class, invert, in_names = self._classify(gate)
            in_rows = np.array(
                [[node_index.get(n, self._zero_row) for n in in_names]],
                dtype=np.int64)
            unresolved_compiled.append(
                (logic_class, invert, node_index[node], in_rows))
        self._unresolved_compiled = unresolved_compiled

        n_groups = len(compiled)
        n_levels = len({c[0] for c in compiled}) if compiled else 0
        logger.info(
            f"  Compiled: {self._n_rows - 1} nodes -> {n_groups} vectorised "
            f"group(s) across {n_levels} level(s)"
            + (f"  (+{len(unresolved_compiled)} unresolved, processed "
               f"individually)" if unresolved_compiled else "")
        )
        self._compiled = True

    # ------------------------------------------------------------------
    # Core vectorised simulation
    # ------------------------------------------------------------------

    @staticmethod
    def _reduce(logic_class: str, gathered: np.ndarray) -> np.ndarray:
        """gathered: (G, A, K) uint8 -> (G, K) uint8, reduced over axis=1."""
        if logic_class == 'AND':
            return np.bitwise_and.reduce(gathered, axis=1)
        if logic_class == 'OR':
            return np.bitwise_or.reduce(gathered, axis=1)
        if logic_class == 'XOR':
            return np.bitwise_xor.reduce(gathered, axis=1)
        # 'BUF' (arity always 1): identity on the single input
        return gathered[:, 0, :]

    def _simulate_matrix(self, vectors) -> np.ndarray:
        """
        Run every compiled group, in level order, on K input vectors.

        Returns M of shape (n_rows, K), dtype uint8.  Row indices are
        given by self._node_index (plus row 0 = permanent zero row).
        """
        if not self._compiled:
            self._compile()

        arr = np.asarray(vectors, dtype=np.uint8)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        K = arr.shape[0]

        M = np.zeros((self._n_rows, K), dtype=np.uint8)
        if self._const_one_rows.size:
            M[self._const_one_rows, :] = 1

        n_pi = len(self.primary_inputs)
        if arr.shape[1] < n_pi:
            # Pad with zeros if a shorter vector was supplied.
            padded = np.zeros((K, n_pi), dtype=np.uint8)
            padded[:, :arr.shape[1]] = arr
            arr = padded

        for i, pi in enumerate(self.primary_inputs):
            M[self._node_index[pi], :] = arr[:, i]

        for _level, logic_class, invert, out_idx, in_idx in self._compiled_groups:
            gathered = M[in_idx]                              # (G, A, K)
            result   = self._reduce(logic_class, gathered)    # (G, K)
            if invert:
                result = 1 - result
            M[out_idx, :] = result

        # Unresolved gates (rare): process individually, left-to-right,
        # so an earlier unresolved gate's freshly-written row is visible
        # to a later one -- matching the old per-gate loop's behaviour.
        for logic_class, invert, out_row, in_idx in self._unresolved_compiled:
            gathered = M[in_idx]                              # (1, A, K)
            result   = self._reduce(logic_class, gathered)    # (1, K)
            if invert:
                result = 1 - result
            M[out_row, :] = result[0]

        return M

    # ------------------------------------------------------------------
    # Public simulation API (unchanged signatures)
    # ------------------------------------------------------------------

    def simulate(self, input_vector) -> 'NodeState':
        """
        Simulate ONE input vector.

        Returns a NodeState: a lightweight dict-like view supporting
        .get(name, default), `name in state`, and state[name] -- the
        full subset of the dict API used elsewhere in this project --
        without materialising a Python dict with one entry per node.
        """
        M = self._simulate_matrix([input_vector])
        return NodeState(M[:, 0], self._node_index)

    def simulate_batch(self, vectors) -> dict:
        """
        Simulate K input vectors simultaneously.

        Returns {node_name: np.ndarray(K, dtype=uint8)} for every constant,
        primary input, and gate output -- identical key set and values to
        the v4 per-gate implementation.
        """
        M = self._simulate_matrix(vectors)
        return {name: M[idx] for name, idx in self._node_index.items()}

    def simulate_matrix(self, vectors):
        """
        Advanced/raw API.  Returns (M, node_index):
          M          : (n_rows, K) uint8 value matrix (row 0 is a
                       permanent zero/"missing" row).
          node_index : dict node_name -> row index into M (shared,
                       precomputed once at compile time -- do not mutate).

        For callers that need to extract MANY node-subsets (e.g. "every
        background gate" -- tens of thousands of nodes) across several
        calls without paying simulate_batch()'s O(n_total_nodes) dict
        construction each time. Precompute index arrays once with
        `node_index`, then slice with `M[idx_array]` (a single vectorised
        gather) instead of `[batch[n] for n in names]` + np.stack.
        See src/dataset_a_generator.py for the canonical usage.
        """
        M = self._simulate_matrix(vectors)
        return M, self._node_index

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_unresolved_details(self) -> list:
        """Return details of unresolved gates for diagnosis."""
        resolved = (set(self.primary_inputs)
                    | set(_CONSTANT_NODES.keys())
                    | set(self.gates.keys()))
        return [
            {
                'node':    n,
                'type':    self.gates[n].gate_type,
                'inputs':  self.gates[n].inputs,
                'missing': [i for i in self.gates[n].inputs if i not in resolved],
            }
            for n in self.unresolved_gates
        ]

    # ------------------------------------------------------------------
    # Legacy helpers
    # ------------------------------------------------------------------

    def simulate_pair(self, v_prev: list, v_curr: list):
        sp = self.simulate(v_prev)
        sc = self.simulate(v_curr)
        switches = {}
        total_sw = 0
        for node in self.all_nodes:
            sw = 1 if sp.get(node) != sc.get(node) else 0
            switches[node] = sw
            total_sw += sw
        return sp, sc, switches, total_sw

    def n_inputs(self) -> int:
        return len(self.primary_inputs)

    def internal_nodes(self) -> list:
        pi_set = set(self.primary_inputs)
        return [n for n in self.eval_order if n not in pi_set]

    def __repr__(self):
        return (f"Circuit('{self._filename}', "
                f"PI={len(self.primary_inputs)}, PO={len(self.primary_outputs)}, "
                f"gates={len(self.gates)}, unresolved={len(self.unresolved_gates)})")
