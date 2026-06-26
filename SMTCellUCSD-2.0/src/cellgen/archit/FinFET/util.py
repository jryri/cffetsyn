"""
FinFET result writer.

FinFET data model:
  - FinFET is a SINGLE-TIER PLANAR SH technology. There is exactly one placement
    tier (the PC layer), so TransistorVar carries x_var / y_var / flip_var with
    z_var left None - placement-tier identity is trivially the single placement
    layer (fin_tech.default_placement_layer, i.e. 'PC'). There is NO
    per-transistor z-index to dereference and NO Z column in the output.
  - s/g/d_col_idx_var are keyed two-deep as `[net_name][col_coord] -> [BoolVar,
    ...]` (QFET inserts an extra `[z_idx]` level because its four tiers share one
    column grid; FinFET, like CFET, resolves the active column directly per net
    since the tier is implicit).
  - X is reported in PC pitch, Y in M0 pitch (both PC and the routing rows share
    the single planar column grid).

The writer emits the same four sections as QFET / CFET:
  ** Placement Result **       per-transistor row - 13 tokens, NO Z column:
                                 Name X Y Flip Width Height SrcCol SrcNet
                                 DrnCol DrnNet GCol GNet Model
  ** Cell Information **        IO-pin list
  ** Routing Result **         active edge_vars (collinear chains merged; vias /
                               diagonals pass through). Each row emits EXACTLY
                               4 tokens on each side of '=>'
                               (MET ROW COL NET => MET ROW COL NET) with NO
                               trailing VIA column. Skipped when edge_vars empty.
  ** Technology Parameters **  FinFET_Tech block (height_config, num_fin,
                               num_rt_track, diffusion_break_type, layer-stack
                               dump)

CRITICAL - FORMAT CONTRACT WITH THE FinFET VISUALIZERS
  src/cellgen/postprocess/visualize_FinFET_4T.py load_results() parses
  (it handles both 3- and 4-track results):
    - PLACEMENT as 13 whitespace tokens with NO Z (tier) column and NO
      per-terminal Row columns:
        name=0, x=1, y=2, flip=3 ['F'/'NF'], width=4, height=5,
        SrcCol=6, SrcNet=7, DrnCol=8, DrnNet=9, GCol=10, GNet=11, Model=12.
      A terminal is present iff its Col token parses to float >= 0; otherwise we
      MUST emit '-1' so the visualizer drops that pin.
    - ROUTING with `lv, rv, cv, _ = right.split()` - i.e. EXACTLY 4 tokens each
      side of '=>'. Emitting a 5th (VIA) token on the right side makes
      right.split() yield 5 values and the unpack raises ValueError, which the
      parser swallows via `except ValueError: continue` - SILENTLY DROPPING the
      routing segment. So NO trailing VIA column here (unlike CFET).
  Diverging from this layout makes post-solve visualization throw or silently
  lose data. Do not add a Z column to placement or a VIA column to routing.

`lgg` is required for accurate physical coord resolution; falls back to PC / M0
pitch when omitted.
"""

from loguru import logger


def _resolve_active_col(solver, per_net_map):
    """Return the col coord whose BoolVar is true for this transistor, else -1.

    `per_net_map` is the two-deep FinFET col map scoped to a single net, i.e.
    `{col_coord: [BoolVar, ...]}`. (QFET keys an extra z-index level; FinFET is
    single-tier so the active layer is implicit in the per-net map and we iterate
    the columns directly - identical resolution to CFET.)
    """
    for col, bvs in per_net_map.items():
        for bv in bvs:
            if solver.Value(bv) == 1:
                return col
    return -1


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


def write_finfet_result(solver, circuit, transistor_vars, edge_vars, net_arc_vars,
                        fin_tech, cpp_cost, filename, lgg=None):
    """
    Dump the FinFET solve into a human-readable text file.

    Args:
        solver: CP-SAT solver (post-Solve).
        circuit: Circuit instance (provides .transistors, .io_pins).
        transistor_vars: {tran_name: TransistorVar}.
        edge_vars: {(u_node, v_node): BoolVar}; u/v are (layer_idx, row, col).
        net_arc_vars: {(net_name, u_arc, v_arc): BoolVar}; used to attribute
                      each active edge back to its net.
        fin_tech: FinFET_Tech instance.
        cpp_cost: CP-SAT IntVar for the overall CPP cost (col-index space).
        filename: Output file path.
        lgg: LayeredGridGraph - used to map (layer_name, index) -> coord.
             Optional but recommended for correct physical coords.

    Output layout MUST match the FinFET visualizers exactly: PLACEMENT is 13
    tokens with NO Z (tier) column, ROUTING is 4 + 4 tokens with NO trailing
    VIA column. See the module docstring's FORMAT CONTRACT section.
    """
    placement_rows = []
    # FinFET is single-tier planar: one placement layer (PC) for everything.
    plc_layer = fin_tech.default_placement_layer
    plc_pitch = fin_tech.get_pitch(layer_name=plc_layer)
    m0_pitch = fin_tech.get_pitch(layer_name="M0")

    for tran in circuit.transistors.values():
        tvar = transistor_vars[tran.name]
        x_idx = solver.Value(tvar.x_var)
        y_idx = solver.Value(tvar.y_var)
        flip_val = solver.Value(tvar.flip_var)

        # Physical coords - prefer LGG when available. Single planar tier, so X
        # resolves through the placement layer and Y through the M0 row grid.
        if lgg is not None:
            x = lgg.col_in_layer(plc_layer, x_idx)
            y = lgg.row_in_layer("M0", y_idx)
        else:
            x = x_idx * plc_pitch
            y = y_idx * m0_pitch * 2

        flip = "F" if flip_val == 1 else "NF"
        # Transistor footprint: gate + 2 s/d cols = 2 * placement pitch wide.
        width = plc_pitch * 2
        # Planar FinFET device height: half the routing tracks tall (one device
        # per row). NOT the CFET form (num_rt_track-1)*2*M0 - that is the taller
        # 2-tier-stacked CFET body and draws FinFET transistors 3x too tall (e.g.
        # 144 instead of 48 at 4T).
        height = fin_tech.num_rt_track / 2 * m0_pitch

        # Resolved active s/g/d col coords for this transistor. FinFET col maps
        # are two-deep ([net][col]); scope to the actual terminal net.
        s_col = _resolve_active_col(solver, tvar.s_col_idx_var.get(tran.source, {}))
        d_col = _resolve_active_col(solver, tvar.d_col_idx_var.get(tran.drain,  {}))
        g_col = _resolve_active_col(solver, tvar.g_col_idx_var.get(tran.gate,   {}))

        model = str(tran.model).split(".")[1]
        # 13-token placement row - NO Z (tier) column (FinFET is single-tier),
        # NO per-terminal Row columns. A terminal is present iff its Col token
        # parses to a float >= 0, else '-1' (the visualizer drops it).
        placement_rows.append((
            tran.name,
            f"{x:.1f}", f"{y:.1f}", flip,
            f"{width:.1f}", f"{height:.1f}",
            f"{s_col:.1f}" if s_col != -1 else "-1", tran.source,
            f"{d_col:.1f}" if d_col != -1 else "-1", tran.drain,
            f"{g_col:.1f}" if g_col != -1 else "-1", tran.gate,
            model,
        ))

    headers = (
        "Name", "X", "Y", "Flip",
        "Width", "Height",
        "SrcCol", "SrcNet", "DrnCol", "DrnNet", "GCol", "GNet",
        "Model",
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
        # (layer, net, axis) run. Vias and diagonals pass through. Skipped
        # entirely when edge_vars is empty (routing not wired).
        #
        # EXACTLY 4 tokens per side of '=>' (MET ROW COL NET). NO trailing VIA
        # column - the FinFET parser does `lv, rv, cv, _ = right.split()` and a
        # 5th token would make the unpack raise and the row be dropped.
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
                routing_rows.append((
                    str(u[0]), str(u[1]), str(u[2]), net,
                    "=>",
                    str(v[0]), str(v[1]), str(v[2]), net,
                ))
            hdrs2 = ("MET", "ROW", "COL", "NET", "", "MET", "ROW", "COL", "NET")
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

        # Technology-Parameters block consumed by gds_FinFET_SH.py. Keys/values
        # follow the .res format so GDS generation parses cleanly: pitches/widths
        # come straight from the layer stack (nm), gap/rail-thickness from the
        # tech (um -> nm).
        tech_params = {
            "COL": solver.Value(cpp_cost) // 2 + 2,
            "TRACK": fin_tech.num_rt_track,
            "CPP": fin_tech.get_pitch("PC"),
            "M0P": fin_tech.get_pitch("M0"),
            "M1P": fin_tech.get_pitch("M1"),
            "M2P": fin_tech.get_pitch("M2"),
            "CP_WIDTH": fin_tech.get_width("PC"),
            "M0_WIDTH": fin_tech.get_width("M0"),
            "M1_WIDTH": fin_tech.get_width("M1"),
            "M2_WIDTH": fin_tech.get_width("M2"),
            "ACTIVE_GAP": getattr(fin_tech, "active_gap", 0.014) * 1e3,
            "PWR_RAIL_THICKNESS": getattr(fin_tech, "power_rail_thickness", 0.036) * 1e3,
            "PWR_CONFIG": fin_tech.power_config,
        }

        for name, value in tech_params.items():
            f.write(f"{name:<25} {str(value):>14}\n")

    logger.info(f"Result written to {filename}")
