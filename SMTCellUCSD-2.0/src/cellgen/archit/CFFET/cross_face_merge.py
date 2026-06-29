"""
Cross-face GM / DM / FDM merge for CFFET dual-face placement (v2).

Convention A (see docs/skills/cffet-layer-nomenclature/SKILL.md):

| Type | Condition |
|------|-----------|
| GM   | Cross-face gate merge — same gate column (x_eq) |
| DM   | Cross-face drain merge — aligned S/D (x_eq) |
| FDM  | Field drain merge — misaligned S/D (|x1-x2| = db_dist), +1 CPP |

Refs: ASPDAC'26 FFET flow; docs/superpowers/specs/2026-06-27-cffet-design.md §4.3 v2.
"""

from loguru import logger

from src.cellgen.core.entity import Model

FRONT_TIER_NAMES = frozenset({"FBOTPC", "FTOPPC"})
BACK_TIER_NAMES = frozenset({"BBOTPC", "BTOPPC"})

_DB_TYPE_TO_DIST = {"SDB": 2, "DDB": 4}


def _face_tier_indices(instance, face: str):
    names = FRONT_TIER_NAMES if face == "front" else BACK_TIER_NAMES
    out = []
    for name in names:
        try:
            out.append(instance.lgg.layer_index(name))
        except KeyError:
            pass
    return out


def _reify_on_face(instance, tran_name: str, face: str, tag: str):
    """True when ``tran_name`` is placed on ``face`` ('front' | 'back')."""
    slot_vars = [
        instance.placed_tran_zi_vars[(tran_name, zi)]
        for zi in _face_tier_indices(instance, face)
        if (tran_name, zi) in instance.placed_tran_zi_vars
    ]
    on_face = instance.opt.NewBoolVar(f"{tag}_on_{face}_{tran_name}")
    if not slot_vars:
        instance.opt.Add(on_face == 0)
        return on_face
    instance.opt.AddBoolOr(slot_vars).OnlyEnforceIf(on_face)
    instance.opt.Add(sum(slot_vars) == 0).OnlyEnforceIf(on_face.Not())
    return on_face


def _reify_cross_face(instance, t1: str, t2: str, tag: str):
    """True when the two devices land on opposite faces."""
    f1 = _reify_on_face(instance, t1, "front", tag)
    f2 = _reify_on_face(instance, t2, "front", tag)
    cf = instance.opt.NewBoolVar(f"cross_face_{tag}_{t1}_{t2}")
    t1_front_t2_back = instance.opt.NewBoolVar(f"cf_{tag}_{t1}f_{t2}b")
    instance.opt.AddBoolAnd([f1, f2.Not()]).OnlyEnforceIf(t1_front_t2_back)
    instance.opt.AddBoolOr([f1.Not(), f2]).OnlyEnforceIf(t1_front_t2_back.Not())
    t2_front_t1_back = instance.opt.NewBoolVar(f"cf_{tag}_{t2}f_{t1}b")
    instance.opt.AddBoolAnd([f2, f1.Not()]).OnlyEnforceIf(t2_front_t1_back)
    instance.opt.AddBoolOr([f2.Not(), f1]).OnlyEnforceIf(t2_front_t1_back.Not())
    instance.opt.AddBoolOr([t1_front_t2_back, t2_front_t1_back]).OnlyEnforceIf(cf)
    instance.opt.AddBoolAnd([t1_front_t2_back.Not(), t2_front_t1_back.Not()]).OnlyEnforceIf(cf.Not())
    return cf


def _db_dist(instance) -> int:
    db_type = instance.c_tech.diffusion_break_type
    if db_type not in _DB_TYPE_TO_DIST:
        raise ValueError(f"Unknown diffusion_break_type {db_type!r}")
    return _DB_TYPE_TO_DIST[db_type]


def pairwise_cross_face_merge(instance):
    """
    Create cross-face GM / DM / FDM reifiers for transistor pairs sharing a net.

    Populates:
        gm_cf_pair_vars   — gate merge across faces
        dm_cf_pair_vars   — aligned drain merge across faces
        fdm_pair_vars     — field drain merge (misaligned), fin-cut path
        net_cross_face_merge_vars — per-net OR of all merge vars on that net
    """
    instance.opt.log_comment("CFFET cross-face GM/DM/FDM merge variables")
    db_dist = _db_dist(instance)
    instance.gm_cf_pair_vars = {}
    instance.dm_cf_pair_vars = {}
    instance.fdm_pair_vars = {}
    instance.net_cross_face_merge_vars = {}
    instance.net_cross_face_pair_vars = {}

    tmp_tran = sorted(instance.circuit.transistors.values())
    for i, tran_1 in enumerate(tmp_tran):
        x1 = instance.transistor_vars[tran_1.name].x_var
        flip1 = instance.transistor_vars[tran_1.name].flip_var
        for tran_2 in tmp_tran[i + 1:]:
            x2 = instance.transistor_vars[tran_2.name].x_var
            flip2 = instance.transistor_vars[tran_2.name].flip_var
            tag = f"{tran_1.name}_{tran_2.name}"
            cross_face = _reify_cross_face(instance, tran_1.name, tran_2.name, tag)

            x_eq = instance.opt.NewBoolVar(f"x_eq_cf_{tag}")
            instance.opt.Add(x1 == x2).OnlyEnforceIf(x_eq)
            instance.opt.Add(x1 != x2).OnlyEnforceIf(x_eq.Not())

            flip_eq = instance.opt.NewBoolVar(f"flip_eq_cf_{tag}")
            instance.opt.Add(flip1 == flip2).OnlyEnforceIf(flip_eq)
            instance.opt.Add(flip1 != flip2).OnlyEnforceIf(flip_eq.Not())

            # Adjacent columns (SDB spacing) — FDM path.
            x_left = instance.opt.NewBoolVar(f"x_left_cf_{tag}")
            instance.opt.Add(x1 + db_dist == x2).OnlyEnforceIf(x_left)
            instance.opt.Add(x1 + db_dist != x2).OnlyEnforceIf(x_left.Not())
            x_right = instance.opt.NewBoolVar(f"x_right_cf_{tag}")
            instance.opt.Add(x2 + db_dist == x1).OnlyEnforceIf(x_right)
            instance.opt.Add(x2 + db_dist != x1).OnlyEnforceIf(x_right.Not())
            x_adj = instance.opt.NewBoolVar(f"x_adj_cf_{tag}")
            instance.opt.AddBoolOr([x_left, x_right]).OnlyEnforceIf(x_adj)
            instance.opt.AddBoolAnd([x_left.Not(), x_right.Not()]).OnlyEnforceIf(x_adj.Not())

            for net in instance.circuit.get_nets(with_power_ground=False):
                conn = net.connected_transistors
                pins1 = {p for t, p in conn if t == tran_1.name}
                pins2 = {p for t, p in conn if t == tran_2.name}
                if not pins1 or not pins2:
                    continue
                sd_pins = {"source", "drain"}
                if pins1 & {"gate"} and pins2 & {"gate"}:
                    if tran_1.model == tran_2.model:
                        continue
                    key = f"gm_cf_{tran_1.name}_{tran_2.name}_{net.name}"
                    gm = instance.opt.NewBoolVar(key)
                    instance.gm_cf_pair_vars[key] = gm
                    instance.opt.AddBoolAnd([cross_face, x_eq]).OnlyEnforceIf(gm)
                    instance.opt.AddBoolOr([cross_face.Not(), x_eq.Not()]).OnlyEnforceIf(gm.Not())
                    instance.net_cross_face_merge_vars.setdefault(net.name, []).append(gm)
                    instance.net_cross_face_pair_vars.setdefault(net.name, []).append(cross_face)

                if not (pins1 & sd_pins and pins2 & sd_pins):
                    continue

                # Terminal compatibility for S/D merge (mirror intra-tier ds rules).
                is_same_terminal = (
                    ("source" in pins1 and "source" in pins2)
                    or ("drain" in pins1 and "drain" in pins2)
                )
                flip_lit = flip_eq if is_same_terminal else flip_eq.Not()

                dm_key = f"dm_cf_{tran_1.name}_{tran_2.name}_{net.name}"
                dm = instance.opt.NewBoolVar(dm_key)
                instance.dm_cf_pair_vars[dm_key] = dm
                instance.opt.AddBoolAnd([cross_face, x_eq, flip_lit]).OnlyEnforceIf(dm)
                instance.opt.AddBoolOr([cross_face.Not(), x_eq.Not(), flip_lit.Not()]).OnlyEnforceIf(dm.Not())
                instance.net_cross_face_merge_vars.setdefault(net.name, []).append(dm)

                fdm_key = f"fdm_{tran_1.name}_{tran_2.name}_{net.name}"
                fdm = instance.opt.NewBoolVar(fdm_key)
                instance.fdm_pair_vars[fdm_key] = fdm
                instance.opt.AddBoolAnd([cross_face, x_adj, flip_lit]).OnlyEnforceIf(fdm)
                instance.opt.AddBoolOr([cross_face.Not(), x_adj.Not(), flip_lit.Not()]).OnlyEnforceIf(fdm.Not())
                instance.net_cross_face_merge_vars.setdefault(net.name, []).append(fdm)

                instance.net_cross_face_pair_vars.setdefault(net.name, []).append(cross_face)

    logger.info(
        f"\t==\t[CFFET] cross-face merge vars: "
        f"GM={len(instance.gm_cf_pair_vars)}, "
        f"DM={len(instance.dm_cf_pair_vars)}, "
        f"FDM={len(instance.fdm_pair_vars)}"
    )


def enforce_cross_face_merge_obligation(instance):
    """
    Per cross-face signal net: if any device pair spans faces, require at least
    one of {GM, DM, FDM} on that net.
    """
    instance.opt.log_comment("CFFET cross-face merge obligation (GM|DM|FDM)")
    obligated = 0
    for net in instance.circuit.get_nets(with_power_ground=False):
        span_vars = instance.net_cross_face_pair_vars.get(net.name, [])
        merge_vars = instance.net_cross_face_merge_vars.get(net.name, [])
        if not span_vars or not merge_vars:
            continue
        spans = instance.opt.NewBoolVar(f"net_spans_faces_{net.name}")
        instance.opt.AddBoolOr(span_vars).OnlyEnforceIf(spans)
        instance.opt.Add(sum(span_vars) == 0).OnlyEnforceIf(spans.Not())
        instance.opt.AddBoolOr(merge_vars).OnlyEnforceIf(spans)
        obligated += 1
    logger.info(f"\t==\t[CFFET] {obligated} net(s) with cross-face merge obligation")
