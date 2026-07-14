import re
import sys
from pathlib import Path

if len(sys.argv) != 3:
    print("Usage: python xilinx_lut_fdre_to_bench.py input_xilinx_netlist.v output.bench")
    sys.exit(1)

vin = Path(sys.argv[1])
bout = Path(sys.argv[2])
text = vin.read_text(errors="ignore")

# Remove comments
text = re.sub(r"//.*", "", text)
text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)

used_internal_names = set()
const_needed = False
bench = []
unsupported = {}
cell_count = 0
dff_count = 0
lut_count = 0


def clean(x: str) -> str:
    x = x.strip()
    x = x.replace("\\", "")
    # remove spaces introduced by multi-line pins
    x = re.sub(r"\s+", "", x)
    # make literals/parser-unfriendly chars deterministic
    x = x.replace("/", "_").replace(".", "_")
    x = x.replace("[", "_").replace("]", "_")
    x = x.replace(":", "_").replace("'", "")
    return x


def split_decl(kind: str):
    nets = []
    for m in re.finditer(r"\b" + kind + r"\b\s+(.*?);", text, flags=re.S):
        body = m.group(1)
        body = re.sub(r"\[[^\]]+\]", " ", body)
        body = re.sub(r"\b(wire|reg|input|output)\b", " ", body)
        for part in body.replace("\n", " ").split(','):
            n = clean(part)
            if n:
                nets.append(n)
    return nets

inputs = split_decl("input")
outputs = split_decl("output")


def emit(line):
    bench.append(line)


def internal(prefix: str, inst: str, suffix: str) -> str:
    name = f"__{prefix}_{clean(inst)}_{suffix}"
    # ensure unique if somehow reused
    if name in used_internal_names:
        k = 1
        while f"{name}_{k}" in used_internal_names:
            k += 1
        name = f"{name}_{k}"
    used_internal_names.add(name)
    return name


inv_cache = {}

def inv(net: str) -> str:
    # do not directly invert constants here; constants handled separately
    if net not in inv_cache:
        n = "__NOT_" + clean(net)
        # Avoid accidental collision by prefixing all generated inverter names
        inv_cache[net] = n
        emit(f"{n} = NOT({net})")
    return inv_cache[net]


def const0() -> str:
    global const_needed
    const_needed = True
    return "__CONST0"


def const1() -> str:
    global const_needed
    const_needed = True
    return "__CONST1"


def parse_init(init: str):
    init = init.strip()
    # Accept formats such as 64'hA5, 1'b0, 32'd123, plain decimal
    m = re.match(r"(\d+)\s*'\s*([hdb])\s*([0-9a-fA-F_xzXZ]+)", init)
    if m:
        width = int(m.group(1))
        base = m.group(2).lower()
        digits = m.group(3).replace("_", "")
        # Treat x/z as 0 because synthesized LUT INIT should not contain x/z
        digits = re.sub(r"[xXzZ]", "0", digits)
        if base == 'h':
            val = int(digits, 16)
        elif base == 'b':
            val = int(digits, 2)
        else:
            val = int(digits, 10)
        return width, val
    # fallback: plain decimal
    return None, int(init, 0)


def emit_lut(cell: str, init: str, inst: str, pins: dict):
    global lut_count, cell_count
    n = int(cell[3:])
    out = pins.get("O") or pins.get("Y")
    if not out:
        unsupported[cell] = unsupported.get(cell, 0) + 1
        return

    inps = []
    for i in range(n):
        k = f"I{i}"
        if k not in pins:
            unsupported[cell + "_missing_input"] = unsupported.get(cell + "_missing_input", 0) + 1
            return
        inps.append(pins[k])

    width, val = parse_init(init)
    total = 1 << n
    mask = (1 << total) - 1
    val = val & mask

    if val == 0:
        emit(f"{out} = BUF({const0()})")
        lut_count += 1
        cell_count += 1
        return
    if val == mask:
        emit(f"{out} = BUF({const1()})")
        lut_count += 1
        cell_count += 1
        return

    # Fast cases for common 1-input LUTs
    if n == 1:
        if val == 0b10:      # I0
            emit(f"{out} = BUF({inps[0]})")
        elif val == 0b01:    # NOT I0
            emit(f"{out} = NOT({inps[0]})")
        else:
            unsupported[cell + "_init"] = unsupported.get(cell + "_init", 0) + 1
            return
        lut_count += 1
        cell_count += 1
        return

    terms = []
    for idx in range(total):
        if ((val >> idx) & 1) == 0:
            continue
        lits = []
        # Xilinx LUT INIT convention: I0 is the least significant address bit.
        for bit in range(n):
            net = inps[bit]
            if (idx >> bit) & 1:
                lits.append(net)
            else:
                lits.append(inv(net))
        if len(lits) == 1:
            terms.append(lits[0])
        else:
            t = internal("LUTTERM", inst, f"m{idx}")
            emit(f"{t} = AND({', '.join(lits)})")
            terms.append(t)

    if len(terms) == 1:
        emit(f"{out} = BUF({terms[0]})")
    else:
        emit(f"{out} = OR({', '.join(terms)})")
    lut_count += 1
    cell_count += 1


# LUT instances: LUTn #(.INIT(...)) inst (.O(...), .I0(...));
lut_re = re.compile(r"\b(LUT[1-6])\s*#\s*\(\s*\.INIT\s*\(\s*(.*?)\s*\)\s*\)\s*([\\\w$./]+)\s*\((.*?)\)\s*;", re.S)
for m in lut_re.finditer(text):
    cell, init, inst, body = m.groups()
    pins = {}
    for pm in re.finditer(r"\.(\w+)\s*\(\s*([^)]+?)\s*\)", body, flags=re.S):
        pins[pm.group(1)] = clean(pm.group(2))
    emit_lut(cell, init, inst, pins)

# FDRE scan DFFs. We keep only functional D->Q relation. Clock/reset/CE are ignored for scan abstraction.
fdre_re = re.compile(r"\b(FDRE|FDSE|FDCE|FDPE)\s*(?:#\s*\(.*?\)\s*)?([\\\w$./]+)\s*\((.*?)\)\s*;", re.S)
for m in fdre_re.finditer(text):
    cell, inst, body = m.groups()
    pins = {}
    for pm in re.finditer(r"\.(\w+)\s*\(\s*([^)]+?)\s*\)", body, flags=re.S):
        pins[pm.group(1)] = clean(pm.group(2))
    d = pins.get("D")
    q = pins.get("Q")
    if d and q:
        emit(f"{q} = DFF({d})")
        dff_count += 1
        cell_count += 1
    else:
        unsupported[cell] = unsupported.get(cell, 0) + 1

# Other simple assigns, if present
for m in re.finditer(r"\bassign\s+(.+?)\s*=\s*(.+?)\s*;", text, flags=re.S):
    lhs = clean(m.group(1))
    rhs_raw = m.group(2).strip()
    rhs = clean(rhs_raw)
    if rhs_raw in ["1'b0", "0"]:
        emit(f"{lhs} = BUF({const0()})")
    elif rhs_raw in ["1'b1", "1"]:
        emit(f"{lhs} = BUF({const1()})")
    elif rhs:
        emit(f"{lhs} = BUF({rhs})")

# Prepend constants only if needed, using a real input and logic identity so parsers that do not accept literal 0/1 still work.
const_lines = []
const_input = "__CONST_DRIVER"
if const_needed:
    if const_input not in inputs:
        inputs.append(const_input)
    const_lines = [
        f"__CONST_DRIVER_N = NOT({const_input})",
        f"__CONST0 = AND({const_input}, __CONST_DRIVER_N)",
        f"__CONST1 = OR({const_input}, __CONST_DRIVER_N)",
    ]

# Deduplicate lines while preserving order. Keep all definitions once.
seen = set()
dedup = []
for line in const_lines + bench:
    if line not in seen:
        dedup.append(line)
        seen.add(line)

with bout.open("w") as f:
    f.write("# BENCH generated from Xilinx LUT/FDRE Verilog netlist\n")
    f.write("# LUT INITs are expanded into AND/OR/NOT/BUF gates.\n")
    f.write("# FDRE cells are emitted as Q = DFF(D) for scan-style MERS simulation.\n\n")
    for n in sorted(set(inputs)):
        f.write(f"INPUT({n})\n")
    f.write("\n")
    for n in sorted(set(outputs)):
        f.write(f"OUTPUT({n})\n")
    f.write("\n")
    for line in dedup:
        f.write(line + "\n")

print("Written:", bout)
print("Primary inputs :", len(set(inputs)))
print("Primary outputs:", len(set(outputs)))
print("LUT cells      :", lut_count)
print("DFFs           :", dff_count)
print("BENCH gates    :", len(dedup))
if unsupported:
    print("Unsupported cells:", unsupported)
    sys.exit(2)
else:
    print("Unsupported cells: 0")
