"""
QFET result writer.

QFET data model:
  - TransistorVar carries x_var / y_var / z_var / flip_var (no tier_var / site_var).
  - Placement-tier identity is the LGG z-index; mapping to layer name via
    `lgg.idx_to_layer[z]`. The placement tiers share the same column grid, but
    coords are still resolved through the active layer.
  - s/d/g_col_idx_var are keyed as `[net_name][z_idx][col_coord] -> [BoolVar, ...]`.
  - Both PMOS and NMOS may legally place on any placement tier (no per-model
    tier restriction).

The writer emits these sections:
  ** Placement Result **       per-transistor row (z = placement-tier layer name)
  ** Cell Information **       IO-pin list
  ** Routing Result **         active edge_vars (skipped when empty)
  ** Technology Parameters **  QFET_Tech / layer-stack dump

`lgg` is required for accurate physical coord resolution; falls back to
default-placement-tier pitch when omitted.
"""

from loguru import logger


def _resolve_active_col(solver, per_z_map, z_idx):
    """Return the col coord whose BoolVar is true for this transistor in z_idx, else -1."""
    for col, bvs in per_z_map.get(z_idx, {}).items():
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


def write_qfet_result(solver, circuit, transistor_vars, edge_vars, net_arc_vars,
                      q_tech, cpp_cost, filename, lgg=None):
    """
    Dump the QFET solve into a human-readable text file.

    Args:
        solver: CP-SAT solver (post-Solve).
        circuit: Circuit instance (provides .transistors, .io_pins).
        transistor_vars: {tran_name: TransistorVar}.
        edge_vars: {(u_node, v_node): BoolVar}; u/v are (layer_idx, row, col).
        net_arc_vars: {(net_name, u_arc, v_arc): BoolVar}; used to attribute
                      each active edge back to its net.
        q_tech: QFET_Tech instance.
        cpp_cost: CP-SAT IntVar for the overall CPP cost (col-index space).
        filename: Output file path.
        lgg: LayeredGridGraph - used to map (layer_name, index) -> coord.
             Optional but recommended for correct physical coords.
    """
    placement_rows = []
    default_layer = q_tech.default_placement_layer
    default_pitch = q_tech.get_pitch(default_layer)

    for tran in circuit.transistors.values():
        tvar = transistor_vars[tran.name]
        x_idx = solver.Value(tvar.x_var)
        y_idx = solver.Value(tvar.y_var)
        z_idx = solver.Value(tvar.z_var)
        flip_val = solver.Value(tvar.flip_var)

        # Active placement-tier layer name (z).
        layer_name = lgg.idx_to_layer[z_idx] if lgg is not None else default_layer
        pitch = q_tech.get_pitch(layer_name)

        # Physical coords - prefer LGG when available.
        if lgg is not None:
            x = lgg.col_in_layer(layer_name, x_idx)
            y = lgg.row_in_layer(layer_name, y_idx)
        else:
            x = x_idx * pitch
            y = y_idx * default_pitch * 2

        flip = "F" if flip_val == 1 else "NF"
        # Transistor footprint: gate + 2 s/d cols = 2 * pitch wide.
        width = pitch * 2
        # Transistor body spans the full pin-access row band: two M0 tracks
        # per row (front: rows 2-3 -> y in [96,144]; back: rows 0-1 -> y in
        # [0,48] with M0 pitch 24). Single-pitch height undercounts and draws
        # the body inside one track instead of the diffusion region.
        height = q_tech.get_pitch("M0") * 2

        # Resolved active s/d/g col coords for this transistor on its tier.
        s_col = _resolve_active_col(solver, tvar.s_col_idx_var.get(tran.source, {}), z_idx)
        d_col = _resolve_active_col(solver, tvar.d_col_idx_var.get(tran.drain,  {}), z_idx)
        g_col = _resolve_active_col(solver, tvar.g_col_idx_var.get(tran.gate,   {}), z_idx)

        model = str(tran.model).split(".")[1]
        placement_rows.append((
            tran.name,
            f"{x:.1f}", f"{y:.1f}", layer_name, flip,
            f"{width:.1f}", f"{height:.1f}",
            f"{s_col:.1f}" if s_col != -1 else "-1", tran.source,
            f"{d_col:.1f}" if d_col != -1 else "-1", tran.drain,
            f"{g_col:.1f}" if g_col != -1 else "-1", tran.gate,
            model,
        ))

    headers = (
        "Name", "X", "Y", "Z", "Flip",
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

        tech_params = {
            "COL": solver.Value(cpp_cost) // 2 + 2,
            "TRACK": q_tech.num_rt_track,
            "NUM_FIN": q_tech.num_fin,
            "NUM_SITES": q_tech.num_sites,
            "TECHNOLOGY": q_tech.TECHNOLOGY,
            "LIB_NAME": q_tech.lib_name,
            "HEIGHT_CONFIG": q_tech.height_config,
            "DIFF_BREAK_TYPE": q_tech.diffusion_break_type,
            "PWR_CONFIG": q_tech.power_config,
            "PWR_RAIL_THICKNESS_nm": q_tech.power_rail_thickness * 1e3,
            "M0_PITCH": q_tech.m0_pitch,
            "DEFAULT_PLC_LAYER": q_tech.default_placement_layer,
            "PLACEMENT_LAYERS": ",".join(sorted(q_tech.placement_layer_names)),
            "PIN_ACCESS_LAYERS": ",".join(sorted(q_tech.pin_access_layer_names)),
            "PIN_LAYER": ",".join(
                l.layer_name for l in q_tech.layer_stack.io_pin_layers()
            ),
        }
        # Per-placement-layer pitch / width
        for layer_name in sorted(q_tech.placement_layer_names):
            tech_params[f"{layer_name}_PITCH"] = q_tech.get_pitch(layer_name)
            tech_params[f"{layer_name}_WIDTH"] = q_tech.get_width(layer_name)
        # Per-pin-access-layer pitch / width
        for layer_name in sorted(q_tech.pin_access_layer_names):
            tech_params[f"{layer_name}_PITCH"] = q_tech.get_pitch(layer_name)
            tech_params[f"{layer_name}_WIDTH"] = q_tech.get_width(layer_name)

        for name, value in tech_params.items():
            f.write(f"{name:<25} {str(value):>14}\n")

    logger.info(f"Result written to {filename}")
