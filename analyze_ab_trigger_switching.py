#!/usr/bin/env python3
"""
analyze_ab_trigger_switching.py
================================
Differential A-vs-B analysis on the TRIGGER nodes that Dataset A actually
controls -- the correct signal for validating the paired control testset.

Why this exists
---------------
A common confusion when validating Dataset A with a clean-vs-infected
payload simulator:

  * The payload simulator's "PARTIAL activation" counts switching of the
    Trojan PAYLOAD net (the net present in the infected netlist but not the
    clean one, e.g. n4448_temp). For a dormant Trojan
    (payload = normal_net XOR trigger, trigger never asserted), the payload
    net's toggling is dominated by the NORMAL net it XORs with -- NOT by the
    trigger. Since Dataset A and Dataset B match background/normal-circuit
    activity by design, their payload toggling is necessarily similar. So
    "PARTIAL activation ~equal for A and B" is EXPECTED and does not mean A
    failed.

  * What Dataset A actually minimises is switching / rare-value activation
    of the TRIGGER nodes (and their cone). That is the differential
    side-channel signal: under Dataset B the trigger logic is exercised
    (switches a lot, occasionally nears assertion); under Dataset A it is
    held quiet. THIS is what to compare.

This script loads the A and B vector files (the same files you feed to the
payload simulator), simulates the trigger nodes on the netlist, and reports
the A-vs-B differential on:

  * per-trigger P(node == rare/active value)   -- lower for A
  * per-trigger toggle rate (switches / pair)  -- lower for A
  * trigger-cone aggregate switching            -- lower for A
  * "any trigger switches" pair rate            -- lower for A

It mirrors the payload simulator's vector parsing (explicit --pi-order,
optional --paired) so the numbers line up with your existing flow.

USAGE
-----
  python analyze_ab_trigger_switching.py \
      --bench s38417.bench \
      --pi-order s38417_primary_input_order.txt \
      --trigger-spec "Q_i_2__292_n_0:1,Q_i_2__363_n_0:1,Q_i_4__103_n_0:1,Q_i_6__5_n_0:0" \
      --vectors-a s38417_AB_Dataset-A_vectors.txt \
      --vectors-b s38417_AB_Dataset-B_vectors.txt
"""

import sys, argparse
import numpy as np
from collections import deque

IGNORE_DEFAULT = ("__LUTTERM_", "__NOT_")


def parse_bench(path):
    norm = {"BUFF": "BUF", "BUF": "BUF", "INV": "NOT", "NOT": "NOT", "AND": "AND",
            "OR": "OR", "NAND": "NAND", "NOR": "NOR", "XOR": "XOR", "XNOR": "XNOR"}
    gates, pis, pos, dff_q, dff_d = [], set(), set(), [], []
    for line in open(path):
        s = line.split("#", 1)[0].strip()
        if not s:
            continue
        u = s.upper()
        if u.startswith("INPUT("):
            pis.add(s[6:s.rindex(")")].strip()); continue
        if u.startswith("OUTPUT("):
            pos.add(s[7:s.rindex(")")].strip()); continue
        if "=" in s:
            o, e = s.split("=", 1); o = o.strip(); e = e.strip()
            gt = e[:e.index("(")].strip().upper()
            args = [a.strip() for a in e[e.index("(") + 1:e.rindex(")")].split(",") if a.strip()]
            if gt == "DFF":
                dff_q.append(o); dff_d.append(args[0])
            elif gt in norm:
                gates.append((norm[gt], args, o))
            else:
                raise ValueError("unknown gate: %s" % gt)
    pis.update(dff_q); pos.update(dff_d)
    driven = {g[2] for g in gates} | pis
    undriven = {a for _, args, _ in gates for a in args} - driven
    pis.update(undriven)
    return gates, pis, pos


def levelize(gates, pis):
    driver = {g[2]: g for g in gates}
    indeg, consumers = {}, {}
    for g in gates:
        need = sum(1 for a in g[1] if a in driver)
        indeg[g[2]] = need
        for a in g[1]:
            if a in driver:
                consumers.setdefault(a, []).append(g)
    ready = deque(g for g in gates if indeg[g[2]] == 0)
    order = []
    while ready:
        g = ready.popleft(); order.append(g)
        for c in consumers.get(g[2], ()):
            indeg[c[2]] -= 1
            if indeg[c[2]] == 0:
                ready.append(c)
    if len(order) != len(gates):
        raise RuntimeError("combinational loop -- full-scan the FFs first")
    return order


def simulate(order, pi_packed):
    val = dict(pi_packed)
    for gtype, args, out in order:
        ins = [val[a] for a in args]
        if gtype == "AND":
            acc = ins[0].copy()
            for v in ins[1:]: acc &= v
        elif gtype == "OR":
            acc = ins[0].copy()
            for v in ins[1:]: acc |= v
        elif gtype == "NAND":
            acc = ins[0].copy()
            for v in ins[1:]: acc &= v
            acc = np.bitwise_not(acc)
        elif gtype == "NOR":
            acc = ins[0].copy()
            for v in ins[1:]: acc |= v
            acc = np.bitwise_not(acc)
        elif gtype == "XOR":
            acc = ins[0].copy()
            for v in ins[1:]: acc ^= v
        elif gtype == "XNOR":
            acc = ins[0].copy()
            for v in ins[1:]: acc ^= v
            acc = np.bitwise_not(acc)
        elif gtype == "NOT":
            acc = np.bitwise_not(ins[0])
        else:
            acc = ins[0].copy()
        val[out] = acc
    return val


def pack_bits(bits01, N, NPAD):
    b = np.zeros(NPAD, dtype=np.uint8); b[:N] = bits01
    return np.packbits(b, bitorder="little").view(np.uint64)


def shifted(P):
    S = np.zeros_like(P)
    one, k = np.uint64(1), np.uint64(63)
    S[:-1] = (P[:-1] >> one) | (P[1:] << k)
    S[-1] = P[-1] >> one
    return S


def load_pi_order(path, pis):
    order = []
    for line in open(path):
        s = line.rstrip("\r\n")
        if not s.strip() or s.lstrip().startswith("#"):
            continue
        parts = s.split("\t") if "\t" in s else s.split()
        name = parts[0] if len(parts) == 1 else parts[1]
        order.append(name.strip())
    return order


def read_vectors(path, pi_order, paired):
    Wpi = len(pi_order)
    want = 2 * Wpi if paired else Wpi
    rows, skipped = [], 0
    for line in open(path):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        toks = raw.split()
        if (paired and len(toks) >= 2 and len(toks[0]) == Wpi and len(toks[1]) == Wpi
                and set(toks[0]) <= set("01") and set(toks[1]) <= set("01")):
            rows.append([int(c) for c in toks[0]])
            rows.append([int(c) for c in toks[1]])
            continue
        s = "".join(ch for ch in raw if ch in "01")
        if len(s) != want:
            skipped += 1
            continue
        if paired:
            rows.append([int(c) for c in s[:Wpi]])
            rows.append([int(c) for c in s[Wpi:]])
        else:
            rows.append([int(c) for c in s])
    if not rows:
        sys.stderr.write("FATAL: no usable vectors of width %d from %s\n" % (want, path))
        sys.exit(1)
    M = np.array(rows, dtype=np.uint8).T   # (n_pi, N_states)
    return M, skipped


def trigger_stats(label, vec_path, gates, pis, pi_order, conds, paired, prefixes):
    M, skipped = read_vectors(vec_path, pi_order, paired)
    N = M.shape[1]
    NPAD = ((N + 63) // 64) * 64
    const = {"VDD": 1, "GND": 0, "vdd": 1, "gnd": 0}
    pi_packed = {}
    for k, p in enumerate(pi_order):
        bits = np.full(N, const[p], np.uint8) if p in const else M[k]
        pi_packed[p] = pack_bits(bits, N, NPAD)
    # any infected/extra PIs not in pi_order -> 0
    for p in pis - set(pi_order):
        pi_packed[p] = pack_bits(np.zeros(N, np.uint8), N, NPAD)

    val = simulate(levelize(gates, pis), pi_packed)
    mask_packed = pack_bits(np.concatenate([np.ones(N - 1, np.uint8), [0]]), N, NPAD)

    if paired:
        take = np.arange(0, N - 1, 2, dtype=np.int64)   # only orig_i -> gen_i
    else:
        take = np.arange(N - 1, dtype=np.int64)

    # per-trigger toggle and activation
    per = {}
    tog_stack = []
    for name, want_val in conds:
        if name not in val:
            sys.stderr.write("warn: %s not in netlist; skipped\n" % name)
            continue
        bits = np.unpackbits(val[name].view(np.uint8), bitorder="little")[:N]
        tr = (val[name] ^ shifted(val[name])) & mask_packed
        tbits = np.unpackbits(tr.view(np.uint8), bitorder="little")[:N - 1][take]
        tog_stack.append(tbits)
        # P(node == activating value), measured on the generated side if paired
        if paired:
            pval = float((bits[1::2] == want_val).mean())
        else:
            pval = float((bits == want_val).mean())
        per[name] = (want_val, pval, float(tbits.mean()))

    tog_stack = np.stack(tog_stack) if tog_stack else np.zeros((1, len(take)))
    any_switch_rate = float((tog_stack.sum(axis=0) > 0).mean())
    cone_sw_mean = float(tog_stack.sum(axis=0).mean())
    n_pairs = len(take)
    if skipped:
        print("  [%s] note: skipped %d malformed rows" % (label, skipped))
    return per, any_switch_rate, cone_sw_mean, n_pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", required=True, help="clean netlist (.bench)")
    ap.add_argument("--pi-order", required=True)
    ap.add_argument("--trigger-spec", required=True,
                    help="e.g. 'Q_i_2__292_n_0:1,Q_i_2__363_n_0:1,...,Q_i_6__5_n_0:0'")
    ap.add_argument("--vectors-a", required=True, help="Dataset A vectors file")
    ap.add_argument("--vectors-b", required=True, help="Dataset B vectors file")
    ap.add_argument("--paired", action="store_true",
                    help="vector files are paired (orig||gen per line). Use this if "
                    "you generated A/B as paired transition vectors.")
    ap.add_argument("--ignore-prefixes", default=",".join(IGNORE_DEFAULT))
    a = ap.parse_args()
    prefixes = tuple(p for p in a.ignore_prefixes.split(",") if p)

    gates, pis, pos = parse_bench(a.bench)
    pi_order = load_pi_order(a.pi_order, pis)

    conds = []
    for tok in a.trigger_spec.split(","):
        name, v = tok.rsplit(":", 1)
        conds.append((name.strip(), int(v)))

    print("=== Trigger-switching differential: Dataset A vs Dataset B ===")
    print("Triggers: %s\n" % ", ".join("%s==%d" % (n, v) for n, v in conds))

    perA, anyA, coneA, npA = trigger_stats("A", a.vectors_a, gates, pis, pi_order,
                                           conds, a.paired, prefixes)
    perB, anyB, coneB, npB = trigger_stats("B", a.vectors_b, gates, pis, pi_order,
                                           conds, a.paired, prefixes)

    print("Per-trigger P(at activating value)   [lower for A = better suppression]")
    print("  %-22s %12s %12s %10s" % ("trigger", "A", "B", "A/B"))
    for name, want_val in conds:
        if name not in perA or name not in perB:
            continue
        pa = perA[name][1]; pb = perB[name][1]
        ratio = (pa / pb) if pb > 0 else float('nan')
        print("  %-22s %11.3f%% %11.3f%% %9.2f" % (name + ("(==%d)" % want_val),
                                                   100 * pa, 100 * pb, ratio))

    print("\nPer-trigger toggle rate (switches per pair)   [lower for A]")
    print("  %-22s %12s %12s" % ("trigger", "A", "B"))
    for name, want_val in conds:
        if name not in perA or name not in perB:
            continue
        print("  %-22s %12.4f %12.4f" % (name, perA[name][2], perB[name][2]))

    print("\n=== Aggregate (the headline differential) ===")
    print("  Trigger-cone switching / pair :  A = %.4f   B = %.4f   (A is %.0f%% of B)"
          % (coneA, coneB, 100 * coneA / coneB if coneB else 0))
    print("  'Any trigger switches' rate   :  A = %.2f%%   B = %.2f%%"
          % (100 * anyA, 100 * anyB))
    print("  Pairs analysed                :  A = %d   B = %d  (paired=%s)"
          % (npA, npB, a.paired))
    if coneB > 0:
        print("\n  => Dataset A reduces trigger-cone switching by %.0f%% relative to B."
              % (100 * (1 - coneA / coneB)))
    print("\nNote: a clean-vs-infected PAYLOAD simulator's 'PARTIAL activation' compares")
    print("the payload net (e.g. n4448_temp = normal_net XOR trigger). Under a dormant")
    print("trigger that toggles with the NORMAL net, which A and B match by design, so")
    print("payload PARTIAL activation is EXPECTED to be ~equal. The trigger differential")
    print("above is the signal Dataset A controls.")


if __name__ == "__main__":
    main()
