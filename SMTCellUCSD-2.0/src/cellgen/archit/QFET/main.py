"""
QFET cell-generation orchestrator.

__init__ runs a fixed workflow:

    store inputs
      -> apply config flags
      -> banner + state init + CP model
      -> analyze circuit + initialize subsystems
      -> build constraints + maybe inject clusters
      -> solve setup + injections + TLU constraint
      -> run solve -> maybe write results

Each step is a private helper called once from __init__. Key structure:
    _init_subsystems  builds graph / tech / CP-SAT domain / variables.
    _init_var         populates every CP-SAT variable container (the
                      containers themselves are pre-allocated by
                      _init_state_containers).

z (tier) axis:
    Each transistor carries a z (tier) IntVar drawn from
    domain_mos_placable_zi, spanning the LGG layer indices of every
    placement-tier name in q_tech.placement_layer_names. SH-mode
    distinctness runs over (x, z) instead of just x, so two same-model
    transistors can share a column iff they live on different tiers.

    Per-slot bools (set up in _init_transistor_vars; consumed by
    _init_diffusion_break_vars and the placement constraints in placement.py):
        placed_tran_ci_vars[(tran, ci)]         tran at col ci (any tier)
        placed_tran_zi_vars[(tran, zi)]         tran at tier zi (any col)
        placed_tran_at_xzi_vars[(tran, ci, zi)] tran at slot (ci, zi) = ci AND zi,
                                                reified once and reused by DB +
                                                placement consumers
        has_tran_at_ci_vars[ci] / has_tran_at_zi_vars[zi]   OR aggregates

    Diffusion breaks mirror the (x, z) shape with per-slot bools
    db_{pmos,nmos}_vars[(ci, zi)] (DB iff no transistor of that model at slot).
    Per-col aggregates db_{pmos,nmos}_cols_vars[ci] (AND over tiers) are kept
    for placement.py + objective.py.

Generalized pin / SON handling:
    _init_SON_positions / _init_SON_vars iterate q_tech.pin_access_layer_names
    rather than hardcoding layer names, so any set of pin-access layers (H, V,
    or mixed) works. Horizontal pin-access layers are col-filtered against their
    adjacent vertical layer, found via _adjacent_vertical_layer_for by walking
    LGG stack adjacency (z +/- 1). The convention assumed: even-indexed metals
    run horizontally, odd vertically. To make a layer pin-accessible, add it to
    pin_access_layer_names in tech.py - no code change needed.

    tvar.{s|g|d}_col_idx_var is keyed [net.name][zi][col] = [bvs]. The zi
    (placement-tier z-index) lets placement.py scope S/D/G constraints per tier.
    Producer: _populate_pin_candidates (per-tier x per-row x per-col candidate
    gen with parity filter, keyed by _PIN_ROLE_CONFIG); inverse direction:
    _gather_region_nodes.

Placement-tier config lives on QFET_Tech (see tech.py):
    placement_layer_names    set of placement-tier names
    default_placement_layer  canonical pick for col/parity/pitch
    is_placement_layer(...)  predicate
    layer_to_kind            dict feeding LGG(layer_to_kind=...)
"""

import math
import os
from collections import OrderedDict

from loguru import logger

from ortools.sat.python import cp_model

from src.cellgen.archit import config
from src.cellgen.archit.QFET.tech import QFET_Tech
from src.cellgen.archit.QFET.util import write_qfet_result
from src.cellgen.postprocess.visualize_QFET_4T import draw_qfet_layout, load_results
from src.cellgen.core import accelerate
from src.cellgen.core import inject
from src.cellgen.core import placement as plc
from src.cellgen.core import routing as rt
from src.cellgen.core import rule
from src.cellgen.core.entity import Circuit, Model
from src.cellgen.core.graph import LayeredGridGraph
from src.cellgen.core.objective import Objective
from src.cellgen.core.util import log_variable_info, print_smtcell_banner
from src.cellgen.core.variable import TransistorVar
from src.cellgen.solver.cpsat_wrapper import CPSAT


class QFET:
    """
    Formulate and place QFETs in the circuit.
    """

    # Pin-role -> (LGG parity-check method name, TransistorVar attribute name).
    # Even col = gate, odd col = source/drain (see LayeredGridGraph.is_even_col).
    # Consumed by _populate_pin_candidates to deduplicate the src/gate/drain
    # triplets in _init_src/term_super_inner_nodes_vars.
    _PIN_ROLE_CONFIG = {
        "source": ("is_odd_col",  "s_col_idx_var"),
        "gate":   ("is_even_col", "g_col_idx_var"),
        "drain":  ("is_odd_col",  "d_col_idx_var"),
    }
    
    # ----- solve policy --------------------------------------------------
    # Default weighted-sum objective table. Each row: (name, Objective method
    # name, default weight, sense). Weight philosophy:
    #   CPP (1000)              dominant - cell area is the primary cost
    #   top_layer_usage (100)   strong secondary - M2 usage drives routability
    #   gate / lisd / db / wl (1)   tertiary tie-breakers
    # Per-run weights are overridable via cell_config["objective_weights"]
    # (dict name -> weight). Weight == 0 disables that objective entirely.
    _DEFAULT_OBJECTIVE_CONFIG = (
        # (name,                Objective fn,           default_weight, sense)
        ("cpp",                 "cpp",                  1000,           "min"),
        ("gate_sharing",        "gate_sharing",         1,              "max"),
        ("lisd_sharing",        "lisd_sharing",         1,              "max"),
        ("weighted_wirelength", "weighted_wirelength",  1,              "min"),
        ("db_placement",        "db_placement",         1,              "max"),
        ("top_layer_usage",     "top_layer_usage",      100,            "min"),
    )

    # Deterministic-solve objective hierarchy. Lex-ordered via 10^k weights
    # so each tier strictly dominates the next - gives a UNIQUE minimizer
    # across `num_search_workers > 1` runs. Includes `_obj_tier_anchor` for
    # QFET's z (tier) dimension. Toggled by cell_config["deterministic_solve"].
    #
    # int64 sanity: max total ~= 10^15 * 10 + 10^12 * 12 + ... ~= 10^16; safe.
    _DETERMINISTIC_OBJECTIVE_CONFIG = (
        ("cpp",                 "cpp",                 10**15, "min"),
        ("gate_sharing",        "gate_sharing",        10**12, "max"),
        ("lisd_sharing",        "lisd_sharing",        10**10, "max"),
        ("left_anchor",         "_obj_left_anchor",    10**7,  "min"),
        ("tier_anchor",         "_obj_tier_anchor",    10**5,  "min"),
        ("flip_anchor",         "_obj_flip_anchor",    10**3,  "min"),
        ("weighted_wirelength", "weighted_wirelength", 10**2,  "min"),
    )

    # Routing-determinism overlay: same as above but with weighted_wirelength
    # bumped to 10**8 (so a 1-unit WL improvement dominates the route tiebreaker)
    # and `_obj_route_tiebreaker` added at weight 1. Pinning net_arc_vars
    # uniquely makes routing topology deterministic across workers too.
    _DETERMINISTIC_ROUTING_OBJECTIVE_CONFIG = (
        ("cpp",                  "cpp",                  10**15, "min"),
        ("gate_sharing",         "gate_sharing",         10**12, "max"),
        ("lisd_sharing",         "lisd_sharing",         10**10, "max"),
        ("left_anchor",          "_obj_left_anchor",     10**7,  "min"),
        ("tier_anchor",          "_obj_tier_anchor",     10**5,  "min"),
        ("flip_anchor",          "_obj_flip_anchor",     10**3,  "min"),
        ("weighted_wirelength",  "weighted_wirelength",  10**8,  "min"),
        ("route_tiebreaker",     "_obj_route_tiebreaker", 1,     "min"),
    )

    def __init__(
        self,
        circuit: Circuit,
        tech: QFET_Tech,
        output_dir: str = "./output/",
        num_col: int | None = None,
        cell_config=None,
        flag_log_constraints: bool = False,
        solver: str = "cpsat",
    ):
        # 1) inputs
        self.circuit = circuit
        self.q_tech = tech
        self.output_dir = output_dir
        # cell_config may arrive as either a path (str) or an already-loaded dict.
        # config.read handles the file path -> dict; pass-through if already dict.
        self.cell_config = config.read(cell_config) if isinstance(cell_config, str) else cell_config
        self.solver_name = solver
        self._apply_config_flags()
        # Tier count drives num_col division: with 2 BPC1/PC1 tiers, each
        # gate column hosts up to 2 PMOS + 2 NMOS, so the bottleneck per
        # tier-row is half. Matches the cpp lower-bound math in _init_cpp.
        self.num_col = (
            num_col if num_col is not None
            else circuit.get_minimum_col(
                num_db=self.insert_num_db + 1,
                num_placement_layers=len(tech.placement_layer_names),
            )
        )

        # 2) state init + solver model
        self._print_banner()
        self._init_state_containers()
        self._init_model(flag_log_constraints)

        # 3) analyze + subsystems + constraints
        accelerate.analyze_circuit(self)
        self._init_subsystems()
        self._build_constraints()
        self._maybe_inject_clusters()

        # 4) solve setup + injections + TLU constraint
        self._setup_solve_strategy()
        # self._apply_injections()
        self._constrain_top_layer_usage()

        # 5) solve + write
        self._run_solve()
        self._maybe_write_results()

    @property
    def tech(self):
        return self.q_tech

    # ------------------------------------------------------------------ #
    # __init__ helpers (each called exactly once, in workflow order)     #
    # ------------------------------------------------------------------ #

    def _apply_config_flags(self):
        """Cache top-level cell_config flags onto self for hot-path access."""
        cfg = self.cell_config
        self.SET = cfg["model_preset"]["value"]
        self.insert_num_db = cfg["insert_num_db"]["value"]
        self.use_break_symmetry = cfg["use_break_symmetry_for_placement"]["value"]
        self.fix_placement_across_pn = cfg["use_placement_order_for_identical_transistors"]["value"]
        self.use_low_degree_net = cfg["close_in_low_degree_net"]["value"]

    def _print_banner(self):
        """Log the SMTCell2.0 banner and current run identity."""
        print_smtcell_banner(
            archit="QFET",
            tech=self.q_tech.lib_name,
            subckt=self.circuit.subckt_name,
        )

    def _init_state_containers(self):
        """
        Pre-create every container that subsequent init methods populate.

        Centralizes the answer to "where does attribute X come from?". Sub-init
        methods (_init_tech / _init_domain / _init_var / _init_graph) only fill
        what's pre-allocated here.
        """
        # geometric primitives (overwritten by _compute_canvas_dimensions)
        self.canvas_width = 0
        self.canvas_height = 0

        # transistor metadata (populated by _init_tech)
        self.mos_to_num_finger = {}              # mos_name -> num_finger
        # Placement / pin-access row indices on the canonical placement tier
        # (q_tech.default_placement_layer). Populated by _compute_placement_row_indices.
        self.nmos_placeable_row_indices = []
        self.pmos_placeable_row_indices = []
        self.nmos_pin_access_ri = []
        self.pmos_pin_access_ri = []

        # transistor / net top-level maps
        self.transistor_vars = {}                # transistor name -> TransistorVar
        self.net_vars = {}                       # net name -> NetVar

        # transistor placement vars (populated by _init_transistor_vars / _init_cpp).
        # Parallel _ci_ (col) and _zi_ (tier) axes; _at_xzi_ is the per-slot
        # AND-reifier that consumers like _init_diffusion_break_vars and the
        # placement constraints in placement.py use directly (avoids re-reifying).
        self.placed_tran_ci_vars = {}            # (tran_name, ci) -> bool var
        self.placed_tran_zi_vars = {}            # (tran_name, zi) -> bool var
        self.placed_tran_at_xzi_vars = {}        # (tran_name, ci, zi) -> bool var (= ci AND zi)
        self.has_tran_at_ci_vars = {}            # ci -> bool var
        self.has_tran_at_zi_vars = {}            # zi -> bool var

        # diffusion break vars (populated by _init_diffusion_break_vars).
        # Per-slot is canonical; per-col is a backward-compat aggregate consumed by
        # placement.py + objective.py until those callers migrate to per-slot keys.
        self.db_pmos_vars = {}                   # (ci, zi) -> bool var: PMOS DB at slot
        self.db_nmos_vars = {}                   # (ci, zi) -> bool var: NMOS DB at slot
        self.db_vars = {}                        # (ci, zi) -> bool var: PMOS+NMOS DB at slot
                                                 # (set by placement.diffusion_alignment when enabled)
        self.db_pmos_cols_vars = {}              # ci -> bool var: PMOS DBs at all tiers of col
        self.db_nmos_cols_vars = {}              # ci -> bool var: NMOS DBs at all tiers of col

        # net source / terminal Super Inner Nodes
        self.node_is_src_vars = {}               # (net) -> (layer, row, col) -> bool var
        self.node_is_term_vars = {}              # (net) -> k -> (layer, row, col) -> bool var

        # net-level vars
        self.num_pins_for_io = 0
        self.net_flow_vars = {}
        self.net_to_flow_cnt = {}                # net -> flow count
        self._int_flow_nets = {}                 # net_name -> total_k (only int-flow nets)
        self.net_arc_vars = {}
        self.edge_vars = {}
        self.edge_to_cost = {}                   # used by objective

        # Super Outer Nodes (I/O pins)
        self.son_terminal_nodes = {}
        self.node_is_SON_vars = {}
        self.node_to_net_SON_vars = {}           # (layer, row, col) -> (net) -> bool var

        # ----- routing state -------------------------------------------- #
        # Populated by the rt.X(self) free-function calls in _routing_constraints;
        # pre-allocated here so attribute provenance stays grep-able.

        # gate sharing / gate-cut windows (per-tier nested column maps).
        # Outer key: zi (placement-tier LGG layer index). Inner key: physical col coord.
        self.gate_share_at_col_vars = {}             # zi -> OrderedDict[col -> gate_share BoolVar]
        self.gate_cut_window_vars   = {}             # zi -> {col_tuple -> gate_cut_window BoolVar}
        self.has_tran_at_xzi_vars   = {}             # (ci, zi) -> bool var (OR over transistors at slot)

        # LISD sharing (per-tier nested column map; same nesting as gate sharing)
        self.lisd_share_at_col_vars = {}             # zi -> OrderedDict[col -> lisd_share BoolVar]

        # routing-window coords + bbox (per net.name)
        self.s_coord_x = {}                          # net.name -> IntVar
        self.s_coord_y = {}                          # net.name -> IntVar
        self.t_coord_x = {}                          # net.name -> [IntVar, ...]
        self.t_coord_y = {}                          # net.name -> [IntVar, ...]
        self.net_min_x = {}                          # net.name -> IntVar
        self.net_max_x = {}                          # net.name -> IntVar
        self.net_min_y = {}                          # net.name -> IntVar
        self.net_max_y = {}                          # net.name -> IntVar
        self.window_xmin_raw = {}                    # net.name -> IntVar
        self.window_xmax_raw = {}                    # net.name -> IntVar
        # Per-tier y-window (legacy 2-tier shape kept; QFET 4-tier pass TBD)
        self.window_ymin_tier = {}                   # net.name -> ti -> IntVar
        self.window_ymax_tier = {}                   # net.name -> ti -> IntVar
        self.has_pins_on_tier = {}                   # net.name -> ti -> BoolVar
        self.net_min_y_tier   = {}                   # net.name -> ti -> IntVar
        self.net_max_y_tier   = {}                   # net.name -> ti -> IntVar

        # design-rule scratch
        self.geometric_vars  = {}                    # node -> {left, right, front, back}
        self.m1_cols_to_used = {}                    # col -> BoolVar (top-side M1)
        self.bm1_cols_to_used = {}                   # col -> BoolVar (back-side BM1)

    def _init_model(self, flag_log_constraints: bool):
        """
        Create the optimization model from the selected solver backend.

        Dispatches on self.solver_name to a builder in the local `builders` map.
        Only "cpsat" is wired; unknown backends raise NotImplementedError with a
        clear message.

        When `flag_log_constraints` is True, every constraint is mirrored to
        `<output_dir>/constraint/<subckt>.log` by the backend wrapper.
        """
        logfile = (
            f"{self.output_dir}/constraint/{self.circuit.subckt_name}.log"
            if flag_log_constraints else None
        )
        builders = {
            "cpsat":  lambda: CPSAT(logfile=logfile),
        }
        if self.solver_name not in builders:
            raise NotImplementedError(
                f"Solver backend {self.solver_name!r} is not supported. "
                f"Available: {sorted(builders)}."
            )
        self.opt = builders[self.solver_name]()

    def _init_subsystems(self):
        """Initialize graph, tech, CP-SAT variable domain, variables, and region caches."""
        self._init_graph()
        self._init_tech()
        self._init_domain()  # CP-SAT-specific today; see _make_domain
        self._init_var()
        # No precompute needed: the z-aware helpers (_gather_region_nodes,
        # _gather_via_vars_in_region, _gather_src_term_vars_in_region) compute
        # region nodes on demand from the LGG. No-op kept for call compatibility.
        self._build_region_caches()

    def _build_region_caches(self):
        """No-op placeholder; per-tier _gather_*_in_region helpers compute on demand."""
        pass

    def _build_constraints(self):
        """
        Emit placement and routing constraints into the CP-SAT model.

        Three stages, picked by `cell_config["routing_stage"]`:
          1. "placement"      - placement constraints only, no routing.
                                MUST always be feasible; baseline for the cell.
          2. "internal"       - placement + every routing constraint EXCEPT
                                `induce_external_routing_flow` (no SON-side
                                tree). Internal flow conservation enforced.
          3. "external"       - placement + every routing constraint INCLUDING
                                `induce_external_routing_flow` (full IO routing).

        `cell_config["enable_routing"]` is honored for back-compat: when False,
        forces stage to "placement" regardless of `routing_stage`.
        """
        self._placement_constraints()
        if not self._cfg_get("enable_routing", True):
            return
        stage = self._cfg_get("routing_stage", "internal")
        if stage == "placement":
            return
        self._routing_constraints(include_external_son=(stage == "external"))

    def _maybe_inject_clusters(self):
        """Apply cluster injection when enabled in the cell config; otherwise no-op."""
        cluster_cfg = self.cell_config["inject_cluster"]
        if not cluster_cfg["value"]:
            return

        method = cluster_cfg["method"]
        min_cs = cluster_cfg.get("min_cluster_size", 2)
        max_cs = cluster_cfg.get("max_cluster_size", None)

        G, clusters = accelerate.cluster_circuit(
            self,
            method="kkhdb",
            visualize=method,
            remove_2d_nets=cluster_cfg["remove_2d_nets"],
        )

        if max_cs is not None:
            before = len(clusters)
            clusters = [c for c in clusters if len(c) <= max_cs]
            logger.info(f"max_cluster_size={max_cs}: kept {len(clusters)}/{before} clusters")

        inject.inject_clusters(self, G, clusters, use_path_trace=True)

    def stats(self):
        """Log CP-SAT model variable / constraint counts."""
        proto = self.opt.Proto()
        logger.info("-" * 80)
        logger.info(f"Total number of variables defined in the model: {len(proto.variables)}")
        logger.info(f"Total number of constraints defined in the model: {len(proto.constraints)}")
        logger.info("-" * 80)

    def _setup_solve_strategy(self):
        """Log model stats then pick the search strategy from config (PLACE / ROUTE / ALL)."""
        self.stats()
        strategy = self.cell_config["use_strategy"]["value"]
        if strategy == "PLACE":
            self.use_placement_strategy()
        elif strategy == "ROUTE":
            self.use_routing_window_strategy()
        elif strategy == "ALL":
            self.use_placement_strategy()
            self.use_routing_window_strategy()

    def use_placement_strategy(self):
        """
        Decision strategy that prioritizes placement vars: transistor x/flip first,
        then per-net source/terminal/SON bools sorted by (z, row, col).
        QFET is SH-only - no site_var.
        """
        self.opt.log_comment("Using placement strategy ...")
        logger.info("\t==\tUsing placement strategy ...")

        spatial = []
        for net in self.circuit.get_nets(with_power_ground=False):
            for node, var in self.node_is_src_vars.get(net.name, {}).items():
                spatial.append((node, var))
            for k in range(net.num_terminals()):
                for node, var in self.node_is_term_vars.get(net.name, {}).get(k, {}).items():
                    spatial.append((node, var))
        for net_name in self.node_is_SON_vars:
            for k in self.node_is_SON_vars[net_name]:
                for node, var in self.node_is_SON_vars[net_name][k].items():
                    spatial.append((node, var))
        spatial.sort(key=lambda nv: (nv[0][0], nv[0][1], nv[0][2]))
        spatial_vars = [v for _, v in spatial]

        tran_vars = []
        for tvar in self.transistor_vars.values():
            tran_vars.append(tvar.x_var)
            tran_vars.append(tvar.z_var)
            tran_vars.append(tvar.flip_var)

        all_vars = tran_vars + spatial_vars
        if all_vars:
            logger.info(f"\t==\tAdding decision strategy for {len(all_vars)} placement variables")
            self.opt.AddDecisionStrategy(
                all_vars, cp_model.CHOOSE_HIGHEST_MAX, cp_model.SELECT_MAX_VALUE,
            )

    # ----- Deterministic-solve tiebreakers -------------------------------
    # Each method returns a linear expression. Combined via the
    # lexicographic 10^k weights in `_DETERMINISTIC_OBJECTIVE_CONFIG`, they
    # collapse the optimum to a unique minimizer so multi-worker CP-SAT runs
    # converge to byte-identical (x, z, flip, arc) assignments regardless of
    # `num_search_workers` or `random_seed`.
    #
    # The `(i + 1)` indexed weighting (i = sorted-name rank) is the key:
    # plain `sum(x_var)` collapses cell-swap symmetry to the same cost
    # (e.g. swapping two transistors leaves the sum unchanged), whereas
    # indexed weights give each transistor a unique contribution.

    @staticmethod
    def _obj_left_anchor(instance):
        """Indexed-weighted sum of x_var (sorted by transistor name).

        Pins each transistor's col uniquely and anchors the merged cell to
        the left edge of the canvas."""
        return sum(
            (i + 1) * tv.x_var
            for i, (_, tv) in enumerate(sorted(instance.transistor_vars.items()))
        )

    @staticmethod
    def _obj_tier_anchor(instance):
        """Indexed-weighted sum of z_var (sorted by transistor name).

        QFET-only - FinFET has no z dimension. Pins each transistor's
        tier uniquely so the (x, z) assignment is fully fixed when the
        cpp / share / left tiers have ties."""
        return sum(
            (i + 1) * tv.z_var
            for i, (_, tv) in enumerate(sorted(instance.transistor_vars.items()))
        )

    @staticmethod
    def _obj_flip_anchor(instance):
        """Indexed-weighted sum of flip_var (sorted by transistor name).

        Prefers flip=0 on lower-index transistors first, breaking flip
        symmetry when (cpp, gate, lisd, left, tier) all tie."""
        return sum(
            (i + 1) * tv.flip_var
            for i, (_, tv) in enumerate(sorted(instance.transistor_vars.items()))
        )

    @staticmethod
    def _obj_route_tiebreaker(instance):
        """Indexed-weighted sum of net_arc_vars (sorted by (net, u, v)).

        Pins per-net arc usage so two equally-WL routings are forced apart.
        Opt-in (heavier weights needed; see _DETERMINISTIC_ROUTING_OBJECTIVE_CONFIG).
        """
        items = sorted(instance.net_arc_vars.items())
        return sum((i + 1) * av for i, (_, av) in enumerate(items))

    def use_routing_window_strategy(self):
        """Decision strategy on routing bounding-box / coord vars (ROUTE / ALL only)."""
        self.opt.log_comment("Using routing window strategy ...")
        logger.info("\t==\tUsing routing window strategy ...")
        rt_vars = []
        for net in self.circuit.get_nets(with_power_ground=False):
            rt_vars.append(self.s_coord_x[net.name])
            rt_vars.append(self.s_coord_y[net.name])
            for k in range(net.num_terminals()):
                rt_vars.append(self.t_coord_x[net.name][k])
                rt_vars.append(self.t_coord_y[net.name][k])
        for net in self.circuit.get_nets(with_power_ground=False):
            rt_vars.append(self.net_min_x[net.name])
            rt_vars.append(self.net_max_x[net.name])
            rt_vars.append(self.net_min_y[net.name])
            rt_vars.append(self.net_max_y[net.name])
        if rt_vars:
            self.opt.AddDecisionStrategy(
                rt_vars, cp_model.CHOOSE_LOWEST_MIN, cp_model.SELECT_MIN_VALUE,
            )

    def solve(self, mode="wsum", objectives=None, exit_on_unsat=True):
        """Dispatch the solve. Only weighted-sum ('wsum') is supported."""
        if mode == "wsum":
            return self.wsum(objectives=objectives, exit_on_unsat=exit_on_unsat)
        raise NotImplementedError(f"solve mode {mode!r} not implemented (use 'wsum')")

    def wsum(self, objectives=None, exit_on_unsat=True, silence=False):
        """
        Weighted-sum CP-SAT solve. Applies the configured model_preset, sums
        weighted objectives, and runs `solver.Solve`. Returns (total_obj_expr,
        ObjectiveValue) on success; honors exit_on_unsat for UNSAT/UNKNOWN.
        """
        import time

        self.opt.log_comment("Defining the objective function ...")
        self.solver = cp_model.CpSolver()
        self.solver.parameters.num_search_workers = self._cfg_get("num_search_workers", 8)
        self.solver.parameters.random_seed = self._cfg_get("seed", 0)
        if silence:
            self.solver.parameters.log_search_progress = False
        else:
            self.solver.parameters.log_search_progress = True
            # CP-SAT emits to BOTH stdout AND log_callback by default. Setting
            # log_to_stdout=False routes only through the callback, otherwise
            # every line prints twice ("Presolved optimization model" headers
            # in particular). Match upstream finfet's expectation.
            self.solver.parameters.log_to_stdout = False
            self.solver.log_callback = print
        max_time = self.cell_config.get("max_time", {})
        if max_time.get("value"):
            self.solver.parameters.max_time_in_seconds = max_time.get("time", 3600)

        # model preset (matches legacy SET semantics)
        preset = self.SET
        if preset == 0:
            self.solver.parameters.cp_model_presolve = True
            self.solver.parameters.cp_model_probing_level = 3
            self.solver.parameters.symmetry_level = 3
            self.solver.parameters.ignore_subsolvers.extend([
                "default_lp", "max_lp",
                "packing_random_lns", "packing_square_lns",
                "packing_precedences_lns", "packing_slice_lns", "packing_swap_lns",
                "scheduling_precedences_lns",
                "graph_dec_lns", "graph_var_lns", "max_lp_sym",
            ])
        elif preset == 1:
            self.solver.parameters.search_branching = cp_model.FIXED_SEARCH
            self.solver.parameters.interleave_search = True
            self.solver.parameters.interleave_batch_size = 2 * self.solver.parameters.num_search_workers
        elif preset == 2:
            self.solver.parameters.ignore_subsolvers.extend([
                "quick_restart", "graph_arc_lns", "graph_cst_lns",
                "graph_dec_lns", "graph_var_lns", "rnd_cst_lns", "rnd_var_lns",
                "reduced_costs", "max_lp_sym", "default_lp",
            ])
            self.solver.parameters.linearization_level = 0
            self.solver.parameters.cp_model_presolve = True
            self.solver.parameters.cp_model_probing_level = 3
            self.solver.parameters.symmetry_level = 3

        total_obj = 0
        self.obj_terms = []
        if not objectives:
            logger.info("\t==\tNo objectives defined. Using default Objective.cpp")
            total_obj = Objective.cpp(self)
        else:
            for i, (obj_fn, weight, sense) in enumerate(objectives):
                expr = obj_fn()
                name = getattr(obj_fn, "__name__", f"obj{i}")
                self.obj_terms.append((name, expr, weight, sense))
                if sense == "min":
                    total_obj += weight * expr
                else:
                    total_obj += (-weight) * expr
                logger.info(f"\t==\tAdded objective {i + 1}: [{sense}] {name} weight={weight}")

        self.opt.Minimize(total_obj)
        rel_gap = self.cell_config.get("use_relative_gap", {})
        if rel_gap.get("value"):
            self.solver.parameters.relative_gap_limit = rel_gap.get("perc", 0.01)

        t0 = time.time()
        logger.info("\t==\tSolving the model (wsum) ...")
        status = self.solver.Solve(self.opt)
        logger.info(f"Elapsed time: {time.time() - t0:.2f} seconds")

        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            logger.info(f"\t==\tObjective value: {self.solver.ObjectiveValue()}")
            for i, (name, expr, weight, sense) in enumerate(self.obj_terms, start=1):
                val = self.solver.Value(expr)
                logger.info(f" Obj#{i} {name:24s} = {val:6d}  ({sense}, w={weight}, result={val * weight})")
            return total_obj, self.solver.ObjectiveValue()
        if status == cp_model.UNKNOWN:
            logger.error("Solver returned UNKNOWN.")
            if exit_on_unsat:
                exit(1)
            return None
        logger.error("No solution found (UNSAT/INFEASIBLE).")
        if exit_on_unsat:
            exit(1)
        return None

    def _apply_injections(self):
        """
        Apply edge / arc / flow / placement injections specified in the cell config.

        Each injection-type config is a dict (or list) under cell_config; keys
        encode node coords as flat tuples that we slice into (z, r, c) triples:
            inject_edge[(uz, ur, uc, vz, vr, vc)]       = value
            inject_arc [(net, uz, ur, uc, vz, vr, vc)]  = value
            inject_flow[(net, k, uz, ur, uc, vz, vr, vc)] = value
            inject_placement = [ (tran, x?, y?, flip?), ... ]
        """
        for k, v in self._cfg_get("inject_edge", {}).items():
            inject.inject_edge(self, k[:3], k[3:6], value=v)
        for k, v in self._cfg_get("inject_arc", {}).items():
            inject.inject_arc(self, k[0], k[1:4], k[4:7], value=v)
        for k, v in self._cfg_get("inject_flow", {}).items():
            inject.inject_flow(self, k[0], k[1], k[2:5], k[5:8], value=v)

        # Placement injection: config values are physical coords (from .res files);
        # x_var uses column indices, so divide by PC pitch to convert.
        pc_pitch = self.q_tech.get_pitch(self.q_tech.default_placement_layer)
        for t in self._cfg_get("inject_placement", []):
            inject.inject_placement(
                self,
                tran_name=t[0],
                x=int(t[1] / pc_pitch) if len(t) > 1 else None,
                y=t[2] if len(t) > 2 else None,
                flip=t[3] if len(t) > 3 else None,
            )

    def _constrain_top_layer_usage(self):
        """
        Tighten the M2 (top-layer) usage upper bound when beneficial.

        TLU weight=100; secondary objectives have bounded range. For small cells
        (<=1 DB), the secondary swing is <100, so reducing TLU by 1 always improves
        the objective - cap at (num_io_nets - 1) since the optimal TLU is always
        the minimum achievable. For large cells, only cap at num_io_nets (natural
        bound) since the minimum achievable TLU may equal num_io_nets.
        """
        num_io_nets = sum(
            1 for n in self.circuit.get_nets(with_power_ground=False) if n.is_io_net()
        )
        tlu_expr = Objective.top_layer_usage(self)
        if self.insert_num_db <= 1:
            self.opt.Add(tlu_expr <= max(num_io_nets - 1, 1))
        else:
            self.opt.Add(tlu_expr <= num_io_nets)

    def _run_solve(self):
        """
        Run the configured solve and record self.solve_status.

        Mode, objective set, weights, and exit-on-UNSAT are all overridable
        via cell_config (see _DEFAULT_OBJECTIVE_CONFIG above). Defaults match
        the prior hardcoded behavior: weighted-sum mode, exit on UNSAT,
        6 objectives with the same weights as before.
        """
        solve_result = self.solve(
            mode=self._cfg_get("solve_mode", "wsum"),
            objectives=self._build_solve_objectives(),
            exit_on_unsat=self._cfg_get("exit_on_unsat", True),
        )
        self.solve_status = self._interpret_solve_result(solve_result)

    def _build_solve_objectives(self):
        """
        Resolve the (factory, weight, sense) objective list for self.solve.

        Three modes, chosen by cell_config:
          deterministic_solve = False (default)
              Use _DEFAULT_OBJECTIVE_CONFIG; honor per-objective
              cell_config["objective_weights"]["value"] (dict name -> int)
              overrides; weight == 0 drops the entry.

          deterministic_solve = True, route_determinism = False
              Use _DETERMINISTIC_OBJECTIVE_CONFIG - lex-ordered 10^k
              weights collapse the optimum to a unique placement
              (x, z, flip) across `num_search_workers > 1`. Routing
              still has best-effort min-WL but is not pinned.

          deterministic_solve = True, route_determinism = True
              Use _DETERMINISTIC_ROUTING_OBJECTIVE_CONFIG - extends
              placement determinism with an arc-level tiebreaker, so
              routing topology (which directed arc per net) is also
              deterministic. Heavier weights -> slower solve.

        Tiebreaker methods live on QFET as `_obj_left_anchor`,
        `_obj_tier_anchor`, `_obj_flip_anchor`, `_obj_route_tiebreaker`
        (q.v.); other names resolve from `Objective`.
        """
        if self._cfg_get("deterministic_solve", False):
            config = (
                self._DETERMINISTIC_ROUTING_OBJECTIVE_CONFIG
                if self._cfg_get("route_determinism", False)
                else self._DETERMINISTIC_OBJECTIVE_CONFIG
            )
            out = []
            for name, fn_name, weight, sense in config:
                fn = getattr(self, fn_name, None) or getattr(Objective, fn_name)
                out.append((lambda f=fn: f(self), weight, sense))
            return out

        overrides = self._cfg_get("objective_weights", {}) or {}
        out = []
        for name, fn_name, default_weight, sense in self._DEFAULT_OBJECTIVE_CONFIG:
            weight = overrides.get(name, default_weight)
            if weight == 0:
                continue  # disabled
            fn = getattr(Objective, fn_name)
            out.append((lambda f=fn: f(self), weight, sense))
        return out

    def _cfg_get(self, key, default):
        """Read cell_config[key]['value'] with a default; treat missing keys as default."""
        entry = self.cell_config.get(key)
        if isinstance(entry, dict) and "value" in entry:
            return entry["value"]
        return default

    @staticmethod
    def _interpret_solve_result(solve_result) -> bool:
        """True iff the solve produced a usable (feasible/optimal) result."""
        return solve_result is not None and solve_result[0] is not None

    def _maybe_write_results(self):
        """Write variable info + final result file when the solve succeeded; otherwise no-op."""
        if not self.solve_status:
            return
        subckt = self.circuit.subckt_name
        # log_variable_info takes the QFET instance (uses .opt + .solver).
        log_variable_info(self, filename=f"{self.output_dir}/result/{subckt}.var")
        res_path = f"{self.output_dir}/result/{subckt}.res"
        write_qfet_result(
            self.solver, self.circuit, self.transistor_vars, self.edge_vars,
            self.net_arc_vars, self.q_tech, self.cpp_cost,
            filename=res_path,
            lgg=self.lgg,
        )
        # Layer-by-layer placement visualization -> output/<lib>/<height>/view/.
        view_dir = os.path.join(self.output_dir, "view")
        os.makedirs(view_dir, exist_ok=True)
        placement, routing, tech = load_results(res_path)
        draw_qfet_layout(
            placement, routing, tech,
            filename=os.path.join(view_dir, f"{subckt}.png"),
        )

    # ------------------------------------------------------------------ #
    # subsystem initializers                                             #
    # ------------------------------------------------------------------ #

    def _init_tech(self):
        """Cache technology-derived state: finger counts + placement row indices."""
        logger.info("Initializing technology configuration...")
        self._compute_finger_counts()
        self._compute_placement_row_indices()

    def _compute_finger_counts(self):
        """Map each transistor name to its finger count (width / unit_width)."""
        for tran in self.circuit.transistors.values():
            self.mos_to_num_finger[tran.name] = int(tran.get_width() / self.q_tech.unit_width)
        logger.info(f"\tNumber of fingers: {self.mos_to_num_finger}")

    # Pin-access row layout per num_rt_track. NMOS rows go bottom-up, PMOS top-down.
    # Asymmetry between 3-track and 4-track is load-bearing: the extra interior
    # tracks in the 4-track config are legitimate pin-contact tracks (not transit),
    # so DB constraints in routing.py must block all of them at a DB column.
    _PIN_ACCESS_RI_BY_TRACK = {
        4: ([0, 1], [2, 3]),
        3: ([0],    [2]),
        # 2: TODO when 2-track QFET is exercised
    }

    def _compute_placement_row_indices(self):
        """
        Set NMOS / PMOS placeable + pin-access row indices.

        Multi-tier interpretation (4-tier QFET):
            These indices reference row positions on the canonical placement
            tier (`q_tech.default_placement_layer`, currently PC1). All 4
            placement tiers share the same row layout, so picking one as
            canonical is sufficient for the constraints downstream code emits
            today. If per-tier differentiation becomes needed, upgrade these
            from flat lists to `dict[layer_name, list[int]]` and fan out the
            6 downstream call sites (5 in this file + 2 in routing.py).

        Track-config table:
            num_rt_track=4 -> NMOS pin access [0,1], PMOS [2,3]
            num_rt_track=3 -> NMOS pin access [0],   PMOS [2]
        """
        track = self.q_tech.num_rt_track
        if track not in self._PIN_ACCESS_RI_BY_TRACK:
            raise NotImplementedError(
                f"num_rt_track={track} pin-access layout not defined for QFET SH."
            )
        self.nmos_placeable_row_indices = [0]
        self.pmos_placeable_row_indices = [2]
        self.nmos_pin_access_ri, self.pmos_pin_access_ri = self._PIN_ACCESS_RI_BY_TRACK[track]

        logger.info(
            f"\tNMOS placeable rows: {self.nmos_placeable_row_indices}, "
            f"PMOS placeable rows: {self.pmos_placeable_row_indices}"
        )
        logger.info(
            f"\tNMOS pin-access rows: {self.nmos_pin_access_ri}, "
            f"PMOS pin-access rows: {self.pmos_pin_access_ri}"
        )


    def _init_graph(self):
        """Build self.lgg (LayeredGridGraph) from the technology layer stack."""
        logger.info("Initializing graph configuration...")
        self._compute_canvas_dimensions()
        idx_to_layer, layer_to_direction, layer_to_rows, layer_to_cols = self._build_layer_maps()

        # Virtual direct-connect "VL" via between adjacent placement tiers.
        # Virtual jump edges come from LayerStack.virtual_pairs (driven by the
        # "layer_type": "virtual" entries in the layer JSON). Each entry is
        # (lower_name, upper_name, method). Pairs may be non-adjacent - the
        # whole point is to bypass an intermediate MIV chain whose own pitch
        # grid may not align with placement S/D cols. The LGG accepts a single
        # method for all pairs; if multiple methods are declared, require them
        # to match (LGG limitation; lift when LGG supports per-pair methods).
        all_pairs = getattr(self.q_tech.layer_stack, "virtual_pairs", []) or []
        present = set(idx_to_layer.values())
        virtual_pairs, methods = [], set()
        for lo, hi, m in all_pairs:
            if lo in present and hi in present:
                virtual_pairs.append((lo, hi))
                methods.add(m)
        if len(methods) > 1:
            raise ValueError(
                f"LayerStack declares mixed virtual methods {methods}; "
                "LGG only supports one method per stack today."
            )
        virtual_method = methods.pop() if methods else "overlap"
        if virtual_pairs:
            logger.info(f"\tVirtual jump edges ({virtual_method}): {virtual_pairs}")

        self.lgg = LayeredGridGraph(
            layer_to_rows=layer_to_rows,
            layer_to_cols=layer_to_cols,
            idx_to_layer=idx_to_layer,
            layer_to_direction=layer_to_direction,
            layer_to_kind=self.q_tech.layer_to_kind,
            virtual_connect_pairs=virtual_pairs,
            virtual_connect_method=virtual_method,
        )
        self.lgg.stats()

    @staticmethod
    def lgg_index_if_exists(idx_to_layer, name):
        """Helper for sorting placement-layer names by their LGG z-index."""
        rev = {v: k for k, v in idx_to_layer.items()}
        return rev.get(name, -1)

    def _compute_canvas_dimensions(self):
        """
        Set self.canvas_width / canvas_height from num_col, num_rt_track, and layer pitches.

        Width  = num_col       * default_placement_layer.pitch
        Height = num_rt_track  * metal_layers[1].pitch * 2

        Width keeps native pitch because num_col already counts source/drain/gate
        columns at half-pitch resolution. Height doubles the routing-track pitch
        so y-coords live in the same doubled-resolution space as x-coords -
        see _coords_along_axis for the invariant.

        Note: the col-grid multiplier is the default placement layer's pitch, not
        metal_layers[0] (which is BM1 / top routing in the QFET stack). Using the
        BM1 pitch (30) instead of the placement pitch (45) makes the canvas too
        narrow by a factor of 2/3 and drops the last valid col after `[:-1]`.
        """
        layers = self.q_tech.layer_stack.metal_layers
        place_pitch = self.q_tech.get_pitch(self.q_tech.default_placement_layer)
        self.canvas_width = self.num_col * place_pitch
        self.canvas_height = self.q_tech.num_rt_track * layers[1].pitch * 2
        logger.info(f"\tCanvas: width={self.canvas_width}, height={self.canvas_height}")

    def _build_layer_maps(self):
        """
        Walk the layer stack once and return the four dicts LayeredGridGraph needs.

        Returns:
            (idx_to_layer, layer_to_direction, layer_to_rows, layer_to_cols)

        V layers contribute to layer_to_cols; H layers contribute to layer_to_rows.
        Coord generation goes through _coords_along_axis (PC-pitch invariant).
        """
        idx_to_layer = {}
        layer_to_direction = {}
        layer_to_rows = {}
        layer_to_cols = {}
        for li, layer in enumerate(self.q_tech.layer_stack.metal_layers):
            idx_to_layer[li] = layer.layer_name
            layer_to_direction[layer.layer_name] = layer.direction
            if layer.direction == "V":
                layer_to_cols[layer.layer_name] = self._coords_along_axis(layer, self.canvas_width)
            elif layer.direction == "H":
                layer_to_rows[layer.layer_name] = self._coords_along_axis(layer, self.canvas_height)
            else:
                raise ValueError(
                    f"Unknown direction {layer.direction!r} for layer {layer.layer_name!r}; expected 'H' or 'V'."
                )
        logger.info(f"\tLayer to cols: {layer_to_cols}")
        return idx_to_layer, layer_to_direction, layer_to_rows, layer_to_cols

    def _coords_along_axis(self, layer, extent):
        """
        Generate integer coords for `layer` along its primary axis, spanning [0, extent).

        Half-pitch convention (INVARIANT):
            The placement layer holds gate columns at integer positions and
            source/drain columns at half-pitch between them. To represent both
            as integers, the model works in a *doubled-resolution* coord space;
            downstream callers divide by 2 to recover physical coords.

            - placement layers : pitch kept native      (already half-resolution)
            - all other layers : pitch doubled          (align with placement grid)
            - offset           : doubled for every layer (canvas is doubled space)
        """
        is_placement = self.q_tech.is_placement_layer(layer)
        pitch = layer.pitch if is_placement else layer.pitch * 2
        offset = layer.offset * 2
        n = int(math.ceil((extent - offset) / pitch))
        return [offset + i * pitch for i in range(n)]


    def _init_domain(self):
        """
        Build CP-SAT Domain wrappers over the placement-tier column / row index sets.

        Convention (matches LayeredGridGraph.is_even_col):
            even-parity col index -> gate column
            odd-parity  col index -> source/drain column

        Indices are taken on the canonical placement tier
        (q_tech.default_placement_layer); all 4 QFET placement tiers share the
        same row/col layout, so one tier's indices serve as the domain for all.

        Outputs (7 attrs; external callers depend on the named ones):
            plc_ci                    list   gate-col indices, boundary col excluded
            sd_ci                     list   source/drain col indices
            pc_ci                     list   all placement-tier col indices
            domain_mos_placable_ci    Domain wraps plc_ci
            domain_mos_placable_ri    Domain over even-parity row indices on canonical tier
            domain_sd_ci              Domain wraps sd_ci
            domain_pc_ci              Domain wraps pc_ci

        TODO: currently CP-SAT-specific (cp_model.Domain). When other solver
        backends are wired, abstract the Domain construction through the
        solver-wrapper layer.
        """
        logger.debug("Initializing CP-SAT variable domain...")
        placement_layer = self.q_tech.default_placement_layer

        # MOS-placement positions: odd-parity (S/D) cols, last col excluded as boundary
        self.plc_ci = self.lgg.col_indices_in_layer(placement_layer, parity="odd")[:-1]
        self.domain_mos_placable_ci = self._make_domain(self.plc_ci, "MOS placeable col indices")

        # MOS-placement rows: even-parity row indices on canonical tier
        plc_ri = self.lgg.row_indices_in_layer(placement_layer, parity="even")
        self.domain_mos_placable_ri = self._make_domain(plc_ri, "MOS placeable row indices")

        # source/drain col indices (odd parity)
        self.sd_ci = self.lgg.col_indices_in_layer(placement_layer, parity="odd")
        self.domain_sd_ci = self._make_domain(self.sd_ci, "S/D col indices")

        # all placement-tier col indices
        self.pc_ci = self.lgg.col_indices_in_layer(placement_layer)
        self.domain_pc_ci = self._make_domain(self.pc_ci, "all placement col indices")

        # MOS-placement z (tier) indices: LGG layer indices of the placement tiers
        self.plc_zi = sorted(
            self.lgg.layer_to_idx[name] for name in self.q_tech.placement_layer_names
        )
        self.domain_mos_placable_zi = self._make_domain(self.plc_zi, "MOS placeable z (tier) indices")

    def _make_domain(self, values, label):
        """Wrap a list of values in cp_model.Domain.FromValues, log, return."""
        domain = cp_model.Domain.FromValues(values)
        logger.info(f"Domain {label}: {domain}")
        return domain

    def _init_var(self):
        """
        Populate every CP-SAT variable container.

        Container dicts/lists already exist (see _init_state_containers); each
        sub-method below fills them. Order matters - net-arc/edge depend on
        node adjacency; SON_vars depends on SON_positions.
        """
        # transistor placement (cell boundary coords are now absorbed into _init_cpp,
        # since they ride the same "cell right edge" axis as cpp_cost)
        self._init_transistor_vars()
        self._init_cpp()

        # diffusion breaks
        self._init_diffusion_break_vars()

        # net source / terminal Super Inner Nodes (for internal pins)
        self._init_src_super_inner_nodes_vars()
        self._init_term_super_inner_nodes_vars()

        # node adjacency cache (used downstream by flow/arc constraints)
        self._build_node_adjacency()

        # net-level variables (each method emits its own opt.log_comment marker):
        # flow  - directed flow of net to terminal k along arc
        # arc   - net touches arc (for capacity + objective)
        # edge  - undirected edge used by any net (for objective)
        self._init_net_flow_vars()
        self._init_net_arc_vars()
        self._init_edge_vars()
        self._cache_sorted_edge_costs()

        # Super Outer Nodes for I/O pins (positions, then per-net SON bindings)
        self._init_SON_positions()
        self._init_SON_vars()

        logger.info("\tEnd of variable initialization.")

    def _build_node_adjacency(self):
        """Build per-node in/out adjacency lists from LGG arcs (consumed by flow/arc constraints)."""
        self.adj_in = {node: [] for node in self.lgg.nodes()}
        self.adj_out = {node: [] for node in self.lgg.nodes()}
        for u_arc, v_arc in self.lgg.arcs():
            self.adj_out[u_arc].append((u_arc, v_arc))
            self.adj_in[v_arc].append((u_arc, v_arc))

    def _cache_sorted_edge_costs(self):
        """Cache sorted unique edge costs (used by the cost-normalization objective)."""
        self.all_possible_edge_cost = sorted(self.edge_to_cost.values())

    def _init_transistor_vars(self):
        """
        Create per-transistor placement vars (x, y, z, flip) and the bool
        variables that bind each transistor to each column it could occupy.
        """
        self.opt.log_comment("Transistor variables")

        # Per-model (PMOS/NMOS) lists for the SH-mode distinctness constraint.
        tmp_pmos_x_var, tmp_pmos_z_var = [], []
        tmp_nmos_x_var, tmp_nmos_z_var = [], []

        for tran in self.circuit.transistors.values():
            tvar = TransistorVar(tran.name)
            self.transistor_vars[tran.name] = tvar

            tvar.x_var = self.opt.NewIntVarFromDomain(self.domain_mos_placable_ci, f"{tran.name}_x")
            tvar.y_var = self.opt.NewIntVarFromDomain(self.domain_mos_placable_ri, f"{tran.name}_y")
            tvar.z_var = self.opt.NewIntVarFromDomain(self.domain_mos_placable_zi, f"{tran.name}_z")
            tvar.flip_var = self.opt.NewBoolVar(f"{tran.name}_flip")

            # SH mode pins y by transistor type; distinctness then runs over (x, z).
            if self.q_tech.height_config == "SH":
                if tran.model == Model.PMOS:
                    self.opt.Add(tvar.y_var == self.pmos_placeable_row_indices[0])
                    tmp_pmos_x_var.append(tvar.x_var)
                    tmp_pmos_z_var.append(tvar.z_var)
                elif tran.model == Model.NMOS:
                    self.opt.Add(tvar.y_var == self.nmos_placeable_row_indices[0])
                    tmp_nmos_x_var.append(tvar.x_var)
                    tmp_nmos_z_var.append(tvar.z_var)

        # Speedup: skip symmetry-breaking ordering when placements are fully injected
        # (injected positions may violate the non-deterministic set-iteration order
        # used by the symmetry-breaking constraints).
        if not self.cell_config["inject_placement"]["value"]:
            accelerate._fix_placement_order_identical_transistors_(
                self, fix_placement_across_pn=self.fix_placement_across_pn,
            )
            if self.use_low_degree_net:
                accelerate._tighten_placement_for_low_degree_net_(self)

        # No two same-model transistors may occupy the same (x, z) slot.
        # _add_distinct_placement picks 1-D AddAllDifferent or packed-int N-D
        # automatically; here we pass 2-D since z varies across the 4 QFET tiers.
        if self.q_tech.height_config == "SH":
            # Packed-int distinct: size must cover the VALUE range, not the
            # cardinality. plc_ci / plc_zi are sparse LGG indices (e.g. {1,3}
            # for cols, {2,5} for tiers), so `len(...)` undersizes the domain
            # and presolve hits empty-domain on `packed = x*z_size + z`.
            x_size = max(self.plc_ci) + 1
            z_size = max(self.plc_zi) + 1
            self._add_distinct_placement("pmos", (tmp_pmos_x_var, x_size), (tmp_pmos_z_var, z_size))
            self._add_distinct_placement("nmos", (tmp_nmos_x_var, x_size), (tmp_nmos_z_var, z_size))

        # Bind each transistor to a bool per candidate placement column AND tier.
        # The _ci_ and _zi_ versions are parallel: same shape, different axis.
        # Together they enable per-slot reasoning (e.g. _init_diffusion_break_vars
        # ANDs the two to derive "this transistor is at slot (ci, zi)").
        for tran in self.circuit.transistors.values():
            tvar = self.transistor_vars[tran.name]
            for ci in self.plc_ci:
                placed = self.opt.NewBoolVar(f"tran_placed_col_{tran.name}_{ci}")
                self.placed_tran_ci_vars[(tran.name, ci)] = placed
                self.opt.Add(tvar.x_var == ci).OnlyEnforceIf(placed)
                self.opt.Add(tvar.x_var != ci).OnlyEnforceIf(placed.Not())
            for zi in self.plc_zi:
                placed = self.opt.NewBoolVar(f"tran_placed_tier_{tran.name}_{zi}")
                self.placed_tran_zi_vars[(tran.name, zi)] = placed
                self.opt.Add(tvar.z_var == zi).OnlyEnforceIf(placed)
                self.opt.Add(tvar.z_var != zi).OnlyEnforceIf(placed.Not())

        # Per-slot AND-reifier: placed_at_xzi[(tran, ci, zi)] iff placed at (ci, zi).
        # Consumed by _init_diffusion_break_vars and the placement constraints in
        # placement.py - centralizing here avoids redundant inline reification.
        for tran in self.circuit.transistors.values():
            for ci in self.plc_ci:
                placed_ci = self.placed_tran_ci_vars[(tran.name, ci)]
                for zi in self.plc_zi:
                    placed_zi = self.placed_tran_zi_vars[(tran.name, zi)]
                    is_at = self.opt.NewBoolVar(f"{tran.name}_at_ci{ci}_zi{zi}")
                    self.opt.AddBoolAnd([placed_ci, placed_zi]).OnlyEnforceIf(is_at)
                    self.opt.AddBoolOr([placed_ci.Not(), placed_zi.Not()]).OnlyEnforceIf(is_at.Not())
                    self.placed_tran_at_xzi_vars[(tran.name, ci, zi)] = is_at

        # Aggregate: has_tran_at_{ci,zi}[idx] == OR over all transistors of placed_at(idx).
        for ci in self.plc_ci:
            placed_here = [self.placed_tran_ci_vars[(tran, ci)] for tran in self.transistor_vars.keys()]
            has_tran = self.opt.NewBoolVar(f"has_tran_at_ci_{ci}")
            self.opt.AddBoolOr(placed_here).OnlyEnforceIf(has_tran)
            self.opt.Add(sum(placed_here) == 0).OnlyEnforceIf(has_tran.Not())
            self.has_tran_at_ci_vars[ci] = has_tran
        for zi in self.plc_zi:
            placed_here = [self.placed_tran_zi_vars[(tran, zi)] for tran in self.transistor_vars.keys()]
            has_tran = self.opt.NewBoolVar(f"has_tran_at_zi_{zi}")
            self.opt.AddBoolOr(placed_here).OnlyEnforceIf(has_tran)
            self.opt.Add(sum(placed_here) == 0).OnlyEnforceIf(has_tran.Not())
            self.has_tran_at_zi_vars[zi] = has_tran
        # Per-slot aggregate - used by per-tier routing helpers in routing.py
        # (bind_gate_sharing_to_columns, gate_cut_window, etc.) to gate the
        # "no transistor at this slot -> trivially shared" branch.
        for ci in self.plc_ci:
            for zi in self.plc_zi:
                placed_here = [
                    self.placed_tran_at_xzi_vars[(tran, ci, zi)]
                    for tran in self.transistor_vars.keys()
                ]
                has_tran = self.opt.NewBoolVar(f"has_tran_at_ci{ci}_zi{zi}")
                self.opt.AddBoolOr(placed_here).OnlyEnforceIf(has_tran)
                self.opt.Add(sum(placed_here) == 0).OnlyEnforceIf(has_tran.Not())
                self.has_tran_at_xzi_vars[(ci, zi)] = has_tran
    
    def _add_distinct_placement(self, kind: str, *dims):
        """
        Enforce that a group of transistors occupy pairwise-distinct N-D positions.

        Args:
            kind:  label for variable naming (e.g. "pmos", "nmos", "all").
            *dims: 1 to N tuples of (var_list, dim_size):
                var_list  parallel list of CP-SAT IntVars (one entry per transistor
                          in the group; all var_lists must have the same length)
                dim_size  exclusive upper bound for that dim's coord values
                          (e.g. max(plc_ci) + 1 for x, n_tiers for z)

        Picks the formulation based on N:
            1-D    : AddAllDifferent on the single var list (CP-SAT native primitive,
                     no auxiliary vars)
            2-D / 3-D / N-D :
                     Pack each (coord_0, coord_1, ..., coord_{N-1}) triple into a
                     single integer via positional base-N encoding (multipliers are
                     suffix-products of dim_size). Then AddAllDifferent on the packed
                     values. Bijective by construction since each coord lives in
                     [0, dim_size_i), so distinct N-tuples map to distinct ints.

        OR-Tools has no NoOverlap3D; the packed-int formulation generalizes cleanly
        to any N and avoids building auxiliary IntervalVars. Equivalent in expressive
        power to AddAllDifferent on tuples.

        Examples:
            # Only x varies (single-tier)
            self._add_distinct_placement("nmos", (x_nmos, max(self.plc_ci) + 1))

            # Multi-tier QFET (z varies, y fixed by transistor model)
            self._add_distinct_placement("nmos",
                (x_nmos, max(self.plc_ci) + 1),
                (z_nmos, len(self.plc_zi)),
            )
        """
        if not dims:
            raise ValueError("_add_distinct_placement needs at least one dimension")
        var_lists = [d[0] for d in dims]
        sizes = [d[1] for d in dims]
        n_trans = len(var_lists[0])
        for vl in var_lists[1:]:
            if len(vl) != n_trans:
                raise ValueError("All dimension var_lists must have the same length")

        if len(dims) == 1:
            self.opt.AddAllDifferent(var_lists[0])
            return

        # N-D (N >= 2): pack via positional base-N encoding.
        # multipliers[i] = product of sizes[i+1:]   (so multipliers[-1] == 1)
        multipliers = [1] * len(dims)
        for i in range(len(dims) - 2, -1, -1):
            multipliers[i] = multipliers[i + 1] * sizes[i + 1]
        max_packed = multipliers[0] * sizes[0]

        packed_vars = []
        for t in range(n_trans):
            packed = self.opt.NewIntVar(0, max_packed - 1, f"distinct_{kind}_packed_{t}")
            self.opt.Add(
                packed == sum(var_lists[i][t] * multipliers[i] for i in range(len(dims)))
            )
            packed_vars.append(packed)
        self.opt.AddAllDifferent(packed_vars)

    def _init_diffusion_break_vars(self):
        """
        Create per-slot diffusion-break vars + the legacy per-col aggregates.

        Slot (z-aware, canonical):
            db_pmos_vars[(ci, zi)] iff no PMOS transistor at slot (ci, zi)
            db_nmos_vars[(ci, zi)] iff no NMOS transistor at slot (ci, zi)

        "Transistor at slot" is reified from placed_tran_ci_vars AND
        placed_tran_zi_vars (set up by _init_transistor_vars). For each
        (tran, ci, zi) we build is_at = placed_ci AND placed_zi.

        Per-col aggregate (kept for placement.py + objective.py):
            db_pmos_cols_vars[ci] iff all tiers at col ci have a PMOS DB
                                     (i.e. AND over zi of db_pmos_vars[(ci, zi)])
            db_nmos_cols_vars[ci] iff same for NMOS

        Semantics: "DB iff no transistor at slot" - the per-slot generalization
        of the per-col "DB iff no transistor at col" across the z axis.

        The N_trans x N_ci x N_zi per-slot reifiers are created once in
        _init_transistor_vars (self.placed_tran_at_xzi_vars) and reused here +
        by placement.py constraints - no redundant reification.
        """
        self.opt.log_comment("Diffusion break variables (z-aware, per slot)")

        for ci in self.plc_ci:
            for zi in self.plc_zi:
                pdb_var = self.opt.NewBoolVar(f"db_pmos_ci{ci}_zi{zi}")
                ndb_var = self.opt.NewBoolVar(f"db_nmos_ci{ci}_zi{zi}")
                self.db_pmos_vars[(ci, zi)] = pdb_var
                self.db_nmos_vars[(ci, zi)] = ndb_var

                pmos_at_slot, nmos_at_slot = [], []
                for tran in self.circuit.transistors.values():
                    is_at = self.placed_tran_at_xzi_vars[(tran.name, ci, zi)]
                    if tran.model == Model.PMOS:
                        pmos_at_slot.append(is_at)
                    elif tran.model == Model.NMOS:
                        nmos_at_slot.append(is_at)

                # DB at slot iff no transistor of that model at slot
                self.opt.Add(sum(pmos_at_slot) == 0).OnlyEnforceIf(pdb_var)
                self.opt.Add(sum(pmos_at_slot) >= 1).OnlyEnforceIf(pdb_var.Not())
                self.opt.Add(sum(nmos_at_slot) == 0).OnlyEnforceIf(ndb_var)
                self.opt.Add(sum(nmos_at_slot) >= 1).OnlyEnforceIf(ndb_var.Not())

        # Per-col aggregates (placement.py / objective.py use these).
        # "All tiers at col are DBs" via AND over per-slot bools.
        for ci in self.plc_ci:
            for model, slot_dict, col_dict in (
                (Model.PMOS, self.db_pmos_vars, self.db_pmos_cols_vars),
                (Model.NMOS, self.db_nmos_vars, self.db_nmos_cols_vars),
            ):
                tag = "pmos" if model == Model.PMOS else "nmos"
                agg = self.opt.NewBoolVar(f"db_{tag}_all_tiers_ci{ci}")
                per_tier = [slot_dict[(ci, zi)] for zi in self.plc_zi]
                self.opt.AddBoolAnd(per_tier).OnlyEnforceIf(agg)
                self.opt.AddBoolOr([v.Not() for v in per_tier]).OnlyEnforceIf(agg.Not())
                col_dict[ci] = agg

    def _init_src_super_inner_nodes_vars(self):
        """
        Create BoolVars for each net's source-pin candidate positions, fanned
        out across every placement tier.

        Output: node_is_src_vars[net.name][(layer_idx, row, col)] = bv
        Also registers each bv on the transistor's s/g/d_col_idx_var dict
        (via _populate_pin_candidates).
        """
        self.opt.log_comment("Super Inner Nodes for src pins")
        for net in self.circuit.get_nets(with_power_ground=False):
            self.node_is_src_vars[net.name] = {}
            tran_name, pin_role = net.source()
            self._populate_pin_candidates(
                net=net,
                tran=self.circuit.transistors[tran_name],
                tvar=self.transistor_vars[tran_name],
                pin_role=pin_role,
                target_dict=self.node_is_src_vars[net.name],
                var_prefix=f"net_issrc_{net.name}",
            )

    def _init_term_super_inner_nodes_vars(self):
        """
        Create BoolVars for each net's terminal-pin candidate positions per
        terminal k, fanned out across every placement tier.

        Output: node_is_term_vars[net.name][k][(layer_idx, row, col)] = bv
        Also registers each bv on the transistor's s/g/d_col_idx_var dict
        (via _populate_pin_candidates).
        """
        self.opt.log_comment("Super Inner Nodes for terminal pins")
        for net in self.circuit.get_nets(with_power_ground=False):
            self.node_is_term_vars[net.name] = {}
            for k, (tran_name, pin_role) in enumerate(net.terminals()):
                self.node_is_term_vars[net.name][k] = {}
                self._populate_pin_candidates(
                    net=net,
                    tran=self.circuit.transistors[tran_name],
                    tvar=self.transistor_vars[tran_name],
                    pin_role=pin_role,
                    target_dict=self.node_is_term_vars[net.name][k],
                    var_prefix=f"net_isterm_{net.name}_{k}",
                )

    def _populate_pin_candidates(self, *, net, tran, tvar, pin_role, target_dict, var_prefix):
        """
        Create one BoolVar per (placement-tier x pin-access-row x candidate col)
        where the col matches the pin role's parity.

        Iterates every layer in q_tech.placement_layer_names so candidates fan
        out across all placement tiers (BPC2 / BPC1 / PC1 / PC2). All tiers
        share the same col grid (per tech.py), so self.pc_ci is reusable across.

        Each created bool is registered in two places:
          1) target_dict[(layer_idx, row, col)]              -- caller's pin map
          2) tvar.{s|g|d}_col_idx_var[net.name][zi][col]     -- transistor's
             per-tier per-col map. Keyed by placement-tier z-index so the
             placement constraints in placement.py can reason about which tier
             a candidate belongs to (was flat [net][col] before z-aware QFET).
        """
        parity_method_name, tvar_attr = self._PIN_ROLE_CONFIG[pin_role]
        parity_check = getattr(self.lgg, parity_method_name)
        if tran.model == Model.PMOS:
            pin_rows = self.pmos_pin_access_ri
        elif tran.model == Model.NMOS:
            pin_rows = self.nmos_pin_access_ri
        else:
            raise ValueError(f"Transistor {tran.name} is not PMOS or NMOS (model={tran.model})")

        per_tier_dict = getattr(tvar, tvar_attr).setdefault(net.name, {})

        for layer_name in self.q_tech.placement_layer_names:
            layer_idx = self.lgg.layer_index(layer_name)
            per_col_dict = per_tier_dict.setdefault(layer_idx, {})
            for ri in pin_rows:
                row = self.lgg.row_in_layer(layer_name, ri)
                for ci in self.pc_ci:
                    col = self.lgg.col_in_layer(layer_name, ci)
                    if not parity_check(layer=layer_name, col=col):
                        continue
                    bv = self.opt.NewBoolVar(f"{var_prefix}_L{layer_name}_R{row}_C{col}")
                    per_col_dict.setdefault(col, []).append(bv)
                    target_dict[(layer_idx, row, col)] = bv

    def _gather_region_nodes(self, zi, col, model):
        """
        Return LGG nodes (layer_idx, row, col) on placement-tier `zi` at column
        `col` matching `model`'s pin-access rows.

        Used by placement constraints (placement.py) to identify the set of
        graph nodes that a given source/drain/gate occupies when a transistor
        is placed at (col, zi). Symmetric with _populate_pin_candidates: the
        same (layer_idx, row, col) keys are produced.
        """
        layer_name = self.lgg.idx_to_layer[zi]
        pin_rows_ri = self._pin_access_ri_for(model)
        return [(zi, self.lgg.row_in_layer(layer_name, ri), col) for ri in pin_rows_ri]

    def _pin_access_ri_for(self, model):
        """Return the pin-access row-index list for the given transistor model."""
        if model == Model.PMOS:
            return self.pmos_pin_access_ri
        if model == Model.NMOS:
            return self.nmos_pin_access_ri
        raise ValueError(f"Unknown model: {model}")

    def _gather_via_vars_in_region(self, zi, col, model):
        """
        Return edge_vars for vias (cross-layer edges) landing at (zi, col) on
        `model`'s pin-access rows.

        For each pin-access node at (zi, row, col), candidate via neighbors are
        (zi +/- 1, row, col); collect the edge_var if such an edge exists in the LGG.
        Consumed by placement.py constraints that ban/condition via use.
        """
        layer_name = self.lgg.idx_to_layer[zi]
        via_vars = []
        for ri in self._pin_access_ri_for(model):
            row = self.lgg.row_in_layer(layer_name, ri)
            anchor = (zi, row, col)
            if not self.lgg.is_node_in_graph(anchor):
                continue
            for dz in (-1, +1):
                neighbor = (zi + dz, row, col)
                if not self.lgg.is_node_in_graph(neighbor):
                    continue
                # nx.Graph edges are undirected; key can be in either order
                for key in ((anchor, neighbor), (neighbor, anchor)):
                    if key in self.edge_vars:
                        via_vars.append(self.edge_vars[key])
                        break
        return via_vars

    def _gather_src_term_vars_in_region(self, zi, col, model):
        """
        Return all source + terminal bool vars at (zi, col) on `model`'s
        pin-access rows, across all non-power-ground nets.

        Used by placement.py prohibit_CA_contact_on_non_source_term_columns to
        require at least one src/term occupant at any col where a via lands.
        """
        layer_name = self.lgg.idx_to_layer[zi]
        out = []
        nodes_in_region = [
            (zi, self.lgg.row_in_layer(layer_name, ri), col)
            for ri in self._pin_access_ri_for(model)
        ]
        for net in self.circuit.get_nets(with_power_ground=False):
            src_dict = self.node_is_src_vars.get(net.name, {})
            term_dicts = self.node_is_term_vars.get(net.name, {})
            for node_key in nodes_in_region:
                if node_key in src_dict:
                    out.append(src_dict[node_key])
                for k_dict in term_dicts.values():
                    if node_key in k_dict:
                        out.append(k_dict[node_key])
        return out

    # ----- net-flow policy ------------------------------------------------
    # Threshold for switching from per-commodity boolean flow to scalar integer
    # flow (single IntVar per arc instead of K BoolVars). With tree enforcement,
    # integer flow is uniquely determined by the tree + terminals, so per-terminal
    # bools are redundant - saves (K - 1) * |arcs| BoolVars per qualifying net.
    _INT_FLOW_THRESHOLD = 3        # use int flow for K >= 3 on internal nets
    _INT_FLOW_DEBUG_NET = None     # None: use threshold; str / set: force-int those nets

    # ----- edge cost defaults --------------------------------------------
    # Per-edge costs live in cell_config (metal_cost, via_cost,
    # virtual_edge_cost). These class constants are fallbacks used when the
    # config dict is missing a key. _edge_cost reads off self at runtime.
    _EDGE_COST_WIRE         = 1
    _EDGE_COST_VIA          = 3
    _EDGE_COST_VIRTUAL_EDGE = 5

    def _init_net_flow_vars(self):
        """
        Create per-net flow variables on every LGG arc.

        Flow type per net is picked by _should_use_int_flow:
            int  flow: 1 IntVar(0, K) per arc, key = (net, 0, u_arc, v_arc)
            bool flow: K BoolVars per arc, key = (net, k, u_arc, v_arc) for k in 0..K-1

        K = num_terminals + 1 for IO nets (extra slot for the I/O pin) else
            num_terminals. Increments self.num_pins_for_io as a side effect.
        """
        self.opt.log_comment("Net flow variables")
        arcs = list(self.lgg.arcs())  # cache once; multiple iterations per net
        for net in self.circuit.get_nets(with_power_ground=False):
            num_extra_flow = 1 if net.is_io_net() else 0
            self.num_pins_for_io += num_extra_flow
            total_k = net.num_terminals() + num_extra_flow

            if self._should_use_int_flow(net, total_k, num_extra_flow):
                self._int_flow_nets[net.name] = total_k
                for u_arc, v_arc in arcs:
                    self.net_flow_vars[(net.name, 0, u_arc, v_arc)] = self.opt.NewIntVar(
                        0, total_k, f"iflow_{net.name}_{u_arc}_{v_arc}",
                    )
                self.net_to_flow_cnt[net.name] = 1
                logger.info(
                    f"\t==\tNet {net.name}: integer flow (K={total_k}, "
                    f"saved {(total_k - 1) * len(arcs)} BoolVars)"
                )
            else:
                for k in range(total_k):
                    for u_arc, v_arc in arcs:
                        self.net_flow_vars[(net.name, k, u_arc, v_arc)] = self.opt.NewBoolVar(
                            f"flow_{net.name}_{k}_{u_arc}_{v_arc}",
                        )
                self.net_to_flow_cnt[net.name] = total_k

    def _should_use_int_flow(self, net, total_k, num_extra_flow) -> bool:
        """
        Decide whether `net` uses scalar integer flow vs per-commodity bool flow.

        Integer flow is only valid for internal nets (num_extra_flow == 0) - IO
        nets have asymmetric source flow that needs per-commodity expression.

        Default policy: int flow when K >= _INT_FLOW_THRESHOLD.
        Debug override: _INT_FLOW_DEBUG_NET (str or container of names) forces
        int flow on the named net(s) regardless of K.
        """
        if num_extra_flow != 0:
            return False
        dbg = self._INT_FLOW_DEBUG_NET
        if dbg is not None:
            return net.name == dbg if isinstance(dbg, str) else net.name in dbg
        return total_k >= self._INT_FLOW_THRESHOLD

    def _init_net_arc_vars(self):
        """One BoolVar per (net, arc): true iff this net uses this directed arc."""
        self.opt.log_comment("Net arc variables")
        for net in self.circuit.get_nets(with_power_ground=False):
            for u_arc, v_arc in self.lgg.arcs():
                self.net_arc_vars[(net.name, u_arc, v_arc)] = self.opt.NewBoolVar(
                    f"arc_{net.name}_{u_arc}_{v_arc}",
                )

    def _init_edge_vars(self):
        """
        One BoolVar per undirected LGG edge plus its cost (used by the objective).

        Costs come from cell_config (metal_cost / via_cost / virtual_edge_cost);
        class constants (_EDGE_COST_*) are fallbacks when a key is missing.
        Edge kind:
          - wire    : same layer
          - via     : adjacent layers (|dz| == 1)
          - virtual : non-adjacent layers (|dz| >  1) - i.e. a VL jump
        """
        # Resolve costs once per build (cheaper than per-edge dict lookups,
        # also makes the values trivially inspectable).
        self._metal_cost   = int(self._cfg_get("metal_cost",         self._EDGE_COST_WIRE))
        self._via_cost     = int(self._cfg_get("via_cost",           self._EDGE_COST_VIA))
        self._virtual_cost = int(self._cfg_get("virtual_edge_cost",  self._EDGE_COST_VIRTUAL_EDGE))
        logger.info(
            f"\t==\tEdge costs: metal={self._metal_cost} "
            f"via={self._via_cost} virtual={self._virtual_cost}"
        )

        self.opt.log_comment("Edge variables")
        for u_edge, v_edge in self.lgg.edges():
            self.edge_vars[(u_edge, v_edge)] = self.opt.NewBoolVar(f"edge_{u_edge}_{v_edge}")
            self.edge_to_cost[(u_edge, v_edge)] = self._edge_cost(u_edge, v_edge)

    def _edge_cost(self, u_edge, v_edge) -> int:
        """Return cost for an edge.
            same layer       -> metal_cost
            adjacent layers  -> via_cost
            non-adjacent     -> virtual_edge_cost   (VL jump)
        """
        dz = abs(u_edge[0] - v_edge[0])
        if dz == 0:
            return self._metal_cost
        if dz == 1:
            return self._via_cost
        return self._virtual_cost

    def _init_SON_positions(self):
        """
        Identify Super Outer Node positions for I/O pin placement.

        Layers come from `q_tech.layer_stack.io_pin_layers()` - driven by the
        JSON's `io_pin: true` flag. Never falls back to a hardcoded list:
        flip `io_pin` in the layer JSON to add/remove pin-access surfaces.

        Horizontal layers are still col-filtered against the adjacent
        vertical layer's grid (so a via from above can land on the pin).
        """
        for io_layer in self.q_tech.layer_stack.io_pin_layers():
            layer_name = io_layer.layer_name
            self.son_terminal_nodes[layer_name] = self._collect_son_nodes_for_layer(layer_name)
            logger.info(
                f"\tSON positions on '{layer_name}' "
                f"({self.lgg.layer_to_direction.get(layer_name, '?')}): "
                f"{len(self.son_terminal_nodes[layer_name])} node(s)"
            )

    def _collect_son_nodes_for_layer(self, layer_name) -> list:
        """
        Collect SON candidate nodes on `layer_name`: ALL rows of the layer
        (matches ?FET `use_all_rows=True`), optionally col-filtered for
        horizontal pin-access layers.
        """
        if layer_name not in self.lgg.layer_to_idx:
            logger.warning(f"\tSON: io_pin layer {layer_name!r} not in LGG; skipping.")
            return []

        all_row_indices = self.lgg.row_indices_in_layer(layer_name)
        if not all_row_indices:
            return []
        son_rows = {self.lgg.row_in_layer(layer_name, ri) for ri in all_row_indices}

        # Horizontal layers: restrict cols to those overlapping the adjacent
        # vertical layer's grid (so a via from the vertical layer can land on
        # the pin). Adjacent layer is found by walking LGG stack adjacency -
        # no hardcoded name mapping needed.
        allowed_cols = None
        if self.lgg.layer_to_direction.get(layer_name) == "H":
            vert_layer = self._adjacent_vertical_layer_for(layer_name)
            if vert_layer is not None:
                allowed_cols = set(self.lgg.cols_in_layer(vert_layer))
            else:
                logger.info(
                    f"\tSON: no adjacent vertical layer found for {layer_name!r}; "
                    "emitting unfiltered cols."
                )

        return [
            node for node in self.lgg.nodes_in_layer(layer_name)
            if node[1] in son_rows
            and (allowed_cols is None or node[2] in allowed_cols)
        ]

    def _adjacent_vertical_layer_for(self, layer_name: str):
        """
        Find the vertical pin layer immediately adjacent to `layer_name` in the
        LGG stack.

        Walks z+1 (above, preferred) then z-1 (below); returns the first
        vertical neighbor found, or None if neither exists / neither is vertical.

        Pure stack-topology lookup - works for any naming convention (Mn / BMn /
        any prefix), any stack depth. Encodes the standard chip-stack convention:
        even-indexed metals run horizontally, odd run vertically, so every
        horizontal layer has a vertical neighbor at z +/- 1.

        Examples (typical stack):
            z=0 "M0"  (H)  -> "M1"  (z=1, V)         above
            z=2 "M2"  (H)  -> "M3"  (z=3, V) or "M1" above-then-below
            z=10 "BM0" (H) -> "BM1" (z=11, V)        above
        """
        z = self.lgg.layer_to_idx.get(layer_name)
        if z is None:
            return None
        for candidate_z in (z + 1, z - 1):
            if candidate_z in self.lgg.idx_to_layer:
                candidate = self.lgg.idx_to_layer[candidate_z]
                if self.lgg.layer_to_direction.get(candidate) == "V":
                    return candidate
        return None

    def _init_SON_vars(self):
        """
        Create BoolVars for Super Outer Node (I/O pin) placement.

        For each I/O net and each extra-flow commodity k, a candidate bool is
        created for every SON node ACROSS ALL pin-access layers (merged into one
        flow). Exactly one SON is chosen per (net, k) - EITHER layer, not both -
        enforced by `sum(bools) == 1`.

        Outputs:
            node_is_SON_vars[net.name][k][(layer_idx, row, col)] = bv
            node_to_net_SON_vars[(layer_idx, row, col)][net.name] = [bv, ...]
        """
        self.opt.log_comment("Super Outer Nodes for I/O pins")
        for net in self.circuit.get_nets(with_power_ground=False):
            if not net.is_io_net():
                continue
            self.node_is_SON_vars[net.name] = {}
            # I/O nets get extra flow commodities past their regular terminals.
            for k in range(net.num_terminals(), self.net_to_flow_cnt[net.name]):
                slot = {}
                for layer_name in self.q_tech.pin_access_layer_names:
                    for node in self.son_terminal_nodes.get(layer_name, []):
                        layer_idx, row, col = node
                        bv = self.opt.NewBoolVar(
                            f"net_isSON_{net.name}_{k}_L{layer_idx}_R{row}_C{col}"
                        )
                        slot[(layer_idx, row, col)] = bv
                        self.node_to_net_SON_vars.setdefault(
                            (layer_idx, row, col), {}
                        ).setdefault(net.name, []).append(bv)
                self.node_is_SON_vars[net.name][k] = slot
                # Exactly one SON across ALL pin-access layers (EITHER, not BOTH)
                self.opt.Add(sum(slot.values()) == 1)

    def _init_cpp(self):
        """
        Set up everything anchored to the cell's right edge - boundary coords +
        the cpp_cost solver var + its lower bound + warm-start hints.

        The same "cell right edge" axis is expressed in TWO unit systems:

            cpp_cost              col-index space  solver IntVar = max(x_vars)
                                                   used by the CPP objective
            max_boundary_col      physical coord   rightmost placement-layer col coord
            min_boundary_col      physical coord   max_boundary_col - DB allowance
                                                   consumed by pin.py + routing.py

        DB allowance, same concept, two units:
            idx-space   db_bound  = insert_num_db * stride   (stride between plc_ci)
            coord-space db_offset = insert_num_db * 2 * pitch (== stride * pitch)

        cpp_cost lower bound = packing requirement + DB padding.

        Warm-start hints: pack transistors at the lowest plc_ci columns and seed
        cpp_cost at the lower bound, so the solver converges faster.

        TODO (z-aware tighter bound): with the QFET 4-tier z-axis, the per-row N
        could drop to ceil(num_model / n_tiers) instead of num_model. The
        conservative bound is kept - always valid, never rejects feasible
        solutions; revisit when the CPP objective dominates solve time.
        """
        self.opt.log_comment("Cell boundary coords + CPP cost variable")

        # 1) Boundary coords (physical units) - sets the extent the solver lives within
        self._cache_cell_boundary_coords()

        # 2) cpp_cost = max(transistor x_vars) (col-index units)
        self.cpp_cost = self.opt.NewIntVarFromDomain(self.domain_sd_ci, "cpp_cost")
        self.opt.AddMaxEquality(
            self.cpp_cost,
            [self.transistor_vars[tran.name].x_var for tran in self.circuit.transistors.values()],
        )

        # 3) Lower bound + warm-start are SH-mode-specific; bail otherwise.
        if self.q_tech.height_config != "SH":
            return
        n_per_model = max(self.circuit.num_pmos_transistors(), self.circuit.num_nmos_transistors())
        if n_per_model <= 0:
            return

        # Warm-start hints: pack each model's transistors at the lowest columns
        # (with z-aware placement, PMOS and NMOS can share these columns on
        # different tiers - that's why each model gets the same packing hint).
        # NOTE: no explicit cpp_cost lower bound - the CPP objective (weight
        # 1000) drives cpp_cost down, and db_placement rewards (max ci-weighted
        # empties) push trans left naturally. Adding a pack+DB lower bound
        # blocked the cpp=1 optimum, leaving BUF stuck at ci=3.
        sorted_plc_ci = sorted(self.plc_ci)
        for model in (Model.PMOS, Model.NMOS):
            trans = [t for t in self.circuit.transistors.values() if t.model == model]
            for i, tran in enumerate(trans):
                if i < len(sorted_plc_ci):
                    self.opt.AddHint(self.transistor_vars[tran.name].x_var, sorted_plc_ci[i])
        self.opt.AddHint(self.cpp_cost, sorted_plc_ci[0])
        
    def _placement_constraints(self):
        """
        Build placement constraints via the per-tier free functions in
        src.cellgen.core.placement (each passed `self`).
        """
        # Per-tier modernized (src.cellgen.core.placement):
        plc.link_source_drain_gate_columns_to_transistor_placement(self)
        plc.ban_other_nets_on_pwr_columns(self)
        plc.prohibit_CA_contact_on_non_source_term_columns(self)
        plc.diffusion_alignment(self)
        if self.use_break_symmetry:
            plc.placement_lexico_order_symmetry_breaking(self)
            plc.placement_site_flip_symmetry_breaking(self)

        # Per-tier pairwise sharing (src.cellgen.core.placement):
        plc.pairwise_diffusion_sharing(self)
        plc.pairwise_lisd_sharing(self)
        plc.pairwise_gate_sharing(self)
    
    def _routing_constraints(self, include_external_son: bool = False):
        """
        Top-level routing constraint orchestration.

        Always emits boundary / gate-cut / DB / contact-cap / localization /
        LISD / link / uniqueness / internal-flow / node_exclusivity. The single
        toggle is `include_external_son`:
          False (stage "internal") - skip `rt.induce_external_routing_flow`.
          True  (stage "external") - also enforce IO-side SON flow conservation.

        Mirrors `_placement_constraints` shape: every call is a free function
        in `src.cellgen.core.routing` (`rt`) or `src.cellgen.core.rule`, with
        `self` passed in so attribute writes land on the QFET instance.

        State containers are pre-created in `_init_state_containers`. Scalar
        config knobs (tolerances, `min_gate_cut_len`) are read inline via
        `_cfg_get` and cached onto `self` for the rt.* callees.

        The DRC block (geometric / EOL / MAR / via-separation rules and the
        top-metal usage / IO-pin geometry helpers) is gated out by the `return`
        after `node_exclusivity`.
        """
        # ----- scalar config knobs (consumed by the rt.* callees) ----------
        self.min_gate_cut_len = self._cfg_get("minimum_gate_cut_length", 1)
        rl_cfg = self.cell_config.get("routing_localization", {}) or {}
        self.routing_tolerance_x          = rl_cfg.get("tolerance_x", 45)
        self.routing_tolerance_y          = rl_cfg.get("tolerance_y", 48)
        self.routing_tolerance_per_fanout = rl_cfg.get("tolerance_per_fanout", 0)
        self.prevent_routing_OOB          = rl_cfg.get("prevent_routing_OOB", True)

        # ----- boundary + gate cut + DB protection -------------------------
        # Per-tier modernized for QFET (src.cellgen.core.routing).
        rt.prohibit_routing_to_left_cell_boundaries(self)
        rt.bind_gate_sharing_to_columns(self)
        rt.gate_cut_window(self)
        rt.enforce_CA_pickup_for_gate_cut(self)
        rt.prohibit_pc_routing_in_diffusion_break_cols(self)

        # ----- gate contact cap (off -> tighten) ----------------------------
        # lig_routing / lisd_routing defaults flipped to True for QFET (was False).
        # True == ALLOW additional gate/LISD contacts (skip the per-slot
        # `limit_gate/lisd_contact <= 1` constraint). With the 2-tier QFET
        # stack having fewer placement cols and a 3-via MIV chain, the
        # 1-contact cap was over-tight and contributed to dangling routes
        # (couldn't add a second tap to anchor a metal segment). Override
        # to False in cell_config if explicit single-contact is needed.
        if not self._cfg_get("lig_routing", True):
            rt.limit_gate_contact(self, num_contact=1)

        # ----- routing-window localization + right-boundary OOB ------------
        self.opt.log_comment("Routing Window Constraint ...")
        rt.routing_localization(self)
        if self.prevent_routing_OOB:
            rt.prohibit_routing_to_right_cell_boundaries(self)

        # ----- LISD sharing + contact cap (off -> tighten) ------------------
        rt.bind_lisd_sharing_to_columns(self)
        if not self._cfg_get("lisd_routing", True):
            rt.limit_lisd_contact(self, num_contact=1)

        # ----- flow <-> arc <-> edge linking + net/SON uniqueness --------------
        self.opt.log_comment("Linking flow variables to arc usage ...")
        rt.link_flow_to_arc(self)
        rt.link_arc_to_edge(self)
        # AtMostOne over the via-stack {VL, MIV1, MIV2, ...} at any (r, c)
        # where a virtual jump lands. No-op when no virtual pairs in the LGG.
        rt.prohibit_virtual_edge_shorting(self)
        rt.net_has_one_src_and_k_terminals(self)
        rt.net_src_node_uniqueness(self)
        rt.net_term_node_uniqueness(self)
        rt.net_SON_node_uniqueness(self)
        rt.prohibit_multiple_SONs_same_column(self)

        # ----- routing flow induction (internal always; external on toggle) -
        rt.induce_internal_routing_flow_with_diffusion(self)
        if include_external_son:
            rt.induce_external_routing_flow(self)
        rt.node_exclusivity(self)

        # ----- via-to-metal connectivity (no orphan vias) ------------------
        # Default OFF: when `link_flow_to_arc` is biconditional (forward+reverse),
        # every active arc carries flow -> every active edge sits on a tree ->
        # no orphan vias possible. via_induce becomes redundant AND conflicts
        # with terminal nodes (the via-landing node has no outgoing flow, but
        # via_induce demands a same-direction metal extension which itself
        # requires flow under arc<->flow - contradiction pushes SON away from
        # the via column, stretching IO routes by 3-5 metal segments).
        # Set True if you've turned arc<->flow back to forward-only.
        if self._cfg_get("enable_via_induce", False):
            supervia_params = {layer: False for layer in self.lgg.layer_to_idx}
            for layer in self._cfg_get("supervia", []):
                if layer in supervia_params:
                    supervia_params[layer] = True
            rule.via_induce_vertical_metal(self, supervia_params)
            rule.via_induce_horizontal_metal(self, supervia_params)

        # The rule.* DRC block below (geometric / EOL / MAR / via-separation,
        # top-metal usage, IO-pin geometry) is gated by the return below.
        return

        # ----- design rules: geometric / EOL / MAR / via -------------------
        self.opt.log_comment("Adding geometric variables ...")
        rule.geometric_vars_in_horizontal_layers(self)
        rule.geometric_vars_in_vertical_layers(self)

        eol_params = dict(self._cfg_get("eol_c2c_rule", {}))
        rule.eol_rules_in_horizontal_layers(self, eol_params)
        rule.eol_rules_in_vertical_layers(self, eol_params)

        mar_params = dict(self._cfg_get("mar_c2c_rule", {}))
        supervia_params = {layer: False for layer in self.lgg.layer_to_idx}
        for layer in self._cfg_get("supervia", []):
            if layer in supervia_params:
                supervia_params[layer] = True
        rule.mar_rules_in_horizontal_layers(self, mar_params, supervia_params)
        rule.mar_rules_in_vertical_layers(self, mar_params, supervia_params)
        rule.via_induce_vertical_metal(self, supervia_params)
        rule.via_induce_horizontal_metal(self, supervia_params)

        # Not wired here:
        #   via_separation_rules(self, via_params)     - via C2C rule (rule.py)
        #   _m1_layer_usage / _bm1_layer_usage         - top-metal usage
        #   _hori_pin_separation / _hori_pin_extension - IO-pin geometry

    def _cache_cell_boundary_coords(self):
        """
        Cache physical-coord right-edge of the cell, accounting for DB allowance.

            max_boundary_col = rightmost placement-layer column coordinate
            min_boundary_col = max_boundary_col - insert_num_db * 2 * pitch

        Consumed externally by pin.py (M0 pin gap validation) and routing.py
        (gate-cut boundary). The DB allowance here (* 2 * pitch in coord space)
        is the same concept as _db_padding_cols (* stride in index space) used
        by the cpp_cost lower bound.
        """
        placement_layer = self.q_tech.default_placement_layer
        pitch = self.q_tech.get_pitch(placement_layer)
        self.max_boundary_col = self.lgg.max_col_in_layer(placement_layer)
        self.min_boundary_col = self.max_boundary_col - self.insert_num_db * 2 * pitch
        logger.info(
            f"\tCell boundary cols: min={self.min_boundary_col}, max={self.max_boundary_col}"
        )

    @staticmethod
    def _packing_min_col(sorted_plc_ci, n):
        """
        Min rightmost-col-index needed to pack n transistors at plc_ci positions.

        For n <= len(plc_ci): the n-th smallest plc_ci column.
        For n  > len(plc_ci): extrapolate using the natural stride between cols.
        """
        if n <= len(sorted_plc_ci):
            return sorted_plc_ci[n - 1]
        stride = sorted_plc_ci[1] - sorted_plc_ci[0] if len(sorted_plc_ci) > 1 else 2
        return sorted_plc_ci[-1] + stride * (n - len(sorted_plc_ci))

    def _db_padding_cols(self, sorted_plc_ci):
        """
        Column-units consumed by inserting self.insert_num_db diffusion breaks.

        Each DB takes one placement slot of `stride` units. Returns 0 when DBs
        are disabled (insert_num_db == 0). TODO: verify the formula matches the
        DB insertion model used in _cache_cell_boundary_coords.
        """
        if self.insert_num_db <= 0:
            return 0
        stride = sorted_plc_ci[1] - sorted_plc_ci[0] if len(sorted_plc_ci) > 1 else 2
        return self.insert_num_db * stride

