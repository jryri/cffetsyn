import math
import os
from collections import OrderedDict, deque

from loguru import logger

from ortools.sat.python import cp_model

from src.cellgen.archit import config
from src.cellgen.archit.FinFET.tech import FinFET_Tech
from src.cellgen.archit.FinFET.util import write_finfet_result
from src.cellgen.core import accelerate
from src.cellgen.core import inject
from src.cellgen.core import pin
from src.cellgen.core import placement as plc
from src.cellgen.core import routing as rt
from src.cellgen.core import rule
from src.cellgen.core.entity import Circuit, Model, PinType
from src.cellgen.core.graph import LayeredGridGraph
from src.cellgen.core.objective import Objective
from src.cellgen.core.util import (
    half_permutations,
    log_variable_info,
    print_smtcell_banner,
    spaced_subsequences,
    split_into_parts,
)
from src.cellgen.core.variable import TransistorVar
from src.cellgen.solver.cpsat_wrapper import CPSAT


class FinFET:
    """
    Formulate and place FinFETs in the circuit.
    """

    # ----- solve policy --------------------------------------------------
    # Default weighted-sum objective table. Each row: (name, Objective method
    # name, default weight, sense). The objective set is:
    #   cpp (1000) dominant; top_layer_usage (100) strong secondary;
    #   gate / lisd / wl / db (1) tertiary tie-breakers.
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

    # Net-flow policy: nets with >= this many flow commodities use a single
    # integer flow IntVar per arc instead of K BoolVars. With tree enforcement
    # the flow is uniquely determined, so the per-commodity bools are redundant.
    _INT_FLOW_THRESHOLD = 3        # use int flow for K >= 3 on internal nets
    _INT_FLOW_DEBUG_NET = None     # None: use threshold; str / set: force-int those nets

    def __init__(
        self,
        circuit: Circuit,
        fin_tech: FinFET_Tech,  # contains layer information
        output_dir: str = "./output/",
        num_col: int | None = None,
        cell_config=None,
        flag_log_constraints: bool = False,
        solver: str = "cpsat",
    ):
        # 1) inputs
        self.circuit = circuit
        self.fin_tech = fin_tech
        # Alias so the shared GENERIC core (instance.q_tech.*) and the FinFET
        # helpers (finfet.tech.*) both resolve to the same tech object.
        self.q_tech = fin_tech
        self.output_dir = output_dir
        # cell_config may arrive as either a path (str) or an already-loaded dict.
        # config.read handles the file path -> dict; pass-through if already dict.
        self.cell_config = config.read(cell_config) if isinstance(cell_config, str) else cell_config
        self.solver_name = solver
        self._apply_config_flags()

        # FinFET is single-tier planar: TransistorVar.z_var stays None and there
        # is no tier (z) placement. The shared accelerate per-tier z_eq gating
        # must therefore use its unconditional form, so pin uses_tier_placement
        # False (consulted by accelerate._uses_tier_placement).
        self.uses_tier_placement = False

        # FinFET MAR/EOL DRC is active, so the reverse arc->flow link in
        # rt.link_flow_to_arc must stay OFF (else the model can be INFEASIBLE).
        # Sourced from the tech (FinFET_Tech.enable_reverse_flow_link == False);
        # set on self BEFORE the routing-constraint phase that calls
        # link_flow_to_arc (rt.link_flow_to_arc reads it via getattr).
        self.enable_reverse_flow_link = getattr(
            fin_tech, "enable_reverse_flow_link", False
        )

        # num_col default: single placement tier, so the minimum-col packing math
        # uses the default num_placement_layers=1.
        self.num_col = (
            num_col if num_col is not None
            else circuit.get_minimum_col(num_db=self.insert_num_db + 1)
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
        self._apply_injections()
        self._constrain_top_layer_usage()

        # 5) solve + write
        self._run_solve()
        self._maybe_write_results()

    @property
    def tech(self):
        return self.fin_tech

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
            archit="FinFET",
            tech=self.fin_tech.lib_name,
            subckt=self.circuit.subckt_name,
        )

    def _init_state_containers(self):
        """
        Pre-create every container that subsequent init methods populate, so
        attribute provenance stays grep-able.
        """
        # geometric primitives (overwritten by _init_graph)
        self.canvas_width = 0
        self.canvas_height = 0

        # transistor metadata (populated by _init_tech)
        self.mos_to_num_finger = {}              # mos_name -> num_finger
        self.nmos_placeable_row_indices = []
        self.pmos_placeable_row_indices = []
        self.nmos_pin_access_ri = []
        self.pmos_pin_access_ri = []

        # transistor / net top-level maps
        self.transistor_vars = {}                # transistor name -> TransistorVar
        self.net_vars = {}                       # net name -> NetVar

        # transistor placement vars (populated by _init_transistor_vars).
        # Parallel _ci_ (col) and _zi_ (tier) axes; _at_xzi_ is the per-slot
        # AND-reifier consumed by _init_diffusion_break_vars + placement.py.
        # FinFET is single-tier so the _zi_ axis has exactly one entry.
        self.placed_tran_ci_vars = {}            # (tran_name, ci) -> bool var
        self.placed_tran_zi_vars = {}            # (tran_name, zi) -> bool var
        self.placed_tran_at_xzi_vars = {}        # (tran_name, ci, zi) -> bool var (= ci AND zi)
        self.has_tran_at_ci_vars = {}            # ci -> bool var
        self.has_tran_at_zi_vars = {}            # zi -> bool var
        self.has_tran_at_xzi_vars = {}           # (ci, zi) -> bool var (OR over transistors at slot)

        # diffusion break vars (populated by _init_diffusion_break_vars).
        # Per-slot is canonical (consumed by the shared placement.py /
        # objective.py); per-col is a backward-compat aggregate.
        self.db_pmos_vars = {}                   # (ci, zi) -> bool var: PMOS DB at slot
        self.db_nmos_vars = {}                   # (ci, zi) -> bool var: NMOS DB at slot
        self.db_vars = {}                        # (ci, zi) -> bool var (set by plc.diffusion_alignment)
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
        # Outer key: zi (placement-tier LGG layer index). Inner key: physical col.
        self.gate_share_at_col_vars = OrderedDict()  # zi -> OrderedDict[col -> gate_share BoolVar]
        self.gate_cut_window_vars = {}               # zi -> {col_tuple -> gate_cut_window BoolVar}

        # LISD sharing (per-tier nested column map; same nesting as gate sharing)
        self.lisd_share_at_col_vars = OrderedDict()  # zi -> OrderedDict[col -> lisd_share BoolVar]

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
        # Per-tier y-window (single-tier for FinFET; shared routing_localization
        # fans these out per placement tier)
        self.window_ymin_tier = {}                   # net.name -> ti -> IntVar
        self.window_ymax_tier = {}                   # net.name -> ti -> IntVar
        self.has_pins_on_tier = {}                   # net.name -> ti -> BoolVar
        self.net_min_y_tier = {}                     # net.name -> ti -> IntVar
        self.net_max_y_tier = {}                     # net.name -> ti -> IntVar

        # design-rule scratch
        self.geometric_vars = {}                     # node -> {left, right, front, back}

        # per-layer usage (diagnostic; feed the m2/m1/m0_usage objectives only)
        self.m2_rows_to_used = {}                    # row -> BoolVar (top-side M2)
        self.m1_cols_to_used = {}                    # col -> BoolVar (M1)
        self.m0_rows_to_used = {}                    # row -> BoolVar (M0)

        # pin / top-layer state
        self.net_use_top_track = {}                  # netname -> BoolVar
        self.net_use_top_track_row_var = {}          # netname -> {row -> BoolVar}

    def _init_model(self, flag_log_constraints: bool):
        """
        Create the optimization model from the selected solver backend.

        Only "cpsat" is wired today; other backends raise NotImplementedError.
        When `flag_log_constraints` is True, every constraint is mirrored to
        `<output_dir>/constraint/<subckt>.log` by the backend wrapper.
        """
        logfile = (
            f"{self.output_dir}/constraint/{self.circuit.subckt_name}.log"
            if flag_log_constraints else None
        )
        builders = {
            "cpsat": lambda: CPSAT(logfile=logfile),
            # TODO: wire the remaining backends from src.cellgen.solver.*_wrapper
        }
        if self.solver_name not in builders:
            raise NotImplementedError(
                f"Solver backend {self.solver_name!r} is not supported. "
                f"Available: {sorted(builders)}."
            )
        self.opt = builders[self.solver_name]()

    def _init_subsystems(self):
        """Initialize graph, tech, CP-SAT variable domain, variables, region caches."""
        self._init_graph()
        self._init_tech()
        self._init_domain()
        self._init_var()
        self._build_region_caches()

    def _build_constraints(self):
        """Emit placement and routing constraints into the CP-SAT model."""
        self._placement_constraints()
        self._routing_constraints()

    def _maybe_inject_clusters(self):
        """Apply cluster injection when enabled in the cell config; otherwise no-op."""
        cluster_cfg = self.cell_config["inject_cluster"]
        if not cluster_cfg["value"]:
            return

        method = cluster_cfg["method"]
        min_cs = cluster_cfg.get("min_cluster_size", 2)
        max_cs = cluster_cfg.get("max_cluster_size", None)

        if isinstance(method, str) and method.endswith(".py"):
            G, clusters = accelerate._load_evolved_clusters_multilevel(
                self.circuit, method, min_cluster_size=min_cs
            )
        else:
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
        Decision strategy prioritizing placement vars: transistor x/flip first,
        then per-net source/terminal/SON bools sorted by (z, row, col).
        FinFET is SH-only single-tier - no z_var, no site_var.
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
            tran_vars.append(tvar.flip_var)

        all_vars = tran_vars + spatial_vars
        if all_vars:
            logger.info(f"\t==\tAdding decision strategy for {len(all_vars)} placement variables")
            self.opt.AddDecisionStrategy(
                all_vars, cp_model.CHOOSE_HIGHEST_MAX, cp_model.SELECT_MAX_VALUE,
            )

    def use_routing_window_strategy(self):
        """Decision strategy on routing bounding-box / coord vars (ROUTE / ALL only)."""
        self.opt.log_comment("Using routing window strategy ...")
        logger.info("\t==\tUsing routing window strategy ...")
        if getattr(self, "routing_tolerance", -1) == -1:
            logger.error("Routing tolerance must be set to use this strategy.")
            return None
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

    def _apply_injections(self):
        """
        Apply edge / arc / flow / placement injections specified in the cell config.

        Each injection-type config encodes node coords as flat tuples that we
        slice into (z, r, c) triples:
            inject_edge[(uz, ur, uc, vz, vr, vc)]          = value
            inject_arc [(net, uz, ur, uc, vz, vr, vc)]     = value
            inject_flow[(net, k, uz, ur, uc, vz, vr, vc)]  = value
            inject_placement = [ (tran, x?, y?, flip?), ... ]
        """
        for k, v in self._cfg_get("inject_edge", {}).items():
            inject.inject_edge(self, (k[0], k[1], k[2]), (k[3], k[4], k[5]), value=v)
        for k, v in self._cfg_get("inject_arc", {}).items():
            inject.inject_arc(self, k[0], (k[1], k[2], k[3]), (k[4], k[5], k[6]), value=v)
        for k, v in self._cfg_get("inject_flow", {}).items():
            inject.inject_flow(self, k[0], k[1], (k[2], k[3], k[4]), (k[5], k[6], k[7]), value=v)

        # Placement injection: config values are physical coords (from .res files);
        # x_var uses column indices, so divide by PC pitch to convert.
        pc_pitch = self.fin_tech.get_pitch(layer_name="PC")
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
        (<=1 DB), the secondary swing is <100, so reducing TLU by 1 always
        improves the objective - cap at (num_io_nets - 1). For large cells, cap
        at num_io_nets (natural bound).
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
        """Run the configured solve and record self.solve_status."""
        solve_result = self.solve(
            mode=self._cfg_get("solve_mode", "wsum"),
            objectives=self._build_solve_objectives(),
            exit_on_unsat=self._cfg_get("exit_on_unsat", True),
        )
        self.solve_status = self._interpret_solve_result(solve_result)

    def _build_solve_objectives(self):
        """
        Resolve the (factory, weight, sense) objective list for self.solve.

        Uses _DEFAULT_OBJECTIVE_CONFIG; honors per-objective
        cell_config["objective_weights"]["value"] (dict name -> int) overrides;
        weight == 0 drops the entry.
        """
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
        log_variable_info(self, filename=f"{self.output_dir}/result/{subckt}.var")
        res_path = f"{self.output_dir}/result/{subckt}.res"

        # FinFET builds three-deep s/g/d col maps ([net][zi][col]) for the
        # shared placement core, but write_finfet_result (like the CFET writer)
        # expects the two-deep [net][col] shape. Since FinFET is single-tier,
        # flatten the one zi level away before writing - a local, lossless
        # adaptation that touches nothing shared.
        flat_vars = self._flatten_tran_col_maps_for_writer()
        write_finfet_result(
            self.solver, self.circuit, flat_vars, self.edge_vars,
            self.net_arc_vars, self.fin_tech, self.cpp_cost,
            filename=res_path,
            lgg=self.lgg,
        )

        # Layer-by-layer placement visualization -> output/.../view/.
        view_dir = os.path.join(self.output_dir, "view")
        os.makedirs(view_dir, exist_ok=True)
        load_results, draw_layout_with_pin_and_routing = self._select_visualizer()
        placement, routing = load_results(res_path)
        draw_layout_with_pin_and_routing(
            placement, routing,
            filename=os.path.join(view_dir, f"{subckt}.png"),
        )

    def _select_visualizer(self):
        """
        Pick the FinFET visualizer by routing-track count.

        Both 3- and 4-track results use visualize_FinFET_4T: the visualizer is
        a data-driven debug plot (it reads coordinates straight from the .res),
        so the same plotter renders either track count. It exposes load_results
        (returns a 2-tuple (placement, routing), no tech arg) and
        draw_layout_with_pin_and_routing. Imported lazily so importing this
        module never requires matplotlib.
        """
        if self.fin_tech.num_rt_track in (3, 4):
            from src.cellgen.postprocess.visualize_FinFET_4T import (
                draw_layout_with_pin_and_routing,
                load_results,
            )
        else:
            raise NotImplementedError(
                f"No FinFET visualizer for num_rt_track={self.fin_tech.num_rt_track} "
                f"(supported: 3, 4)."
            )
        return load_results, draw_layout_with_pin_and_routing

    def _flatten_tran_col_maps_for_writer(self):
        """
        Return a shallow per-transistor view whose s/g/d col maps are flattened
        from the three-deep [net][zi][col] (shared-core shape) to the two-deep
        [net][col] shape that write_finfet_result expects.

        FinFET is single-tier, so each [net] has exactly one zi level whose cols
        merge unambiguously. The original TransistorVar objects are NOT mutated;
        we hand the writer lightweight proxies carrying the flattened maps plus
        the placement vars the writer reads (x/y/flip).
        """
        class _FlatTranView:
            __slots__ = ("x_var", "y_var", "flip_var",
                         "s_col_idx_var", "g_col_idx_var", "d_col_idx_var")

        def _flatten(per_net_three_deep):
            # per_net_three_deep: {net_name: {zi: {col: [bvs]}}} -> {net: {col: [bvs]}}
            flat = {}
            for net_name, per_tier in per_net_three_deep.items():
                merged = {}
                for _zi, per_col in per_tier.items():
                    for col, bvs in per_col.items():
                        merged.setdefault(col, []).extend(bvs)
                flat[net_name] = merged
            return flat

        out = {}
        for name, tvar in self.transistor_vars.items():
            view = _FlatTranView()
            view.x_var = tvar.x_var
            view.y_var = tvar.y_var
            view.flip_var = tvar.flip_var
            view.s_col_idx_var = _flatten(tvar.s_col_idx_var)
            view.g_col_idx_var = _flatten(tvar.g_col_idx_var)
            view.d_col_idx_var = _flatten(tvar.d_col_idx_var)
            out[name] = view
        return out

    # ------------------------------------------------------------------ #
    # solve dispatch                                                     #
    # ------------------------------------------------------------------ #

    def solve(self, mode="wsum", objectives=None, exit_on_unsat=True):
        """
        Dispatch the solve.

        Only single-solve (weighted-sum) is wired; mode=='pareto' raises
        NotImplementedError.
        """
        if mode == "wsum":
            return self.wsum(objectives=objectives, exit_on_unsat=exit_on_unsat)
        if mode == "pareto":
            raise NotImplementedError(
                "FinFET supports single-solve mode only"
            )
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
            # CP-SAT emits to BOTH stdout AND log_callback by default. Route only
            # through the callback so each line prints once (matches upstream).
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
            self.solver.parameters.cp_model_presolve = True
            self.solver.parameters.symmetry_level = 3
            # Adapt solver strategy based on number of diffusion breaks
            # (proxy for model complexity).
            if self.insert_num_db <= 1:
                self.solver.parameters.linearization_level = 1
                self.solver.parameters.cp_model_probing_level = 2
                self.solver.parameters.symmetry_detection_deterministic_time_limit = 5
                self.solver.parameters.num_search_workers = max(
                    self.solver.parameters.num_search_workers, 8
                )
                self.solver.parameters.ignore_subsolvers.extend([
                    "graph_arc_lns", "graph_cst_lns", "graph_dec_lns",
                    "graph_var_lns", "rnd_cst_lns", "rnd_var_lns", "max_lp_sym",
                ])
            else:
                self.solver.parameters.linearization_level = 0
                self.solver.parameters.cp_model_probing_level = 3
                self.solver.parameters.ignore_subsolvers.extend([
                    "quick_restart", "graph_arc_lns", "graph_cst_lns",
                    "graph_dec_lns", "graph_var_lns", "rnd_cst_lns",
                    "rnd_var_lns", "max_lp_sym", "default_lp",
                ])

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

    # ================================================================== #
    # subsystem initializers (FinFET single-tier; QFET-shaped containers) #
    # ================================================================== #

    def _init_tech(self):
        """Cache technology-derived state: finger counts + placement/pin-access rows."""
        logger.info("Initializing technology configuration...")
        # map transistor to number of fingers
        for tran in self.circuit.transistors.values():
            tran_width = tran.get_width()
            self.mos_to_num_finger[tran.name] = int(tran_width / self.fin_tech.unit_width)
        logger.info(f"\tNumber of fingers: {self.mos_to_num_finger}")

        # Row maps (SH only; non-SH already rejected by FinFET_Tech).
        #   SH 4T -> placeable NMOS [0] / PMOS [2]; pin-access NMOS [0,1] / PMOS [2,3]
        if self.fin_tech.height_config == "SH" and self.fin_tech.num_rt_track == 4:
            self.nmos_placeable_row_indices = [0]
            self.pmos_placeable_row_indices = [2]
            self.nmos_pin_access_ri = [0, 1]
            self.pmos_pin_access_ri = [2, 3]
        elif self.fin_tech.height_config == "SH" and self.fin_tech.num_rt_track == 3:
            self.nmos_placeable_row_indices = [0]
            self.pmos_placeable_row_indices = [2]
            self.nmos_pin_access_ri = [0]
            self.pmos_pin_access_ri = [2]
        else:
            # FinFET_Tech only admits SH with num_rt_track in {3, 4}; anything
            # else means the row layout is undefined for this technology.
            raise NotImplementedError(
                f"FinFET row layout undefined for height_config="
                f"{self.fin_tech.height_config!r}, num_rt_track="
                f"{self.fin_tech.num_rt_track}."
            )
        logger.info(
            f"\tNMOS placeable rows: {self.nmos_placeable_row_indices}, "
            f"PMOS placeable rows: {self.pmos_placeable_row_indices}"
        )
        logger.info(
            f"\tNMOS pin accessible rows: {self.nmos_pin_access_ri}, "
            f"PMOS pin accessible rows: {self.pmos_pin_access_ri}"
        )

    def _init_graph(self):
        """Build self.lgg (LayeredGridGraph) from the technology layer stack."""
        logger.info("Initializing graph configuration...")
        # NOTE: for all other layers we double the pitch because the PC layer is
        # expected to have float-point values for SD columns; later we divide by
        # 2 to recover the actual column values. num_col reflects S/D/G columns
        # so the col grid is not doubled.
        self.canvas_width = self.num_col * self.fin_tech.layer_stack.metal_layers[0].pitch
        self.canvas_height = self.fin_tech.num_rt_track * self.fin_tech.layer_stack.metal_layers[1].pitch * 2
        logger.info(f"\tCanvas width: {self.canvas_width}, Canvas height: {self.canvas_height}")

        idx_to_layer = {}
        layer_to_direction = {}
        layer_to_cols = {}
        layer_to_rows = {}
        for li, layer in enumerate(self.fin_tech.layer_stack.metal_layers):
            idx_to_layer[li] = layer.layer_name
        # columns on each layer
        for layer in self.fin_tech.layer_stack.metal_layers:
            layer_to_direction[layer.layer_name] = layer.direction
            if layer.direction == "H":
                continue
            tmp_pitch = layer.pitch * 2 if layer.layer_name != "PC" else layer.pitch
            tmp_offset = layer.offset * 2
            num_cols = int(math.ceil((self.canvas_width - tmp_offset) / tmp_pitch))
            layer_to_cols[layer.layer_name] = [tmp_offset + i * tmp_pitch for i in range(num_cols)]
        logger.info(f"\tLayer to cols: {layer_to_cols}")
        # rows on each layer
        for layer in self.fin_tech.layer_stack.metal_layers:
            if layer.direction == "V":
                continue
            tmp_pitch = layer.pitch * 2 if layer.layer_name != "PC" else layer.pitch
            tmp_offset = layer.offset * 2
            num_rows = int(math.ceil((self.canvas_height - tmp_offset) / tmp_pitch))
            layer_to_rows[layer.layer_name] = [tmp_offset + i * tmp_pitch for i in range(num_rows)]

        # FinFET is single-tier planar: NO virtual jump pairs (contrast CFET's
        # BPC<->M0 boundary jump). Pass layer_to_kind so the shared LGG marks the
        # PC layer PLACE and the rest ROUTE (consumed by the shared core's
        # placement-layer skip sets).
        self.lgg = LayeredGridGraph(
            layer_to_rows=layer_to_rows,
            layer_to_cols=layer_to_cols,
            idx_to_layer=idx_to_layer,
            layer_to_direction=layer_to_direction,
            layer_to_kind=self.fin_tech.layer_to_kind,
        )
        self.lgg.stats()

    def _init_domain(self):
        """
        Build CP-SAT placement domains over the single PC placement-tier col/row
        sets, plus the single-tier z (tier) domain.

        Convention (matches LayeredGridGraph.is_even_col):
            even-parity col index -> gate column
            odd-parity  col index -> source/drain column
        """
        logger.debug("Initializing variable domain...")
        placement_layer = self.fin_tech.default_placement_layer

        # MOS-placement positions: odd-parity (S/D) cols, last col excluded as boundary
        self.plc_ci = self.lgg.col_indices_in_layer(placement_layer, parity="odd")[:-1]
        self.domain_mos_placable_ci = self._make_domain(self.plc_ci, "MOS placeable col indices")

        # MOS-placement rows: even-parity row indices on the placement tier
        self.plc_ri = self.lgg.row_indices_in_layer(placement_layer, parity="even")
        self.domain_mos_placable_ri = self._make_domain(self.plc_ri, "MOS placeable row indices")

        # source/drain col indices (odd parity)
        self.sd_ci = self.lgg.col_indices_in_layer(placement_layer, parity="odd")
        self.domain_sd_ci = self._make_domain(self.sd_ci, "S/D col indices")

        # gate col indices (even parity)
        self.g_ci = self.lgg.col_indices_in_layer(placement_layer, parity="even")
        self.domain_g_ci = self._make_domain(self.g_ci, "gate col indices")

        # all placement-tier col / row indices
        self.pc_ci = self.lgg.col_indices_in_layer(placement_layer)
        self.domain_pc_ci = self._make_domain(self.pc_ci, "all placement col indices")
        self.pc_ri = self.lgg.row_indices_in_layer(placement_layer)
        self.domain_pc_ri = self._make_domain(self.pc_ri, "all placement row indices")

        # MOS-placement z (tier) indices: FinFET is single-tier, so plc_zi has
        # exactly ONE entry - the LGG layer index of the PC placement layer.
        # The shared per-tier placement/routing loops degrade to one iteration.
        self.plc_zi = sorted(
            self.lgg.layer_to_idx[name] for name in self.fin_tech.placement_layer_names
        )

    def _make_domain(self, values, label):
        """Wrap a list of values in cp_model.Domain.FromValues, log, return."""
        domain = cp_model.Domain.FromValues(values)
        logger.info(f"Domain {label}: {domain}")
        return domain

    def _init_var(self):
        """
        Populate every CP-SAT variable container (QFET-shaped, single PC tier).

        Order matters - net-arc/edge depend on node adjacency; SON_vars depends
        on SON_positions; diffusion breaks depend on the per-slot reifiers built
        by _init_transistor_vars.
        """
        # transistor placement + cpp + cell boundaries
        self._init_transistor_vars()
        self._init_cpp()
        self._init_cell_boundaries()

        # diffusion breaks (per-slot canonical + per-col aggregate)
        self._init_diffusion_break_vars()

        # net source / terminal Super Inner Nodes (internal pins)
        self._init_src_super_inner_nodes_vars()
        self._init_term_super_inner_nodes_vars()

        # node adjacency cache (consumed downstream by flow/arc constraints)
        self.adj_in = {node: [] for node in self.lgg.nodes()}
        self.adj_out = {node: [] for node in self.lgg.nodes()}
        for u_arc, v_arc in self.lgg.arcs():
            self.adj_out[u_arc].append((u_arc, v_arc))
            self.adj_in[v_arc].append((u_arc, v_arc))

        # net-level variables
        self._init_net_flow_vars()
        self._init_net_arc_vars()
        self._init_edge_vars()
        # normalize the edge cost to order (reduce the size of the domain)
        self.all_possible_edge_cost = sorted(self.edge_to_cost.values())

        # Super Outer Nodes for I/O pins (positions, then per-net SON bindings)
        self._init_SON_positions()
        self._init_SON_vars()

        logger.info("\tEnd of variable initialization ...")

    def _init_transistor_vars(self):
        """
        Create per-transistor placement vars (x, y, flip) and the per-(col, tier)
        bool reifiers the shared placement/routing core consumes.

        FinFET is single-tier planar: there is NO z_var (left None) and y_var is
        PINNED per model. Distinctness runs over x only (one PC tier). The
        per-slot containers (placed_tran_at_xzi_vars, placed_tran_zi_vars,
        has_tran_at_*_vars) are still built - over the single zi - because the
        shared core reads them by (ci, zi).
        """
        self.opt.log_comment("Transistor variables")

        tmp_pmos_x_var = []
        tmp_nmos_x_var = []

        for tran in self.circuit.transistors.values():
            tvar = TransistorVar(tran.name)
            self.transistor_vars[tran.name] = tvar

            tvar.x_var = self.opt.NewIntVarFromDomain(self.domain_mos_placable_ci, f"{tran.name}_x")
            tvar.y_var = self.opt.NewIntVarFromDomain(self.domain_mos_placable_ri, f"{tran.name}_y")
            # z_var stays None - FinFET is single-tier (no z dimension).
            tvar.z_var = None
            tvar.flip_var = self.opt.NewBoolVar(f"{tran.name}_flip")

            # SH mode pins y by transistor type; distinctness then runs over x.
            if self.fin_tech.height_config == "SH":
                if tran.model == Model.PMOS:
                    self.opt.Add(tvar.y_var == self.pmos_placeable_row_indices[0])
                    tmp_pmos_x_var.append(tvar.x_var)
                elif tran.model == Model.NMOS:
                    self.opt.Add(tvar.y_var == self.nmos_placeable_row_indices[0])
                    tmp_nmos_x_var.append(tvar.x_var)

        # Speedup: skip symmetry-breaking ordering when placements are fully
        # injected (injected positions may violate the non-deterministic
        # set-iteration order used by the symmetry-breaking constraints).
        # FinFET calls both accelerate helpers; with uses_tier_placement=False
        # + z_var=None they emit the unconditional form.
        if not self.cell_config["inject_placement"]["value"]:
            accelerate._fix_placement_order_identical_transistors_(
                self, fix_placement_across_pn=self.fix_placement_across_pn,
            )
            if self.use_low_degree_net:
                accelerate._tighten_placement_for_low_degree_net_(self)

        # Each transistor of a model must occupy a distinct column (single tier).
        if self.fin_tech.height_config == "SH":
            self.opt.AddAllDifferent(tmp_pmos_x_var)
            self.opt.AddAllDifferent(tmp_nmos_x_var)

        # Per-(col) and per-(tier) placement reifiers.
        for tran in self.circuit.transistors.values():
            tvar = self.transistor_vars[tran.name]
            for ci in self.plc_ci:
                placed = self.opt.NewBoolVar(f"tran_placed_col_{tran.name}_{ci}")
                self.placed_tran_ci_vars[(tran.name, ci)] = placed
                self.opt.Add(tvar.x_var == ci).OnlyEnforceIf(placed)
                self.opt.Add(tvar.x_var != ci).OnlyEnforceIf(placed.Not())
            for zi in self.plc_zi:
                # Single-tier: the transistor is trivially on the one PC tier, so
                # placed_tran_zi == True always. Reify it against a fixed-true
                # constant so the per-slot AND below is well-defined.
                placed = self.opt.NewBoolVar(f"tran_placed_tier_{tran.name}_{zi}")
                self.placed_tran_zi_vars[(tran.name, zi)] = placed
                self.opt.Add(placed == 1)

        # Per-slot AND-reifier: placed_at_xzi[(tran, ci, zi)] iff placed at (ci, zi).
        # Consumed by _init_diffusion_break_vars + the shared placement constraints.
        for tran in self.circuit.transistors.values():
            for ci in self.plc_ci:
                placed_ci = self.placed_tran_ci_vars[(tran.name, ci)]
                for zi in self.plc_zi:
                    placed_zi = self.placed_tran_zi_vars[(tran.name, zi)]
                    is_at = self.opt.NewBoolVar(f"{tran.name}_at_ci{ci}_zi{zi}")
                    self.opt.AddBoolAnd([placed_ci, placed_zi]).OnlyEnforceIf(is_at)
                    self.opt.AddBoolOr([placed_ci.Not(), placed_zi.Not()]).OnlyEnforceIf(is_at.Not())
                    self.placed_tran_at_xzi_vars[(tran.name, ci, zi)] = is_at

        # Aggregate: has_tran_at_{ci,zi}[idx] == OR over all transistors.
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
        # Per-slot aggregate - used by the per-tier routing helpers (gate / lisd
        # sharing) to gate the "no transistor at this slot" branch.
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

    def _init_cpp(self):
        """
        cpp_cost = max(transistor x_vars) (col-index units), plus an SH lower
        bound + warm-start hints.
        """
        self.opt.log_comment("Enforcing total cpp...")
        self.cpp_cost = self.opt.NewIntVarFromDomain(self.domain_sd_ci, "cpp_cost")
        self.opt.AddMaxEquality(
            self.cpp_cost,
            [self.transistor_vars[tran.name].x_var for tran in self.circuit.transistors.values()],
        )

        if self.fin_tech.height_config != "SH":
            return
        num_pmos = self.circuit.num_pmos_transistors()
        num_nmos = self.circuit.num_nmos_transistors()
        min_transistors_per_row = max(num_pmos, num_nmos)
        if min_transistors_per_row <= 0:
            return

        sorted_plc_ci = sorted(self.plc_ci)
        if min_transistors_per_row <= len(sorted_plc_ci):
            min_cpp_col = sorted_plc_ci[min_transistors_per_row - 1]
        else:
            stride = sorted_plc_ci[1] - sorted_plc_ci[0] if len(sorted_plc_ci) > 1 else 2
            min_cpp_col = sorted_plc_ci[-1] + stride * (min_transistors_per_row - len(sorted_plc_ci))
        self.opt.Add(self.cpp_cost >= min_cpp_col)
        logger.info(
            f"\t==\tCPP lower bound: cpp_cost >= {min_cpp_col} "
            f"({min_transistors_per_row} transistors per row)"
        )

        # Warm-start hints: pack each model's transistors at the lowest columns.
        pmos_trans = [t for t in self.circuit.transistors.values() if t.model == Model.PMOS]
        nmos_trans = [t for t in self.circuit.transistors.values() if t.model == Model.NMOS]
        for i, tran in enumerate(pmos_trans):
            if i < len(sorted_plc_ci):
                self.opt.AddHint(self.transistor_vars[tran.name].x_var, sorted_plc_ci[i])
        for i, tran in enumerate(nmos_trans):
            if i < len(sorted_plc_ci):
                self.opt.AddHint(self.transistor_vars[tran.name].x_var, sorted_plc_ci[i])
        self.opt.AddHint(self.cpp_cost, min_cpp_col)

    def _init_cell_boundaries(self):
        """Cache physical-coord right-edge of the cell, accounting for DB allowance."""
        pc_pitch = self.fin_tech.get_pitch("PC")
        self.max_boundary_col = self.lgg.max_col_in_layer("PC")
        self.min_boundary_col = self.max_boundary_col - self.insert_num_db * 2 * pc_pitch
        logger.info(
            f"\tCell boundary cols: min={self.min_boundary_col}, max={self.max_boundary_col}"
        )

    def _init_diffusion_break_vars(self):
        """
        Create per-slot diffusion-break vars + the legacy per-col aggregates
        (QFET-shaped, single PC tier).

        Per-slot (z-aware canonical, consumed by the shared placement /
        objective core):
            db_pmos_vars[(ci, zi)] <=> no PMOS transistor at slot (ci, zi)
            db_nmos_vars[(ci, zi)] <=> no NMOS transistor at slot (ci, zi)

        Per-col aggregate (back-compat):
            db_pmos_cols_vars[ci]  <=> all tiers at col ci are PMOS DBs
            db_nmos_cols_vars[ci]  <=> same for NMOS
        """
        self.opt.log_comment("Diffusion break variables (per slot, single tier)")

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

                # DB at slot <=> no transistor of that model at slot
                self.opt.Add(sum(pmos_at_slot) == 0).OnlyEnforceIf(pdb_var)
                self.opt.Add(sum(pmos_at_slot) >= 1).OnlyEnforceIf(pdb_var.Not())
                self.opt.Add(sum(nmos_at_slot) == 0).OnlyEnforceIf(ndb_var)
                self.opt.Add(sum(nmos_at_slot) >= 1).OnlyEnforceIf(ndb_var.Not())

        # Per-col aggregates (back-compat). "All tiers at col are DBs" via AND.
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
        Create BoolVars for each net's source-pin candidate positions on the
        single PC placement tier.

        Output: node_is_src_vars[net.name][(layer_idx, row, col)] = bv
        Also registers each bv three-deep on the transistor's s/g/d_col_idx_var
        ([net.name][zi][col]) - the shape the shared placement core reads.
        _maybe_write_results flattens the zi level for write_finfet_result.
        """
        self.opt.log_comment("Super Inner Nodes for src pins")
        for net in self.circuit.get_nets(with_power_ground=False):
            self.node_is_src_vars[net.name] = {}
            src_tran_name, src_pin = net.source()
            self._populate_pin_candidates(
                net=net,
                tran=self.circuit.transistors[src_tran_name],
                tvar=self.transistor_vars[src_tran_name],
                pin_role=src_pin,
                target_dict=self.node_is_src_vars[net.name],
                var_prefix=f"net_issrc_{net.name}",
            )

    def _init_term_super_inner_nodes_vars(self):
        """
        Create BoolVars for each net's terminal-pin candidate positions per
        terminal k on the single PC placement tier.

        Output: node_is_term_vars[net.name][k][(layer_idx, row, col)] = bv
        """
        self.opt.log_comment("Super Inner Nodes for terminal pins")
        for net in self.circuit.get_nets(with_power_ground=False):
            self.node_is_term_vars[net.name] = {}
            for k, (term_tran_name, term_pin) in enumerate(net.terminals()):
                self.node_is_term_vars[net.name][k] = {}
                self._populate_pin_candidates(
                    net=net,
                    tran=self.circuit.transistors[term_tran_name],
                    tvar=self.transistor_vars[term_tran_name],
                    pin_role=term_pin,
                    target_dict=self.node_is_term_vars[net.name][k],
                    var_prefix=f"net_isterm_{net.name}_{k}",
                )

    # Pin-role -> (LGG parity-check method name, TransistorVar attribute name).
    # Even col = gate, odd col = source/drain (see LayeredGridGraph.is_even_col).
    _PIN_ROLE_CONFIG = {
        "source": ("is_odd_col",  "s_col_idx_var"),
        "gate":   ("is_even_col", "g_col_idx_var"),
        "drain":  ("is_odd_col",  "d_col_idx_var"),
    }

    def _populate_pin_candidates(self, *, net, tran, tvar, pin_role, target_dict, var_prefix):
        """
        Create one BoolVar per (PC tier x pin-access row x candidate col) where
        the col matches the pin role's parity.

        FinFET has a single placement tier (PC), but each created bool is still
        registered three-deep as `tvar.{s|g|d}_col_idx_var[net.name][zi][col]`
        (zi = LGG index of the PC layer) so the shared placement constraints
        (which do `.get(net, {}).get(zi, {})`) can read them. The
        single-tier flatten back to [net][col] for the writer happens at
        write-time only.
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

        # Single placement tier (PC).
        for layer_name in self.fin_tech.placement_layer_names:
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

    def _init_net_flow_vars(self):
        """
        Create per-net flow variables on every LGG arc (int-flow for K >=
        _INT_FLOW_THRESHOLD internal nets, else per-commodity bool flow).
        """
        self.opt.log_comment("Net flow variables")
        arcs = list(self.lgg.arcs())
        for net in self.circuit.get_nets(with_power_ground=False):
            num_extra_flow = 1 if net.is_io_net() else 0
            self.num_pins_for_io += num_extra_flow
            total_k = net.num_terminals() + num_extra_flow

            use_int_flow = total_k >= self._INT_FLOW_THRESHOLD and num_extra_flow == 0
            dbg = self._INT_FLOW_DEBUG_NET
            if dbg is not None:
                if isinstance(dbg, str):
                    use_int_flow = (net.name == dbg and num_extra_flow == 0)
                else:
                    use_int_flow = (net.name in dbg and num_extra_flow == 0)

            if use_int_flow:
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

        FinFET cost model:
            via (cross-layer) -> 5;  wire (same layer) -> 1.
        """
        self.opt.log_comment("Edge variables")
        for u_edge, v_edge in self.lgg.edges():
            self.edge_vars[(u_edge, v_edge)] = self.opt.NewBoolVar(f"edge_{u_edge}_{v_edge}")
            if u_edge[0] != v_edge[0]:
                self.edge_to_cost[(u_edge, v_edge)] = 5
            else:
                self.edge_to_cost[(u_edge, v_edge)] = 1

    def _init_SON_positions(self):
        """
        Identify Super Outer Node positions for I/O pin placement on M1.

        FinFET accesses external pins on the vertical M1 layer (M0 pins are
        handled separately by pin.m0_pin). The SON rows on M1 come from the
        per-track _ROW_MAP via _get_son_row_indices.
        """
        tmp_son_row_indices = self._get_son_row_indices()
        # M0 / M2 kept as empty buckets.
        self.son_terminal_nodes["M0"] = []
        self.son_terminal_nodes["M1"] = []
        for node in self.lgg.nodes_in_layer("M1"):
            for ri in tmp_son_row_indices:
                row = self.lgg.row_in_layer("M1", ri)
                if node[1] == row:
                    self.son_terminal_nodes["M1"].append(node)
        self.son_terminal_nodes["M2"] = []

    def _init_SON_vars(self):
        """
        Create BoolVars for Super Outer Node (I/O pin) placement on M1.

        For each I/O net and each extra-flow commodity k, a candidate bool is
        created for every M1 SON node. Exactly one SON is chosen per (net, k).
        """
        self.opt.log_comment("Super Outer Nodes for I/O pins")
        for net in self.circuit.get_nets(with_power_ground=False):
            if not net.is_io_net():
                continue
            self.node_is_SON_vars[net.name] = {}
            for k in range(net.num_terminals(), self.net_to_flow_cnt[net.name]):
                slot = {}
                for node in self.son_terminal_nodes["M1"]:
                    layer_idx, row, col = node
                    bv = self.opt.NewBoolVar(
                        f"net_isSON_{net.name}_{k}_L{layer_idx}_R{row}_C{col}"
                    )
                    slot[(layer_idx, row, col)] = bv
                    self.node_to_net_SON_vars.setdefault(
                        (layer_idx, row, col), {}
                    ).setdefault(net.name, []).append(bv)
                self.node_is_SON_vars[net.name][k] = slot
                # Exactly one SON node per (net, k).
                self.opt.Add(sum(slot.values()) == 1)

    def _get_son_row_indices(self):
        """SON rows on M1 per routing-track count."""
        if self.fin_tech.height_config != "SH":
            return None
        _ROW_MAP = {
            2: [0, 1],
            3: [0, 2],
            4: [0, 3],
            5: [1, 3],
            6: [2, 3],
        }
        try:
            return _ROW_MAP[self.fin_tech.num_rt_track]
        except KeyError:
            raise ValueError(f"Unsupported number of rows: {self.fin_tech.num_rt_track}")

    def _build_region_caches(self):
        """
        Pre-compute per-column node and via-edge lookups for PMOS/NMOS regions
        on the single PC tier (consumed by the gather_* helpers below).
        """
        pc_idx = self.lgg.layer_to_idx["PC"]
        pmos_rows = set(self.lgg.row_in_layer("PC", ri) for ri in self.pmos_pin_access_ri)
        nmos_rows = set(self.lgg.row_in_layer("PC", ri) for ri in self.nmos_pin_access_ri)

        # nodes
        self._pmos_nodes_by_col = {}
        self._nmos_nodes_by_col = {}
        self._pmos_nodes_all = []
        self._nmos_nodes_all = []
        for node in self.lgg.nodes_in_layer("PC"):
            if node[1] in pmos_rows:
                self._pmos_nodes_by_col.setdefault(node[2], []).append(node)
                self._pmos_nodes_all.append(node)
            if node[1] in nmos_rows:
                self._nmos_nodes_by_col.setdefault(node[2], []).append(node)
                self._nmos_nodes_all.append(node)

        # via edges (PC cross-layer)
        self._pmos_via_by_col = {}
        self._nmos_via_by_col = {}
        self._pmos_via_all = []
        self._nmos_via_all = []
        for u, v in self.lgg.edges():
            if u[0] == pc_idx and u[0] != v[0]:
                if u[1] in pmos_rows and v[1] in pmos_rows:
                    evar = self.edge_vars[(u, v)]
                    self._pmos_via_by_col.setdefault(u[2], []).append(evar)
                    self._pmos_via_all.append(evar)
                if u[1] in nmos_rows and v[1] in nmos_rows:
                    evar = self.edge_vars[(u, v)]
                    self._nmos_via_by_col.setdefault(u[2], []).append(evar)
                    self._nmos_via_all.append(evar)

    # ----- region-gather helpers (consumed by the shared placement/routing    #
    #       core via the self-as-context contract) ----------------------------#

    def _pin_access_ri_for(self, model):
        """Return the pin-access row-index list for the given transistor model."""
        if model == Model.PMOS:
            return self.pmos_pin_access_ri
        if model == Model.NMOS:
            return self.nmos_pin_access_ri
        raise ValueError(f"Unknown model: {model}")

    def _gather_region_nodes(self, zi, col, model):
        """
        Return LGG nodes (zi, row, col) on placement-tier `zi` at column `col`
        matching `model`'s pin-access rows. Symmetric with
        _populate_pin_candidates - the shared placement core uses it to identify
        the nodes a transistor's S/D/G occupies when placed at (col, zi).
        """
        layer_name = self.lgg.idx_to_layer[zi]
        return [
            (zi, self.lgg.row_in_layer(layer_name, ri), col)
            for ri in self._pin_access_ri_for(model)
        ]

    def gather_via_vars_in_pmos_region(self, col=None):
        if col is not None:
            return list(self._pmos_via_by_col.get(col, []))
        return list(self._pmos_via_all)

    def gather_via_vars_in_nmos_region(self, col=None):
        if col is not None:
            return list(self._nmos_via_by_col.get(col, []))
        return list(self._nmos_via_all)

    def gather_nodes_in_pmos_region(self, col=None):
        if col is not None:
            return list(self._pmos_nodes_by_col.get(col, []))
        return list(self._pmos_nodes_all)

    def gather_nodes_in_nmos_region(self, col=None):
        if col is not None:
            return list(self._nmos_nodes_by_col.get(col, []))
        return list(self._nmos_nodes_all)

    def gather_src_term_vars_in_pmos_region(self, col):
        zi = next(iter(self.plc_zi))  # single placement tier (planar FinFET)
        return self._gather_src_term_vars_in_region(zi, col, Model.PMOS)

    def gather_src_term_vars_in_nmos_region(self, col):
        zi = next(iter(self.plc_zi))  # single placement tier (planar FinFET)
        return self._gather_src_term_vars_in_region(zi, col, Model.NMOS)

    def _gather_via_vars_in_region(self, zi, col, model):
        """
        Return edge_vars for vias (cross-layer edges) landing at (zi, col) on
        `model`'s pin-access rows.

        Implements the shared placement-core contract (mirrors
        QFET._gather_via_vars_in_region): the shared placement constraints
        ban_other_nets_on_pwr_columns and prohibit_CA_contact_on_non_source_term_columns
        call instance._gather_via_vars_in_region(zi, col, model). For each
        pin-access node at (zi, row, col), candidate via neighbours are
        (zi+-1, row, col); collect the edge_var when such an edge exists.
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
                # nx.Graph edges are undirected; key can be in either order.
                for key in ((anchor, neighbor), (neighbor, anchor)):
                    if key in self.edge_vars:
                        via_vars.append(self.edge_vars[key])
                        break
        return via_vars

    def _gather_src_term_vars_in_region(self, zi, col, model):
        """
        Return all source + terminal bool vars at (zi, col) on `model`'s
        pin-access rows, across all non-power-ground nets.

        Implements the shared placement-core contract (mirrors
        QFET._gather_src_term_vars_in_region): used by
        prohibit_CA_contact_on_non_source_term_columns to require at least one
        src/term occupant at any col where a via lands. FinFET is single-tier,
        so `zi` is the lone placement tier but is honoured for contract parity.
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

    def _gather_via_arcs(self, net_name, layer_1, layer_2):
        """Gather both directions of net arcs that cross between two layers."""
        tmp_via_arcs = []
        l1 = self.lgg.layer_to_idx[layer_1]
        l2 = self.lgg.layer_to_idx[layer_2]
        for u, v in self.lgg.arcs():
            if u[0] == l1 and v[0] == l2:
                tmp_via_arcs.append(self.net_arc_vars[(net_name, u, v)])
            if u[0] == l2 and v[0] == l1:
                tmp_via_arcs.append(self.net_arc_vars[(net_name, u, v)])
        return tmp_via_arcs

    def _is_1_to_1_gr(self):
        """True iff M1 and PC share pitch and M1 offset is 0 (1-to-1 grid)."""
        return (
            self.fin_tech.get_pitch("M1") == self.fin_tech.get_pitch("PC")
            and self.fin_tech.get_offset("M1") == 0
        )

    def extract_windows_horizontal_bidirectional(self, u, X):
        """
        Slide horizontal windows of length X over the track of node `u`,
        collecting the directed arcs whose endpoints are reachable from `u` and
        lie within each window (consumed by pin.m0_pin_extension / DRC window
        helpers).
        """
        layer_u, row_u, col_u = u
        same_track = [
            (a, b) for (a, b) in self.lgg.arcs()
            if a[0] == layer_u and b[0] == layer_u and a[1] == row_u and b[1] == row_u
        ]
        adj = {}
        for a, b in same_track:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
        reachable = {u}
        queue = deque([u])
        while queue:
            node = queue.popleft()
            for nbr in adj.get(node, ()):
                if nbr not in reachable:
                    reachable.add(nbr)
                    queue.append(nbr)
        col_positions = sorted({n[2] for n in reachable})
        windows = []
        for s in col_positions:
            e = s + X
            if not (s <= col_u <= e):
                continue
            in_window = [
                (a, b) for (a, b) in same_track
                if a in reachable and b in reachable and s <= a[2] <= e and s <= b[2] <= e
            ]
            if in_window:
                windows.append({"start": s, "end": e, "arcs": in_window})
        return windows

    def extract_windows_vertical_bidirectional(self, u, Y):
        """Slide vertical windows of length Y over the column of node `u`."""
        layer_u, row_u, col_u = u
        same_track = [
            (a, b) for (a, b) in self.lgg.arcs()
            if a[0] == layer_u and b[0] == layer_u and a[2] == col_u and b[2] == col_u
        ]
        adj = {}
        for a, b in same_track:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
        reachable = {u}
        queue = deque([u])
        while queue:
            node = queue.popleft()
            for nbr in adj.get(node, ()):
                if nbr not in reachable:
                    reachable.add(nbr)
                    queue.append(nbr)
        row_positions = sorted({n[1] for n in reachable})
        windows = []
        for s in row_positions:
            e = s + Y
            if not (s <= row_u <= e):
                continue
            in_window = [
                (a, b) for (a, b) in same_track
                if a in reachable and b in reachable and s <= a[1] <= e and s <= b[1] <= e
            ]
            if in_window:
                windows.append({"start": s, "end": e, "arcs": in_window})
        return windows

    # ================================================================== #
    # constraints                                                        #
    # ================================================================== #

    def _placement_constraints(self):
        """
        Build placement constraints by calling the shared generic per-tier core
        (plc.*).
        """
        # Linking source/drain/gate columns to transistor placement.
        plc.link_source_drain_gate_columns_to_transistor_placement(self)
        # Ban other nets from using power columns.
        plc.ban_other_nets_on_pwr_columns(self)
        # No CA contact allowed unless the column resides a source or term.
        plc.prohibit_CA_contact_on_non_source_term_columns(self)
        # If a diffusion break is used in PMOS, NMOS must use it at the same col.
        plc.diffusion_alignment(self)
        # Reduce the number of diffusion breaks by setting allowable DB columns.
        self.opt.log_comment("Setting allowable diffusion break columns...")
        logger.info(
            f"\t==\tSetting allowable diffusion break columns to "
            f"{self.fin_tech.allowable_diffusion_break_cols}..."
        )
        plc.limit_diffusion_breaks(self)
        # Lexicographic Order Symmetry Breaking (only when enabled).
        if self.use_break_symmetry:
            plc.placement_lexico_order_symmetry_breaking(self)
        # Pairwise diffusion / lisd / gate sharing.
        plc.pairwise_diffusion_sharing(self)
        plc.pairwise_lisd_sharing(self)
        plc.pairwise_gate_sharing(self)

    def _routing_constraints(self):
        """
        Top-level routing constraint orchestration, calling the shared generic
        per-tier rt.* / rule.* / pin.* free functions.

        Single-tier notes:
          - rt.routing_localization is the generic per-tier function (NOT the
            cfet variant). It auto-derives one tier from placement_layer_names.
          - rt.ban_middle_row_via_for_3T is called ONLY for the 3-track config
            (it also self-gates, but we honor the 3T-only call site explicitly).
          - self.enable_reverse_flow_link is False (set in __init__), so
            rt.link_flow_to_arc omits the reverse arc->flow link (FinFET MAR/EOL
            active -> reverse link could make the model INFEASIBLE).
        """
        # ----- boundary protection -----------------------------------------
        logger.info("Prohibiting routing to left/right cell boundaries ...")
        rt.prohibit_routing_to_left_cell_boundaries(self)
        rt.prohibit_routing_to_right_cell_boundaries(self)

        # ----- gate sharing / gate-cut window / DB protection / CA pickup ---
        rt.bind_gate_sharing_to_columns(self, db_as_gs=True)

        self.min_gate_cut_len = self.cell_config["minimum_gate_cut_length"]["value"]
        rt.gate_cut_window(self)
        rt.prohibit_pc_routing_in_diffusion_break_cols(self)
        rt.enforce_CA_pickup_for_gate_cut(self)

        # ----- gate-contact cap (LIG routing OFF -> tighten) ----------------
        if not self.cell_config["lig_routing"]["value"]:
            rt.limit_gate_contact(self, num_contact=1)

        # ----- LISD sharing + contact cap (LISD routing OFF -> tighten) -----
        rt.bind_lisd_sharing_to_columns(self)
        if not self.cell_config["lisd_routing"]["value"]:
            rt.limit_lisd_contact(self, num_contact=1)

        # ----- [3T only] middle-row PC<->M0 via restriction -----------------
        if self.fin_tech.num_rt_track == 3:
            rt.ban_middle_row_via_for_3T(self)

        # ----- routing-window localization ----------------------------------
        self.opt.log_comment("Routing Window Constraint ...")
        # Generic routing_localization reads the QFET-style tolerance attrs.
        # Derive them from the FinFET config's scalar routing_tolerance (in CPP).
        if self.cell_config["routing_tolerance"]["value"]:
            tol = int(self.cell_config["routing_tolerance"]["tol"] * self.fin_tech.get_pitch("PC"))
        else:
            tol = -1
        # Keep the scalar (consumed by use_routing_window_strategy) and expose
        # the X/Y tolerance attrs the generic localization reads.
        self.routing_tolerance = tol
        self.routing_tolerance_x = tol
        self.routing_tolerance_y = tol
        self.routing_tolerance_per_fanout = 0
        rt.routing_localization(self)

        # ----- flow <-> arc <-> edge linking + net/SON uniqueness -----------
        self.opt.log_comment("Linking flow variables to arc usage ...")
        rt.link_flow_to_arc(self)
        rt.link_arc_to_edge(self)
        rt.net_has_one_src_and_k_terminals(self)
        rt.net_src_node_uniqueness(self)
        rt.net_term_node_uniqueness(self)
        rt.net_SON_node_uniqueness(self)
        rt.prohibit_multiple_SONs_same_column(self)

        # ----- routing flow induction (internal + external) -----------------
        rt.induce_internal_routing_flow_with_diffusion(self)
        rt.induce_external_routing_flow(self)
        # Tree enforcement only for small cells.
        if self.insert_num_db <= 1:
            rt.tree_enforcement(self)
        rt.node_exclusivity(self)

        # ----- design rules: geometric / EOL / MAR --------------------------
        self.opt.log_comment("Adding geometric variables...")
        rule.geometric_vars_in_horizontal_layers(self)
        rule.geometric_vars_in_vertical_layers(self)

        eol_params = self.cell_config["eol_c2c_rule"]["value"]
        rule.eol_rules_in_horizontal_layers(self, eol_params)
        rule.eol_rules_in_vertical_layers(self, eol_params)

        mar_params = self.cell_config["mar_c2c_rule"]["value"]
        supervia_params = {layer: False for layer in self.lgg.layer_to_idx.keys()}
        for layer in self.cell_config["supervia"]["value"]:
            if layer in supervia_params:
                supervia_params[layer] = True
        rule.mar_rules_in_horizontal_layers(self, mar_params, supervia_params)
        rule.mar_rules_in_vertical_layers(self, mar_params, supervia_params)

        # ----- via-to-metal connection rule ---------------------------------
        rule.via_induce_vertical_metal(self, supervia_params)
        rule.via_induce_horizontal_metal(self, supervia_params)
        rule.vertical_metal_must_be_connected_to_via(self)
        rule.horizontal_metal_must_be_connected_to_via(self)
        # FinFET-only DRC: every M2 metal endpoint must terminate on a via (bans
        # floating M2 stubs; SON I/O nodes are exempt). Gated OFF by default;
        # enable for production DRC cleanliness via
        # cell_config["enforce_metal_endpoint_via"] = True (or set
        # self.enforce_metal_endpoint_via). Feasibility-safe on INV/NAND2 when
        # enabled.
        if getattr(self, "enforce_metal_endpoint_via",
                   self._cfg_get("enforce_metal_endpoint_via", False)):
            rule.metal_endpoint_must_have_via(self, target_layers=frozenset({"M2"}))

        # ----- via separation rule ------------------------------------------
        via_params = {
            tuple(k.strip() for k in key.split(",")): value
            for key, value in self.cell_config["via_c2c_rule"]["value"].items()
        }
        rule.via_separation_rules(self, via_params)

        # ----- per-layer usage (diagnostic objectives feed off these) -------
        self._m2_layer_usage(m2_layer="M2")
        self._m1_layer_usage(m1_layer="M1")
        self._m0_layer_usage(m0_layer="M0")

        # ----- pin accessibility --------------------------------------------
        self.opt.log_comment("Binding net usage on top layer ...")
        top_layer = "M2"
        pin.top_layer_net_usage(self, top_layer)
        if self.cell_config.get("limit_m2_usage", {}).get("value", False):
            pin.one_top_layer_track_per_net(self, top_layer)
            pin.one_net_per_top_layer_track(self, top_layer)

        # M1 minimum pin opening (only when MPO > 0).
        self.M1_MPO = self.cell_config["MPO"]["value"]
        if self.M1_MPO > 0:
            pin.m1_minimum_pin_opening(self, top_layer, mar_params, eol_params)

        # M0 pin SON entry-point rule.
        pin.m0_pin(self)
        if self.cell_config["m0_pin_separation"]["value"]:
            pin.m0_pin_separation(self)
        if self.cell_config["m0_pin_extension"]["value"]:
            pin.m0_pin_extension(self, vacancy_edges=self.cell_config["m0_pin_extension"]["vacancy_edges"])

    # ----- per-layer usage helpers ----------------------------------------

    def _m2_layer_usage(self, m2_layer):
        """Bind a per-row M2 usage bool (feeds the m2_usage diagnostic objective)."""
        tmp_m2_row_to_edge_vars = {}
        m2_idx = self.lgg.layer_to_idx[m2_layer]
        for u, v in self.lgg.edges():
            if u[0] == m2_idx and v[0] == m2_idx:
                tmp_m2_row_to_edge_vars.setdefault(u[1], []).append(self.edge_vars[(u, v)])
        for row, vars in tmp_m2_row_to_edge_vars.items():
            self.m2_rows_to_used[row] = self.opt.NewBoolVar(f"M2_row_{row}_usage")
            self.opt.Add(sum(vars) >= 1).OnlyEnforceIf(self.m2_rows_to_used[row])
            self.opt.Add(sum(vars) == 0).OnlyEnforceIf(self.m2_rows_to_used[row].Not())

    def _m1_layer_usage(self, m1_layer):
        """Bind a per-col M1 usage bool (feeds the m1_usage diagnostic objective)."""
        tmp_m1_cols_to_edge_vars = {}
        m1_idx = self.lgg.layer_to_idx[m1_layer]
        for u, v in self.lgg.edges():
            if u[0] == m1_idx and v[0] == m1_idx:
                tmp_m1_cols_to_edge_vars.setdefault(u[2], []).append(self.edge_vars[(u, v)])
        for col, vars in tmp_m1_cols_to_edge_vars.items():
            self.m1_cols_to_used[col] = self.opt.NewBoolVar(f"M1_col_{col}_usage")
            self.opt.Add(sum(vars) >= 1).OnlyEnforceIf(self.m1_cols_to_used[col])
            self.opt.Add(sum(vars) == 0).OnlyEnforceIf(self.m1_cols_to_used[col].Not())

    def _m0_layer_usage(self, m0_layer):
        """Bind a per-row M0 usage bool (feeds the m0_usage diagnostic objective)."""
        tmp_m0_row_to_edge_vars = {}
        m0_idx = self.lgg.layer_to_idx[m0_layer]
        for u, v in self.lgg.edges():
            if u[0] == m0_idx and v[0] == m0_idx:
                tmp_m0_row_to_edge_vars.setdefault(u[1], []).append(self.edge_vars[(u, v)])
        for row, vars in tmp_m0_row_to_edge_vars.items():
            self.m0_rows_to_used[row] = self.opt.NewBoolVar(f"M0_row_{row}_usage")
            self.opt.Add(sum(vars) >= 1).OnlyEnforceIf(self.m0_rows_to_used[row])
            self.opt.Add(sum(vars) == 0).OnlyEnforceIf(self.m0_rows_to_used[row].Not())
