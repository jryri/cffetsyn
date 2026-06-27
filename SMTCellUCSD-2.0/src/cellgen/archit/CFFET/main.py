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

v1 scope (Scope C subset):
  - P2  Dual-rail 4-tier + STV graph, dual M0ICPD power-row bans.
  - P3  Front-block device-tier resolution (PMOS=FTOPPC, NMOS=FBOTPC); the CFET
        column-grid / MIV / long-via model applies per block unchanged. Full
        dual-face z_var fan-out (placing devices on EITHER face) is a documented
        follow-up; v1 places every device on the front block.
  - P4  AtMostOne FMIV/BMIV per column; AtMostOne STV per column; dual
        ``_ban_signal_on_power_rows`` (inherited, driven by tech layer queries).
  - P5  ``_require_cross_face_merge`` classifies cross-face signal nets and
        enforces a gate/drain merge obligation. Inert in v1 (no device lands on
        the back block) but active once dual-face placement is enabled.
  - P6  ``pin_face`` cell_config schema (config.py). Output dual-face SON is a
        documented follow-up; v1 keeps the inherited M1 SON model.

See docs/superpowers/specs/2026-06-27-cffet-design.md.
"""

import os
from collections import defaultdict

from loguru import logger

from src.cellgen.archit.CFET.main import CFET
from src.cellgen.archit.CFFET.tech import CFFET_Tech
from src.cellgen.archit.CFFET.util import write_cffet_result
from src.cellgen.core.entity import Model
from src.cellgen.core.util import log_variable_info
from ortools.sat.python import cp_model


class CFFET(CFET):
    """Formulate and place CFFET cells (Flip-FET, dual CFET blocks)."""

    def _print_banner(self):
        logger.info("SMTCell CFFET orchestrator (extends CFET, dual-face Flip-FET)")

    def _maybe_write_results(self):
        """Write the .res result + .var dump on success.

        The CFET layer-by-layer PNG visualizer (``visualize_CFET_4T``) only
        understands the 2-tier CFET stack and raises on CFFET's 4-tier + dual-M0
        transitions (e.g. FBOTPC->FTOPPC). Dual-face GDS/view is P7; until then
        the PNG step is attempted but failures are downgraded to a warning so a
        valid solve is never lost.
        """
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
        try:
            from src.cellgen.postprocess.visualize_CFET_4T import (
                draw_layout_with_pin_and_routing,
                load_results,
            )
            view_dir = os.path.join(self.output_dir, "view")
            os.makedirs(view_dir, exist_ok=True)
            placement, routing = load_results(res_path)
            draw_layout_with_pin_and_routing(
                placement, routing,
                filename=os.path.join(view_dir, f"{subckt}.png"),
            )
        except Exception as exc:  # noqa: BLE001 - visualization is best-effort (P7)
            logger.warning(
                f"[CFFET] dual-face view not yet supported (P7); skipping PNG for "
                f"{subckt}: {exc}"
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

    # ================================================================== #
    # P3 — z (tier) variable + model->tier restriction                   #
    # ================================================================== #

    def _init_transistor_vars(self):
        """
        Build the CFET transistor placement variables, then attach a CFFET
        ``z_var`` (placement-tier IntVar) per transistor and restrict it to the
        tier(s) legal for the device model.

        Enabling ``uses_tier_placement`` switches the shared ``accelerate``
        helpers to their tier-gated form (z_eq reified from ``z_var``), making
        the CFFET architecture genuinely tier-aware. v1 restricts ``z_var`` to
        the FRONT face tier matching each device (PMOS->FTOPPC, NMOS->FBOTPC),
        so the proven front-block placement is unchanged while the z-axis seam
        is in place for dual-face (P-follow-up): widening
        ``_cffet_model_tier_restriction`` to also admit the back tiers
        (BTOPPC/BBOTPC) is all that is needed to place devices on either face.
        """
        # Must be set BEFORE accelerate's tier-gated constraints are emitted
        # (they run later, in _build_constraints) and before z_eq is reified.
        self.uses_tier_placement = True
        super()._init_transistor_vars()
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

    def _init_tier_vars(self):
        """Create one z (tier) IntVar per transistor and restrict it per model."""
        self.opt.log_comment("CFFET tier (z) variables")
        zi_domain = cp_model.Domain.FromValues(self._placement_tier_indices())
        self.placed_tran_zi_vars = {}
        for tran in self.circuit.transistors.values():
            tvar = self.transistor_vars[tran.name]
            tvar.z_var = self.opt.NewIntVarFromDomain(zi_domain, f"{tran.name}_z")
        self._cffet_model_tier_restriction()

    def _cffet_model_tier_restriction(self):
        """
        Constrain each transistor's ``z_var`` to the placement tier(s) legal for
        its device model via AllowedAssignments.

        v1 (front face): PMOS -> {FTOPPC}, NMOS -> {FBOTPC}. Widen the allowed
        sets to include the back tiers (PMOS->BTOPPC, NMOS->BBOTPC) to unlock
        true dual-face placement.
        """
        pmos_tiers = [self.lgg.layer_index(self.pmos_layer)]
        nmos_tiers = [self.lgg.layer_index(self.nmos_layer)]
        for tran in self.circuit.transistors.values():
            z = self.transistor_vars[tran.name].z_var
            allowed = pmos_tiers if tran.model == Model.PMOS else nmos_tiers
            self.opt.AddAllowedAssignments([z], [(t,) for t in allowed])

    # ================================================================== #
    # P4 — dual intra-block MIV + inter-block STV column constraints     #
    # ================================================================== #

    def _routing_constraints(self):
        """Inherit the full CFET routing pipeline, then add CFFET-specific
        inter-block (STV) and cross-face-merge constraints."""
        super()._routing_constraints()
        self._only_one_stv_per_col()
        self._require_cross_face_merge()

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
    # P5 — cross-face merge obligation (gate / drain)                    #
    # ================================================================== #

    def _net_touches_face(self, net, face):
        """Return True if any device connected to ``net`` is placed on ``face``
        ('front' | 'back'). In v1 every device is on the front face, so this is
        True only for 'front'. Generalizes once dual-face placement lands."""
        front_layers = {"FBOTPC", "FTOPPC"}
        back_layers = {"BBOTPC", "BTOPPC"}
        target = front_layers if face == "front" else back_layers
        for tran_name, _pin in net.connected_transistors:
            tran = self.circuit.transistors[tran_name]
            layer = self.pmos_layer if tran.model == Model.PMOS else self.nmos_layer
            if layer in target:
                return True
        return False

    def _require_cross_face_merge(self):
        """
        For every cross-face signal net (terminals on both the back AND the
        front block) require at least one legal cross-face merge: a shared gate
        column (GM) or a shared drain (col,row) (DM). Connectivity across the
        face boundary is then carried by the merge or by an STV/MIV chain.

        v1 is single-face, so no net is cross-face and this adds no constraints;
        the method is the activation point for dual-face placement (P5+).
        """
        self.opt.log_comment("Enforcing cross-face merge obligation ...")
        cross_face_nets = [
            net for net in self.circuit.get_nets(with_power_ground=False)
            if self._net_touches_face(net, "front") and self._net_touches_face(net, "back")
        ]
        if not cross_face_nets:
            logger.info("\t==\t[CFFET] No cross-face signal nets (single-face v1 placement)")
            return
        logger.info(
            f"\t==\t[CFFET] {len(cross_face_nets)} cross-face net(s) require GM/DM merge"
        )
        # Gate-merge: a cross-face net whose gate terminals share a column is
        # satisfied via the shared poly column. Drain-merge: shared (col,row).
        # Both are expressed through the existing pairwise gate/diffusion share
        # selectors, so require at least one of the net's share vars active.
        for net in cross_face_nets:
            share_vars = []
            for key, var in getattr(self, "gate_share_pair_vars", {}).items():
                if key.endswith(f"_{net.name}"):
                    share_vars.append(var)
            for key, var in getattr(self, "ds_pair_vars", {}).items():
                if key.endswith(f"_{net.name}"):
                    share_vars.append(var)
            if share_vars:
                self.opt.AddBoolOr(share_vars)
