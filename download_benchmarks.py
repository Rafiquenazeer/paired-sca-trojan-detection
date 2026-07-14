#!/usr/bin/env python3
"""
download_benchmarks.py
======================
Step-by-step helper to get the benchmark files you need.

ISCAS-85 / ISCAS-89 .bench files are used directly by MERS.
Trust-Hub Verilog benchmarks require a Verilog->.bench conversion step.

Run this script first:
  python download_benchmarks.py
"""

import os
import sys
import textwrap

BENCH_DIR = "benchmarks"
os.makedirs(BENCH_DIR, exist_ok=True)

# ======================================================================
# Built-in minimal .bench circuits for immediate testing
# ======================================================================

# c17: the classic ISCAS-85 smallest circuit (5 inputs, 2 outputs, 6 NAND gates)
C17_BENCH = """\
# c17  -  ISCAS-85 benchmark  (5 inputs, 2 outputs, 6 NAND gates)
INPUT(N1)
INPUT(N2)
INPUT(N3)
INPUT(N6)
INPUT(N7)
OUTPUT(N22)
OUTPUT(N23)
N10 = NAND(N1, N3)
N11 = NAND(N3, N6)
N16 = NAND(N2, N11)
N19 = NAND(N11, N7)
N22 = NAND(N10, N16)
N23 = NAND(N16, N19)
"""

# Simple demo circuit with intentional rare nodes
DEMO_BENCH = """\
# demo_rare - circuit designed to have identifiable rare nodes
# Rare node: N_AND8 (8-input AND) - P(N_AND8=1) ~ 0.5^8 = 0.004
INPUT(A)
INPUT(B)
INPUT(C)
INPUT(D)
INPUT(E)
INPUT(F)
INPUT(G)
INPUT(H)
INPUT(X)
INPUT(Y)
OUTPUT(OUT1)
OUTPUT(OUT2)
OUTPUT(OUT3)
# Normal logic (non-rare)
N_OR   = OR(A, B)
N_XOR  = XOR(C, D)
N_NAND = NAND(E, F)
N_NOR  = NOR(G, H)
# Rare AND (all inputs must be 1)
N_AND4 = AND(A, B, C, D)
N_AND6 = AND(A, B, C, D, E, F)
# Very rare (AND of 8)
N_AND8 = AND(A, B, C, D, E, F, G, H)
# Trojan-like sub-circuit triggered by N_AND8
N_T1   = AND(N_AND8, X)
N_T2   = OR(N_T1, N_NOR)
# Outputs
OUT1   = OR(N_OR, N_XOR)
OUT2   = NAND(N_NAND, N_AND4)
OUT3   = XOR(N_T2, Y)
"""


def write_builtin_benchmarks():
    """Write the built-in test circuits to the benchmarks directory."""
    for name, content in [("c17", C17_BENCH), ("demo_rare", DEMO_BENCH)]:
        path = os.path.join(BENCH_DIR, f"{name}.bench")
        # BUG FIX: always write as UTF-8 so special chars never trigger CP1252 crash
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"  [OK] Written: {path}")


# ======================================================================
# ISCAS download instructions
# ======================================================================

ISCAS_SOURCES = {
    "Primary (Brglez lab, free)":
        "https://people.engr.ncsu.edu/brglez/CBL/software/ISCAS85/",
    "Alternate (GitHub mirror)":
        "https://github.com/cad-polito-it/benchmarks/tree/master/ISCAS85",
    "Formatted .bench (direct)":
        "https://www.dropbox.com/sh/h6e5a04xnhwi8zv/",
}

ISCAS85_CIRCUITS = ["c17", "c432", "c499", "c880", "c1355",
                    "c1908", "c2670", "c3540", "c5315", "c6288", "c7552"]
ISCAS89_CIRCUITS = ["s27", "s298", "s344", "s349", "s382", "s386", "s400",
                    "s444", "s526", "s641", "s713", "s820", "s832", "s838",
                    "s953", "s1196", "s1238", "s1423", "s1488", "s1494",
                    "s5378", "s9234", "s13207", "s15850", "s35932", "s38417", "s38584"]

TRUSTHUB_URL = "https://trust-hub.org/#/benchmarks/chip-level-trojan"

TRUSTHUB_AES = {
    "AES-T100":  "Sequential Trojan, counts specific key value, leaks through payload",
    "AES-T200":  "Combinational Trojan, rare data bus condition triggers",
    "AES-T400":  "Sequential counter-based trigger (hardest to detect functionally)",
    "AES-T1300": "Most cited in recent Power SCA literature (EM + Power)",
    "AES-T1400": "Payload leaks data through power side-channel",
    "AES-T1800": "Large Trojan - easier to detect via power",
}


# ======================================================================
# Verilog -> .bench converter (structural gate-level only)
# ======================================================================

def verilog_to_bench_instructions():
    print("""
  Trust-Hub benchmarks are in Verilog.  To convert to .bench format:

  OPTION A - Use Yosys (open-source synthesis tool, FREE):
  ----------------------------------------------------------
    1.  Install Yosys:  https://github.com/YosysHQ/yosys/releases
        (Windows: use WSL or the pre-built binaries)

    2.  In Yosys shell:
          read_verilog your_aes_trojan.v
          synth -flatten -top top_module_name
          write_bench -noattr your_circuit.bench

    3.  Run MERS:
          python run_mers.py --bench benchmarks/your_circuit.bench \\
                             --name AES-T200 --N 1000

  OPTION B - Use Icarus Verilog + iVerilog (FREE):
  --------------------------------------------------
    iverilog -o /dev/null -s top -E your_circuit.v  # check syntax
    # Then use Yosys for netlist extraction.

  OPTION C - Use Cadence Genus / Synopsys Design Compiler (commercial):
  -----------------------------------------------------------------------
    read_hdl your_circuit.v
    elaborate
    write_hdl -format bench your_circuit.bench
""")


# ======================================================================
# Main
# ======================================================================

def main():
    print("=" * 64)
    print("  MERS Benchmark Setup Helper")
    print("=" * 64)

    # Step 1: Write built-in circuits
    print("\n[Step 1]  Writing built-in test circuits ...")
    write_builtin_benchmarks()

    # Step 2: ISCAS instructions
    print("\n[Step 2]  ISCAS benchmark download")
    print(textwrap.dedent(f"""
  The MERS paper (Huang et al., CCS 2016) uses these ISCAS-85 circuits:
    {', '.join(["c2670","c3540","c5315","c6288","c7552"])}
  ... and these ISCAS-89 (converted to full-scan):
    {', '.join(["s13207","s15850","s35932"])}

  Download locations (all FREE, no registration):
"""))
    for name, url in ISCAS_SOURCES.items():
        print(f"    [{name}]")
        print(f"      {url}")

    print(f"""
  After downloading, place the .bench files in:  ./{BENCH_DIR}/

  Quick test (c17 is already there):
    python run_mers.py --bench benchmarks/c17.bench --name c17 \\
                       --N 50 --vectors 500 --trojans 50 --skip-mers-s
""")

    # Step 3: Trust-Hub recommendation
    print("[Step 3]  Trust-Hub Benchmark Recommendation for Power SCA + MERS")
    print(textwrap.dedent(f"""
  Best Trust-Hub chip-level benchmark for MERS + Power Side-Channel Analysis:

  +---------------------------------------------------------------------+
  |  TOP PICK:  AES-T200  (or AES-T1300 for latest literature match)   |
  |                                                                     |
  |  Why?                                                               |
  |  * AES has high and well-understood switching activity              |
  |  * Trojan trigger uses RARE internal data-bus conditions -> perfect |
  |    for MERS rare-node analysis                                      |
  |  * Widely benchmarked -> easy to compare your results              |
  |  * Power trace datasets publicly available on IEEE DataPort         |
  |                                                                     |
  |  Runner-up:  s38417 series  (very large sequential circuit,        |
  |  from the same group as ISCAS-89, excellent for showing MERS        |
  |  scalability with region-based partitioning)                        |
  +---------------------------------------------------------------------+

  Trust-Hub AES variants:
"""))
    for bench, desc in TRUSTHUB_AES.items():
        print(f"    {bench:<12}  {desc}")

    print(f"""
  Download from: {TRUSTHUB_URL}
    (requires free registration)
""")

    # Step 4: Verilog conversion
    print("[Step 4]  Converting Trust-Hub Verilog -> .bench")
    verilog_to_bench_instructions()

    # Step 5: Ready to run
    print("[Step 5]  You're ready to run!\n")
    print("  QUICK START (uses built-in demo_rare circuit):")
    print("    python run_mers.py --bench benchmarks/demo_rare.bench \\")
    print("                       --name demo_rare --N 100 --vectors 1000 \\")
    print("                       --trojans 50 --triggers 4 --skip-mers-s")
    print()
    print("  PAPER REPLICATION (needs c2670.bench from ISCAS):")
    print("    python run_mers.py --bench benchmarks/c2670.bench \\")
    print("                       --name c2670 --N 1000 --vectors 10000 \\")
    print("                       --trojans 1000 --triggers 4 8")
    print()
    print("  CHECK PROGRESS (resume after restart):")
    print("    python run_mers.py --bench benchmarks/c2670.bench \\")
    print("                       --name c2670 --status-only")
    print()
    print("=" * 64)


if __name__ == "__main__":
    main()
