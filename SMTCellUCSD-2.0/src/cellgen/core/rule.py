"""
Design Rule Checking functions for FinFET layout.

This module contains functions for enforcing various design rules including:
- End-of-Line (EOL) rules
- Minimum Area Rule (MAR)
- Via separation rules
- Via-to-metal connection rules
"""

from loguru import logger


def eol_rules_in_horizontal_layers(instance, eol_params):
    """
    Enforce EOL (End-of-Line) design rule checking for horizontal layers.

    Args:
        instance: The FinFET instance containing the opt, lgg, and geometric_vars
        eol_params: Dictionary mapping layer names to EOL distance parameters
    """
    DEBUG_EOL = False
    instance.opt.log_comment(f"Enforcing EOL design rule checking for horizontal layers ...")
    # Horizontal layers
    for layer, idx in instance.lgg.layer_to_idx.items():
        if layer in ("PC", "BPC"):
            continue  # skip placement layers
        if instance.lgg.layer_to_direction[layer] != "H":
            continue
        if layer not in eol_params:
            continue  # skip layers without configured EOL params
        for row in instance.lgg.rows_in_layer(layer):
            for col in instance.lgg.cols_in_layer(layer):
                # ^ --- 8.1) From right to left
                u = (idx, row, col)
                gvr_u = instance.geometric_vars[u]["right"]
                # iterate util the given parameter
                eol_dist = eol_params[layer]
                walked_dist = 0
                eol_list = []
                eol_list.append(gvr_u)
                curr_u = u
                logger.info(f"Node: {u} EOL dist: {eol_dist}") if DEBUG_EOL else None
                while walked_dist < eol_dist:
                    # check if the right neighbor exists
                    u_r = instance.lgg.get_right_neighbor(curr_u)
                    if u_r is None:
                        break
                    gvl_u_r = instance.geometric_vars[u_r]["left"]
                    # extract current col
                    curr_col = u_r[2]
                    walked_dist = abs(curr_col - col)
                    if walked_dist > eol_dist:
                        # if the distance is greater than the eol distance, then we need to break
                        break
                    # add the constraints
                    eol_list.append(gvl_u_r)
                    logger.info(f"\t EOL Right-to-Left Banning {gvl_u_r} EOL, walked_dist: {walked_dist}") if DEBUG_EOL else None
                    # update the current node
                    curr_u = u_r
                # add the eol constraints
                if len(eol_list) > 1:
                    # if the list is empty, then there is no need to add the constraints
                    instance.opt.AddAtMostOne(eol_list)
                # ^ --- 8.2) From left to right
                u = (idx, row, col)
                gvl_u = instance.geometric_vars[u]["left"]
                # iterate util the given parameter
                eol_dist = eol_params[layer]
                walked_dist = 0
                eol_list = []
                eol_list.append(gvl_u)
                curr_u = u 
                while walked_dist < eol_dist:
                    # check if the left neighbor exists
                    u_l = instance.lgg.get_left_neighbor(curr_u)
                    if u_l is None:
                        break
                    gvr_u_l = instance.geometric_vars[u_l]["right"]
                    # extract current col
                    curr_col = u_l[2]
                    walked_dist = abs(curr_col - col)
                    if walked_dist > eol_dist:
                        # if the distance is greater than the eol distance, then we need to break
                        break
                    # add the constraints
                    eol_list.append(gvr_u_l)
                    logger.info(f"\t EOL Left-to-Right Banning {gvr_u_l} EOL, walked_dist: {walked_dist}") if DEBUG_EOL else None
                    # update the current node
                    curr_u = u_l
                # add the eol constraints
                if len(eol_list) > 1:
                    # if the list is empty, then there is no need to add the constraints
                    instance.opt.AddAtMostOne(eol_list)


def eol_rules_in_vertical_layers(instance, eol_params):
    """
    Enforce EOL (End-of-Line) design rule checking for vertical layers.

    Args:
        instance: The FinFET instance containing the opt, lgg, and geometric_vars
        eol_params: Dictionary mapping layer names to EOL distance parameters
    """
    DEBUG_EOL = False
    instance.opt.log_comment(f"Enforcing EOL design rule checking for vertical layers ...")
    # Vertical layers
    for layer, idx in instance.lgg.layer_to_idx.items():
        if layer in ("PC", "BPC"):
            continue  # skip placement layers
        if instance.lgg.layer_to_direction[layer] != "V":
            continue
        if layer not in eol_params:
            continue  # skip layers without configured EOL params
        for row in instance.lgg.rows_in_layer(layer):
            for col in instance.lgg.cols_in_layer(layer):
                # ^ --- 8.3) From front to back
                u = (idx, row, col)
                gvb_u = instance.geometric_vars[u]["back"]
                # iterate util the given parameter
                eol_dist = eol_params[layer]
                walked_dist = 0
                eol_list = []
                eol_list.append(gvb_u)
                curr_u = u
                logger.info(f"Node: {u} EOL dist: {eol_dist}") if DEBUG_EOL else None
                while walked_dist < eol_dist:
                    # check if the back neighbor exists
                    u_b = instance.lgg.get_back_neighbor(curr_u)
                    if u_b is None:
                        break
                    gvf_u_b = instance.geometric_vars[u_b]["front"]
                    # extract current col
                    curr_row = u_b[1]
                    walked_dist = abs(curr_row - row)
                    if walked_dist > eol_dist:
                        # if the distance is greater than the eol distance, then we need to break
                        break
                    # add the constraints
                    eol_list.append(gvf_u_b)
                    logger.info(f"\t EOL Front-to-Back Banning {gvf_u_b} EOL, walked_dist: {walked_dist}") if DEBUG_EOL else None
                    # update the current node
                    curr_u = u_b
                # add the eol constraints
                if len(eol_list) > 1:
                    # if the list is empty, then there is no need to add the constraints
                    instance.opt.AddAtMostOne(eol_list)
                # ^ --- 8.4) From back to front
                u = (idx, row, col)
                logger.info(f"Node: {u} EOL dist: {eol_dist}") if DEBUG_EOL else None
                gvf_u = instance.geometric_vars[u]["front"]
                # iterate util the given parameter
                eol_dist = eol_params[layer]
                walked_dist = 0
                eol_list = []
                eol_list.append(gvf_u)
                curr_u = u
                while walked_dist < eol_dist:
                    # check if the front neighbor exists
                    u_f = instance.lgg.get_front_neighbor(curr_u)
                    if u_f is None:
                        break
                    gvb_u_f = instance.geometric_vars[u_f]["back"]
                    # extract current row
                    curr_row = u_f[1]
                    walked_dist = abs(curr_row - row)
                    if walked_dist > eol_dist:
                        # if the distance is greater than the eol distance, then we need to break
                        break
                    # add the constraints
                    eol_list.append(gvb_u_f)
                    logger.info(f"\t EOL Back-to-Front Banning {gvb_u_f} EOL, walked_dist: {walked_dist}") if DEBUG_EOL else None
                    # update the current node
                    curr_u = u_f
                # add the eol constraints
                if len(eol_list) > 1:
                    # if the list is empty, then there is no need to add the constraints
                    instance.opt.AddAtMostOne(eol_list)


def mar_rules_in_horizontal_layers(instance, mar_params, supervia_params):
    """
    Enforce MAR (Minimum Area Rule) design rule checking for horizontal layers.

    Args:
        instance: The FinFET instance containing the opt, lgg, and geometric_vars
        mar_params: Dictionary mapping layer names to MAR distance parameters
        supervia_params: Dictionary indicating which layers are supervias
    """
    DEBUG_MAR = False
    instance.opt.log_comment(f"Enforcing MAR design rule checking for horizontal layers ...")
    for layer, idx in instance.lgg.layer_to_idx.items():
        if layer in ("PC", "BPC"):
            continue  # skip placement layers
        if instance.lgg.layer_to_direction[layer] != "H":
            continue
        if layer not in mar_params:
            continue  # skip layers without configured MAR params
        if supervia_params.get(layer, False):
            # if the layer is a supervia, then we need to skip this layer
            continue
        for row in instance.lgg.rows_in_layer(layer):
            for col in instance.lgg.cols_in_layer(layer):
                # ^ --- 9.1) From right to left
                u = (idx, row, col)
                gvr_u = instance.geometric_vars[u]["right"]
                gvl_u = instance.geometric_vars[u]["left"]
                # iterate util the given parameter
                mar_dist = mar_params[layer]
                walked_dist = 0
                mar_list = []
                mar_list.append(gvr_u)
                mar_list.append(gvl_u)
                curr_u = u
                logger.info(f"Node: {u} MAR dist: {mar_dist}") if DEBUG_MAR else None
                while walked_dist < mar_dist:
                    # check if the right neighbor exists
                    u_l = instance.lgg.get_left_neighbor(curr_u)
                    if u_l is None:
                        break
                    gvl_u_l = instance.geometric_vars[u_l]["left"]
                    # gvr_u_r = instance.geometric_vars[u_r]["right"]
                    # extract current col
                    curr_col = u_l[2]
                    walked_dist = abs(curr_col - col)
                    if walked_dist > mar_dist:
                        # if the distance is greater than the mar distance, then we need to break
                        break
                    # add the constraints
                    mar_list.append(gvl_u_l)
                    # mar_list.append(gvr_u_r) # BUG: why add this
                    logger.info(f"\t MAR Right-to-Left Banning {gvl_u_l} MAR, walked_dist: {walked_dist}") if DEBUG_MAR else None
                    # update the current node
                    curr_u = u_l
                # add the mar constraints
                instance.opt.AddAtMostOne(mar_list)
                # ^ --- 9.1) From left to right
                u = (idx, row, col)
                gvr_u = instance.geometric_vars[u]["right"]
                gvl_u = instance.geometric_vars[u]["left"]
                # iterate util the given parameter
                mar_dist = mar_params[layer]
                walked_dist = 0
                mar_list = []
                mar_list.append(gvl_u)
                mar_list.append(gvr_u)
                curr_u = u
                logger.info(f"Node: {u} MAR dist: {mar_dist}") if DEBUG_MAR else None
                while walked_dist < mar_dist:
                    # check if the right neighbor exists
                    u_r = instance.lgg.get_right_neighbor(curr_u)
                    if u_r is None:
                        break
                    gvr_u_r = instance.geometric_vars[u_r]["right"]
                    # gvr_u_r = instance.geometric_vars[u_r]["right"]
                    # extract current col
                    curr_col = u_r[2]
                    walked_dist = abs(curr_col - col)
                    if walked_dist > mar_dist:
                        # if the distance is greater than the mar distance, then we need to break
                        break
                    # add the constraints
                    mar_list.append(gvr_u_r)
                    # mar_list.append(gvr_u_r) # BUG: why add this
                    logger.info(f"\t MAR Left-to-Right Banning {gvr_u_r} MAR, walked_dist: {walked_dist}") if DEBUG_MAR else None
                    # update the current node
                    curr_u = u_r
                # add the mar constraints
                instance.opt.AddAtMostOne(mar_list)


def mar_rules_in_vertical_layers(instance, mar_params, supervia_params):
    """
    Enforce MAR (Minimum Area Rule) design rule checking for vertical layers.

    Args:
        instance: The FinFET instance containing the opt, lgg, and geometric_vars
        mar_params: Dictionary mapping layer names to MAR distance parameters
        supervia_params: Dictionary indicating which layers are supervias
    """
    DEBUG_MAR = False
    instance.opt.log_comment("Enforcing MAR design rule checking for vertical layers ...")
    for layer, idx in instance.lgg.layer_to_idx.items():
        # no MAR on first layer
        if layer in ("PC", "BPC"):
            continue  # skip placement layers
        # only vertical layers
        if instance.lgg.layer_to_direction[layer] != "V":
            continue
        if layer not in mar_params:
            continue  # skip layers without configured MAR params
        # skip supervia layers entirely
        if supervia_params.get(layer, False):
            continue
        mar_dist = mar_params[layer]
        for row in instance.lgg.rows_in_layer(layer):
            for col in instance.lgg.cols_in_layer(layer):
                # ^ Front to Back
                u = (idx, row, col)
                gvf_u = instance.geometric_vars[u]["front"]
                gvb_u = instance.geometric_vars[u]["back"]
                # iterate util the given parameter
                mar_dist = mar_params[layer]
                walked_dist = 0
                mar_list = []
                mar_list.append(gvf_u)
                mar_list.append(gvb_u)
                curr_u = u
                logger.info(f"Node: {u} MAR dist: {mar_dist}") if DEBUG_MAR else None
                while walked_dist < mar_dist:
                    # check if the right neighbor exists
                    u_b = instance.lgg.get_back_neighbor(curr_u)
                    if u_b is None:
                        break
                    # NOTE: if u_b is the last row, dont restrict it.
                    if u_b[1] == instance.lgg.rows_in_layer(layer)[-1]:
                        logger.info(f"MAR on {layer} exceeding max row. Ignoring last row.") if DEBUG_MAR else None
                        break
                    # gvf_u_b = instance.geometric_vars[u_b]["front"]
                    gvb_u_b = instance.geometric_vars[u_b]["back"]
                    # extract current col
                    curr_row = u_b[1]
                    walked_dist = abs(curr_row - row)
                    if walked_dist > mar_dist:
                        # if the distance is greater than the mar distance, then we need to break
                        break
                    # add the constraints
                    # mar_list.append(gvf_u_b)
                    mar_list.append(gvb_u_b)
                    logger.info(f"\t MAR Front-to-Back Banning {gvb_u_b} MAR, walked_dist: {walked_dist}") if DEBUG_MAR else None
                    # update the current node
                    curr_u = u_b
                # add the mar constraints
                instance.opt.AddAtMostOne(mar_list)
                # ^ Back to Front
                u = (idx, row, col)
                gvf_u = instance.geometric_vars[u]["front"]
                gvb_u = instance.geometric_vars[u]["back"]
                # iterate util the given parameter
                mar_dist = mar_params[layer]
                walked_dist = 0
                mar_list = []
                mar_list.append(gvb_u)
                mar_list.append(gvf_u)
                curr_u = u
                logger.info(f"Node: {u} MAR dist: {mar_dist}") if DEBUG_MAR else None
                while walked_dist < mar_dist:
                    # check if the right neighbor exists
                    u_f = instance.lgg.get_front_neighbor(curr_u)
                    if u_f is None:
                        break
                    # NOTE: if u_f is the first row, dont restrict it.
                    if u_f[1] == instance.lgg.rows_in_layer(layer)[0]:
                        logger.info(f"MAR on {layer} exceeding min row. Ignoring first row.") if DEBUG_MAR else None
                        break
                    # gvf_u_b = instance.geometric_vars[u_b]["front"]
                    gvf_u_f = instance.geometric_vars[u_f]["front"]
                    # extract current col
                    curr_row = u_f[1]
                    walked_dist = abs(curr_row - row)
                    if walked_dist > mar_dist:
                        # if the distance is greater than the mar distance, then we need to break
                        break
                    # add the constraints
                    # mar_list.append(gvf_u_b)
                    mar_list.append(gvf_u_f)
                    logger.info(f"\t MAR Front-to-Back Banning {gvf_u_f} MAR, walked_dist: {walked_dist}") if DEBUG_MAR else None
                    # update the current node
                    curr_u = u_f
                # add the mar constraints
                instance.opt.AddAtMostOne(mar_list)


def _via_induce_metal_on_layer(instance, layer_name, direction, supervia_params):
    """
    For every node `u` on `layer_name`: if ANY via edge incident to `u`
    (the up-via to layer+1 or the down-via to layer-1) is active, then `u`
    must ALSO be connected to a metal-extension edge along the layer's
    own direction (front/back if V, left/right if H).

    Generalization of the legacy `via_induce_vertical_metal` (M1-only)
    and `via_induce_horizontal_metal` (M0-only) into a single direction-
    aware per-layer pass. Iterates BOTH up- and down-vias so internal
    routing layers (e.g. QFET's BM0/H0/M0 sandwich) are constrained on
    BOTH sides, not just down.

    Skips supervia layers (consumer's choice to allow passes-through).
    Returns the count of nodes constrained, for logging.
    """
    if supervia_params.get(layer_name, False):
        return 0

    n_constrained = 0
    for u in instance.lgg.nodes_in_layer(layer_name):
        layer_idx, row, col = u
        via_edges = []
        # down-via: edge_vars keys are (lower, upper), so edge below = (u_d, u)
        u_d = (layer_idx - 1, row, col)
        if instance.lgg.is_node_in_graph(u_d) and (u_d, u) in instance.edge_vars:
            via_edges.append(instance.edge_vars[(u_d, u)])
        # up-via: edge above = (u, u_u)
        u_u = (layer_idx + 1, row, col)
        if instance.lgg.is_node_in_graph(u_u) and (u, u_u) in instance.edge_vars:
            via_edges.append(instance.edge_vars[(u, u_u)])
        if not via_edges:
            continue

        metal_edges = []
        if direction == "V":
            u_f = instance.lgg.get_front_neighbor(u)
            if u_f is not None and (u_f, u) in instance.edge_vars:
                metal_edges.append(instance.edge_vars[(u_f, u)])
            u_b = instance.lgg.get_back_neighbor(u)
            if u_b is not None and (u, u_b) in instance.edge_vars:
                metal_edges.append(instance.edge_vars[(u, u_b)])
        else:  # H
            u_l = instance.lgg.get_left_neighbor(u)
            if u_l is not None and (u_l, u) in instance.edge_vars:
                metal_edges.append(instance.edge_vars[(u_l, u)])
            u_r = instance.lgg.get_right_neighbor(u)
            if u_r is not None and (u, u_r) in instance.edge_vars:
                metal_edges.append(instance.edge_vars[(u, u_r)])

        has_metal = instance.opt.NewBoolVar(
            f"has_metal_conn_{layer_name}_R{row}_C{col}"
        )
        if not metal_edges:
            instance.opt.Add(has_metal == 0)
        else:
            instance.opt.AddBoolOr(metal_edges).OnlyEnforceIf(has_metal)
            for me in metal_edges:
                instance.opt.AddImplication(me, has_metal)

        # Any active incident via implies metal extension on this layer.
        for ve in via_edges:
            instance.opt.AddImplication(ve, has_metal)
        n_constrained += 1
    return n_constrained


def via_induce_vertical_metal(instance, supervia_params):
    """
    For every V-direction NON-placement layer: an active via at a node on
    this layer requires a same-direction metal extension at that node
    (front/back neighbor edge active). Prevents orphan vias landing on
    floating metal segments.

    QFET fix vs. legacy FinFET: no longer hardcoded to "M1". Iterates
    every V-direction layer in `q_tech.layer_stack` that is NOT in
    `q_tech.placement_layer_names` (placement layers anchor sources/
    terminals - the via lands on the pin candidate node itself, no
    metal extension required).
    """
    instance.opt.log_comment("Per-layer V-metal via induction ...")
    logger.info("\t==\tPer-layer V-metal via induction ...")
    layer_to_dir = instance.lgg.layer_to_direction
    skip = set(instance.q_tech.placement_layer_names)
    for layer_name, direction in layer_to_dir.items():
        if direction != "V" or layer_name in skip:
            continue
        n = _via_induce_metal_on_layer(
            instance, layer_name, "V", supervia_params)
        logger.info(f"\t\t{layer_name} (V): {n} node(s) constrained")


def via_induce_horizontal_metal(instance, supervia_params):
    """
    For every H-direction NON-placement layer: an active via at a node on
    this layer requires a same-direction metal extension (left/right
    neighbor edge active). Prevents orphan vias landing on floating
    horizontal segments.

    QFET fix vs. legacy FinFET: no longer hardcoded to "M0". Iterates
    every H-direction layer in `q_tech.layer_stack` that is NOT in
    `q_tech.placement_layer_names`.
    """
    instance.opt.log_comment("Per-layer H-metal via induction ...")
    logger.info("\t==\tPer-layer H-metal via induction ...")
    layer_to_dir = instance.lgg.layer_to_direction
    skip = set(instance.q_tech.placement_layer_names)
    for layer_name, direction in layer_to_dir.items():
        if direction != "H" or layer_name in skip:
            continue
        n = _via_induce_metal_on_layer(
            instance, layer_name, "H", supervia_params)
        logger.info(f"\t\t{layer_name} (H): {n} node(s) constrained")


def geometric_vars_in_horizontal_layers(instance):
    """
    Create geometric variables for horizontal layers to track wire segment boundaries.

    Args:
        instance: The FinFET instance containing the opt, lgg, edge_vars, and geometric_vars
    """
    # --- 7.1) Geometric variables (left)
    instance.opt.log_comment("Adding geometric variables (left)...")
    for layer, idx in instance.lgg.layer_to_idx.items():
        if instance.lgg.layer_to_direction[layer] != "H":
            continue

        # helper to get/create gvL at node u
        def get_left_gv(u):
            inner = instance.geometric_vars.setdefault(u, {})
            if "left" not in inner:
                r, c = u[1], u[2]
                inner["left"] = instance.opt.NewBoolVar(f"gvL_L{idx}_R{r}_C{c}")
            return inner["left"]

        for row in instance.lgg.rows_in_layer(layer):
            for col in instance.lgg.cols_in_layer(layer):
                u = (idx, row, col)
                u_r = instance.lgg.get_right_neighbor(u)
                u_l = instance.lgg.get_left_neighbor(u)
                if u_r is None:
                    continue

                gvl = get_left_gv(u)
                gvl_u_r = get_left_gv(u_r)
                edge = instance.edge_vars[(u, u_r)]

                # start -> outgoing edge
                instance.opt.AddImplication(gvl, edge)

                if u_l is not None:
                    prev = instance.edge_vars[(u_l, u)]
                    tmp = instance.opt.NewBoolVar(f"tmp_left_continue_L{idx}_R{row}_C{col}")
                    # continuation indicator
                    instance.opt.AddBoolOr([gvl, prev]).OnlyEnforceIf(tmp)
                    instance.opt.AddImplication(gvl, tmp)
                    instance.opt.AddImplication(prev, tmp)
                    instance.opt.AddImplication(tmp.Not(), edge.Not())
                else:
                    # first column can't have an incoming edge
                    instance.opt.AddImplication(gvl.Not(), edge.Not())

                # can't both start here and continue
                instance.opt.AddAtMostOne([gvl_u_r, edge])

    # --- 7.2) Geometric variables (right)
    instance.opt.log_comment("Adding geometric variables (right)...")
    for layer, idx in instance.lgg.layer_to_idx.items():
        if instance.lgg.layer_to_direction[layer] != "H":
            continue

        # helper to get/create gvR at node u
        def get_right_gv(u):
            inner = instance.geometric_vars.setdefault(u, {})
            if "right" not in inner:
                r, c = u[1], u[2]
                inner["right"] = instance.opt.NewBoolVar(f"gvR_L{idx}_R{r}_C{c}")
            return inner["right"]

        for row in instance.lgg.rows_in_layer(layer):
            for col in instance.lgg.cols_in_layer(layer):
                u = (idx, row, col)
                u_r = instance.lgg.get_right_neighbor(u)
                u_l = instance.lgg.get_left_neighbor(u)

                gvr = get_right_gv(u)

                if u_r is not None:
                    gvr_u_r = get_right_gv(u_r)
                    edge = instance.edge_vars[(u, u_r)]

                    # can't both end here and continue
                    instance.opt.AddAtMostOne([gvr, edge])

                    tmp = instance.opt.NewBoolVar(f"tmp_right_continue_L{idx}_R{row}_C{col}")
                    instance.opt.AddBoolOr([gvr, edge]).OnlyEnforceIf(tmp)
                    instance.opt.AddImplication(gvr, tmp)
                    instance.opt.AddImplication(edge, tmp)

                    if u_l is not None:
                        prev = instance.edge_vars[(u_l, u)]
                        instance.opt.AddImplication(gvr, prev)
                        instance.opt.AddImplication(tmp.Not(), prev.Not())
                    else:
                        # first column can never have an incoming edge
                        instance.opt.Add(gvr == 0)

                    # neighbor's end -> outgoing edge
                    instance.opt.AddImplication(gvr_u_r, edge)
                else:
                    # boundary column: must match incoming edge (if any)
                    if u_l is not None:
                        prev = instance.edge_vars[(u_l, u)]
                        instance.opt.Add(prev == gvr)
                    else:
                        # single-cell row
                        instance.opt.Add(gvr == 0)


def geometric_vars_in_vertical_layers(instance):
    """
    Create geometric variables for vertical layers to track wire segment boundaries.

    Args:
        instance: The FinFET instance containing the opt, lgg, edge_vars, and geometric_vars
    """
    # ^ --- 7.3) Geometric variables (back) - Defines gvb at u
    # gvb at u: node u is a "back end," meaning a vertical segment (u_b, u) ends at u.
    # This implies:
    # 1. Edge (u_b, u) MUST exist (this was locally named curr_edge in the original 7.3).
    # 2. Edge (u, u_f) (to u's front neighbor) MUST NOT exist (this was locally named prev_edge
    #    in the original 7.3, if u_f itself exists).
    instance.opt.log_comment(f"Adding geometric variables (back)...")
    for layer, idx in instance.lgg.layer_to_idx.items():
        if layer in ("PC", "BPC"):
            continue  # skip placement layers
        if instance.lgg.layer_to_direction[layer] != "V":  # Only for vertical layers
            continue
        for row in instance.lgg.rows_in_layer(layer):
            for col in instance.lgg.cols_in_layer(layer):
                u = (idx, row, col)
                u_b = instance.lgg.get_back_neighbor(u)  # u_b defines the incoming edge from the back
                u_f = instance.lgg.get_front_neighbor(u)  # u_f defines the potential outgoing edge to the back
                # print("u", u, "u_f", u_f, "u_b", u_b)
                # Retrieve or create the geometric variable for "back end" at u
                gvf = instance.geometric_vars.setdefault(u, {}).setdefault("front", instance.opt.NewBoolVar(f"gvF_L{idx}_R{row}_C{col}"))

                if u_b is None:
                    # If there is no back neighbor, the required incoming edge (u_b,u) cannot exist.
                    # Since gvf being true implies this edge exists, gvf must be false.
                    instance.opt.Add(gvf == False)
                    continue

                # If u_b exists, curr_edge is the incoming edge from the back (u_b, u).
                # This is the edge whose existence is primary for gvf.
                # curr_edge = instance.edge_vars[(u_b, u)]
                curr_edge = instance.edge_vars[(u, u_b)]

                # Definition of gvf at u:
                # gvf is true <=> (curr_edge is true AND (prev_edge is false OR u_f is None))

                # Part 1: gvf => curr_edge
                # If u is a "back end" (gvf is true), then curr_edge (u_b, u) must exist.
                instance.opt.AddImplication(gvf, curr_edge)

                if u_f is not None:  # If there is a potential "next" node u_f and thus a potential outgoing edge (u, u_f)
                    # The edge (u, u_f) was locally named prev_edge in the original 7.3 code.
                    # prev_edge = instance.edge_vars[(u, u_f)]
                    prev_edge = instance.edge_vars[(u_f,u)]

                    # Part 2: gvf => prev_edge.Not()
                    # If u is a "back end" (gvf is true), the outgoing edge (prev_edge) to u_f must NOT exist.
                    # AddAtMostOne([gvf, prev_edge]) ensures that gvf and prev_edge cannot both be true.
                    instance.opt.AddAtMostOne([gvf, prev_edge])

                    # Part 3: (curr_edge AND prev_edge.Not()) => gvf
                    # This is typically handled by a "reason" constraint for the primary edge (curr_edge):
                    # curr_edge => (gvf OR prev_edge)
                    # If curr_edge is true AND prev_edge is false, this forces gvf to be true.

                    # Create an indicator variable for the condition (gvf OR prev_edge)
                    # Original naming was tmp_back_continue_indicator, let's use a more general "reason" name
                    tmp_back_reason_indicator = instance.opt.NewBoolVar(f"tmp_back_reason_indicator_L{idx}_R{row}_C{col}")

                    # Establish tmp_back_reason_indicator <=> (gvf OR prev_edge)
                    instance.opt.AddImplication(gvf, tmp_back_reason_indicator)
                    instance.opt.AddImplication(prev_edge, tmp_back_reason_indicator)
                    instance.opt.AddBoolOr([gvf, prev_edge]).OnlyEnforceIf(tmp_back_reason_indicator)

                    # Link curr_edge to this reason: curr_edge => (gvf OR prev_edge)
                    instance.opt.AddImplication(curr_edge, tmp_back_reason_indicator)

                else:  # u_f is None (u is at the foremost boundary of the layer)
                    # In this scenario, the outgoing edge (prev_edge) does not exist (implicitly false).
                    # The condition "prev_edge.Not()" is automatically met.
                    # Therefore, gvf should be true if and only if curr_edge (the incoming edge) exists.
                    # We already have: gvf => curr_edge (from Part 1).
                    # We need to add: curr_edge => gvf to complete the equivalence.
                    instance.opt.AddImplication(curr_edge, gvf)

    # ^ --- 7.4) Geometric variables (front)
    instance.opt.log_comment(f"Adding geometric variables (front)...")
    for layer, idx in instance.lgg.layer_to_idx.items():
        if layer in ("PC", "BPC"):
            continue  # skip placement layers
        if instance.lgg.layer_to_direction[layer] != "V":  # Only for vertical layers
            continue
        for row in instance.lgg.rows_in_layer(layer):
            for col in instance.lgg.cols_in_layer(layer):
                u = (idx, row, col)
                u_f = instance.lgg.get_front_neighbor(u)  # u_f is the "next" node in the segment's direction
                u_b = instance.lgg.get_back_neighbor(u)  # u_b is the "previous" node in this direction
                gvb = instance.geometric_vars.setdefault(u, {}).setdefault("back", instance.opt.NewBoolVar(f"gvB_L{idx}_R{row}_C{col}"))
                # If u_f is None, the main edge (u, u_f) cannot exist.
                # Therefore, u cannot be the "back end" (i.e., start) of such a segment.
                if u_f is None:
                    # If there is no back neighbor, the required incoming edge (u_b,u) cannot exist.
                    # Since gvb being true implies this edge exists, gvb must be false.
                    instance.opt.Add(gvb == False)
                    continue

                # If u_f exists, main_edge_vf is the primary edge associated with gvb at u.
                # This edge goes from u to u_f.
                # main_edge_vf = instance.edge_vars[(u, u_f)]
                main_edge_vf = instance.edge_vars[(u_f, u)]

                # Definition of gvb at u:
                # gvb is true <=> (main_edge_vf is true AND (incoming_edge_vb is false OR u_b is None))

                # Part 1: gvb => main_edge_vf
                # If u is a "front end" (gvb is true), then the main_edge_vf (u, u_f) must exist.
                instance.opt.AddImplication(gvb, main_edge_vf)

                if u_b is not None:  # If there is a potential "previous" node u_b
                    # incoming_edge_vb = instance.edge_vars[(u_b, u)]  # This is the edge (u_b, u)
                    incoming_edge_vb = instance.edge_vars[(u, u_b)]
                    # Part 2: gvb => incoming_edge_vb.Not()
                    # If u is a "front end", the incoming_edge_vb from u_b must NOT exist.
                    # AddAtMostOne([gvb, incoming_edge_vb]) ensures that gvb and incoming_edge_vb
                    # cannot both be true. This implies:
                    #   gvb => incoming_edge_vb.Not()
                    #   incoming_edge_vb => gvb.Not()
                    instance.opt.AddAtMostOne([gvb, incoming_edge_vb])

                    # Part 3: (main_edge_vf AND incoming_edge_vb.Not()) => gvb
                    # This is established using a "reason" constraint for main_edge_vf:
                    # main_edge_vf => (gvb OR incoming_edge_vb)
                    # If main_edge_vf is true AND incoming_edge_vb is false, this forces gvb to be true.

                    # Create an indicator variable for the condition (gvb OR incoming_edge_vb)
                    tmp_reason_for_main_edge_vf = instance.opt.NewBoolVar(f"tmp_reason_main_vf_gvb_L{idx}_R{row}_C{col}")  # Clarified name

                    # Establish tmp_reason_for_main_edge_vf <=> (gvb OR incoming_edge_vb)
                    instance.opt.AddImplication(gvb, tmp_reason_for_main_edge_vf)
                    instance.opt.AddImplication(incoming_edge_vb, tmp_reason_for_main_edge_vf)
                    instance.opt.AddBoolOr([gvb, incoming_edge_vb]).OnlyEnforceIf(tmp_reason_for_main_edge_vf)

                    # Link main_edge_vf to this reason: main_edge_vf => (gvb OR incoming_edge_vb)
                    instance.opt.AddImplication(main_edge_vf, tmp_reason_for_main_edge_vf)

                else:  # u_b is None (u is at the rearmost boundary of the layer for vertical tracks)
                    # In this scenario, incoming_edge_vb effectively does not exist (is implicitly false).
                    # The condition "incoming_edge_vb.Not()" is automatically met.
                    # Therefore, gvb should be true if and only if main_edge_vf exists.
                    # We already have: gvb => main_edge_vf (from Part 1).
                    # We need to add: main_edge_vf => gvb to complete the equivalence.
                    instance.opt.AddImplication(main_edge_vf, gvb)


# =============================================================================
# Via-rule connectivity / separation family
# -----------------------------------------------------------------------------
# Additive DRC rules: new functions that do NOT alter any existing rule.py code
# path. QFET gates DRC off and does not call them. CFET/FinFET call them for
# DRC; without them those techs lose via-separation / metal-must-have-via
# connectivity rules and can produce DRC-violating layouts.
#
# Notes on behavior:
#   - The placement-layer skip set comes from
#     instance.q_tech.placement_layer_names (matching the via_induce_* helpers
#     above), not a hardcoded {"PC","BPC"} literal.
#   - metal_endpoint_must_have_via target layers are parameterized
#     (default frozenset({"M2"})) and node_is_SON_vars is getattr-guarded so
#     techs that do not populate it (e.g. CFET) simply exempt no nodes.
# =============================================================================


def _gather_bottom_via_between_nodes(instance, layer_idx, u1, u2):
    """
    Helper function to gather via edges between two nodes u1 and u2 on a specific layer.
    Returns a list of via edges if they exist, otherwise returns an empty list.

    Args:
        instance: The cell instance (self-as-context-object)
        layer_idx: The layer index
        u1: First node tuple (layer_idx, row, col)
        u2: Second node tuple (layer_idx, row, col)

    Returns:
        List of via edge variables
    """
    via_edges = []
    assert instance.lgg.is_node_in_graph(u1), f"Node {u1} does not exist in the graph"
    assert instance.lgg.is_node_in_graph(u2), f"Node {u2} does not exist in the graph"
    assert layer_idx == u1[0] == u2[0], f"Layer index mismatch: {layer_idx} != {u1[0]} or {u2[0]}"

    if instance.lgg.layer_to_direction[instance.lgg.idx_to_layer[layer_idx]] == "V":
        assert u1[2] == u2[2], f"Nodes {u1} and {u2} must be in the same column for vertical layers"
        bottom_layer_idx = layer_idx - 1
        if bottom_layer_idx < 0:
            return via_edges
        start_row = min(u1[1], u2[1])
        end_row = max(u1[1], u2[1])
        col = u1[2]

        # get the nearest node in the bottom layer
        nearest_node = instance.lgg.nearest_node_in_layer(layer=bottom_layer_idx, row=start_row, col=u1[2])
        assert nearest_node is not None, f"No nearest node found in layer {bottom_layer_idx} at column {u1[2]}"
        # latch on to the nearest node and start iterating through the rows
        current_row = nearest_node[1]
        for row in instance.lgg.rows_in_layer_from(layer=bottom_layer_idx, row=current_row):
            if not (start_row <= row <= end_row):
                continue
            current_bottom_node = (bottom_layer_idx, row, col)
            # check if the current bottom node has a via above
            if instance.lgg._has_via_above(node=current_bottom_node):
                nn_above = instance.lgg._get_via_above(node=current_bottom_node)
                via_edge = instance.edge_vars.get((current_bottom_node, nn_above))
                assert via_edge is not None, f"Via edge between {current_bottom_node} and {nn_above} does not exist"
                via_edges.append(via_edge)

    elif instance.lgg.layer_to_direction[instance.lgg.idx_to_layer[layer_idx]] == "H":
        assert u1[1] == u2[1], f"Nodes {u1} and {u2} must be in the same row for horizontal layers"
        bottom_layer_idx = layer_idx - 1
        if bottom_layer_idx < 0:
            return via_edges
        start_col = min(u1[2], u2[2])
        end_col = max(u1[2], u2[2])
        row = u1[1]

        # get the nearest node in the bottom layer
        nearest_node = instance.lgg.nearest_node_in_layer(layer=bottom_layer_idx, row=u1[1], col=start_col)
        assert nearest_node is not None, f"No nearest node found in layer {bottom_layer_idx} at row {u1[1]}"
        # latch on to the nearest node and start iterating through the columns
        current_col = nearest_node[2]
        for col in instance.lgg.cols_in_layer_from(layer=bottom_layer_idx, col=current_col):
            if not (start_col <= col <= end_col):
                continue
            current_bottom_node = (bottom_layer_idx, row, col)
            # check if the current bottom node has a via above
            if instance.lgg._has_via_above(node=current_bottom_node):
                nn_above = instance.lgg._get_via_above(node=current_bottom_node)
                via_edge = instance.edge_vars.get((current_bottom_node, nn_above))
                assert via_edge is not None, f"Via edge between {current_bottom_node} and {nn_above} does not exist"
                via_edges.append(via_edge)
    else:
        raise ValueError(f"Layer {instance.lgg.idx_to_layer[layer_idx]} is neither horizontal nor vertical")

    return via_edges


def vertical_metal_must_be_connected_to_via(instance):
    """
    For each vertical layer, if a node is connected to a vertical metal edge,
    it must also be connected to a via.

    Args:
        instance: The cell instance (self-as-context-object)
    """
    instance.opt.log_comment("Vertical metal must be connected to a via ...")
    skip = set(instance.q_tech.placement_layer_names)
    for layer, idx in instance.lgg.layer_to_idx.items():
        if instance.lgg.layer_to_direction[layer] != "V":
            continue
        if layer in skip:
            continue  # skip placement layers
        for row in instance.lgg.rows_in_layer(layer):
            for col in instance.lgg.cols_in_layer(layer):
                u = (idx, row, col)
                gvf_u = instance.geometric_vars[u]["front"]
                current_u = u
                while True:
                    # gvb_u is the 'back' geometric var, so the node we step to
                    # must be the BACK neighbor. Use get_back_neighbor here
                    # (do not use get_front_neighbor, which swaps the direction).
                    u_b = instance.lgg.get_back_neighbor(current_u)
                    if u_b is None:
                        break
                    gvb_u = instance.geometric_vars[u]["back"]
                    # gather all via edges between u and its back neighbor
                    via_edges = _gather_bottom_via_between_nodes(instance, layer_idx=idx, u1=u, u2=u_b)
                    # if there are no via edges, then these two nodes cannot be true together
                    if not via_edges:
                        # not((gvf_u and gvb_u))
                        instance.opt.AddImplication(gvf_u, gvb_u.Not())
                        instance.opt.AddImplication(gvb_u, gvf_u.Not())
                    else:
                        # if there are via edges, then we need to ensure that at least one of them is true when gvf_u and gvb_u are true
                        # Create a variable for the conjunction (gvf_u AND gvb_u)
                        both_vars = instance.opt.NewBoolVar(f"both_gv_L{idx}_R{row}_C{col}_and_gvB_L{idx}_R{u_b[1]}_C{u_b[2]}")

                        # Set both_vars to be equivalent to (gvf_u AND gvb_u)
                        instance.opt.AddBoolAnd([gvf_u, gvb_u]).OnlyEnforceIf(both_vars)
                        instance.opt.Add(gvf_u + gvb_u < 2).OnlyEnforceIf(both_vars.Not())

                        # If both_vars is true, then at least one via edge must be true
                        # Add a constraint that if both geometric variables are true, at least one via must be true
                        instance.opt.Add(sum(via_edges) >= 1).OnlyEnforceIf(both_vars)
                    current_u = u_b  # Move to the next node in the vertical direction


def horizontal_metal_must_be_connected_to_via(instance):
    """
    For each horizontal layer, if a node is connected to a horizontal metal edge,
    it must also be connected to a via.

    Args:
        instance: The cell instance (self-as-context-object)
    """
    instance.opt.log_comment("Horizontal metal must be connected to a via ...")
    skip = set(instance.q_tech.placement_layer_names)
    for layer, idx in instance.lgg.layer_to_idx.items():
        if instance.lgg.layer_to_direction[layer] != "H":
            continue
        if layer in skip:
            continue  # skip placement layers
        for row in instance.lgg.rows_in_layer(layer):
            for col in instance.lgg.cols_in_layer(layer):
                u = (idx, row, col)
                gvl_u = instance.geometric_vars[u]["left"]
                current_u = u
                while True:
                    u_r = instance.lgg.get_right_neighbor(current_u)
                    if u_r is None:
                        break
                    gvr_u = instance.geometric_vars[u]["right"]
                    # gather all via edges between u and its right neighbor
                    via_edges = _gather_bottom_via_between_nodes(instance, layer_idx=idx, u1=u, u2=u_r)
                    # if there are no via edges, then these two nodes cannot be true together
                    if not via_edges:
                        # not((gvl_u and gvr_u))
                        instance.opt.AddImplication(gvl_u, gvr_u.Not())
                        instance.opt.AddImplication(gvr_u, gvl_u.Not())
                    else:
                        # if there are via edges, then we need to ensure that at least one of them is true when gvl_u and gvr_u are true
                        # Create a variable for the conjunction (gvl_u AND gvr_u)
                        both_vars = instance.opt.NewBoolVar(f"both_gv_L{idx}_R{row}_C{col}_and_gvR_L{idx}_R{u_r[1]}_C{u_r[2]}")

                        # Set both_vars to be equivalent to (gvl_u AND gvr_u)
                        instance.opt.AddBoolAnd([gvl_u, gvr_u]).OnlyEnforceIf(both_vars)
                        instance.opt.Add(gvl_u + gvr_u < 2).OnlyEnforceIf(both_vars.Not())

                        # If both_vars is true, then at least one via edge must be true
                        # Add a constraint that if both geometric variables are true, at least one via must be true
                        instance.opt.Add(sum(via_edges) >= 1).OnlyEnforceIf(both_vars)
                    current_u = u_r


def metal_endpoint_must_have_via(instance, target_layers=frozenset({"M2"})):
    """
    Strengthen metal-via connectivity: if ANY metal edge is active at a node
    on a target metal layer, the node must also have at least one via edge.

    This eliminates "metal stubs" - dead-end wires that don't connect to any
    adjacent layer. Such stubs inflate wirelength without serving any routing
    purpose and loosen the LP relaxation bound.

    Exception: SON (Super Outer Node) nodes are I/O pin access points that
    legitimately exist on metal layers without downward vias.

    Args:
        instance: The cell instance (self-as-context-object)
        target_layers: Set of metal layer names to apply the rule to
            (default frozenset({"M2"}) - the top metal layer, where stubs are
            always useless; lower-layer stubs may be needed for DRC compliance).
    """
    logger.info("\t==\tEnforcing metal endpoint must have via ...")
    instance.opt.log_comment("Metal endpoint must have via (no dead-end stubs) ...")

    # Collect all SON nodes (I/O pin access - exempt from via requirement).
    # node_is_SON_vars is getattr-guarded: techs that do not populate it
    # (e.g. CFET) simply exempt no nodes.
    son_nodes = set()
    for net_name in getattr(instance, "node_is_SON_vars", {}):
        for k in instance.node_is_SON_vars[net_name]:
            for node in instance.node_is_SON_vars[net_name][k]:
                son_nodes.add(node)

    num_constraints = 0
    for layer, layer_idx in instance.lgg.layer_to_idx.items():
        if layer not in target_layers:
            continue
        direction = instance.lgg.layer_to_direction[layer]

        for u in instance.lgg.nodes_in_layer(layer):
            if u in son_nodes:
                continue  # SON nodes don't need via

            # Collect ALL metal edges at this node (on the same layer)
            metal_edges = []
            if direction == "V":
                u_f = instance.lgg.get_front_neighbor(u)
                if u_f is not None and (u_f, u) in instance.edge_vars:
                    metal_edges.append(instance.edge_vars[(u_f, u)])
                u_b = instance.lgg.get_back_neighbor(u)
                if u_b is not None and (u, u_b) in instance.edge_vars:
                    metal_edges.append(instance.edge_vars[(u, u_b)])
            elif direction == "H":
                u_l = instance.lgg.get_left_neighbor(u)
                if u_l is not None and (u_l, u) in instance.edge_vars:
                    metal_edges.append(instance.edge_vars[(u_l, u)])
                u_r = instance.lgg.get_right_neighbor(u)
                if u_r is not None and (u, u_r) in instance.edge_vars:
                    metal_edges.append(instance.edge_vars[(u, u_r)])

            if not metal_edges:
                continue

            # Collect via edges at this node (connecting to adjacent layers)
            via_edges = []
            row, col = u[1], u[2]
            # Via below
            u_below = (layer_idx - 1, row, col)
            if instance.lgg.is_node_in_graph(u_below) and (u_below, u) in instance.edge_vars:
                via_edges.append(instance.edge_vars[(u_below, u)])
            # Via above
            u_above = (layer_idx + 1, row, col)
            if instance.lgg.is_node_in_graph(u_above) and (u, u_above) in instance.edge_vars:
                via_edges.append(instance.edge_vars[(u, u_above)])

            if not via_edges:
                # No via possible at this node - skip (wire may pass through)
                continue
            else:
                # Metal endpoint (stub) must have via - no dead-end wires
                # A stub is metal in only ONE direction. If metal extends in
                # both directions, the existing two-directional constraint
                # already requires via. This handles the one-directional case.
                for me in metal_edges:
                    instance.opt.Add(sum(via_edges) >= 1).OnlyEnforceIf(me)
                    num_constraints += 1

    logger.info(f"\t==\tMetal endpoint via constraints: {num_constraints} added, {len(son_nodes)} SON nodes exempted")


def via_separation_rules(instance, via_params):
    """
    Enforce via separation rules to ensure vias maintain minimum L1 (Manhattan) distance.

    Args:
        instance: The cell instance (self-as-context-object)
        via_params: Dictionary mapping layer pairs to via separation distance parameters
    """
    DEBUG_VR_DIST = False
    instance.opt.log_comment(f"Enforcing via separation rules (L1 Manhattan distance)...")
    for layer_pair, via_dist in via_params.items():
        layer_1, layer_2 = layer_pair
        # check layer direction
        hori_layer, vert_layer = None, None
        if instance.lgg.layer_to_direction[layer_1] == "H":
            hori_layer = layer_1
            vert_layer = layer_2
        elif instance.lgg.layer_to_direction[layer_2] == "H":
            hori_layer = layer_2
            vert_layer = layer_1
        else:
            raise ValueError(f"Layer {layer_1} and {layer_2} are not horizontal or vertical")

        # Get all rows and columns
        all_rows = instance.lgg.rows_in_layer(hori_layer)
        all_cols = instance.lgg.cols_in_layer(vert_layer)

        for row in all_rows:
            for col in all_cols:
                u_1 = (instance.lgg.layer_to_idx[layer_1], row, col)
                u_2 = (instance.lgg.layer_to_idx[layer_2], row, col)

                # Check if via edge exists at this position
                via_edge = instance.edge_vars.get((u_1, u_2))
                if via_edge is None:
                    # No via edge at this position, skip
                    continue

                logger.info(f"Node: {u_1} via dist: {via_dist}") if DEBUG_VR_DIST else None

                # Iterate over all possible positions within L1 distance
                # L1 distance = |row_delta| + |col_delta| < via_dist
                # Enforce pairwise mutual exclusion constraints
                for other_row in all_rows:
                    row_delta = abs(other_row - row)
                    if row_delta >= via_dist:
                        continue  # Too far in row direction alone

                    # Calculate remaining distance budget for column
                    max_col_delta = via_dist - row_delta - 1

                    for other_col in all_cols:
                        col_delta = abs(other_col - col)

                        # Skip the current via position
                        if other_row == row and other_col == col:
                            continue

                        # Check if within L1 distance
                        l1_distance = row_delta + col_delta
                        if l1_distance >= via_dist:
                            continue

                        # Check if this via position exists in the graph
                        u_1_other = (instance.lgg.layer_to_idx[layer_1], other_row, other_col)
                        u_2_other = (instance.lgg.layer_to_idx[layer_2], other_row, other_col)

                        if not instance.lgg.is_node_in_graph(u_2_other):
                            continue

                        # Get the via edge variable
                        other_via_edge = instance.edge_vars.get((u_1_other, u_2_other))
                        if other_via_edge is not None:
                            # Fix: Add pairwise constraint: via_edge and other_via_edge cannot both be true
                            instance.opt.AddAtMostOne([via_edge, other_via_edge])
                            logger.info(
                                f"\tPairwise exclusion: via at ({row}, {col}) and ({other_row}, {other_col}), "
                                f"L1 distance: {l1_distance} < {via_dist}"
                            ) if DEBUG_VR_DIST else None

