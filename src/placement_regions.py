r"""
placement_regions.py
====================
Physical (placement-aware) region clustering for FPGA side-channel analysis.

The logical regions in rare_node_regions.py group rare nodes by SHARED FAN-IN
CONE -- proximity in the Boolean netlist. For side-channel analysis on a real
FPGA, what matters instead is PHYSICAL proximity on the die: rare nodes whose
logic is placed in nearby slices share a power/EM signature, so a Trojan
hidden among physically-clustered rare logic is what a differential SCA can
localise. This module builds regions from Vivado placement coordinates.

Getting placement data out of Vivado
------------------------------------
After place_design (or opening a routed .dcp), run in the Vivado Tcl console:

    # one row per leaf cell: name, X, Y (BEL/site coordinates)
    set fp [open "placement.csv" w]
    puts $fp "cell,x,y"
    foreach c [get_cells -hierarchical -filter {IS_PRIMITIVE}] {
        set site [get_property LOC $c]
        if {$site eq ""} { continue }
        set x [get_property RPM_X $c]
        set y [get_property RPM_Y $c]
        if {$x eq "" || $y eq ""} {
            # fall back to parsing SLICE_X#Y# from the site name
            if {[regexp {X(\d+)Y(\d+)} $site -> sx sy]} { set x $sx; set y $sy }
        }
        puts $fp "$c,$x,$y"
    }
    close $fp

Any of these formats are accepted by load_placement():
  * CSV with a header containing cell/x/y columns (any order)
  * whitespace/CSV "cellname  X  Y"
  * "cellname  SLICE_X12Y34"  (X/Y parsed from the site string)
  * "cellname  X12Y34"

Cell-name matching
------------------
The .bench node names came from the Verilog netlist. Vivado cell names may
have hierarchy prefixes/suffixes (e.g. "design_1_i/u_core/Q_i_2__292_n_0" or
"...Q_i_2__292_n_0_reg"). load_placement() indexes cells by a normalised
suffix so a rare-node name like "Q_i_2__292_n_0" matches the full Vivado path.
Match rate is reported so you can see how many rare nodes were located.
"""

import logging
import re

logger = logging.getLogger(__name__)

_XY_RE = re.compile(r'X(\d+)Y(\d+)')


# ----------------------------------------------------------------------
# Placement loading
# ----------------------------------------------------------------------

def _normalise_cell(name):
    """
    Reduce a (possibly hierarchical) Vivado cell name to a comparable key.
    Strips hierarchy (keeps the last path component), trailing _reg / _i_*
    register-replication suffixes, and surrounding quotes/whitespace.
    """
    n = name.strip().strip('"').strip("'")
    if '/' in n:
        n = n.split('/')[-1]
    # common Vivado register suffixes
    n = re.sub(r'_reg(\[\d+\])?$', '', n)
    n = re.sub(r'_replica(_\d+)?$', '', n)
    return n


def load_placement(path):
    """
    Parse a placement file into {normalised_cell_name: (x, y)}.

    Accepts CSV with a header (cell/x/y or cell/loc columns) or plain
    whitespace/comma rows. SLICE_X#Y# or X#Y# site strings are parsed for
    coordinates when explicit x/y are absent.
    """
    coords = {}
    raw_rows = 0
    with open(path) as f:
        lines = [ln.rstrip('\n') for ln in f if ln.strip()]

    if not lines:
        raise SystemExit(f"placement file is empty: {path}")

    # Detect a header line.
    header = None
    first = lines[0].lower()
    if any(k in first for k in ('cell', 'name', 'loc', ',x', 'x,', ' x ')):
        if re.search(r'[a-z_]', lines[0].split(',')[0], re.I) and \
           ('x' in first or 'loc' in first):
            header = [h.strip().lower() for h in re.split(r'[,\t]', lines[0])]

    def split_row(s):
        if ',' in s:
            return [t.strip() for t in s.split(',')]
        return s.split()

    ci = xi = yi = li = None
    if header:
        for i, h in enumerate(header):
            if h in ('cell', 'name', 'cell_name', 'instance'):
                ci = i
            elif h == 'x':
                xi = i
            elif h == 'y':
                yi = i
            elif h in ('loc', 'site', 'placement'):
                li = i
        data_lines = lines[1:]
    else:
        data_lines = lines

    for s in data_lines:
        if s.lstrip().startswith('#'):
            continue
        parts = split_row(s)
        raw_rows += 1
        if not parts:
            continue
        try:
            if header and ci is not None:
                cell = parts[ci]
                if xi is not None and yi is not None and xi < len(parts) \
                        and yi < len(parts) and parts[xi] and parts[yi]:
                    x, y = float(parts[xi]), float(parts[yi])
                elif li is not None and li < len(parts):
                    m = _XY_RE.search(parts[li])
                    if not m:
                        continue
                    x, y = float(m.group(1)), float(m.group(2))
                else:
                    continue
            else:
                # headerless: cell then either "X Y" or a site string
                cell = parts[0]
                if len(parts) >= 3 and _is_num(parts[1]) and _is_num(parts[2]):
                    x, y = float(parts[1]), float(parts[2])
                elif len(parts) >= 2:
                    m = _XY_RE.search(parts[1])
                    if not m:
                        continue
                    x, y = float(m.group(1)), float(m.group(2))
                else:
                    continue
        except (ValueError, IndexError):
            continue
        coords[_normalise_cell(cell)] = (x, y)

    logger.info("[Placement] Loaded %d placed cell(s) from %s (%d row(s) read).",
                len(coords), path, raw_rows)
    if not coords:
        raise SystemExit(
            "No coordinates parsed. Expected rows like 'cell,x,y' or "
            "'cell SLICE_X#Y#'. See placement_regions.py header for the "
            "Vivado export snippet.")
    return coords


def _is_num(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def match_rare_to_coords(rare_nodes, coords):
    """
    Map rare-node names to (x, y) using normalised-suffix matching.
    Returns (located: {rare_node: (x,y)}, missing: [rare_node, ...]).
    """
    # Build a lookup from normalised key -> coord, and also index by the
    # raw rare-node name normalised the same way.
    located, missing = {}, []
    for r in rare_nodes:
        key = _normalise_cell(r)
        if key in coords:
            located[r] = coords[key]
        else:
            missing.append(r)
    return located, missing


# ----------------------------------------------------------------------
# Physical clustering
# ----------------------------------------------------------------------

def build_physical_regions(rare_nodes, coords, radius=4.0,
                           min_region_size=1, max_region_size=40,
                           mode='compact'):
    """
    Cluster rare nodes by PHYSICAL proximity on the FPGA die.

    Parameters
    ----------
    rare_nodes      : dict {name: info} (only the located ones are clustered)
    coords          : {rare_node: (x, y)}  (from match_rare_to_coords)
    radius          : two rare nodes are "neighbours" if their Euclidean
                      slice-distance <= radius (in placement-grid units).
    min/max_region_size, mode : as in rare_node_regions.build_regions
                      ('compact' = size-capped dense pockets, recommended;
                       'components' = connected components of the proximity
                       graph).

    Returns the same dict shape as rare_node_regions.build_regions so the
    Excel reporter and selection helpers work unchanged. Each region also
    carries 'centroid' (x,y), 'extent' (max pairwise distance), and the
    physical bounding box.
    """
    import numpy as np

    names = [r for r in rare_nodes if r in coords]
    if not names:
        raise SystemExit("No rare nodes have placement coordinates; cannot "
                         "build physical regions.")
    pts = np.array([coords[r] for r in names], dtype=float)
    n = len(names)

    logger.info("[PhysRegions] Clustering %d placed rare node(s), radius=%.2f, "
                "mode=%s ...", n, radius, mode)

    # Neighbour graph by radius. For the circuit sizes here (hundreds of rare
    # nodes) an O(n^2) distance pass is trivial and avoids a KD-tree dep.
    d2 = ((pts[:, None, :] - pts[None, :, :]) ** 2).sum(axis=2)
    r2 = radius * radius
    neighbours = {i: set(np.where((d2[i] <= r2))[0].tolist()) - {i}
                  for i in range(n)}

    if mode == 'components':
        groups = _components(n, neighbours)
    else:
        groups = _compact(n, neighbours, max_region_size)

    regions = []
    singletons = []
    for members in groups:
        if len(members) < min_region_size:
            singletons.extend(names[i] for i in members)
            continue
        mpts = pts[members]
        centroid = mpts.mean(axis=0)
        if len(members) > 1:
            dd = ((mpts[:, None, :] - mpts[None, :, :]) ** 2).sum(axis=2)
            extent = float(np.sqrt(dd.max()))
        else:
            extent = 0.0
        area = max(1.0, (np.ptp(mpts[:, 0]) + 1) * (np.ptp(mpts[:, 1]) + 1))
        regions.append({
            'rare_nodes': sorted(names[i] for i in members),
            'size': len(members),
            'cone_size': int(round(area)),     # physical footprint (slices)
            'density': len(members) / area,     # rare nodes per slice-area
            'centroid': (float(centroid[0]), float(centroid[1])),
            'extent': extent,
            'bbox': (float(mpts[:, 0].min()), float(mpts[:, 1].min()),
                     float(mpts[:, 0].max()), float(mpts[:, 1].max())),
        })

    regions.sort(key=lambda x: (-x['size'], -x['density']))
    for i, reg in enumerate(regions):
        reg['id'] = i

    logger.info("[PhysRegions] Formed %d physical region(s) (+%d singleton). "
                "Largest: %d rare nodes.", len(regions), len(singletons),
                regions[0]['size'] if regions else 0)

    return {
        'regions': regions,
        'singletons': singletons,
        'params': {
            'kind': 'physical',
            'radius': radius,
            'mode': mode,
            'min_region_size': min_region_size,
            'max_region_size': max_region_size,
        },
    }


def _components(n, neighbours):
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in range(n):
        for b in neighbours[a]:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    comp = {}
    for i in range(n):
        comp.setdefault(find(i), []).append(i)
    return list(comp.values())


def _compact(n, neighbours, max_region_size):
    used = set()
    order = sorted(range(n), key=lambda i: -len(neighbours[i]))
    groups = []
    for seed in order:
        if seed in used:
            continue
        members = [seed]
        used.add(seed)
        cand = sorted((x for x in neighbours[seed] if x not in used),
                      key=lambda x: -len(neighbours[x]))
        for x in cand:
            if len(members) >= max_region_size:
                break
            if x not in used:
                members.append(x)
                used.add(x)
        groups.append(members)
    return groups
