"""
Intra-tier inter-row merge for CFFET multi-row placement (v3).

When devices on the same placement tier (z) share a net but sit on different
placement y rows, connectivity is via inter-row structures (ASPDAC FFET):

| Type | Condition |
|------|-----------|
| IRGM | Gate net — same column (x_eq), different y, same z |
| IRMD | S/D net — same column (x_eq), different y, same z, flip match |

Refs: ASPDAC'26 FFET inter-row MD / gate connections.
"""

from loguru import logger

from src.cellgen.core.accelerate import _reify_z_eq


def _reify_y_neq(instance, t1: str, t2: str, tag: str):
    y1 = instance.transistor_vars[t1].y_var
    y2 = instance.transistor_vars[t2].y_var
    y_eq = instance.opt.NewBoolVar(f"y_eq_{tag}_{t1}_{t2}")
    instance.opt.Add(y1 == y2).OnlyEnforceIf(y_eq)
    instance.opt.Add(y1 != y2).OnlyEnforceIf(y_eq.Not())
    y_neq = instance.opt.NewBoolVar(f"y_neq_{tag}_{t1}_{t2}")
    instance.opt.AddImplication(y_neq, y_eq.Not())
    instance.opt.AddImplication(y_eq, y_neq.Not())
    return y_neq


def pairwise_inter_row_merge(instance):
    """
    IRGM / IRMD vars for same-tier, different-y pairs on a shared net.

    Populates:
        irgm_pair_vars, irmd_pair_vars
        net_inter_row_merge_vars, net_inter_row_span_vars
    """
    if len(getattr(instance, "nmos_placeable_row_indices", [0])) <= 1:
        return

    instance.opt.log_comment("CFFET inter-row GM/MD merge variables")
    instance.irgm_pair_vars = {}
    instance.irmd_pair_vars = {}
    instance.net_inter_row_merge_vars = {}
    instance.net_inter_row_span_vars = {}

    trans = sorted(instance.circuit.transistors.values(), key=lambda t: t.name)
    for i, t1 in enumerate(trans):
        x1 = instance.transistor_vars[t1.name].x_var
        flip1 = instance.transistor_vars[t1.name].flip_var
        for t2 in trans[i + 1:]:
            x2 = instance.transistor_vars[t2.name].x_var
            flip2 = instance.transistor_vars[t2.name].flip_var
            tag = f"{t1.name}_{t2.name}"

            x_eq = instance.opt.NewBoolVar(f"x_eq_ir_{tag}")
            instance.opt.Add(x1 == x2).OnlyEnforceIf(x_eq)
            instance.opt.Add(x1 != x2).OnlyEnforceIf(x_eq.Not())

            flip_eq = instance.opt.NewBoolVar(f"flip_eq_ir_{tag}")
            instance.opt.Add(flip1 == flip2).OnlyEnforceIf(flip_eq)
            instance.opt.Add(flip1 != flip2).OnlyEnforceIf(flip_eq.Not())

            z_eq = _reify_z_eq(instance, t1.name, t2.name, tag="ir")
            y_neq = _reify_y_neq(instance, t1.name, t2.name, tag)

            for net in instance.circuit.get_nets(with_power_ground=False):
                conn = net.connected_transistors
                pins1 = {p for t, p in conn if t == t1.name}
                pins2 = {p for t, p in conn if t == t2.name}
                if not pins1 or not pins2:
                    continue

                base = [y_neq, x_eq, z_eq]

                if pins1 & {"gate"} and pins2 & {"gate"}:
                    if t1.model == t2.model:
                        continue
                    key = f"irgm_{t1.name}_{t2.name}_{net.name}"
                    var = instance.opt.NewBoolVar(key)
                    instance.irgm_pair_vars[key] = var
                    instance.opt.AddBoolAnd(base).OnlyEnforceIf(var)
                    instance.opt.AddBoolOr([c.Not() for c in base]).OnlyEnforceIf(var.Not())
                    instance.net_inter_row_merge_vars.setdefault(net.name, []).append(var)
                    instance.net_inter_row_span_vars.setdefault(net.name, []).append(y_neq)

                sd = {"source", "drain"}
                if not (pins1 & sd and pins2 & sd):
                    continue

                is_same = (
                    ("source" in pins1 and "source" in pins2)
                    or ("drain" in pins1 and "drain" in pins2)
                )
                flip_lit = flip_eq if is_same else flip_eq.Not()
                key = f"irmd_{t1.name}_{t2.name}_{net.name}"
                var = instance.opt.NewBoolVar(key)
                instance.irmd_pair_vars[key] = var
                instance.opt.AddBoolAnd(base + [flip_lit]).OnlyEnforceIf(var)
                instance.opt.AddBoolOr([c.Not() for c in base + [flip_lit]]).OnlyEnforceIf(var.Not())
                instance.net_inter_row_merge_vars.setdefault(net.name, []).append(var)
                instance.net_inter_row_span_vars.setdefault(net.name, []).append(y_neq)

    logger.info(
        f"\t==\t[CFFET] inter-row merge vars: "
        f"IRGM={len(instance.irgm_pair_vars)}, IRMD={len(instance.irmd_pair_vars)}"
    )


def enforce_inter_row_merge_obligation(instance):
    """If a net spans placement y rows (same-tier pair), require IRGM or IRMD."""
    if not getattr(instance, "net_inter_row_span_vars", None):
        return
    instance.opt.log_comment("CFFET inter-row merge obligation")
    count = 0
    for net in instance.circuit.get_nets(with_power_ground=False):
        span_vars = instance.net_inter_row_span_vars.get(net.name, [])
        merge_vars = instance.net_inter_row_merge_vars.get(net.name, [])
        if not span_vars or not merge_vars:
            continue
        spans = instance.opt.NewBoolVar(f"net_spans_y_{net.name}")
        instance.opt.AddBoolOr(span_vars).OnlyEnforceIf(spans)
        instance.opt.Add(sum(span_vars) == 0).OnlyEnforceIf(spans.Not())
        instance.opt.AddBoolOr(merge_vars).OnlyEnforceIf(spans)
        count += 1
    logger.info(f"\t==\t[CFFET] {count} net(s) with inter-row merge obligation")
