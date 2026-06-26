"""
CFET cell-generation orchestrator.

CFET is a 2-tier vertically-stacked transistor architecture: the top device
lives on the PC (Poly Contact) layer and the bottom device on the BPC (Bottom
Poly Contact) layer. Which device is PMOS vs NMOS is set by the tech's
`stacking_config` ('P_on_N' | 'N_on_P'). There is NO `z_var`: the placement
tier of a transistor is the PHYSICAL device layer, resolved from its model
(`self.pmos_layer` / `self.nmos_layer`). Gate / LISD sharing in CFET is a
CROSS-DEVICE (PC<->BPC) operation, so the per-tier (same-tier) sharing model of
the shared core does NOT apply; this class carries the CFET-specific sharing /
gate-cut / DB logic.

Linear pipeline:

    store inputs
      -> apply config flags
      -> banner + state init + CP model
      -> analyze circuit + initialize subsystems (graph/tech/domain/var/caches)
      -> build constraints (placement + routing) + maybe inject clusters
      -> solve setup + TLU constraint
      -> run solve -> maybe write results

Per-type seams:
    - tech:        CFET_Tech                  (archit/CFET/tech.py)
    - result:      write_cfet_result          (archit/CFET/util.py)
    - visualizer:  visualize_CFET_4T.{draw_layout_with_pin_and_routing, load_results}
    - solver:      self.opt = CPSAT(logfile=...)

CFET-specific routing helpers from the shared core, in constraint order:
    - rt.routing_localization_cfet            (NOT generic routing_localization)
    - rt.cfet_cross_device_via_lower_bound    (after net_terminal_is_shared set)
    - rt.cfet_hpwl_via_cost_tightening        (optional, config-gated)

The shared free functions (rule.*, pin.*, inject.*, accelerate.*, Objective)
follow the "self-as-context-object" contract: fn(self) reads/writes attributes
on this instance. The shared generic functions consult `self.q_tech` and the
CFET-specific ones consult `self.tech`; both alias `self.c_tech`.
"""

import math
import os
import re
from collections import OrderedDict, defaultdict, deque

from loguru import logger

from ortools.sat.python import cp_model

from src.cellgen.archit import config
from src.cellgen.archit.CFET.tech import CFET_Tech
from src.cellgen.archit.CFET.util import write_cfet_result
from src.cellgen.postprocess.visualize_CFET_4T import (
    draw_layout_with_pin_and_routing,
    load_results,
)
from src.cellgen.core import accelerate
from src.cellgen.core import inject
from src.cellgen.core import pin
from src.cellgen.core import routing as rt
from src.cellgen.core import rule
from src.cellgen.core.entity import Circuit, Model
from src.cellgen.core.graph import LayeredGridGraph
from src.cellgen.core.objective import Objective
from src.cellgen.core.util import log_variable_info, print_smtcell_banner
from src.cellgen.core.variable import TransistorVar
from src.cellgen.solver.cpsat_wrapper import CPSAT

_NUM_COL_SDG_ = 3  # number of columns needed for source/drain/gate


class CFET:
    """
    Formulate and place CFETs in the circuit.
    """

    # ----- solve policy --------------------------------------------------
    # Default weighted-sum objective table. Each row: (name, Objective method
    # name, default weight, sense):
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

    def __init__(
        self,
        circuit: Circuit,
        c_tech: CFET_Tech,  # contains layer information
        output_dir: str = "./output/",
        num_col: int | None = None,
        cell_config=None,
        flag_log_constraints: bool = False,
        solver: str = "cpsat",
    ):
        # 1) inputs
        self.circuit = circuit
        self.c_tech = c_tech
        # Alias so the shared GENERIC core (instance.q_tech.*) and the CFET
        # helpers (cfet.tech.*) both resolve to the same tech object.
        self.q_tech = c_tech
        self.output_dir = output_dir
        # cell_config may arrive as either a path (str) or an already-loaded dict.
        # config.read handles the file path -> dict; pass-through if already dict.
        self.cell_config = config.read(cell_config) if isinstance(cell_config, str) else cell_config
        self.solver_name = solver
        self._apply_config_flags()

        # CFET models tiers as PHYSICAL device layers (PC/BPC), not z_var. The
        # accelerate per-tier z_eq gating must therefore use its UNCONDITIONAL
        # form, so leave uses_tier_placement False (TransistorVar.z_var stays
        # None on this instance).
        self.uses_tier_placement = False

        # num_col division reflects the 2 placement tiers (PC + BPC): each gate
        # CFET's two tiers (PC=PMOS, BPC=NMOS) hold DIFFERENT models and share
        # the column grid exactly like a planar cell's P/N rows: a column hosts
        # one PMOS (PC) over one NMOS (BPC). The width bottleneck is therefore
        # max(#PMOS, #NMOS) columns and must NOT be divided by the tier count
        # (num_placement_layers=1). Stacking buys cell HEIGHT, not WIDTH. Using
        # =2 here under-sized the grid and made any cell with >~7
        # transistors/row infeasible (e.g. DFFs).
        self.num_col = (
            num_col if num_col is not None
            else circuit.get_minimum_col(
                num_db=self.insert_num_db + 1,
                num_placement_layers=1,
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

        # 4) solve setup + TLU constraint
        self._setup_solve_strategy()
        self._constrain_top_layer_usage()

        # 5) solve + write
        self._run_solve()
        self._maybe_write_results()

    @property
    def tech(self):
        return self.c_tech

    # ------------------------------------------------------------------ #
    # __init__ helpers (each called exactly once, in workflow order)     #
    # ------------------------------------------------------------------ #

    def _apply_config_flags(self):
        """Cache top-level cell_config flags onto self for hot-path access."""
        cfg = self.cell_config
        self.SET = cfg["model_preset"]["value"]
        self.insert_num_db = cfg["insert_num_db"]["value"]
        self.use_break_symmetry = cfg["use_break_symmetry_for_placement"]["value"]

    def _print_banner(self):
        """Log the SMTCell2.0 banner and current run identity."""
        print_smtcell_banner(
            archit="CFET",
            tech=self.c_tech.lib_name,
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
        self.mos_to_num_finger = {}
        self.nmos_placeable_row_indices = []
        self.pmos_placeable_row_indices = []
        self.nmos_pin_access_ri = []
        self.pmos_pin_access_ri = []
        self.signal_row_indices = []
        self.power_row_indices = {}
        # device-layer resolution (populated by _init_tech)
        self.pmos_layer = None
        self.nmos_layer = None

        # transistor / net top-level maps
        self.transistor_vars = {}
        self.net_vars = {}

        # transistor placement vars (populated by _init_transistor_vars)
        self.placed_tran_ci_vars = {}
        self.has_tran_at_ci_vars = {}

        # diffusion break vars (populated by _init_diffusion_break_vars)
        self.db_pmos_cols_vars = {}
        self.db_nmos_cols_vars = {}
        self.db_cols_vars = {}

        # net source / terminal Super Inner Nodes
        self.node_is_src_vars = {}
        self.node_is_term_vars = {}

        # net-level vars
        self.num_pins_for_io = 0
        self.net_flow_vars = {}
        self.net_to_flow_cnt = {}
        self.net_arc_vars = {}
        self.edge_vars = {}
        self.edge_to_cost = {}

        # Super Outer Nodes (I/O pins)
        self.son_terminal_nodes = {}
        self.node_is_SON_vars = {}
        self.node_to_net_SON_vars = {}

        # ----- routing state ------------------------------------------- #
        self.gate_share_at_col_vars = OrderedDict()
        self.gate_cut_window_vars = {}
        self.lisd_share_at_col_vars = OrderedDict()

        # routing-window coords + bbox (per net.name)
        self.s_coord_x = {}
        self.s_coord_y = {}
        self.t_coord_x = {}
        self.t_coord_y = {}
        self.net_min_x = {}
        self.net_max_x = {}
        self.net_min_y = {}
        self.net_max_y = {}
        self.window_xmin_raw = {}
        self.window_xmax_raw = {}
        self.window_ymin_raw = {}
        self.window_ymax_raw = {}

        # sharing decision provenance (set by _induce_internal_routing_flow_*)
        self.net_terminal_is_shared = {}

        # design-rule scratch
        self.geometric_vars = {}

        # pin / top-layer state
        self.net_use_top_track = {}
        self.net_use_top_track_row_var = {}

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
        }
        if self.solver_name not in builders:
            raise NotImplementedError(
                f"Solver backend {self.solver_name!r} is not supported. "
                f"Available: {sorted(builders)}."
            )
        self.opt = builders[self.solver_name]()

    def _init_subsystems(self):
        """Initialize tech, graph, CP-SAT variable domain, variables, region caches."""
        # _init_tech must run before _init_graph (provides device layers + pin rows).
        self._init_tech()
        self._init_graph()
        self._init_domain()
        self._init_var()
        self._build_region_caches()

    def _build_region_caches(self):
        """No-op placeholder; per-region gather helpers compute on demand."""
        pass

    def _build_constraints(self):
        """Emit placement and routing constraints into the CP-SAT model."""
        self._placement_constraints()
        # ^ Placement Injection - config values are physical coords (from .res
        # files); x_var uses column indices, so divide by PC pitch to convert.
        pc_pitch = self.c_tech.get_pitch(layer_name="PC")
        for t in self._cfg_get("inject_placement", []):
            inject.inject_placement(self, tran_name=t[0], x=int(t[1] / pc_pitch))
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
        """Log model stats; routing-strategy dispatch happens inside wsum (VIA_FIRST)."""
        self.stats()

    def use_via_first_strategy(self):
        """Force early branching on inter-layer (via) edge variables.

        CFET-specific diagnostic strategy. BPC<->PC<->M0 via edges are the main
        source of routing symmetry in CFET: two physically distinct via paths
        (MIV chain vs long via) have equal cost, creating a large symmetric
        search region. Forcing the solver to commit to via edges before
        intra-layer wires tests whether resolving that symmetry early reduces
        prove time. Enable via ``"use_strategy": {"value": "VIA_FIRST"}``.
        """
        via_edge_vars = []
        for (u, v), var in self.edge_vars.items():
            if u[0] != v[0]:  # inter-layer edge = physical via
                via_edge_vars.append(var)

        logger.info(
            f"\t==\t[VIA_FIRST] Adding decision strategy for "
            f"{len(via_edge_vars)} via edge vars"
        )
        if via_edge_vars:
            self.opt.AddDecisionStrategy(
                via_edge_vars,
                cp_model.CHOOSE_LOWEST_MIN,
                cp_model.SELECT_MIN_VALUE,
            )

    def _constrain_top_layer_usage(self):
        """
        Optionally tighten the M2 (top-layer) usage upper bound.

        DEFAULT (CFET): OFF. top_layer_usage is used ONLY as a weight-100
        minimize objective with no hard cap. For a CFET cell whose minimum
        achievable M2 usage equals num_io_nets while insert_num_db<=1, the
        `<= num_io_nets-1` cap can flip a feasible cell to INFEASIBLE or cut off
        the true optimum, so the cap is gated behind an opt-in flag that
        defaults OFF.

        Enable explicitly via cell_config `constrain_top_layer_usage` (or by
        setting `self.constrain_top_layer_usage = True`) ONLY after validating on
        a real CFET cell that the cap does not cut the optimum / cause UNSAT.

        TLU weight=100; secondary objectives have bounded range. For small cells
        (<=1 DB), the secondary swing is <100, so reducing TLU by 1 always improves
        the objective - cap at (num_io_nets - 1). For large cells, cap at
        num_io_nets (natural bound).
        """
        enabled = getattr(
            self,
            "constrain_top_layer_usage",
            self._cfg_get("constrain_top_layer_usage", False),
        )
        if not enabled:
            return

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
        write_cfet_result(
            self.solver, self.circuit, self.transistor_vars, self.edge_vars,
            self.net_arc_vars, self.c_tech, self.cpp_cost,
            filename=res_path,
            lgg=self.lgg,
        )
        # Layer-by-layer placement visualization -> output/.../view/.
        view_dir = os.path.join(self.output_dir, "view")
        os.makedirs(view_dir, exist_ok=True)
        placement, routing = load_results(res_path)
        draw_layout_with_pin_and_routing(
            placement, routing,
            filename=os.path.join(view_dir, f"{subckt}.png"),
        )

    # ================================================================== #
    # subsystem initializers (CFET-specific)                             #
    # ================================================================== #

    def _init_tech(self):
        """Cache technology-derived state: finger counts, placement/pin-access rows, device layers."""
        logger.info("Initializing technology configuration...")
        # map transistor to number of fingers
        for tran in self.circuit.transistors.values():
            tran_width = tran.get_width()
            self.mos_to_num_finger[tran.name] = int(tran_width / self.c_tech.unit_width)
        logger.info(f"\tNumber of fingers: {self.mos_to_num_finger}")

        # row idx where MOSFETs can be placed on
        # CFET: both PMOS and NMOS always place at row 0 on their respective layers
        if self.c_tech.height_config == "SH":
            self.nmos_placeable_row_indices = [0]
            self.pmos_placeable_row_indices = [0]
        logger.info(
            f"\tNMOS placeable rows: {self.nmos_placeable_row_indices}, "
            f"PMOS placeable rows: {self.pmos_placeable_row_indices}"
        )

        # row idx where MOSFETs' pin can be accessed
        # Top device (PC): middle rows; Bottom device (BPC): 2 boundary rows [0, 1].
        if self.c_tech.height_config == "SH" and self.c_tech.num_rt_track == 4:
            top_pin_row = [1, 2]                # two middle rows for top device
            bottom_boundary_rows = [0, 1]       # first & last for bottom device (2-row layer)
            if self.c_tech.stacking_config == "P_on_N":
                self.pmos_pin_access_ri = top_pin_row
                self.nmos_pin_access_ri = bottom_boundary_rows
            else:  # N_on_P
                self.nmos_pin_access_ri = top_pin_row
                self.pmos_pin_access_ri = bottom_boundary_rows
        elif self.c_tech.height_config == "SH" and self.c_tech.num_rt_track == 3:
            signal_rows = [1, 2, 3, 4]
            self.signal_row_indices = signal_rows
            self.power_row_indices = {0: "VSS", 5: "VDD"}
            self.nmos_pin_access_ri = list(signal_rows)
            self.pmos_pin_access_ri = list(signal_rows)
        logger.info(
            f"\tNMOS pin accessible rows: {self.nmos_pin_access_ri}, "
            f"PMOS pin accessible rows: {self.pmos_pin_access_ri}"
        )

        # Set layer names for PMOS and NMOS based on stacking configuration
        self.pmos_layer = self.c_tech.get_pmos_layer()
        self.nmos_layer = self.c_tech.get_nmos_layer()
        logger.info(f"\tPMOS layer: {self.pmos_layer}, NMOS layer: {self.nmos_layer}")

    def _init_graph(self):
        """Build self.lgg (LayeredGridGraph) from the technology layer stack."""
        logger.info("Initializing graph configuration...")
        # NOTE: for all other layers we double the pitch because PC/BPC layers
        # are expected to have float-point values for SD columns; later we divide
        # by 2 to recover the actual column values. num_col reflects S/D/G columns
        # so no doubling for the col grid.
        self.canvas_width = self.num_col * self.c_tech.layer_stack.metal_layers[0].pitch
        self.canvas_height = self.c_tech.num_rt_track * self.c_tech.get_pitch("M0") * 2
        logger.info(f"\tCanvas width: {self.canvas_width}, Canvas height: {self.canvas_height}")

        idx_to_layer = {}
        layer_to_direction = {}
        layer_to_cols = {}
        layer_to_rows = {}
        for li, layer in enumerate(self.c_tech.layer_stack.metal_layers):
            idx_to_layer[li] = layer.layer_name
        # Get list of placement layers (PC and BPC)
        placement_layers = self.c_tech.get_placement_layers()
        # columns on each layer
        for layer in self.c_tech.layer_stack.metal_layers:
            layer_to_direction[layer.layer_name] = layer.direction
            if layer.direction == "H":
                continue
            tmp_pitch = layer.pitch if layer.layer_name in placement_layers else layer.pitch * 2
            tmp_offset = layer.offset
            num_cols = int(math.ceil((self.canvas_width - tmp_offset) / tmp_pitch))
            layer_to_cols[layer.layer_name] = [tmp_offset + i * tmp_pitch for i in range(num_cols)]
        logger.info(f"\tLayer to cols: {layer_to_cols}")

        # rows on each layer - first compute rows for horizontal layers
        h_layer_rows = {}
        for layer in self.c_tech.layer_stack.metal_layers:
            if layer.direction == "V":
                continue
            if self.c_tech.power_config == "M0ICPD" and layer.layer_name == "M0":
                tmp_pitch = layer.pitch
            else:
                tmp_pitch = layer.pitch if layer.layer_name in placement_layers else layer.pitch * 2
            tmp_offset = layer.offset
            num_rows = int(math.ceil((self.canvas_height - tmp_offset) / tmp_pitch))
            h_layer_rows[layer.layer_name] = [tmp_offset + i * tmp_pitch for i in range(num_rows)]

        # For vertical layers, specify rows so they connect to horizontal layers.
        # Top placement layer (directly accessible from M0) gets all M0 rows.
        # Bottom placement layer uses boundary rows by default. For M0ICPD, keep
        # all fine rows so PC/BPC/M0 share top-view row indices.
        m0_rows = h_layer_rows.get("M0", [])
        top_layer = self.c_tech.get_top_placement_layer()
        bottom_layer = self.c_tech.get_bottom_placement_layer()
        if self.c_tech.power_config == "M0ICPD":
            expected_m0_rows = self.c_tech.num_rt_track * 2
            if len(m0_rows) != expected_m0_rows:
                raise ValueError(
                    f"M0ICPD expects {expected_m0_rows} M0 fine rows; actual {len(m0_rows)}"
                )
            bottom_rows = m0_rows
            vconnect_method = "overlap"
        else:
            bottom_rows = [m0_rows[0], m0_rows[-1]] if len(m0_rows) >= 2 else m0_rows
            vconnect_method = "boundary"
        for layer in self.c_tech.layer_stack.metal_layers:
            if layer.direction == "V" and layer.layer_name == top_layer:
                layer_to_rows[layer.layer_name] = m0_rows
            elif layer.direction == "V" and layer.layer_name == bottom_layer:
                layer_to_rows[layer.layer_name] = bottom_rows
            elif layer.direction == "H":
                layer_to_rows[layer.layer_name] = h_layer_rows[layer.layer_name]

        logger.info(f"\tLayer to rows: {layer_to_rows}")
        self.lgg = LayeredGridGraph(
            layer_to_rows=layer_to_rows,
            layer_to_cols=layer_to_cols,
            idx_to_layer=idx_to_layer,
            layer_to_direction=layer_to_direction,
            layer_to_kind=self.c_tech.layer_to_kind,
            virtual_connect_pairs=[("BPC", "M0")],
            virtual_connect_method=vconnect_method,
        )
        self.lgg.stats()

    def _init_domain(self):
        """Initialize the CP-SAT placement domains over the PC (canonical tier) col/row sets."""
        logger.debug("Initializing variable domain...")
        # Convention: odd col index = source/drain (placeable), even col index = gate.
        # Use PC layer for the placement domain (same columns for both PC and BPC).
        self.plc_ci = self.lgg.col_indices_in_layer("PC", parity="odd")[:-1]
        self.domain_mos_placable_ci = cp_model.Domain.FromValues(self.plc_ci)
        logger.info(f"Domain MOS placeable col indices: {self.domain_mos_placable_ci}")
        self.plc_ri = self.lgg.row_indices_in_layer("PC", parity="even")
        self.domain_mos_placable_ri = cp_model.Domain.FromValues(self.plc_ri)
        logger.debug(f"Domain MOS placeable row indices: {self.domain_mos_placable_ri}")
        # source/drain/gate col indices
        self.sd_ci = self.lgg.col_indices_in_layer("PC", parity="odd")
        self.domain_sd_ci = cp_model.Domain.FromValues(self.sd_ci)
        logger.debug(f"Domain SD indices: {self.domain_sd_ci}")
        self.g_ci = self.lgg.col_indices_in_layer("PC", parity="even")
        self.domain_g_ci = cp_model.Domain.FromValues(self.g_ci)
        logger.debug(f"Domain G col indices: {self.domain_g_ci}")
        # all col indices in the PC layer
        self.pc_ci = self.lgg.col_indices_in_layer("PC")
        self.domain_pc_ci = cp_model.Domain.FromValues(self.pc_ci)
        logger.info(f"Domain placement column indices: {self.domain_pc_ci}")
        # all row indices in the PC layer
        self.pc_ri = self.lgg.row_indices_in_layer("PC")
        self.domain_pc_ri = cp_model.Domain.FromValues(self.pc_ri)
        logger.info(f"Domain placement row indices: {self.domain_pc_ri}")
        # all row / col coords in the PC layer
        self.all_pc_row = self.lgg.rows_in_layer("PC")
        self.domain_pc_ri = cp_model.Domain.FromValues(self.all_pc_row)
        logger.info(f"Domain PC row: {self.domain_pc_ri}")
        self.all_pc_col = self.lgg.cols_in_layer("PC")
        self.domain_pc_ci = cp_model.Domain.FromValues(self.all_pc_col)
        logger.info(f"Domain PC col: {self.domain_pc_ci}")

        # BPC layer domains (always enabled for CFET)
        self.bpc_ci = self.lgg.col_indices_in_layer("BPC")
        self.domain_bpc_ci = cp_model.Domain.FromValues(self.bpc_ci)
        logger.info(f"Domain BPC col indices: {self.domain_bpc_ci}")
        self.bpc_ri = self.lgg.row_indices_in_layer("BPC")
        self.domain_bpc_ri = cp_model.Domain.FromValues(self.bpc_ri)
        logger.info(f"Domain BPC row indices: {self.domain_bpc_ri}")
        self.all_bpc_row = self.lgg.rows_in_layer("BPC")
        self.all_bpc_col = self.lgg.cols_in_layer("BPC")
        logger.info(f"Domain BPC row: {self.all_bpc_row}, BPC col: {self.all_bpc_col}")

    def _init_var(self):
        """Populate every CP-SAT variable container."""
        # ^ Transistors
        self._init_transistor_vars()
        self._init_cpp()

        # ^ enforce the min/max column boundaries
        self._init_cell_boundaries()

        # ^ Diffusion breaks
        self._init_diffusion_break_vars()

        # ^ (NET SRC) Super Inner Nodes for internal pins
        self._init_src_super_inner_nodes_vars()

        # ^ (NET TERMINAL) Super Inner Nodes for internal pins
        self._init_term_super_inner_nodes_vars()

        # ^ node adjacency cache (used downstream by flow/arc constraints)
        self.adj_in = {node: [] for node in self.lgg.nodes()}
        self.adj_out = {node: [] for node in self.lgg.nodes()}
        for u_arc, v_arc in self.lgg.arcs():
            self.adj_out[u_arc].append((u_arc, v_arc))
            self.adj_in[v_arc].append((u_arc, v_arc))

        # ^ Net flow / arc / edge variables
        self._init_net_flow_vars()
        self.opt.log_comment("Net arc variables")
        self._init_net_arc_vars()
        self.opt.log_comment("Edge variables")
        self._init_edge_vars()

        # ^ normalize the edge cost to order (reduce the size of the domain)
        self.all_possible_edge_cost = sorted(list(self.edge_to_cost.values()))

        # ^ Super Outer Nodes for I/O pins
        self._init_SON_positions()
        self._init_SON_vars()

        logger.info("\tEnd of variable initialization ...")

    def _init_transistor_vars(self):
        tmp_pmos_x_var = []
        tmp_nmos_x_var = []
        self.opt.log_comment("Transistor variables")
        for tran in self.circuit.transistors.values():
            tvar = TransistorVar(tran.name)
            self.transistor_vars[tran.name] = tvar
            # Variables for placement
            tvar.x_var = self.opt.NewIntVarFromDomain(
                self.domain_mos_placable_ci,
                f"{tran.name}_x",
            )
            tvar.y_var = self.opt.NewIntVarFromDomain(
                self.domain_mos_placable_ri,
                f"{tran.name}_y",
            )
            tvar.flip_var = self.opt.NewBoolVar(
                f"{tran.name}_flip",
            )
            # if SH then y_var is fixed
            if self.c_tech.height_config == "SH":
                if tran.model == Model.PMOS:
                    self.opt.Add(tvar.y_var == self.pmos_placeable_row_indices[0])
                    tmp_pmos_x_var.append(tvar.x_var)
                elif tran.model == Model.NMOS:
                    self.opt.Add(tvar.y_var == self.nmos_placeable_row_indices[0])
                    tmp_nmos_x_var.append(tvar.x_var)
        # each transistor must be placed in a different column
        if self.c_tech.height_config == "SH":
            self.opt.AddAllDifferent(tmp_pmos_x_var)
            self.opt.AddAllDifferent(tmp_nmos_x_var)

        for tran in self.circuit.transistors.values():
            tvar = self.transistor_vars[tran.name]
            for ci in self.plc_ci:
                tran_is_placed_col_var = self.opt.NewBoolVar(f"tran_placed_col_{tran.name}_{ci}")
                self.placed_tran_ci_vars[(tran.name, ci)] = tran_is_placed_col_var
                # if x_var is placed at col, then turn on this variable
                self.opt.Add(tvar.x_var == ci).OnlyEnforceIf(tran_is_placed_col_var)
                self.opt.Add(tvar.x_var != ci).OnlyEnforceIf(tran_is_placed_col_var.Not())

        # Per-column indicator: is ANY transistor placed here?
        for ci in self.plc_ci:
            has_tran = self.opt.NewBoolVar(f"has_tran_ci_{ci}")
            self.has_tran_at_ci_vars[ci] = has_tran
            all_placed_here = [self.placed_tran_ci_vars[(t.name, ci)] for t in self.circuit.transistors.values()]
            self.opt.AddBoolOr(all_placed_here).OnlyEnforceIf(has_tran)
            self.opt.Add(sum(all_placed_here) == 0).OnlyEnforceIf(has_tran.Not())

    def _init_cpp(self):
        self.opt.log_comment("Enforcing total cpp...")
        # NOTE: define cpp_cost early to provide boundary constraints
        self.cpp_cost = self.opt.NewIntVarFromDomain(
            self.domain_sd_ci,
            "cpp_cost",
        )
        self.opt.AddMaxEquality(
            self.cpp_cost,
            [self.transistor_vars[tran.name].x_var for tran in self.circuit.transistors.values()],
        )
        # Lower bound from AllDifferent: N transistors need N distinct columns
        num_pmos = self.circuit.num_pmos_transistors()
        num_nmos = self.circuit.num_nmos_transistors()
        min_transistors_per_row = max(num_pmos, num_nmos)
        if min_transistors_per_row > 0:
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
            # Warm-start hints: pack transistors at the lowest columns
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
        self.min_boundary_col = self.lgg.max_col_in_layer("PC") - self.insert_num_db * 2 * self.c_tech.get_pitch("PC")
        self.max_boundary_col = self.lgg.max_col_in_layer("PC")

    def _init_diffusion_break_vars(self):
        # NOTE diffusion break should only be inserted in plc columns
        self.opt.log_comment("Diffusion break variables")
        for ci in self.plc_ci:
            pdb_var = self.opt.NewBoolVar(f"db_pmos_ci_{ci}")
            self.db_pmos_cols_vars[ci] = pdb_var
            ndb_var = self.opt.NewBoolVar(f"db_nmos_ci_{ci}")
            self.db_nmos_cols_vars[ci] = ndb_var
            # 1) if db then no MOSFETs sits at this column
            for tran in self.circuit.transistors.values():
                tvar = self.transistor_vars[tran.name]
                if tran.model == Model.PMOS:
                    self.opt.Add(tvar.x_var != ci).OnlyEnforceIf(pdb_var)
                elif tran.model == Model.NMOS:
                    self.opt.Add(tvar.x_var != ci).OnlyEnforceIf(ndb_var)
            # 2) build reifiers for "x_pmos[k] == col" via placed_tran_ci_vars
            tmp_pmos_eqs = []
            tmp_nmos_eqs = []
            for tran in self.circuit.transistors.values():
                if tran.model == Model.PMOS:
                    plc_var = self.placed_tran_ci_vars[(tran.name, ci)]
                    tmp_pmos_eqs.append(plc_var)
                elif tran.model == Model.NMOS:
                    plc_var = self.placed_tran_ci_vars[(tran.name, ci)]
                    tmp_nmos_eqs.append(plc_var)
            # if not db then somebody *must* sit at col
            self.opt.Add(sum(tmp_pmos_eqs) >= 1).OnlyEnforceIf(pdb_var.Not())
            self.opt.Add(sum(tmp_nmos_eqs) >= 1).OnlyEnforceIf(ndb_var.Not())

    def _init_src_super_inner_nodes_vars(self):
        self.opt.log_comment("Super Inner Nodes for src pins")
        for net in self.circuit.get_nets(with_power_ground=False):
            self.node_is_src_vars[net.name] = {}  # (layer, row, col) -> bool var
            src_tran_name, src_pin = net.source()
            src_tvar = self.transistor_vars[src_tran_name]
            src_tran = self.circuit.transistors[src_tran_name]
            if src_tran.model == Model.PMOS:
                tmp_pin_accessible_row_indices = self.pmos_pin_access_ri
                tran_layer = self.pmos_layer  # PC or BPC based on stacking
            elif src_tran.model == Model.NMOS:
                tmp_pin_accessible_row_indices = self.nmos_pin_access_ri
                tran_layer = self.nmos_layer  # BPC or PC based on stacking
            else:
                raise ValueError(f"Transistor {src_tran_name} is not a PMOS or NMOS transistor")
            layer_idx = self.lgg.layer_index(tran_layer)
            tran_ci = self.lgg.col_indices_in_layer(tran_layer)
            for ri in tmp_pin_accessible_row_indices:
                row = self.lgg.row_in_layer(tran_layer, ri)
                for ci in tran_ci:
                    col = self.lgg.col_in_layer(tran_layer, ci)
                    if src_pin == "source" and self.lgg.is_odd_col(layer=tran_layer, col=col):
                        s_col_var = self.opt.NewBoolVar(f"net_issrc_{net.name}_L{layer_idx}_R{row}_C{col}")
                        src_tvar.s_col_idx_var.setdefault(net.name, {}).setdefault(col, []).append(s_col_var)
                        self.node_is_src_vars[net.name][(layer_idx, row, col)] = s_col_var
                    elif src_pin == "gate" and self.lgg.is_even_col(layer=tran_layer, col=col):
                        g_col_var = self.opt.NewBoolVar(f"net_issrc_{net.name}_L{layer_idx}_R{row}_C{col}")
                        src_tvar.g_col_idx_var.setdefault(net.name, {}).setdefault(col, []).append(g_col_var)
                        self.node_is_src_vars[net.name][(layer_idx, row, col)] = g_col_var
                    elif src_pin == "drain" and self.lgg.is_odd_col(layer=tran_layer, col=col):
                        d_col_var = self.opt.NewBoolVar(f"net_issrc_{net.name}_L{layer_idx}_R{row}_C{col}")
                        src_tvar.d_col_idx_var.setdefault(net.name, {}).setdefault(col, []).append(d_col_var)
                        self.node_is_src_vars[net.name][(layer_idx, row, col)] = d_col_var

    def _init_term_super_inner_nodes_vars(self):
        self.opt.log_comment("Super Inner Nodes for terminal pins")
        for net in self.circuit.get_nets(with_power_ground=False):
            self.node_is_term_vars[net.name] = {}  # k -> (layer, row, col) -> bool var
            for k, (term_tran_name, term_pin) in enumerate(net.terminals()):
                self.node_is_term_vars[net.name][k] = {}
                term_tran = self.circuit.transistors[term_tran_name]
                term_tvar = self.transistor_vars[term_tran_name]
                if term_tran.model == Model.PMOS:
                    tmp_pin_accessible_row_indices = self.pmos_pin_access_ri
                    tran_layer = self.pmos_layer
                elif term_tran.model == Model.NMOS:
                    tmp_pin_accessible_row_indices = self.nmos_pin_access_ri
                    tran_layer = self.nmos_layer
                else:
                    raise ValueError(f"Transistor {term_tran} is not a PMOS or NMOS transistor")
                layer_idx = self.lgg.layer_index(tran_layer)
                tran_ci = self.lgg.col_indices_in_layer(tran_layer)
                for ri in tmp_pin_accessible_row_indices:
                    row = self.lgg.row_in_layer(tran_layer, ri)
                    for ci in tran_ci:
                        col = self.lgg.col_in_layer(tran_layer, ci)
                        if term_pin == "source" and self.lgg.is_odd_col(layer=tran_layer, col=col):
                            s_col_var = self.opt.NewBoolVar(f"net_isterm_{net.name}_{k}_L{layer_idx}_R{row}_C{col}")
                            term_tvar.s_col_idx_var.setdefault(net.name, {}).setdefault(col, []).append(s_col_var)
                            self.node_is_term_vars[net.name][k][(layer_idx, row, col)] = s_col_var
                        elif term_pin == "gate" and self.lgg.is_even_col(layer=tran_layer, col=col):
                            g_col_var = self.opt.NewBoolVar(f"net_isterm_{net.name}_{k}_L{layer_idx}_R{row}_C{col}")
                            term_tvar.g_col_idx_var.setdefault(net.name, {}).setdefault(col, []).append(g_col_var)
                            self.node_is_term_vars[net.name][k][(layer_idx, row, col)] = g_col_var
                        elif term_pin == "drain" and self.lgg.is_odd_col(layer=tran_layer, col=col):
                            d_col_var = self.opt.NewBoolVar(f"net_isterm_{net.name}_{k}_L{layer_idx}_R{row}_C{col}")
                            term_tvar.d_col_idx_var.setdefault(net.name, {}).setdefault(col, []).append(d_col_var)
                            self.node_is_term_vars[net.name][k][(layer_idx, row, col)] = d_col_var

    def _init_net_flow_vars(self):
        self.opt.log_comment("Net flow variables")
        for net in self.circuit.get_nets(with_power_ground=False):
            num_extra_flow = 0
            if net.is_io_net():
                num_extra_flow = 1
                self.num_pins_for_io += 1
            for k in range(net.num_terminals() + num_extra_flow):
                for u_arc, v_arc in self.lgg.arcs():
                    net_flow_var = self.opt.NewBoolVar(f"flow_{net.name}_{k}_{u_arc}_{v_arc}")
                    self.net_flow_vars[(net.name, k, u_arc, v_arc)] = net_flow_var
            self.net_to_flow_cnt[net.name] = net.num_terminals() + num_extra_flow

    def _init_net_arc_vars(self):
        self.opt.log_comment("Net arc variables")
        for net in self.circuit.get_nets(with_power_ground=False):
            for u_arc, v_arc in self.lgg.arcs():
                net_arc_var = self.opt.NewBoolVar(f"arc_{net.name}_{u_arc}_{v_arc}")
                self.net_arc_vars[(net.name, u_arc, v_arc)] = net_arc_var

    def _init_edge_vars(self):
        self.opt.log_comment("Edge variables")
        # Get layer indices for placement layers (BPC and PC)
        placement_layer_indices = set()
        for layer_name in self.c_tech.get_placement_layers():
            try:
                placement_layer_indices.add(self.lgg.layer_index(layer_name))
            except Exception:
                pass  # Layer may not exist
        # Get top routing layer index (M2 in typical stack)
        try:
            top_routing_layer_idx = self.lgg.layer_index("M2")
        except Exception:
            top_routing_layer_idx = max(self.lgg.idx_to_layer.keys())

        bpc_idx = self.lgg.layer_index("BPC")
        m0_idx = self.lgg.layer_index("M0")
        for u_edge, v_edge in self.lgg.edges():
            edge_var = self.opt.NewBoolVar(f"edge_{u_edge}_{v_edge}")
            self.edge_vars[(u_edge, v_edge)] = edge_var
            # if edge is a via
            if u_edge[0] != v_edge[0]:
                # BPC<->M0 (long via) is expensive - penalize to prefer MIV path
                if {u_edge[0], v_edge[0]} == {bpc_idx, m0_idx}:
                    self.edge_to_cost[(u_edge, v_edge)] = 20
                else:
                    self.edge_to_cost[(u_edge, v_edge)] = 5
            else:
                # CFET FLAG: flat wire cost (placement / top routing layers == 1 too)
                if u_edge[0] in placement_layer_indices or u_edge[0] == top_routing_layer_idx:
                    self.edge_to_cost[(u_edge, v_edge)] = 1
                else:
                    self.edge_to_cost[(u_edge, v_edge)] = 1

    def _init_SON_positions(self):
        # allow pin access at M0 / M1 / M2; CFET uses M1 SONs
        tmp_son_row_indices = self._get_son_row_indices()
        self.son_terminal_nodes["M0"] = []
        for _nodes in self.lgg.nodes_in_layer("M0"):
            pass
        self.son_terminal_nodes["M1"] = []
        for nodes in self.lgg.nodes_in_layer("M1"):
            for ri in tmp_son_row_indices:
                row = self.lgg.row_in_layer("M1", ri)
                if nodes[1] == row:
                    self.son_terminal_nodes["M1"].append(nodes)
        self.son_terminal_nodes["M2"] = []
        for _nodes in self.lgg.nodes_in_layer("M2"):
            pass

    def _init_SON_vars(self):
        self.opt.log_comment("Super Outer Nodes for I/O pins")
        for net in self.circuit.get_nets(with_power_ground=False):
            if not net.is_io_net():
                continue
            self.node_is_SON_vars[net.name] = {}  # k -> (layer, row, col) -> bool var
            for k in range(net.num_terminals(), self.net_to_flow_cnt[net.name], 1):
                self.node_is_SON_vars[net.name][k] = {}
                for node in self.son_terminal_nodes["M1"]:
                    layer_idx = node[0]
                    row = node[1]
                    col = node[2]
                    self.node_is_SON_vars[net.name][k][(layer_idx, row, col)] = self.opt.NewBoolVar(
                        f"net_isSON_{net.name}_{k}_L{layer_idx}_R{row}_C{col}"
                    )
                    self.node_to_net_SON_vars.setdefault((layer_idx, row, col), {}).setdefault(net.name, []).append(
                        self.node_is_SON_vars[net.name][k][(layer_idx, row, col)]
                    )
                # exactly one SON node must be used per (net, k)
                self.opt.Add(sum(self.node_is_SON_vars[net.name][k].values()) == 1)

    def _get_son_row_indices(self):
        if self.c_tech.height_config != "SH":
            return None

        _ROW_MAP = {
            2: [0, 1],
            3: [1, 4],
            4: [1, 2],
            5: [1, 3],
            6: [2, 3],
        }
        try:
            return _ROW_MAP[self.c_tech.num_rt_track]
        except KeyError:
            raise ValueError(f"Unsupported number of rows: {self.c_tech.num_rt_track}")

    # ================================================================== #
    # placement constraints (CFET-specific)                             #
    # ================================================================== #

    def _placement_constraints(self):
        """Build placement constraints (CFET physical-layer-tier model)."""
        # ^ Linking source/drain/gate columns to transistor placement
        self._link_source_drain_gate_columns_to_transistor_placement()
        pwr_config = "pONn" if self.c_tech.stacking_config == "P_on_N" else "nONp"
        self._ban_other_nets_on_pwr_columns(config=pwr_config)
        # ^ no CA contact allowed unless the column resides a source or term
        self._prohibit_CA_contact_on_non_source_term_columns()
        # ^ if a diffusion break is used in PMOS, then NMOS must also use it at the same column
        self._diffusion_alignment()
        # ^ reduce the number of diffusion breaks by setting the allowable columns
        self._limit_diffusion_breaks()
        # ^ Lexicographic Order Symmetry Breaking. Skip when placements injected.
        if not self._cfg_get("inject_placement", []):
            self._placement_lexico_order_symmetry_breaking()
        # ^ pairwise diffusion / lisd / gate sharing (cross-device for CFET)
        self._pairwise_diffusion_sharing()
        self._pairwise_lisd_sharing()
        self._pairwise_gate_sharing()

    def _link_source_drain_gate_columns_to_transistor_placement(self):
        self.opt.log_comment("Linking source/drain/gate columns to transistor placement")
        for tran in self.circuit.transistors.values():
            tvar = self.transistor_vars[tran.name]
            source_net, gate_net, drain_net = tran.source, tran.gate, tran.drain
            for ci in self.plc_ci:
                tran_is_placed_col_var = self.placed_tran_ci_vars.get((tran.name, ci))
                col = self.lgg.col_in_layer("PC", ci)        # s/d col
                ci_r = ci + 1
                col_r = self.lgg.col_in_layer("PC", ci_r)    # gate col
                ci_rr = ci + 2
                col_rr = self.lgg.col_in_layer("PC", ci_rr)  # s/d col
                # ^ if flipped, then source @ col_rr, drain @ col
                if tran.model == Model.PMOS:
                    nodes_at_s_col = self.gather_nodes_in_pmos_region(col=col_rr)
                    nodes_at_d_col = self.gather_nodes_in_pmos_region(col=col)
                elif tran.model == Model.NMOS:
                    nodes_at_s_col = self.gather_nodes_in_nmos_region(col=col_rr)
                    nodes_at_d_col = self.gather_nodes_in_nmos_region(col=col)
                else:
                    raise ValueError(f"Unknown model: {tran.model}")
                # exactly one of the s_col_idx_var must be 1 at col_rr
                s_col_vars_on_col_rr = []
                for net, col_vars in tvar.s_col_idx_var.items():
                    s_col_vars = col_vars.get(col_rr, [])
                    if len(s_col_vars) > 0:
                        s_col_vars_on_col_rr.extend(s_col_vars)
                if len(s_col_vars_on_col_rr) > 0:
                    self.opt.Add(sum(s_col_vars_on_col_rr) == 1).OnlyEnforceIf(
                        [tvar.flip_var, tran_is_placed_col_var]
                    )
                self._ban_other_nets_from_using_nodes(
                    net_to_skip=source_net,
                    nodes=nodes_at_s_col,
                    cond=[tvar.flip_var, tran_is_placed_col_var],
                )
                for net, col_vars in tvar.s_col_idx_var.items():
                    for col_other, s_col_vars in col_vars.items():
                        if col_other != col_rr and len(s_col_vars) > 0:
                            self.opt.Add(sum(s_col_vars) == 0).OnlyEnforceIf([tvar.flip_var, tran_is_placed_col_var])
                # exactly one of the d_col_idx_var must be 1 at col
                d_col_vars_on_col = []
                for net, col_vars in tvar.d_col_idx_var.items():
                    d_col_vars = col_vars.get(col, [])
                    if len(d_col_vars) > 0:
                        d_col_vars_on_col.extend(d_col_vars)
                if len(d_col_vars_on_col) > 0:
                    self.opt.Add(sum(d_col_vars_on_col) == 1).OnlyEnforceIf(
                        [tvar.flip_var, tran_is_placed_col_var]
                    )
                self._ban_other_nets_from_using_nodes(
                    net_to_skip=drain_net,
                    nodes=nodes_at_d_col,
                    cond=[tvar.flip_var, tran_is_placed_col_var],
                )
                for net, col_vars in tvar.d_col_idx_var.items():
                    for col_other, d_col_vars in col_vars.items():
                        if col_other != col and len(d_col_vars) > 0:
                            self.opt.Add(sum(d_col_vars) == 0).OnlyEnforceIf(tvar.flip_var, tran_is_placed_col_var)
                # ^ if not flipped, then source @ col, drain @ col_rr
                if tran.model == Model.PMOS:
                    nodes_at_s_col = self.gather_nodes_in_pmos_region(col=col)
                    nodes_at_d_col = self.gather_nodes_in_pmos_region(col=col_rr)
                elif tran.model == Model.NMOS:
                    nodes_at_s_col = self.gather_nodes_in_nmos_region(col=col)
                    nodes_at_d_col = self.gather_nodes_in_nmos_region(col=col_rr)
                else:
                    raise ValueError(f"Unknown model {tran.model}")
                s_col_vars_on_col = []
                for net, col_vars in tvar.s_col_idx_var.items():
                    s_col_vars = col_vars.get(col, [])
                    if len(s_col_vars) > 0:
                        s_col_vars_on_col.extend(s_col_vars)
                if len(s_col_vars_on_col) > 0:
                    self.opt.Add(sum(s_col_vars_on_col) == 1).OnlyEnforceIf(
                        [tvar.flip_var.Not(), tran_is_placed_col_var]
                    )
                self._ban_other_nets_from_using_nodes(
                    net_to_skip=source_net,
                    nodes=nodes_at_s_col,
                    cond=[tvar.flip_var.Not(), tran_is_placed_col_var],
                )
                for net, col_vars in tvar.s_col_idx_var.items():
                    for col_other, s_col_vars in col_vars.items():
                        if col_other != col and len(s_col_vars) > 0:
                            self.opt.Add(sum(s_col_vars) == 0).OnlyEnforceIf([tvar.flip_var.Not(), tran_is_placed_col_var])
                d_col_vars_on_col_rr = []
                for net, col_vars in tvar.d_col_idx_var.items():
                    d_col_vars = col_vars.get(col_rr, [])
                    if len(d_col_vars) > 0:
                        d_col_vars_on_col_rr.extend(d_col_vars)
                if len(d_col_vars_on_col_rr) > 0:
                    self.opt.Add(sum(d_col_vars_on_col_rr) == 1).OnlyEnforceIf(
                        [tvar.flip_var.Not(), tran_is_placed_col_var]
                    )
                self._ban_other_nets_from_using_nodes(
                    net_to_skip=drain_net,
                    nodes=nodes_at_d_col,
                    cond=[tvar.flip_var.Not(), tran_is_placed_col_var],
                )
                for net, col_vars in tvar.d_col_idx_var.items():
                    for col_other, d_col_vars in col_vars.items():
                        if col_other != col_rr and len(d_col_vars) > 0:
                            self.opt.Add(sum(d_col_vars) == 0).OnlyEnforceIf([tvar.flip_var.Not(), tran_is_placed_col_var])
                # ^ regardless of flip, gate @ col_r
                g_col_vars_on_col_r = []
                for net, col_vars in tvar.g_col_idx_var.items():
                    g_col_vars = col_vars.get(col_r, [])
                    if len(g_col_vars) > 0:
                        g_col_vars_on_col_r.extend(g_col_vars)
                if len(g_col_vars_on_col_r) > 0:
                    self.opt.Add(sum(g_col_vars_on_col_r) == 1).OnlyEnforceIf(
                        [tran_is_placed_col_var]
                    )
                if tran.model == Model.PMOS:
                    nodes_at_g_col = self.gather_nodes_in_pmos_region(col=col_r)
                elif tran.model == Model.NMOS:
                    nodes_at_g_col = self.gather_nodes_in_nmos_region(col=col_r)
                self._ban_other_nets_from_using_nodes(
                    net_to_skip=gate_net,
                    nodes=nodes_at_g_col,
                    cond=tran_is_placed_col_var,
                )
                for net, col_vars in tvar.g_col_idx_var.items():
                    for col_other, g_col_vars in col_vars.items():
                        if col_other != col_r and len(g_col_vars) > 0:
                            self.opt.Add(sum(g_col_vars) == 0).OnlyEnforceIf(tran_is_placed_col_var)

    def _ban_other_nets_on_pwr_columns(self, config):
        """
        Do not allow any other net to use the power columns on the top placement layer.
        For P_on_N: ban on VDD columns (PMOS power).
        For N_on_P: ban on VSS columns (NMOS power).
        """
        assert config in ["pONn", "nONp"], f"Unknown config: {config}"
        self.opt.log_comment("Enforcing no other net on power columns ...")
        if config == "pONn":
            target_pwr_net = "VDD"
        else:
            target_pwr_net = "VSS"
        for net in self.circuit.get_power_ground_nets():
            if net.name != target_pwr_net:
                continue
            logger.info(f"Net: {net.name} Connected Transistors: {net.connected_transistors}")
            for tran_name, tran_pin in net.connected_transistors:
                if tran_pin == "gate":
                    continue
                tran = self.circuit.transistors[tran_name]
                tvar = self.transistor_vars[tran_name]
                if tran.model == Model.PMOS:
                    pin_access_ri = self.pmos_pin_access_ri
                    gather_fn = self.gather_nodes_in_pmos_region
                    tran_layer = self.pmos_layer
                elif tran.model == Model.NMOS:
                    pin_access_ri = self.nmos_pin_access_ri
                    gather_fn = self.gather_nodes_in_nmos_region
                    tran_layer = self.nmos_layer
                else:
                    raise ValueError(f"Unknown model: {tran.model}")
                for ci in self.plc_ci:
                    tran_is_placed_col_var = self.placed_tran_ci_vars.get((tran_name, ci))
                    col = self.lgg.col_in_layer("PC", ci)
                    col_rr = self.lgg.col_in_layer("PC", ci + 2)
                    # flipped: source at col_rr, drain at col
                    nodes_at_s_col = []
                    nodes_at_d_col = []
                    for ri in pin_access_ri:
                        row = self.lgg.row_in_layer(tran_layer, ri)
                        nodes_at_s_col += gather_fn(col=col_rr, row=row)
                        nodes_at_d_col += gather_fn(col=col, row=row)
                    if tran_pin == "source":
                        self._ban_other_nets_from_using_nodes(
                            net_to_skip=net.name, nodes=nodes_at_s_col,
                            cond=[tvar.flip_var, tran_is_placed_col_var])
                    elif tran_pin == "drain":
                        self._ban_other_nets_from_using_nodes(
                            net_to_skip=net.name, nodes=nodes_at_d_col,
                            cond=[tvar.flip_var, tran_is_placed_col_var])
                    # not flipped: source at col, drain at col_rr
                    nodes_at_s_col = []
                    nodes_at_d_col = []
                    for ri in pin_access_ri:
                        row = self.lgg.row_in_layer(tran_layer, ri)
                        nodes_at_s_col += gather_fn(col=col, row=row)
                        nodes_at_d_col += gather_fn(col=col_rr, row=row)
                    if tran_pin == "source":
                        self._ban_other_nets_from_using_nodes(
                            net_to_skip=net.name, nodes=nodes_at_s_col,
                            cond=[tvar.flip_var.Not(), tran_is_placed_col_var])
                    elif tran_pin == "drain":
                        self._ban_other_nets_from_using_nodes(
                            net_to_skip=net.name, nodes=nodes_at_d_col,
                            cond=[tvar.flip_var.Not(), tran_is_placed_col_var])

    def _ban_other_nets_from_using_nodes(self, net_to_skip, nodes, cond, debug_mode=False, ignore_bottom_via=False):
        # Collect arcs touching any protected node via adjacency index
        node_set = set(nodes)
        touching_arcs = set()
        for node in node_set:
            for arc in self.adj_out.get(node, ()):
                touching_arcs.add(arc)
            for arc in self.adj_in.get(node, ()):
                touching_arcs.add(arc)
        if ignore_bottom_via:
            bpc_idx = self.lgg.layer_index("BPC")
            pc_idx = self.lgg.layer_index("PC")
            touching_arcs = {
                (u_arc, v_arc) for u_arc, v_arc in touching_arcs
                if not ({u_arc[0], v_arc[0]} == {bpc_idx, pc_idx} and u_arc[1] == 0 and v_arc[1] == 0)
            }
        # Pre-collect the other nets once
        other_nets = [net for net in self.circuit.get_nets(with_power_ground=False) if net.name != net_to_skip]
        if not other_nets:
            return
        # For each arc, create ONE aggregated sum constraint. Flow bans are
        # omitted: _link_flow_to_arc (flow -> arc) ensures flow=0 when arc=0.
        for u_arc, v_arc in touching_arcs:
            other_arc_vars = [self.net_arc_vars[(net.name, u_arc, v_arc)] for net in other_nets]
            if debug_mode:
                logger.info(f"\t\t{net_to_skip} banning {len(other_nets)} nets from ({u_arc}, {v_arc}) if {cond}")
            self.opt.Add(sum(other_arc_vars) == 0).OnlyEnforceIf(cond)

    def _prohibit_CA_contact_on_non_source_term_columns(self):
        for ci in self.sd_ci:
            col = self.lgg.col_in_layer("PC", ci)
            self.opt.log_comment(f"Prohibiting CA contact on non-source term at columns {col} ...")
            # PMOS region
            src_term_vars_pmos = self.gather_src_term_vars_in_pmos_region(col=col)
            pmos_contact_edge_vars = self.gather_via_vars_in_pmos_region(col=col)
            for p_via_var in pmos_contact_edge_vars:
                self.opt.AddBoolOr(src_term_vars_pmos).OnlyEnforceIf(p_via_var)
            # NMOS region
            src_term_vars_nmos = self.gather_src_term_vars_in_nmos_region(col=col)
            nmos_contact_edge_vars = self.gather_via_vars_in_nmos_region(col=col)
            for n_via_var in nmos_contact_edge_vars:
                self.opt.AddBoolOr(src_term_vars_nmos).OnlyEnforceIf(n_via_var)

    def _diffusion_alignment(self):
        self.opt.log_comment("Enforcing diffusion alignment between PMOS and NMOS...")
        # NOTE: double implication is somehow better than the single bidirectional implication
        if self.c_tech.enforce_diffusion_alignment:
            logger.info("\t==\tEnforcing diffusion alignment between PMOS and NMOS...")
            for ci in self.plc_ci:
                self.opt.AddImplication(
                    self.db_pmos_cols_vars[ci],
                    self.db_nmos_cols_vars[ci],
                )
                self.opt.AddImplication(
                    self.db_nmos_cols_vars[ci],
                    self.db_pmos_cols_vars[ci],
                )
                self.db_cols_vars[ci] = self.opt.NewBoolVar(f"db_ci_{ci}")
                self.opt.Add(self.db_cols_vars[ci] == 1).OnlyEnforceIf(
                    [self.db_pmos_cols_vars[ci], self.db_nmos_cols_vars[ci]]
                )
                self.opt.Add(self.db_cols_vars[ci] == 0).OnlyEnforceIf(
                    [self.db_pmos_cols_vars[ci].Not(), self.db_nmos_cols_vars[ci].Not()]
                )

    def _limit_diffusion_breaks(self):
        self.opt.log_comment("Setting allowable diffusion break columns...")
        if self.c_tech.allowable_diffusion_break_cols == "ALL":
            pass
        elif self.c_tech.allowable_diffusion_break_cols == "NONE":
            for ci in self.plc_ci:
                self.opt.Add(self.db_pmos_cols_vars[ci] == 0)
                self.opt.Add(self.db_nmos_cols_vars[ci] == 0)
        elif self.c_tech.allowable_diffusion_break_cols == "SPLIT":
            col_indices = self.plc_ci
            total_cols = len(col_indices)
            one_fourth_col_idx = int(total_cols / 4) + 1  # +1 to make it less aggressive
            for pos, ci in enumerate(col_indices):
                if pos >= one_fourth_col_idx and pos <= total_cols - one_fourth_col_idx:
                    self.opt.Add(self.db_pmos_cols_vars[ci] == 0)
                    self.opt.Add(self.db_nmos_cols_vars[ci] == 0)
        elif self.c_tech.allowable_diffusion_break_cols == "CENTER":
            col_indices = self.plc_ci
            total_cols = len(col_indices)
            one_fourth_col_idx = int(total_cols / 4) - 1  # -1 to make it less aggressive
            for pos, ci in enumerate(col_indices):
                if pos >= one_fourth_col_idx and pos <= total_cols - one_fourth_col_idx:
                    self.opt.Add(self.db_pmos_cols_vars[ci] == 0)
                    self.opt.Add(self.db_nmos_cols_vars[ci] == 0)
        elif self.c_tech.allowable_diffusion_break_cols == "OTHER":
            for i, ci in enumerate(self.plc_ci):
                if i % 2 == 0:
                    self.opt.Add(self.db_pmos_cols_vars[ci] == 0)
                    self.opt.Add(self.db_nmos_cols_vars[ci] == 0)
        else:
            raise ValueError(f"Unknown diffusion break cols: {self.c_tech.allowable_diffusion_break_cols}")

    def _placement_lexico_order_symmetry_breaking(self):
        self.opt.log_comment("Enforcing Lexicographic Order Symmetry Breaking...")
        tmp_X = [self.transistor_vars[tran.name].x_var for tran in sorted(self.circuit.transistors.values())]
        tmp_X_rev = list(reversed(tmp_X))
        tmp_eq, tmp_lt = [], []
        for i in range(len(tmp_X)):
            ei = self.opt.NewBoolVar(f"eq_{i}")
            li = self.opt.NewBoolVar(f"lt_{i}")
            self.opt.Add(tmp_X[i] == tmp_X_rev[i]).OnlyEnforceIf(ei)
            self.opt.Add(tmp_X[i] != tmp_X_rev[i]).OnlyEnforceIf(ei.Not())
            self.opt.Add(tmp_X[i] < tmp_X_rev[i]).OnlyEnforceIf(li)
            self.opt.Add(tmp_X[i] >= tmp_X_rev[i]).OnlyEnforceIf(li.Not())
            tmp_eq.append(ei)
            tmp_lt.append(li)
        tmp_clause = []
        tmp_prefix = None
        for i in range(len(tmp_X)):
            if i == 0:
                tmp_clause.append(tmp_lt[i])
                tmp_prefix = tmp_eq[i]
            else:
                ci = self.opt.NewBoolVar(f"lex_break_{i}")
                self.opt.AddBoolAnd([tmp_prefix, tmp_lt[i]]).OnlyEnforceIf(ci)
                self.opt.AddBoolOr([tmp_prefix.Not(), tmp_lt[i].Not()]).OnlyEnforceIf(ci.Not())
                tmp_clause.append(ci)
                new_pref = self.opt.NewBoolVar(f"lex_pref_{i}")
                self.opt.AddBoolAnd([tmp_prefix, tmp_eq[i]]).OnlyEnforceIf(new_pref)
                self.opt.AddBoolOr([tmp_prefix.Not(), tmp_eq[i].Not()]).OnlyEnforceIf(new_pref.Not())
                tmp_prefix = new_pref
        if not tmp_X:
            pass
        elif not tmp_clause and tmp_prefix is not None:
            self.opt.Add(tmp_prefix == 1)
        else:
            self.opt.AddBoolOr(tmp_clause + [tmp_prefix])

    def _pairwise_diffusion_sharing(self):
        self.opt.log_comment("Enforcing pairwise diffusion sharing...")
        db_dist = None
        if self.c_tech.diffusion_break_type == "SDB":
            db_dist = 2
        elif self.c_tech.diffusion_break_type == "DDB":
            db_dist = 4
        elif self.c_tech.diffusion_break_type == "MDB":
            raise NotImplementedError("Mixed Diffusion Break is not implemented.")
        self.ds_pair_vars = {}
        self.net_ds_sharable_pairs = {}
        tmp_tran = sorted(list(self.circuit.transistors.values()))

        for i, tran_1 in enumerate(tmp_tran):
            x_var_1 = self.transistor_vars[tran_1.name].x_var
            flip_var_1 = self.transistor_vars[tran_1.name].flip_var
            for tran_2 in tmp_tran[i + 1:]:
                x_var_2 = self.transistor_vars[tran_2.name].x_var
                flip_var_2 = self.transistor_vars[tran_2.name].flip_var
                # same mos type
                if tran_1.model != tran_2.model:
                    continue

                # 1) Collect all nets that connect k1 and k2 (src/drn on either)
                shared_nets = [
                    (net.name, net.connected_transistors)
                    for net in self.circuit.nets.values()
                    if ((tran_1.name, "source") in net.connected_transistors or (tran_1.name, "drain") in net.connected_transistors)
                    and ((tran_2.name, "source") in net.connected_transistors or (tran_2.name, "drain") in net.connected_transistors)
                ]

                # 1a) If no shared net at all, forbid adjacency outright:
                if not shared_nets:
                    self.opt.Add(x_var_1 != x_var_2 + db_dist)
                    self.opt.Add(x_var_2 != x_var_1 + db_dist)
                    continue

                for shared_net in shared_nets:
                    net_name = shared_net[0]
                    self.net_ds_sharable_pairs.setdefault(net_name, []).append((tran_1.name, tran_2.name))

                # 2) One BoolVar "sel" per shared net; pick at most one
                selectors = []
                for net, _ in shared_nets:
                    sel = self.opt.NewBoolVar(f"sel_{tran_1.name}_{tran_2.name}_{net}")
                    selectors.append(sel)
                self.opt.Add(sum(selectors) <= 1)

                # 2a) If *none* is selected, forbid adjacency entirely:
                none_selected = [sel.Not() for sel in selectors]
                self.opt.Add(x_var_1 != x_var_2 + db_dist).OnlyEnforceIf(none_selected)
                self.opt.Add(x_var_2 != x_var_1 + db_dist).OnlyEnforceIf(none_selected)

                # 3) For each net, gate its adjacency+flip logic on sel==True
                for (net, conn), sel in zip(shared_nets, selectors):
                    keyL = f"ds_left_{tran_1.name}_{tran_2.name}_{net}"
                    keyR = f"ds_right_{tran_1.name}_{tran_2.name}_{net}"
                    adj_left = self.ds_pair_vars.get(keyL, self.opt.NewBoolVar(keyL))
                    adj_right = self.ds_pair_vars.get(keyR, self.opt.NewBoolVar(keyR))
                    self.ds_pair_vars[keyL] = adj_left
                    self.ds_pair_vars[keyR] = adj_right

                    # 3a) exactly one orientation if sel, none otherwise
                    self.opt.Add(adj_left + adj_right == 1).OnlyEnforceIf(sel)
                    self.opt.Add(adj_left == 0).OnlyEnforceIf(sel.Not())
                    self.opt.Add(adj_right == 0).OnlyEnforceIf(sel.Not())

                    # 3b) recover the four sharing-cases, all under sel:
                    # 3b.1) source-source sharing
                    if (tran_1.name, "source") in conn and (tran_2.name, "source") in conn:
                        self.opt.Add(x_var_1 + db_dist == x_var_2).OnlyEnforceIf([adj_left, sel])
                        self.opt.Add(x_var_1 + db_dist != x_var_2).OnlyEnforceIf([adj_left.Not(), sel])
                        self.opt.AddImplication(adj_left, flip_var_1).OnlyEnforceIf(sel)
                        self.opt.AddImplication(adj_left, flip_var_2.Not()).OnlyEnforceIf(sel)

                        self.opt.Add(x_var_1 == x_var_2 + db_dist).OnlyEnforceIf([adj_right, sel])
                        self.opt.Add(x_var_1 != x_var_2 + db_dist).OnlyEnforceIf([adj_right.Not(), sel])
                        self.opt.AddImplication(adj_right, flip_var_2).OnlyEnforceIf(sel)
                        self.opt.AddImplication(adj_right, flip_var_1.Not()).OnlyEnforceIf(sel)
                    # 3b.2) drain-drain sharing
                    elif (tran_1.name, "drain") in conn and (tran_2.name, "drain") in conn:
                        self.opt.Add(x_var_1 == x_var_2 + db_dist).OnlyEnforceIf([adj_right, sel])
                        self.opt.Add(x_var_1 != x_var_2 + db_dist).OnlyEnforceIf([adj_right.Not(), sel])
                        self.opt.AddImplication(adj_right, flip_var_1).OnlyEnforceIf(sel)
                        self.opt.AddImplication(adj_right, flip_var_2.Not()).OnlyEnforceIf(sel)

                        self.opt.Add(x_var_1 + db_dist == x_var_2).OnlyEnforceIf([adj_left, sel])
                        self.opt.Add(x_var_1 + db_dist != x_var_2).OnlyEnforceIf([adj_left.Not(), sel])
                        self.opt.AddImplication(adj_left, flip_var_2).OnlyEnforceIf(sel)
                        self.opt.AddImplication(adj_left, flip_var_1.Not()).OnlyEnforceIf(sel)
                    # 3b.3) source-drain sharing
                    elif (tran_1.name, "source") in conn and (tran_2.name, "drain") in conn:
                        self.opt.Add(x_var_1 + db_dist == x_var_2).OnlyEnforceIf([adj_left, sel])
                        self.opt.Add(x_var_1 + db_dist != x_var_2).OnlyEnforceIf([adj_left.Not(), sel])
                        self.opt.AddImplication(adj_left, flip_var_1).OnlyEnforceIf(sel)
                        self.opt.AddImplication(adj_left, flip_var_2).OnlyEnforceIf(sel)

                        self.opt.Add(x_var_1 == x_var_2 + db_dist).OnlyEnforceIf([adj_right, sel])
                        self.opt.Add(x_var_1 != x_var_2 + db_dist).OnlyEnforceIf([adj_right.Not(), sel])
                        self.opt.AddImplication(adj_right, flip_var_1.Not()).OnlyEnforceIf(sel)
                        self.opt.AddImplication(adj_right, flip_var_2.Not()).OnlyEnforceIf(sel)
                    # 3b.4) drain-source sharing
                    elif (tran_1.name, "drain") in conn and (tran_2.name, "source") in conn:
                        self.opt.Add(x_var_1 + db_dist == x_var_2).OnlyEnforceIf([adj_left, sel])
                        self.opt.Add(x_var_1 + db_dist != x_var_2).OnlyEnforceIf([adj_left.Not(), sel])
                        self.opt.AddImplication(adj_left, flip_var_1.Not()).OnlyEnforceIf(sel)
                        self.opt.AddImplication(adj_left, flip_var_2.Not()).OnlyEnforceIf(sel)

                        self.opt.Add(x_var_1 == x_var_2 + db_dist).OnlyEnforceIf([adj_right, sel])
                        self.opt.Add(x_var_1 != x_var_2 + db_dist).OnlyEnforceIf([adj_right.Not(), sel])
                        self.opt.AddImplication(adj_right, flip_var_1).OnlyEnforceIf(sel)
                        self.opt.AddImplication(adj_right, flip_var_2).OnlyEnforceIf(sel)
        logger.info(f"\t==\t{len(self.ds_pair_vars)} pairwise diffusion sharing variables created ...")

    def _pairwise_lisd_sharing(self):
        self.opt.log_comment("Enforcing pairwise lisd sharing...")
        logger.info("\t==\tEnforcing pairwise lisd sharing ...")
        tmp_tran = sorted(list(self.circuit.transistors.values()))
        self.lisd_share_pair_vars = {}
        for i, tran_1 in enumerate(tmp_tran):
            x_var_1 = self.transistor_vars[tran_1.name].x_var
            flip_var_1 = self.transistor_vars[tran_1.name].flip_var
            for tran_2 in tmp_tran[i + 1:]:
                x_var_2 = self.transistor_vars[tran_2.name].x_var
                flip_var_2 = self.transistor_vars[tran_2.name].flip_var
                # diff mos type
                if tran_1.model == tran_2.model:
                    continue

                # 1) gather all nets where k1,k2 share a source or drain
                shared_nets = [
                    (net.name, net.connected_transistors)
                    for net in self.circuit.get_nets(with_power_ground=False)
                    if ((tran_1.name, "source") in net.connected_transistors or (tran_1.name, "drain") in net.connected_transistors)
                    and ((tran_2.name, "source") in net.connected_transistors or (tran_2.name, "drain") in net.connected_transistors)
                ]

                # 1a) if no shared net, skip
                if not shared_nets:
                    continue

                # 2) one selector per net; pick at most one
                selectors = []
                for net, _ in shared_nets:
                    sel = self.opt.NewBoolVar(f"sel_{tran_1.name}_{tran_2.name}_{net}")
                    selectors.append(sel)
                self.opt.Add(sum(selectors) <= 1)

                # 3) for each net, gate its vertical alignment+flip under sel
                for (net, conn), sel in zip(shared_nets, selectors):
                    key = f"lisd_share_{tran_1.name}_{tran_2.name}_{net}"
                    lisd_var = self.lisd_share_pair_vars.get(
                        key,
                        self.opt.NewBoolVar(key),
                    )
                    self.lisd_share_pair_vars[key] = lisd_var

                    # 3a) force verti=1 when sel, verti=0 otherwise
                    self.opt.Add(lisd_var == 1).OnlyEnforceIf(sel)
                    self.opt.Add(lisd_var == 0).OnlyEnforceIf(sel.Not())

                    # 3b) geometry: same column iff verti & sel
                    self.opt.Add(x_var_1 == x_var_2).OnlyEnforceIf([lisd_var, sel])
                    self.opt.Add(x_var_1 != x_var_2).OnlyEnforceIf([lisd_var.Not(), sel])

                    # 3c) flip-relation under sel
                    if ((tran_1.name, "source") in conn and (tran_2.name, "source") in conn) or (
                        (tran_1.name, "drain") in conn and (tran_2.name, "drain") in conn
                    ):
                        self.opt.Add(flip_var_1 == flip_var_2).OnlyEnforceIf(sel)
                    else:
                        self.opt.Add(flip_var_1 != flip_var_2).OnlyEnforceIf(sel)
        logger.info(f"\t==\t{len(self.lisd_share_pair_vars)} pairwise lisd sharing variables created ...")

    def _pairwise_gate_sharing(self):
        self.opt.log_comment("Enforcing pairwise gate sharing...")
        logger.info("\t==\tEnforcing pairwise gate sharing ...")
        self.gate_share_pair_vars = {}
        tmp_tran = sorted(list(self.circuit.transistors.values()))
        self.net_gate_sharable_pairs = {}
        for i, tran_1 in enumerate(tmp_tran):
            x_var_1 = self.transistor_vars[tran_1.name].x_var
            for tran_2 in tmp_tran[i + 1:]:
                x_var_2 = self.transistor_vars[tran_2.name].x_var
                # diff mos type
                if tran_1.model == tran_2.model:
                    continue
                shared_any_diffusion = False
                for net in self.circuit.get_nets(with_power_ground=False):
                    conn = net.connected_transistors
                    if (tran_1.name, "gate") in conn and (tran_2.name, "gate") in conn:
                        self.net_gate_sharable_pairs.setdefault(net.name, []).append((tran_1.name, tran_2.name))
                        shared_any_diffusion = True
                        key = f"gate_share_{tran_1.name}_{tran_2.name}_{net.name}"
                        gate_var = self.gate_share_pair_vars.get(
                            key,
                            self.opt.NewBoolVar(key),
                        )
                        self.opt.Add(x_var_1 == x_var_2).OnlyEnforceIf(gate_var)
                        self.opt.Add(x_var_1 != x_var_2).OnlyEnforceIf(gate_var.Not())
                        self.gate_share_pair_vars[key] = gate_var
                if not shared_any_diffusion:
                    pass
        logger.info(f"\t==\t{len(self.gate_share_pair_vars)} pairwise gate sharing variables created ...")

    # ================================================================== #
    # routing constraints (CFET-specific)                               #
    # ================================================================== #

    def _routing_constraints(self):
        # ^ prohibit routing to touch left/right cell boundaries
        logger.info("Prohibiting routing to left cell boundaries ...")
        self._prohibit_routing_to_left_cell_boundaries()
        self._prohibit_routing_to_right_cell_boundaries()

        # ^ enforce that gate cut is continuous and is at least X CPP long
        self.gate_share_at_col_vars = OrderedDict()  # ci -> gate_share_vars
        self._bind_gate_sharing_to_columns()

        self.opt.log_comment("Defining gate cut windows ...")
        self.min_gate_cut_len = 1  # TODO make it a parameter
        self.gate_cut_window_vars = {}
        self._gate_cut_window()

        # ^ If a db is placed at col, then no net arc and no net edge can use the immediate right gate col
        self._prohibit_pc_routing_in_diffusion_break_cols()

        # ^ At most one long via (BPC to M0) per column
        self._only_one_long_via_per_col()

        # ^ At most one MIV (BPC to PC) per column
        self._only_one_miv_per_col()

        # ^ Bind LISD sharing to columns and limit CA contacts
        self.lisd_share_at_col_vars = OrderedDict()
        self._bind_lisd_sharing_to_columns()
        self._limit_lisd_contact(num_contact=1)

        # ^ Limit gate CA contacts on top placement layer
        self._limit_gate_contact(num_contact=1)

        # ^ Variables for Routing Window Constraint
        self.opt.log_comment("Routing Window Constraint ...")
        self.s_coord_x = {}
        self.s_coord_y = {}
        self.t_coord_x = {}
        self.t_coord_y = {}
        self.net_min_x = {}
        self.net_max_x = {}
        self.net_min_y = {}
        self.net_max_y = {}
        self.window_xmin_raw = {}
        self.window_xmax_raw = {}
        self.window_ymin_raw = {}
        self.window_ymax_raw = {}
        # -1 => Free for all routing; 0 => No tolerance (dangerous); > 0 => preferred
        rt_cfg = self.cell_config.get("routing_tolerance", {}) or {}
        if rt_cfg.get("value") is True:
            self.routing_tolerance = int(rt_cfg.get("tol", 1) * self.c_tech.get_pitch("PC"))
        else:
            self.routing_tolerance = -1
        rt.routing_localization_cfet(self)

        # M0ICPD power rows are top-view routing rows, not CFET device tiers.
        # Power rails are drawn by GDS, so signal routing must not consume them.
        self._ban_signal_on_power_rows()

        # ^ Linking flow variables to arc usage
        self.opt.log_comment("Linking flow variables to arc usage ...")
        self._link_flow_to_arc()
        self._link_arc_to_edge()

        #  ^ Net unique edge constraint
        self._net_has_one_src_and_k_terminals()
        # ^ A node cannot be a source for more than one net.
        self._net_src_node_uniqueness()
        # ^ A node cannot be a terminal for more than one net.
        self._net_term_node_uniqueness()
        # ^ an SON terminal cannot be a terminal for more than one net.
        self._net_SON_node_uniqueness()
        # ^ an SON cannot be aligned at the same column
        self._prohibit_multiple_SONs_same_column()

        # ^ Directed flow-conservation per net, per terminal (ignore diffusion shared)
        self._induce_internal_routing_flow_with_diffusion()
        # ^ Route to I/O pins
        self._induce_external_routing_flow()
        # ^ CFET-specific: cross-device flows must use at least 1 cross-layer arc
        rt.cfet_cross_device_via_lower_bound(self)
        # ^ optional: tighten the HPWL lower bound with mandatory cross-device via cost
        if self._cfg_get("cfet_hpwl_via_tightening", False):
            rt.cfet_hpwl_via_cost_tightening(self)
        # ^ A node cannot be propagated flow for more than one net.
        self._node_exclusivity()

        # ^ Geometric variables for design rule checking
        self.opt.log_comment("Adding geometric variables...")
        self.geometric_vars = {}
        rule.geometric_vars_in_horizontal_layers(self)
        rule.geometric_vars_in_vertical_layers(self)

        # ^ EOL Design Rule Checking (C2C)
        eol_params = self.cell_config["eol_c2c_rule"]["value"]
        rule.eol_rules_in_horizontal_layers(self, eol_params)
        rule.eol_rules_in_vertical_layers(self, eol_params)

        # ^ MAR Design Rule Checking (C2C)
        mar_params = self.cell_config["mar_c2c_rule"]["value"]
        supervia_params = {layer: False for layer in self.lgg.layer_to_idx.keys()}
        for layer in self.cell_config["supervia"]["value"]:
            if layer in supervia_params:
                supervia_params[layer] = True
        rule.mar_rules_in_horizontal_layers(self, mar_params, supervia_params)
        rule.mar_rules_in_vertical_layers(self, mar_params, supervia_params)

        # ^ Via to metal connection rule
        rule.via_induce_vertical_metal(self, supervia_params)
        rule.via_induce_horizontal_metal(self, supervia_params)
        rule.vertical_metal_must_be_connected_to_via(self)
        rule.horizontal_metal_must_be_connected_to_via(self)

        # ^ Via distance rule
        via_params = {
            tuple(k.strip() for k in key.split(",")): value
            for key, value in self.cell_config["via_c2c_rule"]["value"].items()
        }
        rule.via_separation_rules(self, via_params)

        # ^ Pin Accessibility Rule
        self.opt.log_comment("Binding net usage on top layer ...")
        top_layer = "M2"
        self.net_use_top_track = {}  # netname -> bool var
        self.net_use_top_track_row_var = {}  # netname -> list of tracks row var
        pin.top_layer_net_usage(self, top_layer)

        # ^ Each net uses one M2 track at most & each M2 track used by one net at most
        if self.cell_config.get("limit_m2_usage", {}).get("value", False):
            pin.one_top_layer_track_per_net(self, top_layer)
            pin.one_net_per_top_layer_track(self, top_layer)

        # ^ For each M1 pin, ensure at least MPO entry point usable for routing on M2
        self.M1_MPO = self.cell_config["MPO"]["value"]
        if self.M1_MPO > 0:
            pin.m1_minimum_pin_opening(self, top_layer, mar_params, eol_params)

        # ^ M0 pin: at least MPO entry point (besides its M1 SON) usable for routing
        pin.m0_pin(self)

        # ^ M0 pins separated across different rows
        if self.cell_config["m0_pin_separation"]["value"]:
            pin.m0_pin_separation(self)

        # ^ Extend M0 pin to vacancy edges
        if self.cell_config["m0_pin_extension"]["value"]:
            pin.m0_pin_extension(self, vacancy_edges=self.cell_config["m0_pin_extension"]["vacancy_edges"])

    def _prohibit_routing_to_left_cell_boundaries(self):
        """The leftmost gate column (col index 0) should not be used for routing."""
        self.opt.log_comment("Prohibiting routing to left cell boundaries ...")
        logger.info("\t==\tProhibiting routing to left cell boundaries ...")
        left_bound_col = 0
        # Banning edges is sufficient: edge=0 -> arc=0 -> flow=0
        gathered_edge_vars = []
        for u_edge, v_edge in self.lgg.edges():
            if u_edge[2] == left_bound_col or v_edge[2] == left_bound_col:
                gathered_edge_vars.append(self.edge_vars[(u_edge, v_edge)])
        self.opt.Add(sum(gathered_edge_vars) == 0)

    def _ban_signal_on_power_rows(self):
        """For M0ICPD, forbid signal routes on top-view power rows."""
        if self.c_tech.power_config != "M0ICPD":
            return

        if not self.power_row_indices:
            raise ValueError("M0ICPD requires power_row_indices for top-view power rows")

        power_row_indices = sorted(self.power_row_indices.keys())
        m0_row_count = self.lgg.num_rows_in_layer("M0")
        invalid_m0_rows = [ri for ri in power_row_indices if not (0 <= ri < m0_row_count)]
        if invalid_m0_rows:
            raise ValueError(
                f"M0ICPD power row indices {invalid_m0_rows} out of range for "
                f"M0 rows 0..{m0_row_count - 1}"
            )

        power_row_coords_by_layer = {}
        for layer_name in ("M0", "PC", "BPC"):
            row_count = self.lgg.num_rows_in_layer(layer_name)
            invalid_rows = [ri for ri in power_row_indices if not (0 <= ri < row_count)]
            if invalid_rows:
                raise ValueError(
                    f"M0ICPD power row indices {invalid_rows} out of range for "
                    f"{layer_name} top-view rows 0..{row_count - 1}; PC/BPC/M0 "
                    "must share row indices"
                )
            layer_idx = self.lgg.layer_index(layer_name)
            power_row_coords_by_layer[layer_idx] = {
                self.lgg.row_in_layer(layer_name, ri) for ri in power_row_indices
            }

        banned_edges = OrderedDict()
        empty_forbidden_rows = set()
        for edge, edge_var in self.edge_vars.items():
            u_edge, v_edge = edge
            u_forbidden_rows = power_row_coords_by_layer.get(u_edge[0], empty_forbidden_rows)
            v_forbidden_rows = power_row_coords_by_layer.get(v_edge[0], empty_forbidden_rows)
            if u_edge[1] in u_forbidden_rows or v_edge[1] in v_forbidden_rows:
                banned_edges[edge] = edge_var

        logger.info(
            f"\t==\t[M0ICPD] Banning signal routing on top-view power rows "
            f"{power_row_indices}: {len(banned_edges)} edge(s)"
        )
        self.opt.log_comment("Banning signal routing on M0ICPD top-view power rows ...")
        for edge_var in banned_edges.values():
            self.opt.Add(edge_var == 0)

    def _prohibit_routing_to_right_cell_boundaries(self):
        """For each possible cpp value, any column beyond the rightmost S/D column is unusable."""
        self.opt.log_comment("Prohibiting routing to right cell boundaries ...")
        logger.info("\t==\tProhibiting routing to right cell boundaries ...")
        for possible_cpp in self.plc_ci:
            right_bound_col = (possible_cpp + (_NUM_COL_SDG_ - 1)) * math.ceil(self.c_tech.get_pitch("PC"))
            self.opt.log_comment(f"Prohibiting right bound {right_bound_col} at possible_cpp {possible_cpp}...")
            gathered_edge_vars = []
            for u_edge, v_edge in self.lgg.edges():
                if u_edge[2] > right_bound_col or v_edge[2] > right_bound_col:
                    gathered_edge_vars.append(self.edge_vars[(u_edge, v_edge)])
            cpp_bool = self.opt.NewBoolVar(f"cpp_is_{possible_cpp}")
            self.opt.Add(self.cpp_cost == possible_cpp).OnlyEnforceIf(cpp_bool)
            self.opt.Add(self.cpp_cost != possible_cpp).OnlyEnforceIf(cpp_bool.Not())
            self.opt.Add(sum(gathered_edge_vars) == 0).OnlyEnforceIf(cpp_bool)

    def _bind_gate_sharing_to_columns(self):
        self.opt.log_comment("Binding gate sharing at column ...")
        for ci in self.plc_ci:
            col_r = self.lgg.col_in_layer("PC", ci + 1)
            self.gate_share_at_col_vars[col_r] = self.opt.NewBoolVar(f"gate_share_at_col_{col_r}")
            gate_share = self.gate_share_at_col_vars[col_r]

            # 1) Gather ALL the "placed-transistor-at-ci" vars
            placed_here = [self.placed_tran_ci_vars[(tran, ci)] for tran in self.transistor_vars.keys()]

            # 2) Make has_tran_at_ci == True  <=>  OR(placed_here)
            has_tran = self.opt.NewBoolVar(f"has_tran_at_ci_{ci}")
            self.opt.AddBoolOr(placed_here).OnlyEnforceIf(has_tran)
            self.opt.Add(sum(placed_here) == 0).OnlyEnforceIf(has_tran.Not())

            # 3) Build per-pair "tran_gate_share_at_col" list
            tmp_gate_share_vars_at_col = []
            for key, gs_var in self.gate_share_pair_vars.items():
                m = re.match(r"gate_share_(M\w+)_(M\w+)_(\w+)", key)
                if not m:
                    continue
                t1, t2, net = m.group(1), m.group(2), m.group(3)
                p1 = self.placed_tran_ci_vars[(t1, ci)]
                p2 = self.placed_tran_ci_vars[(t2, ci)]
                tv = self.opt.NewBoolVar(f"tran_gate_share_at_col_{t1}_{t2}_{net}_{col_r}")
                self.opt.Add(tv == 1).OnlyEnforceIf([gs_var, p1, p2])
                self.opt.Add(gs_var == 1).OnlyEnforceIf(tv)
                self.opt.Add(p1 == 1).OnlyEnforceIf(tv)
                self.opt.Add(p2 == 1).OnlyEnforceIf(tv)
                tmp_gate_share_vars_at_col.append(tv)

            # 4) gate_share true <=> (some tv true) OR (no transistor at ci)
            self.opt.AddBoolOr(tmp_gate_share_vars_at_col + [has_tran.Not()]).OnlyEnforceIf(gate_share)
            for tv in tmp_gate_share_vars_at_col:
                self.opt.AddImplication(tv, gate_share)
            self.opt.AddImplication(has_tran.Not(), gate_share)

            # 5) gate_share false => no tv true AND at least one transistor placed
            self.opt.Add(sum(tmp_gate_share_vars_at_col) == 0).OnlyEnforceIf(gate_share.Not())
            self.opt.AddBoolOr(placed_here).OnlyEnforceIf(gate_share.Not())
        # ^ always let the first gate col be shared
        self.opt.log_comment("Allowing gate sharing at the first column ...")
        self.opt.Add(self.gate_share_at_col_vars[self.lgg.col_in_layer("PC", self.plc_ci[0] + 1)] == 1)

    def _gate_cut_window(self):
        self.opt.log_comment("Defining gate cut windows ...")
        gate_cut_windows = self._sliding_windows(list(self.gate_share_at_col_vars.keys()), self.min_gate_cut_len)
        self.gate_cut_window_vars = {windows: self.opt.NewBoolVar(f"gate_cut_window_{windows}") for windows in gate_cut_windows}
        # ^ Boundary condition for gate cut and diffusion break
        self.opt.log_comment("Enforcing gate cut boundary condition and diffusion break ...")
        logger.info(f"\t==\tEnforcing gate cut boundary condition to {self.min_boundary_col} and {self.max_boundary_col} ...")
        for gcw in gate_cut_windows:
            can_be_oob = False
            for gc in gcw:
                if gc > self.min_boundary_col:
                    can_be_oob = True
                    break
            if can_be_oob:
                max_col_in_gcw = max(gcw)
                max_ci_in_gcw = self.lgg.col_index_in_layer("PC", max_col_in_gcw)
                plc_ci_in_gcw = max_ci_in_gcw - 1
                self.opt.Add(self.cpp_cost >= plc_ci_in_gcw).OnlyEnforceIf(self.gate_cut_window_vars[gcw])
        # ^ Binding gate cut windows to gate cut
        self.opt.log_comment("Binding gate cut windows to gate cut ...")
        for gcw in gate_cut_windows:
            gs_vars = [self.gate_share_at_col_vars[col] for col in gcw]
            gs_vars_negated = [var.Not() for var in gs_vars]
            gcw_var = self.gate_cut_window_vars[gcw]
            self.opt.Add(gcw_var == 1).OnlyEnforceIf(gs_vars_negated)
            for gs_var in gs_vars:
                self.opt.Add(gcw_var == 0).OnlyEnforceIf(gs_var)

        # ^ enforce that gate cut is continuous and is at least X CPP long
        self.opt.log_comment("Enforcing gate cut continuity ...")
        for gcol in self.gate_share_at_col_vars.keys():
            possible_gate_cuts = []
            for gcw in gate_cut_windows:
                if gcol in gcw:
                    possible_gate_cuts.append(self.gate_cut_window_vars[gcw])
            self.opt.Add(sum(possible_gate_cuts) == 1).OnlyEnforceIf(self.gate_share_at_col_vars[gcol].Not())

    def _prohibit_pc_routing_in_diffusion_break_cols(self):
        self.opt.log_comment("Enforcing if a db is placed at col, then no net arc and no net edge can use the immediate right gate col ...")
        pmos_layer_idx = self.lgg.layer_index(self.pmos_layer)
        nmos_layer_idx = self.lgg.layer_index(self.nmos_layer)
        # NOTE diffusion break should only be inserted in plc columns
        for ci in self.plc_ci:
            pdb_var = self.db_pmos_cols_vars[ci]
            ndb_var = self.db_nmos_cols_vars[ci]
            try:
                cr = self.lgg.col_in_layer("PC", ci + 1)
            except IndexError:
                continue
            # PMOS
            gathered_pmos_edge_vars = []
            gathered_pmos_net_arc_vars = []
            gathered_pmos_net_flow_vars = []
            pmos_row = []
            for ri in self.pmos_pin_access_ri:
                pmos_row.append(self.lgg.row_in_layer(self.pmos_layer, ri))
            for u_edge, v_edge in self.lgg.edges():
                if u_edge[0] == pmos_layer_idx and u_edge[1] in pmos_row and u_edge[2] == cr:
                    gathered_pmos_edge_vars.append(self.edge_vars[(u_edge, v_edge)])
                elif v_edge[0] == pmos_layer_idx and v_edge[1] in pmos_row and v_edge[2] == cr:
                    gathered_pmos_edge_vars.append(self.edge_vars[(u_edge, v_edge)])
            for net in self.circuit.get_nets(with_power_ground=False):
                for u_arc, v_arc in self.lgg.arcs():
                    if u_arc[0] == pmos_layer_idx and u_arc[1] in pmos_row and u_arc[2] == cr:
                        gathered_pmos_net_arc_vars.append(self.net_arc_vars[(net.name, u_arc, v_arc)])
                    elif v_arc[0] == pmos_layer_idx and v_arc[1] in pmos_row and v_arc[2] == cr:
                        gathered_pmos_net_arc_vars.append(self.net_arc_vars[(net.name, u_arc, v_arc)])
            for net in self.circuit.get_nets(with_power_ground=False):
                for k in range(net.num_terminals()):
                    for u_arc, v_arc in self.lgg.arcs():
                        if u_arc[0] == pmos_layer_idx and u_arc[1] in pmos_row and u_arc[2] == cr:
                            gathered_pmos_net_flow_vars.append(self.net_flow_vars[(net.name, k, u_arc, v_arc)])
                        elif v_arc[0] == pmos_layer_idx and v_arc[1] in pmos_row and v_arc[2] == cr:
                            gathered_pmos_net_flow_vars.append(self.net_flow_vars[(net.name, k, u_arc, v_arc)])

            self.opt.Add(sum(gathered_pmos_edge_vars) == 0).OnlyEnforceIf(pdb_var)
            self.opt.Add(sum(gathered_pmos_net_arc_vars) == 0).OnlyEnforceIf(pdb_var)
            self.opt.Add(sum(gathered_pmos_net_flow_vars) == 0).OnlyEnforceIf(pdb_var)
            # NMOS
            gathered_nmos_edge_vars = []
            gathered_nmos_net_arc_vars = []
            gathered_nmos_net_flow_vars = []
            nmos_row = []
            for ri in self.nmos_pin_access_ri:
                nmos_row.append(self.lgg.row_in_layer(self.nmos_layer, ri))
            for u_edge, v_edge in self.lgg.edges():
                if u_edge[0] == nmos_layer_idx and u_edge[1] in nmos_row and u_edge[2] == cr:
                    gathered_nmos_edge_vars.append(self.edge_vars[(u_edge, v_edge)])
                elif v_edge[0] == nmos_layer_idx and v_edge[1] in nmos_row and v_edge[2] == cr:
                    gathered_nmos_edge_vars.append(self.edge_vars[(u_edge, v_edge)])
            for net in self.circuit.get_nets(with_power_ground=False):
                for u_arc, v_arc in self.lgg.arcs():
                    if u_arc[0] == nmos_layer_idx and u_arc[1] in nmos_row and u_arc[2] == cr:
                        gathered_nmos_net_arc_vars.append(self.net_arc_vars[(net.name, u_arc, v_arc)])
                    elif v_arc[0] == nmos_layer_idx and v_arc[1] in nmos_row and v_arc[2] == cr:
                        gathered_nmos_net_arc_vars.append(self.net_arc_vars[(net.name, u_arc, v_arc)])
            for net in self.circuit.get_nets(with_power_ground=False):
                for k in range(net.num_terminals()):
                    for u_arc, v_arc in self.lgg.arcs():
                        if u_arc[0] == nmos_layer_idx and u_arc[1] in nmos_row and u_arc[2] == cr:
                            gathered_nmos_net_flow_vars.append(self.net_flow_vars[(net.name, k, u_arc, v_arc)])
                        elif v_arc[0] == nmos_layer_idx and v_arc[1] in nmos_row and v_arc[2] == cr:
                            gathered_nmos_net_flow_vars.append(self.net_flow_vars[(net.name, k, u_arc, v_arc)])
            self.opt.Add(sum(gathered_nmos_edge_vars) == 0).OnlyEnforceIf(ndb_var)
            self.opt.Add(sum(gathered_nmos_net_arc_vars) == 0).OnlyEnforceIf(ndb_var)
            self.opt.Add(sum(gathered_nmos_net_flow_vars) == 0).OnlyEnforceIf(ndb_var)

    def _only_one_long_via_per_col(self):
        self.opt.log_comment("At most one long via (BPC to M0) per column")
        for col, edge_pairs in self.lgg.virtual_edges_along_col.items():
            edge_vars_at_col = []
            for (u, v) in edge_pairs:
                if (u, v) in self.edge_vars:
                    edge_vars_at_col.append(self.edge_vars[(u, v)])
                elif (v, u) in self.edge_vars:
                    edge_vars_at_col.append(self.edge_vars[(v, u)])
            if len(edge_vars_at_col) > 0:
                self.opt.Add(sum(edge_vars_at_col) <= 1)

    def _only_one_miv_per_col(self):
        self.opt.log_comment("At most one MIV (BPC to PC) per column")
        bpc_layer_idx = self.lgg.layer_index("BPC")
        pc_layer_idx = self.lgg.layer_index("PC")
        miv_edges_by_col = defaultdict(list)
        for (u, v), evar in self.edge_vars.items():
            # cross-layer edge between BPC and PC
            if {u[0], v[0]} == {bpc_layer_idx, pc_layer_idx}:
                # col is the same for both endpoints on a via edge
                miv_edges_by_col[u[2]].append(evar)
        for col, evars in miv_edges_by_col.items():
            if len(evars) > 0:
                self.opt.Add(sum(evars) <= 1)

    def _bind_lisd_sharing_to_columns(self):
        """Per-column booleans tracking whether LISD sharing is active at an S/D column."""
        self.opt.log_comment("Binding LISD sharing at column ...")
        self.lisd_share_at_col_vars = OrderedDict()

        # Create sharing variables for all S/D columns
        all_sd_cols = set()
        for ci in self.plc_ci:
            all_sd_cols.add(self.lgg.col_in_layer("PC", ci))
            all_sd_cols.add(self.lgg.col_in_layer("PC", ci + 2))
        for col in all_sd_cols:
            self.lisd_share_at_col_vars[col] = self.opt.NewBoolVar(f"lisd_share_at_col_{col}")

        for ci in self.plc_ci:
            col = self.lgg.col_in_layer("PC", ci)
            col_rr = self.lgg.col_in_layer("PC", ci + 2)
            lisd_share_col = self.lisd_share_at_col_vars[col]
            lisd_share_col_rr = self.lisd_share_at_col_vars[col_rr]

            lisd_vars_at_col = []
            lisd_vars_at_col_rr = []
            for tran in self.circuit.transistors.values():
                tran_placed = self.placed_tran_ci_vars.get((tran.name, ci))
                if tran_placed is None:
                    continue
                for key, lisd_var in self.lisd_share_pair_vars.items():
                    if tran.name in key:
                        tmp_and = self.opt.NewBoolVar(f"lisd_active_{key}_ci{ci}")
                        self.opt.AddBoolAnd([tran_placed, lisd_var]).OnlyEnforceIf(tmp_and)
                        self.opt.AddBoolOr([tran_placed.Not(), lisd_var.Not()]).OnlyEnforceIf(tmp_and.Not())
                        lisd_vars_at_col.append(tmp_and)
                        lisd_vars_at_col_rr.append(tmp_and)

            if lisd_vars_at_col:
                self.opt.AddBoolOr(lisd_vars_at_col).OnlyEnforceIf(lisd_share_col)
                self.opt.Add(sum(lisd_vars_at_col) == 0).OnlyEnforceIf(lisd_share_col.Not())
            else:
                self.opt.Add(lisd_share_col == 0)
            if lisd_vars_at_col_rr:
                self.opt.AddBoolOr(lisd_vars_at_col_rr).OnlyEnforceIf(lisd_share_col_rr)
                self.opt.Add(sum(lisd_vars_at_col_rr) == 0).OnlyEnforceIf(lisd_share_col_rr.Not())
            else:
                self.opt.Add(lisd_share_col_rr == 0)

    def gather_all_ca_via_vars_at_col(self, col):
        """
        Gather ALL CA via edge vars (top_placement_layer <-> M0) at a given column,
        across ALL rows - routing can place CAs at any row where PC and M0 overlap.
        """
        top_layer = self.c_tech.get_top_placement_layer()
        top_layer_idx = self.lgg.layer_index(top_layer)
        m0_layer_idx = self.lgg.layer_index("M0")
        gathered = []
        for (u, v), evar in self.edge_vars.items():
            if {u[0], v[0]} == {top_layer_idx, m0_layer_idx}:
                if u[2] == col or v[2] == col:
                    gathered.append(evar)
        return gathered

    def _limit_lisd_contact(self, num_contact=1):
        """Limit CA via contacts at LISD-shared S/D columns (all rows)."""
        self.opt.log_comment(f"Limiting LISD contact to {num_contact} ...")
        for ci in self.sd_ci:
            col = self.lgg.col_in_layer("PC", ci)
            if col not in self.lisd_share_at_col_vars:
                continue
            lisd_var = self.lisd_share_at_col_vars[col]
            ca_via_vars = self.gather_all_ca_via_vars_at_col(col)
            if ca_via_vars:
                self.opt.Add(sum(ca_via_vars) <= num_contact).OnlyEnforceIf(lisd_var)
                self.opt.Add(sum(ca_via_vars) <= num_contact).OnlyEnforceIf(lisd_var.Not())

    def _limit_gate_contact(self, num_contact=1):
        """Limit CA via contacts at gate-shared columns (all rows)."""
        self.opt.log_comment(f"Limiting gate contact to {num_contact} ...")
        for ci in self.plc_ci:
            col_r = self.lgg.col_in_layer("PC", ci + 1)  # gate column
            if col_r not in self.gate_share_at_col_vars:
                continue
            gs_var = self.gate_share_at_col_vars[col_r]
            ca_via_vars = self.gather_all_ca_via_vars_at_col(col_r)
            if ca_via_vars:
                self.opt.Add(sum(ca_via_vars) <= num_contact).OnlyEnforceIf(gs_var)
                self.opt.Add(sum(ca_via_vars) <= num_contact).OnlyEnforceIf(gs_var.Not())

    def _link_flow_to_arc(self):
        self.opt.log_comment("Linking flow variables to arc usage ...")
        # NOTE: flow is the minimum route; arc sits on top of flow and can extend.
        for net in self.circuit.get_nets(with_power_ground=False):
            for u_arc, v_arc in self.lgg.arcs():
                for k in range(self.net_to_flow_cnt[net.name]):
                    self.opt.AddImplication(
                        self.net_flow_vars[(net.name, k, u_arc, v_arc)],
                        self.net_arc_vars[(net.name, u_arc, v_arc)],
                    )

    def _link_arc_to_edge(self):
        for u, v in self.lgg.edges():
            # net arc cannot go in both directions
            self.opt.Add(
                sum(
                    self.net_arc_vars[(net.name, u, v)] + self.net_arc_vars[(net.name, v, u)]
                    for net in self.circuit.get_nets(with_power_ground=False)
                    if (net.name, u, v) in self.net_arc_vars and (net.name, v, u) in self.net_arc_vars
                )
                <= 1
            )
            # Link edge usage to net arc usage
            conditions_for_edge_usage = []
            for net in self.circuit.get_nets(with_power_ground=False):
                if (net.name, u, v) in self.net_arc_vars and (net.name, v, u) in self.net_arc_vars:
                    conditions_for_edge_usage.append(self.net_arc_vars[(net.name, u, v)])
                    conditions_for_edge_usage.append(self.net_arc_vars[(net.name, v, u)])
            self.opt.AddBoolOr(conditions_for_edge_usage).OnlyEnforceIf(self.edge_vars[(u, v)])
            self.opt.Add(sum(conditions_for_edge_usage) == 0).OnlyEnforceIf(self.edge_vars[(u, v)].Not())

        # also forbid flow from using the same edge in both directions
        for net in self.circuit.get_nets(with_power_ground=False):
            for k in range(self.net_to_flow_cnt[net.name]):
                for u, v in self.lgg.edges():
                    self.opt.Add(self.net_flow_vars[(net.name, k, u, v)] + self.net_flow_vars[(net.name, k, v, u)] <= 1)

    def _net_has_one_src_and_k_terminals(self):
        self.opt.log_comment("Enforcing net unique edge constraint ...")
        for net in self.circuit.get_nets(with_power_ground=False):
            source_candidates_for_net = [self.node_is_src_vars[net.name][node] for node in self.node_is_src_vars[net.name]]
            if source_candidates_for_net:
                self.opt.Add(sum(source_candidates_for_net) == 1)
            else:
                logger.error(f"Net {net.name} has no potential source locations defined.")

            for k in range(net.num_terminals()):
                kth_terminal_candidates = [self.node_is_term_vars[net.name][k][node] for node in self.node_is_term_vars[net.name][k].keys()]
                if kth_terminal_candidates:
                    self.opt.Add(sum(kth_terminal_candidates) == 1)
                else:
                    logger.error(f"Net {net.name}, terminal {k} has no potential locations defined.")

    def _net_src_node_uniqueness(self):
        self.opt.log_comment("Enforcing a node cannot be a source for more than one net ...")
        for node in self.lgg.nodes():
            tmp_node_is_src_vars = []
            for net in self.circuit.get_nets(with_power_ground=False):
                if node in self.node_is_src_vars[net.name]:
                    tmp_node_is_src_vars.append(self.node_is_src_vars[net.name][node])
            if len(tmp_node_is_src_vars) > 0:
                self.opt.Add(sum(tmp_node_is_src_vars) <= 1)

    def _net_term_node_uniqueness(self):
        self.opt.log_comment("Enforcing a node cannot be a terminal for more than one net ...")
        # multiple terminals of the SAME net can share a node; different nets cannot.
        for node in self.lgg.nodes():
            tmp_node_is_term_vars = []
            for net in self.circuit.get_nets(with_power_ground=False):
                tmp_term_placed_var = self.opt.NewBoolVar(f"{net.name}_isterm_placed_at_{node}")
                tmp_node_is_term_vars.append(tmp_term_placed_var)
                for k in range(net.num_terminals()):
                    if node in self.node_is_term_vars[net.name][k]:
                        self.opt.AddImplication(self.node_is_term_vars[net.name][k][node], tmp_term_placed_var)
            if len(tmp_node_is_term_vars) > 0:
                self.opt.Add(sum(tmp_node_is_term_vars) <= 1)

    def _net_SON_node_uniqueness(self):
        self.opt.log_comment("Enforcing an SON terminal cannot be a terminal for more than one net ...")
        for node in self.son_terminal_nodes["M1"]:
            tmp_SON_vars_for_nets = []
            for net in self.circuit.get_nets(with_power_ground=False):
                if not net.is_io_net():
                    continue
                for k in range(net.num_terminals(), self.net_to_flow_cnt[net.name], 1):
                    tmp_SON_vars_for_nets.append(self.node_is_SON_vars[net.name][k][node])
            self.opt.Add(sum(tmp_SON_vars_for_nets) <= 1)

    def _prohibit_multiple_SONs_same_column(self):
        self.opt.log_comment("Enforcing an SON cannot be aligned at the same column ...")
        tmp_SON_vars_for_at_col = {}
        for col in self.lgg.cols_in_layer("M1"):
            tmp_SON_vars_for_at_col[col] = []
            for node in self.son_terminal_nodes["M1"]:
                for net in self.circuit.get_nets(with_power_ground=False):
                    if not net.is_io_net():
                        continue
                    for k in range(net.num_terminals(), self.net_to_flow_cnt[net.name], 1):
                        if node[2] == col:
                            tmp_SON_vars_for_at_col[col].append(self.node_is_SON_vars[net.name][k][node])
        for col in self.lgg.cols_in_layer("M1"):
            self.opt.Add(sum(tmp_SON_vars_for_at_col[col]) <= 1)

    def _induce_internal_routing_flow_with_diffusion(self):
        self.opt.log_comment("Simplified routing: Enforcing directed flow-conservation per net, per terminal ...")
        self.net_terminal_is_shared = {}  # (net_name, k) -> is_shared BoolVar
        for net in self.circuit.get_nets(with_power_ground=False):
            src_tran_name, src_pin = net.source()
            for k, (term_tran_name, term_pin) in enumerate(net.terminals()):
                k_shareable_vars = []
                # In CFET, only same-tier (diffusion) sharing can zero the flow.
                # Cross-tier sharing (LISD, gate) requires explicit MIV routing
                # because BPC and PC are physically separate layers.
                tmp_ds_share_vars = self.gather_ds_shareable_vars(net.name, src_tran_name, term_tran_name, src_pin, term_pin)
                k_shareable_vars.extend(tmp_ds_share_vars)
                for k_prev in range(k):
                    prev_k_term_tran_name, prev_k_pin = net.terminals()[k_prev]
                    tmp_ds_share_vars = self.gather_ds_shareable_vars(net.name, prev_k_term_tran_name, term_tran_name, prev_k_pin, term_pin)
                    k_shareable_vars.extend(tmp_ds_share_vars)
                is_shared = self.opt.NewBoolVar(f"shared_{net.name}_{k}")
                self.net_terminal_is_shared[(net.name, k)] = is_shared
                self.opt.AddBoolOr(k_shareable_vars).OnlyEnforceIf(is_shared)
                self.opt.Add(sum(k_shareable_vars) == 0).OnlyEnforceIf(is_shared.Not())
                for node in self.lgg.nodes():
                    in_flows = sum(
                        self.net_flow_vars[(net.name, k, u_arc, v_arc)]
                        for u_arc, v_arc in self.adj_in.get(node, [])
                        if (net.name, k, u_arc, v_arc) in self.net_flow_vars
                    )
                    out_flows = sum(
                        self.net_flow_vars[(net.name, k, u_arc, v_arc)]
                        for u_arc, v_arc in self.adj_out.get(node, [])
                        if (net.name, k, u_arc, v_arc) in self.net_flow_vars
                    )

                    can_be_src = self.node_is_src_vars[net.name].get(node)
                    if can_be_src is not None:
                        self.opt.Add(out_flows - in_flows == 1).OnlyEnforceIf([can_be_src, is_shared.Not()])

                    can_be_kth_terminal = self.node_is_term_vars[net.name][k].get(node)
                    if can_be_kth_terminal is not None:
                        self.opt.Add(in_flows - out_flows == 1).OnlyEnforceIf([can_be_kth_terminal, is_shared.Not()])

                    conditions_for_intermediate = []
                    if can_be_src is not None:
                        conditions_for_intermediate.append(can_be_src.Not())
                    if can_be_kth_terminal is not None:
                        conditions_for_intermediate.append(can_be_kth_terminal.Not())
                    conditions_for_intermediate += [is_shared.Not()]
                    if not conditions_for_intermediate:
                        self.opt.Add(in_flows == out_flows).OnlyEnforceIf(is_shared.Not())
                    else:
                        self.opt.Add(in_flows == out_flows).OnlyEnforceIf(conditions_for_intermediate)
                    self.opt.Add(in_flows <= 1).OnlyEnforceIf(is_shared.Not())
                    self.opt.Add(out_flows <= 1).OnlyEnforceIf(is_shared.Not())
                    self.opt.Add(in_flows == 0).OnlyEnforceIf(is_shared)
                    self.opt.Add(out_flows == 0).OnlyEnforceIf(is_shared)

    def _induce_external_routing_flow(self):
        self.opt.log_comment("Enforcing directed flow-conservation per net, per terminal to I/O pins ...")
        for net in self.circuit.get_nets(with_power_ground=False):
            if net.is_io_net():
                logger.info(
                    f"Route to I/O pins: {net.name} has {net.num_terminals()} terminals and {self.net_to_flow_cnt[net.name]} flow variables"
                )
                for k in range(net.num_terminals(), self.net_to_flow_cnt[net.name], 1):
                    logger.info(f"Route to I/O pins: {net.name} {k}")
                    for node in self.lgg.nodes():
                        in_flows = sum(
                            self.net_flow_vars[(net.name, k, u_arc, v_arc)]
                            for u_arc, v_arc in self.adj_in.get(node, [])
                            if (net.name, k, u_arc, v_arc) in self.net_flow_vars
                        )
                        out_flows = sum(
                            self.net_flow_vars[(net.name, k, u_arc, v_arc)]
                            for u_arc, v_arc in self.adj_out.get(node, [])
                            if (net.name, k, u_arc, v_arc) in self.net_flow_vars
                        )
                        can_be_src = self.node_is_src_vars[net.name].get(node)
                        if can_be_src is not None:
                            self.opt.Add(out_flows - in_flows == 1).OnlyEnforceIf(can_be_src)

                        can_be_kth_SON = self.node_is_SON_vars[net.name][k].get(node)
                        if can_be_kth_SON is not None:
                            self.opt.Add(in_flows - out_flows == 1).OnlyEnforceIf(can_be_kth_SON)

                        conditions_for_intermediate = []
                        if can_be_src is not None:
                            conditions_for_intermediate.append(can_be_src.Not())
                        if can_be_kth_SON is not None:
                            conditions_for_intermediate.append(can_be_kth_SON.Not())
                        if not conditions_for_intermediate:
                            self.opt.Add(in_flows == out_flows)
                        else:
                            self.opt.Add(in_flows == out_flows).OnlyEnforceIf(conditions_for_intermediate)
                        self.opt.Add(in_flows <= 1)
                        self.opt.Add(out_flows <= 1)

    def _node_exclusivity(self):
        logger.info("\t==\tAdding node exclusivity constraints...")
        self.opt.log_comment("Enforcing a node cannot be propagated flow for more than one net ...")
        for node in self.lgg.nodes():
            net_touches_node_indicators = []
            for net in self.circuit.get_nets(with_power_ground=False):
                net_touches_node_var = self.opt.NewBoolVar(f"net_{net.name}_touches_node_L{node[0]}R{node[1]}C{node[2]}")
                net_touches_node_indicators.append(net_touches_node_var)
                incident_net_arc_vars_for_node_net = []
                for u_arc, __ in self.adj_in.get(node, []):
                    arc_key = (net.name, u_arc, node)
                    if arc_key in self.net_arc_vars:
                        incident_net_arc_vars_for_node_net.append(self.net_arc_vars[arc_key])
                for __, v_arc in self.adj_out.get(node, []):
                    arc_key = (net.name, node, v_arc)
                    if arc_key in self.net_arc_vars:
                        incident_net_arc_vars_for_node_net.append(self.net_arc_vars[arc_key])

                if incident_net_arc_vars_for_node_net:
                    self.opt.AddBoolOr(incident_net_arc_vars_for_node_net).OnlyEnforceIf(net_touches_node_var)
                    self.opt.Add(sum(incident_net_arc_vars_for_node_net) == 0).OnlyEnforceIf(net_touches_node_var.Not())
                else:
                    self.opt.Add(net_touches_node_var == 0)
            if net_touches_node_indicators:
                self.opt.Add(sum(net_touches_node_indicators) <= 1)

    # ================================================================== #
    # gather / window helpers (CFET-specific)                           #
    # ================================================================== #

    def _gather_via_arcs(self, net_name, layer_1, layer_2):
        tmp_via_arcs = []
        for u, v in self.lgg.arcs():
            if u[0] == self.lgg.layer_to_idx[layer_1] and v[0] == self.lgg.layer_to_idx[layer_2]:
                tmp_via_arcs.append(self.net_arc_vars[(net_name, u, v)])
            if u[0] == self.lgg.layer_to_idx[layer_2] and v[0] == self.lgg.layer_to_idx[layer_1]:
                tmp_via_arcs.append(self.net_arc_vars[(net_name, u, v)])
        return tmp_via_arcs

    def _sliding_windows(self, lst, X):
        start_min = 0
        start_max = len(lst) - X
        windows = []
        for start in range(start_min, start_max + 1):
            windows.append(tuple(lst[start: start + X]))
        return windows

    def extract_windows_horizontal_bidirectional(self, u, X):
        """
        Slide windows of length X along the column axis of node u's track,
        collecting directed arcs whose endpoints are reachable from u and lie
        within each window. Used by the M1 minimum-pin-opening rule.
        """
        layer_u, row_u, col_u = u

        # 1) Restrict to arcs on the same layer & row as u
        same_track = [(a, b) for (a, b) in self.lgg.arcs() if a[0] == layer_u and b[0] == layer_u and a[1] == row_u and b[1] == row_u]

        # 2) Build undirected adjacency and BFS to find every node reachable from u
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

        # 3) Gather all column positions of those reachable nodes
        col_positions = sorted({n[2] for n in reachable})

        # 4) Slide windows of length X starting at each of those positions
        windows = []
        for s in col_positions:
            e = s + X
            if not (s <= col_u <= e):
                continue
            in_window = [(a, b) for (a, b) in same_track if a in reachable and b in reachable and s <= a[2] <= e and s <= b[2] <= e]
            if in_window:
                windows.append({"start": s, "end": e, "arcs": in_window})
        return windows

    def gather_src_term_vars_in_pmos_region(self, col):
        gathered_vars = []
        for tran in self.circuit.transistors.values():
            tvar = self.transistor_vars[tran.name]
            if tran.model == Model.PMOS:
                for net, col_vars in tvar.s_col_idx_var.items():
                    for var in col_vars.get(col, []):
                        gathered_vars.append(var)
                for net, col_vars in tvar.d_col_idx_var.items():
                    for var in col_vars.get(col, []):
                        gathered_vars.append(var)
                for net, col_vars in tvar.g_col_idx_var.items():
                    for var in col_vars.get(col, []):
                        gathered_vars.append(var)
        return gathered_vars

    def gather_src_term_vars_in_nmos_region(self, col):
        gathered_vars = []
        for tran in self.circuit.transistors.values():
            tvar = self.transistor_vars[tran.name]
            if tran.model == Model.NMOS:
                for net, col_vars in tvar.s_col_idx_var.items():
                    for var in col_vars.get(col, []):
                        gathered_vars.append(var)
                for net, col_vars in tvar.d_col_idx_var.items():
                    for var in col_vars.get(col, []):
                        gathered_vars.append(var)
                for net, col_vars in tvar.g_col_idx_var.items():
                    for var in col_vars.get(col, []):
                        gathered_vars.append(var)
        return gathered_vars

    def gather_via_vars_in_pmos_region(self, col=None):
        gathered_edge_vars = []
        pmos_pin_access_row = []
        for ri in self.pmos_pin_access_ri:
            pmos_pin_access_row.append(self.lgg.row_in_layer(self.pmos_layer, ri))
        for u, v in self.lgg.edges():
            if u[0] == self.lgg.layer_to_idx[self.pmos_layer] and u[0] != v[0]:
                if col is not None:
                    if u[2] == col and u[1] in pmos_pin_access_row and v[1] in pmos_pin_access_row:
                        gathered_edge_vars.append(self.edge_vars[(u, v)])
                else:
                    if u[1] in pmos_pin_access_row and v[1] in pmos_pin_access_row:
                        gathered_edge_vars.append(self.edge_vars[(u, v)])
        return gathered_edge_vars

    def gather_via_vars_in_nmos_region(self, col=None):
        gathered_edge_vars = []
        nmos_pin_access_row = []
        for ri in self.nmos_pin_access_ri:
            nmos_pin_access_row.append(self.lgg.row_in_layer(self.nmos_layer, ri))
        for u, v in self.lgg.edges():
            if u[0] == self.lgg.layer_to_idx[self.nmos_layer] and u[0] != v[0]:
                if col is not None:
                    if u[2] == col and u[1] in nmos_pin_access_row and v[1] in nmos_pin_access_row:
                        gathered_edge_vars.append(self.edge_vars[(u, v)])
                else:
                    if u[1] in nmos_pin_access_row and v[1] in nmos_pin_access_row:
                        gathered_edge_vars.append(self.edge_vars[(u, v)])
        return gathered_edge_vars

    def gather_nodes_in_pmos_region(self, col=None, row=None):
        """Gather nodes in the PMOS region on self.pmos_layer (per stacking config)."""
        gathered_nodes = []
        pmos_pin_access_row = []
        for ri in self.pmos_pin_access_ri:
            pmos_pin_access_row.append(self.lgg.row_in_layer(self.pmos_layer, ri))
        for node in self.lgg.nodes_in_layer(self.pmos_layer):
            if col is not None and row is not None:
                if node[2] == col and node[1] == row and node[1] in pmos_pin_access_row:
                    gathered_nodes.append(node)
            elif row is not None:
                if node[1] == row and node[1] in pmos_pin_access_row:
                    gathered_nodes.append(node)
            elif col is not None:
                if node[2] == col and node[1] in pmos_pin_access_row:
                    gathered_nodes.append(node)
            else:
                if node[1] in pmos_pin_access_row:
                    gathered_nodes.append(node)
        return gathered_nodes

    def gather_nodes_in_nmos_region(self, col=None, row=None):
        """Gather nodes in the NMOS region on self.nmos_layer (per stacking config)."""
        gathered_nodes = []
        nmos_pin_access_row = []
        for ri in self.nmos_pin_access_ri:
            nmos_pin_access_row.append(self.lgg.row_in_layer(self.nmos_layer, ri))
        for node in self.lgg.nodes_in_layer(self.nmos_layer):
            if col is not None and row is not None:
                if node[2] == col and node[1] == row and node[1] in nmos_pin_access_row:
                    gathered_nodes.append(node)
            elif col is not None:
                if node[2] == col and node[1] in nmos_pin_access_row:
                    gathered_nodes.append(node)
            elif row is not None:
                if node[1] == row and node[1] in nmos_pin_access_row:
                    gathered_nodes.append(node)
            else:
                if node[1] in nmos_pin_access_row:
                    gathered_nodes.append(node)
        return gathered_nodes

    def gather_ds_shareable_vars(self, net_name, tran_name_1, tran_name_2, pin_1, pin_2):
        """Gather diffusion-shareable vars for a given net and two transistors."""
        if pin_1 == "gate" or pin_2 == "gate":
            return []
        key_left_12 = f"ds_left_{tran_name_1}_{tran_name_2}_{net_name}"
        key_left_21 = f"ds_left_{tran_name_2}_{tran_name_1}_{net_name}"
        key_right_12 = f"ds_right_{tran_name_1}_{tran_name_2}_{net_name}"
        key_right_21 = f"ds_right_{tran_name_2}_{tran_name_1}_{net_name}"
        shareable_vars = []
        if key_left_12 in self.ds_pair_vars:
            shareable_vars.append(self.ds_pair_vars[key_left_12])
        if key_left_21 in self.ds_pair_vars:
            shareable_vars.append(self.ds_pair_vars[key_left_21])
        if key_right_12 in self.ds_pair_vars:
            shareable_vars.append(self.ds_pair_vars[key_right_12])
        if key_right_21 in self.ds_pair_vars:
            shareable_vars.append(self.ds_pair_vars[key_right_21])
        return shareable_vars

    def gather_lisd_shareable_vars(self, net_name, tran_name_1, tran_name_2, pin_1, pin_2):
        """Gather LISD-shareable vars for a given net and two transistors."""
        if pin_1 == "gate" or pin_2 == "gate":
            return []
        key_12 = f"lisd_share_{tran_name_1}_{tran_name_2}_{net_name}"
        key_21 = f"lisd_share_{tran_name_2}_{tran_name_1}_{net_name}"
        shareable_vars = []
        if key_12 in self.lisd_share_pair_vars:
            shareable_vars.append(self.lisd_share_pair_vars[key_12])
        if key_21 in self.lisd_share_pair_vars:
            shareable_vars.append(self.lisd_share_pair_vars[key_21])
        return shareable_vars

    def gather_gate_shareable_vars(self, net_name, tran_name_1, tran_name_2, pin_1=None, pin_2=None, check_pin=True):
        """Gather gate-shareable vars for a given net and two transistors."""
        if check_pin and not (pin_1 == "gate" and pin_2 == "gate"):
            return []
        key_12 = f"gate_share_{tran_name_1}_{tran_name_2}_{net_name}"
        key_21 = f"gate_share_{tran_name_2}_{tran_name_1}_{net_name}"
        shareable_vars = []
        if key_12 in self.gate_share_pair_vars:
            shareable_vars.append(self.gate_share_pair_vars[key_12])
        if key_21 in self.gate_share_pair_vars:
            shareable_vars.append(self.gate_share_pair_vars[key_21])
        return shareable_vars

    # ================================================================== #
    # solve                                                              #
    # ================================================================== #

    def solve(self, mode="wsum", objectives=None, exit_on_unsat=True):
        """Dispatch the solve. Only weighted-sum ("wsum") is currently supported."""
        if mode == "wsum":
            return self.wsum(objectives=objectives, solve_setting=self.SET, exit_on_unsat=exit_on_unsat)
        elif mode == "lex":
            raise NotImplementedError("Lexicographic objective function is not implemented")
        elif mode == "pareto":
            raise NotImplementedError("Pareto objective function is not implemented")
        else:
            raise ValueError(f"Invalid objective function mode: {mode}")

    def wsum(self, solve_setting, objectives=None, exit_on_unsat=True):
        """
        Weighted-sum CP-SAT solve. Applies the configured model_preset, sums
        weighted objectives, runs `solver.Solve`. Returns (total_obj_expr,
        ObjectiveValue) on success; honors exit_on_unsat for UNSAT/UNKNOWN.
        """
        import time

        self.opt.log_comment("Defining the objective function ...")
        self.solver = cp_model.CpSolver()
        self.solver.parameters.num_search_workers = self.cell_config["num_search_workers"]["value"]
        self.solver.parameters.random_seed = self.cell_config["seed"]["value"]
        self.solver.parameters.log_search_progress = True
        self.solver.log_callback = print
        if self.cell_config["max_time"]["value"]:
            self.solver.parameters.max_time_in_seconds = self.cell_config["max_time"]["time"]

        if solve_setting == 0:
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
        elif solve_setting == 1:
            self.solver.parameters.search_branching = cp_model.FIXED_SEARCH
            self.solver.parameters.interleave_search = True
            self.solver.parameters.interleave_batch_size = 2 * self.solver.parameters.num_search_workers
        elif solve_setting == 2:
            self.solver.parameters.ignore_subsolvers.extend([
                "quick_restart", "graph_arc_lns", "graph_cst_lns",
                "graph_dec_lns", "graph_var_lns", "rnd_cst_lns", "rnd_var_lns",
                "reduced_costs", "max_lp_sym", "default_lp",
            ])
            self.solver.parameters.linearization_level = 0
            self.solver.parameters.cp_model_presolve = True
            self.solver.parameters.cp_model_probing_level = 3
            self.solver.parameters.symmetry_level = 3
            self.solver.parameters.symmetry_detection_deterministic_time_limit = 180
        elif solve_setting == 3:
            # Large-instance preset: enable LP relaxation for better bound propagation
            self.solver.parameters.ignore_subsolvers.extend([
                "quick_restart", "graph_arc_lns", "graph_cst_lns",
                "graph_dec_lns", "graph_var_lns", "rnd_cst_lns", "rnd_var_lns",
            ])
            self.solver.parameters.linearization_level = 2
            self.solver.parameters.cp_model_presolve = True
            self.solver.parameters.cp_model_probing_level = 2
            self.solver.parameters.symmetry_level = 2

        total_obj = 0
        self.obj_terms = []
        if objectives is None or not objectives:
            logger.info("\t==\tNo objectives defined. Using default objective function ...")
            total_obj = Objective.cpp(self)
        else:
            for i, obj in enumerate(objectives):
                if len(obj) != 3:
                    raise ValueError(f"Invalid objective function format: {obj}. Expected (obj, weight, opt)")
                obj_func, weight, opt = obj
                if not callable(obj_func):
                    raise ValueError(f"Invalid objective function: {obj_func}. Expected a callable.")
                if not isinstance(weight, int):
                    raise ValueError(f"Invalid weight: {weight}. Expected an Integer.")
                if opt not in ("min", "max"):
                    raise ValueError(f"Invalid objective option: {opt}. Expected 'min' or 'max'.")
                obj_name = getattr(obj_func, "__name__", f"obj{i}")
                if obj_name == "<lambda>":
                    import inspect
                    try:
                        src = inspect.getsource(obj_func).strip()
                        match = re.search(r"Objective\.(\w+)\s*\(", src)
                        if match:
                            obj_name = match.group(1)
                    except (OSError, TypeError):
                        pass
                logger.info(f"\t==\tAdding objective function {i + 1}: [{opt}] {obj_name} with weight {weight}")
                obj_expr = obj_func()
                self.obj_terms.append((obj_name, obj_expr, weight, opt))
                if opt == "min":
                    total_obj += weight * obj_expr
                elif opt == "max":
                    total_obj += (-weight) * obj_expr

        self.opt.Minimize(total_obj)
        if self.cell_config["use_relative_gap"]["value"]:
            self.solver.parameters.relative_gap_limit = self.cell_config["use_relative_gap"]["perc"]

        # Decision strategy dispatch (CFET-specific)
        use_strategy = self.cell_config.get("use_strategy", {}).get("value", None)
        if use_strategy == "VIA_FIRST":
            self.use_via_first_strategy()

        time_start = time.time()
        logger.info("\t==\tSolving the model with objective function: WSUM ...")
        status = self.solver.Solve(self.opt)
        time_end = time.time()
        logger.info(f"Elapsed time: {time_end - time_start:.2f} seconds")

        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            logger.info(f"\t==\tObjective function value: {self.solver.ObjectiveValue()}")
            for i, (name, expr, weight, opt) in enumerate(self.obj_terms, start=1):
                val = self.solver.Value(expr)
                logger.info(f" Obj#{i} {name:20s} = {val:6d}    ({opt}imize, weight={weight}, result={val * weight})")
        elif status == cp_model.UNKNOWN:
            logger.error("Solver returned UNKNOWN.")
            if exit_on_unsat:
                exit(1)
            return None, None
        else:
            logger.error("No solution found (UNSAT/INFEASIBLE).")
            if exit_on_unsat:
                exit(1)
            return None, None
        return total_obj, self.solver.ObjectiveValue()
