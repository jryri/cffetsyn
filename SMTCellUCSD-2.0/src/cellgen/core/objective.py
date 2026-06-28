"""
Objective functions for FinFET circuit layout optimization.
This module contains all objective function implementations used by the CP-SAT solver.
"""

from loguru import logger


class Objective:
    """
    Container for objective function implementations.
    Each method takes a FinFET instance and computes a specific objective.
    """

    @staticmethod
    def cpp(finfet):
        """
        Objective: Minimize the cell placement pitch (CPP).
        Returns the maximum column position among all transistors.
        """
        return finfet.cpp_cost

    @staticmethod
    def weighted_wirelength(finfet):
        """
        Objective: Minimize weighted wirelength.
        Computes sum of edge_var * wirelength for all edges.
        """
        weighted_wirelength = 0
        for (u_edge, v_edge), edge_var in finfet.edge_vars.items():
            wire_cost = finfet.edge_to_cost[(u_edge, v_edge)]
            weighted_wirelength += edge_var * wire_cost
        return weighted_wirelength

    @staticmethod
    def via_count(finfet):
        """
        Objective: Minimize via count.
        Counts the number of edges that connect different layers.
        """
        via_count = 0
        for (u_edge, v_edge), edge_var in finfet.edge_vars.items():
            if u_edge[0] != v_edge[0]:
                via_count += edge_var
        return via_count

    @staticmethod
    def weighted_via_count(finfet):
        """
        Objective: Minimize weighted via count.
        Vias on higher layers are weighted more heavily.
        """
        weighted_via_count = 0
        for (u_edge, v_edge), edge_var in finfet.edge_vars.items():
            if u_edge[0] != v_edge[0]:
                via_cost = edge_var * max(u_edge[0], v_edge[0])
                weighted_via_count += via_cost
        return weighted_via_count

    @staticmethod
    def pin_gap(finfet):
        """
        Objective: Minimize pin gap.
        Computes weighted sum of pin gaps based on minimum gap constraints.
        """
        nominal_pin_gap = 0
        for min_gap in finfet.min_gap_to_pin_placement_case_vars:
            for pin_col_net_var in finfet.min_gap_to_pin_placement_case_vars[min_gap]:
                nominal_pin_gap += min_gap * pin_col_net_var
        return nominal_pin_gap

    @staticmethod
    def wire_segments(finfet):
        """
        Objective: Minimize wire segments.
        Counts geometric segments in all directions (left, right, front, back).
        """
        wire_segments = 0
        for key, var in finfet.geometric_vars.items():
            if "left" in var:
                wire_segments += var["left"]
            if "right" in var:
                wire_segments += var["right"]
            if "front" in var:
                wire_segments += var["front"]
            if "back" in var:
                wire_segments += var["back"]
        return wire_segments

    @staticmethod
    def weighted_net_span(finfet):
        """
        Objective: Minimize weighted net span.
        Computes net degree weighted span for each net.
        """
        net_span = 0
        for net in finfet.circuit.get_nets(with_power_ground=False):
            net_degree = len(net.connected_transistors)
            net_span += net_degree * (finfet.net_span_max_vars[net.name] - finfet.net_span_min_vars[net.name])
        return net_span

    @staticmethod
    def fdm_penalty(finfet):
        """Minimize field-drain merge count (+1 CPP each in the WSUM table)."""
        if not getattr(finfet, "fdm_pair_vars", None):
            return 0
        return sum(finfet.fdm_pair_vars.values())

    @staticmethod
    def gate_sharing(finfet):
        """
        Objective: Maximize gate sharing.
        Counts the number of gate sharing instances between transistor pairs.
        """
        if not hasattr(finfet, "gate_share_pair_vars"):
            logger.error("Gate sharing variables not defined in the model.")
            return 0
        return sum(finfet.gate_share_pair_vars.values())

    @staticmethod
    def lisd_sharing(finfet):
        """
        Objective: Maximize LISD (Local Interconnect Source/Drain) sharing.
        Counts the number of LISD sharing instances between transistor pairs.
        """
        if not hasattr(finfet, "lisd_share_pair_vars"):
            logger.error("LISD sharing variables not defined in the model.")
            return 0
        return sum(finfet.lisd_share_pair_vars.values())

    @staticmethod
    def db_placement(finfet):
        """
        Objective: Encourage diffusion break placement on the right.
        Higher column indices are weighted more heavily.

        QFET z-aware: sum per-slot DB vars (db_pmos_vars[(ci, zi)]) weighted by
        ci. Each tier contributes independently - matches upstream single-tier
        semantics extended to the z axis. Per-col AND-aggregation would reward
        clustering all transistors in one col (since "all-tier-empty" requires
        every tier empty), and BUF would land at x=3 instead of split x=1/x=3.
        Falls back to legacy db_*_cols_vars when per-slot dicts are absent.
        """
        db_placement = 0
        per_slot_pmos = getattr(finfet, "db_pmos_vars", None)
        per_slot_nmos = getattr(finfet, "db_nmos_vars", None)
        if per_slot_pmos and per_slot_nmos:
            for (ci, _zi), db_var in per_slot_pmos.items():
                db_placement += db_var * ci
            for (ci, _zi), db_var in per_slot_nmos.items():
                db_placement += db_var * ci
            return db_placement
        for ci, db_var in finfet.db_pmos_cols_vars.items():
            db_placement += db_var * ci
        for ci, db_var in finfet.db_nmos_cols_vars.items():
            db_placement += db_var * ci
        return db_placement

    @staticmethod
    def output_pin_placement(finfet):
        """
        Objective: Encourage output pins to be placed on the right.
        DEPRECATED: This objective is no longer recommended.
        """
        raise DeprecationWarning("obj_output_pin_placement is deprecated.")
        # encourage output pins to be put on the right
        output_pin_x = []
        for netname, val in finfet.node_is_SON_vars.items():
            if netname not in finfet.circuit.output_pins:
                continue
            for k, nodes in val.items():
                for node_key, node_val in nodes.items():
                    output_pin_x.append(node_key[2] * node_val)
        return sum(output_pin_x)

    @staticmethod
    def pin_2_pin_distance(finfet):
        """
        Objective: Maximize pin-to-pin separation (Pin Separation - PS).
        Uses auxiliary variables d_x and d_y constrained by actual distances.
        This prevents the solver from gaming the objective by selecting suboptimal pin positions.
        Based on the paper formulation with d_ij^x <= |c_pi - c_pj| and d_ij^y <= |r_pi - r_pj|.
        
        IMPORTANT: Only considers SONs that have actual via access (net arcs connecting to different layers).
        """
        pin_separation_terms = []

        # First, create auxiliary variables for "SON has via access"
        son_has_via_access = {}
        for netname, val_map in finfet.node_is_SON_vars.items():
            for k_idx, nodes_map in val_map.items():
                for node_key, node_var in nodes_map.items():
                    # Gather all via arcs from this SON node (arcs connecting to different layers)
                    via_arcs_from_son = []
                    for (net_arc_name, u_arc, v_arc), arc_var in finfet.net_arc_vars.items():
                        if net_arc_name != netname:
                            continue
                        # Check if this arc involves the SON node and is a via (different layers)
                        if (u_arc == node_key or v_arc == node_key) and u_arc[0] != v_arc[0]:
                            via_arcs_from_son.append(arc_var)
                    
                    # Create auxiliary variable: SON has via access if at least one via arc is used
                    if via_arcs_from_son:
                        via_access_var_name = f"son_via_access_{netname}_k{k_idx}_L{node_key[0]}R{node_key[1]}C{node_key[2]}"
                        via_access_var = finfet.opt.NewBoolVar(via_access_var_name)
                        son_has_via_access[(netname, k_idx, node_key)] = via_access_var
                        
                        # via_access_var = 1 iff at least one via arc is used
                        finfet.opt.Add(sum(via_arcs_from_son) >= 1).OnlyEnforceIf(via_access_var)
                        finfet.opt.Add(sum(via_arcs_from_son) == 0).OnlyEnforceIf(via_access_var.Not())
                    else:
                        # No via arcs possible from this SON, so via access is always false
                        son_has_via_access[(netname, k_idx, node_key)] = finfet.opt.NewConstant(0)

        # Collect all (netname, k_idx, node_key, node_variable, via_access_var) for SONs
        all_son_tuples = []
        for netname, val_map in finfet.node_is_SON_vars.items():
            for k_idx, nodes_map in val_map.items():
                for node_key, node_var in nodes_map.items():
                    via_var = son_has_via_access.get((netname, k_idx, node_key), finfet.opt.NewConstant(0))
                    all_son_tuples.append((netname, k_idx, node_key, node_var, via_var))

        num_all_sons = len(all_son_tuples)
        for i in range(num_all_sons):
            netname1, k1, node_key1, node_val1_var, via_access1_var = all_son_tuples[i]

            for j in range(i + 1, num_all_sons):
                netname2, k2, node_key2, node_val2_var, via_access2_var = all_son_tuples[j]

                # Consider distance between pins of DIFFERENT nets
                if netname1 == netname2:
                    continue

                # Create auxiliary integer variables for horizontal and vertical distances
                s1_repr = f"{netname1}_k{k1}_L{node_key1[0]}R{node_key1[1]}C{node_key1[2]}"
                s2_repr = f"{netname2}_k{k2}_L{node_key2[0]}R{node_key2[1]}C{node_key2[2]}"
                
                d_x_name = f"d_x_{s1_repr}_vs_{s2_repr}"
                d_y_name = f"d_y_{s1_repr}_vs_{s2_repr}"
                
                # Create auxiliary nonnegative integer variables
                max_cols = finfet.num_cols if hasattr(finfet, 'num_cols') else 100
                max_rows = finfet.num_rows if hasattr(finfet, 'num_rows') else 100
                
                d_x = finfet.opt.NewIntVar(0, max_cols, d_x_name)
                d_y = finfet.opt.NewIntVar(0, max_rows, d_y_name)
                
                # Calculate actual distance components normalized by pitch
                # Horizontal distance in terms of PC pitch units
                # Vertical distance in terms of M0 pitch units
                pc_pitch = finfet.tech.get_pitch("PC")
                m0_pitch = finfet.tech.get_pitch("M0")
                dist_col = int(abs(node_key1[2] - node_key2[2]) // pc_pitch)  # node_key[2] is column
                dist_row = int(abs(node_key1[1] - node_key2[1]) // m0_pitch)  # node_key[1] is row
                
                # Create AND variable: both SONs are selected AND both have via access
                and_var_name = f"and_pin_sep_{s1_repr}_vs_{s2_repr}"
                and_var = finfet.opt.NewBoolVar(and_var_name)
                finfet.opt.AddMultiplicationEquality(and_var, [node_val1_var, node_val2_var, via_access1_var, via_access2_var])
                
                # Add constraints: d_x <= dist_col and d_y <= dist_row when both pins are selected with via access
                # When and_var = 0, d_x and d_y should be 0
                # When and_var = 1, d_x <= dist_col and d_y <= dist_row
                finfet.opt.Add(d_x <= dist_col * and_var)
                finfet.opt.Add(d_y <= dist_row * and_var)
                
                # Add the auxiliary variables to the objective (to be maximized)
                pin_separation_terms.append(d_x + d_y)

        if not pin_separation_terms:
            return finfet.opt.NewConstant(0)

        return sum(pin_separation_terms)

    @staticmethod
    def cffet_npvp_utilization(finfet):
        """
        Maximize CFFET NPNP tier usage (FFET-inspired dual-block spread).

        Requires ``tier_occupied_vars`` / ``npvp_spread_vars`` from
        ``CFFET.tier_utilization.init_npvp_utilization_vars``.
        """
        score = 0
        for var in getattr(finfet, "tier_occupied_vars", {}).values():
            score += var
        for var in getattr(finfet, "npvp_spread_vars", {}).values():
            score += var
        return score

    @staticmethod
    def cffet_npvp_block_imbalance(finfet):
        """Minimize |back-block devices − front-block devices|."""
        var = getattr(finfet, "npvp_block_imbalance_var", None)
        if var is None:
            return 0
        return var

    @staticmethod
    def top_layer_usage(finfet):
        """
        Objective: Minimize top layer usage.
        Counts the number of nets using top layer tracks.
        """
        if not hasattr(finfet, "net_use_top_track"):
            logger.warning("net_use_top_track is not defined. Returning 0 for top layer usage.")
            return 0
        top_layer_usage = 0
        for netname, val in finfet.net_use_top_track.items():
            top_layer_usage += val
        return top_layer_usage
    
    @staticmethod
    def m2_usage(finfet):
        if not hasattr(finfet, "m2_rows_to_used"):
            logger.warning("m2_rows_to_used is not defined. Returning 0 for M2 layer usage.")
            return 0
        return sum(finfet.m2_rows_to_used.values())
    
    @staticmethod
    def m1_usage(finfet):
        if not hasattr(finfet, "m1_cols_to_used"):
            logger.warning("m1_cols_to_used is not defined. Returning 0 for M1 layer usage.")
            return 0
        return sum(finfet.m1_cols_to_used.values())
    
    @staticmethod
    def m0_usage(finfet):
        if not hasattr(finfet, "m0_rows_to_used"):
            logger.warning("m0_rows_to_used is not defined. Returning 0 for M0 layer usage.")
            return 0
        return sum(finfet.m0_rows_to_used.values())
