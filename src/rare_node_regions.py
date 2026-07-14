"""
rare_node_regions.py
====================
Group rare nodes into structural "regions" and rank them by density, so MERS
(Dataset B) and the Dataset-A cone can target the densest pocket(s) of rare
nodes instead of all of them.

Threat-model rationale
----------------------
A hardware-Trojan trigger is a small gate (often one AND/comparator) that
fires when several rare signals simultaneously hit their rare values. To wire
that gate, those rare nodes must share logic locality -- their fan-in cones
overlap, or they sit within a few gates of a common point. So the rare nodes
an attacker could realistically combine into one trigger are exactly the rare
nodes whose cones converge. A dense cluster of such rare nodes is therefore
both the most likely hiding spot AND a much smaller set to excite than "all
rare nodes," which is what makes MERS heavy.

Region definition (structural proximity by shared fan-in cone)
--------------------------------------------------------------
1. For each rare node r, compute its depth-d fan-in cone fin(r): the gate
   nodes reachable by walking backwards through gate inputs up to d levels.
2. Two rare nodes are "linked" if their cones overlap enough --
   |fin(a) & fin(b)| / |fin(a) | fin(b)|  >=  overlap_threshold  (Jaccard),
   OR one rare node lies inside the other's cone (direct structural coupling).
3. Regions = connected components of the resulting rare-node graph. Each
   component is a set of rare nodes that pairwise share trigger-able locality.
4. Regions are ranked by rare-node count (density). The top-K densest are the
   most probable Trojan locations and the recommended targeting set.

This is deterministic, dependency-free (no community-detection library), and
interpretable: a region literally is "rare nodes that could feed a common
trigger."
"""

import logging
from collections import deque

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Fan-in cone
# ----------------------------------------------------------------------

def fanin_cone(circuit, node, depth):
    """
    Set of gate-output node names within `depth` fan-in levels of `node`
    (excluding primary inputs / constants, which are not gates). depth<=0
    returns {node} if node is a gate, else empty.
    """
    gates = circuit.gates
    if node not in gates:
        return set()
    cone = {node}
    if depth is not None and depth <= 0:
        return cone
    frontier = {node}
    d = 0
    while frontier and (depth is None or d < depth):
        nxt = set()
        for n in frontier:
            g = gates.get(n)
            if g is None:
                continue
            for inp in g.inputs:
                if inp in gates and inp not in cone:
                    cone.add(inp)
                    nxt.add(inp)
        frontier = nxt
        d += 1
    return cone


# ----------------------------------------------------------------------
# Region construction
# ----------------------------------------------------------------------

def build_regions(circuit, rare_nodes, fanin_depth=3,
                  overlap_threshold=0.20, min_region_size=1,
                  mode='compact', max_region_size=40):
    """
    Cluster rare nodes into structural regions.

    mode
    ----
    'compact' (default, recommended for targeting): grow size-capped, tight
        clusters around the densest rare-node seeds. Each region collects a
        seed rare node plus the other rare nodes whose cones overlap it
        (Jaccard >= overlap_threshold), up to max_region_size, then removes
        them and repeats with the next densest unused seed. Produces many
        small, genuinely dense pockets -- the realistic "an attacker could
        wire these into one trigger" unit -- instead of one giant connected
        blob.
    'components': connected components of the cone-overlap graph (a rare node
        joins a region if it overlaps ANY member). Fewer, larger regions;
        useful for a coarse "where is rare logic concentrated" map, but tends
        to merge a large fraction of rare nodes into one component because
        real circuits share a logic backbone.

    Other parameters as before. Returns the same dict shape for both modes.
    """
    rnames = [r for r in rare_nodes if r in circuit.gates]
    skipped = [r for r in rare_nodes if r not in circuit.gates]
    if skipped:
        logger.info("[Regions] %d rare node(s) are not gate outputs "
                    "(PIs/constants); excluded from regions: %s",
                    len(skipped), skipped[:5])

    n = len(rnames)
    logger.info("[Regions] mode=%s  Building cones (depth=%d) for %d rare nodes ...",
                mode, fanin_depth, n)
    cones = {r: fanin_cone(circuit, r, fanin_depth) for r in rnames}

    contains = {}
    for r in rnames:
        for g in cones[r]:
            contains.setdefault(g, []).append(r)

    # Precompute, for each rare node, the set of other rare nodes whose cones
    # overlap it strongly enough to be trigger-combinable. Shared via the
    # inverted index so we never do a full O(n^2) compare.
    neighbours = {r: set() for r in rnames}
    checked = set()
    for g, members in contains.items():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            a = members[i]
            ca = cones[a]
            for j in range(i + 1, len(members)):
                b = members[j]
                key = (a, b) if a < b else (b, a)
                if key in checked:
                    continue
                checked.add(key)
                cb = cones[b]
                inter = len(ca & cb)
                if inter == 0:
                    continue
                strong = (a in cb) or (b in ca)
                jac = inter / float(len(ca) + len(cb) - inter)
                if strong or jac >= overlap_threshold:
                    neighbours[a].add(b)
                    neighbours[b].add(a)

    if mode == 'components':
        regions, singletons = _regions_components(
            rnames, neighbours, cones, min_region_size)
    else:
        regions, singletons = _regions_compact(
            rnames, neighbours, cones, min_region_size, max_region_size)

    regions.sort(key=lambda x: (-x['size'], -x['density']))
    for i, reg in enumerate(regions):
        reg['id'] = i

    logger.info("[Regions] Formed %d region(s) (+%d singleton rare node(s)). "
                "Largest region: %d rare nodes.",
                len(regions), len(singletons),
                regions[0]['size'] if regions else 0)

    return {
        'regions': regions,
        'singletons': singletons,
        'params': {
            'fanin_depth': fanin_depth,
            'overlap_threshold': overlap_threshold,
            'min_region_size': min_region_size,
            'mode': mode,
            'max_region_size': max_region_size,
        },
    }


def _make_region(members, cones):
    cone_union = set()
    for r in members:
        cone_union |= cones[r]
    return {
        'rare_nodes': sorted(members),
        'size': len(members),
        'cone': cone_union,
        'cone_size': len(cone_union),
        'density': len(members) / float(max(1, len(cone_union))),
    }


def _regions_compact(rnames, neighbours, cones, min_region_size, max_region_size):
    """Greedy: seed at the highest-degree unused rare node, grab its closest
    unused neighbours up to max_region_size, remove, repeat."""
    used = set()
    regions, singletons = [], []
    # Order seeds by local density (number of trigger-combinable neighbours).
    order = sorted(rnames, key=lambda r: -len(neighbours[r]))
    for seed in order:
        if seed in used:
            continue
        nbrs = [x for x in neighbours[seed] if x not in used]
        if not nbrs:
            # No trigger-combinable partner -> singleton pocket.
            used.add(seed)
            if min_region_size <= 1:
                regions.append(_make_region([seed], cones))
            else:
                singletons.append(seed)
            continue
        # Rank candidate neighbours by how many *other* current-members they
        # also touch (tighter = better), grow up to the cap.
        members = [seed]
        used.add(seed)
        # simple greedy fill ordered by overlap degree with the seed set
        cand = sorted(nbrs, key=lambda x: -len(neighbours[x] & set(nbrs)))
        for x in cand:
            if len(members) >= max_region_size:
                break
            if x in used:
                continue
            members.append(x)
            used.add(x)
        if len(members) < min_region_size:
            singletons.extend(members)
        else:
            regions.append(_make_region(members, cones))
    return regions, singletons


def _regions_components(rnames, neighbours, cones, min_region_size):
    """Connected components of the overlap graph (original behaviour)."""
    parent = {r: r for r in rnames}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in rnames:
        for b in neighbours[a]:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

    comp = {}
    for r in rnames:
        comp.setdefault(find(r), []).append(r)

    regions, singletons = [], []
    for members in comp.values():
        if len(members) < min_region_size:
            singletons.extend(members)
        else:
            regions.append(_make_region(members, cones))
    return regions, singletons


def select_top_regions(region_result, top_k=1):
    """
    Return the union of rare-node names from the top-K densest regions, plus
    the list of selected region dicts. Regions are already sorted densest-first.
    """
    regions = region_result['regions']
    chosen = regions[:max(1, top_k)] if regions else []
    selected_rare = []
    for reg in chosen:
        selected_rare.extend(reg['rare_nodes'])
    # de-dup, stable
    seen = set()
    out = []
    for r in selected_rare:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out, chosen


def summarize_regions(region_result, top_n=10):
    """Return a human-readable summary table (list of rows) for logging/report."""
    rows = []
    for reg in region_result['regions'][:top_n]:
        rows.append({
            'region_id': reg['id'],
            'rare_nodes': reg['size'],
            'cone_gates': reg['cone_size'],
            'density': round(reg['density'], 4),
            'sample': reg['rare_nodes'][:4],
        })
    return rows
