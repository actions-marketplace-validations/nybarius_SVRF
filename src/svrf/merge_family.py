"""Select the train's first family without constructing its unused siblings.

The supplied order is unchanged: maximum cardinality, then lexicographically
least sorted PR numbers. Pair reads and union-path interpretation belong to the
caller. This module neither acquires a read nor admits or lands a merge.
"""
from __future__ import annotations


def normalize(nodes, edges):
    nodes = tuple(sorted(set(nodes)))
    if any(type(n) is not int for n in nodes):
        raise ValueError('INTEGER_LANES_REQUIRED')
    positions = {n: i for i, n in enumerate(nodes)}
    adjacency = [0] * len(nodes)
    kept = set()
    for a, b in edges:
        if a in positions and b in positions and a != b:
            i, j = positions[a], positions[b]
            adjacency[i] |= 1 << j
            adjacency[j] |= 1 << i
            kept.add(tuple(sorted((a, b))))
    return nodes, tuple(adjacency), tuple(sorted(kept))


def _bits(mask):
    while mask:
        bit = mask & -mask
        yield bit.bit_length() - 1
        mask ^= bit


def components(adjacency, mask):
    """Connected components of precisely the supplied conflict edges."""
    result = []
    while mask:
        todo = mask & -mask
        component = 0
        while todo:
            component |= todo
            mask &= ~todo
            neighbors = 0
            for v in _bits(todo):
                neighbors |= adjacency[v]
            todo = neighbors & mask
        result.append(component)
    return result


def plan(adjacency):
    """A shared finite decision DAG, constructed before evaluating any candidate.

    A split adds its independent components. Otherwise every independent set
    either omits v or contains v and omits its neighbors. Masks decrease on
    every edge; repeated masks name one retained state. No family list exists.
    """
    root = (1 << len(adjacency)) - 1
    states = {0: {'mask': 0, 'kind': 'zero', 'reads': []}}
    pending = [(root, False)]
    while pending:
        mask, ready = pending.pop()
        if mask in states:
            continue
        pieces = components(adjacency, mask)
        if len(pieces) > 1:
            row = {'mask': mask, 'kind': 'sum', 'reads': pieces}
        else:
            v = max(_bits(mask), key=lambda i: ((adjacency[i] & mask).bit_count(), -i))
            without = mask & ~(1 << v)
            row = {'mask': mask, 'kind': 'branch', 'vertex': v,
                   'reads': [without, without & ~adjacency[v]]}
        if ready:
            states[mask] = row
        else:
            pending.append((mask, True))
            pending.extend((child, False) for child in reversed(row['reads']) if child not in states)
    return list(states.values())


def evaluate(adjacency, states):
    """Exact encoding of the caller's existing two-part comparison.

    For k local vertices, each selected vertex contributes 2**k plus its
    descending rank bit. Cardinality dominates every rank bit; among equal
    cardinalities the larger bit field is exactly the lexicographically first
    sorted family. These are order codes, not measured resource prices.
    """
    k = len(adjacency)
    scores = {}
    for row in states:
        mask, reads = row['mask'], row['reads']
        if row['kind'] == 'zero':
            value = 0
        elif row['kind'] == 'sum':
            value = sum(scores[child] for child in reads)
        else:
            weight = (1 << k) + (1 << (k - 1 - row['vertex']))
            value = max(scores[reads[0]], weight + scores[reads[1]])
        scores[mask] = value
    return scores[(1 << k) - 1]


def decode(score, size):
    return [i for i in range(size) if score & (1 << (size - 1 - i))]


def compose(nodes, edges):
    """Factor disconnected lanes and share identical ordered component shapes.

    Sharing is local to this invocation and includes the complete adjacency,
    including absent edges. Original lane numbers remain on each component.
    No persisted cache can turn a changed/missing Git read into a clean pair.
    """
    nodes, adjacency, edges = normalize(nodes, edges)
    shapes, ids, bindings, chosen = [], {}, [], []
    for mask in components(adjacency, (1 << len(nodes)) - 1):
        indices = list(_bits(mask))
        local = tuple(sum(1 << j for j, other in enumerate(indices)
                          if adjacency[v] & (1 << other)) for v in indices)
        if local not in ids:
            states = plan(local)
            ids[local] = len(shapes)
            shapes.append({'adjacency': list(local), 'states': states,
                           'score': evaluate(local, states)})
        shape = ids[local]
        lanes = [nodes[i] for i in indices]
        bindings.append({'lanes': lanes, 'shape': shape})
        chosen.extend(lanes[i] for i in decode(shapes[shape]['score'], len(lanes)))
    return {'nodes': list(nodes), 'edges': [list(e) for e in edges],
            'chosen': sorted(chosen), 'components': bindings, 'shapes': shapes,
            'work': {'vertices': len(nodes), 'edges': len(edges),
                     'components': len(bindings), 'distinct_shapes': len(shapes),
                     'states': sum(len(s['states']) for s in shapes),
                     'families_materialized': 0}}


def maximum_family(nodes, edges):
    return compose(nodes, edges)['chosen']
