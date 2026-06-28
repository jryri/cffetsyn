"""CFFET result writer — extends CFET with Z tier + CFFET via classification."""

from src.cellgen.archit.CFET.util import (
    _merge_collinear_edges,
    _resolve_active_row_col,
    _resolve_edge_net,
)
from src.cellgen.core.entity import Model
from loguru import logger


# LGG metal indices for PROBE3_CFFET_2F_3T (bottom -> top).
_CFFET_LAYER_NAMES = {
    0: "BM1", 1: "BM0",
    2: "BBOTPC", 3: "BTOPPC",
    4: "FBOTPC", 5: "FTOPPC",
    6: "M0", 7: "M1", 8: "M2",
}


def _default_tier_for_model(c_tech, model: Model) -> str:
    """Front-block fallback when ``z_var`` is absent (legacy .res)."""
    if model == Model.PMOS:
        return c_tech.get_pmos_layer()
    return c_tech.get_nmos_layer()


def _resolve_z_tier(solver, tvar, tran, lgg, c_tech) -> str:
    z_var = getattr(tvar, "z_var", None)
    if z_var is not None and lgg is not None:
        zi = solver.Value(z_var)
        return lgg.idx_to_layer[zi]
    return _default_tier_for_model(c_tech, tran.model)


def _classify_cffet_via(u, v, lgg):
    """Tag cross-layer hops for CFFET's 9-metal LGG stack."""
    if u[0] == v[0]:
        return ""
    if lgg is None:
        return "VIA"
    pair = frozenset({u[0], v[0]})
    name_u = _CFFET_LAYER_NAMES.get(u[0])
    name_v = _CFFET_LAYER_NAMES.get(v[0])
    if name_u is None or name_v is None:
        return "VIA"

    checks = (
        ({"BBOTPC", "BM0"}, "BBOTCA"),
        ({"FBOTPC", "M0"}, "FBOTCA"),
        ({"BBOTPC", "BTOPPC"}, "BMIV"),
        ({"FBOTPC", "FTOPPC"}, "FMIV"),
        ({"BTOPPC", "FBOTPC"}, "STV"),
        ({"BM0", "BM1"}, "BV0"),
        ({"M0", "M1"}, "FV0"),
        ({"M1", "M2"}, "FV1"),
        ({"BTOPPC", "BM0"}, "BTOPCA"),
        ({"FTOPPC", "M0"}, "FTOPCA"),
    )
    names = {name_u, name_v}
    for key_names, tag in checks:
        if names == key_names:
            return tag

    # Adjacent metal layers without a dedicated tag.
    if abs(u[0] - v[0]) == 1:
        return "VIA"
    return "VIA"


def write_cffet_result(
    solver,
    circuit,
    transistor_vars,
    edge_vars,
    net_arc_vars,
    c_tech,
    cpp_cost,
    filename,
    lgg=None,
    mdi_at_col_vars=None,
):
    """
    Dump CFFET solve with an explicit placement tier (Z) column per transistor.

    Format mirrors QFET placement rows:
      Name X Y Z Flip Width Height SrcRow SrcCol SrcNet ...
    """
    placement_rows = []
    plc_layer = c_tech.get_domain_placement_layer()
    pc_pitch = c_tech.get_pitch(layer_name=plc_layer)
    m0_pitch = c_tech.get_pitch(layer_name="M0")

    for tran in circuit.transistors.values():
        tvar = transistor_vars[tran.name]
        x_idx = solver.Value(tvar.x_var)
        y_idx = solver.Value(tvar.y_var)
        flip_val = solver.Value(tvar.flip_var)
        model = str(tran.model).split(".")[1]
        z_name = _resolve_z_tier(solver, tvar, tran, lgg, c_tech)

        if lgg is not None:
            x = lgg.col_in_layer(plc_layer, x_idx)
            y = lgg.row_in_layer("M0", y_idx)
        else:
            x = x_idx * pc_pitch
            y = y_idx * m0_pitch * 2

        flip = "F" if flip_val == 1 else "NF"
        width = pc_pitch * 2
        height = (c_tech.num_rt_track - 1) * 2 * m0_pitch

        s_row, s_col = _resolve_active_row_col(
            solver, tvar.s_col_idx_var.get(tran.source, {})
        )
        d_row, d_col = _resolve_active_row_col(
            solver, tvar.d_col_idx_var.get(tran.drain, {})
        )
        g_row, g_col = _resolve_active_row_col(
            solver, tvar.g_col_idx_var.get(tran.gate, {})
        )

        placement_rows.append((
            tran.name,
            f"{x:.1f}", f"{y:.1f}", z_name, flip,
            f"{width:.1f}", f"{height:.1f}",
            f"{s_row:.1f}", f"{s_col:.1f}", tran.source,
            f"{d_row:.1f}", f"{d_col:.1f}", tran.drain,
            f"{g_row:.1f}", f"{g_col:.1f}", tran.gate,
            model,
        ))

    headers = (
        "Name", "X", "Y", "Z", "Flip", "Width", "Height",
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

        if mdi_at_col_vars and lgg is not None:
            plc_layer = c_tech.get_domain_placement_layer()
            mdi_rows = []
            for ci, var in mdi_at_col_vars.items():
                if solver.Value(var) == 1:
                    gate_col = lgg.col_in_layer(plc_layer, ci + 1)
                    mdi_rows.append((str(ci), str(gate_col), "MDI"))
            if mdi_rows:
                f.write("\n** MDI Split-Gate **\n")
                mdi_hdr = ("DeviceCol", "GateCol", "Tag")
                mdi_cols = list(zip(mdi_hdr, *mdi_rows))
                mdi_w = [max(len(str(x)) for x in col) for col in mdi_cols]
                mdi_fmt = "  ".join(f"{{:>{w}}}" for w in mdi_w) + "\n"
                f.write(mdi_fmt.format(*mdi_hdr))
                f.write("-" * (sum(mdi_w) + 2 * (len(mdi_w) - 1)) + "\n")
                for row in mdi_rows:
                    f.write(mdi_fmt.format(*row))

        f.write("\n** Cell Information **\n")
        f.write("IO Pins\n")
        f.write("-" * 22 + "\n")
        f.write(" ".join(circuit.io_pins) + "\n")

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
                via_type = _classify_cffet_via(u, v, lgg)
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
            parts2 = [
                f"{{:<{w}}}" if i in (0, 5) else
                f"{{:^{w}}}" if i == 4 else f"{{:>{w}}}"
                for i, w in enumerate(widths2)
            ]
            fmt2 = "  ".join(parts2) + "\n"
            f.write(fmt2.format(*hdrs2))
            f.write("-" * (sum(widths2) + 2 * (len(widths2) - 1)) + "\n")
            for row in routing_rows:
                f.write(fmt2.format(*row))

        f.write("\n** Technology Parameters **\n")
        f.write(f"{'Name':<25} {'Value':>14}\n")
        f.write("-" * 42 + "\n")
        tech_params = {
            # cpp_cost = max device-column index (odd SDG grid). Stacked CFFET:
            # one x column hosts up to 4 z-tiers → COL counts CPP from index
            # without the extra +1 boundary pad used in planar CFET/FinFET.
            "COL": max(1, solver.Value(cpp_cost) // 2 + 1),
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
            "M0_PWR_RAIL_THICKNESS": getattr(
                c_tech, "m0_power_rail_thickness", 0.036
            ) * 1e3,
            "PWR_CONFIG": c_tech.power_config,
        }
        for name, value in tech_params.items():
            f.write(f"{name:<25} {str(value):>14}\n")

    logger.info(f"CFFET result written to {filename}")
