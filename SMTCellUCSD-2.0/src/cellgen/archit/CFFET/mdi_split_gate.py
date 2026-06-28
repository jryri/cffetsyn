"""
MDI (Middle Dielectric Isolation) split-gate support for CFFET.

Split gate at the CFFET center seam (BTOPPC <-> FBOTPC, co-located with STV):
same gate column, PMOS gate on back top tier, NMOS gate on front bottom tier,
isolated by MDI between gate stacks.

Refs: docs/skills/cffet-process-technology/SKILL.md § MDI split gate.
"""

from collections import OrderedDict

from loguru import logger

from src.cellgen.core.entity import Model

_SD_PINS = frozenset({"source", "drain"})


def _net_sd_pins(net, tran_name: str):
    return {p for t, p in net.connected_transistors if t == tran_name}


def _shared_channel_net(tran_p, tran_n, circuit):
    """Return channel net name if PMOS and NMOS share a source/drain net."""
    for net in circuit.get_nets(with_power_ground=False):
        p_pins = _net_sd_pins(net, tran_p.name)
        n_pins = _net_sd_pins(net, tran_n.name)
        if p_pins & _SD_PINS and n_pins & _SD_PINS:
            return net.name
    return None


def gates_are_complementary(gate_p: str, gate_n: str) -> bool:
    """
    Heuristic complement check for TG split-gate pairs (S / S̄ style).

    Returns False unless naming suggests true complementary control nets.
    """
    if gate_p == gate_n:
        return False
    pairs = (
        (gate_p, gate_n),
        (gate_n, gate_p),
    )
    for a, b in pairs:
        if b in (f"{a}N", f"{a}_B", f"{a}BAR", f"{a}#"):
            return True
        if a == "S" and b in ("SN", "S_B", "SB"):
            return True
        if a == "SN" and b == "S":
            return True
    return False


def detect_tg_split_gate_pairs(circuit):
    """
    Find PMOS+NMOS pairs sharing a channel net with different gate nets.

    Returns a list of dicts (candidate transmission-gate legs, not necessarily
    complementary yet — see ``complementary`` flag).
    """
    pairs = []
    pmos_list = [t for t in circuit.transistors.values() if t.model == Model.PMOS]
    nmos_list = [t for t in circuit.transistors.values() if t.model == Model.NMOS]
    for p in pmos_list:
        for n in nmos_list:
            channel = _shared_channel_net(p, n, circuit)
            if channel is None or p.gate == n.gate:
                continue
            pairs.append(
                {
                    "pmos": p.name,
                    "nmos": n.name,
                    "channel_net": channel,
                    "gate_p": p.gate,
                    "gate_n": n.gate,
                    "complementary": gates_are_complementary(p.gate, n.gate),
                }
            )
    return pairs


def _tier_index(instance, tier_name: str):
    return instance.lgg.layer_index(tier_name)


def _device_cols_for_ci(instance, ci: int):
    """LGG column coordinates for SDG slots at placement column ``ci``."""
    return [
        instance.lgg.col_in_layer(instance._plc_layer, ci + offset)
        for offset in (0, 1, 2)
    ]


def _stv_edges_at_ci(instance, ci: int):
    """STV (BTOPPC<->FBOTPC) edge vars whose column overlaps device ``ci``."""
    try:
        bot_idx = _tier_index(instance, "BTOPPC")
        top_idx = _tier_index(instance, "FBOTPC")
    except KeyError:
        return []
    cols = set(_device_cols_for_ci(instance, ci))
    edges = []
    for (u, v), evar in instance.edge_vars.items():
        if {u[0], v[0]} == {bot_idx, top_idx} and u[2] in cols:
            edges.append(evar)
    return edges


def _init_mdi_at_col_vars(instance):
    if getattr(instance, "mdi_at_col_vars", None):
        return
    instance.opt.log_comment("CFFET MDI split-gate column markers")
    instance.mdi_at_col_vars = OrderedDict()
    for ci in instance.plc_ci:
        instance.mdi_at_col_vars[ci] = instance.opt.NewBoolVar(f"mdi_at_col_{ci}")


def _link_stv_when_mdi(instance, ci: int, *, hard: bool):
    """When MDI is declared at ``ci``, require STV at the seam (if edges exist)."""
    stv_edges = _stv_edges_at_ci(instance, ci)
    if not stv_edges:
        return
    mdi = instance.mdi_at_col_vars[ci]
    if hard:
        instance.opt.Add(sum(stv_edges) >= 1).OnlyEnforceIf(mdi)
    else:
        for edge in stv_edges:
            instance.opt.AddImplication(mdi, edge)


def _enforce_pair_at_ci(instance, pair, ci: int, *, btop_zi, fbot_zi):
    """Hard constraints for one complementary TG pair at placement column ``ci``."""
    p_name, n_name = pair["pmos"], pair["nmos"]
    at_col = instance.opt.NewBoolVar(f"mdi_split_{p_name}_{n_name}_ci{ci}")

    col_p = instance.placed_tran_ci_vars[(p_name, ci)]
    col_n = instance.placed_tran_ci_vars[(n_name, ci)]
    p_btop = instance.placed_tran_at_xzi_vars[(p_name, ci, btop_zi)]
    n_fbot = instance.placed_tran_at_xzi_vars[(n_name, ci, fbot_zi)]
    mdi = instance.mdi_at_col_vars[ci]

    gate_col = instance.lgg.col_in_layer(instance._plc_layer, ci + 1)
    gs = instance.gate_share_at_col_vars.get(gate_col)

    lits = [col_p, col_n, p_btop, n_fbot, mdi]
    instance.opt.AddBoolAnd(lits).OnlyEnforceIf(at_col)
    for lit in lits:
        instance.opt.AddImplication(at_col, lit)
    if gs is not None:
        instance.opt.Add(gs == 0).OnlyEnforceIf(at_col)
        instance.opt.AddImplication(at_col, gs.Not())

    _link_stv_when_mdi(instance, ci, hard=True)
    return at_col


def enforce_mdi_split_gate(instance):
    """
    Create ``mdi_at_col_vars`` and optionally hard-enforce MDI split-gate
    placement for complementary TG candidate pairs.

    Config:
        enable_mdi_split_gate (default True for CFFET)
        enforce_mdi_split_gate (default False — opt-in hard constraints)
    """
    if not instance._cfg_get("enable_mdi_split_gate", True):
        return

    _init_mdi_at_col_vars(instance)
    pairs = detect_tg_split_gate_pairs(instance.circuit)
    instance.mdi_split_gate_pairs = pairs
    if not pairs:
        return

    logger.info(f"\t==\t[CFFET] MDI split-gate candidates: {len(pairs)}")

    enforce = instance._cfg_get("enforce_mdi_split_gate", False)
    comp_pairs = [p for p in pairs if p["complementary"]]
    if not comp_pairs:
        return

    try:
        btop_zi = _tier_index(instance, "BTOPPC")
        fbot_zi = _tier_index(instance, "FBOTPC")
    except KeyError:
        return

    if enforce:
        instance.opt.log_comment(
            f"CFFET MDI split-gate hard enforcement ({len(comp_pairs)} pair(s))"
        )
        for pair in comp_pairs:
            placement_vars = [
                _enforce_pair_at_ci(
                    instance, pair, ci, btop_zi=btop_zi, fbot_zi=fbot_zi
                )
                for ci in instance.plc_ci
            ]
            instance.opt.Add(sum(placement_vars) == 1)
    else:
        instance.opt.log_comment(
            "CFFET MDI split-gate soft STV link when mdi_at_col set"
        )
        for ci in instance.plc_ci:
            _link_stv_when_mdi(instance, ci, hard=False)
