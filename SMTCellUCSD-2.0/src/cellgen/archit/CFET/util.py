"""
CFET result writer.

CFET data model:
  - TransistorVar carries x_var / y_var / flip_var (no z_var). Placement-tier
    identity is the PHYSICAL device layer (PC for the top device, BPC for the
    bottom device), resolved from the transistor's model through the stacking
    configuration: `c_tech.get_pmos_layer()` / `c_tech.get_nmos_layer()` return
    'PC' or 'BPC'. There is no per-transistor z-index to dereference.
  - s/d/g_col_idx_var are keyed two-deep as `[net_name][col_coord] -> [BoolVar,
    ...]`; there is no z level, so the active column is resolved directly per
    layer.
  - Both PC and BPC share the same column grid, so X is reported in PC pitch.

The writer emits four sections:
  ** Placement Result **       per-transistor row (Z = physical device layer)
  ** Cell Information **        IO-pin list
  ** Routing Result **         active edge_vars (collinear chains merged; vias
                               classified as BPC->M0 long via, BPC<->PC MIV, or
                               a generic VIA); skipped when edge_vars is empty
  ** Technology Parameters **  CFET_Tech block (stacking_config, power_config,
                               m0_power_rail_thickness, layer-stack dump)

`lgg` is required for accurate physical coord resolution and via classification;
falls back to PC/M0 pitch when omitted.
"""

import re

from loguru import logger

# Candidate S/G/D BoolVar names encode the landing node as ..._R<row>_C<col>.
_NODE_RC_RE = re.compile(r"_R(\d+)_C(\d+)$")


def _resolve_active_col(solver, per_net_map):
    """Return the col coord whose BoolVar is true for this transistor, else -1.

    `per_net_map` is the two-deep CFET col map scoped to a single net, i.e.
    `{col_coord: [BoolVar, ...]}`. There is no z level, so the active layer is
    implicit in the per-net map and we iterate the columns directly.
    """
    for col, bvs in per_net_map.items():
        for bv in bvs:
            if solver.Value(bv) == 1:
                return col
    return -1


def _resolve_active_row_col(solver, per_net_map):
    """Return (row, col) of the active S/G/D candidate, else (-1, -1).

    The col is the per-net map key; the row is parsed from the winning
    BoolVar's name (``..._R<row>_C<col>``). The .res emits Row+Col per terminal
    (the format gds_CFET_SH.py and visualize_CFET_4T.py both consume).
    """
    for col, bvs in per_net_map.items():
        for bv in bvs:
            if solver.Value(bv) == 1:
                m = _NODE_RC_RE.search(bv.Name())
                row = int(m.group(1)) if m else -1
                return row, col
    return -1, -1


def _resolve_edge_net(solver, net_arc_vars, u, v):
    """
    Find which net's arc var is true for either direction of edge (u, v).
    Returns net name or None. net_arc_vars is keyed as (net_name, u_arc, v_arc).
    """
    for (net_name, u_arc, v_arc), bv in net_arc_vars.items():
        if ((u_arc == u and v_arc == v) or (u_arc == v and v_arc == u)) \
                and solver.Value(bv) == 1:
            return net_name
    return None


def _merge_collinear_edges(active_edges, edge_nets):
    """Collapse chains of collinear, same-net, same-layer edges into one
    segment. Cross-layer edges (vias) and diagonal edges pass through
    untouched. Returns a list of (u, v, net) tuples ready to emit.

    Algorithm:
      1. For each unprocessed edge, classify it (via | diagonal | h | v).
         Vias / diagonals emit as-is.
      2. For h/v edges, repeatedly scan for an unprocessed candidate on the
         same (layer, net, axis) whose endpoint touches the current path's
         start or end, and extend. Stop when no candidate matches.

    O(n^2) in active edges; fine for cell-sized routing graphs.
    """
    segments = []
    for u, v in active_edges:
        net = edge_nets.get((u, v), "N/A")
        segments.append({"u": u, "v": v, "net": net})

    processed = [False] * len(segments)
    out = []

    def _axis(u, v):
        """Return 'via' if cross-layer, 'h' for horizontal (row match),
        'v' for vertical (col match), 'd' for diagonal."""
        if u[0] != v[0]:
            return "via"
        if u[1] == v[1] and u[2] != v[2]:
            return "h"
        if u[2] == v[2] and u[1] != v[1]:
            return "v"
        return "d"

    for i, seg in enumerate(segments):
        if processed[i]:
            continue
        processed[i] = True
        kind = _axis(seg["u"], seg["v"])
        if kind in ("via", "d"):
            out.append((seg["u"], seg["v"], seg["net"]))
            continue

        cu, cv, net = seg["u"], seg["v"], seg["net"]
        layer = cu[0]
        # Extend while a same-axis, same-net, same-layer neighbour touches
        # either endpoint of the current path.
        while True:
            extended = False
            for j, cand in enumerate(segments):
                if processed[j]:
                    continue
                cu2, cv2 = cand["u"], cand["v"]
                if cu2[0] != cv2[0] or cu2[0] != layer or cand["net"] != net:
                    continue
                if _axis(cu2, cv2) != kind:
                    continue
                if cv == cu2:
                    cv = cv2
                elif cv2 == cu:
                    cu = cu2
                elif cv == cv2:
                    cv = cu2
                elif cu == cu2:
                    cu = cv2
                else:
                    continue
                processed[j] = True
                extended = True
                break
            if not extended:
                break
        out.append((cu, cv, net))
    return out


def _classify_via(u, v, lgg):
    """Tag a cross-layer (via) routing segment by the layer pair it spans.

    CFET has two physically distinct vertical connections that the solver may
    place (see CFET._only_one_long_via_per_col / _only_one_miv_per_col):
      - BPC <-> M0 : a "long via" punching the bottom device straight to M0.
      - BPC <-> PC : a MIV (metal inter-via) between the two device layers.
    Anything else (or an intra-layer segment) is a plain wire / generic VIA.

    Returns one of "BPC2M0", "MIV", "VIA", or "" (non-via). lgg supplies the
    BPC/PC/M0 layer indices; when omitted, all vias degrade to "VIA".
    """
    if u[0] == v[0]:
        return ""
    if lgg is None:
        return "VIA"
    pair = {u[0], v[0]}
    try:
        bpc_idx = lgg.layer_index("BPC")
        pc_idx = lgg.layer_index("PC")
        m0_idx = lgg.layer_index("M0")
    except Exception:
        return "VIA"
    if pair == {bpc_idx, m0_idx}:
        return "BPC2M0"
    if pair == {bpc_idx, pc_idx}:
        return "MIV"
    return "VIA"


def write_cfet_result(solver, circuit, transistor_vars, edge_vars, net_arc_vars,
                      c_tech, cpp_cost, filename, lgg=None):
    """
    Dump the CFET solve into a human-readable text file.

    Args:
        solver: CP-SAT solver (post-Solve).
        circuit: Circuit instance (provides .transistors, .io_pins).
        transistor_vars: {tran_name: TransistorVar}.
        edge_vars: {(u_node, v_node): BoolVar}; u/v are (layer_idx, row, col).
        net_arc_vars: {(net_name, u_arc, v_arc): BoolVar}; used to attribute
                      each active edge back to its net.
        c_tech: CFET_Tech instance.
        cpp_cost: CP-SAT IntVar for the overall CPP cost (col-index space).
        filename: Output file path.
        lgg: LayeredGridGraph - used to map (layer_name, index) -> coord and to
             classify via segments. Optional but recommended for correct
             physical coords.
    """
    placement_rows = []
    # Canonical placement tier (CFET: "PC"; CFFET: "FTOPPC"). All placement
    # tiers share the column grid, so X coords are reported in this tier's pitch.
    plc_layer = c_tech.get_domain_placement_layer()
    pc_pitch = c_tech.get_pitch(layer_name=plc_layer)
    m0_pitch = c_tech.get_pitch(layer_name="M0")

    for tran in circuit.transistors.values():
        tvar = transistor_vars[tran.name]
        x_idx = solver.Value(tvar.x_var)
        y_idx = solver.Value(tvar.y_var)
        flip_val = solver.Value(tvar.flip_var)
        model = str(tran.model).split(".")[1]

        # Physical coords - prefer LGG when available. Both PC and BPC share the
        # column grid, so X is reported in PC pitch.
        if lgg is not None:
            x = lgg.col_in_layer(plc_layer, x_idx)
            y = lgg.row_in_layer("M0", y_idx)
        else:
            x = x_idx * pc_pitch
            y = y_idx * m0_pitch * 2

        flip = "F" if flip_val == 1 else "NF"
        # Transistor footprint: gate + 2 s/d cols = 2 * PC pitch wide.
        width = pc_pitch * 2
        # CFET stacks PMOS over NMOS, so the body spans the full 2-tier band.
        height = (c_tech.num_rt_track - 1) * 2 * m0_pitch

        # Resolved active S/G/D (row, col) for this transistor. CFET col maps
        # are two-deep ([net][col]); scope to the actual terminal net. The
        # device tier (PC top / BPC bottom) is inferred downstream from `model`.
        s_row, s_col = _resolve_active_row_col(solver, tvar.s_col_idx_var.get(tran.source, {}))
        d_row, d_col = _resolve_active_row_col(solver, tvar.d_col_idx_var.get(tran.drain, {}))
        g_row, g_col = _resolve_active_row_col(solver, tvar.g_col_idx_var.get(tran.gate, {}))

        placement_rows.append((
            tran.name,
            f"{x:.1f}", f"{y:.1f}", flip,
            f"{width:.1f}", f"{height:.1f}",
            f"{s_row:.1f}", f"{s_col:.1f}", tran.source,
            f"{d_row:.1f}", f"{d_col:.1f}", tran.drain,
            f"{g_row:.1f}", f"{g_col:.1f}", tran.gate,
            model,
        ))

    headers = (
        "Name", "X", "Y", "Flip", "Width", "Height",
        "SrcRow", "SrcCol", "SrcNet", "DrnRow", "DrnCol", "DrnNet",
        "GRow", "GCol", "GNet", "Model",
    )
    cols = list(zip(headers, *placement_rows))
    col_widths = [max(len(str(item)) for item in col) for col in cols]

    parts = [f"{{:<{w}}}" if i == 0 else f"{{:>{w}}}" for i, w in enumerate(col_widths)]
    fmt = "  ".join(parts) + "\n"

    with open(filename, "w") as f:
        f.write(f"** Objective value: {solver.ObjectiveValue()}\n")
        f.write(f"** CPP cost (col idx): {solver.Value(cpp_cost)}\n\n")

        f.write("** Placement Result **\n")
        f.write(fmt.format(*headers))
        f.write("-" * (sum(col_widths) + 2 * (len(col_widths) - 1)) + "\n")
        for row in placement_rows:
            f.write(fmt.format(*row))

        f.write("\n** Cell Information **\n")
        f.write("IO Pins\n")
        f.write("-" * 22 + "\n")
        f.write(" ".join(circuit.io_pins) + "\n")

        # ---- Routing ---------------------------------------------------- #
        # Active edges, with collinear chains merged into one segment per
        # (layer, net, axis) run. Vias and diagonals pass through, tagged with
        # the CFET via class (BPC->M0 long via vs BPC<->PC MIV). Skipped
        # entirely when edge_vars is empty (routing not wired).
        active_edges = [
            (u, v) for (u, v), bv in edge_vars.items() if solver.Value(bv) == 1
        ]
        if active_edges:
            f.write("\n** Routing Result **\n")
            edge_nets = {
                (u, v): (_resolve_edge_net(solver, net_arc_vars, u, v) or "N/A")
                for u, v in active_edges
            }
            merged = _merge_collinear_edges(active_edges, edge_nets)
            routing_rows = []
            for u, v, net in merged:
                via_type = _classify_via(u, v, lgg)
                routing_rows.append((
                    str(u[0]), str(u[1]), str(u[2]), net,
                    "=>",
                    str(v[0]), str(v[1]), str(v[2]), net,
                    via_type,
                ))
            hdrs2 = ("MET", "ROW", "COL", "NET", "",
                     "MET", "ROW", "COL", "NET", "VIA")
            cols2 = list(zip(hdrs2, *routing_rows))
            widths2 = [max(len(item) for item in col) for col in cols2]
            parts2 = [f"{{:<{w}}}" if i in (0, 5) else
                      f"{{:^{w}}}" if i == 4 else f"{{:>{w}}}"
                      for i, w in enumerate(widths2)]
            fmt2 = "  ".join(parts2) + "\n"
            f.write(fmt2.format(*hdrs2))
            f.write("-" * (sum(widths2) + 2 * (len(widths2) - 1)) + "\n")
            for row in routing_rows:
                f.write(fmt2.format(*row))

        # ---- Technology parameters -------------------------------------- #
        f.write("\n** Technology Parameters **\n")
        f.write(f"{'Name':<25} {'Value':>14}\n")
        f.write("-" * 42 + "\n")

        # Canonical Technology-Parameters block consumed by gds_CFET_SH.py (and
        # the reference flow). CFET uses M0_PWR_RAIL_THICKNESS (vs FinFET's
        # PWR_RAIL_THICKNESS). Pitches/widths come from the layer stack (nm).
        tech_params = {
            "COL": solver.Value(cpp_cost) // 2 + 2,
            "TRACK": c_tech.num_rt_track,
            "CPP": c_tech.get_pitch(plc_layer),
            "M0P": c_tech.get_pitch("M0"),
            "M1P": c_tech.get_pitch("M1"),
            "M2P": c_tech.get_pitch("M2"),
            "CP_WIDTH": c_tech.get_width(plc_layer),
            "M0_WIDTH": c_tech.get_width("M0"),
            "M1_WIDTH": c_tech.get_width("M1"),
            "M2_WIDTH": c_tech.get_width("M2"),
            "ACTIVE_GAP": getattr(c_tech, "active_gap", 0.014) * 1e3,
            "M0_PWR_RAIL_THICKNESS": getattr(c_tech, "m0_power_rail_thickness", 0.036) * 1e3,
            "PWR_CONFIG": c_tech.power_config,
        }

        for name, value in tech_params.items():
            f.write(f"{name:<25} {str(value):>14}\n")

    logger.info(f"Result written to {filename}")
