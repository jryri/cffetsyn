"""
CFFET NPNP tier utilization — FFET-inspired placement objectives.

FFET (Guo TED'25) places N logic and P logic on opposite wafer faces and
distributes input pins evenly across FS/BS.  For CFFET's 4-tier NPNP stack
(BBOTPC, BTOPPC, FBOTPC, FTOPPC), we encourage:

  1. Occupying as many distinct placement tiers as the cell topology allows.
  2. Spreading same-polarity devices across back and front blocks when the
     cell has multiple PMOS or multiple NMOS (NAND2-style NPNP usage).

Soft objectives only; CPP minimization remains dominant (weight 1000).
"""

from __future__ import annotations

from loguru import logger

from src.cellgen.core.entity import Model


def _tier_index(instance, tier_name: str) -> int:
    return instance.lgg.layer_index(tier_name)


def _block_tiers(instance) -> tuple[tuple[str, str], tuple[str, str]]:
    """(back_nm, back_pm), (front_nm, front_pm) tier names for P_on_N stack."""
    bpc = instance.c_tech.get_bpc_tiers()
    pc = instance.c_tech.get_pc_tiers()
    back_nm, front_nm = bpc[0], bpc[1]
    back_pm, front_pm = pc[0], pc[1]
    return (back_nm, back_pm), (front_nm, front_pm)


def init_npvp_utilization_vars(instance) -> None:
    """
    Create reified tier-occupancy and per-polarity block-spread variables.

    Populates on ``instance``:
      - ``tier_occupied_vars[zi]`` — bool, true iff any device uses tier *zi*
      - ``npvp_spread_vars[model]`` — bool, true iff devices of *model* occupy
        BOTH back and front block tiers (when count >= 2)
      - ``npvp_block_imbalance_var`` — |back devices − front devices|
    """
    if not getattr(instance, "placed_tran_zi_vars", None):
        return

    instance.opt.log_comment("CFFET NPNP tier utilization variables (FFET-inspired)")
    (back_nm, back_pm), (front_nm, front_pm) = _block_tiers(instance)
    back_zis = {_tier_index(instance, back_nm), _tier_index(instance, back_pm)}
    front_zis = {_tier_index(instance, front_nm), _tier_index(instance, front_pm)}

    instance.tier_occupied_vars = {}
    trans = list(instance.circuit.transistors.values())
    for zi in instance.plc_zi:
        placed_on_zi = [
            instance.placed_tran_zi_vars[(t.name, zi)] for t in trans
        ]
        occupied = instance.opt.NewBoolVar(f"tier_occupied_zi{zi}")
        instance.tier_occupied_vars[zi] = occupied
        instance.opt.Add(sum(placed_on_zi) >= 1).OnlyEnforceIf(occupied)
        instance.opt.Add(sum(placed_on_zi) == 0).OnlyEnforceIf(occupied.Not())

    instance.npvp_spread_vars = {}
    model_to_tiers = {
        Model.PMOS: (back_pm, front_pm),
        Model.NMOS: (back_nm, front_nm),
    }
    for model, (back_name, front_name) in model_to_tiers.items():
        model_trans = [t for t in trans if t.model == model]
        if len(model_trans) < 2:
            continue
        back_zi = _tier_index(instance, back_name)
        front_zi = _tier_index(instance, front_name)
        back_used = instance.opt.NewBoolVar(f"npvp_{model.name}_back_used")
        front_used = instance.opt.NewBoolVar(f"npvp_{model.name}_front_used")
        spread = instance.opt.NewBoolVar(f"npvp_{model.name}_spread")
        instance.npvp_spread_vars[model] = spread

        back_placed = [
            instance.placed_tran_zi_vars[(t.name, back_zi)] for t in model_trans
        ]
        front_placed = [
            instance.placed_tran_zi_vars[(t.name, front_zi)] for t in model_trans
        ]
        instance.opt.Add(sum(back_placed) >= 1).OnlyEnforceIf(back_used)
        instance.opt.Add(sum(back_placed) == 0).OnlyEnforceIf(back_used.Not())
        instance.opt.Add(sum(front_placed) >= 1).OnlyEnforceIf(front_used)
        instance.opt.Add(sum(front_placed) == 0).OnlyEnforceIf(front_used.Not())
        instance.opt.AddBoolAnd([back_used, front_used]).OnlyEnforceIf(spread)
        instance.opt.AddBoolOr([back_used.Not(), front_used.Not()]).OnlyEnforceIf(
            spread.Not()
        )

    n = len(trans)
    if n > 0:
        back_count = instance.opt.NewIntVar(0, n, "npvp_back_count")
        front_count = instance.opt.NewIntVar(0, n, "npvp_front_count")
        back_terms = []
        front_terms = []
        for t in trans:
            for zi in back_zis:
                back_terms.append(instance.placed_tran_zi_vars[(t.name, zi)])
            for zi in front_zis:
                front_terms.append(instance.placed_tran_zi_vars[(t.name, zi)])
        instance.opt.Add(back_count == sum(back_terms))
        instance.opt.Add(front_count == sum(front_terms))
        imbalance = instance.opt.NewIntVar(0, n, "npvp_block_imbalance")
        instance.opt.AddAbsEquality(imbalance, back_count - front_count)
        instance.npvp_block_imbalance_var = imbalance
    else:
        instance.npvp_block_imbalance_var = None

    logger.info(
        f"\t==\t[CFFET] NPNP util: {len(instance.tier_occupied_vars)} tier(s), "
        f"{len(instance.npvp_spread_vars)} spread group(s)"
    )
