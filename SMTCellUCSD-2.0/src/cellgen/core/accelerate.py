"""
Acceleration and analysis functions for circuit optimization.

This module contains functions for:
- Circuit analysis and transistor grouping
- Circuit clustering for improved placement
- Graph-based circuit representations
"""

import copy
import re
import time
from itertools import pairwise
import networkx as nx
import numpy as np
import matplotlib.pyplot as plt
from collections import defaultdict
from sklearn.cluster import DBSCAN
from loguru import logger

_NUM_COL_SDG_ = 3

# Global flag to enable/disable early stopping
EARLY_STOP = False

# Early stopping configuration knobs
EARLY_STOP_PATIENCE = 5          # Number of consecutive stalls before stopping (2=aggressive, 5=balanced, 10=conservative)
EARLY_STOP_MIN_RUNTIME = 120     # Minimum runtime in seconds before allowing early stop (0=disabled, 60=1min, 120=2min)


class EarlyStoppingCallback:
    """
    Callback to monitor solver output and trigger early stopping when stalling is detected.
    Specifically monitors bool_core outputs and stops when consecutive lines show the same fixed= value.
    
    Configuration knobs:
    - EARLY_STOP_PATIENCE: Number of consecutive stalls required before stopping
    - EARLY_STOP_MIN_RUNTIME: Minimum runtime (seconds) before early stopping is allowed
    """
    def __init__(self, solver, patience=EARLY_STOP_PATIENCE, min_runtime=EARLY_STOP_MIN_RUNTIME):
        self.solver = solver
        self.patience = patience
        self.min_runtime = min_runtime
        self.start_time = time.time()
        self.last_fixed_value = None
        self.stall_count = 0
        self.bool_core_pattern = re.compile(r'bool_core.*fixed=(\d+)/\d+')
        
    def __call__(self, message):
        """Called by the solver for each log message."""
        # Always print the message
        print(message)
        
        # Check if this is a bool_core line
        match = self.bool_core_pattern.search(message)
        if match:
            current_fixed = int(match.group(1))
            
            # Check if this matches the previous bool_core fixed value
            if self.last_fixed_value is not None and current_fixed == self.last_fixed_value:
                self.stall_count += 1
                elapsed_time = time.time() - self.start_time
                
                # Only stop if we've stalled enough times AND met minimum runtime
                if self.stall_count >= self.patience and elapsed_time >= self.min_runtime:
                    logger.warning(f"\n{'='*80}")
                    logger.warning(f"EARLY STOPPING TRIGGERED!")
                    logger.warning(f"Detected {self.stall_count} consecutive stalls with fixed={current_fixed}")
                    logger.warning(f"Elapsed time: {elapsed_time:.1f}s (min required: {self.min_runtime}s)")
                    logger.warning(f"Stopping solver to use current best solution...")
                    logger.warning(f"{'='*80}\n")
                    # Stop the solver
                    self.solver.StopSearch()
                else:
                    # Log the stall but don't stop yet
                    if elapsed_time < self.min_runtime:
                        logger.info(f"Stall {self.stall_count}/{self.patience} detected, but only {elapsed_time:.1f}s elapsed (min: {self.min_runtime}s)")
                    else:
                        logger.info(f"Stall {self.stall_count}/{self.patience} detected at {elapsed_time:.1f}s")
            else:
                # Progress detected, reset stall counter
                if self.stall_count > 0:
                    logger.info(f"Progress detected! Resetting stall counter (was {self.stall_count})")
                self.stall_count = 0
            
            # Update the last fixed value
            self.last_fixed_value = current_fixed
        # Reset if it's not a bool_core line (to only track consecutive bool_core lines)
        elif '#Bound' in message and 'bool_core' not in message:
            self.last_fixed_value = None
            # Don't reset stall_count here to maintain consecutive stall tracking


def analyze_circuit(instance):
    """
    Extract transistor groups from the circuit.
    
    Args:
        instance: The FinFET instance containing circuit and configuration
    """
    instance.net_to_tran_group = instance.circuit.group_transistors_by_nets()
    instance.net_to_pmos_tran_group = instance.net_to_tran_group["PMOS"] 
    instance.net_to_nmos_tran_group = instance.net_to_tran_group["NMOS"]
    instance.tran_group_by_low_degree_nets = instance.circuit.group_transistors_by_low_degree_nets_and_types()
    
    # Check if most pmos are partitioned, if so, then do not use breaking symmetry later on 
    merged_list = []
    for _, value_set in instance.net_to_pmos_tran_group.items():
        merged_list.extend(list(value_set))
    num_added_pmos_partition = len(merged_list)
    merged_list = []
    for _, value_set in instance.net_to_nmos_tran_group.items():
        merged_list.extend(list(value_set))
    num_added_nmos_partition = len(merged_list)    
    
    # NOTE: although BS is not a hard constraints on partition, CP-SAT presolve can result in INFEASIBLE
    if num_added_pmos_partition > instance.circuit.num_pmos_transistors() // 2:
        logger.info(f"Detecting {num_added_pmos_partition} partition is provided for PMOS. Disabled breaking symmetry.")
        instance.use_break_symmetry = False
    elif num_added_nmos_partition > instance.circuit.num_nmos_transistors() // 2:
        logger.info(f"Detecting {num_added_nmos_partition} partition is provided for NMOS. Disabled breaking symmetry.")
        instance.use_break_symmetry = False
    else:
        logger.info(f"Provided {num_added_pmos_partition} partition for PMOS. Using breaking symmetry.")
    
    logger.info(f"net_to_tran_group : {instance.net_to_tran_group}")
    logger.info(f"tran_group_by_low_degree_nets : {instance.tran_group_by_low_degree_nets}")

def _reify_z_eq(instance, tran_i_name, tran_j_name, tag):
    """
    Reify z_eq = (z_var_i == z_var_j) for a transistor pair. Used by per-tier
    placement-order helpers to gate adjacency / ordering on "same placement
    tier" (cross-tier transistors are physically independent in QFET's 4-tier
    stack so column ordering between them is meaningless).
    """
    z_i = instance.transistor_vars[tran_i_name].z_var
    z_j = instance.transistor_vars[tran_j_name].z_var
    z_eq = instance.opt.NewBoolVar(f"{tag}_z_eq_{tran_i_name}_{tran_j_name}")
    instance.opt.Add(z_i == z_j).OnlyEnforceIf(z_eq)
    instance.opt.Add(z_i != z_j).OnlyEnforceIf(z_eq.Not())
    return z_eq


def _uses_tier_placement(instance):
    """Whether per-tier (z_var) placement gating applies to this instance.

    Honors an explicit ``instance.uses_tier_placement`` flag when set; otherwise
    infers it from whether any transistor carries a populated ``z_var``. QFET's
    4-tier stack populates z_var (so gating is ON and behavior is byte-for-byte
    the pre-refactor form), while planar FinFET and physical-tier CFET leave
    z_var None (gating OFF -> unconditional form). Inferring from z_var
    keeps the shared core correct for every technology without requiring each
    orchestrator to remember to set the flag - an unset flag can never silently
    route a tier tech into the unconditional branch.
    """
    explicit = getattr(instance, "uses_tier_placement", None)
    if explicit is not None:
        return bool(explicit)
    return any(
        getattr(tv, "z_var", None) is not None
        for tv in instance.transistor_vars.values()
    )


def _fix_placement_order_identical_transistors_(instance, fix_placement_across_pn=False):
    """
    Per-tier: symmetry break by ordering same-net same-model transistors by x.

    For each multi-transistor group sharing a net, force x_i < x_j for adjacent
    pairs WITHIN THE SAME PLACEMENT TIER (gated on z_eq). Cross-tier pairs are
    physically independent and don't need column ordering.

    PMOS-NMOS alignment (fix_placement_across_pn=True) follows the same rule:
    only equate x when same tier.

    The z_eq gating is CONDITIONAL on whether the instance uses tier placement
    (see _uses_tier_placement: explicit flag, else inferred from a populated
    TransistorVar.z_var). Tier techs (QFET) reify z_eq from z_var and gate every
    constraint with OnlyEnforceIf(z_eq). Non-tier techs (FinFET/CFET, z_var=None)
    emit the unconditional ordering/alignment constraints, since there is no
    tier to gate on.
    """
    uses_tier = _uses_tier_placement(instance)
    # PMOS
    for net_key, grp in instance.net_to_pmos_tran_group.items():
        if len(grp) < 2:
            continue
        logger.info(f"Fixing PMOS placement order for group {grp}")
        instance.opt.log_comment(f"Fixing PMOS placement order for group {grp}")
        for tran_i_name, tran_j_name in pairwise(grp):
            x_i = instance.transistor_vars[tran_i_name].x_var
            x_j = instance.transistor_vars[tran_j_name].x_var
            if uses_tier:
                z_eq = _reify_z_eq(instance, tran_i_name, tran_j_name, tag="ord_p")
                instance.opt.Add(x_i < x_j).OnlyEnforceIf(z_eq)
            else:
                instance.opt.Add(x_i < x_j)
        # PMOS-NMOS counterpart (swap VDD <-> VSS in the net key)
        tmp_nmos_groups = []
        new_net_key = net_key.replace("VDD", "VSS", 1)
        if new_net_key in instance.net_to_nmos_tran_group and fix_placement_across_pn:
            logger.info(f"Found PMOS-NMOS placement order for group {grp}")
            instance.opt.log_comment(f"Found PMOS-NMOS placement order for group {grp}")
            tmp_nmos_groups = instance.net_to_nmos_tran_group[new_net_key]
        for tran_pmos_name, tran_nmos_name in zip(grp, tmp_nmos_groups):
            x_p = instance.transistor_vars[tran_pmos_name].x_var
            x_n = instance.transistor_vars[tran_nmos_name].x_var
            if uses_tier:
                z_eq = _reify_z_eq(instance, tran_pmos_name, tran_nmos_name, tag="align_pn")
                instance.opt.Add(x_p == x_n).OnlyEnforceIf(z_eq)
            else:
                instance.opt.Add(x_p == x_n)
    # NMOS
    for net_key, grp in instance.net_to_nmos_tran_group.items():
        if len(grp) < 2:
            continue
        logger.info(f"Fixing NMOS placement order for group {grp}")
        instance.opt.log_comment(f"Fixing NMOS placement order for group {grp}")
        for tran_i_name, tran_j_name in pairwise(grp):
            x_i = instance.transistor_vars[tran_i_name].x_var
            x_j = instance.transistor_vars[tran_j_name].x_var
            if uses_tier:
                z_eq = _reify_z_eq(instance, tran_i_name, tran_j_name, tag="ord_n")
                instance.opt.Add(x_i < x_j).OnlyEnforceIf(z_eq)
            else:
                instance.opt.Add(x_i < x_j)


def _tighten_placement_for_low_degree_net_(instance):
    """
    Per-tier: force pairs sharing a low-degree net to be column-adjacent.

    Adjacency only physically makes sense within the same placement tier;
    cross-tier pairs cannot be "adjacent" (they're stacked, not side-by-side).
    Gate both left and right adjacency on z_eq (same tier); when tiers
    differ, no constraint fires.

    NOTE: do not assume flip condition as VDD/VSS can also be shared.

    The "require one of the two adjacencies" clause is gated on z_eq (same tier)
    ONLY when the instance uses tier placement (see _uses_tier_placement: explicit
    flag, else inferred from a populated TransistorVar.z_var). Tier techs (QFET)
    reify z_eq from z_var and gate the AddBoolOr with OnlyEnforceIf(z_eq), since
    cross-tier pairs are stacked and cannot be column-adjacent. Non-tier techs
    (FinFET/CFET, z_var=None) require the adjacency UNCONDITIONALLY.
    """
    uses_tier = _uses_tier_placement(instance)
    for pair in instance.tran_group_by_low_degree_nets:
        tran_i_name, tran_j_name = pair[0], pair[1]
        logger.info(f"Placing transistors in close proximity {pair}")
        instance.opt.log_comment(f"Placing transistors in close proximity {pair}")
        x_i = instance.transistor_vars[tran_i_name].x_var
        x_j = instance.transistor_vars[tran_j_name].x_var

        # i one slot left of j
        i_left_j = instance.opt.NewBoolVar(f"tight_{tran_i_name}_left_of_{tran_j_name}")
        instance.opt.Add(x_i + (_NUM_COL_SDG_ - 1) == x_j).OnlyEnforceIf(i_left_j)
        instance.opt.Add(x_i + (_NUM_COL_SDG_ - 1) != x_j).OnlyEnforceIf(i_left_j.Not())
        # i one slot right of j
        i_right_j = instance.opt.NewBoolVar(f"tight_{tran_i_name}_right_of_{tran_j_name}")
        instance.opt.Add(x_i == x_j + (_NUM_COL_SDG_ - 1)).OnlyEnforceIf(i_right_j)
        instance.opt.Add(x_i != x_j + (_NUM_COL_SDG_ - 1)).OnlyEnforceIf(i_right_j.Not())
        # Require one of the two adjacencies - gate on same tier only when the
        # instance uses tier placement; otherwise require it unconditionally.
        if uses_tier:
            z_eq = _reify_z_eq(instance, tran_i_name, tran_j_name, tag="tight")
            instance.opt.AddBoolOr([i_left_j, i_right_j]).OnlyEnforceIf(z_eq)
        else:
            instance.opt.AddBoolOr([i_left_j, i_right_j])
            
def cluster_circuit(instance, method="kkhdb", visualize=False, remove_2d_nets=False):
    """
    Cluster the circuit for improved placement.
    
    KKHDB : Kamada-Kawai + HDBSCAN
    - Remove 2-degree nets but optionally preserve them (Good for SDFFSQ) 
    - Remove VDD/VSS but optionally add them back later (Good for SDFFSQ?)
    - Constrained max_cluster_size to only 4 because generating Euler path for large cluster is not efficient
    - When adding back VDD/VSS node, use clique model to represent.  
    - (TODO) Later we can use max/min x to constrain KNN-like clusters
    - Discard noise clusters (-1)
    
    Args:
        instance: The FinFET instance containing circuit and configuration
        method: Clustering method to use ("kkhdb" or "kkdb")
        visualize: Whether to generate visualization plots
        remove_2d_nets: Whether to remove 2-degree nets
        
    Returns:
        tuple: (networkx graph, list of clusters)
    """
    # Build circuit graph
    G = build_circuit_graph(instance)
    
    # Remove VDD and VSS but preserve their structure
    vdd_neighbors = list(G.neighbors('VDD'))
    vdd_node_attrs = G.nodes['VDD'] if 'VDD' in G.nodes else {}
    vdd_edge_attrs = {
        ('VDD', nbr): G.get_edge_data('VDD', nbr)
        for nbr in vdd_neighbors
    }
    G.remove_node('VDD')
    vss_neighbors = list(G.neighbors('VSS'))
    vss_node_attrs = G.nodes['VSS'] if 'VSS' in G.nodes else {}
    vss_edge_attrs = {
        ('VSS', nbr): G.get_edge_data('VSS', nbr)
        for nbr in vss_neighbors
    }
    G.remove_node('VSS')
    
    # Get set of transistor names for efficient lookup
    transistor_names = set(instance.circuit.transistors.keys())

    # Remove 2-degree net nodes and connect transistor directly together
    if remove_2d_nets:
        static_node_list = copy.deepcopy(G.nodes)
        for node in static_node_list:
            if node not in transistor_names:  # node is a net, not a transistor
                degree = G.degree(node)
                if degree > 2:
                    continue
                else:
                    # clique conn style
                    for neighbor_1 in G.neighbors(node):
                        for neighbor_2 in G.neighbors(node):
                            # every other node
                            if neighbor_1 == neighbor_2:
                                continue
                            # assign a default edge weight of 1
                            G.add_edge(neighbor_1, neighbor_2, weight=1)
                    # remove the net node
                    G.remove_node(node)

    # Add a weight to the net node's edges based on their net degree
    for node in G.nodes:
        if node not in transistor_names:  # node is a net, not a transistor
            degree = G.degree(node)
            for neighbor in G.neighbors(node):
                # Ensure consistent ordering for undirected edges
                u, v = sorted([node, neighbor])
                # You can set the weight to the degree of the current node
                G[u][v]['weight'] = degree
    
    if method == "kkhdb":
        from sklearn.cluster import HDBSCAN
        pos = nx.kamada_kawai_layout(G)
        nx.set_node_attributes(G, pos, 'pos')
        if visualize:
            plt.figure(figsize=(10, 10))
            nx.draw(
                G,
                pos,
                with_labels=True,
                edge_color='gray',
            )
        # Filter out any node that is a net (keep only transistors)
        G_prime = G.copy()
        static_node_list = copy.deepcopy(G_prime.nodes)
        for node in static_node_list:
            if node not in transistor_names:
                G_prime.remove_node(node)
        X = np.array([pos[node] for node in G_prime.nodes])
        # Handle case when there are no transistor nodes (empty array)
        if len(X) == 0:
            logger.warning("No transistor nodes found for clustering. Returning empty clusters.")
            return G, []
        db = HDBSCAN(
            min_cluster_size=instance.cell_config["inject_cluster"]["min_cluster_size"],
            max_cluster_size=instance.cell_config["inject_cluster"]["max_cluster_size"]
        ).fit(X)
        labels = db.labels_
    elif method == "kkdb":
        from sklearn.cluster import HDBSCAN
        pos = nx.kamada_kawai_layout(G)
        nx.set_node_attributes(G, pos, 'pos')
        # Filter out any node that is a net (keep only transistors)
        G_prime = G.copy()
        static_node_list = copy.deepcopy(G_prime.nodes)
        for node in static_node_list:
            if node not in transistor_names:
                G_prime.remove_node(node)
        X = np.array([pos[node] for node in G_prime.nodes])
        # Handle case when there are no transistor nodes (empty array)
        if len(X) == 0:
            logger.warning("No transistor nodes found for clustering. Returning empty clusters.")
            return G, []
        db = DBSCAN(eps=0.05, min_samples=2).fit(X)  # min sample should be 2
        labels = db.labels_
    
    # Collect clusters
    clusters = defaultdict(list)
    for node, label in zip(G_prime.nodes, labels):
        clusters[int(label)].append(node)
    
    # Map labels to nodes
    node_cluster = dict(zip(G_prime.nodes, labels))
    
    # Generate a color map
    unique_labels = sorted(set(labels))
    num_clusters = len(unique_labels)
    # Use a colormap
    cmap = plt.get_cmap('tab10')  # or 'tab20', 'hsv', etc.
    color_map = {label: cmap(label % cmap.N) for label in unique_labels}
    
    # Assign colors to each node
    node_colors = []
    for node in G.nodes():
        try:
            node_colors.append(color_map[node_cluster[node]])
        except KeyError:
            node_colors.append("grey")
    
    if visualize:
        plt.figure(figsize=(10, 10))
        nx.draw(
            G,
            pos,
            node_color=node_colors,
            with_labels=True,
            edge_color='gray',
        )
        plt.savefig(f"{instance.output_dir}/view/cluster_{instance.circuit.subckt_name}.png")
        plt.close()
    
    # Remove -1 noise
    clusters.pop(-1, None)
    return G, list(clusters.values())


def build_circuit_graph(instance):
    """
    Build a NetworkX graph representation of the circuit.

    Args:
        instance: The FinFET instance containing the circuit

    Returns:
        networkx.Graph: Graph representation of the circuit
    """
    return instance.circuit.generate_networkx_graph()


def _build_local_transistor_graph(circuit, members):
    """Build a weighted transistor graph for a subset of transistors.

    Edges connect transistors sharing non-power S/D or gate nets.
    Same-type S/D pairs get higher weight (diffusion sharing candidates).
    """
    member_set = set(members)
    g = nx.Graph()
    for name in members:
        g.add_node(name)

    # S/D net sharing edges
    for net_name, net in circuit.nets.items():
        if net.is_power_or_ground_net():
            continue
        sd = [
            t for t, p in net.connected_transistors
            if p in ('source', 'drain') and t in member_set
        ]
        if len(sd) < 2:
            continue
        for i, t1 in enumerate(sd):
            for t2 in sd[i + 1:]:
                w = 3.0 if circuit.transistors[t1].model == circuit.transistors[t2].model else 1.0
                if g.has_edge(t1, t2):
                    g[t1][t2]['weight'] += w
                else:
                    g.add_edge(t1, t2, weight=w)

    # Gate net sharing edges
    gate_groups = {}
    for name in members:
        t = circuit.transistors[name]
        gate_net = circuit.nets.get(t.gate)
        if gate_net and not gate_net.is_power_or_ground_net():
            gate_groups.setdefault(t.gate, []).append(name)

    for group in gate_groups.values():
        for i, t1 in enumerate(group):
            for t2 in group[i + 1:]:
                if g.has_edge(t1, t2):
                    g[t1][t2]['weight'] += 1.0
                else:
                    g.add_edge(t1, t2, weight=1.0)

    return g


def _sub_cluster_members(circuit, members, min_size=2):
    """Split *members* into finer sub-clusters using Louvain on local
    connectivity.  Returns the coarsest valid split (>1 sub-clusters
    each with >= *min_size* members), or ``[members]`` if unsplittable.
    """
    g = _build_local_transistor_graph(circuit, members)
    if g.number_of_edges() == 0:
        return [list(members)]

    for res in [0.3, 0.5, 0.7, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0]:
        communities = list(nx.community.louvain_communities(
            g, resolution=res, seed=42, weight='weight',
        ))
        valid = [sorted(c) for c in communities if len(c) >= min_size]
        if len(valid) > 1:
            return valid

    return [list(members)]


def _collect_multilevel_clusters(circuit, members, min_size, collected):
    """Recursively collect *members* and all finer sub-clusters."""
    if len(members) < min_size:
        return
    collected.append(sorted(members))

    if len(members) <= min_size:
        return  # can't sub-divide further

    subs = _sub_cluster_members(circuit, members, min_size)
    if len(subs) > 1:
        for sc in subs:
            _collect_multilevel_clusters(circuit, sc, min_size, collected)


def _load_evolved_clusters_multilevel(circuit, program_path, min_cluster_size=2):
    """Load evolved program, get base clusters, then recursively sub-cluster.

    Returns ``(G, clusters)`` where *clusters* is a deduplicated list
    spanning all hierarchical levels.  A transistor may appear in
    multiple clusters (e.g. a pair AND its parent group of 6).
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("best_program", program_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load evolved program from {program_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    result = module.cluster_transistors(circuit)

    # Base clusters: drop noise (cid == -1) and singletons
    base_clusters = [
        members for cid, members in result.items()
        if cid != -1 and len(members) >= min_cluster_size
    ]

    # Recursively collect multi-level clusters
    all_clusters = []
    for cluster in base_clusters:
        _collect_multilevel_clusters(circuit, cluster, min_cluster_size, all_clusters)

    # Deduplicate
    seen = set()
    unique = []
    for clst in all_clusters:
        key = frozenset(clst)
        if key not in seen:
            seen.add(key)
            unique.append(clst)

    logger.info(
        f"Multi-level clustering: {len(base_clusters)} base clusters "
        f"-> {len(unique)} total (base + sub-clusters)"
    )

    G = circuit.generate_networkx_graph()
    return G, unique
