import os
import logging
from collections import defaultdict
from loguru import logger

from itertools import product
import networkx as nx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

"""
LayeredGridGraph: a layered (z, row, col) grid graph for cell layout.

Naming conventions:
  - `_in_layer(layer, ...)` family for layer-keyed lookups; `_from(layer, key)`
    for "starting at" variants.
  - `is_*` for predicates; `get_*` reserved for node-input accessors.
  - Single-letter locals only for tuple-position math (`z, r, c`).
  - Caller dicts are never mutated.

Layer-kind classification ("PLACE" / "ROUTE") lets downstream code branch on
what a layer is used for. Caller-supplied dict mirrors the layer_to_direction
shape; partial classification is allowed (missing -> None).
  constructor kwarg : layer_to_kind = {"PC": "PLACE", "M0": "ROUTE", ...}
  attribute         : self.layer_to_kind  (every layer key present, value or None)
  module constants  : LAYER_KIND_PLACE, LAYER_KIND_ROUTE, LAYER_KINDS
  accessors         : layer_kind(layer), is_place_layer(layer),
                      is_route_layer(layer), layers_of_kind(kind)
  Validation: unknown layer names and bad kind values raise ValueError.

Behavioral notes (guard against regressions):
  - nodes_in_layer(parity=...) keys off "V"/"H" (matching layer_to_direction),
    not "vertical"/"horizontal".
  - is_edge_cross_site uses an explicit None guard on site_division_row; with a
    sentinel default it silently disabled itself for non-negative row coords.
  - sdcolwise uses each pair's own bottom layer for the gate-col filter, so it
    generalizes to multiple placement layers (PC, PC2, ...).
  - nearest_node_in_layer uses squared distance (argmin is sqrt-monotonic).

Non-public API is underscore-prefixed (verified no callers across placement.py,
routing.py, pin.py, rule.py). `stats` stays public: QFET._init_graph calls it.
`virtual_edges_along_col` is exposed read-only via a property over the
`_virtual_edges_along_col` backing cache.

Module surface (__all__):
    LayeredGridGraph, LAYER_KIND_PLACE, LAYER_KIND_ROUTE, LAYER_KINDS
"""

__all__ = [
    "LayeredGridGraph",
    "LAYER_KIND_PLACE",
    "LAYER_KIND_ROUTE",
    "LAYER_KINDS",
]

# Layer kinds - coarse classification of what a layer is used for, for downstream
# routing/placement code to branch on. Strings (not Enum) to match the existing
# "H"/"V" / "overlap" string-tag convention used elsewhere in this module.
LAYER_KIND_PLACE = "PLACE"
LAYER_KIND_ROUTE = "ROUTE"
LAYER_KINDS = frozenset({LAYER_KIND_PLACE, LAYER_KIND_ROUTE})


class LayeredGridGraph:
    def __init__(
        self,
        layer_to_rows,
        layer_to_cols,
        idx_to_layer,
        layer_to_direction,
        layer_to_kind=None,
        virtual_connect_pairs=None,
        virtual_connect_method="overlap",
        site_division_row=None,
        virtual_gate_cols=None,
        virtual_gate_rows=None,
    ):
        if virtual_connect_pairs is None:
            virtual_connect_pairs = []
        # normalize inputs into fresh dicts (do not mutate caller's data)
        layer_to_rows = {name: sorted({int(r) for r in rs}) for name, rs in layer_to_rows.items()}
        layer_to_cols = {name: sorted({int(c) for c in cs}) for name, cs in layer_to_cols.items()}

        # 1) Build "complete" row & col maps by merging across neighbors
        complete_rows = {}
        complete_cols = {}
        layer_indices = sorted(idx_to_layer)  # adjacency is by +/-1 in this list
        layer_to_idx = {name: z for z, name in idx_to_layer.items()}

        self._site_division_row = site_division_row  # row coord that splits sites; None = no site constraint

        # For each layer that lacks rows/cols, infer from the NEAREST layer in the
        # stack that has them (walk outward in z; not limited to z+/-1, since
        # several consecutive same-direction layers - e.g. QFET's 4 V-direction
        # placement tiers BPC2/BPC1/PC1/PC2 - can leave no H neighbor adjacent).
        def _infer_from_nearest(z, target_map, axis_label):
            for distance in range(1, max(layer_indices) + 1):
                hits = []
                for dz in (-distance, distance):
                    z2 = z + dz
                    if z2 in idx_to_layer and idx_to_layer[z2] in target_map:
                        hits.append(target_map[idx_to_layer[z2]])
                if hits:
                    return sorted(set().union(*hits))
            return None

        for z in layer_indices:
            layer_name = idx_to_layer[z]
            # rows
            if layer_name in layer_to_rows:
                complete_rows[layer_name] = layer_to_rows[layer_name]
            else:
                inferred = _infer_from_nearest(z, layer_to_rows, "rows")
                if inferred is None:
                    raise ValueError(f"Cannot infer rows for {layer_name!r}")
                complete_rows[layer_name] = inferred
            # cols
            if layer_name in layer_to_cols:
                complete_cols[layer_name] = layer_to_cols[layer_name]
            else:
                inferred = _infer_from_nearest(z, layer_to_cols, "cols")
                if inferred is None:
                    raise ValueError(f"Cannot infer cols for {layer_name!r}")
                complete_cols[layer_name] = inferred

        # ensure all rows/cols are unique within each layer
        for layer_name, rs in complete_rows.items():
            if len(rs) != len(set(rs)):
                raise ValueError(f"Duplicate rows in layer {layer_name!r}")
        for layer_name, cs in complete_cols.items():
            if len(cs) != len(set(cs)):
                raise ValueError(f"Duplicate cols in layer {layer_name!r}")

        # store for later lookup
        self._complete_rows = complete_rows
        self._complete_cols = complete_cols
        # O(1) coord -> position dicts; replace every cols.index(c) / rows.index(r)
        self._row_idx = {name: {r: i for i, r in enumerate(rs)} for name, rs in complete_rows.items()}
        self._col_idx = {name: {c: i for i, c in enumerate(cs)} for name, cs in complete_cols.items()}
        self.idx_to_layer = idx_to_layer
        self.layer_to_idx = layer_to_idx
        self.layer_to_direction = layer_to_direction

        # layer kind classification (PLACE / ROUTE). Optional; layers not listed map to None.
        self.layer_to_kind = self._normalize_layer_to_kind(layer_to_kind, idx_to_layer)

        # virtual cross-layer connections (resolve names -> z-indices)
        self._virtual_connect_method = virtual_connect_method
        self._virtual_gate_cols = virtual_gate_cols
        self._virtual_gate_rows = virtual_gate_rows
        virtual_pairs_idx = [(layer_to_idx[bot], layer_to_idx[top]) for bot, top in virtual_connect_pairs]

        # 2) build the graph
        self.G = nx.Graph()
        self._build_graph(complete_rows, complete_cols, idx_to_layer, virtual_pairs_idx)

    def _build_graph(self, complete_rows, complete_cols, idx_to_layer, virtual_pairs_idx=None):
        if virtual_pairs_idx is None:
            virtual_pairs_idx = []

        # add one node per (z, row, col) in each layer
        for z, layer_name in idx_to_layer.items():
            for r, c in product(complete_rows[layer_name], complete_cols[layer_name]):
                self.G.add_node((z, r, c), layer_idx=z, layer=layer_name, row=r, col=c)

        # add edges, restricting same-layer moves to layer_to_direction[layer_name]
        for z, layer_name in idx_to_layer.items():
            direction = self.layer_to_direction[layer_name]
            rows, cols = complete_rows[layer_name], complete_cols[layer_name]
            row_idx = self._row_idx[layer_name]
            col_idx = self._col_idx[layer_name]

            for r, c in product(rows, cols):
                u = (z, r, c)
                i, j = row_idx[r], col_idx[c]

                # same-layer neighbors, only along the layer's axis
                if direction == "H":
                    if j + 1 < len(cols):
                        self.G.add_edge(u, (z, r, cols[j + 1]))
                    if j - 1 >= 0:
                        self.G.add_edge(u, (z, r, cols[j - 1]))
                elif direction == "V":
                    if i + 1 < len(rows):
                        self.G.add_edge(u, (z, rows[i + 1], c))
                    if i - 1 >= 0:
                        self.G.add_edge(u, (z, rows[i - 1], c))
                else:
                    raise ValueError(f"Unknown direction {direction!r} for layer {layer_name!r}")

                # cross-layer neighbors (vias): connect to +/-1 layer if same (r, c) exists there
                for dz in (-1, +1):
                    z2 = z + dz
                    if z2 in idx_to_layer:
                        layer_name_2 = idx_to_layer[z2]
                        if r in complete_rows[layer_name_2] and c in complete_cols[layer_name_2]:
                            self.G.add_edge(u, (z2, r, c))

        self._virtual_edges_along_col = defaultdict(list)
        # add cross-layer "virtual" edges per the chosen connection method
        method = self._virtual_connect_method
        if virtual_pairs_idx and method == "overlap":
            # connect only nodes that overlap in (r, c) across the pair
            for zb, zt in virtual_pairs_idx:
                layer_b, layer_t = idx_to_layer[zb], idx_to_layer[zt]
                rows_b, cols_b = complete_rows[layer_b], complete_cols[layer_b]
                rows_t, cols_t = complete_rows[layer_t], complete_cols[layer_t]
                for r in set(rows_b) & set(rows_t):
                    for c in set(cols_b) & set(cols_t):
                        ub, ut = (zb, r, c), (zt, r, c)
                        self.G.add_edge(ub, ut)
                        self._virtual_edges_along_col[c].append((ub, ut))  # bot first, top second
            num_virtual = sum(len(v) for v in self._virtual_edges_along_col.values())
            logger.info(f"Added {num_virtual} virtual edges across {len(virtual_pairs_idx)} layer pair(s) using 'overlap' method.")

        elif virtual_pairs_idx and method == "colwise":
            # connect every (rb, c) to every (rt, c) along each shared column
            for zb, zt in virtual_pairs_idx:
                layer_b, layer_t = idx_to_layer[zb], idx_to_layer[zt]
                rows_b, cols_b = complete_rows[layer_b], complete_cols[layer_b]
                rows_t, cols_t = complete_rows[layer_t], complete_cols[layer_t]
                for c in set(cols_b) & set(cols_t):
                    for rb in rows_b:
                        for rt in rows_t:
                            ub, ut = (zb, rb, c), (zt, rt, c)
                            self.G.add_edge(ub, ut)
                            self._virtual_edges_along_col[c].append((ub, ut))

        elif virtual_pairs_idx and method == "sdcolwise":
            # Like 'colwise', restricted to source/drain columns of the BOTTOM layer of each
            # pair. The bottom layer's own column ordering defines even (gate) vs odd (S/D);
            # skipping even cols leaves S/D-only connections. Generalizes to any placement-
            # style layer (e.g. PC, PC2, ...) - uses each pair's own bottom layer so multiple
            # placement layers with distinct col layouts work.
            for zb, zt in virtual_pairs_idx:
                layer_b, layer_t = idx_to_layer[zb], idx_to_layer[zt]
                rows_b, cols_b = complete_rows[layer_b], complete_cols[layer_b]
                rows_t, cols_t = complete_rows[layer_t], complete_cols[layer_t]
                bot_col_idx = self._col_idx[layer_b]
                for c in set(cols_b) & set(cols_t):
                    if bot_col_idx[c] % 2 == 0:  # skip even (gate) cols of bot layer
                        continue
                    for rb in rows_b:
                        for rt in rows_t:
                            ub, ut = (zb, rb, c), (zt, rt, c)
                            self.G.add_edge(ub, ut)
                            self._virtual_edges_along_col[c].append((ub, ut))
            num_virtual = sum(len(v) for v in self._virtual_edges_along_col.values())
            logger.info(f"Added {num_virtual} virtual edges across {len(virtual_pairs_idx)} layer pair(s) using 'sdcolwise' method.")

        elif virtual_pairs_idx and method == "overlapGate":
            # Like 'overlap', restricted to gate columns (and optionally specific rows).
            assert self._virtual_gate_cols is not None, "overlapGate requires virtual_gate_cols"
            allowed_cols = set(self._virtual_gate_cols)
            allowed_rows = set(self._virtual_gate_rows) if self._virtual_gate_rows is not None else None
            for zb, zt in virtual_pairs_idx:
                layer_b, layer_t = idx_to_layer[zb], idx_to_layer[zt]
                rows_b, cols_b = complete_rows[layer_b], complete_cols[layer_b]
                rows_t, cols_t = complete_rows[layer_t], complete_cols[layer_t]
                for r in set(rows_b) & set(rows_t):
                    if allowed_rows is not None and r not in allowed_rows:
                        continue
                    for c in set(cols_b) & set(cols_t):
                        if c not in allowed_cols:
                            continue
                        ub, ut = (zb, r, c), (zt, r, c)
                        self.G.add_edge(ub, ut)
                        self._virtual_edges_along_col[c].append((ub, ut))
            num_virtual = sum(len(v) for v in self._virtual_edges_along_col.values())
            row_info = f" at {len(allowed_rows)} row(s)" if allowed_rows is not None else ""
            logger.info(f"Added {num_virtual} virtual edges at {len(allowed_cols)} gate col(s){row_info} using 'overlapGate' method.")

        elif virtual_pairs_idx and method == "boundary":
            # Like 'overlap', restricted to the first and last shared row of each column.
            for zb, zt in virtual_pairs_idx:
                layer_b, layer_t = idx_to_layer[zb], idx_to_layer[zt]
                rows_b, cols_b = complete_rows[layer_b], complete_cols[layer_b]
                rows_t, cols_t = complete_rows[layer_t], complete_cols[layer_t]
                common_rows = sorted(set(rows_b) & set(rows_t))
                if not common_rows:
                    continue
                boundary_rows = {common_rows[0], common_rows[-1]}
                for c in set(cols_b) & set(cols_t):
                    for r in boundary_rows:
                        ub, ut = (zb, r, c), (zt, r, c)
                        self.G.add_edge(ub, ut)
                        self._virtual_edges_along_col[c].append((ub, ut))
            num_virtual = sum(len(v) for v in self._virtual_edges_along_col.values())
            logger.info(f"Added {num_virtual} virtual edges at boundary rows using 'boundary' method.")

        if nx.is_directed(self.G):
            raise ValueError("Graph is not undirected")

    def is_node_in_graph(self, node):
        """
        Check if the node (z, row, col) is in the graph.
        """
        return node in self.G.nodes()

    def row_in_layer(self, layer, idx):
        """
        Return the row-coordinate at position `idx` in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        rows = self._complete_rows[layer_name]
        if not (0 <= idx < len(rows)):
            raise IndexError(f"Row index {idx} out of range for layer {layer_name!r}")
        return rows[idx]

    @property
    def virtual_edges_along_col(self):
        """
        Public read-only view of the per-column virtual (cross-layer "jump")
        edges built during construction: col -> list of (bottom_node, top_node).

        Additive accessor so callers (e.g. CFET._only_one_long_via_per_col) need
        not reach into the private `_virtual_edges_along_col` cache. Read-only;
        does not affect QFET, which never consults virtual edges.
        """
        return self._virtual_edges_along_col

    def col_in_layer(self, layer, idx):
        """
        Return the col-coordinate at position `idx` in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        cols = self._complete_cols[layer_name]
        if not (0 <= idx < len(cols)):
            raise IndexError(f"Col index {idx} out of range for layer {layer_name!r}")
        return cols[idx]

    def is_edge_cross_site(self, node_1, node_2):
        """
        Return True if an edge between node_1 and node_2 crosses the site division row.
        Always False when no site division row is configured (DH cell only).
        """
        if self._site_division_row is None:
            return False
        div = self._site_division_row
        if node_1[1] > div and node_2[1] < div:
            return True
        if node_2[1] > div and node_1[1] < div:
            return True
        return False

    def right_col_in_layer(self, layer, col):
        """
        Return the right column coordinate in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        cols = self._complete_cols[layer_name]
        idx_map = self._col_idx[layer_name]
        if col not in idx_map:
            raise ValueError(f"Col {col} not in layer {layer_name!r}")
        idx = idx_map[col]
        if idx == len(cols) - 1:
            return None
        return cols[idx + 1]

    def left_col_in_layer(self, layer, col):
        """
        Return the left column coordinate in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        cols = self._complete_cols[layer_name]
        idx_map = self._col_idx[layer_name]
        if col not in idx_map:
            raise ValueError(f"Col {col} not in layer {layer_name!r}")
        idx = idx_map[col]
        if idx == 0:
            return None
        return cols[idx - 1]

    def front_row_in_layer(self, layer, row, check_site=False):
        """
        Return the front row coordinate in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        rows = self._complete_rows[layer_name]
        idx_map = self._row_idx[layer_name]
        if row not in idx_map:
            raise ValueError(f"Row {row} not in layer {layer_name!r}")
        idx = idx_map[row]
        if idx == 0:
            return None
        # prevent crossing site division row
        if check_site and self._site_division_row is not None:
            div = self._site_division_row
            if rows[idx - 1] <= div and row >= div:
                return None
        return rows[idx - 1]

    def back_row_in_layer(self, layer, row, check_site=False):
        """
        Return the back row coordinate in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        rows = self._complete_rows[layer_name]
        idx_map = self._row_idx[layer_name]
        if row not in idx_map:
            raise ValueError(f"Row {row} not in layer {layer_name!r}")
        idx = idx_map[row]
        if idx == len(rows) - 1:
            return None
        # prevent crossing site division row
        if check_site and self._site_division_row is not None:
            div = self._site_division_row
            if rows[idx + 1] >= div and row <= div:
                return None
        return rows[idx + 1]

    def col_index_in_layer(self, layer, col):
        """
        Return the index of the given column in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        idx_map = self._col_idx[layer_name]
        if col not in idx_map:
            raise ValueError(f"Col {col} not in layer {layer_name!r}")
        return idx_map[col]

    def _row_index_in_layer(self, layer, row):
        """
        Return the index of the given row in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        idx_map = self._row_idx[layer_name]
        if row not in idx_map:
            raise ValueError(f"Row {row} not in layer {layer_name!r}")
        return idx_map[row]

    def node_at(self, layer, row, col):
        """
        Return the node (z, row, col) in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        if row not in self._row_idx[layer_name]:
            raise ValueError(f"Row {row} not in layer {layer_name!r}")
        if col not in self._col_idx[layer_name]:
            raise ValueError(f"Col {col} not in layer {layer_name!r}")
        return (z, row, col)

    def nodes_in_layer(self, layer, parity=None):
        """
        Iterate over all nodes (z, row, col) in the given layer,
        optionally filtering to only those whose row/col index is even or odd.

        Args:
          layer : int (layer index) or str (layer name)
          parity: None (default) -> return all nodes
                  "even"   -> return only nodes whose index along the *directional*
                              axis is even
                  "odd"    -> return only nodes whose index along that axis is odd

        Directional axis:
          - if layer_to_direction[layer]=="V" (vertical), we look at the *column* index
          - if layer_to_direction[layer]=="H" (horizontal), we look at the *row* index
        """
        # resolve
        z, layer_name = self._resolve_layer(layer)

        # grab all nodes in layer z
        all_nodes = [n for n in self.G.nodes() if n[0] == z]
        if parity is None:
            return all_nodes

        if parity not in ("even", "odd"):
            raise ValueError("parity must be one of {None, 'even', 'odd'}")

        # pick the right index-map (V layer indexes by col, H by row)
        direction = self.layer_to_direction[layer_name]
        if direction == "V":
            idx_map = self._col_idx[layer_name]
            get_idx = lambda n: idx_map[n[2]]
        elif direction == "H":
            idx_map = self._row_idx[layer_name]
            get_idx = lambda n: idx_map[n[1]]
        else:
            raise ValueError(f"Unknown direction {direction!r} for layer {layer_name!r}")

        want_even = parity == "even"
        filtered = [n for n in all_nodes if (get_idx(n) % 2 == 0) is want_even]
        return filtered

    def _nodes_in_layer_at(self, layer, row=None, col=None):
        """
        Return the list of nodes in the given vertical layer.
        """
        z, layer_name = self._resolve_layer(layer)
        if row is None and col is None:
            return [n for n in self.G.nodes() if n[0] == z]
        elif row is not None and col is not None:
            return [(z, row, col)]
        elif row is not None:
            return [(z, row, c) for c in self._complete_cols[layer_name]]
        elif col is not None:
            return [(z, r, col) for r in self._complete_rows[layer_name]]

    def _edges_in_layer(self, layer, sort=False):
        """
        Return the list of edges in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        edges = [(u, v) for u, v in self.G.edges() if u[0] == z and v[0] == z]
        if sort:
            edges.sort()
        return edges

    def cols_in_layer(self, layer, parity=None):
        """
        Return the list of column coordinates in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        cols = self._complete_cols[layer_name]
        if parity is None:
            return cols
        if parity not in ("even", "odd"):
            raise ValueError("parity must be one of {None, 'even', 'odd'}")
        want_even = parity == "even"
        return [c for i, c in enumerate(cols) if (i % 2 == 0) is want_even]

    def col_indices_in_layer(self, layer, parity=None):
        """
        Return the list of column indices in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        cols = self._complete_cols[layer_name]
        if parity is None:
            return list(range(len(cols)))
        if parity not in ("even", "odd"):
            raise ValueError("parity must be one of {None, 'even', 'odd'}")
        return [i for i in range(len(cols)) if (i % 2 == 0) is (parity == "even")]

    def rows_in_layer(self, layer, parity=None):
        """
        Return the list of row coordinates in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        rows = self._complete_rows[layer_name]
        if parity is None:
            return rows
        if parity not in ("even", "odd"):
            raise ValueError("parity must be one of {None, 'even', 'odd'}")
        want_even = parity == "even"
        return [r for i, r in enumerate(rows) if (i % 2 == 0) is want_even]

    def layer_index(self, layer):
        """
        Return the layer index (z) for the given layer (int index or str name).
        """
        z, _ = self._resolve_layer(layer)
        return z

    def layer_kind(self, layer):
        """
        Return the kind ("PLACE" / "ROUTE") of the given layer, or None if unclassified.
        """
        _, layer_name = self._resolve_layer(layer)
        return self.layer_to_kind[layer_name]

    def is_place_layer(self, layer):
        """Return True if the layer is classified as a placement layer."""
        return self.layer_kind(layer) == LAYER_KIND_PLACE

    def is_route_layer(self, layer):
        """Return True if the layer is classified as a routing layer."""
        return self.layer_kind(layer) == LAYER_KIND_ROUTE

    def layers_of_kind(self, kind):
        """
        Return the list of layer names with the given kind, ordered by layer index (z).
        """
        if kind not in LAYER_KINDS:
            raise ValueError(f"Unknown layer kind {kind!r}; expected one of {sorted(LAYER_KINDS)}")
        return [self.idx_to_layer[z] for z in sorted(self.idx_to_layer)
                if self.layer_to_kind[self.idx_to_layer[z]] == kind]

    def is_even_col(self, layer, col):
        """
        Return True if the column is at an even index in the given layer (i.e. a gate col).
        """
        z, layer_name = self._resolve_layer(layer)
        idx_map = self._col_idx[layer_name]
        if col not in idx_map:
            raise ValueError(f"Col {col} not in layer {layer_name!r}")
        return idx_map[col] % 2 == 0

    def is_odd_col(self, layer, col):
        """
        Return True if the column is at an odd index in the given layer (i.e. a source/drain col).
        """
        return not self.is_even_col(layer, col)

    def row_indices_in_layer(self, layer, parity=None):
        """
        Return the list of row indices in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        rows = self._complete_rows[layer_name]
        if parity is None:
            return list(range(len(rows)))
        if parity not in ("even", "odd"):
            raise ValueError("parity must be one of {None, 'even', 'odd'}")
        return [i for i in range(len(rows)) if (i % 2 == 0) is (parity == "even")]

    def max_col_in_layer(self, layer):
        """
        Return the maximum column coordinate in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        cols = self._complete_cols[layer_name]
        return max(cols)

    def max_row_in_layer(self, layer):
        """
        Return the maximum row coordinate in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        rows = self._complete_rows[layer_name]
        return max(rows)

    def num_cols_in_layer(self, layer):
        """
        Return the number of columns in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        cols = self._complete_cols[layer_name]
        return len(cols)

    def num_rows_in_layer(self, layer):
        """
        Return the number of rows in the given layer.
        """
        z, layer_name = self._resolve_layer(layer)
        rows = self._complete_rows[layer_name]
        return len(rows)

    def _resolve_layer(self, layer):
        """
        Return (z, layer_name) given a layer specified by index (int) or name (str).
        """
        if isinstance(layer, int):
            if layer not in self.idx_to_layer:
                raise KeyError(f"Unknown layer index {layer}")
            return layer, self.idx_to_layer[layer]
        if isinstance(layer, str):
            if layer not in self.layer_to_idx:
                raise KeyError(f"Unknown layer name {layer!r}")
            return self.layer_to_idx[layer], layer
        raise TypeError("layer must be int (index) or str (name)")

    @staticmethod
    def _normalize_layer_to_kind(layer_to_kind, idx_to_layer):
        """
        Build a complete {layer_name: kind_or_None} dict from the caller's input.

        - None input              -> every layer maps to None (no classification).
        - Unknown layer name      -> ValueError.
        - Bad kind value          -> ValueError.
        - Layer missing from dict -> kind is None (partial classification allowed).
        """
        known = set(idx_to_layer.values())
        result = {name: None for name in known}
        if layer_to_kind is None:
            return result
        for name, kind in layer_to_kind.items():
            if name not in known:
                raise ValueError(f"layer_to_kind has unknown layer {name!r}; known: {sorted(known)}")
            if kind is not None and kind not in LAYER_KINDS:
                raise ValueError(f"layer_to_kind[{name!r}] = {kind!r}; expected one of {sorted(LAYER_KINDS)} or None")
            result[name] = kind
        return result

    def edges(self):
        """
        Return the list of edges in the graph.
        """
        return list(self.G.edges())

    def arcs(self):
        """
        Return the list of arcs (bidirectional edges) in the graph.
        """
        return [(u, v) for u, v in self.G.edges()] + [(v, u) for u, v in self.G.edges()]

    def nodes(self):
        """
        Return the list of nodes in the graph.
        """
        return list(self.G.nodes())

    def stats(self):
        """
        Print the number of nodes and edges in the graph.
        """
        num_nodes = len(self.G.nodes())
        num_edges = len(self.G.edges())
        num_layers = len(self.idx_to_layer)
        num_arcs = len(self.arcs())
        logging.info(f"LayeredGridGraph has {num_nodes} nodes and {num_edges} edges and {num_layers} layers")
        logging.info(f"                 has {num_arcs} arcs (bidirectional edges)")
        return num_nodes, num_edges

    def _has_via_above(self, node):
        """
        Check if the node has a via above it in the graph.
        """
        if not self.is_node_in_graph(node):
            raise ValueError(f"Node {node} does not exist in the graph.")
        z, r, c = node
        above_node = (z + 1, r, c)
        return above_node in self.G.nodes()

    def _has_via_below(self, node):
        """
        Check if the node has a via below it in the graph.
        """
        if not self.is_node_in_graph(node):
            raise ValueError(f"Node {node} does not exist in the graph.")
        z, r, c = node
        above_node = (z - 1, r, c)
        return above_node in self.G.nodes()
    
    def _get_via_above(self, node):
        """
        Return the node above the given node in the graph.
        """
        if not self.is_node_in_graph(node):
            raise ValueError(f"Node {node} does not exist in the graph.")
        z, r, c = node
        above_node = (z + 1, r, c)
        if above_node not in self.G.nodes():
            raise LookupError(f"Expected via above {node} missing from graph.")
        return above_node
    
    def _get_via_below(self, node):
        """
        Return the node below the given node in the graph.
        """
        if not self.is_node_in_graph(node):
            raise ValueError(f"Node {node} does not exist in the graph.")
        z, r, c = node
        below_node = (z - 1, r, c)
        if below_node not in self.G.nodes():
            raise LookupError(f"Expected via below {node} missing from graph.")
        return below_node
    
    def rows_in_layer_from(self, layer, row):
        """
        Return the list of rows in the given layer starting at the specified row.
        Raises ValueError if the row does not exist.
        """
        z, layer_name = self._resolve_layer(layer)
        idx_map = self._row_idx[layer_name]
        if row not in idx_map:
            raise ValueError(f"Row {row} not in layer {layer_name!r}")
        return self._complete_rows[layer_name][idx_map[row]:]

    def cols_in_layer_from(self, layer, col):
        """
        Return the list of cols in the given layer starting at the specified col.
        Raises ValueError if the col does not exist.
        """
        z, layer_name = self._resolve_layer(layer)
        idx_map = self._col_idx[layer_name]
        if col not in idx_map:
            raise ValueError(f"Col {col} not in layer {layer_name!r}")
        return self._complete_cols[layer_name][idx_map[col]:]

    def get_right_neighbor(self, node):
        if not self.is_node_in_graph(node):
            raise ValueError(f"Node {node} does not exist in the graph.")
        z, r, c = node

        # Resolve layer and direction in one go
        try:
            layer = self.idx_to_layer[z]
            direction = self.layer_to_direction[layer]
        except KeyError as e:
            raise ValueError(f"Graph data inconsistency: {e}")

        if direction == "V":
            raise ValueError(f"Layer '{layer}' is vertical; no horizontal neighbor.")
        if direction != "H":
            raise ValueError(f"Unknown direction '{direction}' for layer '{layer}'.")

        # Find next column
        cols = self._complete_cols[layer]
        idx_map = self._col_idx[layer]
        if c not in idx_map:
            raise ValueError(f"Column {c} not in layer '{layer}'.")
        i = idx_map[c]
        if i == len(cols) - 1:
            return None  # No neighbor to the right

        neighbor = (z, r, cols[i + 1])
        if not self.is_node_in_graph(neighbor):
            raise LookupError(f"Expected neighbor {neighbor} missing from graph.")
        return neighbor

    def get_left_neighbor(self, node):
        if not self.is_node_in_graph(node):
            raise ValueError(f"Node {node} does not exist in the graph.")
        z, r, c = node

        # Resolve layer and direction in one go
        try:
            layer = self.idx_to_layer[z]
            direction = self.layer_to_direction[layer]
        except KeyError as e:
            raise ValueError(f"Graph data inconsistency: {e}")

        if direction == "V":
            raise ValueError(f"Layer '{layer}' is vertical; no horizontal neighbor.")
        if direction != "H":
            raise ValueError(f"Unknown direction '{direction}' for layer '{layer}'.")

        # Find previous column
        cols = self._complete_cols[layer]
        idx_map = self._col_idx[layer]
        if c not in idx_map:
            raise ValueError(f"Column {c} not in layer '{layer}'.")
        i = idx_map[c]
        if i == 0:
            return None
        neighbor = (z, r, cols[i - 1])
        if not self.is_node_in_graph(neighbor):
            raise LookupError(f"Expected neighbor {neighbor} missing from graph.")
        return neighbor

    # Convention (verified 07/26/25): in a V-layer, rows are sorted ascending;
    # "front" = previous row (smaller coord), "back" = next row (larger coord).
    def get_front_neighbor(self, node, check_site=False):
        if not self.is_node_in_graph(node):
            raise ValueError(f"Node {node} does not exist in the graph.")
        z, r, c = node

        # Resolve layer and direction in one go
        try:
            layer = self.idx_to_layer[z]
            direction = self.layer_to_direction[layer]
        except KeyError as e:
            raise ValueError(f"Graph data inconsistency: {e}")

        if direction == "H":
            raise ValueError(f"Layer '{layer}' is horizontal; no vertical neighbor.")
        if direction != "V":
            raise ValueError(f"Unknown direction '{direction}' for layer '{layer}'.")

        # Find previous row
        rows = self._complete_rows[layer]
        idx_map = self._row_idx[layer]
        if r not in idx_map:
            raise ValueError(f"Row {r} not in layer '{layer}'.")
        i = idx_map[r]
        if i == 0:
            return None
        neighbor = (z, rows[i - 1], c)
        if not self.is_node_in_graph(neighbor):
            raise LookupError(f"Expected neighbor {neighbor} missing from graph.")
        # prevent crossing site division line
        if check_site and self._site_division_row is not None:
            div = self._site_division_row
            if neighbor[1] <= div and r >= div:
                return None
        return neighbor

    def get_back_neighbor(self, node, check_site=False):
        if not self.is_node_in_graph(node):
            raise ValueError(f"Node {node} does not exist in the graph.")
        z, r, c = node

        # Resolve layer and direction in one go
        try:
            layer = self.idx_to_layer[z]
            direction = self.layer_to_direction[layer]
        except KeyError as e:
            raise ValueError(f"Graph data inconsistency: {e}")

        if direction == "H":
            raise ValueError(f"Layer '{layer}' is horizontal; no vertical neighbor.")
        if direction != "V":
            raise ValueError(f"Unknown direction '{direction}' for layer '{layer}'.")

        # Find next row
        rows = self._complete_rows[layer]
        idx_map = self._row_idx[layer]
        if r not in idx_map:
            raise ValueError(f"Row {r} not in layer '{layer}'.")
        i = idx_map[r]
        if i == len(rows) - 1:
            return None
        neighbor = (z, rows[i + 1], c)
        if not self.is_node_in_graph(neighbor):
            raise LookupError(f"Expected neighbor {neighbor} missing from graph.")
        # prevent crossing site division line
        if check_site and self._site_division_row is not None:
            div = self._site_division_row
            if neighbor[1] >= div and r <= div:
                return None
        return neighbor

    def nearest_node_in_layer(self, layer, row, col):
        """
        Return the node in the given layer closest to (row, col) by Euclidean distance.
        """
        z, layer_name = self._resolve_layer(layer)
        # Squared distance suffices for argmin (sqrt is monotonic).
        closest_node = None
        min_dist_sq = float("inf")
        for n in self.G.nodes():
            if n[0] != z:
                continue
            d_sq = (n[1] - row) ** 2 + (n[2] - col) ** 2
            if d_sq < min_dist_sq:
                min_dist_sq = d_sq
                closest_node = n
        if closest_node is None:
            raise ValueError(f"No nodes found in layer {layer_name!r}.")
        return closest_node

    @staticmethod
    def draw_one_layer_grid_2d(G, layer, node_size=40, edge_color="gray", node_edge_color="k", outdir=""):
        """
        Draw a single layer of G in 2D.

        Parameters:
          G               : networkx.Graph with nodes (z, r, c) and node['layer'] name
          layer           : int (layer index) or str (layer name)
          node_size       : size of each node marker
          edge_color      : color for in-layer edges
          node_edge_color : edgecolor for node markers
        """
        # sanity check
        if not hasattr(G, "nodes") or not hasattr(G, "edges"):
            raise TypeError("Expected a networkx.Graph")

        # extract unique layer-indices and names
        layers = sorted({n[0] for n in G.nodes()})
        layer_names = {z: next(iter({G.nodes[n]["layer"] for n in G.nodes() if n[0] == z})) for z in layers}

        # positions in 2D
        pos2d = {n: (n[2], n[1]) for n in G.nodes()}
        # x=col, y=row
        # collect nodes & in-layer edges
        nodes_z = [n for n in G.nodes() if n[0] == layer]
        edges_z = [(u, v) for u, v in G.edges() if u[0] == v[0] == layer]
        # draw edges
        fig, ax = plt.subplots(figsize=(6, 6))
        for u, v in edges_z:
            x0, y0 = pos2d[u]
            x1, y1 = pos2d[v]
            ax.plot([x0, x1], [y0, y1], color=edge_color, alpha=0.6, linewidth=1)
        # draw nodes
        xs = [pos2d[n][0] for n in nodes_z]
        ys = [pos2d[n][1] for n in nodes_z]
        ax.scatter(
            xs,
            ys,
            s=node_size,
            c=[plt.get_cmap("tab10")(layers.index(layer))],
            edgecolor=node_edge_color,
            zorder=5,
        )
        # remove border
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_visible(False)
        ax.spines["bottom"].set_visible(False)
        # disable grid
        ax.xaxis.set_major_locator(plt.NullLocator())
        ax.yaxis.set_major_locator(plt.NullLocator())
        # rotate ticks
        ax.tick_params(axis="x", rotation=45)
        ax.set_title(layer_names[layer])
        ax.set_xlabel("Column (x)")
        ax.set_ylabel("Row (y)")
        ax.set_aspect("equal")
        ax.grid(True, linestyle="--", alpha=0.3)
        plt.tight_layout()
        # plt.show()
        if outdir != "":
            plt.savefig(f"{outdir}/debug/graph_{layer}.png")
        else:
            plt.show()

    @staticmethod
    def draw_layered_grid_2d(G, node_size=40, edge_color="gray", node_edge_color="k", figsize_per_layer=(4, 4), outdir=""):
        """
        Draw every layer of G in its own 2D subplot, arranged side by side.

        Parameters:
          G               : networkx.Graph with nodes (z, r, c) and node['layer'] name
          node_size       : size of each node marker
          edge_color      : color for in-layer edges
          node_edge_color : edgecolor for node markers
          figsize_per_layer : (width, height) of each subplot
        """
        # sanity check
        if not hasattr(G, "nodes") or not hasattr(G, "edges"):
            raise TypeError("Expected a networkx.Graph")

        # extract unique layer-indices and names
        layers = sorted({n[0] for n in G.nodes()})
        layer_names = {z: next(iter({G.nodes[n]["layer"] for n in G.nodes() if n[0] == z})) for z in layers}

        # positions in 2D
        pos2d = {n: (n[2], n[1]) for n in G.nodes()}  # x=col, y=row

        # build subplots
        n = len(layers)
        fig, axes = plt.subplots(1, n, figsize=(figsize_per_layer[0] * n, figsize_per_layer[1]), sharex=True, sharey=True)
        if n == 1:
            axes = [axes]

        for ax, z in zip(axes, layers):
            # collect nodes & in-layer edges
            nodes_z = [n for n in G.nodes() if n[0] == z]
            edges_z = [(u, v) for u, v in G.edges() if u[0] == v[0] == z]

            # draw edges
            for u, v in edges_z:
                x0, y0 = pos2d[u]
                x1, y1 = pos2d[v]
                ax.plot([x0, x1], [y0, y1], color=edge_color, alpha=0.6, linewidth=1)

            # draw nodes
            xs = [pos2d[n][0] for n in nodes_z]
            ys = [pos2d[n][1] for n in nodes_z]
            ax.scatter(
                xs,
                ys,
                s=node_size,
                c=[plt.get_cmap("tab10")(layers.index(z))],
                edgecolor=node_edge_color,
                zorder=5,
            )
            # remove border
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.spines["left"].set_visible(False)
            ax.spines["bottom"].set_visible(False)
            # rotate ticks
            ax.tick_params(axis="x", rotation=45)
            ax.set_title(layer_names[z])
            ax.set_xlabel("Column (x)")
            ax.set_ylabel("Row (y)")
            ax.set_aspect("equal")
            ax.grid(True, linestyle="--", alpha=0.3)

        plt.tight_layout()
        # plt.show()
        if outdir != "":
            os.makedirs(f"{outdir}/debug", exist_ok=True)
            plt.savefig(f"{outdir}/debug/grid_2d.png")
        else:
            plt.show()

    @staticmethod
    def draw_layered_grid_3d(
        G,
        elev=30,
        azim=45,
        node_size=40,
        intra_edge_alpha=0.6,
        inter_edge_alpha=0.3,
        intra_color="gray",
        inter_color="black",
    ):
        """
        Draws a 3D networkx.Graph G with transparent background
        and no z-axis ticks.  Nodes are (z, r, c) triples.
        """
        if not hasattr(G, "nodes") or not hasattr(G, "edges"):
            raise TypeError("Expected networkx.Graph")

        # 1) positions & layer-info
        pos3d = {n: (n[2], n[1], n[0]) for n in G.nodes()}
        layers = sorted({n[0] for n in G.nodes()})
        layer_names = {z: next(iter({G.nodes[n]["layer"] for n in G.nodes() if n[0] == z})) for z in layers}
        cmap = plt.get_cmap("tab10")
        colors = {z: cmap(i % 10) for i, z in enumerate(layers)}

        # 2) figure & transparent background
        fig = plt.figure(figsize=(8, 6), facecolor="none")
        fig.patch.set_alpha(0)  # transparent figure
        ax = fig.add_subplot(111, projection="3d", facecolor="none")

        # make the 3D panes transparent
        for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
            axis.set_pane_color((1, 1, 1, 0))
            axis._axinfo["grid"]["color"] = (1, 1, 1, 0)

        # 3) view angles
        ax.view_init(elev=elev, azim=azim)

        # 4) split edges
        intra = [(u, v) for u, v in G.edges() if u[0] == v[0]]
        inter = [(u, v) for u, v in G.edges() if u[0] != v[0]]

        # 5) draw edges
        for u, v in intra:
            x, y, z = zip(pos3d[u], pos3d[v])
            ax.plot(x, y, z, color=intra_color, alpha=intra_edge_alpha, linewidth=1)
        for u, v in inter:
            x, y, z = zip(pos3d[u], pos3d[v])
            ax.plot(x, y, z, color=inter_color, alpha=inter_edge_alpha, linestyle="dashed", linewidth=1)

        # 6) draw nodes per layer
        for z in layers:
            xs = [pos3d[n][0] for n in G.nodes() if n[0] == z]
            ys = [pos3d[n][1] for n in G.nodes() if n[0] == z]
            zs = [pos3d[n][2] for n in G.nodes() if n[0] == z]
            ax.scatter(
                xs,
                ys,
                zs,
                s=node_size,
                c=[colors[z]],
                label=layer_names[z],
                edgecolor="k",
                alpha=0.9,
            )

        # 7) labels & legend & disable z-ticks
        ax.set_xlabel("Column (x)")
        ax.set_ylabel("Row (y)")
        ax.set_zlabel("Layer (z)")
        ax.set_zticks([])
        ax.legend(title="Layer Name", loc="upper left", bbox_to_anchor=(1.05, 1))
        plt.tight_layout()
        plt.show()