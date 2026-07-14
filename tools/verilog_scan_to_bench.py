
import re
import sys
from pathlib import Path

if len(sys.argv) != 3:
    print("Usage: python verilog_scan_to_bench.py s15850scan.v s15850.bench")
    sys.exit(1)

vin = Path(sys.argv[1])
bout = Path(sys.argv[2])

text = vin.read_text(errors="ignore")

# Remove Verilog comments
text = re.sub(r"//.*", "", text)
text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)

def clean(x: str) -> str:
    x = x.strip()
    # Remove Verilog escaped identifier backslash, keep the actual name
    if x.startswith("\\"):
        x = x[1:]
    # A Verilog escaped identifier can end at whitespace
    x = x.strip()
    # BENCH parsers often dislike slash/dot/backslash in node names.
    # Keep names deterministic and simple.
    x = x.replace("/", "_")
    x = x.replace(".", "_")
    x = x.replace("[", "_")
    x = x.replace("]", "_")
    x = x.replace("'", "")
    return x

def split_decl(kind: str):
    nets = []
    # Match declarations possibly spanning multiple lines: input ...;
    for m in re.finditer(r"\b" + kind + r"\b\s+(.*?);", text, flags=re.S):
        body = m.group(1)
        # Drop ranges like [3:0] if any
        body = re.sub(r"\[[^\]]+\]", " ", body)
        for part in body.replace("\n", " ").split(","):
            n = clean(part)
            # remove keywords if present
            n = re.sub(r"\b(wire|reg|input|output)\b", "", n).strip()
            if n:
                nets.append(n)
    return nets

inputs = split_decl("input")
outputs = split_decl("output")

bench = []

def emit(line):
    bench.append(line)

# Assigns
for m in re.finditer(r"\bassign\s+(.+?)\s*=\s*(.+?)\s*;", text, flags=re.S):
    lhs = clean(m.group(1))
    rhs = clean(m.group(2))
    if rhs in ["1b0", "1'b0", "0"]:
        emit(f"{lhs} = BUF(0)")
    elif rhs in ["1b1", "1'b1", "1"]:
        emit(f"{lhs} = BUF(1)")
    else:
        emit(f"{lhs} = BUF({rhs})")

# Cell instances: <type> <instance> ( .PIN(net), ... );
cell_re = re.compile(r"(?ms)^\s*([A-Za-z_][\w$]*)\s+([\\\w$./]+)\s*\((.*?)\)\s*;")

unsupported = {}
cell_count = 0
dff_count = 0

for m in cell_re.finditer(text):
    cell = m.group(1)
    inst = m.group(2)
    body = m.group(3)

    if cell == "module":
        continue

    pins = {}
    for pm in re.finditer(r"\.(\w+)\s*\(\s*([^)]+?)\s*\)", body, flags=re.S):
        pins[pm.group(1)] = clean(pm.group(2).replace("\n", " "))

    def dins():
        ds = []
        for i in range(1, 10):
            k = f"DIN{i}"
            if k in pins:
                ds.append(pins[k])
        if "DIN" in pins and not ds:
            ds.append(pins["DIN"])
        return ds

    out = pins.get("Q") or pins.get("Y") or pins.get("ZN")

    if not out:
        unsupported[cell] = unsupported.get(cell, 0) + 1
        continue

    c = cell.lower()

    # scan DFF: use functional data input DIN; ignore scan pins SDIN/SSEL/CLK
    if c.startswith("sdff") or "dff" in c:
        d = pins.get("DIN") or pins.get("D")
        q = pins.get("Q")
        qn = pins.get("QN")
        if d and q:
            emit(f"{q} = DFF({d})")
            dff_count += 1
            cell_count += 1
            if qn:
                emit(f"{qn} = NOT({q})")
        else:
            unsupported[cell] = unsupported.get(cell, 0) + 1
        continue

    ins = dins()

    # 1-input cells
    if c.startswith("i1"):
        if len(ins) == 1:
            emit(f"{out} = NOT({ins[0]})")
            cell_count += 1
        else:
            unsupported[cell] = unsupported.get(cell, 0) + 1
        continue

    # ib1 appears as buffer cells in this scanned ISCAS netlist
    if c.startswith("ib1"):
        if len(ins) == 1:
            emit(f"{out} = BUF({ins[0]})")
            cell_count += 1
        else:
            unsupported[cell] = unsupported.get(cell, 0) + 1
        continue

    gate = None
    if c.startswith("nnd"):
        gate = "NAND"
    elif c.startswith("nor"):
        gate = "NOR"
    elif c.startswith("and"):
        gate = "AND"
    elif c.startswith("or"):
        gate = "OR"
    elif c.startswith("xor"):
        gate = "XOR"
    elif c.startswith("xnr") or c.startswith("xnor"):
        gate = "XNOR"

    if gate and len(ins) >= 1:
        emit(f"{out} = {gate}({', '.join(ins)})")
        cell_count += 1
    else:
        unsupported[cell] = unsupported.get(cell, 0) + 1

with bout.open("w") as f:
    f.write("# Scan-style BENCH generated from s15850scan.v\n")
    f.write("# DFFs are emitted as Q = DFF(DIN). Scan pins are ignored.\n\n")

    for n in sorted(set(inputs)):
        f.write(f"INPUT({n})\n")

    f.write("\n")

    for n in sorted(set(outputs)):
        f.write(f"OUTPUT({n})\n")

    f.write("\n")

    # avoid duplicate lines while preserving order
    seen = set()
    for line in bench:
        if line not in seen:
            f.write(line + "\n")
            seen.add(line)

print("Written:", bout)
print("Primary inputs :", len(set(inputs)))
print("Primary outputs:", len(set(outputs)))
print("DFFs           :", dff_count)
print("Gate cells     :", cell_count)
print("BENCH lines    :", len(set(bench)))
if unsupported:
    print("Unsupported cells:", unsupported)
    sys.exit(2)
else:
    print("Unsupported cells: 0")
