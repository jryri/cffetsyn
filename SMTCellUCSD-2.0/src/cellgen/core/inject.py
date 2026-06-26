from loguru import logger
import sys
import networkx as nx
from itertools import pairwise, permutations
from collections import defaultdict
from ortools.sat.python import cp_model

from src.cellgen.core.entity import Model

_NUM_COL_SDG_ = 3  # number of columns needed for source/drain/gate


def _get_shared_sd_nets(finfet, tran_i_name, tran_j_name):
    """
    Find all nets (including VDD/VSS) shared between two transistors
    via their source/drain terminals.

    Returns:
        List of (net_name, tran_i_terminal, tran_j_terminal) tuples.
        e.g. [("VDD", "source", "source"), ("net3", "drain", "source")]
        A single net may appear multiple times if a transistor has the same
        net on both its source and drain (e.g. VDD on both).
    """
    results = []
    tran_i = finfet.circuit.transistors[tran_i_name]
    tran_j = finfet.circuit.transistors[tran_j_name]
    for net in finfet.circuit.nets.values():
        conn = net.connected_transistors
        # Collect which terminals of tran_i / tran_j attach to this net
        i_terminals = [pin for (t, pin) in conn if t == tran_i_name and pin in ("source", "drain")]
        j_terminals = [pin for (t, pin) in conn if t == tran_j_name and pin in ("source", "drain")]
        if not i_terminals or not j_terminals:
            continue
        # Emit all combinations (handles e.g. VDD on both source and drain)
        for it in i_terminals:
            for jt in j_terminals:
                results.append((net.name, it, jt))
    return results


def _find_shared_sd_nets(finfet, tran_name_1, tran_name_2):
    """
    Find all nets where tran_1 and tran_2 share a source or drain connection.
    Uses the complete circuit (including VDD/VSS and 2-pin nets) unlike the
    clustering graph which excludes them.

    Returns:
        list of (net_name, connected_transistors) tuples
    """
    shared_nets = []
    for net in finfet.circuit.get_nets(with_power_ground=True):
        conn = net.connected_transistors
        t1_sd = (tran_name_1, "source") in conn or (tran_name_1, "drain") in conn
        t2_sd = (tran_name_2, "source") in conn or (tran_name_2, "drain") in conn
        if t1_sd and t2_sd:
            shared_nets.append((net.name, conn))
    return shared_nets


def _constrain_diffusion_sharing_for_path(finfet, var, tran_i_name, tran_j_name):
    """
    For same-MOS-type pair in path [tran_i, tran_j] where i is LEFT of j,
    constrain flip_var and ds_pair_vars based on their shared source/drain net.

    In pairwise_diffusion_sharing, keys use sorted transistor order:
      ds_left_{sorted_1}_{sorted_2}_{net}  means sorted_1 left of sorted_2
      ds_right_{sorted_1}_{sorted_2}_{net} means sorted_1 right of sorted_2
    """
    tvar_i = finfet.transistor_vars[tran_i_name]
    tvar_j = finfet.transistor_vars[tran_j_name]

    sorted_names = sorted([tran_i_name, tran_j_name])
    sorted_1, sorted_2 = sorted_names[0], sorted_names[1]

    shared_nets = _find_shared_sd_nets(finfet, tran_i_name, tran_j_name)
    if not shared_nets:
        logger.warning(
            f"No shared S/D net found between {tran_i_name} and {tran_j_name} "
            f"for diffusion sharing constraint."
        )
        return

    for net_name, conn in shared_nets:
        t_i_is_source = (tran_i_name, "source") in conn
        t_i_is_drain = (tran_i_name, "drain") in conn
        t_j_is_source = (tran_j_name, "source") in conn
        t_j_is_drain = (tran_j_name, "drain") in conn

        # Determine which ds_pair_var direction applies
        if tran_i_name == sorted_1:
            ds_key = f"ds_left_{sorted_1}_{sorted_2}_{net_name}"
        else:
            ds_key = f"ds_right_{sorted_1}_{sorted_2}_{net_name}"

        # Force the ds_pair_var on if it exists
        if hasattr(finfet, 'ds_pair_vars') and ds_key in finfet.ds_pair_vars:
            logger.info(
                f"\tCluster forcing {ds_key} = 1 when path active"
            )
            finfet.opt.Add(finfet.ds_pair_vars[ds_key] == 1).OnlyEnforceIf(var)

        # Constrain flip_var based on sharing type
        # path [i, j]: i is left of j
        if t_i_is_source and t_j_is_source:
            logger.info(
                f"\tCluster S-S sharing {net_name}: "
                f"{tran_i_name} flipped, {tran_j_name} not flipped"
            )
            finfet.opt.Add(tvar_i.flip_var == 1).OnlyEnforceIf(var)
            finfet.opt.Add(tvar_j.flip_var == 0).OnlyEnforceIf(var)
        elif t_i_is_drain and t_j_is_drain:
            logger.info(
                f"\tCluster D-D sharing {net_name}: "
                f"{tran_i_name} not flipped, {tran_j_name} flipped"
            )
            finfet.opt.Add(tvar_i.flip_var == 0).OnlyEnforceIf(var)
            finfet.opt.Add(tvar_j.flip_var == 1).OnlyEnforceIf(var)
        elif t_i_is_source and t_j_is_drain:
            logger.info(
                f"\tCluster S-D sharing {net_name}: "
                f"{tran_i_name} flipped, {tran_j_name} flipped"
            )
            finfet.opt.Add(tvar_i.flip_var == 1).OnlyEnforceIf(var)
            finfet.opt.Add(tvar_j.flip_var == 1).OnlyEnforceIf(var)
        elif t_i_is_drain and t_j_is_source:
            logger.info(
                f"\tCluster D-S sharing {net_name}: "
                f"{tran_i_name} not flipped, {tran_j_name} not flipped"
            )
            finfet.opt.Add(tvar_i.flip_var == 0).OnlyEnforceIf(var)
            finfet.opt.Add(tvar_j.flip_var == 0).OnlyEnforceIf(var)
        break  # only constrain based on the first shared net found


def _constrain_lisd_sharing_for_path(finfet, var, tran_i_name, tran_j_name):
    """
    For different-MOS-type pair in path [tran_i, tran_j] where i is ALIGNED with j,
    constrain flip_var and lisd_share_pair_vars based on their shared source/drain net.

    In pairwise_lisd_sharing, keys use sorted transistor order:
      lisd_share_{sorted_1}_{sorted_2}_{net}
    """
    tvar_i = finfet.transistor_vars[tran_i_name]
    tvar_j = finfet.transistor_vars[tran_j_name]

    sorted_names = sorted([tran_i_name, tran_j_name])
    sorted_1, sorted_2 = sorted_names[0], sorted_names[1]

    shared_nets = _find_shared_sd_nets(finfet, tran_i_name, tran_j_name)
    if not shared_nets:
        logger.warning(
            f"No shared S/D net found between {tran_i_name} and {tran_j_name} "
            f"for LISD sharing constraint."
        )
        return

    for net_name, conn in shared_nets:
        # Skip power/ground nets for LISD sharing constraints
        net_obj = finfet.circuit.nets.get(net_name)
        if net_obj and net_obj.is_power_or_ground_net():
            continue

        t_i_is_source = (tran_i_name, "source") in conn
        t_i_is_drain = (tran_i_name, "drain") in conn
        t_j_is_source = (tran_j_name, "source") in conn
        t_j_is_drain = (tran_j_name, "drain") in conn

        lisd_key = f"lisd_share_{sorted_1}_{sorted_2}_{net_name}"

        # Force the lisd_share_pair_var on if it exists
        if hasattr(finfet, 'lisd_share_pair_vars') and lisd_key in finfet.lisd_share_pair_vars:
            logger.info(
                f"\tCluster forcing {lisd_key} = 1 when path active"
            )
            finfet.opt.Add(finfet.lisd_share_pair_vars[lisd_key] == 1).OnlyEnforceIf(var)

        # Constrain flip_var based on terminal matching
        if (t_i_is_source and t_j_is_source) or (t_i_is_drain and t_j_is_drain):
            logger.info(
                f"\tCluster LISD same-terminal sharing {net_name}: "
                f"{tran_i_name} and {tran_j_name} flip_var must be equal"
            )
            finfet.opt.Add(tvar_i.flip_var == tvar_j.flip_var).OnlyEnforceIf(var)
        elif (t_i_is_source and t_j_is_drain) or (t_i_is_drain and t_j_is_source):
            logger.info(
                f"\tCluster LISD cross-terminal sharing {net_name}: "
                f"{tran_i_name} and {tran_j_name} flip_var must differ"
            )
            finfet.opt.Add(tvar_i.flip_var != tvar_j.flip_var).OnlyEnforceIf(var)
        break  # only constrain based on the first non-power shared net found


def _get_diffusion_sharing_options(finfet, tran_i_name, tran_j_name):
    """
    For same-MOS pair where tran_i is LEFT of tran_j in the path,
    return all possible sharing options from shared S/D nets.

    Each option: (net_name, sharing_type, flip_i, flip_j, ds_key)
    """
    sorted_names = sorted([tran_i_name, tran_j_name])
    sorted_1, sorted_2 = sorted_names[0], sorted_names[1]

    shared_nets = _find_shared_sd_nets(finfet, tran_i_name, tran_j_name)
    options = []

    for net_name, conn in shared_nets:
        t_i_src = (tran_i_name, "source") in conn
        t_i_drn = (tran_i_name, "drain") in conn
        t_j_src = (tran_j_name, "source") in conn
        t_j_drn = (tran_j_name, "drain") in conn

        if tran_i_name == sorted_1:
            ds_key = f"ds_left_{sorted_1}_{sorted_2}_{net_name}"
        else:
            ds_key = f"ds_right_{sorted_1}_{sorted_2}_{net_name}"

        if t_i_src and t_j_src:
            options.append((net_name, "S-S", 1, 0, ds_key))
        elif t_i_drn and t_j_drn:
            options.append((net_name, "D-D", 0, 1, ds_key))
        elif t_i_src and t_j_drn:
            options.append((net_name, "S-D", 1, 1, ds_key))
        elif t_i_drn and t_j_src:
            options.append((net_name, "D-S", 0, 0, ds_key))

    return options


def _get_lisd_sharing_options(finfet, tran_i_name, tran_j_name):
    """
    For cross-MOS pair (P-N or N-P) where tran_i is ALIGNED with tran_j,
    return all possible LISD sharing options from shared internal S/D nets.

    LISD sharing constraints:
      same terminal (s-s, d-d): flips must be equal  -> (0,0) or (1,1)
      diff terminal (s-d, d-s): flips must differ    -> (0,1) or (1,0)

    Each option: (net_name, sharing_type, flip_i, flip_j, lisd_key)
    """
    sorted_names = sorted([tran_i_name, tran_j_name])
    sorted_1, sorted_2 = sorted_names[0], sorted_names[1]

    shared_nets = _find_shared_sd_nets(finfet, tran_i_name, tran_j_name)
    options = []

    for net_name, conn in shared_nets:
        # Skip power/ground nets for LISD sharing
        net_obj = finfet.circuit.nets.get(net_name)
        if net_obj and net_obj.is_power_or_ground_net():
            continue

        t_i_src = (tran_i_name, "source") in conn
        t_i_drn = (tran_i_name, "drain") in conn
        t_j_src = (tran_j_name, "source") in conn
        t_j_drn = (tran_j_name, "drain") in conn

        lisd_key = f"lisd_share_{sorted_1}_{sorted_2}_{net_name}"

        # same terminal: flips equal -> two concrete options
        if (t_i_src and t_j_src) or (t_i_drn and t_j_drn):
            stype = "LISD-same"
            options.append((net_name, stype, 0, 0, lisd_key))
            options.append((net_name, stype, 1, 1, lisd_key))
        # different terminal: flips differ -> two concrete options
        elif (t_i_src and t_j_drn) or (t_i_drn and t_j_src):
            stype = "LISD-cross"
            options.append((net_name, stype, 0, 1, lisd_key))
            options.append((net_name, stype, 1, 0, lisd_key))

    return options


def _get_gate_sharing_keys(finfet, tran_i_name, tran_j_name):
    """
    For cross-MOS pair (P-N or N-P) where tran_i is ALIGNED with tran_j,
    return all gate_share_pair_var keys for shared gate nets.

    Gate sharing requires no flip constraint (gate is always the center column),
    only vertical alignment (same x), which is already enforced by the path.

    Returns:
        list of (net_name, gate_key) tuples
    """
    results = []
    # gate_share keys use sorted order: gate_share_{tran_1}_{tran_2}_{net}
    sorted_names = sorted([tran_i_name, tran_j_name])
    sorted_1, sorted_2 = sorted_names[0], sorted_names[1]

    for net in finfet.circuit.get_nets(with_power_ground=False):
        conn = net.connected_transistors
        if (tran_i_name, "gate") in conn and (tran_j_name, "gate") in conn:
            gate_key = f"gate_share_{sorted_1}_{sorted_2}_{net.name}"
            results.append((net.name, gate_key))

    return results


def _trace_path_flip_assignments(finfet, path):
    """
    Enumerate all consistent flip assignments for a path of transistors.
    Both same-MOS (diffusion sharing) and cross-MOS (LISD sharing) consecutive
    pairs contribute to flip constraints.  Cross-MOS gate sharing is also
    collected (gates sit at the center column so no flip constraint is needed,
    only vertical alignment which the path already enforces).

    Uses backtracking to find all valid combinations where each transistor's
    flip value is consistent across all pairs it appears in.

    Returns list of valid traces:
    [
        {
            'flips': {tran_name: 0 or 1, ...},
            'sharing': [(tran_i, tran_j, net_name, sharing_type, ds_key), ...],
            'lisd_sharing': [(tran_i, tran_j, net_name, sharing_type, lisd_key), ...],
            'gate_sharing': [(tran_i, tran_j, net_name, gate_key), ...],
        },
        ...
    ]
    """
    # Collect ALL consecutive pairs with their options and types
    constrained_pairs = []
    options_per_pair = []
    pair_kinds = []  # 'ds' or 'lisd'

    # Gate sharing keys per cross-MOS pair (no flip constraint, collected once)
    cross_mos_gate_keys = []  # list of (tran_i, tran_j, net_name, gate_key)

    for tran_i_name, tran_j_name in pairwise(path):
        tran_i = finfet.circuit.transistors[tran_i_name]
        tran_j = finfet.circuit.transistors[tran_j_name]
        if tran_i.model == tran_j.model:
            opts = _get_diffusion_sharing_options(finfet, tran_i_name, tran_j_name)
            constrained_pairs.append((tran_i_name, tran_j_name))
            options_per_pair.append(opts)
            pair_kinds.append('ds')
        # Cross-MOS pairs: no LISD/gate constraints from the path.
        # In parallel P/N layout the two chains are independently ordered
        # within a shared range; actual LISD/gate sharing is resolved by
        # the global model when columns happen to align.

    if not constrained_pairs:
        return [{'flips': {}, 'sharing': [], 'lisd_sharing': [],
                 'gate_sharing': list(cross_mos_gate_keys)}]

    results = []

    def backtrack(pair_idx, flips, ds_sharing, lisd_sharing):
        if pair_idx == len(constrained_pairs):
            results.append({
                'flips': dict(flips),
                'sharing': list(ds_sharing),
                'lisd_sharing': list(lisd_sharing),
                'gate_sharing': list(cross_mos_gate_keys),
            })
            return

        tran_i, tran_j = constrained_pairs[pair_idx]
        kind = pair_kinds[pair_idx]

        if not options_per_pair[pair_idx]:
            backtrack(pair_idx + 1, flips, ds_sharing, lisd_sharing)
            return

        for option in options_per_pair[pair_idx]:
            net_name, stype, flip_i, flip_j, key = option

            if tran_i in flips and flips[tran_i] != flip_i:
                continue
            if tran_j in flips and flips[tran_j] != flip_j:
                continue

            new_flips = dict(flips)
            new_flips[tran_i] = flip_i
            new_flips[tran_j] = flip_j

            new_ds = list(ds_sharing)
            new_lisd = list(lisd_sharing)
            if kind == 'ds':
                new_ds.append((tran_i, tran_j, net_name, stype, key))
            else:
                new_lisd.append((tran_i, tran_j, net_name, stype, key))

            backtrack(pair_idx + 1, new_flips, new_ds, new_lisd)

    backtrack(0, {}, [], [])
    return results


def _is_valid_path_transition_by_model(finfet, path):
    """
    Check valid P/N ordering using actual transistor model types (not name prefix).
    """
    block = []
    for tran_name in path:
        tran = finfet.circuit.transistors[tran_name]
        block.append('P' if tran.model == Model.PMOS else 'N')
    return block == sorted(block) or block == sorted(block, reverse=True)


def _find_cluster_paths_complete(finfet, cluster):
    """
    Find all valid transistor orderings for a cluster using the COMPLETE circuit graph
    (including all net nodes, VDD/VSS, and 2-pin nets).
    """
    G_complete = finfet.circuit.generate_networkx_graph()

    transistor_names = set(finfet.circuit.transistors.keys())
    cluster_set = set(cluster)

    neighbors = set()
    for node in cluster:
        if node in G_complete:
            neighbors.update(G_complete.neighbors(node))
    neighbors -= cluster_set

    all_nodes = list(cluster_set | neighbors)
    subg = G_complete.subgraph(all_nodes)

    if not nx.is_connected(subg):
        logger.warning(
            f"Complete subgraph for cluster {cluster} is not connected, "
            f"cannot find paths."
        )
        return []

    cutoff = len(cluster) * 6
    paths = []
    for i in range(len(cluster)):
        src = cluster[i]
        for j in range(i + 1, len(cluster)):
            tgt = cluster[j]
            for path in nx.all_simple_paths(subg, source=src, target=tgt, cutoff=cutoff):
                tran_path = [node for node in path if node in transistor_names]
                if set(tran_path) == cluster_set and len(tran_path) == len(cluster):
                    paths.append(tran_path)

    valid_paths = [p for p in paths if _is_valid_path_transition_by_model(finfet, p)]
    unique_paths = [list(p) for p in {tuple(p) for p in valid_paths}]

    result = []
    for p in unique_paths:
        result.append(p)
        result.append(list(reversed(p)))

    logger.info(
        f"Complete graph path search for cluster {cluster}: "
        f"found {len(paths)} raw, {len(valid_paths)} valid P/N, "
        f"{len(unique_paths)} unique, {len(result)} with reverses"
    )
    return result


def _find_subgroup_orderings(finfet, subgroup):
    """
    Find all valid orderings of a same-MOS subgroup where every consecutive
    pair shares at least one source/drain net (eligible for diffusion sharing).
    """
    if len(subgroup) <= 1:
        return [list(subgroup)]

    valid = []
    for perm in permutations(subgroup):
        ok = True
        for i in range(len(perm) - 1):
            shared = _find_shared_sd_nets(finfet, perm[i], perm[i + 1])
            if not shared:
                ok = False
                break
        if ok:
            valid.append(list(perm))
    return valid


def _process_subgroup_path(finfet, cluster_idx, subgroup_type, path_id, path, trace):
    """
    Create a BoolVar for a same-MOS subgroup path with a consistent flip trace.
    Adds position constraints (left-of adjacency), flip constraints, and
    ds_pair_var activations.
    """
    var = finfet.opt.NewBoolVar(
        f"cluster_{cluster_idx}_{subgroup_type}_path_{path_id}"
    )

    logger.info(f"\t  {subgroup_type} path-trace {path_id}: {path}")
    if trace['flips']:
        flip_str = ", ".join(
            f"{t}={'FLIPPED' if f else 'NOT-FLIPPED'}"
            for t, f in trace['flips'].items()
        )
        logger.info(f"\t    Flips: [{flip_str}]")
    else:
        logger.info(f"\t    Flips: [none]")
    for ti, tj, net, st, dk in trace['sharing']:
        dk_exists = hasattr(finfet, 'ds_pair_vars') and dk in finfet.ds_pair_vars
        logger.info(
            f"\t    DS sharing: {ti} -- {net}({st}) -- {tj}  "
            f"key={dk} exists={dk_exists}"
        )

    # Position constraints: consecutive must be left-of adjacent
    for tran_i_name, tran_j_name in pairwise(path):
        tvar_i = finfet.transistor_vars[tran_i_name]
        tvar_j = finfet.transistor_vars[tran_j_name]
        finfet.opt.Add(
            tvar_j.x_var == tvar_i.x_var + (_NUM_COL_SDG_ - 1)
        ).OnlyEnforceIf(var)

    # Flip constraints from trace
    for tran_name, flip_val in trace['flips'].items():
        tvar = finfet.transistor_vars[tran_name]
        finfet.opt.Add(tvar.flip_var == flip_val).OnlyEnforceIf(var)

    # Activate ds_pair_vars from trace
    if hasattr(finfet, 'ds_pair_vars'):
        for tran_i, tran_j, net_name, stype, ds_key in trace['sharing']:
            if ds_key in finfet.ds_pair_vars:
                finfet.opt.Add(finfet.ds_pair_vars[ds_key] == 1).OnlyEnforceIf(var)

    return var


def _process_long_path(finfet, cluster_idx, path_id, path, trace):
    """
    Create a BoolVar for a longer path with a specific consistent flip trace.
    Adds same-MOS adjacency constraints (left-of) and flip/DS sharing activations.
    Cross-MOS column alignment is NOT enforced here; the parallel P/N layout is
    bounded by _process_cluster_range using max(|PMOS|, |NMOS|) columns.
    """
    var = finfet.opt.NewBoolVar(f"cluster_{cluster_idx}_long_path_{path_id}")

    logger.info(f"\t  Path-trace {path_id}: {path}")
    if trace['flips']:
        flip_str = ", ".join(
            f"{t}={'FLIPPED' if f else 'NOT-FLIPPED'}" for t, f in trace['flips'].items()
        )
        logger.info(f"\t    Flips: [{flip_str}]")
    else:
        logger.info(f"\t    Flips: [none - no constrained pairs]")
    if trace['sharing']:
        for ti, tj, net, st, dk in trace['sharing']:
            dk_exists = hasattr(finfet, 'ds_pair_vars') and dk in finfet.ds_pair_vars
            logger.info(
                f"\t    DS sharing: {ti} -- {net}({st}) -- {tj}  "
                f"key={dk} exists={dk_exists}"
            )
    else:
        logger.info(f"\t    DS sharing: [none]")
    if trace.get('lisd_sharing'):
        for ti, tj, net, st, lk in trace['lisd_sharing']:
            lk_exists = hasattr(finfet, 'lisd_share_pair_vars') and lk in finfet.lisd_share_pair_vars
            logger.info(
                f"\t    LISD sharing: {ti} -- {net}({st}) -- {tj}  "
                f"key={lk} exists={lk_exists}"
            )
    else:
        logger.info(f"\t    LISD sharing: [none]")
    if trace.get('gate_sharing'):
        for ti, tj, net, gk in trace['gate_sharing']:
            gk_exists = hasattr(finfet, 'gate_share_pair_vars') and gk in finfet.gate_share_pair_vars
            logger.info(
                f"\t    Gate sharing: {ti} -- {net} -- {tj}  "
                f"key={gk} exists={gk_exists}"
            )
    else:
        logger.info(f"\t    Gate sharing: [none]")

    # Position constraints for each consecutive pair
    for tran_i_name, tran_j_name in pairwise(path):
        tran_i = finfet.circuit.transistors[tran_i_name]
        tran_j = finfet.circuit.transistors[tran_j_name]
        tvar_i = finfet.transistor_vars[tran_i_name]
        tvar_j = finfet.transistor_vars[tran_j_name]

        if tran_i.model == tran_j.model:
            finfet.opt.Add(
                tvar_j.x_var == tvar_i.x_var + (_NUM_COL_SDG_ - 1)
            ).OnlyEnforceIf(var)
        # Cross-MOS: no position constraint. The parallel P/N layout is
        # bounded by _process_cluster_range; each chain's internal order
        # is determined by the same-MOS adjacency above.

    # Flip constraints from trace (same-MOS DS sharing only)
    for tran_name, flip_val in trace['flips'].items():
        tvar = finfet.transistor_vars[tran_name]
        finfet.opt.Add(tvar.flip_var == flip_val).OnlyEnforceIf(var)

    # Activate ds_pair_vars from trace (same-MOS diffusion sharing)
    if hasattr(finfet, 'ds_pair_vars'):
        for tran_i, tran_j, net_name, stype, ds_key in trace['sharing']:
            if ds_key in finfet.ds_pair_vars:
                finfet.opt.Add(finfet.ds_pair_vars[ds_key] == 1).OnlyEnforceIf(var)

    # Cross-MOS LISD/gate sharing is NOT activated here - without
    # column alignment the path cannot guarantee which columns overlap.
    # The global model will detect and reward sharing opportunistically.

    return var


def _process_path(finfet, pid, path):
    var = finfet.opt.NewBoolVar(f"cluster_path_{path}_id_{pid}")
    for tran_i_name, tran_j_name in pairwise(path):
        tran_i_name = str(tran_i_name)
        tran_j_name = str(tran_j_name)
        # entity
        tran_i = finfet.circuit.transistors[tran_i_name]
        tran_j = finfet.circuit.transistors[tran_j_name]
        # variable
        tvar_i = finfet.transistor_vars[tran_i_name]
        tvar_j = finfet.transistor_vars[tran_j_name]
        flip_i = tvar_i.flip_var
        flip_j = tvar_j.flip_var

        # Collect shared source/drain nets (including VDD/VSS)
        shared_sd = _get_shared_sd_nets(finfet, tran_i_name, tran_j_name)

        # ---- Same-type: horizontal adjacency (i LEFT of j) ----
        if tran_i.model == tran_j.model:
            # i left of j
            finfet.opt.Add(
                tvar_j.x_var == tvar_i.x_var + (_NUM_COL_SDG_ - 1)
            ).OnlyEnforceIf(var)

            # Diffusion-sharing flip constraints
            if shared_sd:
                # One selector per shared (net, terminal_i, terminal_j) combo;
                # exactly one must be active when this path is chosen.
                selectors = []
                for idx, (net_name, ti_term, tj_term) in enumerate(shared_sd):
                    sel = finfet.opt.NewBoolVar(
                        f"cluster_ds_sel_{pid}_{tran_i_name}_{tran_j_name}_{net_name}_{ti_term}_{tj_term}_{idx}"
                    )
                    selectors.append(sel)

                    # Flip logic: tran_i is LEFT, tran_j is RIGHT.
                    # The touching boundary is tran_i's RIGHT terminal and
                    # tran_j's LEFT terminal; they must carry the same net.
                    #
                    # Layout reminder (flip=0): [Source | Gate | Drain]
                    #                (flip=1): [Drain  | Gate | Source]
                    # So: right-side terminal when flip=0 is drain,
                    #     right-side terminal when flip=1 is source.
                    #     left-side terminal when flip=0 is source,
                    #     left-side terminal when flip=1 is drain.
                    #
                    # For tran_i's <ti_term> to face RIGHT:
                    #   ti_term=source => flip_i=1  (source goes right)
                    #   ti_term=drain  => flip_i=0  (drain goes right)
                    # For tran_j's <tj_term> to face LEFT:
                    #   tj_term=source => flip_j=0  (source goes left)
                    #   tj_term=drain  => flip_j=1  (drain goes left)

                    # tran_i flip
                    if ti_term == "source":
                        finfet.opt.AddImplication(sel, flip_i).OnlyEnforceIf(var)
                    else:  # drain
                        finfet.opt.AddImplication(sel, flip_i.Not()).OnlyEnforceIf(var)
                    # tran_j flip
                    if tj_term == "source":
                        finfet.opt.AddImplication(sel, flip_j.Not()).OnlyEnforceIf(var)
                    else:  # drain
                        finfet.opt.AddImplication(sel, flip_j).OnlyEnforceIf(var)

                # Exactly one shared net is at the boundary when path is active
                finfet.opt.Add(sum(selectors) == 1).OnlyEnforceIf(var)
            else:
                logger.warning(
                    f"Path {pid}: same-type pair ({tran_i_name}, {tran_j_name}) "
                    f"has no shared S/D net — diffusion sharing impossible"
                )

        # ---- Cross-type: vertical alignment (same x) ----
        else:
            # P-N or N-P: align columns
            finfet.opt.Add(tvar_j.x_var == tvar_i.x_var).OnlyEnforceIf(var)

            # LISD-style flip constraints for shared S/D nets
            # (exclude power/ground - LISD sharing is for internal nets only)
            internal_shared = [
                (n, ti, tj) for (n, ti, tj) in shared_sd
                if not finfet.circuit.nets[n].is_power_or_ground_net()
            ]
            if internal_shared:
                selectors = []
                for idx, (net_name, ti_term, tj_term) in enumerate(internal_shared):
                    sel = finfet.opt.NewBoolVar(
                        f"cluster_lisd_sel_{pid}_{tran_i_name}_{tran_j_name}_{net_name}_{ti_term}_{tj_term}_{idx}"
                    )
                    selectors.append(sel)

                    # Vertically aligned: same column, different rows.
                    # For the shared terminal to occupy the same column:
                    #   same terminal type (source-source, drain-drain) => same flip
                    #   different terminal type (source-drain, drain-source) => opposite flip
                    if ti_term == tj_term:  # source-source or drain-drain
                        finfet.opt.Add(flip_i == flip_j).OnlyEnforceIf([var, sel])
                    else:  # source-drain or drain-source
                        finfet.opt.Add(flip_i != flip_j).OnlyEnforceIf([var, sel])

                # At most one LISD sharing net at this column when path active
                finfet.opt.Add(sum(selectors) <= 1).OnlyEnforceIf(var)

    return var


def _is_valid_path_transition(finfet, path):
    """
    All travel path should just be handled in ordered P/N because we do not want zigzag paths.
    Uses actual transistor model type instead of name prefix for robustness.
    """
    block = []
    for item in path:
        if item in finfet.circuit.transistors:
            tran = finfet.circuit.transistors[item]
            if tran.model == Model.NMOS:
                block.append('N')
            elif tran.model == Model.PMOS:
                block.append('P')
    # Valid if block is all Ns then all Ps or all Ps then all Ns
    return block == sorted(block) or block == sorted(block, reverse=True)


def eulerian_subpaths(finfet, netlist_graph, cluster):
    """
    TODO: improve eulerian path finding.
    - Do not mix P/N order when constructing path as it may be spatially similar to other paths
    - If using clique model for VDD/VSS, then VDD/VSS node must be traveled consecutively for correctness
    - remove duplicated path
    - add reverse path
    - Optionally, use cluster constraint instead (min/max on x range)
    """
    # extract subgraph only containing the nodes
    # and immediate connected neighbors
    # Get one-hop neighbors of the subset
    neighbors = set()
    for node in cluster:
        neighbors.update(netlist_graph.neighbors(node))
    # Remove original subset nodes (if needed)
    neighbors -= set(cluster)

    subg = netlist_graph.subgraph(cluster + list(neighbors))
    # if not connected
    if not nx.is_connected(subg):
        return []
    # get all paths
    paths = []
    for i in range(len(cluster)):
        src = cluster[i]
        for j in range(i, len(cluster)):
            tgt = cluster[j]
            for path in nx.all_simple_paths(subg, source=src, target=tgt):
                if set(cluster).issubset(path):
                    # filter out path that contains net node (keep only actual transistors)
                    paths.append([node for node in path if node in finfet.circuit.transistors])
    # ^ remove paths that are mixing P and N orders
    new_paths = []
    for p in paths:
        if _is_valid_path_transition(finfet, p):
            new_paths.append(p)
    # ^ remove duplicated paths
    unique_paths = [list(p) for p in {tuple(path) for path in new_paths}]
    # add reversed paths
    path_w_reverse = []
    for p in unique_paths:
        path_w_reverse.append(p)
        path_w_reverse.append(list(reversed(p)))
    return path_w_reverse


def _process_cluster_range(finfet, cluster):
    """
    Constrain cluster transistors to be placed within a compact x-range.
    Uses the larger group (PMOS or NMOS) to define the range bounds.
    """
    if not cluster:
        logger.warning("Empty cluster passed to _process_cluster_range")
        return

    min_x_var = finfet.opt.NewIntVarFromDomain(
        finfet.domain_mos_placable_ci,
        f"cluster_min_x_{cluster}"
    )
    max_x_var = finfet.opt.NewIntVarFromDomain(
        finfet.domain_mos_placable_ci,
        f"cluster_max_x_{cluster}"
    )

    # Separate into PMOS and NMOS groups
    pmos_group = []
    nmos_group = []
    for tran_name in cluster:
        if finfet.circuit.transistors[tran_name].model == Model.PMOS:
            pmos_group.append(tran_name)
        elif finfet.circuit.transistors[tran_name].model == Model.NMOS:
            nmos_group.append(tran_name)

    # Handle homogeneous clusters (all PMOS or all NMOS)
    if not pmos_group:  # Only NMOS
        nmos_xs = [finfet.transistor_vars[nmos_tran_name].x_var for nmos_tran_name in nmos_group]
        finfet.opt.AddMinEquality(min_x_var, nmos_xs)
        finfet.opt.AddMaxEquality(max_x_var, nmos_xs)
        finfet.opt.Add(max_x_var - min_x_var == int((_NUM_COL_SDG_ - 1) * (len(nmos_group) - 1)))
        return

    if not nmos_group:  # Only PMOS
        pmos_xs = [finfet.transistor_vars[pmos_tran_name].x_var for pmos_tran_name in pmos_group]
        finfet.opt.AddMinEquality(min_x_var, pmos_xs)
        finfet.opt.AddMaxEquality(max_x_var, pmos_xs)
        finfet.opt.Add(max_x_var - min_x_var == int((_NUM_COL_SDG_ - 1) * (len(pmos_group) - 1)))
        return

    # Mixed PMOS and NMOS: use larger group to define range
    if len(pmos_group) >= len(nmos_group):
        pmos_xs = [finfet.transistor_vars[pmos_tran_name].x_var for pmos_tran_name in pmos_group]
        # placement range
        finfet.opt.AddMinEquality(
            min_x_var,
            pmos_xs,
        )
        finfet.opt.AddMaxEquality(
            max_x_var,
            pmos_xs,
        )
        # bag everything within range regardless order
        finfet.opt.Add(max_x_var - min_x_var == int((_NUM_COL_SDG_ - 1) * (len(pmos_group) - 1)))
        # nmos within such range
        for nmos_tran_name in nmos_group:
            nmos_tvar = finfet.transistor_vars[nmos_tran_name].x_var
            finfet.opt.Add(min_x_var <= nmos_tvar)
            finfet.opt.Add(nmos_tvar <= max_x_var)
    elif len(pmos_group) < len(nmos_group):
        nmos_xs = [finfet.transistor_vars[nmos_tran_name].x_var for nmos_tran_name in nmos_group]
        # placement range
        finfet.opt.AddMinEquality(
            min_x_var,
            nmos_xs,
        )
        finfet.opt.AddMaxEquality(
            max_x_var,
            nmos_xs,
        )
        # bag everything within range regardless order
        finfet.opt.Add(max_x_var - min_x_var == int((_NUM_COL_SDG_ - 1) * (len(nmos_group) - 1)))
        # pmos within such range
        for pmos_tran_name in pmos_group:
            pmos_tvar = finfet.transistor_vars[pmos_tran_name].x_var
            finfet.opt.Add(min_x_var <= pmos_tvar)
            finfet.opt.Add(pmos_tvar <= max_x_var)


def inject_clusters(finfet, graph, clusters, use_path_trace=True):
    """
    Inject cluster constraints into the provided FinFET instance.

    Args:
        finfet: The FinFET instance
        graph: NetworkX graph of the circuit
        clusters: List of clusters (each cluster is a list of transistor names)
        use_path_trace: If True, use path-based flip+diffusion sharing constraints.
                        If False, fall back to range-only constraints.
    """
    for idx, clst in enumerate(clusters):
        if len(clst) < 2:
            logger.warning(f"Cluster {clst} has fewer than 2 transistors - skipping")
            continue

        logger.info(f"Injecting Clusters: {clst}")
        finfet.opt.log_comment(f"Injecting Clusters: {clst}")

        if not use_path_trace:
            logger.info(f"Path tracing disabled, using range-only for cluster {clst}")
            _process_cluster_range(finfet, clst)
            continue

        if len(clst) == 2:  # small cluster constrain placement adjacency
            paths = eulerian_subpaths(finfet, graph, clst)
            # Also try complete graph to cover paths via VDD/VSS or 2-pin nets
            complete_paths = _find_cluster_paths_complete(finfet, clst)
            # Union and deduplicate
            all_path_tuples = {tuple(p) for p in (paths or [])} | {tuple(p) for p in complete_paths}
            paths = [list(p) for p in all_path_tuples]
            if paths:
                logger.info(f"clst has paths {paths}")
            if not paths:
                logger.warning(f"No valid paths found for cluster {clst}")
                continue
            pids = []
            for pid, p in enumerate(paths):
                path_var = _process_path(finfet, pid, p)
                pids.append(path_var)
            # exactly one
            finfet.opt.Add(sum(pids) == 1)
        else:  # larger cluster: try full euler paths first, then split-by-MOS fallback
            pmos_group = [
                t for t in clst
                if finfet.circuit.transistors[t].model == Model.PMOS
            ]
            nmos_group = [
                t for t in clst
                if finfet.circuit.transistors[t].model == Model.NMOS
            ]
            logger.info(
                f"Large cluster (size={len(clst)}): "
                f"PMOS={pmos_group}, NMOS={nmos_group}"
            )
            finfet.opt.log_comment(
                f"Large cluster: {clst} "
                f"PMOS={pmos_group}, NMOS={nmos_group}"
            )

            # --- Mixed clusters: split-by-MOS + range constraint ---
            # Full euler paths are NOT used for mixed clusters because they
            # couple P and N chain orderings into a single sequence, preventing
            # the optimal parallel layout where each chain is independently
            # ordered within max(|PMOS|, |NMOS|) columns.
            full_path_success = False
            is_mixed = len(pmos_group) > 0 and len(nmos_group) > 0

            # --- Phase 2: Split-by-MOS + range (always used for mixed clusters) ---
            if not full_path_success:
                logger.info(
                    f"\tUsing split-by-MOS approach for cluster"
                )
                any_subgroup_constrained = False

                for subgroup_type, subgroup in [("PMOS", pmos_group), ("NMOS", nmos_group)]:
                    if len(subgroup) < 2:
                        continue

                    orderings = _find_subgroup_orderings(finfet, subgroup)
                    logger.info(
                        f"\t{subgroup_type} subgroup {subgroup}: "
                        f"{len(orderings)} valid orderings"
                    )
                    if not orderings:
                        logger.warning(
                            f"\tNo valid orderings for {subgroup_type} "
                            f"subgroup {subgroup}"
                        )
                        continue

                    all_vars = []
                    pid = 0
                    for ordering in orderings:
                        traces = _trace_path_flip_assignments(finfet, ordering)
                        if not traces:
                            logger.info(
                                f"\t  Ordering {ordering}: no consistent flip trace"
                            )
                            continue
                        for trace in traces:
                            var = _process_subgroup_path(
                                finfet, idx, subgroup_type, pid, ordering, trace
                            )
                            all_vars.append(var)
                            pid += 1

                    if all_vars:
                        finfet.opt.Add(sum(all_vars) == 1)
                        any_subgroup_constrained = True
                        logger.info(
                            f"\t{subgroup_type} subgroup: {len(all_vars)} "
                            f"path-traces, exactly 1 must be active"
                        )
                    else:
                        logger.warning(
                            f"\tNo valid path-traces for {subgroup_type} "
                            f"subgroup {subgroup}"
                        )

                # Cross-MOS proximity: minority group within dominant group's range
                if is_mixed:
                    _process_cluster_range(finfet, clst)

                if not any_subgroup_constrained:
                    logger.warning(
                        f"No subgroups constrained for cluster {clst}, "
                        f"range-only fallback"
                    )


def _process_soft_cluster_range(finfet, idx, cluster):
    """
    Soft version of range constraint. Returns bool var True when cluster is contiguous.
    """
    pmos_group = [t for t in cluster if finfet.circuit.transistors[t].model == Model.PMOS]
    nmos_group = [t for t in cluster if finfet.circuit.transistors[t].model == Model.NMOS]

    if len(pmos_group) >= len(nmos_group):
        dom_group = pmos_group
    else:
        dom_group = nmos_group

    if len(dom_group) < 2:
        return None

    min_x_var = finfet.opt.NewIntVarFromDomain(
        finfet.domain_mos_placable_ci, f"soft_cluster_{idx}_min_x"
    )
    max_x_var = finfet.opt.NewIntVarFromDomain(
        finfet.domain_mos_placable_ci, f"soft_cluster_{idx}_max_x"
    )

    dom_xs = [finfet.transistor_vars[t].x_var for t in dom_group]
    finfet.opt.AddMinEquality(min_x_var, dom_xs)
    finfet.opt.AddMaxEquality(max_x_var, dom_xs)

    expected_span = int((_NUM_COL_SDG_ - 1) * (len(dom_group) - 1))

    cluster_var = finfet.opt.NewBoolVar(f"soft_cluster_{idx}_valid")
    span_var = finfet.opt.NewIntVar(0, max(finfet.plc_ci), f"soft_cluster_{idx}_span")
    finfet.opt.Add(span_var == max_x_var - min_x_var)

    finfet.opt.Add(span_var == expected_span).OnlyEnforceIf(cluster_var)
    finfet.opt.Add(span_var != expected_span).OnlyEnforceIf(cluster_var.Not())

    return cluster_var


def inject_soft_clusters(finfet, graph, clusters, use_path_trace=True):
    """
    Inject soft cluster constraints as objective rewards.
    Valid clusters (continuous diffusion) are rewarded when placed together.

    Args:
        finfet: The FinFET instance
        graph: NetworkX graph of the circuit
        clusters: List of clusters (each cluster is a list of transistor names)
        use_path_trace: If True, use path-based flip+diffusion sharing constraints.
                        If False, fall back to range-only soft constraints.
    """
    finfet.soft_cluster_vars = []

    for idx, clst in enumerate(clusters):
        logger.info(f"Injecting Soft Cluster {idx}: {clst}")
        finfet.opt.log_comment(f"Soft Cluster {idx}: {clst}")

        if not use_path_trace:
            logger.info(f"Path tracing disabled, using range-only for soft cluster {clst}")
            cluster_var = _process_soft_cluster_range(finfet, idx, clst)
            if cluster_var is None:
                continue
            finfet.soft_cluster_vars.append((cluster_var, clst))
            logger.info(f"Soft cluster {idx} created (range-only): {clst}")
            continue

        if len(clst) == 2:
            euler_paths = eulerian_subpaths(finfet, graph, clst) or []
            complete_paths = _find_cluster_paths_complete(finfet, clst)
            all_path_tuples = {tuple(p) for p in euler_paths} | {tuple(p) for p in complete_paths}
            paths = [list(p) for p in all_path_tuples]
            if not paths:
                continue

            pids = []
            for pid, p in enumerate(paths):
                path_var = _process_path(finfet, pid, p)
                pids.append(path_var)

            cluster_var = finfet.opt.NewBoolVar(f"soft_cluster_{idx}_used")
            finfet.opt.Add(sum(pids) == 1).OnlyEnforceIf(cluster_var)
            finfet.opt.Add(sum(pids) == 0).OnlyEnforceIf(cluster_var.Not())

        else:
            pmos_group = [
                t for t in clst
                if finfet.circuit.transistors[t].model == Model.PMOS
            ]
            nmos_group = [
                t for t in clst
                if finfet.circuit.transistors[t].model == Model.NMOS
            ]
            logger.info(
                f"Soft large cluster (size={len(clst)}): "
                f"PMOS={pmos_group}, NMOS={nmos_group}"
            )
            finfet.opt.log_comment(
                f"Soft large cluster: {clst} "
                f"PMOS={pmos_group}, NMOS={nmos_group}"
            )

            is_mixed = len(pmos_group) > 0 and len(nmos_group) > 0
            full_path_success = False

            # --- Mixed clusters: split-by-MOS + range ---
            # Full euler paths couple P and N orderings, preventing
            # optimal parallel layout. Skip Phase 1 for mixed clusters.

            # --- Phase 2: Split-by-MOS fallback ---
            if not full_path_success:
                subgroup_vars = []

                for subgroup_type, subgroup in [("PMOS", pmos_group), ("NMOS", nmos_group)]:
                    if len(subgroup) < 2:
                        continue

                    orderings = _find_subgroup_orderings(finfet, subgroup)
                    logger.info(
                        f"\t{subgroup_type} subgroup {subgroup}: "
                        f"{len(orderings)} valid orderings"
                    )
                    if not orderings:
                        logger.warning(
                            f"\tNo valid orderings for {subgroup_type} "
                            f"subgroup {subgroup} (soft)"
                        )
                        continue

                    all_path_vars = []
                    pid = 0
                    for ordering in orderings:
                        traces = _trace_path_flip_assignments(finfet, ordering)
                        if not traces:
                            logger.info(
                                f"\t  Ordering {ordering}: no consistent flip trace (soft)"
                            )
                            continue
                        for trace in traces:
                            var = _process_subgroup_path(
                                finfet, f"soft_{idx}", subgroup_type, pid,
                                ordering, trace
                            )
                            all_path_vars.append(var)
                            pid += 1

                    if all_path_vars:
                        sub_var = finfet.opt.NewBoolVar(
                            f"soft_cluster_{idx}_{subgroup_type}_used"
                        )
                        finfet.opt.Add(
                            sum(all_path_vars) == 1
                        ).OnlyEnforceIf(sub_var)
                        finfet.opt.Add(
                            sum(all_path_vars) == 0
                        ).OnlyEnforceIf(sub_var.Not())
                        subgroup_vars.append(sub_var)
                        logger.info(
                            f"\t{subgroup_type} subgroup: {len(all_path_vars)} "
                            f"soft path-traces"
                        )
                    else:
                        logger.warning(
                            f"\tNo valid path-traces for {subgroup_type} "
                            f"subgroup {subgroup} (soft)"
                        )

                if not subgroup_vars:
                    cluster_var = _process_soft_cluster_range(finfet, idx, clst)
                    if cluster_var is None:
                        continue
                elif len(subgroup_vars) == 1:
                    cluster_var = subgroup_vars[0]
                else:
                    cluster_var = finfet.opt.NewBoolVar(f"soft_cluster_{idx}_used")
                    finfet.opt.AddBoolAnd(subgroup_vars).OnlyEnforceIf(cluster_var)
                    finfet.opt.AddBoolOr(
                        [v.Not() for v in subgroup_vars]
                    ).OnlyEnforceIf(cluster_var.Not())

        finfet.soft_cluster_vars.append((cluster_var, clst))
        logger.info(f"Soft cluster {idx} created: {clst}")


def inject_placement(finfet, tran_name, x=None, y=None, flip=None):
    """
    Inject a placement solution into the model for a single transistor.
    """
    finfet.opt.log_comment(f"Injecting placement for {tran_name} ...")
    if tran_name not in finfet.transistor_vars:
        logger.error(f"Transistor {tran_name} not found in the model. Please check the name.")
        sys.exit(1)
    if x is not None:
        finfet.opt.Add(finfet.transistor_vars[tran_name].x_var == x)
    if y is not None:
        finfet.opt.Add(finfet.transistor_vars[tran_name].y_var == y)
    if flip is not None:
        if flip:
            finfet.opt.Add(finfet.transistor_vars[tran_name].flip_var == 1)
        else:
            finfet.opt.Add(finfet.transistor_vars[tran_name].flip_var == 0)


def inject_placements(finfet, tran_names, xs, flips, hint=False):
    """
    Inject placement solutions into the model for multiple transistors.
    """
    if not all(isinstance(param, list) for param in [tran_names, xs, flips]):
        logger.error("All parameters (tran_names, xs, flips) must be lists.")
        sys.exit(1)

    if not (len(tran_names) == len(xs) == len(flips)):
        logger.error(
            f"All lists must have the same length. Got: tran_names({len(tran_names)}), xs({len(xs)}), flips({len(flips)})"
        )
        sys.exit(1)

    finfet.opt.log_comment(f"{'Hinting' if hint else 'Injecting'} placement for transistors: {tran_names} ...")

    for name in tran_names:
        if name not in finfet.transistor_vars:
            logger.error(f"Transistor {name} not found in the model. Please check the name.")
            sys.exit(1)

    for i in range(len(tran_names)):
        name = tran_names[i]
        x = xs[i]
        flip = flips[i]

        if hint:
            finfet.opt.AddHint(finfet.transistor_vars[name].x_var, x)
            finfet.opt.AddHint(finfet.transistor_vars[name].flip_var, 1 if flip else 0)
        else:
            finfet.opt.Add(finfet.transistor_vars[name].x_var == x)
            finfet.opt.Add(finfet.transistor_vars[name].flip_var == (1 if flip else 0))


def inject_edge(finfet, u, v, value=1):
    finfet.opt.log_comment(f"Injecting edge ({u}, {v}) ...")
    if (u, v) not in finfet.edge_vars:
        logger.error(f"Edge ({u}, {v}) not found in the model. Please check the coordinates.")
        sys.exit(1)
    finfet.opt.Add(finfet.edge_vars[(u, v)] == value)


def inject_flow(finfet, net_name, k_idx, u, v, value=1):
    finfet.opt.log_comment(f"Injecting flow ({net_name}, {k_idx}, {u}, {v}) ...")
    if (net_name, k_idx, u, v) not in finfet.net_flow_vars:
        logger.error(f"Flow ({net_name}, {k_idx}, {u}, {v}) not found in the model. Please check the coordinates.")
        logger.error(f"Possible keys under net_name {net_name} k_idx {k_idx}")
        for key in finfet.net_flow_vars.keys():
            if key[0] == net_name and key[1] == k_idx:
                logger.error(f"Key: {key}")
        sys.exit(1)
    finfet.opt.Add(finfet.net_flow_vars[(net_name, k_idx, u, v)] == value)


def inject_arc(finfet, net_name, u, v, value=1):
    finfet.opt.log_comment(f"Injecting arc ({net_name}, {u}, {v}) ...")
    if (net_name, u, v) not in finfet.net_arc_vars:
        logger.error(f"Arc ({net_name}, {u}, {v}) not found in the model. Please check the coordinates.")
        sys.exit(1)
    finfet.opt.Add(finfet.net_arc_vars[(net_name, u, v)] == value)


def inject_pin_order(finfet, pin_order):
    """
    Enforce pin ordering from left to right based on SON variables.

    Args:
        finfet: The FinFET instance
        pin_order: List of pin/net names in the desired left-to-right order.
                   e.g., ["A1", "A2", "Z"] means A1 is leftmost, Z is rightmost.

    This method enforces that for each pair of adjacent pins in the order,
    the left pin's column is strictly less than the right pin's column.
    """
    finfet.opt.log_comment(f"Enforcing pin order: {pin_order} ...")
    logger.info(f"Enforcing pin order: {pin_order}")

    # Validate that all pins are valid IO nets
    for pin_name in pin_order:
        if pin_name not in finfet.node_is_SON_vars:
            logger.error(f"Pin {pin_name} not found in SON variables. Available pins: {list(finfet.node_is_SON_vars.keys())}")
            return

    # Create an integer variable for each pin's column location
    pin_col_vars = {}
    all_cols = sorted(set(node[2] for node in finfet.son_terminal_nodes["M1"]))

    for pin_name in pin_order:
        # Create an integer variable representing this pin's column
        pin_col_var = finfet.opt.NewIntVarFromDomain(
            cp_model.Domain.FromValues(all_cols),
            f"pin_col_{pin_name}"
        )
        pin_col_vars[pin_name] = pin_col_var

        # Link the column variable to the SON vars
        # For each possible SON location, if SON is active, column var must equal that column
        for k in finfet.node_is_SON_vars[pin_name]:
            for node, son_var in finfet.node_is_SON_vars[pin_name][k].items():
                col = node[2]
                # If SON var is true, then pin_col_var == col
                finfet.opt.Add(pin_col_var == col).OnlyEnforceIf(son_var)

    # Enforce ordering: for each adjacent pair, left pin's column < right pin's column
    for i in range(len(pin_order) - 1):
        left_pin = pin_order[i]
        right_pin = pin_order[i + 1]
        # Strictly less than
        finfet.opt.Add(pin_col_vars[left_pin] < pin_col_vars[right_pin])
        logger.info(f"Enforced: {left_pin} (col) < {right_pin} (col)")


def inject_m0_pin_row_assignment(finfet, assignment):
    """
    Force each M0 pin net's pin access point to a specific M0 row.

    For each net in the assignment:
      1. Force it to remain an M0 pin (m0_pin_var == 1), which means
         no M2 arcs and exactly one V0 via - as defined by pin.m0_pin().
      2. Block V0 vias (M0-M1 arcs) on all M0 rows except the target,
         so the single V0 via must land on the target row.

    General M0 routing is left unconstrained - the net can still use M0
    arcs on any row to reach its transistors.

    Args:
        finfet: The FinFET instance
        assignment: dict {netname: m0_row} mapping each M0 pin net to its target row
    """
    finfet.opt.log_comment(f"Injecting M0 pin row assignment: {assignment} ...")
    logger.info(f"Injecting M0 pin row assignment: {assignment}")

    M0_layer_idx = finfet.lgg.layer_to_idx["M0"]
    M1_layer_idx = finfet.lgg.layer_to_idx["M1"]

    for netname, target_row in assignment.items():
        # 1. Force the net to be an M0 pin (uses m0_pin_vars from pin.m0_pin())
        if netname in finfet.m0_pin_vars:
            finfet.opt.Add(finfet.m0_pin_vars[netname] == 1)
            logger.info(f"  Forced {netname} to be M0 pin")

        # 2. Block V0 vias on non-target rows
        for u, v in finfet.lgg.arcs():
            is_m0_to_m1 = (u[0] == M0_layer_idx and v[0] == M1_layer_idx)
            is_m1_to_m0 = (u[0] == M1_layer_idx and v[0] == M0_layer_idx)
            if not (is_m0_to_m1 or is_m1_to_m0):
                continue

            # M0-side row determines the pin access row
            m0_row = u[1] if u[0] == M0_layer_idx else v[1]

            arc_key = (netname, u, v)
            if arc_key not in finfet.net_arc_vars:
                continue

            if m0_row != target_row:
                finfet.opt.Add(finfet.net_arc_vars[arc_key] == 0)

        logger.info(f"  Constrained {netname} V0 via -> M0 row {target_row}")
