"""
CFFET cell-generation orchestrator — extends CFET (Architecture A).

CFFET models two back-to-back CFET blocks with Z-axis symmetry:

    Back block:  BBOTPC / BTOPPC  + BM0 (M0ICPD)
    Front block: FBOTPC / FTOPPC  + FM0 (legacy JSON key "M0", M0ICPD)
    Stitch:      STV  (sole inter-block via, BTOPPC <-> FBOTPC)
    Intra-block: BMIV (BBOTPC<->BTOPPC), FMIV (FBOTPC<->FTOPPC)

LGG z-order (bottom -> top), built by CFET's graph machinery from the layer
stack ordered by ``layer_number``:

    BM1 - BM0 - BBOTPC - BTOPPC - STV - FBOTPC - FTOPPC - M0 - M1 - M2

so adjacent-layer vias resolve naturally: BBOTCA (BM0<->BBOTPC), BMIV
(BBOTPC<->BTOPPC), STV (BTOPPC<->FBOTPC), FMIV (FBOTPC<->FTOPPC), FTOPCA
(FTOPPC<->M0), FV0 (M0<->M1), FV1 (M1<->M2). FBOTCA (FBOTPC<->M0) and the
back-block BBOTPC<->BM0 contact are non-adjacent "long vias" added as virtual
overlap edges (``get_virtual_connect_pairs``).

Scope C:
  - P2  Dual-rail 4-tier + STV graph, dual M0ICPD power-row bans.
  - P3/P3b  Dual-face device placement. Each transistor carries a placement-tier
        IntVar ``z_var`` admitting BOTH blocks per model (P_on_N: PMOS ->
        {BTOPPC, FTOPPC}, NMOS -> {BBOTPC, FBOTPC}). Source/gate/drain pin
        candidates are fanned across both legal tiers and each candidate is
        coupled to ``z_var`` (a chosen pin on tier L forces z == L), so a
        device's three pins never straddle the seam. ``placed_tran_at_xzi_vars``
        (tran, ci, zi) slot reifiers and per-block diffusion alignment
        (FBOTPC<->FTOPPC, BBOTPC<->BTOPPC) mirror the QFET tier machinery.
  - P4  AtMostOne FMIV/BMIV per column; AtMostOne STV per column; dual
        ``_ban_signal_on_power_rows`` (inherited, driven by tech layer queries).
  - P5  Cross-face GM/DM/FDM merge (``cross_face_merge``) + at-least-one obligation.
  - P6/P6b  Dual-face ``pin_face`` policy. Inputs are single-face (round-robin
        FIN/BIN over CDL pin order); outputs are dual-face with a Super-Outer-
        Node required on BOTH route metals (front M0 and back BM0). SONs live
        directly on M0/BM0 at signal rows, replacing the inherited M1 SON model.

See docs/superpowers/specs/2026-06-27-cffet-design.md.
"""

import os
from collections import defaultdict

from loguru import logger

from src.cellgen.archit.CFET.main import CFET
from src.cellgen.archit.CFFET.tech import CFFET_Tech
from src.cellgen.archit.CFFET.util import write_cffet_result
from src.cellgen.archit.CFFET.cross_face_merge import (
    pairwise_cross_face_merge,
    enforce_cross_face_merge_obligation,
)
from src.cellgen.archit.CFFET.inter_row_merge import (
    pairwise_inter_row_merge,
    enforce_inter_row_merge_obligation,
)
from src.cellgen.core.entity import Model
from src.cellgen.core.variable import TransistorVar
from src.cellgen.core.util import log_variable_info
from ortools.sat.python import cp_model


class CFFET(CFET):
    """Formulate and place CFFET cells (Flip-FET, dual CFET blocks)."""

    def _print_banner(self):
        logger.info("SMTCell CFFET orchestrator (extends CFET, dual-face Flip-FET)")

    def _maybe_write_results(self):
        """Write the .res result + .var dump + dual-face PNG on success."""
        if not self.solve_status:
            return
        subckt = self.circuit.subckt_name
        log_variable_info(self, filename=f"{self.output_dir}/result/{subckt}.var")
        res_path = f"{self.output_dir}/result/{subckt}.res"
        write_cffet_result(
            self.solver, self.circuit, self.transistor_vars, self.edge_vars,
            self.net_arc_vars, self.c_tech, self.cpp_cost,
            filename=res_path,
            lgg=self.lgg,
        )
        from src.cellgen.postprocess.visualize_CFFET_4T import (
            draw_cffet_layout,
            load_results,
        )
        view_dir = os.path.join(self.output_dir, "view")
        os.makedirs(view_dir, exist_ok=True)
        placement, routing, _tech = load_results(res_path)
        draw_cffet_layout(
            placement, routing,
            filename=os.path.join(view_dir, f"{subckt}.png"),
        )

    # ================================================================== #
    # P2 — domain over the front canonical placement tier (FTOPPC)       #
    # ================================================================== #

    def _init_domain(self):
        """
        Initialize CP-SAT placement domains over the canonical front placement
        tier (``FTOPPC``). All four CFFET placement tiers share the same column
        and row grid, so one tier's indices serve as the domain for all.

        Mirrors ``CFET._init_domain`` but uses the tech-declared front tiers in
        place of the hardcoded ``PC`` / ``BPC`` layer names (which do not exist
        in the CFFET stack).
        """
        logger.debug("Initializing CFFET variable domain (front-tier grid)...")
        domain_layer = self.c_tech.get_domain_placement_layer()       # FTOPPC
        bottom_layer = self.c_tech.get_front_bottom_placement_layer()  # FBOTPC

        # MOS-placeable columns: odd (S/D) col indices, drop the last so a gate
        # column always exists to the right of every placed device.
        self.plc_ci = self.lgg.col_indices_in_layer(domain_layer, parity="odd")[:-1]
        self.domain_mos_placable_ci = cp_model.Domain.FromValues(self.plc_ci)
        logger.info(f"Domain MOS placeable col indices: {self.domain_mos_placable_ci}")
        self.plc_ri = self.lgg.row_indices_in_layer(domain_layer, parity="even")
        self.domain_mos_placable_ri = cp_model.Domain.FromValues(self.plc_ri)

        # source/drain/gate col indices
        self.sd_ci = self.lgg.col_indices_in_layer(domain_layer, parity="odd")
        self.domain_sd_ci = cp_model.Domain.FromValues(self.sd_ci)
        self.g_ci = self.lgg.col_indices_in_layer(domain_layer, parity="even")
        self.domain_g_ci = cp_model.Domain.FromValues(self.g_ci)

        # all col / row coords + indices on the canonical front tier
        self.pc_ci = self.lgg.col_indices_in_layer(domain_layer)
        self.domain_pc_ci = cp_model.Domain.FromValues(self.pc_ci)
        self.pc_ri = self.lgg.row_indices_in_layer(domain_layer)
        self.domain_pc_ri = cp_model.Domain.FromValues(self.pc_ri)
        self.all_pc_row = self.lgg.rows_in_layer(domain_layer)
        self.domain_pc_ri = cp_model.Domain.FromValues(self.all_pc_row)
        self.all_pc_col = self.lgg.cols_in_layer(domain_layer)
        self.domain_pc_ci = cp_model.Domain.FromValues(self.all_pc_col)
        logger.info(f"Domain front-tier row: {self.all_pc_row}, col: {self.all_pc_col}")

        # front-bottom tier domains (the v1 "BPC" analogue)
        self.bpc_ci = self.lgg.col_indices_in_layer(bottom_layer)
        self.domain_bpc_ci = cp_model.Domain.FromValues(self.bpc_ci)
        self.bpc_ri = self.lgg.row_indices_in_layer(bottom_layer)
        self.domain_bpc_ri = cp_model.Domain.FromValues(self.bpc_ri)
        self.all_bpc_row = self.lgg.rows_in_layer(bottom_layer)
        self.all_bpc_col = self.lgg.cols_in_layer(bottom_layer)

        if (
            self.c_tech.height_config == "SH"
            and self.c_tech.num_rt_track == 3
            and len(getattr(self, "nmos_placeable_row_indices", [])) > 1
        ):
            rows = self.nmos_placeable_row_indices
            self.domain_mos_placable_ri = cp_model.Domain.FromValues(rows)
            logger.info(f"\t==\t[CFFET] y domain: {rows}")

    # ================================================================== #
    # P3a — relaxed placement y (signal rows) + per-row x uniqueness     #
    # ================================================================== #

    def _init_tech(self):
        """CFET tech + CFFET: allow placement on M0ICPD signal rows (not y=0 only)."""
        super()._init_tech()
        if (
            self.c_tech.height_config == "SH"
            and self.c_tech.num_rt_track == 3
            and hasattr(self, "signal_row_indices")
        ):
            rows = list(self.signal_row_indices)
            self.nmos_placeable_row_indices = rows
            self.pmos_placeable_row_indices = rows
            logger.info(
                f"\t==\t[CFFET] relaxed placement y rows: {rows} "
                f"(was CFET single-row [0])"
            )

    def _multi_row_placement_enabled(self) -> bool:
        if not self._cfg_get("enable_multi_row", True):
            return False
        return len(getattr(self, "nmos_placeable_row_indices", [0])) > 1

    def _reify_y_eq(self, t1: str, t2: str):
        y1 = self.transistor_vars[t1].y_var
        y2 = self.transistor_vars[t2].y_var
        y_eq = self.opt.NewBoolVar(f"y_eq_{t1}_{t2}")
        self.opt.Add(y1 == y2).OnlyEnforceIf(y_eq)
        self.opt.Add(y1 != y2).OnlyEnforceIf(y_eq.Not())
        return y_eq

    def _cffet_init_transistor_placement_vars(self):
        """CFET transistor x/y/flip + per-row AllDifferent (relaxed y)."""
        self.opt.log_comment("CFFET transistor variables (relaxed y)")
        pmos_rows = self.pmos_placeable_row_indices
        nmos_rows = self.nmos_placeable_row_indices
        y_domain = cp_model.Domain.FromValues(pmos_rows)

        for tran in self.circuit.transistors.values():
            tvar = TransistorVar(tran.name)
            self.transistor_vars[tran.name] = tvar
            tvar.x_var = self.opt.NewIntVarFromDomain(
                self.domain_mos_placable_ci, f"{tran.name}_x",
            )
            tvar.y_var = self.opt.NewIntVarFromDomain(y_domain, f"{tran.name}_y")
            tvar.flip_var = self.opt.NewBoolVar(f"{tran.name}_flip")
            allowed = pmos_rows if tran.model == Model.PMOS else nmos_rows
            self.opt.AddAllowedAssignments([tvar.y_var], [(r,) for r in allowed])

        # x uniqueness only among same-model devices on the SAME y row.
        trans = sorted(self.circuit.transistors.values(), key=lambda t: t.name)
        for i, t1 in enumerate(trans):
            for t2 in trans[i + 1:]:
                if t1.model != t2.model:
                    continue
                x1 = self.transistor_vars[t1.name].x_var
                x2 = self.transistor_vars[t2.name].x_var
                y_eq = self._reify_y_eq(t1.name, t2.name)
                self.opt.Add(x1 != x2).OnlyEnforceIf(y_eq)

        for tran in self.circuit.transistors.values():
            tvar = self.transistor_vars[tran.name]
            for ci in self.plc_ci:
                placed = self.opt.NewBoolVar(f"tran_placed_col_{tran.name}_{ci}")
                self.placed_tran_ci_vars[(tran.name, ci)] = placed
                self.opt.Add(tvar.x_var == ci).OnlyEnforceIf(placed)
                self.opt.Add(tvar.x_var != ci).OnlyEnforceIf(placed.Not())

        for ci in self.plc_ci:
            has_tran = self.opt.NewBoolVar(f"has_tran_ci_{ci}")
            self.has_tran_at_ci_vars[ci] = has_tran
            all_placed = [
                self.placed_tran_ci_vars[(t.name, ci)]
                for t in self.circuit.transistors.values()
            ]
            self.opt.AddBoolOr(all_placed).OnlyEnforceIf(has_tran)
            self.opt.Add(sum(all_placed) == 0).OnlyEnforceIf(has_tran.Not())

    def _init_cpp(self):
        """CPP cost + lower bound (per-row transistor count when multi-y)."""
        self.opt.log_comment("Enforcing total cpp...")
        self.cpp_cost = self.opt.NewIntVarFromDomain(
            self.domain_sd_ci,
            "cpp_cost",
        )
        self.opt.AddMaxEquality(
            self.cpp_cost,
            [self.transistor_vars[t.name].x_var for t in self.circuit.transistors.values()],
        )
        num_pmos = self.circuit.num_pmos_transistors()
        num_nmos = self.circuit.num_nmos_transistors()
        num_y_rows = max(1, len(self.nmos_placeable_row_indices))
        per_row = -(-max(num_pmos, num_nmos) // num_y_rows)
        if per_row > 0:
            sorted_plc_ci = sorted(self.plc_ci)
            if per_row <= len(sorted_plc_ci):
                min_cpp_col = sorted_plc_ci[per_row - 1]
            else:
                stride = sorted_plc_ci[1] - sorted_plc_ci[0] if len(sorted_plc_ci) > 1 else 2
                min_cpp_col = sorted_plc_ci[-1] + stride * (per_row - len(sorted_plc_ci))
            self.opt.Add(self.cpp_cost >= min_cpp_col)
            logger.info(
                f"\t==\t[CFFET] CPP lower bound: cpp_cost >= {min_cpp_col} "
                f"(per_row={per_row}, y_rows={num_y_rows})"
            )
            sorted_plc_ci = sorted(self.plc_ci)
            for i, tran in enumerate(
                t for t in self.circuit.transistors.values() if t.model == Model.PMOS
            ):
                if i < len(sorted_plc_ci):
                    self.opt.AddHint(self.transistor_vars[tran.name].x_var, sorted_plc_ci[i])
            for i, tran in enumerate(
                t for t in self.circuit.transistors.values() if t.model == Model.NMOS
            ):
                if i < len(sorted_plc_ci):
                    self.opt.AddHint(self.transistor_vars[tran.name].x_var, sorted_plc_ci[i])
            self.opt.AddHint(self.cpp_cost, min_cpp_col)

    # ================================================================== #
    # P3 — z (tier) variable + model->tier restriction                   #
    # ================================================================== #

    def _init_transistor_vars(self):
        """
        Build the CFET transistor placement variables, then attach the CFFET
        ``z_var`` (placement-tier IntVar) per transistor, restrict it to the
        tier(s) legal for the device model, and build the per-(tran, ci, zi)
        slot reifiers used by dual-face pin resolution + diffusion alignment.

        Setting ``uses_tier_placement`` makes the architecture genuinely
        tier-aware: the shared ``accelerate`` / routing helpers switch to their
        tier-gated form and the CFET per-net layer pruning keeps every tier
        reachable (routing.py). ``_cffet_model_tier_restriction`` now admits
        BOTH faces per model (P_on_N: PMOS -> {BTOPPC, FTOPPC}, NMOS ->
        {BBOTPC, FBOTPC}), so a device may land on either block; the chosen
        face is bound to ``z_var`` through the pin candidates (a source/gate/
        drain candidate on tier L implies z == L, see
        ``_cffet_populate_pin_candidates``).
        """
        # Must be set BEFORE accelerate's / routing's tier-gated constraints
        # are emitted (they run later, in _build_constraints).
        self.uses_tier_placement = True
        self._cffet_init_transistor_placement_vars()
        self._init_tier_vars()

    def _placement_tier_indices(self):
        """LGG layer indices of the four CFFET placement tiers, low z -> high z."""
        out = []
        for name in self.c_tech.get_placement_layers():
            try:
                out.append(self.lgg.layer_index(name))
            except KeyError:
                pass
        return sorted(out)

    def _pmos_tier_names(self):
        """Placement tiers a PMOS may occupy (top tier of each block)."""
        return list(self.c_tech.get_pc_tiers())

    def _nmos_tier_names(self):
        """Placement tiers an NMOS may occupy (bottom tier of each block)."""
        return list(self.c_tech.get_bpc_tiers())

    def _model_tier_names(self, model):
        return self._pmos_tier_names() if model == Model.PMOS else self._nmos_tier_names()

    def _init_tier_vars(self):
        """
        Create one z (tier) IntVar per transistor, restrict it per model, and
        build the per-tier (z) + per-slot (x, z) placement reifiers.

        Reifiers (mirroring QFET):
            placed_tran_zi_vars[(tran, zi)]          z_var == zi
            placed_tran_at_xzi_vars[(tran, ci, zi)]  placed at col ci AND tier zi
        """
        self.opt.log_comment("CFFET tier (z) variables")
        self.plc_zi = self._placement_tier_indices()
        zi_domain = cp_model.Domain.FromValues(self.plc_zi)
        self.placed_tran_zi_vars = {}
        self.placed_tran_at_xzi_vars = {}
        for tran in self.circuit.transistors.values():
            tvar = self.transistor_vars[tran.name]
            tvar.z_var = self.opt.NewIntVarFromDomain(zi_domain, f"{tran.name}_z")
        self._cffet_model_tier_restriction()

        # Per-tier reifier: placed_tran_zi[(tran, zi)] <-> z_var == zi.
        for tran in self.circuit.transistors.values():
            z = self.transistor_vars[tran.name].z_var
            for zi in self.plc_zi:
                placed = self.opt.NewBoolVar(f"tran_placed_tier_{tran.name}_{zi}")
                self.placed_tran_zi_vars[(tran.name, zi)] = placed
                self.opt.Add(z == zi).OnlyEnforceIf(placed)
                self.opt.Add(z != zi).OnlyEnforceIf(placed.Not())

        # Per-slot reifier: placed_at_xzi[(tran, ci, zi)] = placed_ci AND placed_zi.
        for tran in self.circuit.transistors.values():
            for ci in self.plc_ci:
                placed_ci = self.placed_tran_ci_vars[(tran.name, ci)]
                for zi in self.plc_zi:
                    placed_zi = self.placed_tran_zi_vars[(tran.name, zi)]
                    is_at = self.opt.NewBoolVar(f"{tran.name}_at_ci{ci}_zi{zi}")
                    self.opt.AddBoolAnd([placed_ci, placed_zi]).OnlyEnforceIf(is_at)
                    self.opt.AddBoolOr([placed_ci.Not(), placed_zi.Not()]).OnlyEnforceIf(is_at.Not())
                    self.placed_tran_at_xzi_vars[(tran.name, ci, zi)] = is_at

    def _cffet_model_tier_restriction(self):
        """
        Constrain each transistor's ``z_var`` to the placement tier(s) legal for
        its device model via AllowedAssignments.

        Dual-face (P3b): PMOS may sit on EITHER block's top tier and NMOS on
        EITHER block's bottom tier (P_on_N: PMOS -> {BTOPPC, FTOPPC}, NMOS ->
        {BBOTPC, FBOTPC}; the tech swaps top/bottom for N_on_P).
        """
        pmos_tiers = [self.lgg.layer_index(n) for n in self._pmos_tier_names()]
        nmos_tiers = [self.lgg.layer_index(n) for n in self._nmos_tier_names()]
        logger.info(
            f"\t==\t[CFFET] PMOS tiers {self._pmos_tier_names()} -> {pmos_tiers}, "
            f"NMOS tiers {self._nmos_tier_names()} -> {nmos_tiers}"
        )
        for tran in self.circuit.transistors.values():
            z = self.transistor_vars[tran.name].z_var
            allowed = pmos_tiers if tran.model == Model.PMOS else nmos_tiers
            self.opt.AddAllowedAssignments([z], [(t,) for t in allowed])

    # ================================================================== #
    # P3b — dual-face pin candidate generation (tier-aware) + z coupling #
    # ================================================================== #

    def _init_src_super_inner_nodes_vars(self):
        """Per-net source-pin candidates fanned across BOTH faces (z-coupled)."""
        self.opt.log_comment("Super Inner Nodes for src pins (CFFET dual-face)")
        for net in self.circuit.get_nets(with_power_ground=False):
            self.node_is_src_vars[net.name] = {}
            tran_name, pin_role = net.source()
            self._cffet_populate_pin_candidates(
                net=net,
                tran=self.circuit.transistors[tran_name],
                tvar=self.transistor_vars[tran_name],
                pin_role=pin_role,
                target_dict=self.node_is_src_vars[net.name],
                var_prefix=f"net_issrc_{net.name}",
            )

    def _init_term_super_inner_nodes_vars(self):
        """Per-net terminal-pin candidates fanned across BOTH faces (z-coupled)."""
        self.opt.log_comment("Super Inner Nodes for terminal pins (CFFET dual-face)")
        for net in self.circuit.get_nets(with_power_ground=False):
            self.node_is_term_vars[net.name] = {}
            for k, (tran_name, pin_role) in enumerate(net.terminals()):
                self.node_is_term_vars[net.name][k] = {}
                self._cffet_populate_pin_candidates(
                    net=net,
                    tran=self.circuit.transistors[tran_name],
                    tvar=self.transistor_vars[tran_name],
                    pin_role=pin_role,
                    target_dict=self.node_is_term_vars[net.name][k],
                    var_prefix=f"net_isterm_{net.name}_{k}",
                )

    def _cffet_populate_pin_candidates(self, *, net, tran, tvar, pin_role, target_dict, var_prefix):
        """
        Create source/gate/drain candidate bools for ``tran`` on BOTH of its
        legal device tiers (one per block face), register them on the flat
        per-column ``tvar.{s|g|d}_col_idx_var[net][col]`` maps the inherited
        CFET link consumes, and couple each candidate to ``z_var``.

        Coupling (the heart of dual-face placement): a candidate placed on tier
        ``L`` implies ``z_var == L``. Because the link forces exactly one
        chosen source/gate/drain candidate and each implies its own tier, the
        solver is forced to pick all three on the SAME face (a single ``z_var``
        cannot equal two tiers), so a device's pins never straddle the seam.
        """
        if tran.model == Model.PMOS:
            pin_rows = self.pmos_pin_access_ri
        elif tran.model == Model.NMOS:
            pin_rows = self.nmos_pin_access_ri
        else:
            raise ValueError(f"Transistor {tran.name} is not PMOS or NMOS (model={tran.model})")

        attr = {
            "source": "s_col_idx_var",
            "gate": "g_col_idx_var",
            "drain": "d_col_idx_var",
        }[pin_role]
        want_odd = pin_role in ("source", "drain")

        for tier_name in self._model_tier_names(tran.model):
            layer_idx = self.lgg.layer_index(tier_name)
            placed_zi = self.placed_tran_zi_vars[(tran.name, layer_idx)]
            for ri in pin_rows:
                row = self.lgg.row_in_layer(tier_name, ri)
                for ci in self.lgg.col_indices_in_layer(tier_name):
                    col = self.lgg.col_in_layer(tier_name, ci)
                    is_odd = self.lgg.is_odd_col(layer=tier_name, col=col)
                    if want_odd and not is_odd:
                        continue
                    if (not want_odd) and is_odd:
                        continue
                    bv = self.opt.NewBoolVar(f"{var_prefix}_L{layer_idx}_R{row}_C{col}")
                    getattr(tvar, attr).setdefault(net.name, {}).setdefault(col, []).append(bv)
                    target_dict[(layer_idx, row, col)] = bv
                    # Pin tier (z) and placement row (y) coupling.
                    self.opt.AddImplication(bv, placed_zi)
                    if self._multi_row_placement_enabled():
                        self.opt.Add(tvar.y_var == ri).OnlyEnforceIf(bv)

    # ----- tier-aware region gathers (union over a model's two faces) ----- #

    def gather_nodes_in_pmos_region(self, col=None, row=None):
        return self._cffet_gather_nodes(self._pmos_tier_names(), self.pmos_pin_access_ri, col, row)

    def gather_nodes_in_nmos_region(self, col=None, row=None):
        return self._cffet_gather_nodes(self._nmos_tier_names(), self.nmos_pin_access_ri, col, row)

    def _cffet_gather_nodes(self, tier_names, pin_access_ri, col, row):
        gathered = []
        for tier_name in tier_names:
            access_rows = {self.lgg.row_in_layer(tier_name, ri) for ri in pin_access_ri}
            for node in self.lgg.nodes_in_layer(tier_name):
                if node[1] not in access_rows:
                    continue
                if col is not None and node[2] != col:
                    continue
                if row is not None and node[1] != row:
                    continue
                gathered.append(node)
        return gathered

    def gather_via_vars_in_pmos_region(self, col=None):
        return self._cffet_gather_via_vars(self._pmos_tier_names(), self.pmos_pin_access_ri, col)

    def gather_via_vars_in_nmos_region(self, col=None):
        return self._cffet_gather_via_vars(self._nmos_tier_names(), self.nmos_pin_access_ri, col)

    def _cffet_gather_via_vars(self, tier_names, pin_access_ri, col):
        gathered = []
        tier_idxs = {self.lgg.layer_to_idx[t] for t in tier_names}
        access_rows = set()
        for tier_name in tier_names:
            for ri in pin_access_ri:
                access_rows.add(self.lgg.row_in_layer(tier_name, ri))
        for u, v in self.lgg.edges():
            if u[0] in tier_idxs and u[0] != v[0]:
                if col is not None and u[2] != col:
                    continue
                if u[1] in access_rows and v[1] in access_rows:
                    gathered.append(self.edge_vars[(u, v)])
        return gathered

    # ----- per-block diffusion alignment (P3b) ---------------------------- #

    def _diffusion_alignment(self):
        """
        Override CFET's single PMOS<->NMOS column alignment with CFFET's
        per-block alignment: a diffusion break in a block's bottom tier must
        align (same column) with one in that block's top tier, for EACH block
        independently (front: FBOTPC<->FTOPPC, back: BBOTPC<->BTOPPC).
        """
        self._cffet_pair_diffusion_alignment()

    def _cffet_pair_diffusion_alignment(self):
        """Build per-tier diffusion-break vars and align the two tiers within
        each CFFET block (cross-block alignment is NOT required: the blocks
        have independent diffusions)."""
        if not getattr(self.c_tech, "enforce_diffusion_alignment", True):
            return
        self.opt.log_comment("CFFET per-block diffusion alignment (FBOTPC<->FTOPPC, BBOTPC<->BTOPPC) ...")
        logger.info("\t==\t[CFFET] per-block diffusion alignment")

        # Per-tier diffusion break: db_tier[(zi, ci)] iff NO device of that
        # tier's model is placed at slot (ci, zi).
        self.db_tier_vars = {}
        pmos_tier_names = set(self._pmos_tier_names())
        for tier_name in self.c_tech.get_placement_layers():
            zi = self.lgg.layer_index(tier_name)
            model = Model.PMOS if tier_name in pmos_tier_names else Model.NMOS
            at_slot_by_ci = {
                ci: [
                    self.placed_tran_at_xzi_vars[(t.name, ci, zi)]
                    for t in self.circuit.transistors.values() if t.model == model
                ]
                for ci in self.plc_ci
            }
            for ci in self.plc_ci:
                dbv = self.opt.NewBoolVar(f"db_tier_z{zi}_ci{ci}")
                self.db_tier_vars[(zi, ci)] = dbv
                at_slot = at_slot_by_ci[ci]
                if at_slot:
                    self.opt.Add(sum(at_slot) == 0).OnlyEnforceIf(dbv)
                    self.opt.Add(sum(at_slot) >= 1).OnlyEnforceIf(dbv.Not())
                else:
                    self.opt.Add(dbv == 1)

        # Align bottom<->top tier per block (intra-block MIV pairs are exactly
        # the (bottom, top) tier pairs of each block).
        for bot_name, top_name in self.c_tech.get_intra_block_miv_pairs():
            bot_zi = self.lgg.layer_index(bot_name)
            top_zi = self.lgg.layer_index(top_name)
            for ci in self.plc_ci:
                self.opt.AddImplication(self.db_tier_vars[(bot_zi, ci)], self.db_tier_vars[(top_zi, ci)])
                self.opt.AddImplication(self.db_tier_vars[(top_zi, ci)], self.db_tier_vars[(bot_zi, ci)])

    def _placement_constraints(self):
        """CFET placement + cross-face (v2) + inter-row (v3) merge vars."""
        super()._placement_constraints()
        if self._cfg_get("enable_cross_face_merge", True):
            pairwise_cross_face_merge(self)
        if self._multi_row_placement_enabled():
            pairwise_inter_row_merge(self)

    def _build_solve_objectives(self):
        """WSUM objectives + FDM fin-cut penalty (+1 CPP per active FDM)."""
        objectives = super()._build_solve_objectives()
        overrides = self._cfg_get("objective_weights", {}) or {}
        fdm_weight = overrides.get("fdm_penalty", 1000)
        if fdm_weight:
            from src.cellgen.core.objective import Objective
            objectives = list(objectives) + [
                (lambda: Objective.fdm_penalty(self), fdm_weight, "min"),
            ]
        return objectives

    # ================================================================== #
    # P4 — dual intra-block MIV + inter-block STV column constraints     #
    # ================================================================== #

    def _routing_constraints(self):
        """Inherit the full CFET routing pipeline, then add CFFET-specific
        inter-block (STV) and cross-face-merge constraints.

        CFFET pins live directly on the route metals (M0 front / BM0 back) via
        the P6b dual-face SON model, so the inherited M1-SON pin-accessibility
        rules (M1 minimum-pin-opening, M0-pin end-of-line extension) do not
        apply: the former asserts an M2 node exists above every SON column
        (false for fine-pitch M0/BM0 SON cols) and the latter is an M1-pin EOL
        rule. Neutralize them here before delegating to the CFET pipeline.
        """
        if isinstance(self.cell_config.get("MPO"), dict):
            self.cell_config["MPO"]["value"] = 0
        if isinstance(self.cell_config.get("m0_pin_extension"), dict):
            self.cell_config["m0_pin_extension"]["value"] = False
        super()._routing_constraints()
        self._only_one_stv_per_col()
        if self._cfg_get("enable_cross_face_merge", True) and self._cfg_get(
            "enforce_cross_face_merge", True
        ):
            enforce_cross_face_merge_obligation(self)
        if self._multi_row_placement_enabled() and self._cfg_get(
            "enforce_inter_row_merge", True
        ):
            enforce_inter_row_merge_obligation(self)

    def _only_one_miv_per_col(self):
        """
        AtMostOne MIV per column, applied to BOTH intra-block pairs
        (BBOTPC<->BTOPPC and FBOTPC<->FTOPPC).

        Overrides the CFET single-pair version: CFFET has two MIV layers, one
        per block, and each must be limited to one via per column independently.
        """
        for bot_name, top_name in self.c_tech.get_intra_block_miv_pairs():
            self.opt.log_comment(f"At most one MIV ({bot_name} to {top_name}) per column")
            try:
                bot_idx = self.lgg.layer_index(bot_name)
                top_idx = self.lgg.layer_index(top_name)
            except KeyError:
                continue
            miv_edges_by_col = defaultdict(list)
            for (u, v), evar in self.edge_vars.items():
                if {u[0], v[0]} == {bot_idx, top_idx}:
                    miv_edges_by_col[u[2]].append(evar)
            for col, evars in miv_edges_by_col.items():
                if evars:
                    self.opt.Add(sum(evars) <= 1)

    def _only_one_stv_per_col(self):
        """AtMostOne inter-block STV (BTOPPC<->FBOTPC) per column."""
        stv = self.c_tech.get_stitch_via_name()
        if stv is None:
            return
        # STV connects the two tiers named by its upper/lower layer in the stack.
        bot_name, top_name = "BTOPPC", "FBOTPC"
        self.opt.log_comment(f"At most one STV ({bot_name} to {top_name}) per column")
        try:
            bot_idx = self.lgg.layer_index(bot_name)
            top_idx = self.lgg.layer_index(top_name)
        except KeyError:
            return
        stv_edges_by_col = defaultdict(list)
        for (u, v), evar in self.edge_vars.items():
            if {u[0], v[0]} == {bot_idx, top_idx}:
                stv_edges_by_col[u[2]].append(evar)
        for col, evars in stv_edges_by_col.items():
            if evars:
                self.opt.Add(sum(evars) <= 1)

    # ================================================================== #
    # P6b — dual-face pin policy                                         #
    #   inputs : single-face SON (round-robin FIN/BIN over CDL order)    #
    #   outputs: dual-face SON (front M0 + back BM0), both required      #
    # ================================================================== #

    def _cffet_route_metals(self):
        """(front_metal, back_metal) route-metal layer names (e.g. M0, BM0)."""
        front = self.c_tech.get_front_route_metal()
        back = self.c_tech.get_back_route_metals()[0]
        return front, back

    def _cffet_pin_face_cfg(self):
        """Return the resolved ``pin_face`` config payload, or None if absent."""
        entry = self.cell_config.get("pin_face")
        if isinstance(entry, dict) and "value" in entry:
            return entry["value"]
        return None

    def _resolve_input_faces(self):
        """
        Map each primary-input net to its single assigned route-metal layer.

        Policy (from cell_config ``pin_face.input``):
          - ``explicit`` dict (net -> "front"/"back") wins when present;
          - otherwise round-robin across faces in CDL pin order, starting at
            ``front`` (FIN, BIN, FIN, ...).

        The computed mapping is written back into the config's ``explicit``
        block so the assignment is recorded on the instance (config.py emits
        the schema; the CDL-order round-robin is materialized here where the
        circuit is known).
        """
        front_metal, back_metal = self._cffet_route_metals()
        face_to_layer = {"front": front_metal, "back": back_metal}
        faces = ["front", "back"]
        default_face = "front"
        explicit = {}
        cfg = self._cffet_pin_face_cfg()
        if cfg:
            face_to_layer = dict(cfg.get("face_to_layer", face_to_layer))
            in_cfg = cfg.get("input", {}) or {}
            explicit = dict(in_cfg.get("explicit", {}) or {})
            default_face = in_cfg.get("default_face", "front")

        assignment = {}
        rr = 0
        for net_name in self.circuit.input_net_names():
            if net_name in explicit:
                face = explicit[net_name]
            else:
                face = faces[rr % len(faces)]
                rr += 1
                explicit[net_name] = face
            layer = face_to_layer.get(face) or face_to_layer.get(default_face, front_metal)
            assignment[net_name] = layer

        # Record the materialized round-robin back into the config schema.
        if cfg is not None:
            cfg.setdefault("input", {})["explicit"] = explicit
        logger.info(f"\t==\t[CFFET] input pin faces: {assignment}")
        return assignment

    def _init_net_flow_vars(self):
        """
        P6b flow policy: outputs carry TWO extra-flow commodities (one per
        face, so the output net is forced to reach BOTH M0 and BM0), inputs
        carry ONE (single assigned face), internal nets none.
        """
        self.opt.log_comment("Net flow variables (CFFET dual-face pin policy)")
        for net in self.circuit.get_nets(with_power_ground=False):
            num_extra_flow = self._cffet_num_extra_flow(net)
            self.num_pins_for_io += num_extra_flow
            for k in range(net.num_terminals() + num_extra_flow):
                for u_arc, v_arc in self.lgg.arcs():
                    self.net_flow_vars[(net.name, k, u_arc, v_arc)] = self.opt.NewBoolVar(
                        f"flow_{net.name}_{k}_{u_arc}_{v_arc}"
                    )
            self.net_to_flow_cnt[net.name] = net.num_terminals() + num_extra_flow

    def _cffet_num_extra_flow(self, net):
        """Extra-flow (SON) commodity count for a net under the P6b policy."""
        if self.circuit.is_output_net(net.name):
            return 2  # dual-face output: front (M0) + back (BM0)
        if self.circuit.is_input_net(net.name):
            return 1  # single-face input
        if net.is_io_net():
            return 1  # unclassified IO: treat as single-face
        return 0

    def _init_SON_positions(self):
        """
        P6b: collect Super-Outer-Node candidates on BOTH route metals (front
        M0 and back BM0) at the signal rows (power rows excluded), like QFET's
        io_pin-layer SONs. Replaces CFET's M1-only SON model.
        """
        front_metal, back_metal = self._cffet_route_metals()
        self.son_terminal_nodes = {}
        son_row_indices = getattr(self, "signal_row_indices", None) or self._get_son_row_indices()
        for layer_name in (front_metal, back_metal):
            if layer_name not in self.lgg.layer_to_idx:
                self.son_terminal_nodes[layer_name] = []
                continue
            son_rows = {
                self.lgg.row_in_layer(layer_name, ri)
                for ri in son_row_indices
                if ri < self.lgg.num_rows_in_layer(layer_name)
            }
            self.son_terminal_nodes[layer_name] = [
                node for node in self.lgg.nodes_in_layer(layer_name) if node[1] in son_rows
            ]
            logger.info(
                f"\t==\t[CFFET] SON positions on '{layer_name}': "
                f"{len(self.son_terminal_nodes[layer_name])} node(s)"
            )

    def _init_SON_vars(self):
        """
        P6b SON binding:
          - input net : exactly one SON on its assigned face layer (sum == 1);
          - output net: exactly one SON on M0 (front commodity) AND exactly one
            SON on BM0 (back commodity), so the output appears on both faces.
        """
        self.opt.log_comment("Super Outer Nodes for I/O pins (CFFET dual-face)")
        front_metal, back_metal = self._cffet_route_metals()
        input_face_layer = self._resolve_input_faces()
        for net in self.circuit.get_nets(with_power_ground=False):
            if not net.is_io_net():
                continue
            self.node_is_SON_vars[net.name] = {}
            ks = list(range(net.num_terminals(), self.net_to_flow_cnt[net.name]))
            is_output = self.circuit.is_output_net(net.name)
            for idx, k in enumerate(ks):
                self.node_is_SON_vars[net.name][k] = {}
                if is_output:
                    # First extra-flow commodity -> front (M0); second -> back (BM0).
                    layer_name = front_metal if idx == 0 else back_metal
                else:
                    layer_name = input_face_layer.get(net.name, front_metal)
                for node in self.son_terminal_nodes.get(layer_name, []):
                    layer_idx, row, col = node
                    bv = self.opt.NewBoolVar(
                        f"net_isSON_{net.name}_{k}_L{layer_idx}_R{row}_C{col}"
                    )
                    self.node_is_SON_vars[net.name][k][(layer_idx, row, col)] = bv
                    self.node_to_net_SON_vars.setdefault(
                        (layer_idx, row, col), {}
                    ).setdefault(net.name, []).append(bv)
                self.opt.Add(sum(self.node_is_SON_vars[net.name][k].values()) == 1)

    def _all_son_nodes(self):
        """Iterate every (layer_idx, row, col) SON candidate across face metals."""
        for nodes in self.son_terminal_nodes.values():
            for node in nodes:
                yield node

    def _net_SON_node_uniqueness(self):
        """A SON node may serve at most one net (across face metals + commodities)."""
        self.opt.log_comment("Enforcing an SON terminal cannot be a terminal for more than one net ...")
        for node in self._all_son_nodes():
            tmp_SON_vars_for_nets = []
            for net in self.circuit.get_nets(with_power_ground=False):
                if not net.is_io_net():
                    continue
                for k in range(net.num_terminals(), self.net_to_flow_cnt[net.name]):
                    bv = self.node_is_SON_vars[net.name][k].get(node)
                    if bv is not None:
                        tmp_SON_vars_for_nets.append(bv)
            if tmp_SON_vars_for_nets:
                self.opt.Add(sum(tmp_SON_vars_for_nets) <= 1)

    def _prohibit_multiple_SONs_same_column(self):
        """At most one net's SON per (face metal, column)."""
        self.opt.log_comment("Enforcing an SON cannot be aligned at the same column ...")
        per_layer_col = defaultdict(list)
        for node in self._all_son_nodes():
            layer_idx, _row, col = node
            for net in self.circuit.get_nets(with_power_ground=False):
                if not net.is_io_net():
                    continue
                for k in range(net.num_terminals(), self.net_to_flow_cnt[net.name]):
                    bv = self.node_is_SON_vars[net.name][k].get(node)
                    if bv is not None:
                        per_layer_col[(layer_idx, col)].append(bv)
        for key, bvs in per_layer_col.items():
            if len(bvs) > 1:
                self.opt.Add(sum(bvs) <= 1)
