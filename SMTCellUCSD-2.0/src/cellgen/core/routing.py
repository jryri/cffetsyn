"""
Routing-related constraints for FinFET layout optimization.
This module contains all routing constraint implementations.
"""

import math
import re
from collections import OrderedDict

from loguru import logger
from ortools.sat.python import cp_model
from src.cellgen.core.entity import Model
from src.cellgen.core.util import sliding_windows

_NUM_COL_SDG_ = 3  # number of columns needed for source/drain/gate


def prohibit_routing_to_left_cell_boundaries(instance):
    """
    Ban every edge that touches the left cell boundary (col == 0).

    Layer-agnostic: applies on every LGG layer (placement + pin-access + routing).
    No per-tier dispatch needed - col index 0 is the same physical zero on every
    layer (all placement tiers share the col grid; routing layers ride the same
    canvas). Edge bans propagate to arc / flow via link_arc_to_edge.
    """
    instance.opt.log_comment("Prohibiting routing to left cell boundaries ...")
    logger.info("\t==\tProhibiting routing to left cell boundaries ...")
    left_bound_col = 0
    gathered = [
        instance.edge_vars[(u, v)]
        for u, v in instance.lgg.edges()
        if u[2] == left_bound_col or v[2] == left_bound_col
    ]
    instance.opt.Add(sum(gathered) == 0)


def prohibit_routing_to_right_cell_boundaries(instance):
    """
    Ban every edge whose col exceeds the cell's right boundary at the chosen
    cpp_cost value (cell-wide; layer-agnostic).

    For each candidate cpp_cost (in plc_ci), compute the physical right-boundary
    col from the placement-tier pitch and reify a `cpp_is_<v>` BoolVar so the
    edge bans are conditional on cpp_cost == v. Edge bans propagate to arc/flow
    via link_arc_to_edge / link_flow_to_arc.

    No hardcoded "PC": all placement tiers share the same col pitch, so the
    default placement layer's pitch is canonical.
    """
    instance.opt.log_comment("Prohibiting routing to right cell boundaries ...")
    logger.info("\t==\tProhibiting routing to right cell boundaries ...")
    pitch = math.ceil(instance.q_tech.get_pitch(instance.q_tech.default_placement_layer))
    for possible_cpp in instance.plc_ci:
        right_bound_col = (possible_cpp + (_NUM_COL_SDG_ - 1)) * pitch
        gathered = [
            instance.edge_vars[(u, v)]
            for u, v in instance.lgg.edges()
            if u[2] > right_bound_col or v[2] > right_bound_col
        ]
        cpp_bool = instance.opt.NewBoolVar(f"cpp_is_{possible_cpp}")
        instance.opt.Add(instance.cpp_cost == possible_cpp).OnlyEnforceIf(cpp_bool)
        instance.opt.Add(instance.cpp_cost != possible_cpp).OnlyEnforceIf(cpp_bool.Not())
        instance.opt.Add(sum(gathered) == 0).OnlyEnforceIf(cpp_bool)


def bind_gate_sharing_to_columns(instance, db_as_gs=True):
    """
    Per-tier gate sharing reification.

    For every (placement-tier, gate-col) slot, build a `gate_share` BoolVar
    that is True iff either (a) some cross-MOS pair on this tier shares its
    gate at this col, or (b) (when `db_as_gs`) no transistor is placed at
    this slot at all. False => a gate cut exists at (zi, col_r).

    Output is nested per tier:
        instance.gate_share_at_col_vars[zi][col_r] = gate_share BoolVar

    Notes:
      - No hardcoded "PC". Iterates every layer in `q_tech.placement_layer_names`,
        looks up `zi = lgg.layer_to_idx[layer_name]`, and uses that layer's own
        col grid via `lgg.col_in_layer(layer_name, ci+1)`.
      - Per-slot reification: gates `tv` on `placed_tran_at_xzi_vars[(t, ci, zi)]`
        (not `placed_tran_ci_vars[(t, ci)]`) so two pairs sharing the same gate
        net on different tiers are scored independently.
      - "no transistor" branch uses `has_tran_at_xzi_vars[(ci, zi)]`.
      - `gate_share_pair_vars` already bakes in `z_eq` (see
        placement.pairwise_gate_sharing) so a pair var being true on this slot
        already implies both transistors are on the same tier.
    """
    instance.opt.log_comment("Per-tier binding of gate sharing to columns ...")
    logger.info("\t==\tPer-tier binding of gate sharing to columns ...")
    tech = instance.q_tech

    for layer_name in sorted(tech.placement_layer_names):
        zi = instance.lgg.layer_to_idx[layer_name]
        per_tier = instance.gate_share_at_col_vars.setdefault(zi, OrderedDict())

        for ci in instance.plc_ci:
            col_r = instance.lgg.col_in_layer(layer_name, ci + 1)
            gate_share = instance.opt.NewBoolVar(f"gate_share_zi{zi}_col{col_r}")
            per_tier[col_r] = gate_share

            has_tran = instance.has_tran_at_xzi_vars[(ci, zi)]

            # 1) Per-pair "tran shares gate at this slot" reifiers.
            tmp = []
            for key, gs_pair in instance.gate_share_pair_vars.items():
                m = re.match(r"gate_share_(M\w+)_(M\w+)_(\w+)", key)
                if not m:
                    continue
                t1, t2, net = m.group(1), m.group(2), m.group(3)
                p1 = instance.placed_tran_at_xzi_vars[(t1, ci, zi)]
                p2 = instance.placed_tran_at_xzi_vars[(t2, ci, zi)]
                tv = instance.opt.NewBoolVar(
                    f"tran_gs_zi{zi}_col{col_r}_{t1}_{t2}_{net}"
                )
                instance.opt.Add(tv == 1).OnlyEnforceIf([gs_pair, p1, p2])
                instance.opt.Add(gs_pair == 1).OnlyEnforceIf(tv)
                instance.opt.Add(p1 == 1).OnlyEnforceIf(tv)
                instance.opt.Add(p2 == 1).OnlyEnforceIf(tv)
                tmp.append(tv)

            # 2) gate_share <=> (OR(tmp) [OR not has_tran when db_as_gs])
            if db_as_gs:
                instance.opt.AddBoolOr(tmp + [has_tran.Not()]).OnlyEnforceIf(gate_share)
            else:
                instance.opt.AddBoolOr(tmp).OnlyEnforceIf(gate_share)
            for tv in tmp:
                instance.opt.AddImplication(tv, gate_share)
            instance.opt.AddImplication(has_tran.Not(), gate_share)

            # gate_share=False => no pair active AND at least one transistor placed
            instance.opt.Add(sum(tmp) == 0).OnlyEnforceIf(gate_share.Not())
            instance.opt.AddImplication(gate_share.Not(), has_tran)

        # 3) Leftmost gate col on this tier is always shared (cell-edge artifact).
        first_col = instance.lgg.col_in_layer(layer_name, 2)
        instance.opt.log_comment(
            f"Allowing gate sharing at leftmost gate col on tier {layer_name} ..."
        )
        instance.opt.Add(per_tier[first_col] == 1)


def gate_cut_window(instance):
    """
    Per-tier sliding-window reification of "gate is cut over X consecutive cols".

    Built on top of the per-tier `gate_share_at_col_vars[zi][col]` map produced
    by `bind_gate_sharing_to_columns`. For every placement tier, slides a
    window of width `min_gate_cut_len` over that tier's ordered cols and creates
    one `gcw_var` per window such that:

        gcw_var = AND over col in window of NOT gate_share_at_col_vars[zi][col]

    Plus:
      - cpp_cost lower bound: an OOB window can only be active if the cpp_cost
        actually extends past it (right-edge guard, cell-wide).
      - continuity: every gate-cut col on this tier must be covered by exactly
        one active window (== exactly `min_gate_cut_len` long; lengthen by
        relaxing to `>= 1` if longer cuts are allowed).

    Notes:
      - No hardcoded "PC". For each placement layer, derive the tier's own
        col list and use `lgg.col_index_in_layer(layer_name, ...)` for the
        ci-lookup in the OOB guard.
      - `gate_cut_window_vars` is now nested per tier: `[zi][window_tuple]`.
    """
    instance.opt.log_comment("Per-tier definition of gate cut windows ...")
    logger.info(
        f"\t==\tEnforcing gate cut boundary condition to "
        f"{instance.min_boundary_col} / {instance.max_boundary_col} per tier ..."
    )

    for layer_name in sorted(instance.q_tech.placement_layer_names):
        zi = instance.lgg.layer_to_idx[layer_name]
        per_tier_gs = instance.gate_share_at_col_vars.get(zi, {})
        if not per_tier_gs:
            continue

        windows = sliding_windows(list(per_tier_gs.keys()), instance.min_gate_cut_len)
        per_tier_gcw = {
            w: instance.opt.NewBoolVar(f"gate_cut_window_zi{zi}_{w}")
            for w in windows
        }
        instance.gate_cut_window_vars[zi] = per_tier_gcw

        # 1) Right-edge OOB guard
        for gcw in windows:
            if any(c > instance.min_boundary_col for c in gcw):
                max_col = max(gcw)
                max_ci = instance.lgg.col_index_in_layer(layer_name, max_col)
                plc_ci_in_gcw = max_ci - 1
                instance.opt.Add(
                    instance.cpp_cost >= plc_ci_in_gcw
                ).OnlyEnforceIf(per_tier_gcw[gcw])

        # 2) gcw_var <=> AND over col in window of NOT gate_share
        for gcw in windows:
            gs_vars = [per_tier_gs[col] for col in gcw]
            gcw_var = per_tier_gcw[gcw]
            instance.opt.Add(gcw_var == 1).OnlyEnforceIf([v.Not() for v in gs_vars])
            for gs_var in gs_vars:
                instance.opt.Add(gcw_var == 0).OnlyEnforceIf(gs_var)

        # 3) Continuity: every gate-cut col is covered by exactly one window
        for gcol, gs in per_tier_gs.items():
            covering = [per_tier_gcw[w] for w in windows if gcol in w]
            instance.opt.Add(sum(covering) == 1).OnlyEnforceIf(gs.Not())


def prohibit_pc_routing_in_diffusion_break_cols(instance):
    """
    Per-tier PC-routing prohibition at diffusion-break columns.

    For every (placement-tier, plc_ci) slot: if a P-side or N-side diffusion
    break is placed at that slot, ban every edge / arc / flow that uses the
    immediate-right gate col on that tier through the corresponding model's
    pin-access rows. (The slot's own col `c` and the right-side s/d col `crr`
    stay free - `crr` belongs to the next transistor.)

    Notes:
      - No hardcoded "PC". Iterates every layer in `q_tech.placement_layer_names`,
        and uses that layer's own `lgg.col_in_layer(layer_name, ci+1)` and
        `lgg.row_in_layer(layer_name, ri)` for the pin-access rows.
      - Per-slot DB vars: `db_pmos_vars[(ci, zi)]` / `db_nmos_vars[(ci, zi)]`
        (col-aggregate would be wrong; DBs are tier-local).
      - Constraint scope is the placement tier itself: the layer-component of
        each gathered edge/arc must equal `zi`.
    """
    instance.opt.log_comment(
        "Per-tier ban of routing through gate col at diffusion-break slots ..."
    )
    nets = list(instance.circuit.get_nets(with_power_ground=False))

    for layer_name in sorted(instance.q_tech.placement_layer_names):
        zi = instance.lgg.layer_to_idx[layer_name]
        pmos_rows = {instance.lgg.row_in_layer(layer_name, ri)
                     for ri in instance.pmos_pin_access_ri}
        nmos_rows = {instance.lgg.row_in_layer(layer_name, ri)
                     for ri in instance.nmos_pin_access_ri}

        for ci in instance.plc_ci:
            pdb_var = instance.db_pmos_vars.get((ci, zi))
            ndb_var = instance.db_nmos_vars.get((ci, zi))
            if pdb_var is None and ndb_var is None:
                continue
            try:
                cr = instance.lgg.col_in_layer(layer_name, ci + 1)
            except IndexError:
                continue

            def _gather(rows):
                """Edges / arcs / flows on this tier at col cr in the given pin-access rows."""
                edges, arcs, flows = [], [], []
                for u, v in instance.lgg.edges():
                    if u[0] != zi:
                        continue
                    if (u[1] in rows and u[2] == cr) or (v[1] in rows and v[2] == cr):
                        edges.append(instance.edge_vars[(u, v)])
                for net in nets:
                    for ua, va in instance.lgg.arcs():
                        if ua[0] != zi:
                            continue
                        if (ua[1] in rows and ua[2] == cr) or (va[1] in rows and va[2] == cr):
                            arcs.append(instance.net_arc_vars[(net.name, ua, va)])
                for net in nets:
                    for k in range(instance.net_to_flow_cnt[net.name]):
                        for ua, va in instance.lgg.arcs():
                            if ua[0] != zi:
                                continue
                            if (ua[1] in rows and ua[2] == cr) or (va[1] in rows and va[2] == cr):
                                flows.append(instance.net_flow_vars[(net.name, k, ua, va)])
                return edges, arcs, flows

            if pdb_var is not None:
                p_edges, p_arcs, p_flows = _gather(pmos_rows)
                instance.opt.Add(sum(p_edges) == 0).OnlyEnforceIf(pdb_var)
                instance.opt.Add(sum(p_arcs)  == 0).OnlyEnforceIf(pdb_var)
                instance.opt.Add(sum(p_flows) == 0).OnlyEnforceIf(pdb_var)
            if ndb_var is not None:
                n_edges, n_arcs, n_flows = _gather(nmos_rows)
                instance.opt.Add(sum(n_edges) == 0).OnlyEnforceIf(ndb_var)
                instance.opt.Add(sum(n_arcs)  == 0).OnlyEnforceIf(ndb_var)
                instance.opt.Add(sum(n_flows) == 0).OnlyEnforceIf(ndb_var)


def enforce_CA_pickup_for_gate_cut(instance):
    """
    Per-tier CA-pickup enforcement at gate-cut columns.

    Reads `instance.gate_share_at_col_vars[zi][col]` produced by
    `bind_gate_sharing_to_columns`. For each placement tier and each gate col
    on it: when the gate is CUT (gate_share=False), forbid via edges that
    would pick the gate stripe up through the tier's adjacent layers
    (zi-1, zi+1) at the inner pin-access rows.

    "Inner pin-access rows" = pin-access rows other than the row farthest
    from the placement tier (the "middle" rows). For QFET 4-track SH this is
    row index 1 (NMOS inner) and 2 (PMOS inner). The constraint forces any
    CA pickup at a gate cut to use the OUTER row instead.

    Notes:
      - No hardcoded layers 0 / 1 or "PC" lookups. Iterates placement tiers
        from `q_tech.placement_layer_names`, computes the (zi-1, zi+1)
        adjacency from the LGG, and reads the inner row coords off the
        placement tier itself.
      - No 3-track/4-track special branches: the inner-row set is derived
        directly from `nmos_pin_access_ri` / `pmos_pin_access_ri` by dropping
        the outermost index on each side.
    """
    instance.opt.log_comment("Per-tier enforcement of CA pickup for gate cut ...")
    logger.info("\t==\tPer-tier enforcement of CA pickup for gate cut ...")

    # Inner pin-access row indices = drop outermost.
    nmos_inner = sorted(instance.nmos_pin_access_ri)[1:] \
        if len(instance.nmos_pin_access_ri) > 1 else []
    pmos_inner = sorted(instance.pmos_pin_access_ri)[:-1] \
        if len(instance.pmos_pin_access_ri) > 1 else []
    inner_ri = list(nmos_inner) + list(pmos_inner)
    if not inner_ri:
        logger.info("\t==\tNo inner pin-access rows on this height_config; skipping")
        return

    max_zi = max(instance.lgg.layer_to_idx.values())
    nets = list(instance.circuit.get_nets(with_power_ground=False))

    for layer_name in sorted(instance.q_tech.placement_layer_names):
        zi = instance.lgg.layer_to_idx[layer_name]
        per_tier_gs = instance.gate_share_at_col_vars.get(zi, {})
        if not per_tier_gs:
            continue

        inner_rows_phys = {instance.lgg.row_in_layer(layer_name, ri) for ri in inner_ri}
        adj_zis = {z for z in (zi - 1, zi + 1) if 0 <= z <= max_zi}

        for gcol, gs_var in per_tier_gs.items():
            # Via edges between (zi) and its adjacent layers, at gcol, on inner rows.
            edges = []
            for u, v in instance.lgg.edges():
                if u[2] != gcol or v[2] != gcol:
                    continue
                if u[1] != v[1] or u[1] not in inner_rows_phys:
                    continue
                pair = {u[0], v[0]}
                if zi in pair and pair & adj_zis:
                    edges.append(instance.edge_vars[(u, v)])

            arcs = []
            for net in nets:
                for ua, va in instance.lgg.arcs():
                    if ua[2] != gcol or va[2] != gcol:
                        continue
                    if ua[1] != va[1] or ua[1] not in inner_rows_phys:
                        continue
                    pair = {ua[0], va[0]}
                    if zi in pair and pair & adj_zis:
                        arcs.append(instance.net_arc_vars[(net.name, ua, va)])

            flows = []
            for net in nets:
                for k in range(instance.net_to_flow_cnt[net.name]):
                    for ua, va in instance.lgg.arcs():
                        if ua[2] != gcol or va[2] != gcol:
                            continue
                        if ua[1] != va[1] or ua[1] not in inner_rows_phys:
                            continue
                        pair = {ua[0], va[0]}
                        if zi in pair and pair & adj_zis:
                            flows.append(instance.net_flow_vars[(net.name, k, ua, va)])

            instance.opt.Add(sum(edges) == 0).OnlyEnforceIf(gs_var.Not())
            instance.opt.Add(sum(arcs)  == 0).OnlyEnforceIf(gs_var.Not())
            instance.opt.Add(sum(flows) == 0).OnlyEnforceIf(gs_var.Not())


def _gather_contact_via_vars(instance, zi, col, rows):
    """
    Return the list of via-edge BoolVars adjacent to placement tier `zi` at
    physical col `col`, restricted to the given pin-access `rows`.

    A "contact via" is an LGG edge whose two endpoints share `col` and `row`
    (vertical via on a single col-row), with one endpoint on tier `zi` and the
    other on `zi+/-1`. Used by the gate/LISD contact-cap helpers below.
    """
    out = []
    rows = set(rows)
    for u, v in instance.lgg.edges():
        if u[2] != col or v[2] != col:
            continue
        if u[1] != v[1] or u[1] not in rows:
            continue
        pair = {u[0], v[0]}
        if zi in pair and (pair - {zi}).pop() in (zi - 1, zi + 1):
            out.append(instance.edge_vars[(u, v)])
    return out


def limit_gate_contact(instance, num_contact=1):
    """
    Per-tier cap on the number of CA-style gate-contact vias at each gate col.

    For every (placement-tier, plc_ci) slot:
      - gather PMOS-side via edges between `zi` and `zi+/-1` at col_r, on the
        PMOS pin-access rows of THIS tier;
      - same for NMOS;
      - when the per-tier `gate_share_at_col_vars[zi][col_r]` is True, allow
        at most `num_contact` total (PMOS+NMOS share one contact);
      - when False, allow at most `num_contact` on each side independently.

    Notes:
      - No hardcoded "PC". Iterates `q_tech.placement_layer_names`, uses each
        tier's own `col_in_layer` / `row_in_layer`.
      - Uses `_gather_contact_via_vars(self, zi, col, rows)` to gather only
        vias adjacent to the chosen placement tier.
      - Reads from per-tier `gate_share_at_col_vars[zi][col_r]` (nested).
    """
    instance.opt.log_comment(f"Per-tier limit gate contact to {num_contact} ...")
    for layer_name in sorted(instance.q_tech.placement_layer_names):
        zi = instance.lgg.layer_to_idx[layer_name]
        per_tier_gs = instance.gate_share_at_col_vars.get(zi, {})
        if not per_tier_gs:
            continue
        pmos_rows = {instance.lgg.row_in_layer(layer_name, ri)
                     for ri in instance.pmos_pin_access_ri}
        nmos_rows = {instance.lgg.row_in_layer(layer_name, ri)
                     for ri in instance.nmos_pin_access_ri}

        for ci in instance.plc_ci:
            col_r = instance.lgg.col_in_layer(layer_name, ci + 1)
            gs_col_var = per_tier_gs[col_r]
            pmos_vias = _gather_contact_via_vars(instance, zi, col_r, pmos_rows)
            nmos_vias = _gather_contact_via_vars(instance, zi, col_r, nmos_rows)
            instance.opt.Add(
                sum(pmos_vias + nmos_vias) <= num_contact
            ).OnlyEnforceIf(gs_col_var)
            instance.opt.Add(sum(pmos_vias) <= num_contact).OnlyEnforceIf(gs_col_var.Not())
            instance.opt.Add(sum(nmos_vias) <= num_contact).OnlyEnforceIf(gs_col_var.Not())


def bind_lisd_sharing_to_columns(instance):
    """
    Per-tier LISD sharing reification.

    For every (placement-tier, plc_ci) slot, build `lisd_share` BoolVars at
    BOTH the left S/D col (`col`, ci) and the right S/D col (`col_rr`, ci+2):

        lisd_share_at_col_vars[zi][col]     <=> some cross-MOS pair shares
                                                LISD at (zi, col), or no
                                                transistor is placed at (ci, zi).
        lisd_share_at_col_vars[zi][col_rr]  <=> same, for the right S/D col.

    `lisd_share_pair_vars[key]` (from placement.pairwise_lisd_sharing) already
    bakes `x_eq AND z_eq` into its reifier, so a pair var being true on a slot
    already implies both transistors are on the same tier.

    Flip / column mapping (NF = flip 0; F = flip 1):
        S-S: both sources -> col when both NF, col_rr when both F
        D-D: both drains  -> col when both F,  col_rr when both NF
        S-D: t1 src, t2 drn -> col when t1 NF & t2 F, col_rr when t1 F & t2 NF
        D-S: t1 drn, t2 src -> col when t1 F & t2 NF, col_rr when t1 NF & t2 F

    Notes:
      - No hardcoded "PC". Iterates `q_tech.placement_layer_names`, derives
        `zi = lgg.layer_to_idx[layer_name]`, uses each tier's own
        `col_in_layer(layer_name, ...)`.
      - Output nested per tier: `lisd_share_at_col_vars[zi][col]`.
      - Per-slot reification: uses `placed_tran_at_xzi_vars[(t, ci, zi)]`
        (not col-aggregate `placed_tran_ci_vars[(t, ci)]`) so two pairs
        on different tiers don't false-trigger each other.
      - "no transistor" branch uses `has_tran_at_xzi_vars[(ci, zi)]`.
    """
    instance.opt.log_comment("Per-tier binding of LISD sharing to columns ...")
    logger.info("\t==\tPer-tier binding of LISD sharing to columns ...")

    for layer_name in sorted(instance.q_tech.placement_layer_names):
        zi = instance.lgg.layer_to_idx[layer_name]
        per_tier = instance.lisd_share_at_col_vars.setdefault(zi, OrderedDict())

        # Pre-create the lisd_share BoolVars for every unique col on this tier
        # that could be a left- or right-side S/D col of some plc_ci.
        for ci in instance.plc_ci:
            for off in (0, 2):
                c = instance.lgg.col_in_layer(layer_name, ci + off)
                if c not in per_tier:
                    per_tier[c] = instance.opt.NewBoolVar(
                        f"lisd_share_zi{zi}_col{c}"
                    )

        for ci in instance.plc_ci:
            col    = instance.lgg.col_in_layer(layer_name, ci)
            col_rr = instance.lgg.col_in_layer(layer_name, ci + 2)
            share_col    = per_tier[col]
            share_col_rr = per_tier[col_rr]
            has_tran     = instance.has_tran_at_xzi_vars[(ci, zi)]

            tmp_col, tmp_col_rr = [], []

            for key, ls_pair in instance.lisd_share_pair_vars.items():
                m = re.match(r"lisd_share_(M\w+)_(M\w+)_(\w+)", key)
                if not m:
                    continue
                t1, t2, net = m.group(1), m.group(2), m.group(3)
                p1 = instance.placed_tran_at_xzi_vars[(t1, ci, zi)]
                p2 = instance.placed_tran_at_xzi_vars[(t2, ci, zi)]
                f1 = instance.transistor_vars[t1].flip_var
                f2 = instance.transistor_vars[t2].flip_var
                sn1 = instance.circuit.transistors[t1].source
                dn1 = instance.circuit.transistors[t1].drain
                sn2 = instance.circuit.transistors[t2].source
                dn2 = instance.circuit.transistors[t2].drain

                t1_is_src = (sn1 == net)
                t1_is_drn = (dn1 == net)
                t2_is_src = (sn2 == net)
                t2_is_drn = (dn2 == net)

                tv    = instance.opt.NewBoolVar(
                    f"tran_lisd_zi{zi}_col{col}_{t1}_{t2}_{net}")
                tv_rr = instance.opt.NewBoolVar(
                    f"tran_lisd_zi{zi}_colrr{col_rr}_{t1}_{t2}_{net}")

                # Flip table: which (flip1, flip2) combo lands the shared net
                # at `col` vs `col_rr`, given the (src/drn, src/drn) pairing.
                if t1_is_src and t2_is_src:
                    flip_col, flip_col_rr = (f1.Not(), f2.Not()), (f1, f2)
                elif t1_is_drn and t2_is_drn:
                    flip_col, flip_col_rr = (f1, f2), (f1.Not(), f2.Not())
                elif t1_is_src and t2_is_drn:
                    flip_col, flip_col_rr = (f1.Not(), f2), (f1, f2.Not())
                elif t1_is_drn and t2_is_src:
                    flip_col, flip_col_rr = (f1, f2.Not()), (f1.Not(), f2)
                else:
                    raise ValueError(
                        f"Net '{net}' does not connect to S/D of both {t1} and {t2}: "
                        f"{t1}(s={sn1},d={dn1}) {t2}(s={sn2},d={dn2})"
                    )

                # tv     <=> ls_pair AND p1 AND p2 AND flip_col
                instance.opt.AddBoolAnd([ls_pair, p1, p2, *flip_col]
                                        ).OnlyEnforceIf(tv)
                instance.opt.AddBoolOr([ls_pair.Not(), p1.Not(), p2.Not(),
                                        flip_col[0].Not(), flip_col[1].Not()]
                                       ).OnlyEnforceIf(tv.Not())
                # tv_rr  <=> ls_pair AND p1 AND p2 AND flip_col_rr
                instance.opt.AddBoolAnd([ls_pair, p1, p2, *flip_col_rr]
                                       ).OnlyEnforceIf(tv_rr)
                instance.opt.AddBoolOr([ls_pair.Not(), p1.Not(), p2.Not(),
                                        flip_col_rr[0].Not(), flip_col_rr[1].Not()]
                                       ).OnlyEnforceIf(tv_rr.Not())
                tmp_col.append(tv)
                tmp_col_rr.append(tv_rr)

            # Column-share <=> (some pair active) OR (no transistor at slot)
            if tmp_col:
                instance.opt.AddBoolOr(tmp_col + [has_tran.Not()]
                                       ).OnlyEnforceIf(share_col)
                instance.opt.Add(sum(tmp_col) == 0).OnlyEnforceIf(
                    [share_col.Not(), has_tran])
            else:
                instance.opt.AddImplication(has_tran.Not(), share_col)
                instance.opt.AddImplication(has_tran, share_col.Not())

            if tmp_col_rr:
                instance.opt.AddBoolOr(tmp_col_rr + [has_tran.Not()]
                                       ).OnlyEnforceIf(share_col_rr)
                instance.opt.Add(sum(tmp_col_rr) == 0).OnlyEnforceIf(
                    [share_col_rr.Not(), has_tran])
            else:
                instance.opt.AddImplication(has_tran.Not(), share_col_rr)
                instance.opt.AddImplication(has_tran, share_col_rr.Not())


def limit_lisd_contact(instance, num_contact=1):
    """
    Per-tier cap on the number of CA-style LISD-contact vias at each S/D col.

    Same shape as `limit_gate_contact`, but iterates `sd_ci` (S/D col indices,
    odd parity) and reads `lisd_share_at_col_vars[zi][col]`. The "shared => one
    contact across PMOS+NMOS, else one per side" duality is identical.

    Notes:
      - No hardcoded "PC". Iterates `q_tech.placement_layer_names`, uses each
        tier's own `col_in_layer` / `row_in_layer`.
      - Uses the local `_gather_contact_via_vars` helper, scoped to vias
        adjacent to the chosen placement tier.
      - Reads from per-tier `lisd_share_at_col_vars[zi][col]` (nested).
    """
    instance.opt.log_comment(f"Per-tier limit lisd contact to {num_contact} ...")
    for layer_name in sorted(instance.q_tech.placement_layer_names):
        zi = instance.lgg.layer_to_idx[layer_name]
        per_tier_ls = instance.lisd_share_at_col_vars.get(zi, {})
        if not per_tier_ls:
            continue
        pmos_rows = {instance.lgg.row_in_layer(layer_name, ri)
                     for ri in instance.pmos_pin_access_ri}
        nmos_rows = {instance.lgg.row_in_layer(layer_name, ri)
                     for ri in instance.nmos_pin_access_ri}

        for ci in instance.sd_ci:
            col = instance.lgg.col_in_layer(layer_name, ci)
            if col not in per_tier_ls:
                continue
            lisd_share_col_var = per_tier_ls[col]
            pmos_vias = _gather_contact_via_vars(instance, zi, col, pmos_rows)
            nmos_vias = _gather_contact_via_vars(instance, zi, col, nmos_rows)
            instance.opt.Add(
                sum(pmos_vias + nmos_vias) <= num_contact
            ).OnlyEnforceIf(lisd_share_col_var)
            instance.opt.Add(sum(pmos_vias) <= num_contact).OnlyEnforceIf(lisd_share_col_var.Not())
            instance.opt.Add(sum(nmos_vias) <= num_contact).OnlyEnforceIf(lisd_share_col_var.Not())


def ban_middle_row_via_for_3T(finfet):
    """
    [3-Track SH only] Restrict PC-to-M0 via usage at the middle row based
    on lisd_routing and lig_routing config flags.

    When lisd_routing is OFF:
        On S/D columns, ban all PC<->M0 edges/arcs/flows at the middle row
        unless the column has LISD sharing.

    When lig_routing is OFF:
        On gate columns, ban all PC<->M0 edges/arcs/flows at the middle row
        unless the column has gate sharing.

    Args:
        finfet: The FinFET instance
    """
    if not (finfet.tech.height_config == "SH" and finfet.tech.num_rt_track == 3):
        return  # only applies to 3-track SH

    middle_row = finfet.lgg.row_in_layer("PC", 1)
    pc_idx = finfet.lgg.layer_to_idx["PC"]
    m0_idx = finfet.lgg.layer_to_idx["M0"]

    # Single placement tier's zi - resolved the same way sibling per-tier
    # helpers do (zi = lgg.layer_to_idx of the one placement layer). The
    # module writes the sharing vars NESTED per tier ([zi][col]), so reads
    # here must index by this zi, not flat by col.
    placement_layer_name = next(iter(finfet.q_tech.placement_layer_names))
    zi = finfet.lgg.layer_to_idx[placement_layer_name]
    per_tier_lisd = finfet.lisd_share_at_col_vars.get(zi, {})
    per_tier_gate = finfet.gate_share_at_col_vars.get(zi, {})

    LISD_ROUTING = finfet.cell_config["lisd_routing"]["value"]
    LIG_ROUTING = finfet.cell_config["lig_routing"]["value"]

    finfet.opt.log_comment("3T middle-row via restriction (LISD / LIG routing)")
    logger.info("\t==\t[3T] Enforcing middle-row via restrictions ...")

    # --- Collect PC<->M0 edges at the middle row, keyed by column ---
    middle_via_edges_by_col = {}  # col -> list of (u, v) edge keys
    for u, v in finfet.lgg.edges():
        # PC->M0 via at middle row
        if u[0] == pc_idx and v[0] == m0_idx and u[1] == middle_row and v[1] == middle_row:
            middle_via_edges_by_col.setdefault(u[2], []).append((u, v))

    # --- S/D columns: ban via unless LISD shared ---
    if not LISD_ROUTING:
        logger.info("\t==\t[3T] LISD routing OFF → banning middle-row PC↔M0 via on S/D cols unless LISD shared")
        for col, edge_keys in middle_via_edges_by_col.items():
            if not finfet.lgg.is_odd_col("PC", col):
                continue  # not an S/D column
            if col not in per_tier_lisd:
                # no sharing variable exists -> unconditionally ban
                for ek in edge_keys:
                    finfet.opt.Add(finfet.edge_vars[ek] == 0)
                continue
            lisd_var = per_tier_lisd[col]
            for ek in edge_keys:
                # ban edge when LISD is NOT shared
                finfet.opt.Add(finfet.edge_vars[ek] == 0).OnlyEnforceIf(lisd_var.Not())

    # --- Gate columns: ban via unless gate shared ---
    if not LIG_ROUTING:
        logger.info("\t==\t[3T] LIG routing OFF → banning middle-row PC↔M0 via on gate cols unless gate shared")
        for col, edge_keys in middle_via_edges_by_col.items():
            if not finfet.lgg.is_even_col("PC", col):
                continue  # not a gate column
            if col not in per_tier_gate:
                # no sharing variable exists -> unconditionally ban
                for ek in edge_keys:
                    finfet.opt.Add(finfet.edge_vars[ek] == 0)
                continue
            gs_var = per_tier_gate[col]
            for ek in edge_keys:
                # ban edge when gate is NOT shared
                finfet.opt.Add(finfet.edge_vars[ek] == 0).OnlyEnforceIf(gs_var.Not())


def link_flow_to_arc(instance):
    """
    Biconditional link: per-net flow on (u,v) <=> the arc is active.

    Forward (`flow -> arc`):
      - Boolean-flow nets: `AddImplication(flow_k -> arc)` per k.
      - Integer-flow nets (in `instance._int_flow_nets`): big-M `flow <= K*arc`.

    Reverse (`arc -> exists flow`):
      - Boolean-flow nets: `arc -> OR(flow_k for k)`. Without this, the solver
        can leave arc=true with all flow_k=0 - producing dangling wires
        (every edge with edge_var=true requires arc=true via link_arc_to_edge,
        but arc=true was free to enable phantom routes). Tying arc to actual
        flow usage kills those phantoms.
      - Integer-flow nets: `arc -> flow >= 1`.

    Note: with active MAR/EOL DRC rules the reverse link must stay OFF because
    those rules can force arc activation WITHOUT flow (metal-extension
    side-effect). QFET currently runs WITHOUT MAR/EOL so the reverse link is
    safe AND necessary to prevent dangling routes.

    Layer-agnostic - no per-tier dispatch needed.
    """
    instance.opt.log_comment("Linking flow ⇔ arc usage ...")
    enable_reverse = getattr(instance, "enable_reverse_flow_link", True)
    int_flow_nets = getattr(instance, "_int_flow_nets", {})
    for net in instance.circuit.get_nets(with_power_ground=False):
        if net.name in int_flow_nets:
            K = int_flow_nets[net.name]
            for u, v in instance.lgg.arcs():
                flow_var = instance.net_flow_vars[(net.name, 0, u, v)]
                arc_var  = instance.net_arc_vars[(net.name, u, v)]
                # forward: flow <= K * arc
                instance.opt.Add(flow_var <= K * arc_var)
                # reverse: arc=1 -> flow >= 1
                if enable_reverse:
                    instance.opt.Add(flow_var >= arc_var)
        else:
            for u, v in instance.lgg.arcs():
                flow_vars = [
                    instance.net_flow_vars[(net.name, k, u, v)]
                    for k in range(instance.net_to_flow_cnt[net.name])
                ]
                arc_var = instance.net_arc_vars[(net.name, u, v)]
                # forward: each flow_k implies arc
                for fv in flow_vars:
                    instance.opt.AddImplication(fv, arc_var)
                # reverse: arc=1 -> OR(flow_k)=1
                if enable_reverse and flow_vars:
                    instance.opt.AddBoolOr(flow_vars).OnlyEnforceIf(arc_var)


def link_arc_to_edge(instance):
    """
    Couple per-net arcs to the undirected `edge_vars`:
      1) Per edge: at most one arc-direction across all nets (no two nets share).
      2) edge_var = OR of arc_var(u,v) + arc_var(v,u) across nets.
      3) Per terminal-k flow: a flow can't use both directions of the same edge
         (skipped when tree enforcement is active OR for integer-flow IntVars).

    Layer-agnostic - operates on `lgg.edges()` / `lgg.arcs()` directly.
    """
    for u, v in instance.lgg.edges():
        # Per-edge: at most one arc-direction across all nets.
        instance.opt.Add(
            sum(
                instance.net_arc_vars[(net.name, u, v)]
                + instance.net_arc_vars[(net.name, v, u)]
                for net in instance.circuit.get_nets(with_power_ground=False)
                if (net.name, u, v) in instance.net_arc_vars
                and (net.name, v, u) in instance.net_arc_vars
            )
            <= 1
        )
        # edge <=> OR of arc(u,v) + arc(v,u) across nets.
        conditions = []
        for net in instance.circuit.get_nets(with_power_ground=False):
            if (net.name, u, v) in instance.net_arc_vars \
                    and (net.name, v, u) in instance.net_arc_vars:
                conditions.append(instance.net_arc_vars[(net.name, u, v)])
                conditions.append(instance.net_arc_vars[(net.name, v, u)])
        instance.opt.AddBoolOr(conditions).OnlyEnforceIf(instance.edge_vars[(u, v)])
        instance.opt.Add(sum(conditions) == 0).OnlyEnforceIf(
            instance.edge_vars[(u, v)].Not())

    # Per-terminal flow: skip both-directions of the same edge. Tree enforcement
    # subsumes this. Integer-flow IntVars use arc-level edge exclusivity above.
    int_flow_nets = getattr(instance, "_int_flow_nets", {})
    if not getattr(instance, "_tree_enforcement_active", False):
        for net in instance.circuit.get_nets(with_power_ground=False):
            if net.name in int_flow_nets:
                continue
            for k in range(instance.net_to_flow_cnt[net.name]):
                for u, v in instance.lgg.edges():
                    instance.opt.Add(
                        instance.net_flow_vars[(net.name, k, u, v)]
                        + instance.net_flow_vars[(net.name, k, v, u)] <= 1
                    )


def prohibit_virtual_edge_shorting(instance):
    """At every (row, col) where a virtual jump (VL) lands, allow at most
    one cross-layer edge to be active - across the VL itself and every
    regular via-style edge whose two endpoints share that same (row, col).

    Why: a VL like BPC1<->PC1 sits on top of the MIV chain (BPC1<->H0,
    H0<->H1, H1<->PC1) at the same column. With both available, the solver
    can pick the VL shortcut AND the MIV stack for the same net, double-
    counting via cost and emitting overlapping geometry that DRC will flag.
    Capping the total at 1 forces the solver to choose one vertical path
    per (row, col).

    No-op when the LGG has no virtual edges. Generalizes to any virtual
    pair the layer JSON declares (BPC1<->PC1, H0<->PC1, etc.) and to
    arbitrary stack depths between them.
    """
    lgg = instance.lgg
    virt_by_col = getattr(lgg, "_virtual_edges_along_col", None)
    if not virt_by_col:
        return

    # Coords (row, col) at which any virtual edge lands. Both endpoints share
    # the same (r, c) for "overlap" / "colwise" methods.
    virt_coords: set[tuple[float, float]] = set()
    for c, vlist in virt_by_col.items():
        for u, v in vlist:
            if (u[1], u[2]) == (v[1], v[2]):
                virt_coords.add((u[1], u[2]))

    if not virt_coords:
        return

    # Bucket every cross-layer edge that lives at one of those (r, c)'s.
    # An edge counts if BOTH endpoints sit at the same (r, c) (so it's a
    # vertical via at that column, not a diagonal layer jump).
    buckets: dict[tuple[float, float], list] = {rc: [] for rc in virt_coords}
    for u, v in lgg.edges():
        if u[0] == v[0]:
            continue  # same-layer wire, not a via
        if (u[1], u[2]) != (v[1], v[2]):
            continue  # diagonal cross-layer edge - not a stacked via
        rc = (u[1], u[2])
        if rc not in buckets:
            continue
        # Explicit None checks - `or` on a BoolVar raises NotImplementedError
        # ("Evaluating a Literal as a Boolean value is not supported").
        ev = instance.edge_vars.get((u, v))
        if ev is None:
            ev = instance.edge_vars.get((v, u))
        if ev is not None:
            buckets[rc].append(ev)

    instance.opt.log_comment(
        "Virtual-edge shorting prevention (at most 1 via per virtual-touched (r, c))"
    )
    n_added = 0
    for rc, edges in buckets.items():
        if len(edges) >= 2:
            instance.opt.AddAtMostOne(edges)
            n_added += 1
    logger.info(
        f"\t==\tAdded AtMostOne(via) at {n_added} virtual-touched (r, c) "
        f"coord(s) [out of {len(virt_coords)} total]."
    )


def net_has_one_src_and_k_terminals(instance):
    """
    Per net: exactly one source node and exactly one k-th terminal node.

    Reads `node_is_src_vars[net.name]` and `node_is_term_vars[net.name][k]`.
    Layer-agnostic.
    """
    instance.opt.log_comment("Enforcing net unique source / k-th terminal ...")
    for net in instance.circuit.get_nets(with_power_ground=False):
        src_candidates = list(instance.node_is_src_vars[net.name].values())
        if src_candidates:
            instance.opt.Add(sum(src_candidates) == 1)
        else:
            logger.error(f"Net {net.name} has no potential source locations defined.")

        for k in range(net.num_terminals()):
            term_candidates = list(instance.node_is_term_vars[net.name][k].values())
            if term_candidates:
                instance.opt.Add(sum(term_candidates) == 1)
            else:
                logger.error(
                    f"Net {net.name}, terminal {k} has no potential locations defined."
                )


def _ignored_middle_rows(instance) -> set:
    """
    Per-tier middle-row coords that should be skipped by node uniqueness.

    A "middle row" is a row index that appears in BOTH the NMOS and PMOS
    pin-access row sets on the same placement tier - the 3-track-SH "shared"
    routing row between the two devices. QFET 4-track SH has disjoint sets =>
    this set is empty; 3-track configurations naturally produce one middle
    row per tier.

    Derived from the pin-access row sets (works on any placement-tier layout)
    rather than a hardcoded `lgg.row_in_layer("PC", 1)` check.
    """
    shared_ri = set(instance.nmos_pin_access_ri) & set(instance.pmos_pin_access_ri)
    if not shared_ri:
        return set()
    out = set()
    for layer_name in instance.q_tech.placement_layer_names:
        for ri in shared_ri:
            out.add(instance.lgg.row_in_layer(layer_name, ri))
    return out


def net_src_node_uniqueness(instance):
    """
    Per node: at most one net can claim this node as its source.

    Iterates every LGG node; for each, gather every `node_is_src_vars[net][node]`
    that exists and bound their sum to <= 1.

    No hardcoded `"PC"` row lookup: middle-row skips (if any) are derived per
    placement tier from `_ignored_middle_rows(instance)`.
    """
    instance.opt.log_comment("Enforcing per-node source uniqueness ...")
    skip_rows = _ignored_middle_rows(instance)
    for node in instance.lgg.nodes():
        if node[1] in skip_rows:
            continue
        tmp = [
            instance.node_is_src_vars[net.name][node]
            for net in instance.circuit.get_nets(with_power_ground=False)
            if node in instance.node_is_src_vars[net.name]
        ]
        if tmp:
            instance.opt.Add(sum(tmp) <= 1)


def net_term_node_uniqueness(instance):
    """
    Per node: at most one (net, terminal-k) pair can claim this node.

    For each node + net, reifies a `<net>_isterm_placed_at_<node>` BoolVar that
    is True iff some k-th terminal of that net lands on this node, then bounds
    the sum across nets to <= 1.

    No hardcoded `"PC"` row lookup: middle-row skips (if any) derived per
    placement tier via `_ignored_middle_rows(instance)`.
    """
    instance.opt.log_comment("Enforcing per-node terminal uniqueness ...")
    skip_rows = _ignored_middle_rows(instance)
    for node in instance.lgg.nodes():
        if node[1] in skip_rows:
            continue
        tmp_node_is_term_vars = []
        for net in instance.circuit.get_nets(with_power_ground=False):
            tmp_var = instance.opt.NewBoolVar(f"{net.name}_isterm_placed_at_{node}")
            tmp_node_is_term_vars.append(tmp_var)
            for k in range(net.num_terminals()):
                if node in instance.node_is_term_vars[net.name][k]:
                    instance.opt.AddImplication(
                        instance.node_is_term_vars[net.name][k][node], tmp_var
                    )
        if tmp_node_is_term_vars:
            instance.opt.Add(sum(tmp_node_is_term_vars) <= 1)


def net_SON_node_uniqueness(instance):
    """
    Per SON node: at most one (io-net, terminal-k) pair can claim it.

    Iterates every pin-access layer in `son_terminal_nodes` and every SON
    candidate node on that layer. Works for VERTICAL pin layers (M1) and
    HORIZONTAL pin layers (QFET BM0 / M0) - orientation-agnostic, since
    "one net per node" is a node-local constraint that doesn't depend on
    whether pins run along col or row.

    Notes:
      - No hardcoded `son_terminal_nodes["M1"]`. Iterates every entry in
        `son_terminal_nodes` (one per layer in `q_tech.pin_access_layer_names`).
      - Defensive `if node in node_is_SON_vars[net][k]` guard - QFET SON
        candidates can be col-filtered per `_collect_son_nodes_for_layer`, so
        not every (net, k) need have a var at every node.
    """
    instance.opt.log_comment("Enforcing per-SON-node uniqueness across IO nets ...")
    for layer_name, nodes in instance.son_terminal_nodes.items():
        for node in nodes:
            tmp = []
            for net in instance.circuit.get_nets(with_power_ground=False):
                if not net.is_io_net():
                    continue
                for k in range(net.num_terminals(), instance.net_to_flow_cnt[net.name]):
                    son_map = instance.node_is_SON_vars.get(net.name, {}).get(k, {})
                    if node in son_map:
                        tmp.append(son_map[node])
            if tmp:
                instance.opt.Add(sum(tmp) <= 1)


def prohibit_multiple_SONs_same_column(instance):
    """
    Per pin track on every pin-access layer: at most one SON can land on it.

    A "pin track" depends on layer direction:
      - VERTICAL pin layer (e.g. M1) - each col is one pin track. Group SONs
        by `node[2]` (col).
      - HORIZONTAL pin layer (e.g. QFET M0 / BM0) - each row is one pin track.
        Group SONs by `node[1]` (row).

    The function name stays for backward compatibility; the semantic is "one
    SON per physical pin track", with the col / row axis chosen by orientation.

    Notes:
      - No hardcoded `cols_in_layer("M1")` / `son_terminal_nodes["M1"]`. Iterates
        every pin-access layer in `son_terminal_nodes`.
      - Direction dispatch via `lgg.layer_to_direction[layer_name]` ("V" / "H").
    """
    instance.opt.log_comment("Enforcing per-pin-track SON uniqueness ...")
    nets = [
        net for net in instance.circuit.get_nets(with_power_ground=False)
        if net.is_io_net()
    ]
    for layer_name, nodes in instance.son_terminal_nodes.items():
        direction = instance.lgg.layer_to_direction.get(layer_name, "V")
        # Vertical pin -> group by col (node[2]); horizontal pin -> group by row (node[1]).
        track_axis = 2 if direction == "V" else 1
        by_track = {}
        for node in nodes:
            track = node[track_axis]
            by_track.setdefault(track, []).append(node)

        for track, track_nodes in by_track.items():
            tmp = []
            for net in nets:
                for k in range(net.num_terminals(), instance.net_to_flow_cnt[net.name]):
                    son_map = instance.node_is_SON_vars.get(net.name, {}).get(k, {})
                    for node in track_nodes:
                        if node in son_map:
                            tmp.append(son_map[node])
            if tmp:
                instance.opt.Add(sum(tmp) <= 1)


def _gather_ds_shareable_vars(instance, net_name, t1, t2, pin1, pin2):
    """ds (drain/source) pair vars shareable between t1 and t2 for `net_name`.

    Each cross-MOS pair carries up to four ds vars (left/right x t1-t2 / t2-t1).
    Returns any of them keyed on the pair-name pattern from
    `placement.pairwise_diffusion_sharing`. Empty when either pin is a gate.
    Layer-agnostic - reads from `instance.ds_pair_vars` directly.
    """
    if pin1 == "gate" or pin2 == "gate":
        return []
    keys = (
        f"ds_left_{t1}_{t2}_{net_name}",  f"ds_left_{t2}_{t1}_{net_name}",
        f"ds_right_{t1}_{t2}_{net_name}", f"ds_right_{t2}_{t1}_{net_name}",
    )
    return [instance.ds_pair_vars[k] for k in keys if k in instance.ds_pair_vars]


def _gather_lisd_shareable_vars(instance, net_name, t1, t2, pin1, pin2):
    """LISD-share pair vars for the (t1, t2, net) pair. Empty on gate pins."""
    if pin1 == "gate" or pin2 == "gate":
        return []
    keys = (
        f"lisd_share_{t1}_{t2}_{net_name}",
        f"lisd_share_{t2}_{t1}_{net_name}",
    )
    return [instance.lisd_share_pair_vars[k]
            for k in keys if k in instance.lisd_share_pair_vars]


def _gather_gate_shareable_vars(instance, net_name, t1, t2, pin1=None, pin2=None,
                                check_pin=True):
    """Gate-share pair vars for the (t1, t2, net) pair. Empty unless both pins are 'gate'."""
    if check_pin and not (pin1 == "gate" and pin2 == "gate"):
        return []
    keys = (
        f"gate_share_{t1}_{t2}_{net_name}",
        f"gate_share_{t2}_{t1}_{net_name}",
    )
    return [instance.gate_share_pair_vars[k]
            for k in keys if k in instance.gate_share_pair_vars]


def _induce_int_flow_conservation(instance, net):
    """
    Single-commodity integer-flow conservation for a net.

    Tree-enforcement collapses K boolean flow commodities into one IntVar(0,K)
    per arc (saving (K-1)*|arcs| BoolVars). Per-terminal "shared" reifiers come
    from the same diffusion / LISD / gate share pools used by the boolean path.

    Layer-agnostic - operates on lgg.nodes() / adj_in / adj_out.
    """
    total_k = instance._int_flow_nets[net.name]
    src_tran_name, src_pin = net.source()

    # 1) Per-terminal is_shared reifiers.
    is_shared_vars = []
    for k, (term_tran_name, term_pin) in enumerate(net.terminals()):
        k_shareable_vars = []
        k_shareable_vars.extend(_gather_ds_shareable_vars(
            instance, net.name, src_tran_name, term_tran_name, src_pin, term_pin))
        k_shareable_vars.extend(_gather_lisd_shareable_vars(
            instance, net.name, src_tran_name, term_tran_name, src_pin, term_pin))
        k_shareable_vars.extend(_gather_gate_shareable_vars(
            instance, net.name, src_tran_name, term_tran_name, src_pin, term_pin))
        for k_prev in range(k):
            prev_tran, prev_pin = net.terminals()[k_prev]
            k_shareable_vars.extend(_gather_ds_shareable_vars(
                instance, net.name, prev_tran, term_tran_name, prev_pin, term_pin))
            k_shareable_vars.extend(_gather_lisd_shareable_vars(
                instance, net.name, prev_tran, term_tran_name, prev_pin, term_pin))
            k_shareable_vars.extend(_gather_gate_shareable_vars(
                instance, net.name, prev_tran, term_tran_name, prev_pin, term_pin))
        is_shared = instance.opt.NewBoolVar(f"shared_{net.name}_{k}")
        if k_shareable_vars:
            instance.opt.AddBoolOr(k_shareable_vars).OnlyEnforceIf(is_shared)
            instance.opt.Add(sum(k_shareable_vars) == 0).OnlyEnforceIf(is_shared.Not())
        else:
            instance.opt.Add(is_shared == 0)
        is_shared_vars.append(is_shared)

    active_K_expr = total_k - sum(is_shared_vars)

    # 2) Pre-compute terminal candidates per node.
    term_at_node = {}
    for k in range(len(net.terminals())):
        for node, var in instance.node_is_term_vars[net.name][k].items():
            term_at_node.setdefault(node, []).append((k, var, is_shared_vars[k]))

    # 3) Flow conservation at each node.
    for node in instance.lgg.nodes():
        in_flows = sum(
            instance.net_flow_vars[(net.name, 0, u, v)]
            for u, v in instance.adj_in.get(node, [])
            if (net.name, 0, u, v) in instance.net_flow_vars
        )
        out_flows = sum(
            instance.net_flow_vars[(net.name, 0, u, v)]
            for u, v in instance.adj_out.get(node, [])
            if (net.name, 0, u, v) in instance.net_flow_vars
        )

        can_be_src = instance.node_is_src_vars[net.name].get(node)
        terms_here = term_at_node.get(node, [])

        has_demand = bool(terms_here)
        if not terms_here:
            demand = 0
        elif len(terms_here) == 1:
            _, can_be_kth, is_sh = terms_here[0]
            demand = instance.opt.NewBoolVar(
                f"dem_{net.name}_{node[0]}_{node[1]}_{node[2]}")
            instance.opt.AddBoolAnd([can_be_kth, is_sh.Not()]).OnlyEnforceIf(demand)
            instance.opt.AddBoolOr([can_be_kth.Not(), is_sh]).OnlyEnforceIf(demand.Not())
        else:
            absorb_vars = []
            for k_idx, can_be_kth, is_sh in terms_here:
                ab = instance.opt.NewBoolVar(
                    f"dem_{net.name}_{k_idx}_{node[0]}_{node[1]}_{node[2]}")
                instance.opt.AddBoolAnd([can_be_kth, is_sh.Not()]).OnlyEnforceIf(ab)
                instance.opt.AddBoolOr([can_be_kth.Not(), is_sh]).OnlyEnforceIf(ab.Not())
                absorb_vars.append(ab)
            demand = sum(absorb_vars)

        if can_be_src is not None:
            instance.opt.Add(out_flows - in_flows == active_K_expr - demand
                             ).OnlyEnforceIf(can_be_src)
            if has_demand:
                instance.opt.Add(in_flows - out_flows == demand
                                 ).OnlyEnforceIf(can_be_src.Not())
            else:
                instance.opt.Add(in_flows == out_flows
                                 ).OnlyEnforceIf(can_be_src.Not())
        elif has_demand:
            instance.opt.Add(in_flows - out_flows == demand)
        else:
            instance.opt.Add(in_flows == out_flows)

        all_shared = instance.opt.NewBoolVar(
            f"allsh_{net.name}_{node[0]}_{node[1]}_{node[2]}")
        instance.opt.AddBoolAnd(is_shared_vars).OnlyEnforceIf(all_shared)
        instance.opt.AddBoolOr([v.Not() for v in is_shared_vars]
                               ).OnlyEnforceIf(all_shared.Not())
        instance.opt.Add(in_flows == 0).OnlyEnforceIf(all_shared)
        instance.opt.Add(out_flows == 0).OnlyEnforceIf(all_shared)


def induce_internal_routing_flow_with_diffusion(instance):
    """
    Per-terminal directed flow conservation with diffusion / LISD / gate
    sharing as "free routes" (terminal absorbed without a flow path).

    For every (net, terminal-k):
      - Reify `is_shared` = OR of every diffusion / LISD / gate share var that
        connects k to the source or any previous terminal.
      - On every LGG node, build per-(net,k) in/out flow sums.
      - Source node: out - in = 1 when source AND NOT shared.
      - Terminal node: in - out = 1 when terminal AND NOT shared.
      - Intermediate: in == out when NOT source AND NOT terminal AND NOT shared.
      - When `is_shared` is true, force in = out = 0 (no flow needed).
      - Capacity (in/out <= 1) skipped under tree enforcement.

    Integer-flow nets (in `_int_flow_nets`) use `_induce_int_flow_conservation`
    which collapses K commodities into a single IntVar(0,K) per arc.

    Layer-agnostic - operates on `lgg.nodes()` / `adj_in` / `adj_out`. Reads
    `ds_pair_vars`, `lisd_share_pair_vars`, `gate_share_pair_vars` populated
    by the per-tier placement helpers.
    """
    instance.opt.log_comment(
        "Per-terminal directed flow-conservation with diffusion sharing ..."
    )
    logger.info("\t==\tPer-terminal directed flow-conservation ...")
    int_flow_nets = getattr(instance, "_int_flow_nets", {})
    tree_active = getattr(instance, "_tree_enforcement_active", False)

    for net in instance.circuit.get_nets(with_power_ground=False):
        if net.name in int_flow_nets:
            _induce_int_flow_conservation(instance, net)
            continue

        src_tran_name, src_pin = net.source()
        for k, (term_tran_name, term_pin) in enumerate(net.terminals()):
            # Gather every share var that connects k to source / previous terminals.
            k_shareable = []
            k_shareable.extend(_gather_ds_shareable_vars(
                instance, net.name, src_tran_name, term_tran_name, src_pin, term_pin))
            k_shareable.extend(_gather_lisd_shareable_vars(
                instance, net.name, src_tran_name, term_tran_name, src_pin, term_pin))
            k_shareable.extend(_gather_gate_shareable_vars(
                instance, net.name, src_tran_name, term_tran_name, src_pin, term_pin))
            for k_prev in range(k):
                prev_tran, prev_pin = net.terminals()[k_prev]
                k_shareable.extend(_gather_ds_shareable_vars(
                    instance, net.name, prev_tran, term_tran_name, prev_pin, term_pin))
                k_shareable.extend(_gather_lisd_shareable_vars(
                    instance, net.name, prev_tran, term_tran_name, prev_pin, term_pin))
                k_shareable.extend(_gather_gate_shareable_vars(
                    instance, net.name, prev_tran, term_tran_name, prev_pin, term_pin))

            is_shared = instance.opt.NewBoolVar(f"shared_{net.name}_{k}")
            if k_shareable:
                instance.opt.AddBoolOr(k_shareable).OnlyEnforceIf(is_shared)
                instance.opt.Add(sum(k_shareable) == 0).OnlyEnforceIf(is_shared.Not())
            else:
                instance.opt.Add(is_shared == 0)

            for node in instance.lgg.nodes():
                in_flows = sum(
                    instance.net_flow_vars[(net.name, k, u, v)]
                    for u, v in instance.adj_in.get(node, [])
                    if (net.name, k, u, v) in instance.net_flow_vars
                )
                out_flows = sum(
                    instance.net_flow_vars[(net.name, k, u, v)]
                    for u, v in instance.adj_out.get(node, [])
                    if (net.name, k, u, v) in instance.net_flow_vars
                )

                can_be_src = instance.node_is_src_vars[net.name].get(node)
                if can_be_src is not None:
                    instance.opt.Add(out_flows - in_flows == 1).OnlyEnforceIf(
                        [can_be_src, is_shared.Not()])

                can_be_kth = instance.node_is_term_vars[net.name][k].get(node)
                if can_be_kth is not None:
                    instance.opt.Add(in_flows - out_flows == 1).OnlyEnforceIf(
                        [can_be_kth, is_shared.Not()])

                intermediate = []
                if can_be_src is not None:
                    intermediate.append(can_be_src.Not())
                if can_be_kth is not None:
                    intermediate.append(can_be_kth.Not())
                intermediate.append(is_shared.Not())
                instance.opt.Add(in_flows == out_flows).OnlyEnforceIf(intermediate)

                if not tree_active:
                    instance.opt.Add(in_flows  <= 1).OnlyEnforceIf(is_shared.Not())
                    instance.opt.Add(out_flows <= 1).OnlyEnforceIf(is_shared.Not())
                instance.opt.Add(in_flows  == 0).OnlyEnforceIf(is_shared)
                instance.opt.Add(out_flows == 0).OnlyEnforceIf(is_shared)


def induce_external_routing_flow(instance):
    """
    Per-IO-net flow conservation routing each k-th SON terminal (k beyond the
    internal terminals) to its source.

    Layer-agnostic - uses `node_is_SON_vars[net][k][node]`, which QFET's
    `_init_SON_vars` populates over every pin-access layer (horizontal pins
    BM0 / M0 in QFET, vertical M1 in earlier techs).
    """
    instance.opt.log_comment("Per-IO-net flow conservation to SON terminals ...")
    logger.info("\t==\tPer-IO-net flow conservation to SON terminals ...")
    for net in instance.circuit.get_nets(with_power_ground=False):
        if not net.is_io_net():
            continue
        logger.info(
            f"\t\tIO net {net.name}: {net.num_terminals()} internal terminals, "
            f"{instance.net_to_flow_cnt[net.name]} total flow slots"
        )
        for k in range(net.num_terminals(), instance.net_to_flow_cnt[net.name]):
            for node in instance.lgg.nodes():
                in_flows = sum(
                    instance.net_flow_vars[(net.name, k, u, v)]
                    for u, v in instance.adj_in.get(node, [])
                    if (net.name, k, u, v) in instance.net_flow_vars
                )
                out_flows = sum(
                    instance.net_flow_vars[(net.name, k, u, v)]
                    for u, v in instance.adj_out.get(node, [])
                    if (net.name, k, u, v) in instance.net_flow_vars
                )

                can_be_src = instance.node_is_src_vars[net.name].get(node)
                if can_be_src is not None:
                    instance.opt.Add(out_flows - in_flows == 1
                                     ).OnlyEnforceIf(can_be_src)

                can_be_kth_SON = instance.node_is_SON_vars.get(net.name, {}) \
                    .get(k, {}).get(node)
                if can_be_kth_SON is not None:
                    instance.opt.Add(in_flows - out_flows == 1
                                     ).OnlyEnforceIf(can_be_kth_SON)

                intermediate = []
                if can_be_src is not None:
                    intermediate.append(can_be_src.Not())
                if can_be_kth_SON is not None:
                    intermediate.append(can_be_kth_SON.Not())
                if intermediate:
                    instance.opt.Add(in_flows == out_flows
                                     ).OnlyEnforceIf(intermediate)
                else:
                    instance.opt.Add(in_flows == out_flows)
                instance.opt.Add(in_flows  <= 1)
                instance.opt.Add(out_flows <= 1)


def tree_enforcement(finfet):
    """
    Enforce tree structure on per-net arc usage: each non-source node
    may have at most one incoming arc active per net.  This is the
    KComm+Tree constraint (<=1 in-arc per non-source node).

    Args:
        finfet: The FinFET instance
    """
    logger.info("\t==\tAdding tree enforcement constraints...")
    finfet.opt.log_comment("Tree enforcement: at most one incoming arc per non-source node per net")
    num_constraints = 0
    for net in finfet.circuit.get_nets(with_power_ground=False):
        for node in finfet.lgg.nodes():
            in_arcs = [
                finfet.net_arc_vars[(net.name, u_arc, v_arc)]
                for u_arc, v_arc in finfet.adj_in.get(node, [])
                if (net.name, u_arc, v_arc) in finfet.net_arc_vars
            ]
            if len(in_arcs) <= 1:
                continue  # trivially satisfied
            src_var = finfet.node_is_src_vars[net.name].get(node)
            if src_var is not None:
                # node could be the source - only enforce when it is NOT the source
                finfet.opt.Add(sum(in_arcs) <= 1).OnlyEnforceIf(src_var.Not())
            else:
                # node can never be the source for this net - always enforce
                finfet.opt.Add(sum(in_arcs) <= 1)
            num_constraints += 1
    logger.info(f"\t==\tTree enforcement: {num_constraints} constraints added")


def node_exclusivity(instance):
    """
    Per LGG node: at most one net may "touch" it via arc usage.

    For each (net, node) pair, reifies `net_touches_node_var` as an OR over
    incident `net_arc_vars` (incoming + outgoing arcs at that node). Sum of
    per-net indicators bounded <= 1 per node.

    Layer-agnostic - operates on `lgg.nodes()` / `adj_in` / `adj_out`.
    """
    logger.info("\t==\tAdding node exclusivity constraints ...")
    instance.opt.log_comment("Enforcing per-node net exclusivity ...")
    for node in instance.lgg.nodes():
        net_touches_indicators = []
        for net in instance.circuit.get_nets(with_power_ground=False):
            touches = instance.opt.NewBoolVar(
                f"net_{net.name}_touches_node_L{node[0]}R{node[1]}C{node[2]}"
            )
            net_touches_indicators.append(touches)

            incident = []
            for u, _ in instance.adj_in.get(node, []):
                key = (net.name, u, node)
                if key in instance.net_arc_vars:
                    incident.append(instance.net_arc_vars[key])
            for _, v in instance.adj_out.get(node, []):
                key = (net.name, node, v)
                if key in instance.net_arc_vars:
                    incident.append(instance.net_arc_vars[key])

            if incident:
                instance.opt.AddBoolOr(incident).OnlyEnforceIf(touches)
                instance.opt.Add(sum(incident) == 0).OnlyEnforceIf(touches.Not())
            else:
                instance.opt.Add(touches == 0)

        if net_touches_indicators:
            instance.opt.Add(sum(net_touches_indicators) <= 1)


def _build_layer_to_tier(instance):
    """
    Group every LGG layer into a placement-tier bucket.

    Tier index `ti` runs 0..N-1 where N = number of placement layers. The
    placement layers themselves are sorted by LGG layer index (BPC2/BPC1/PC1/PC2
    on QFET => ti = 0/1/2/3). Every non-placement layer is bucketed into the
    tier of its closest placement layer by LGG-index distance - so on QFET,
    backside routing/pin-access layers (BM1, BM0) ride tier 0 (BPC2) and
    frontside (M0, M1) ride tier 3 (PC2).

    Returns:
        layer_to_tier:     {lgg_layer_idx -> tier_id}
        tier_layer_names:  {tier_id -> [layer_name, ...]}
        placement_layers:  [layer_name, ...] sorted by LGG idx
    """
    lgg = instance.lgg
    placement_layers = sorted(
        instance.q_tech.placement_layer_names,
        key=lambda n: lgg.layer_to_idx[n],
    )
    placement_idxs = [lgg.layer_to_idx[n] for n in placement_layers]

    layer_to_tier = {}
    tier_layer_names = {ti: [] for ti in range(len(placement_layers))}
    for layer_name, idx in lgg.layer_to_idx.items():
        ti = min(range(len(placement_idxs)),
                 key=lambda i: abs(placement_idxs[i] - idx))
        layer_to_tier[idx] = ti
        tier_layer_names[ti].append(layer_name)
    return layer_to_tier, tier_layer_names, placement_layers


def routing_localization(instance):
    """
    Per-tier routing-localization (generic N-tier).

    Auto-derives the tier count from `q_tech.placement_layer_names`:
      - QFET (4 placement tiers BPC2/BPC1/PC1/PC2) => 4 per-tier Y windows.
      - 2 placement tiers (BPC/PC)                 => 2 per-tier Y windows.
      - FinFET-style single-tier (PC)              => 1 trivial window.

    Layout:
      X axis        - single global window per net (physical coords).
      Y axis        - one window per (net, tier). Each arc endpoint is bounded
                       by its own tier's Y window; tiers where the net has no
                       pin are unconstrained (gated by `has_pins_on_tier`).
      Wirelength LB - HPWL bound on edge count using min placement pitch.
      Tolerance     - `routing_tolerance_x` / `routing_tolerance_y` with
                       per-fanout slack.

    Output containers (all pre-allocated in QFET `_init_state_containers`):
        s_coord_x[net], s_coord_y[net], t_coord_x[net][k], t_coord_y[net][k]
        net_min/max_x[net], net_min/max_y[net]
        window_xmin/xmax_raw[net]
        net_min/max_y_tier[net][ti], window_ymin/ymax_tier[net][ti],
        has_pins_on_tier[net][ti]
    """
    lgg = instance.lgg
    tech = instance.q_tech
    layer_to_tier, tier_layer_names, placement_layers = _build_layer_to_tier(instance)
    all_tiers = list(range(len(placement_layers)))

    # --- Unified col/row domains ---
    all_layer_names = list(lgg.idx_to_layer.values())
    all_cols = sorted(set(c for ln in all_layer_names for c in lgg.cols_in_layer(ln)))
    all_placement_rows = sorted(set(
        r for ln in placement_layers for r in lgg.rows_in_layer(ln)
    ))
    all_rows = sorted(set(r for ln in all_layer_names for r in lgg.rows_in_layer(ln)))
    all_cols_domain = cp_model.Domain.FromValues(all_cols)
    all_placement_rows_domain = cp_model.Domain.FromValues(all_placement_rows)
    all_rows_domain = cp_model.Domain.FromValues(all_rows)

    # Per-tier Y ceiling (for clamping + neutral values)
    tier_max_y = {
        ti: max((max(lgg.rows_in_layer(ln), default=0) for ln in tier_layer_names[ti]),
                default=0)
        for ti in all_tiers
    }
    max_col_all = max(all_cols)
    logger.info(
        f"\t==\tRouting localization (N={len(all_tiers)} tiers): "
        f"tier_max_y={tier_max_y}, max_col_all={max_col_all}"
    )

    # --- Per-net coordinate + bounding-box variables ---
    for net in instance.circuit.get_nets(with_power_ground=False):
        # Source X / Y (global) - net pins can land anywhere across the canvas.
        instance.s_coord_x[net.name] = instance.opt.NewIntVarFromDomain(
            all_cols_domain, f"s_coord_x_{net.name}",
        )
        instance.s_coord_y[net.name] = instance.opt.NewIntVarFromDomain(
            all_placement_rows_domain, f"s_coord_y_{net.name}",
        )

        # Internal + external (SON) terminal coords - same global domain.
        instance.t_coord_x[net.name] = []
        instance.t_coord_y[net.name] = []
        for k in range(net.num_terminals()):
            instance.t_coord_x[net.name].append(instance.opt.NewIntVarFromDomain(
                all_cols_domain, f"t_coord_x_{net.name}_{k}"))
            instance.t_coord_y[net.name].append(instance.opt.NewIntVarFromDomain(
                all_rows_domain, f"t_coord_y_{net.name}_{k}"))
        for k in range(net.num_terminals(), instance.net_to_flow_cnt[net.name]):
            instance.t_coord_x[net.name].append(instance.opt.NewIntVarFromDomain(
                all_cols_domain, f"t_coord_x_{net.name}_{k}"))
            instance.t_coord_y[net.name].append(instance.opt.NewIntVarFromDomain(
                all_rows_domain, f"t_coord_y_{net.name}_{k}"))

        # Global bounding box (HPWL lower-bound source).
        instance.net_min_x[net.name] = instance.opt.NewIntVarFromDomain(
            all_cols_domain, f"net_min_x_{net.name}")
        instance.net_max_x[net.name] = instance.opt.NewIntVarFromDomain(
            all_cols_domain, f"net_max_x_{net.name}")
        instance.net_min_y[net.name] = instance.opt.NewIntVarFromDomain(
            all_rows_domain, f"net_min_y_{net.name}")
        instance.net_max_y[net.name] = instance.opt.NewIntVarFromDomain(
            all_rows_domain, f"net_max_y_{net.name}")

        # Global X window vars.
        domain_x = cp_model.Domain(0, max_col_all)
        instance.window_xmin_raw[net.name] = instance.opt.NewIntVarFromDomain(
            domain_x, f"window_xmin_raw_{net.name}")
        instance.window_xmax_raw[net.name] = instance.opt.NewIntVarFromDomain(
            domain_x, f"window_xmax_raw_{net.name}")

        # Per-tier Y variables - N entries (one per placement tier).
        instance.net_min_y_tier[net.name]   = {}
        instance.net_max_y_tier[net.name]   = {}
        instance.window_ymin_tier[net.name] = {}
        instance.window_ymax_tier[net.name] = {}
        instance.has_pins_on_tier[net.name] = {}
        for ti in all_tiers:
            my = tier_max_y[ti]
            instance.net_min_y_tier[net.name][ti]   = instance.opt.NewIntVar(
                0, my, f"net_min_y_t{ti}_{net.name}")
            instance.net_max_y_tier[net.name][ti]   = instance.opt.NewIntVar(
                0, my, f"net_max_y_t{ti}_{net.name}")
            instance.window_ymin_tier[net.name][ti] = instance.opt.NewIntVar(
                0, my, f"window_ymin_t{ti}_{net.name}")
            instance.window_ymax_tier[net.name][ti] = instance.opt.NewIntVar(
                0, my, f"window_ymax_t{ti}_{net.name}")
            instance.has_pins_on_tier[net.name][ti] = instance.opt.NewBoolVar(
                f"has_pins_t{ti}_{net.name}")
        # logger.info(f"\t{len(finfet.s_coord_x)} source coordinates created, {len(finfet.t_coord_x)} terminal coordinates created")
        # logger.info(f"\t{len(finfet.net_min_x)} net min x coordinates created, {len(finfet.net_max_x)} net max x coordinates created")
        # logger.info(f"\t{len(finfet.net_min_y)} net min y coordinates created, {len(finfet.net_max_y)} net max y coordinates created")
        # logger.info(
        #     f"\t{len(finfet.window_xmin_raw)} window x min coordinates created, {len(finfet.window_xmax_raw)} window x max coordinates created"
        # )
        # logger.info(
        #     f"\t{len(finfet.window_ymin_raw)} window y min coordinates created, {len(finfet.window_ymax_raw)} window y max coordinates created"
        # )
        # --- 1) Link node-is-{src,term,SON} bools -> coordinate sums ---
        # node = (layer_idx, row, col): node[2] = col, node[1] = row.
        instance.opt.Add(instance.s_coord_x[net.name] == sum(
            n[2] * v for n, v in instance.node_is_src_vars[net.name].items()))
        instance.opt.Add(instance.s_coord_y[net.name] == sum(
            n[1] * v for n, v in instance.node_is_src_vars[net.name].items()))
        for k in range(net.num_terminals()):
            instance.opt.Add(instance.t_coord_x[net.name][k] == sum(
                n[2] * v for n, v in instance.node_is_term_vars[net.name][k].items()))
            instance.opt.Add(instance.t_coord_y[net.name][k] == sum(
                n[1] * v for n, v in instance.node_is_term_vars[net.name][k].items()))
        for k in range(net.num_terminals(), instance.net_to_flow_cnt[net.name]):
            instance.opt.Add(instance.t_coord_x[net.name][k] == sum(
                n[2] * v for n, v in instance.node_is_SON_vars[net.name][k].items()))
            instance.opt.Add(instance.t_coord_y[net.name][k] == sum(
                n[1] * v for n, v in instance.node_is_SON_vars[net.name][k].items()))

        # --- 2) Global bounding box (drives HPWL lower bound) ---
        num_t = len(instance.t_coord_x[net.name])
        all_x = [instance.s_coord_x[net.name]] + [instance.t_coord_x[net.name][k] for k in range(num_t)]
        all_y = [instance.s_coord_y[net.name]] + [instance.t_coord_y[net.name][k] for k in range(num_t)]
        instance.opt.AddMinEquality(instance.net_min_x[net.name], all_x)
        instance.opt.AddMaxEquality(instance.net_max_x[net.name], all_x)
        instance.opt.AddMinEquality(instance.net_min_y[net.name], all_y)
        instance.opt.AddMaxEquality(instance.net_max_y[net.name], all_y)

        # --- 3) Per-tier Y bounding box (neutral-value pattern) ---
        # For each (tier, element) pair, build a "neutral-valued" y so that
        # AddMinEquality / AddMaxEquality ignore tiers where the element has
        # no candidate node.
        elements = [("src", dict(instance.node_is_src_vars[net.name]))]
        for k in range(net.num_terminals()):
            elements.append((f"t{k}", dict(instance.node_is_term_vars[net.name][k])))
        for k in range(net.num_terminals(), instance.net_to_flow_cnt[net.name]):
            elements.append((f"son{k}", dict(instance.node_is_SON_vars[net.name][k])))

        for ti in all_tiers:
            my = tier_max_y[ti]
            min_cands, max_cands, all_tier_vars = [], [], []

            for elem_label, elem_nodes in elements:
                tier_nodes = {n: v for n, v in elem_nodes.items()
                              if layer_to_tier.get(n[0]) == ti}
                if not tier_nodes:
                    continue
                tier_vars = list(tier_nodes.values())
                all_tier_vars.extend(tier_vars)

                has_elem = instance.opt.NewBoolVar(f"has_{elem_label}_t{ti}_{net.name}")
                instance.opt.Add(sum(tier_vars) >= 1).OnlyEnforceIf(has_elem)
                instance.opt.Add(sum(tier_vars) == 0).OnlyEnforceIf(has_elem.Not())

                elem_y = sum(n[1] * v for n, v in tier_nodes.items())
                y_for_min = instance.opt.NewIntVar(0, my, f"ymin_{elem_label}_t{ti}_{net.name}")
                instance.opt.Add(y_for_min == elem_y).OnlyEnforceIf(has_elem)
                instance.opt.Add(y_for_min == my).OnlyEnforceIf(has_elem.Not())
                min_cands.append(y_for_min)

                y_for_max = instance.opt.NewIntVar(0, my, f"ymax_{elem_label}_t{ti}_{net.name}")
                instance.opt.Add(y_for_max == elem_y).OnlyEnforceIf(has_elem)
                instance.opt.Add(y_for_max == 0).OnlyEnforceIf(has_elem.Not())
                max_cands.append(y_for_max)

            if all_tier_vars:
                instance.opt.Add(sum(all_tier_vars) >= 1).OnlyEnforceIf(
                    instance.has_pins_on_tier[net.name][ti])
                instance.opt.Add(sum(all_tier_vars) == 0).OnlyEnforceIf(
                    instance.has_pins_on_tier[net.name][ti].Not())
            else:
                instance.opt.Add(instance.has_pins_on_tier[net.name][ti] == 0)

            if min_cands:
                instance.opt.AddMinEquality(instance.net_min_y_tier[net.name][ti], min_cands)
                instance.opt.AddMaxEquality(instance.net_max_y_tier[net.name][ti], max_cands)
            else:
                instance.opt.Add(instance.net_min_y_tier[net.name][ti] == my)
                instance.opt.Add(instance.net_max_y_tier[net.name][ti] == 0)

    # --- HPWL lower bound (global) ---
    # Use the minimum placement-tier X pitch - tight when tiers have
    # different col pitches (BPC vs PC). All QFET placement tiers share
    # one pitch so this collapses to that value.
    pitch_x = min(int(tech.get_pitch(ln)) for ln in placement_layers)
    pin_access_layer = ("M0" if "M0" in tech.pin_access_layer_names
                        else next(iter(tech.pin_access_layer_names)))
    pitch_y = int(tech.get_pitch(pin_access_layer))
    scaled_hpwl_sum = []
    for net in instance.circuit.get_nets(with_power_ground=False):
        scaled_hpwl_sum.append(
            (instance.net_max_x[net.name] - instance.net_min_x[net.name]) * pitch_y)
        scaled_hpwl_sum.append(
            (instance.net_max_y[net.name] - instance.net_min_y[net.name]) * pitch_x)
    total_edge_count = sum(instance.edge_vars.values())
    instance.opt.Add(total_edge_count * pitch_x * pitch_y >= sum(scaled_hpwl_sum))
    logger.info(
        f"\t==\tWirelength lower bound: edges * {pitch_x * pitch_y} >= "
        f"sum(HPWL), pitch_x={pitch_x} ({placement_layers[0]}-tier), "
        f"pitch_y={pitch_y} ({pin_access_layer})"
    )

    # Per-net wirelength bounds (small-cell tightening; skip when DBs > 1).
    if getattr(instance, "insert_num_db", 1) <= 1:
        instance.opt.log_comment("Per-net wirelength lower bounds ...")
        num_bounds = 0
        for net in instance.circuit.get_nets(with_power_ground=False):
            net_arc_sum = sum(
                instance.net_arc_vars[(net.name, u, v)]
                for u, v in instance.lgg.arcs()
                if (net.name, u, v) in instance.net_arc_vars
            )
            hpwl_x = (instance.net_max_x[net.name] - instance.net_min_x[net.name]) * pitch_y
            hpwl_y = (instance.net_max_y[net.name] - instance.net_min_y[net.name]) * pitch_x
            instance.opt.Add(net_arc_sum * pitch_x * pitch_y >= hpwl_x + hpwl_y)
            num_bounds += 1
        logger.info(f"\t==\tPer-net wirelength bounds: {num_bounds} constraints added")

    # --- Tolerance branch: global X + per-tier Y windows ---
    tol_x = instance.routing_tolerance_x
    tol_y = instance.routing_tolerance_y
    per_fanout = instance.routing_tolerance_per_fanout
    if tol_x != -1 or tol_y != -1:
        instance.opt.log_comment(
            f"Enforcing per-tier routing window (X tol={tol_x}, Y tol={tol_y}, "
            f"per_fanout={per_fanout}) ..."
        )
        max_col_const = max_col_all
        logger.info(
            f"\t==\tAdaptive tolerance: tol_x={tol_x}, tol_y={tol_y}, "
            f"per_fanout={per_fanout}"
        )

        for net in instance.circuit.get_nets(with_power_ground=False):
            fanout = 1 + instance.net_to_flow_cnt[net.name]
            net_tol_x = (tol_x + max(0, fanout - 2) * per_fanout) if tol_x != -1 else -1
            net_tol_y = (tol_y + max(0, fanout - 2) * per_fanout) if tol_y != -1 else -1

            # --- Global X window ---
            if net_tol_x != -1:
                unclamped_xmin = instance.opt.NewIntVar(
                    -net_tol_x, max_col_const, f"unclamped_xmin_{net.name}")
                instance.opt.Add(
                    unclamped_xmin == instance.net_min_x[net.name] - net_tol_x)
                instance.opt.AddMaxEquality(
                    instance.window_xmin_raw[net.name], [unclamped_xmin, 0])

                unclamped_xmax = instance.opt.NewIntVar(
                    0, max_col_const + net_tol_x, f"unclamped_xmax_{net.name}")
                instance.opt.Add(
                    unclamped_xmax == instance.net_max_x[net.name] + net_tol_x)
                instance.opt.AddMinEquality(
                    instance.window_xmax_raw[net.name], [unclamped_xmax, max_col_const])

            # --- Per-tier Y window ---
            if net_tol_y != -1:
                for ti in all_tiers:
                    my = tier_max_y[ti]
                    unclamped_ymin = instance.opt.NewIntVar(
                        -net_tol_y, my, f"unclamped_ymin_t{ti}_{net.name}")
                    instance.opt.Add(
                        unclamped_ymin
                        == instance.net_min_y_tier[net.name][ti] - net_tol_y)
                    instance.opt.AddMaxEquality(
                        instance.window_ymin_tier[net.name][ti],
                        [unclamped_ymin, 0])

                    unclamped_ymax = instance.opt.NewIntVar(
                        0, my + net_tol_y, f"unclamped_ymax_t{ti}_{net.name}")
                    instance.opt.Add(
                        unclamped_ymax
                        == instance.net_max_y_tier[net.name][ti] + net_tol_y)
                    instance.opt.AddMinEquality(
                        instance.window_ymax_tier[net.name][ti],
                        [unclamped_ymax, my])

            # --- Flow-level window enforcement (coordinate-grouped) ---
            # Bind the routing window to per-net FLOW vars, NOT arc vars. With
            # enable_reverse_flow_link=False (under active MAR/EOL DRC), a DRC
            # rule can force an arc active WITHOUT flow - a metal-extension
            # side-effect (e.g. via_induce forcing M0 horizontal metal).
            # Clamping the ARC would kill that flow-less DRC arc and make
            # routing-heavy cells (flip-flops) spuriously INFEASIBLE; clamping
            # FLOW lets the DRC arc sit just outside the window. For QFET
            # (enable_reverse_flow_link=True) link_flow_to_arc forces
            # arc=>flow, so banning flow at a coord also bans the arc there -
            # flow- and arc-clamps are equivalent -> no QFET regression.
            # Grouping flow vars by endpoint coordinate keeps this
            # O(unique_coords) not O(arcs*K); sum(fvars)==0 reification (not a
            # per-var OnlyEnforceIf) keeps it valid for int-flow nets whose flow
            # vars are IntVars rather than BoolVars.
            flow_by_ux, flow_by_vx = {}, {}
            flow_by_uy, flow_by_vy = {}, {}  # keyed by (tier, coord)
            for u_arc, v_arc in instance.lgg.arcs():
                for k in range(instance.net_to_flow_cnt[net.name]):
                    fv = instance.net_flow_vars.get((net.name, k, u_arc, v_arc))
                    if fv is None:
                        continue
                    if net_tol_x != -1:
                        flow_by_ux.setdefault(u_arc[2], []).append(fv)
                        flow_by_vx.setdefault(v_arc[2], []).append(fv)
                    if net_tol_y != -1:
                        flow_by_uy.setdefault((layer_to_tier[u_arc[0]], u_arc[1]), []).append(fv)
                        flow_by_vy.setdefault((layer_to_tier[v_arc[0]], v_arc[1]), []).append(fv)

            if net_tol_x != -1:
                w_xmin = instance.window_xmin_raw[net.name]
                w_xmax = instance.window_xmax_raw[net.name]
                for side, grouped in (("ux", flow_by_ux), ("vx", flow_by_vx)):
                    for coord, fvars in grouped.items():
                        blo = instance.opt.NewBoolVar(f"wxmin_gt_{side}{coord}_{net.name}")
                        instance.opt.Add(w_xmin > coord).OnlyEnforceIf(blo)
                        instance.opt.Add(w_xmin <= coord).OnlyEnforceIf(blo.Not())
                        instance.opt.Add(sum(fvars) == 0).OnlyEnforceIf(blo)
                        bhi = instance.opt.NewBoolVar(f"{side}{coord}_gt_wxmax_{net.name}")
                        instance.opt.Add(w_xmax < coord).OnlyEnforceIf(bhi)
                        instance.opt.Add(w_xmax >= coord).OnlyEnforceIf(bhi.Not())
                        instance.opt.Add(sum(fvars) == 0).OnlyEnforceIf(bhi)

            if net_tol_y != -1:
                for side, grouped in (("uy", flow_by_uy), ("vy", flow_by_vy)):
                    for (ti, coord), fvars in grouped.items():
                        has = instance.has_pins_on_tier[net.name][ti]
                        w_ymin = instance.window_ymin_tier[net.name][ti]
                        w_ymax = instance.window_ymax_tier[net.name][ti]
                        blo = instance.opt.NewBoolVar(f"wymin_gt_{side}t{ti}c{coord}_{net.name}")
                        instance.opt.Add(w_ymin > coord).OnlyEnforceIf(blo)
                        instance.opt.Add(w_ymin <= coord).OnlyEnforceIf(blo.Not())
                        instance.opt.Add(sum(fvars) == 0).OnlyEnforceIf([blo, has])
                        bhi = instance.opt.NewBoolVar(f"{side}t{ti}c{coord}_gt_wymax_{net.name}")
                        instance.opt.Add(w_ymax < coord).OnlyEnforceIf(bhi)
                        instance.opt.Add(w_ymax >= coord).OnlyEnforceIf(bhi.Not())
                        instance.opt.Add(sum(fvars) == 0).OnlyEnforceIf([bhi, has])
    else:
        # No tolerance: canvas-boundary bans handled by
        # `prohibit_routing_to_{left,right}_cell_boundaries`. Nothing else
        # to do here (per-arc canvas bounds are redundant with edge bans).
        pass

def _get_net_reachable_layers_cfet(cfet, net):
    """
    Determine which layer indices a net can reach based on its connected transistors.
    A pure-PMOS net only needs the PMOS placement layer; a pure-NMOS net only needs
    the NMOS placement layer. Cross-device nets need both. All nets can use M0, M1, M2.

    Returns:
        set of layer indices that this net is allowed to use.
    """
    pmos_layer_idx = cfet.lgg.layer_index(cfet.pmos_layer)
    nmos_layer_idx = cfet.lgg.layer_index(cfet.nmos_layer)
    # routing layers (M0, M1, M2) are always reachable
    reachable = set()
    for layer_name in cfet.lgg.idx_to_layer.values():
        if layer_name not in ("BPC", "PC"):
            reachable.add(cfet.lgg.layer_index(layer_name))
    # check which device types this net touches
    has_pmos = False
    has_nmos = False
    for tran_name, _ in net.connected_transistors:
        tran = cfet.circuit.transistors[tran_name]
        if tran.model == Model.PMOS:
            has_pmos = True
        elif tran.model == Model.NMOS:
            has_nmos = True
    if has_pmos:
        reachable.add(pmos_layer_idx)
    if has_nmos:
        reachable.add(nmos_layer_idx)
    return reachable


def routing_localization_cfet(cfet):
    """
    Enforce routing localization constraints for CFET.
    CFET-tailored: uses per-device layers (BPC/PC) for coordinate domains
    and prunes arcs on unreachable layers per net.

    Args:
        cfet: The CFET instance
    """
    # Unified column/row domains across all routing-relevant layers
    all_cols = list(sorted(set(
        cfet.lgg.cols_in_layer("PC") + cfet.lgg.cols_in_layer("M1")
    )))
    all_rows = list(sorted(set(
        cfet.lgg.rows_in_layer("PC") + cfet.lgg.rows_in_layer("M1")
    )))
    all_cols_domain = cp_model.Domain.FromValues(all_cols)
    all_rows_domain = cp_model.Domain.FromValues(all_rows)
    pc_cols_domain = cp_model.Domain.FromValues(cfet.lgg.cols_in_layer("PC"))

    # Pre-compute reachable layers per net and banned arcs
    net_reachable_layers = {}
    net_banned_arcs = {}
    for net in cfet.circuit.get_nets(with_power_ground=False):
        reachable = _get_net_reachable_layers_cfet(cfet, net)
        net_reachable_layers[net.name] = reachable
        # An arc is banned if BOTH endpoints are on an unreachable layer
        # (cross-layer vias where one side is reachable are kept)
        banned = set()
        for u_arc, v_arc in cfet.lgg.arcs():
            if u_arc[0] not in reachable and v_arc[0] not in reachable:
                banned.add((u_arc, v_arc))
        net_banned_arcs[net.name] = banned

    # Pre-emptively ban arcs/flows on unreachable layers
    ban_count = 0
    for net in cfet.circuit.get_nets(with_power_ground=False):
        banned = net_banned_arcs[net.name]
        for (u_arc, v_arc) in banned:
            cfet.opt.Add(cfet.net_arc_vars[(net.name, u_arc, v_arc)] == 0)
            for k in range(cfet.net_to_flow_cnt[net.name]):
                cfet.opt.Add(cfet.net_flow_vars[(net.name, k, u_arc, v_arc)] == 0)
            ban_count += 1
    logger.info(f"\t==\tCFET layer pruning: banned {ban_count} unreachable arc-net pairs")

    # NOTE: populate coordinate variables regardless of tolerance
    for net in cfet.circuit.get_nets(with_power_ground=False):
        src_tran_name, src_pin = net.source()
        src_tran = cfet.circuit.transistors[src_tran_name]
        # Use the correct placement layer for this transistor's coordinates
        src_layer = cfet.pmos_layer if src_tran.model == Model.PMOS else cfet.nmos_layer

        # eligible src pin x coordinate (PC and BPC share same column grid)
        cfet.s_coord_x[net.name] = cfet.opt.NewIntVarFromDomain(
            pc_cols_domain,
            f"s_coord_x_{net.name}",
        )
        # eligible src pin y coordinate (use the device's actual layer rows)
        cfet.s_coord_y[net.name] = cfet.opt.NewIntVarFromDomain(
            cp_model.Domain.FromValues(cfet.lgg.rows_in_layer(src_layer)),
            f"s_coord_y_{net.name}",
        )

        cfet.t_coord_x[net.name] = []
        cfet.t_coord_y[net.name] = []
        # internal terminal pins
        for k in range(net.num_terminals()):
            term_tran_name, term_pin = net.terminals()[k]
            term_tran = cfet.circuit.transistors[term_tran_name]
            term_layer = cfet.pmos_layer if term_tran.model == Model.PMOS else cfet.nmos_layer

            cfet.t_coord_x[net.name].append(
                cfet.opt.NewIntVarFromDomain(
                    pc_cols_domain,
                    f"t_coord_x_{net.name}_{k}",
                )
            )
            # y domain: device layer rows + M1 rows (for routing flexibility)
            cfet.t_coord_y[net.name].append(
                cfet.opt.NewIntVarFromDomain(
                    cp_model.Domain.FromValues(
                        list(sorted(set(cfet.lgg.rows_in_layer(term_layer) + cfet.lgg.rows_in_layer("M1"))))
                    ),
                    f"t_coord_y_{net.name}_{k}",
                )
            )
        # external IO terminal pins
        for k in range(net.num_terminals(), cfet.net_to_flow_cnt[net.name], 1):
            cfet.t_coord_x[net.name].append(
                cfet.opt.NewIntVarFromDomain(
                    all_cols_domain,
                    f"t_coord_x_{net.name}_{k}",
                )
            )
            cfet.t_coord_y[net.name].append(
                cfet.opt.NewIntVarFromDomain(
                    all_rows_domain,
                    f"t_coord_y_{net.name}_{k}",
                )
            )

        # routing window can be at any column and row
        cfet.net_min_x[net.name] = cfet.opt.NewIntVarFromDomain(all_cols_domain, f"net_min_x_{net.name}")
        cfet.net_max_x[net.name] = cfet.opt.NewIntVarFromDomain(all_cols_domain, f"net_max_x_{net.name}")
        cfet.net_min_y[net.name] = cfet.opt.NewIntVarFromDomain(all_rows_domain, f"net_min_y_{net.name}")
        cfet.net_max_y[net.name] = cfet.opt.NewIntVarFromDomain(all_rows_domain, f"net_max_y_{net.name}")

        max_coord = max(all_cols[-1], all_rows[-1]) if all_cols and all_rows else 0
        domain_window = cp_model.Domain(0, max_coord * 2)
        cfet.window_xmin_raw[net.name] = cfet.opt.NewIntVarFromDomain(domain_window, f"window_xmin_raw_{net.name}")
        cfet.window_xmax_raw[net.name] = cfet.opt.NewIntVarFromDomain(domain_window, f"window_xmax_raw_{net.name}")
        cfet.window_ymin_raw[net.name] = cfet.opt.NewIntVarFromDomain(domain_window, f"window_ymin_raw_{net.name}")
        cfet.window_ymax_raw[net.name] = cfet.opt.NewIntVarFromDomain(domain_window, f"window_ymax_raw_{net.name}")

        # 1. Link placement variables to actual source/terminal coordinate variables
        cfet.opt.Add(cfet.s_coord_x[net.name] == sum(node[2] * var for node, var in cfet.node_is_src_vars[net.name].items()))
        cfet.opt.Add(cfet.s_coord_y[net.name] == sum(node[1] * var for node, var in cfet.node_is_src_vars[net.name].items()))

        for k in range(net.num_terminals()):
            cfet.opt.Add(cfet.t_coord_x[net.name][k] == sum(node[2] * var for node, var in cfet.node_is_term_vars[net.name][k].items()))
            cfet.opt.Add(cfet.t_coord_y[net.name][k] == sum(node[1] * var for node, var in cfet.node_is_term_vars[net.name][k].items()))
        for k in range(net.num_terminals(), cfet.net_to_flow_cnt[net.name], 1):
            cfet.opt.Add(cfet.t_coord_x[net.name][k] == sum(node[2] * var for node, var in cfet.node_is_SON_vars[net.name][k].items()))
            cfet.opt.Add(cfet.t_coord_y[net.name][k] == sum(node[1] * var for node, var in cfet.node_is_SON_vars[net.name][k].items()))

        # 2. Define net's bounding box
        all_x_coords_for_net = [cfet.s_coord_x[net.name]] + [
            cfet.t_coord_x[net.name][k]
            for k in range(cfet.net_to_flow_cnt[net.name])
        ]
        all_y_coords_for_net = [cfet.s_coord_y[net.name]] + [
            cfet.t_coord_y[net.name][k]
            for k in range(cfet.net_to_flow_cnt[net.name])
        ]
        cfet.opt.AddMinEquality(cfet.net_min_x[net.name], all_x_coords_for_net)
        cfet.opt.AddMaxEquality(cfet.net_max_x[net.name], all_x_coords_for_net)
        cfet.opt.AddMinEquality(cfet.net_min_y[net.name], all_y_coords_for_net)
        cfet.opt.AddMaxEquality(cfet.net_max_y[net.name], all_y_coords_for_net)

    # Wirelength lower bound based on bounding box half-perimeters (HPWL)
    pitch_x = int(cfet.tech.get_pitch("PC"))
    pitch_y = int(cfet.tech.get_pitch("M0"))
    scaled_hpwl_sum = []
    for net in cfet.circuit.get_nets(with_power_ground=False):
        scaled_hpwl_sum.append((cfet.net_max_x[net.name] - cfet.net_min_x[net.name]) * pitch_y)
        scaled_hpwl_sum.append((cfet.net_max_y[net.name] - cfet.net_min_y[net.name]) * pitch_x)
    total_edge_count = sum(cfet.edge_vars.values())
    cfet.opt.Add(total_edge_count * pitch_x * pitch_y >= sum(scaled_hpwl_sum))
    logger.info(f"\t==\tWirelength lower bound: edges * {pitch_x * pitch_y} >= sum(HPWL), pitch_x={pitch_x}, pitch_y={pitch_y}")

    # if tolerance is set
    if cfet.routing_tolerance != -1:
        cfet.opt.log_comment(f"Enforcing routing window constraints {cfet.routing_tolerance} ...")
        max_col = (cfet.cpp_cost + (_NUM_COL_SDG_ - 1)) * int(cfet.tech.get_pitch("PC"))
        max_row = (cfet.tech.num_rt_track - 1) * int(cfet.tech.get_pitch("M0")) * 2
        logger.info(f"\t==\tEnforcing max row and col for routing window to {max_row} and {max_col} ...")
        for net in cfet.circuit.get_nets(with_power_ground=False):
            banned = net_banned_arcs[net.name]

            # 3. Define routing window boundaries with clamping
            unclamped_xmin = cfet.opt.NewIntVar(
                -cfet.routing_tolerance,
                cfet.lgg.max_col_in_layer("PC") + cfet.lgg.max_col_in_layer("M1"),
                f"unclamped_xmin_{net.name}"
            )
            cfet.opt.Add(unclamped_xmin == cfet.net_min_x[net.name] - cfet.routing_tolerance)
            cfet.opt.AddMaxEquality(cfet.window_xmin_raw[net.name], [unclamped_xmin, 0])

            unclamped_xmax = cfet.opt.NewIntVar(
                0,
                cfet.lgg.max_col_in_layer("PC") + cfet.lgg.max_col_in_layer("M1") + cfet.routing_tolerance,
                f"unclamped_xmax_{net.name}"
            )
            cfet.opt.Add(unclamped_xmax == cfet.net_max_x[net.name] + cfet.routing_tolerance)
            cfet.opt.AddMinEquality(cfet.window_xmax_raw[net.name], [unclamped_xmax, max_col])

            unclamped_ymin = cfet.opt.NewIntVar(
                -cfet.routing_tolerance,
                cfet.lgg.max_row_in_layer("PC") + cfet.lgg.max_row_in_layer("M1"),
                f"unclamped_ymin_{net.name}"
            )
            cfet.opt.Add(unclamped_ymin == cfet.net_min_y[net.name] - cfet.routing_tolerance)
            cfet.opt.AddMaxEquality(cfet.window_ymin_raw[net.name], [unclamped_ymin, 0])

            unclamped_ymax = cfet.opt.NewIntVar(
                0,
                cfet.lgg.max_row_in_layer("PC") + cfet.lgg.max_row_in_layer("M1") + cfet.routing_tolerance,
                f"unclamped_ymax_{net.name}"
            )
            cfet.opt.Add(unclamped_ymax == cfet.net_max_y[net.name] + cfet.routing_tolerance)
            cfet.opt.AddMinEquality(cfet.window_ymax_raw[net.name], [unclamped_ymax, max_row])

            # 4. Constrain flow to routing window using coordinate grouping.
            # Arc-level canvas constraints are redundant with prohibit_routing_to_*_cell_boundaries.
            # Group flow vars by endpoint coordinates for O(unique_coords) instead of O(arcs * K * 8).
            flow_by_u_x = {}
            flow_by_u_y = {}
            flow_by_v_x = {}
            flow_by_v_y = {}
            for u_arc, v_arc in cfet.lgg.arcs():
                if (u_arc, v_arc) in banned:
                    continue
                for k in range(cfet.net_to_flow_cnt[net.name]):
                    fv = cfet.net_flow_vars[(net.name, k, u_arc, v_arc)]
                    flow_by_u_x.setdefault(u_arc[2], []).append(fv)
                    flow_by_u_y.setdefault(u_arc[1], []).append(fv)
                    flow_by_v_x.setdefault(v_arc[2], []).append(fv)
                    flow_by_v_y.setdefault(v_arc[1], []).append(fv)

            w_xmin = cfet.window_xmin_raw[net.name]
            w_xmax = cfet.window_xmax_raw[net.name]
            w_ymin = cfet.window_ymin_raw[net.name]
            w_ymax = cfet.window_ymax_raw[net.name]

            for coord_val, fvars in flow_by_u_x.items():
                b = cfet.opt.NewBoolVar(f"wxmin_gt_ux{coord_val}_{net.name}")
                cfet.opt.Add(w_xmin > coord_val).OnlyEnforceIf(b)
                cfet.opt.Add(w_xmin <= coord_val).OnlyEnforceIf(b.Not())
                cfet.opt.Add(sum(fvars) == 0).OnlyEnforceIf(b)
            for coord_val, fvars in flow_by_u_x.items():
                b = cfet.opt.NewBoolVar(f"ux{coord_val}_gt_wxmax_{net.name}")
                cfet.opt.Add(w_xmax < coord_val).OnlyEnforceIf(b)
                cfet.opt.Add(w_xmax >= coord_val).OnlyEnforceIf(b.Not())
                cfet.opt.Add(sum(fvars) == 0).OnlyEnforceIf(b)
            for coord_val, fvars in flow_by_u_y.items():
                b = cfet.opt.NewBoolVar(f"wymin_gt_uy{coord_val}_{net.name}")
                cfet.opt.Add(w_ymin > coord_val).OnlyEnforceIf(b)
                cfet.opt.Add(w_ymin <= coord_val).OnlyEnforceIf(b.Not())
                cfet.opt.Add(sum(fvars) == 0).OnlyEnforceIf(b)
            for coord_val, fvars in flow_by_u_y.items():
                b = cfet.opt.NewBoolVar(f"uy{coord_val}_gt_wymax_{net.name}")
                cfet.opt.Add(w_ymax < coord_val).OnlyEnforceIf(b)
                cfet.opt.Add(w_ymax >= coord_val).OnlyEnforceIf(b.Not())
                cfet.opt.Add(sum(fvars) == 0).OnlyEnforceIf(b)
            for coord_val, fvars in flow_by_v_x.items():
                b = cfet.opt.NewBoolVar(f"wxmin_gt_vx{coord_val}_{net.name}")
                cfet.opt.Add(w_xmin > coord_val).OnlyEnforceIf(b)
                cfet.opt.Add(w_xmin <= coord_val).OnlyEnforceIf(b.Not())
                cfet.opt.Add(sum(fvars) == 0).OnlyEnforceIf(b)
            for coord_val, fvars in flow_by_v_x.items():
                b = cfet.opt.NewBoolVar(f"vx{coord_val}_gt_wxmax_{net.name}")
                cfet.opt.Add(w_xmax < coord_val).OnlyEnforceIf(b)
                cfet.opt.Add(w_xmax >= coord_val).OnlyEnforceIf(b.Not())
                cfet.opt.Add(sum(fvars) == 0).OnlyEnforceIf(b)
            for coord_val, fvars in flow_by_v_y.items():
                b = cfet.opt.NewBoolVar(f"wymin_gt_vy{coord_val}_{net.name}")
                cfet.opt.Add(w_ymin > coord_val).OnlyEnforceIf(b)
                cfet.opt.Add(w_ymin <= coord_val).OnlyEnforceIf(b.Not())
                cfet.opt.Add(sum(fvars) == 0).OnlyEnforceIf(b)
            for coord_val, fvars in flow_by_v_y.items():
                b = cfet.opt.NewBoolVar(f"vy{coord_val}_gt_wymax_{net.name}")
                cfet.opt.Add(w_ymax < coord_val).OnlyEnforceIf(b)
                cfet.opt.Add(w_ymax >= coord_val).OnlyEnforceIf(b.Not())
                cfet.opt.Add(sum(fvars) == 0).OnlyEnforceIf(b)

    # ^ enforce that each net must be routed within the boundary
    else:
        # Canvas boundary constraints are fully handled by prohibit_routing_to_*_cell_boundaries
        # and layer-pruning bans above. Trivially true and redundant constraints omitted.
        pass


def cfet_cross_device_via_lower_bound(cfet):
    """
    For each flow (net, k) where the source and the k-th terminal are on
    different device layers, enforce that the flow uses at least 2 cross-layer
    arcs - CONDITIONED on the flow not being shared (i.e., is_shared == False).

    When is_shared == True, all flows for that terminal are zeroed out and the
    net is satisfied through a shared diffusion/LISD/gate contact, so no via
    constraint applies.

    Requires: cfet.net_terminal_is_shared must be populated
    (call after _induce_internal_routing_flow_with_diffusion).
    """
    cfet.opt.log_comment("Cross-device via lower bound for CFET (conditional on sharing)")
    pmos_layer_idx = cfet.lgg.layer_index(cfet.pmos_layer)
    nmos_layer_idx = cfet.lgg.layer_index(cfet.nmos_layer)

    num_tightened = 0
    for net in cfet.circuit.get_nets(with_power_ground=False):
        src_tran_name, _ = net.source()
        src_model = cfet.circuit.transistors[src_tran_name].model
        src_layer_idx = pmos_layer_idx if src_model == Model.PMOS else nmos_layer_idx

        for k, (term_tran_name, _) in enumerate(net.terminals()):
            term_model = cfet.circuit.transistors[term_tran_name].model
            term_layer_idx = pmos_layer_idx if term_model == Model.PMOS else nmos_layer_idx

            if term_layer_idx == src_layer_idx:
                continue  # same-device flow, no mandatory cross-layer traversal

            is_shared = cfet.net_terminal_is_shared.get((net.name, k))
            if is_shared is None:
                continue

            # Cross-device, non-shared flow: must use >= 2 cross-layer arcs
            cross_layer_flow_vars = []
            for u_arc, v_arc in cfet.lgg.arcs():
                if u_arc[0] != v_arc[0]:
                    cross_layer_flow_vars.append(
                        cfet.net_flow_vars[(net.name, k, u_arc, v_arc)]
                    )
            if len(cross_layer_flow_vars) > 0:
                # Minimum is 1 (via MIV: BPC->PC directly), not 2
                cfet.opt.Add(sum(cross_layer_flow_vars) >= 1).OnlyEnforceIf(is_shared.Not())
                num_tightened += 1

    logger.info(f"\t==\tCFET cross-device via lower bound: tightened {num_tightened} flows")


def cfet_hpwl_via_cost_tightening(cfet):
    """
    Tighten the HPWL lower bound by adding mandatory via costs for cross-device
    flows that are NOT shared.

    For each (net, k) where source and terminal k are on different device layers
    and is_shared == False, the routing must use at least 2 cross-layer edges
    (cost 5 each). We encode this as a dynamic via cost term conditioned on
    the sharing decision variables.

    Requires: cfet.net_terminal_is_shared must be populated.
    """
    cfet.opt.log_comment("HPWL via cost tightening for CFET (conditional on sharing)")
    pmos_layer_idx = cfet.lgg.layer_index(cfet.pmos_layer)
    nmos_layer_idx = cfet.lgg.layer_index(cfet.nmos_layer)
    via_cost = 5  # from _init_edge_vars

    # Build a dynamic via cost: sum of (2 * via_cost) for each non-shared cross-device flow
    # Use integer variables: per-flow via cost is 2*via_cost if not shared, 0 if shared
    via_cost_vars = []
    num_cross_flows = 0
    for net in cfet.circuit.get_nets(with_power_ground=False):
        src_tran_name, _ = net.source()
        src_model = cfet.circuit.transistors[src_tran_name].model
        src_layer_idx = pmos_layer_idx if src_model == Model.PMOS else nmos_layer_idx

        for k, (term_tran_name, _) in enumerate(net.terminals()):
            term_model = cfet.circuit.transistors[term_tran_name].model
            term_layer_idx = pmos_layer_idx if term_model == Model.PMOS else nmos_layer_idx

            if term_layer_idx == src_layer_idx:
                continue

            is_shared = cfet.net_terminal_is_shared.get((net.name, k))
            if is_shared is None:
                continue

            # non-shared cross-device flow contributes at least 1 * via_cost
            # (minimum path: BPC->PC via MIV = 1 cross-layer edge at cost 5)
            flow_via_cost = cfet.opt.NewIntVar(0, via_cost, f"via_cost_{net.name}_{k}")
            cfet.opt.Add(flow_via_cost == via_cost).OnlyEnforceIf(is_shared.Not())
            cfet.opt.Add(flow_via_cost == 0).OnlyEnforceIf(is_shared)
            via_cost_vars.append(flow_via_cost)
            num_cross_flows += 1

    if via_cost_vars:
        total_weighted_wl = sum(
            edge_var * cfet.edge_to_cost[(u, v)]
            for (u, v), edge_var in cfet.edge_vars.items()
        )
        hpwl_sum = []
        for net in cfet.circuit.get_nets(with_power_ground=False):
            hpwl_sum.append(cfet.net_max_x[net.name] - cfet.net_min_x[net.name])
            hpwl_sum.append(cfet.net_max_y[net.name] - cfet.net_min_y[net.name])

        cfet.opt.Add(total_weighted_wl >= sum(hpwl_sum) + sum(via_cost_vars))

    logger.info(
        f"\t==\tCFET HPWL+via tightening: {num_cross_flows} cross-device flows "
        f"with conditional via cost of {via_cost} each"
    )
