"""
Placement-related constraints for instance layout optimization.
This module contains all placement constraint implementations.
"""

from loguru import logger
from src.cellgen.core.entity import Model


def _enforce_pin_at_col(instance, tvar_attr, tvar, net, zi, target_col, cond):
    """
    For one S/D/G pin: require exactly one bool true at `target_col` (this tier) and
    zero out every other-col bool for this transistor in this tier - all under `cond`.

    `tvar_attr` is one of "s_col_idx_var" / "g_col_idx_var" / "d_col_idx_var".
    The structure is `tvar.{attr}[net_name][zi][col] = [BoolVars]`. `net` is a
    net-name string (callers pass `tran.source` / `tran.gate` / `tran.drain`).
    """
    per_tier = getattr(tvar, tvar_attr).get(net, {}).get(zi, {})
    # exactly one at target_col
    on_target = per_tier.get(target_col, [])
    if on_target:
        instance.opt.Add(sum(on_target) == 1).OnlyEnforceIf(cond)
    # zero everywhere else (within this tier)
    for c, bvs in per_tier.items():
        if c != target_col and bvs:
            instance.opt.Add(sum(bvs) == 0).OnlyEnforceIf(cond)


def link_source_drain_gate_columns_to_transistor_placement(instance):
    """
    Link source / drain / gate column vars to transistor (col, tier) placement.

    For each transistor, walks every (col, tier) slot it could occupy
    (instance.plc_ci x instance.plc_zi). Conditioned on the per-slot placement
    bool `placed_tran_at_xzi_vars[(tran, ci, zi)]` and the flip variable,
    enforces - within the chosen tier:

        - Flipped:     source @ col_rr (ci+2),  drain @ col (ci),     gate @ col_r (ci+1)
        - Not flipped: source @ col (ci),       drain @ col_rr (ci+2), gate @ col_r (ci+1)
        - Gate is at col_r regardless of flip.

    Also bans other nets from routing through the occupied s/d/g nodes (via
    `ban_other_nets_from_using_nodes`) and zeros out the transistor's own s/d/g
    bools at all other cols within this tier.

    QFET 4-tier adaptation: every constraint is scoped per (col, tier) - the
    placement is z-aware. Two same-model transistors can share a column iff
    they're on different tiers (already enforced by _add_distinct_placement).
    """
    instance.opt.log_comment("Linking source/drain/gate columns to transistor placement")
    for tran in instance.circuit.transistors.values():
        tvar = instance.transistor_vars[tran.name]
        source_net, gate_net, drain_net = tran.source, tran.gate, tran.drain

        for zi in instance.plc_zi:
            layer_name = instance.lgg.idx_to_layer[zi]
            for ci in instance.plc_ci:
                placed_here = instance.placed_tran_at_xzi_vars[(tran.name, ci, zi)]
                col    = instance.lgg.col_in_layer(layer_name, ci)      # left s/d
                col_r  = instance.lgg.col_in_layer(layer_name, ci + 1)  # gate
                col_rr = instance.lgg.col_in_layer(layer_name, ci + 2)  # right s/d

                # FLIPPED: source @ col_rr, drain @ col
                flip_cond = [tvar.flip_var, placed_here]
                _enforce_pin_at_col(instance, "s_col_idx_var", tvar, source_net, zi, col_rr, flip_cond)
                _enforce_pin_at_col(instance, "d_col_idx_var", tvar, drain_net,  zi, col,    flip_cond)
                ban_other_nets_from_using_nodes(
                    instance, net_to_skip=source_net,
                    nodes=instance._gather_region_nodes(zi, col_rr, tran.model),
                    cond=flip_cond,
                )
                ban_other_nets_from_using_nodes(
                    instance, net_to_skip=drain_net,
                    nodes=instance._gather_region_nodes(zi, col,    tran.model),
                    cond=flip_cond,
                )

                # NOT FLIPPED: source @ col, drain @ col_rr
                noflip_cond = [tvar.flip_var.Not(), placed_here]
                _enforce_pin_at_col(instance, "s_col_idx_var", tvar, source_net, zi, col,    noflip_cond)
                _enforce_pin_at_col(instance, "d_col_idx_var", tvar, drain_net,  zi, col_rr, noflip_cond)
                ban_other_nets_from_using_nodes(
                    instance, net_to_skip=source_net,
                    nodes=instance._gather_region_nodes(zi, col,    tran.model),
                    cond=noflip_cond,
                )
                ban_other_nets_from_using_nodes(
                    instance, net_to_skip=drain_net,
                    nodes=instance._gather_region_nodes(zi, col_rr, tran.model),
                    cond=noflip_cond,
                )

                # GATE: always @ col_r (independent of flip)
                _enforce_pin_at_col(instance, "g_col_idx_var", tvar, gate_net, zi, col_r, placed_here)
                ban_other_nets_from_using_nodes(
                    instance, net_to_skip=gate_net,
                    nodes=instance._gather_region_nodes(zi, col_r, tran.model),
                    cond=placed_here,
                )



def diffusion_alignment(instance):
    """
    Enforce per-tier diffusion alignment between PMOS and NMOS.

    For every placement slot (ci, zi):
      - PMOS DB at the slot <-> NMOS DB at the same slot (bidirectional).
      - Unified marker `db_vars[(ci, zi)]` is true iff both are DBs.

    The per-tier scope (was per-col aggregate) means a DB on PMOS at tier 2 col 5
    forces NMOS to also have a DB at tier 2 col 5 - not just "some tier of col 5".
    No-op when tech.enforce_diffusion_alignment is False.

    NOTE: bidirectional via two AddImplications was empirically faster to solve
    than a single equivalence constraint.
    """
    if not instance.tech.enforce_diffusion_alignment:
        return

    instance.opt.log_comment("Enforcing per-tier diffusion alignment between PMOS and NMOS")
    logger.info("\t==\tEnforcing per-tier diffusion alignment between PMOS and NMOS")

    for ci in instance.plc_ci:
        for zi in instance.plc_zi:
            pdb = instance.db_pmos_vars[(ci, zi)]
            ndb = instance.db_nmos_vars[(ci, zi)]
            # Per-tier bidirectional alignment
            instance.opt.AddImplication(pdb, ndb)
            instance.opt.AddImplication(ndb, pdb)
            # Unified marker (consumed by downstream constraints / objective)
            unified = instance.opt.NewBoolVar(f"db_ci{ci}_zi{zi}")
            instance.opt.Add(unified == 1).OnlyEnforceIf([pdb, ndb])
            instance.opt.Add(unified == 0).OnlyEnforceIf([pdb.Not(), ndb.Not()])
            instance.db_vars[(ci, zi)] = unified


def limit_diffusion_breaks(instance):
    """
    Set allowable diffusion break columns.

    Args:
        instance: The instance instance
    """
    instance.opt.log_comment(f"Setting allowable diffusion break columns...")
    if instance.tech.allowable_diffusion_break_cols == "ALL":
        # diffusion break is allowed in all placeable columns.
        pass
    elif instance.tech.allowable_diffusion_break_cols == "NONE":
        # diffusion break is not allowed in any placeable columns.
        for ci in instance.plc_ci:
            instance.opt.Add(instance.db_pmos_cols_vars[ci] == 0)
            instance.opt.Add(instance.db_nmos_cols_vars[ci] == 0)
    elif instance.tech.allowable_diffusion_break_cols == "SPLIT":
        # diffusion break is allowed in the two end portions of the placeable columns.
        col_indices = instance.plc_ci
        total_cols = len(col_indices)
        one_fourth_col_idx = int(total_cols / 4) + 1  # +1 to make it less aggressive
        for ci in col_indices:
            if ci >= one_fourth_col_idx and ci <= total_cols - one_fourth_col_idx:
                instance.opt.Add(instance.db_pmos_cols_vars[ci] == 0)
                instance.opt.Add(instance.db_nmos_cols_vars[ci] == 0)
    elif instance.tech.allowable_diffusion_break_cols == "CENTER":
        # diffusion break is allowed in the center portion of the placeable columns.
        col_indices = instance.plc_ci
        total_cols = len(col_indices)
        one_fourth_col_idx = int(total_cols / 4) - 1  # -1 to make it less aggressive
        for ci in col_indices:
            if ci >= one_fourth_col_idx and ci <= total_cols - one_fourth_col_idx:
                instance.opt.Add(instance.db_pmos_cols_vars[ci] == 0)
                instance.opt.Add(instance.db_nmos_cols_vars[ci] == 0)
    elif instance.tech.allowable_diffusion_break_cols == "OTHER":
        # diffusion break is allowed on every other column.
        for i, ci in enumerate(instance.plc_ci):
            if i % 2 == 0:
                instance.opt.Add(instance.db_pmos_cols_vars[ci] == 0)
                instance.opt.Add(instance.db_nmos_cols_vars[ci] == 0)
    else:
        raise ValueError(f"Unknown diffusion break cols: {instance.tech.allowable_diffusion_break_cols}")


def placement_lexico_order_symmetry_breaking(instance):
    """
    Break left-right reflection symmetry across all placement tiers.

    Linearises each transistor's (x, z) position into a single ordinal:

        ord = x_var + max_col * z_var

    z_var is the LGG layer index of the placement tier. With 4 QFET tiers, two
    same-model transistors at the same column on different tiers get distinct
    ordinals - so the lex constraint discriminates them correctly (was x-only
    before z-aware placement; same-x same-tier pairs collapsed and lex
    couldn't separate them).

    Then enforces  ord(T_sorted)  <=lex  ord(T_sorted_reversed)  to rule out
    the mirror-image placement.

    NOTE: the lex-leq encoding below (eq/lt reifiers + prefix chain) is
    standard; only the ordinal definition changes vs the legacy code.
    """
    instance.opt.log_comment("Enforcing Lexicographic Order Symmetry Breaking (x + max_col * z)")

    if not instance.circuit.transistors:
        return

    # Build per-transistor ordinal: x + max_col * z. Domain bound covers the
    # widest possible product so the IntVar can hold any (x, z) combination.
    max_col = max(instance.plc_ci) + 1
    max_z   = max(instance.plc_zi) + 1
    ord_range = max_col * max_z - 1

    ord_vars = {}
    for tran in instance.circuit.transistors.values():
        tvar = instance.transistor_vars[tran.name]
        o = instance.opt.NewIntVar(0, ord_range, f"ord_{tran.name}")
        # Single-tier (planar) techs (FinFET) leave z_var=None: the ordinal is
        # just x (one placement tier, so z contributes nothing). Tier techs
        # (QFET) fold the tier into the ordinal so cross-tier placements are
        # ordered distinctly. Default path (z_var set) is unchanged.
        if tvar.z_var is None:
            instance.opt.Add(o == tvar.x_var)
        else:
            instance.opt.Add(o == tvar.x_var + max_col * tvar.z_var)
        ord_vars[tran.name] = o

    seq     = [ord_vars[t.name] for t in sorted(instance.circuit.transistors.values())]
    seq_rev = list(reversed(seq))
    _enforce_lex_leq(instance, seq, seq_rev, tag="lex")


def _enforce_lex_leq(instance, seq, seq_rev, tag):
    """
    Enforce  seq <=lex seq_rev  via the standard eq/lt reifier + prefix chain.

    For each position i build:
        eq[i] <-> (seq[i] == seq_rev[i])
        lt[i] <-> (seq[i] <  seq_rev[i])
    Then assert OR of   (lt[0])  OR  (eq[0] AND lt[1])  OR  (eq[0] AND eq[1] AND lt[2])  OR  ...
    plus the trailing prefix (all eq) so equal sequences are also allowed.
    """
    n = len(seq)
    if n == 0:
        return
    eq, lt = [], []
    for i in range(n):
        ei = instance.opt.NewBoolVar(f"{tag}_eq_{i}")
        li = instance.opt.NewBoolVar(f"{tag}_lt_{i}")
        instance.opt.Add(seq[i] == seq_rev[i]).OnlyEnforceIf(ei)
        instance.opt.Add(seq[i] != seq_rev[i]).OnlyEnforceIf(ei.Not())
        instance.opt.Add(seq[i] <  seq_rev[i]).OnlyEnforceIf(li)
        instance.opt.Add(seq[i] >= seq_rev[i]).OnlyEnforceIf(li.Not())
        eq.append(ei)
        lt.append(li)

    clause = [lt[0]]
    prefix = eq[0]
    for i in range(1, n):
        # ci = prefix AND lt[i]
        ci = instance.opt.NewBoolVar(f"{tag}_break_{i}")
        instance.opt.AddBoolAnd([prefix, lt[i]]).OnlyEnforceIf(ci)
        instance.opt.AddBoolOr([prefix.Not(), lt[i].Not()]).OnlyEnforceIf(ci.Not())
        clause.append(ci)
        # new_prefix = prefix AND eq[i]
        new_pref = instance.opt.NewBoolVar(f"{tag}_pref_{i}")
        instance.opt.AddBoolAnd([prefix, eq[i]]).OnlyEnforceIf(new_pref)
        instance.opt.AddBoolOr([prefix.Not(), eq[i].Not()]).OnlyEnforceIf(new_pref.Not())
        prefix = new_pref

    instance.opt.AddBoolOr(clause + [prefix])


def placement_site_flip_symmetry_breaking(instance):
    """
    Per-transistor flip symmetry break (QFET SH analog of ?FET's site-flip).

    For each transistor whose source and drain attach to the SAME net,
    flipping is a no-op for connectivity - fix flip_var = 0 (canonical).
    Conservative: never rules out valid solutions, just removes the redundant
    flipped twin from the search.

    Other flip symmetries (e.g. paired-identical transistors) need richer
    netlist analysis and are out of scope here.
    """
    instance.opt.log_comment("Enforcing per-transistor site-flip symmetry breaking")
    fixed = 0
    for tran in instance.circuit.transistors.values():
        if tran.source == tran.drain:
            tvar = instance.transistor_vars[tran.name]
            instance.opt.Add(tvar.flip_var == 0)
            fixed += 1
    logger.info(f"\t==\tSite-flip break: fixed flip=0 on {fixed} S==D transistor(s)")


# Diffusion-break-type -> column distance for adjacency. MDB unsupported today.
_DB_TYPE_TO_DIST = {"SDB": 2, "DDB": 4}


def _reify_z_eq(instance, tran_1, tran_2, tag):
    """
    Reify `z_eq = (z_var_1 == z_var_2)` for a transistor pair. Used by all the
    per-tier pairwise sharing constraints to gate adjacency / equality
    conditions on "same placement tier" (sharing across tiers is physically
    impossible in QFET's 4-tier stack).
    """
    z1 = instance.transistor_vars[tran_1.name].z_var
    z2 = instance.transistor_vars[tran_2.name].z_var
    z_eq = instance.opt.NewBoolVar(f"{tag}_z_eq_{tran_1.name}_{tran_2.name}")
    if z1 is None or z2 is None:
        # Single-tier (planar) techs (FinFET) leave z_var unset: every
        # transistor is on the one placement tier, so the pair is trivially
        # same-tier. Pin z_eq True so the per-tier sharing constraints fire
        # unconditionally (the tier-less case). Tier techs (QFET, z_var
        # populated) keep the reified == / != form below.
        instance.opt.Add(z_eq == 1)
        return z_eq
    instance.opt.Add(z1 == z2).OnlyEnforceIf(z_eq)
    instance.opt.Add(z1 != z2).OnlyEnforceIf(z_eq.Not())
    return z_eq


def pairwise_diffusion_sharing(instance):
    """
    Per-tier pairwise diffusion sharing.

    For each unordered SAME-MODEL transistor pair, find S/D nets they share.
    Diffusion sharing requires adjacent columns on the SAME placement tier
    (each tier is a distinct diffusion layer in QFET's 4-tier stack; sharing
    across tiers is physically impossible). All adjacency / flip constraints
    are gated on z_eq (same tier).

    When the pair shares no S/D net AND lands on the same tier, adjacency at
    db_dist is forbidden outright. When tiers differ, no constraint fires
    (adjacency is meaningless across tiers).

    db_dist comes from instance.tech.diffusion_break_type: SDB=2, DDB=4.
    """
    instance.opt.log_comment("Enforcing per-tier pairwise diffusion sharing")
    if instance.tech.diffusion_break_type == "MDB":
        raise NotImplementedError("Mixed Diffusion Break is not supported.")
    if instance.tech.diffusion_break_type not in _DB_TYPE_TO_DIST:
        raise ValueError(f"Unknown diffusion_break_type {instance.tech.diffusion_break_type!r}")
    db_dist = _DB_TYPE_TO_DIST[instance.tech.diffusion_break_type]

    instance.ds_pair_vars = {}
    instance.net_ds_sharable_pairs = {}
    tmp_tran = sorted(list(instance.circuit.transistors.values()))

    for i, tran_1 in enumerate(tmp_tran):
        x_var_1 = instance.transistor_vars[tran_1.name].x_var
        flip_var_1 = instance.transistor_vars[tran_1.name].flip_var
        for tran_2 in tmp_tran[i + 1 :]:
            x_var_2 = instance.transistor_vars[tran_2.name].x_var
            flip_var_2 = instance.transistor_vars[tran_2.name].flip_var
            # same mos type
            if tran_1.model != tran_2.model:
                continue

            # Per-tier guard: only meaningful when both land on the same tier.
            z_eq = _reify_z_eq(instance, tran_1, tran_2, tag="ds")

            # 1) Collect all nets that connect k1 and k2 (src/drn on either)
            shared_nets = [
                (net.name, net.connected_transistors)
                for net in instance.circuit.nets.values()
                if ((tran_1.name, "source") in net.connected_transistors or (tran_1.name, "drain") in net.connected_transistors)
                and ((tran_2.name, "source") in net.connected_transistors or (tran_2.name, "drain") in net.connected_transistors)
            ]

            # 1a) If no shared net at all, forbid adjacency on the SAME tier only:
            if not shared_nets:
                instance.opt.Add(x_var_1 != x_var_2 + db_dist).OnlyEnforceIf(z_eq)
                instance.opt.Add(x_var_2 != x_var_1 + db_dist).OnlyEnforceIf(z_eq)
                continue

            for shared_net in shared_nets:
                net_name = shared_net[0]
                instance.net_ds_sharable_pairs.setdefault(net_name, []).append((tran_1.name, tran_2.name))

            # 2) One BoolVar "sel" per shared net; pick at most one - and
            # only when same tier (cross-tier can't share diffusion at all).
            selectors = []
            for net, _ in shared_nets:
                sel = instance.opt.NewBoolVar(f"sel_{tran_1.name}_{tran_2.name}_{net}")
                selectors.append(sel)
            instance.opt.Add(sum(selectors) <= 1).OnlyEnforceIf(z_eq)
            instance.opt.Add(sum(selectors) == 0).OnlyEnforceIf(z_eq.Not())

            # 2a) If none selected AND same tier, forbid adjacency:
            none_selected = [sel.Not() for sel in selectors]
            instance.opt.Add(x_var_1 != x_var_2 + db_dist).OnlyEnforceIf(none_selected + [z_eq])
            instance.opt.Add(x_var_2 != x_var_1 + db_dist).OnlyEnforceIf(none_selected + [z_eq])

            # 3) For each net, gate its adjacency+flip logic on (sel AND z_eq)
            for (net, conn), sel in zip(shared_nets, selectors):
                # reuse or create the two adjacency reifiers
                keyL = f"ds_left_{tran_1.name}_{tran_2.name}_{net}"
                keyR = f"ds_right_{tran_1.name}_{tran_2.name}_{net}"
                adj_left = instance.ds_pair_vars.get(keyL, instance.opt.NewBoolVar(keyL))
                adj_right = instance.ds_pair_vars.get(keyR, instance.opt.NewBoolVar(keyR))
                instance.ds_pair_vars[keyL] = adj_left
                instance.ds_pair_vars[keyR] = adj_right

                # 3a) exactly one orientation if sel, none otherwise
                instance.opt.Add(adj_left + adj_right == 1).OnlyEnforceIf([sel, z_eq])
                instance.opt.Add(adj_left == 0).OnlyEnforceIf(sel.Not())
                instance.opt.Add(adj_right == 0).OnlyEnforceIf(sel.Not())

                # 3b) Now recover your four sharing-cases, *all* under sel:
                # 3b.1) source-source sharing
                if (tran_1.name, "source") in conn and (
                    tran_2.name,
                    "source",
                ) in conn:
                    # tran_1 immediately left of tran_2
                    instance.opt.Add(x_var_1 + db_dist == x_var_2).OnlyEnforceIf([adj_left, sel, z_eq])
                    instance.opt.Add(x_var_1 + db_dist != x_var_2).OnlyEnforceIf([adj_left.Not(), sel, z_eq])
                    # tran_1 is flipped and tran_2 is not
                    instance.opt.AddImplication(adj_left, flip_var_1).OnlyEnforceIf([sel, z_eq])
                    instance.opt.AddImplication(adj_left, flip_var_2.Not()).OnlyEnforceIf([sel, z_eq])

                    # tran_1 immediately right of tran_2
                    instance.opt.Add(x_var_1 == x_var_2 + db_dist).OnlyEnforceIf([adj_right, sel, z_eq])
                    instance.opt.Add(x_var_1 != x_var_2 + db_dist).OnlyEnforceIf([adj_right.Not(), sel, z_eq])
                    # tran_1 is not flipped and tran_2 is flipped
                    instance.opt.AddImplication(adj_right, flip_var_2).OnlyEnforceIf([sel, z_eq])
                    instance.opt.AddImplication(adj_right, flip_var_1.Not()).OnlyEnforceIf([sel, z_eq])
                # 3b.2) drain-drain sharing
                elif (tran_1.name, "drain") in conn and (
                    tran_2.name,
                    "drain",
                ) in conn:
                    # tran_1 immediately right of tran_2
                    instance.opt.Add(x_var_1 == x_var_2 + db_dist).OnlyEnforceIf([adj_right, sel, z_eq])
                    instance.opt.Add(x_var_1 != x_var_2 + db_dist).OnlyEnforceIf([adj_right.Not(), sel, z_eq])
                    # tran_1 is flipped and tran_2 is not flipped
                    instance.opt.AddImplication(adj_right, flip_var_1).OnlyEnforceIf([sel, z_eq])
                    instance.opt.AddImplication(adj_right, flip_var_2.Not()).OnlyEnforceIf([sel, z_eq])

                    # tran_1 immediately left of tran_2
                    instance.opt.Add(x_var_1 + db_dist == x_var_2).OnlyEnforceIf([adj_left, sel, z_eq])
                    instance.opt.Add(x_var_1 + db_dist != x_var_2).OnlyEnforceIf([adj_left.Not(), sel, z_eq])
                    # tran_1 is not flipped and tran_2 is flipped
                    instance.opt.AddImplication(adj_left, flip_var_2).OnlyEnforceIf([sel, z_eq])
                    instance.opt.AddImplication(adj_left, flip_var_1.Not()).OnlyEnforceIf([sel, z_eq])
                # 3b.3) source-drain sharing
                elif (tran_1.name, "source") in conn and (
                    tran_2.name,
                    "drain",
                ) in conn:
                    # tran_1 immediately left of tran_2
                    instance.opt.Add(x_var_1 + db_dist == x_var_2).OnlyEnforceIf([adj_left, sel, z_eq])
                    instance.opt.Add(x_var_1 + db_dist != x_var_2).OnlyEnforceIf([adj_left.Not(), sel, z_eq])
                    # tran_1 is flipped and tran_2 is flipped
                    instance.opt.AddImplication(adj_left, flip_var_1).OnlyEnforceIf([sel, z_eq])
                    instance.opt.AddImplication(adj_left, flip_var_2).OnlyEnforceIf([sel, z_eq])

                    # tran_1 immediately right of tran_2
                    instance.opt.Add(x_var_1 == x_var_2 + db_dist).OnlyEnforceIf([adj_right, sel, z_eq])
                    instance.opt.Add(x_var_1 != x_var_2 + db_dist).OnlyEnforceIf([adj_right.Not(), sel, z_eq])
                    # tran_1 is not flipped and tran_2 is not flipped
                    instance.opt.AddImplication(adj_right, flip_var_1.Not()).OnlyEnforceIf([sel, z_eq])
                    instance.opt.AddImplication(adj_right, flip_var_2.Not()).OnlyEnforceIf([sel, z_eq])
                # 3b.4) drain-source sharing
                elif (tran_1.name, "drain") in conn and (
                    tran_2.name,
                    "source",
                ) in conn:
                    # tran_1 immediately left of tran_2
                    instance.opt.Add(x_var_1 + db_dist == x_var_2).OnlyEnforceIf([adj_left, sel, z_eq])
                    instance.opt.Add(x_var_1 + db_dist != x_var_2).OnlyEnforceIf([adj_left.Not(), sel, z_eq])
                    # tran_1 is not flipped and tran_2 is not flipped
                    instance.opt.AddImplication(adj_left, flip_var_1.Not()).OnlyEnforceIf([sel, z_eq])
                    instance.opt.AddImplication(adj_left, flip_var_2.Not()).OnlyEnforceIf([sel, z_eq])

                    # tran_1 immediately right of tran_2
                    instance.opt.Add(x_var_1 == x_var_2 + db_dist).OnlyEnforceIf([adj_right, sel, z_eq])
                    instance.opt.Add(x_var_1 != x_var_2 + db_dist).OnlyEnforceIf([adj_right.Not(), sel, z_eq])
                    # tran_1 is flipped and tran_2 is flipped
                    instance.opt.AddImplication(adj_right, flip_var_1).OnlyEnforceIf([sel, z_eq])
                    instance.opt.AddImplication(adj_right, flip_var_2).OnlyEnforceIf([sel, z_eq])
    logger.info(f"\t==\t{len(instance.ds_pair_vars)} pairwise diffusion sharing variables created ...")

def pairwise_lisd_sharing(instance):
    """
    Per-tier pairwise LISD sharing via biconditional reification.

    For each cross-MOS (PMOS+NMOS) pair sharing one or more S/D nets, reify
    a lisd_var per shared net as the physical sharing condition:

        lisd_var <-> x_eq AND z_eq AND flip_condition

    where:
        x_eq            <-> x_var_1 == x_var_2
        z_eq            <-> z_var_1 == z_var_2   (PER-TIER GUARD)
        flip_condition  = flip_eq         when S-S or D-D sharing
                        = NOT flip_eq     when S-D or D-S (cross-terminal)
        flip_eq         <-> flip_var_1 == flip_var_2

    Per-tier: LISD is a tier-local interconnect - PMOS on tier 2 cannot
    share LISD with NMOS on tier 0. The z_eq conjunct enforces this.
    """
    instance.opt.log_comment("Enforcing per-tier pairwise lisd sharing")
    logger.info("\t==\tEnforcing per-tier pairwise lisd sharing")
    tmp_tran = sorted(list(instance.circuit.transistors.values()))
    instance.lisd_share_pair_vars = {}
    for i, tran_1 in enumerate(tmp_tran):
        x_var_1 = instance.transistor_vars[tran_1.name].x_var
        flip_var_1 = instance.transistor_vars[tran_1.name].flip_var
        for tran_2 in tmp_tran[i + 1 :]:
            x_var_2 = instance.transistor_vars[tran_2.name].x_var
            flip_var_2 = instance.transistor_vars[tran_2.name].flip_var
            # cross-MOS only
            if tran_1.model == tran_2.model:
                continue

            shared_nets = [
                (net.name, net.connected_transistors)
                for net in instance.circuit.get_nets(with_power_ground=False)
                if ((tran_1.name, "source") in net.connected_transistors or (tran_1.name, "drain") in net.connected_transistors)
                and ((tran_2.name, "source") in net.connected_transistors or (tran_2.name, "drain") in net.connected_transistors)
            ]
            if not shared_nets:
                continue

            # Per-pair reifiers (shared across all this pair's shared nets)
            x_eq = instance.opt.NewBoolVar(f"x_eq_{tran_1.name}_{tran_2.name}")
            instance.opt.Add(x_var_1 == x_var_2).OnlyEnforceIf(x_eq)
            instance.opt.Add(x_var_1 != x_var_2).OnlyEnforceIf(x_eq.Not())

            flip_eq = instance.opt.NewBoolVar(f"flip_eq_{tran_1.name}_{tran_2.name}")
            instance.opt.Add(flip_var_1 == flip_var_2).OnlyEnforceIf(flip_eq)
            instance.opt.Add(flip_var_1 != flip_var_2).OnlyEnforceIf(flip_eq.Not())

            z_eq = _reify_z_eq(instance, tran_1, tran_2, tag="lisd")

            for net, conn in shared_nets:
                key = f"lisd_share_{tran_1.name}_{tran_2.name}_{net}"
                lisd_var = instance.opt.NewBoolVar(key)
                instance.lisd_share_pair_vars[key] = lisd_var

                is_same_terminal = (
                    ((tran_1.name, "source") in conn and (tran_2.name, "source") in conn)
                    or ((tran_1.name, "drain") in conn and (tran_2.name, "drain") in conn)
                )
                # Build the flip literal for this terminal type.
                flip_lit = flip_eq if is_same_terminal else flip_eq.Not()
                # lisd_var <-> x_eq AND z_eq AND flip_lit
                instance.opt.AddBoolAnd([x_eq, z_eq, flip_lit]).OnlyEnforceIf(lisd_var)
                instance.opt.AddBoolOr([x_eq.Not(), z_eq.Not(), flip_lit.Not()]).OnlyEnforceIf(lisd_var.Not())
    logger.info(f"\t==\t{len(instance.lisd_share_pair_vars)} pairwise lisd sharing variables created")
    
def pairwise_gate_sharing(instance):
    """
    Per-tier pairwise gate sharing.

    For each cross-MOS pair sharing a gate net, reify a gate_var:

        gate_var <-> (x_var_1 == x_var_2) AND (z_var_1 == z_var_2)

    Per-tier: poly gate stripes in QFET are tier-local - each placement tier
    has its own gate layer. PMOS on tier 2 cannot share a gate stripe with
    NMOS on tier 0 via column alignment alone; the z_eq conjunct enforces
    the tier match.
    """
    instance.opt.log_comment("Enforcing per-tier pairwise gate sharing")
    logger.info("\t==\tEnforcing per-tier pairwise gate sharing")
    instance.gate_share_pair_vars = {}
    instance.net_gate_sharable_pairs = {}
    tmp_tran = sorted(list(instance.circuit.transistors.values()))
    for i, tran_1 in enumerate(tmp_tran):
        x_var_1 = instance.transistor_vars[tran_1.name].x_var
        for tran_2 in tmp_tran[i + 1 :]:
            x_var_2 = instance.transistor_vars[tran_2.name].x_var
            # cross-MOS only
            if tran_1.model == tran_2.model:
                continue

            # Reify per-pair x_eq + z_eq once; reuse across shared gate nets.
            shared_gate_nets = [
                net for net in instance.circuit.get_nets(with_power_ground=False)
                if (tran_1.name, "gate") in net.connected_transistors
                and (tran_2.name, "gate") in net.connected_transistors
            ]
            if not shared_gate_nets:
                continue

            x_eq = instance.opt.NewBoolVar(f"gs_x_eq_{tran_1.name}_{tran_2.name}")
            instance.opt.Add(x_var_1 == x_var_2).OnlyEnforceIf(x_eq)
            instance.opt.Add(x_var_1 != x_var_2).OnlyEnforceIf(x_eq.Not())
            z_eq = _reify_z_eq(instance, tran_1, tran_2, tag="gs")

            for net in shared_gate_nets:
                instance.net_gate_sharable_pairs.setdefault(net.name, []).append((tran_1.name, tran_2.name))
                key = f"gate_share_{tran_1.name}_{tran_2.name}_{net.name}"
                gate_var = instance.opt.NewBoolVar(key)
                instance.gate_share_pair_vars[key] = gate_var
                # gate_var <-> x_eq AND z_eq
                instance.opt.AddBoolAnd([x_eq, z_eq]).OnlyEnforceIf(gate_var)
                instance.opt.AddBoolOr([x_eq.Not(), z_eq.Not()]).OnlyEnforceIf(gate_var.Not())
    logger.info(f"\t==\t{len(instance.gate_share_pair_vars)} pairwise gate sharing variables created")


def net_span_from_placement(instance, use_span_limit=False):
    """
    Enforce net spanning from placement.

    Args:
        instance: The instance instance
        use_span_limit: Whether to enforce span limit (default False)
    """
    instance.opt.log_comment(f"Enforcing net spanning...")
    # TODO This is a pre-mature implementation for placement only flow
    logger.info(f"\t==\tEnforcing net spanning ...")
    instance.net_span_min_vars = {}
    instance.net_span_max_vars = {}
    for net in instance.circuit.get_nets(with_power_ground=False):
        conn = net.connected_transistors
        tmp_net_x_vars = []
        for tran_name, p in conn:
            tvar = instance.transistor_vars[tran_name]
            tmp_net_x_vars.append(tvar.x_var)
        # min and max x vars
        net_span_min_var = instance.opt.NewIntVarFromDomain(
            instance.domain_pc_ci,
            f"{net.name}_min",
        )
        net_span_min_var = instance.opt.NewIntVar(0, instance.lgg.num_cols_in_layer("PC"), f"{net.name}_net_span_min")
        net_span_max_var = instance.opt.NewIntVar(0, instance.lgg.num_cols_in_layer("PC"), f"{net.name}_net_span_max")
        instance.net_span_min_vars[net.name] = net_span_min_var
        instance.net_span_max_vars[net.name] = net_span_max_var
        instance.opt.AddMinEquality(
            net_span_min_var,
            tmp_net_x_vars,
        )
        instance.opt.AddMaxEquality(
            net_span_max_var,
            tmp_net_x_vars,
        )
    # conditional constrain net spanning
    # NOTE: do not use this
    if use_span_limit:
        logger.info(f"\t==\tEnforcing net spanning limit ...")
        instance.opt.log_comment(f"Enforcing net spanning limit ...")
        # enforce that the net spanning is within the limit
        for net in instance.circuit.get_nets(with_power_ground=False):
            net_degree = len(net.connected_transistors)
            # enforce that the net spanning is within the limit
            span_limit = int(net_degree**2)
            instance.opt.Add((instance.net_span_max_vars[net.name] - instance.net_span_min_vars[net.name]) <= span_limit)
    logger.info(f"\tEnd of placement constraints ...")


def ban_other_nets_from_using_nodes(instance, net_to_skip, nodes, cond, debug_mode=False):
    """
    Ban other nets from using specified nodes.

    Args:
        instance: The instance instance
        net_to_skip: The net name to skip
        nodes: List of nodes to protect
        cond: Condition for enforcement
        debug_mode: Enable debug logging (default False)
    """
    # Collect the unique set of arcs touching any protected node via adjacency index
    node_set = set(nodes)
    touching_arcs = set()
    for node in node_set:
        for arc in instance.adj_out.get(node, ()):
            touching_arcs.add(arc)
        for arc in instance.adj_in.get(node, ()):
            touching_arcs.add(arc)
    # Pre-collect the other nets once
    other_nets = [net for net in instance.circuit.get_nets(with_power_ground=False) if net.name != net_to_skip]
    if not other_nets:
        return
    # For each arc, create ONE aggregated sum constraint instead of per-net ones.
    # Flow bans are omitted: link_flow_to_arc (flow -> arc) ensures flow=0 when arc=0.
    for u_arc, v_arc in touching_arcs:
        other_arc_vars = [instance.net_arc_vars[(net.name, u_arc, v_arc)] for net in other_nets]
        (logger.info(f"\t\t{net_to_skip} banning {len(other_nets)} nets from ({u_arc}, {v_arc}) if {cond}") if debug_mode else None)
        instance.opt.Add(sum(other_arc_vars) == 0).OnlyEnforceIf(cond)


# Per-pin / per-flip -> which col offset (relative to ci) the pin sits at.
# Source: flip -> col_rr (ci+2); noflip -> col (ci+0)
# Drain : flip -> col (ci+0);   noflip -> col_rr (ci+2)
_PWR_PIN_COL_OFFSET = {
    ("source", True):  2,
    ("source", False): 0,
    ("drain",  True):  0,
    ("drain",  False): 2,
}


def ban_other_nets_on_pwr_columns(instance):
    """
    Per-tier: ban any via on placement-tier slot (ci, zi) at the col where a
    power/ground-connected source/drain sits.

    For every power/ground net's (transistor, pin) connection (excluding gate
    pins, which have no S/D contact to constrain), iterates every slot the
    transistor could occupy. Under [placed_at_slot, flip_state] the via-edge
    vars at the source/drain col (which one depends on flip + pin) are forced
    to zero.

    Per-tier scope (was per-col): the constraint now targets the SPECIFIC tier
    the transistor lands on - not all tiers at once.

    Table-driven flip/pin dispatch via _PWR_PIN_COL_OFFSET (was 8 hand-written
    branches x bug-prone OnlyEnforceIf signatures).
    """
    instance.opt.log_comment("Enforcing no other net on power-ground source/drain columns")
    for net in instance.circuit.get_power_ground_nets():
        logger.info(f"Net: {net.name} Connected Transistors: {net.connected_transistors}")
        for tran_name, tran_pin in net.connected_transistors:
            if tran_pin == "gate":
                continue  # gate<->power has no S/D contact to constrain
            tran = instance.circuit.transistors[tran_name]
            tvar = instance.transistor_vars[tran_name]
            for zi in instance.plc_zi:
                layer_name = instance.lgg.idx_to_layer[zi]
                for ci in instance.plc_ci:
                    placed_here = instance.placed_tran_at_xzi_vars[(tran_name, ci, zi)]
                    for is_flipped in (True, False):
                        offset = _PWR_PIN_COL_OFFSET[(tran_pin, is_flipped)]
                        target_col = instance.lgg.col_in_layer(layer_name, ci + offset)
                        via_vars = instance._gather_via_vars_in_region(zi, target_col, tran.model)
                        if not via_vars:
                            continue
                        flip_cond = tvar.flip_var if is_flipped else tvar.flip_var.Not()
                        instance.opt.Add(sum(via_vars) == 0).OnlyEnforceIf([placed_here, flip_cond])


def prohibit_CA_contact_on_non_source_term_columns(instance):
    """
    Per-tier: at every S/D col, a CA contact (via) is only valid if at least
    one source/terminal of some net lands in the same (tier, col) region.

    For each S/D col `ci`, for each tier `zi`, for each model (PMOS/NMOS):
      - Collect src/term bool vars at the (zi, col) region for that model.
      - For each via edge_var at the same region:
            via_var == 1  ->  OR(src_term_vars)  (i.e. CA needs a S/D pin)

    Per-tier scope means the constraint distinguishes which placement tier the
    via lands on - was hardcoded "PC" before.
    """
    instance.opt.log_comment("Prohibiting CA contacts at non-source-term cols (per tier)")
    for ci in instance.sd_ci:
        for zi in instance.plc_zi:
            layer_name = instance.lgg.idx_to_layer[zi]
            col = instance.lgg.col_in_layer(layer_name, ci)
            for model in (Model.PMOS, Model.NMOS):
                src_term_vars = instance._gather_src_term_vars_in_region(zi, col, model)
                via_vars = instance._gather_via_vars_in_region(zi, col, model)
                if not (src_term_vars and via_vars):
                    continue
                for via_var in via_vars:
                    instance.opt.AddBoolOr(src_term_vars).OnlyEnforceIf(via_var)
