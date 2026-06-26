"""Visualise QFET placement results.

QFET stacks four placement tiers - BPC2, BPC1, PC1, PC2 - so this produces
a four-panel figure (one per tier, left->right = bottom->top of the stack):

  BPC2 | BPC1 | PC1 | PC2

Each transistor is drawn as a rectangle at its solved (x, y) position with
source/drain/gate pin circles. Routing segments and vias are overlaid per
tier when present in the .res file. Cross-tier vias (MIV / CA*) are echoed
on every panel so the eye can chase a net up the stack.

PMOS and NMOS may both occupy any of the 4 tiers (no per-model tier
restriction); we colour by model the same way the other FET writers do.

Usage:
    python visualize_QFET_4T.py <results.res> [output.png]
"""

import sys
from collections import defaultdict
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle, Patch
from matplotlib.lines import Line2D


def _nudge_overlap(placed, x, y, threshold, step):
    """Return (dx, dy) that moves (x, y) clear of any prior point in `placed`
    by at most ~3 steps. Deterministic spiral: right, up, left, down, then
    diagonals. Same (x,y) twice always picks the same offset."""
    if not any(abs(px - x) < threshold and abs(py - y) < threshold for px, py in placed):
        placed.append((x, y))
        return 0.0, 0.0
    offsets = [(step, 0), (0, step), (-step, 0), (0, -step),
               (step, step), (-step, step), (step, -step), (-step, -step),
               (2 * step, 0), (0, 2 * step), (-2 * step, 0), (0, -2 * step)]
    for dx, dy in offsets:
        nx, ny = x + dx, y + dy
        if not any(abs(px - nx) < threshold and abs(py - ny) < threshold for px, py in placed):
            placed.append((nx, ny))
            return dx, dy
    placed.append((x, y))
    return 0.0, 0.0


# QFET layer index -> name mapping.
# Layer order (bottom->top), 2-placement-tier + 2 mid-routing build:
#   BM1(0) BM0(1) BPC1(2) H0(3) H1(4) PC1(5) M0(6) M1(7)
LAYER_NAMES = {
    0: "BM1", 1: "BM0",
    2: "BPC1", 3: "H0", 4: "H1", 5: "PC1",
    6: "M0", 7: "M1",
}
NAME_TO_LAYER = {v: k for k, v in LAYER_NAMES.items()}

# The two placement tiers - drawn left->right in this order.
PLACEMENT_TIERS = ("BPC1", "PC1")

# Every LGG layer -> its "host" placement-tier panel. Mirrors
# routing._build_layer_to_tier: each layer is assigned to the placement
# layer closest to it by LGG-idx distance. Mid-routing H0 / H1 split
# between BPC1 (H0 closer to BPC1) and PC1 (H1 closer to PC1).
LAYER_TO_TIER = {
    "BM1":  "BPC1",
    "BM0":  "BPC1",
    "BPC1": "BPC1",
    "H0":   "BPC1",
    "H1":   "PC1",
    "PC1":  "PC1",
    "M0":   "PC1",
    "M1":   "PC1",
}

# Via naming (best-effort; unknown gaps fall back to "V<l1>-<l2>")
VIA_NAMES = {
    (0, 1): "BV0",     # BM1<->BM0
    (1, 2): "BCA1",    # BM0<->BPC1
    (2, 3): "MIV1",    # BPC1<->H0
    (3, 4): "MIV2",    # H0<->H1
    (4, 5): "MIV3",    # H1<->PC1
    (5, 6): "CA1",     # PC1<->M0
    (6, 7): "V0",      # M0<->M1
}


def _via_name(l1, l2):
    key = (min(l1, l2), max(l1, l2))
    return VIA_NAMES.get(key, f"V{l1}-{l2}")


def _layer_of(name):
    """LGG layer index for a layer name; -1 if unknown."""
    return NAME_TO_LAYER.get(name, -1)


# Colours (match the other FET writers' art direction).
PMOS_COLOR = "#E8575780"
NMOS_COLOR = "#4A90D980"
PMOS_EDGE  = "#C0392B"
NMOS_EDGE  = "#2471A3"
# Faint background tint per tier so panels read distinctly
TIER_BG = {
    "BPC1": "#FFF9C4",
    "PC1":  "#E8F5E9",
}


def load_results(filename):
    """Parse a QFET .res file.

    Returns
    -------
    placement : dict[name] -> {x, y, z (layer name), flip, width, height,
                              s_col, s_net, d_col, d_net, g_col, g_net, model}
    routing   : list[dict]  with keys layer_u/v, row_u/v, col_u/v, net
                (empty when QFET routing constraints are not wired)
    tech      : dict[str -> str]  technology parameters
    """
    placement, routing, tech = {}, [], {}
    mode = None

    with open(filename) as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue

            if line.startswith("** Placement Result"):
                mode = "placement"
                next(f, None)  # header
                next(f, None)  # separator
                continue
            if line.startswith("** Cell Information"):
                mode = None
                continue
            if line.startswith("** Routing Result"):
                mode = "routing"
                next(f, None)
                next(f, None)
                continue
            if line.startswith("** Technology Parameters"):
                mode = "tech"
                next(f, None)
                next(f, None)
                continue

            if mode == "placement":
                toks = line.split()
                # QFET writer emits 14 tokens (no site col, z is a string):
                # Name X Y Z Flip Width Height SCol SNet DCol DNet GCol GNet Model
                if len(toks) < 14:
                    continue
                try:
                    name = toks[0]
                    x, y = float(toks[1]), float(toks[2])
                    z = toks[3]
                    flip = toks[4] == "F"
                    width, height = float(toks[5]), float(toks[6])
                    s_col = float(toks[7]) if toks[7] != "-1" else None
                    s_net = toks[8]
                    d_col = float(toks[9]) if toks[9] != "-1" else None
                    d_net = toks[10]
                    g_col = float(toks[11]) if toks[11] != "-1" else None
                    g_net = toks[12]
                    model = toks[13]
                except ValueError:
                    continue

                placement[name] = {
                    "x": x, "y": y, "z": z,
                    "flip": flip, "width": width, "height": height,
                    "s_col": s_col, "s_net": s_net,
                    "d_col": d_col, "d_net": d_net,
                    "g_col": g_col, "g_net": g_net,
                    "model": model,
                }

            elif mode == "routing":
                if "=>" not in raw:
                    continue
                try:
                    left, right = raw.split("=>")
                    lu, ru, cu, net = left.split()
                    lv, rv, cv, _ = right.split()
                except ValueError:
                    continue
                routing.append({
                    "layer_u": int(lu), "row_u": float(ru), "col_u": float(cu),
                    "layer_v": int(lv), "row_v": float(rv), "col_v": float(cv),
                    "net": net,
                })

            elif mode == "tech":
                toks = line.split()
                if len(toks) >= 2:
                    tech[toks[0]] = toks[1]

    return placement, routing, tech


def draw_qfet_layout(placement, routing, tech, filename=None,
                     pin_radius=2.5, track_width=5.0):
    """Draw a four-panel QFET layout visualisation (one panel per placement tier)."""

    # Split placement by tier name.
    tiers = {z: {} for z in PLACEMENT_TIERS}
    for name, info in placement.items():
        z = info["z"]
        if z in tiers:
            tiers[z][name] = info

    # Split routing by panel.
    # Same-LAYER routes (lu == lv) ride their layer's host tier panel via
    # LAYER_TO_TIER - so BM0/BM1 metal renders on the BPC2 panel and M0/M1
    # metal renders on the PC2 panel, instead of leaking into the cross-tier
    # overlay (which would echo a marker on every panel and label it as a
    # bogus "V<l>-<l>" via).
    # Cross-LAYER routes (lu != lv) are the real vias; echoed on every panel
    # so the eye can chase a net up the stack.
    tier_routing = {z: [] for z in PLACEMENT_TIERS}
    cross_tier_routing = []
    for r in routing:
        lu, lv = r["layer_u"], r["layer_v"]
        if lu == lv:
            host_tier = LAYER_TO_TIER.get(LAYER_NAMES.get(lu))
            if host_tier is not None:
                tier_routing[host_tier].append(r)
            else:
                cross_tier_routing.append(r)
        else:
            cross_tier_routing.append(r)

    # Bounds per tier + uniform y across panels.
    def _bounds(pl, rt):
        xs, ys = [], []
        for info in pl.values():
            xs += [info["x"], info["x"] + info["width"]]
            ys += [info["y"], info["y"] + info["height"]]
        for r in rt:
            xs += [r["col_u"], r["col_v"]]
            ys += [r["row_u"], r["row_v"]]
        if not xs:
            return 0, 100, 0, 100
        return min(xs), max(xs), min(ys), max(ys)

    bounds = {z: _bounds(tiers[z], tier_routing[z]) for z in PLACEMENT_TIERS}
    min_x = min(b[0] for b in bounds.values())
    max_x = max(b[1] for b in bounds.values())
    min_y = min(b[2] for b in bounds.values())
    max_y = max(b[3] for b in bounds.values())
    margin = 30

    # Figure setup (N side-by-side panels, one per placement tier).
    n_tiers = len(PLACEMENT_TIERS)
    panel_w = max((max_x - min_x + 2 * margin) / 25, 3)
    panel_h = max((max_y - min_y + 2 * margin) / 25, 4)
    fig, axes_list = plt.subplots(
        1, n_tiers, figsize=(panel_w * n_tiers + 1.5, panel_h), dpi=150,
        gridspec_kw={"width_ratios": [1] * n_tiers},
    )
    if n_tiers == 1:
        axes_list = [axes_list]
    axes = dict(zip(PLACEMENT_TIERS, axes_list))

    for z, ax in axes.items():
        n = sum(1 for _ in tiers[z])
        ax.set_title(f"Tier {z} — {n} transistor(s)", fontsize=10, fontweight="bold")
        ax.set_xlabel("X (col coord)")
        ax.set_ylabel("Y (row coord)")
        ax.set_aspect("equal", adjustable="box")
        ax.grid(True, linestyle="--", linewidth=0.3, alpha=0.5)

    # Net colour map (shared across panels).
    all_nets = set()
    for info in placement.values():
        for k in ("s_net", "d_net", "g_net"):
            if info[k]:
                all_nets.add(info[k])
    for r in routing:
        all_nets.add(r["net"])
    all_nets = sorted(all_nets)
    cmap = plt.get_cmap("tab10")
    net_colors = {n: cmap(i % cmap.N) for i, n in enumerate(all_nets)}

    # Per-axis log of placed label centers (for overlap-nudging across both
    # same-tier-routing labels and cross-tier-via labels). Threshold ~ a half
    # M0 pitch; step ~ a third of that - small enough to stay near the wire,
    # large enough to render fully readable.
    label_centers = defaultdict(list)
    nudge_thresh = 1.0
    nudge_step   = 5.0

    # Draw per tier.
    for z in PLACEMENT_TIERS:
        ax = axes[z]
        pl = tiers[z]
        rt = tier_routing[z]

        # 1) Tier background band
        ax.add_patch(Rectangle(
            (min_x - margin + 5, min_y - margin + 5),
            (max_x - min_x) + 2 * margin - 10,
            (max_y - min_y) + 2 * margin - 10,
            facecolor=TIER_BG[z], edgecolor="none", alpha=0.35, zorder=0,
        ))

        # 2) Transistor bodies + pins
        for name, info in pl.items():
            x, y = info["x"], info["y"]
            w, h = info["width"], info["height"]
            flip = info["flip"]
            is_pmos = info["model"].upper() == "PMOS"

            body_color = PMOS_COLOR if is_pmos else NMOS_COLOR
            edge_color = PMOS_EDGE if is_pmos else NMOS_EDGE
            txt_color  = edge_color

            ax.add_patch(Rectangle(
                (x, y), w, h, edgecolor=edge_color, facecolor=body_color,
                lw=1.2, alpha=0.6, zorder=2,
            ))
            flip_tag = " (F)" if flip else ""
            ax.text(x + w / 2, y + h / 2, f"{name}{flip_tag}",
                    ha="center", va="center", fontsize=6,
                    fontweight="bold", color=txt_color, zorder=3)

            # Pin layout - flip swaps S/D
            if flip:
                pins = [
                    ("D", info["d_col"], info["d_net"]),
                    ("G", info["g_col"], info["g_net"]),
                    ("S", info["s_col"], info["s_net"]),
                ]
            else:
                pins = [
                    ("S", info["s_col"], info["s_net"]),
                    ("G", info["g_col"], info["g_net"]),
                    ("D", info["d_col"], info["d_net"]),
                ]

            for _pin_label, col_val, net in pins:
                if col_val is None:
                    continue
                py_top, py_bot = y + h, y
                for py in (py_top, py_bot):
                    ax.add_patch(Circle(
                        (col_val, py), radius=pin_radius,
                        edgecolor=edge_color, facecolor="white",
                        lw=0.8, zorder=5,
                    ))
                if net:
                    ax.text(col_val, py_top + pin_radius + 2, net,
                            ha="center", va="bottom", fontsize=5,
                            fontstyle="italic", fontweight="bold",
                            color=txt_color, zorder=6)

        # 3) Same-tier routing segments
        for r in rt:
            x0, y0 = r["col_u"], r["row_u"]
            x1, y1 = r["col_v"], r["row_v"]
            col = net_colors.get(r["net"], "gray")
            layer_name = LAYER_NAMES.get(r["layer_u"], f"L{r['layer_u']}")
            dx, dy = x1 - x0, y1 - y0
            if abs(dy) < 1e-6:                                 # horizontal
                ax.add_patch(Rectangle(
                    (min(x0, x1), y0 - track_width / 2),
                    abs(dx), track_width,
                    facecolor=col, edgecolor="black", lw=0.3,
                    alpha=0.55, zorder=4,
                ))
            elif abs(dx) < 1e-6:                               # vertical
                ax.add_patch(Rectangle(
                    (x0 - track_width / 2, min(y0, y1)),
                    track_width, abs(dy),
                    facecolor=col, edgecolor="black", lw=0.3,
                    alpha=0.55, zorder=4,
                ))
            else:
                ax.plot([x0, x1], [y0, y1], color=col, lw=1.2,
                        alpha=0.6, zorder=4)
            xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
            dxn, dyn = _nudge_overlap(label_centers[id(ax)], xm, ym,
                                      nudge_thresh, nudge_step)
            ax.text(xm + dxn, ym + dyn, f"{r['net']},{layer_name}",
                    ha="center", va="center", fontsize=4.5,
                    color="white", zorder=7,
                    bbox=dict(facecolor=col, edgecolor="none",
                              boxstyle="round,pad=0.15", alpha=0.6))

        ax.set_xlim(min_x - margin, max_x + margin)
        ax.set_ylim(min_y - margin, max_y + margin)

    # Cross-tier via overlays (bridge-only).
    # A via is drawn ONLY on the panels of its two host tiers - the tier
    # the lower endpoint rides on AND the tier the upper endpoint rides on.
    # When both endpoints map to the same host tier (e.g. BV0 = BM1<->BM0, both
    # ride BPC2), the via is drawn once on that one panel.
    for r in cross_tier_routing:
        col = net_colors.get(r["net"], "gray")
        l_lo = min(r["layer_u"], r["layer_v"])
        l_hi = max(r["layer_u"], r["layer_v"])
        via_label = _via_name(l_lo, l_hi)
        is_virtual = (l_hi - l_lo) > 1
        if is_virtual:
            via_label = f"VIRT({LAYER_NAMES.get(l_lo,'?')}↔{LAYER_NAMES.get(l_hi,'?')})"
        marker  = "s" if is_virtual else "D"
        fc_lbl  = "#E1BEE7" if is_virtual else "lightyellow"
        ec_lbl  = "#6A1B9A" if is_virtual else "orange"

        host_tiers = []
        for lyr in (l_lo, l_hi):
            host = LAYER_TO_TIER.get(LAYER_NAMES.get(lyr))
            if host and host not in host_tiers:
                host_tiers.append(host)
        if not host_tiers:
            continue

        for tier_name in host_tiers:
            ax = axes[tier_name]
            ax.plot(r["col_u"], r["row_u"], marker=marker, color=col,
                    markersize=track_width * 2, markeredgewidth=1.5,
                    markeredgecolor="black", alpha=0.7, zorder=10)
            lx, ly = r["col_u"], r["row_u"] - track_width * 1.5
            dxn, dyn = _nudge_overlap(label_centers[id(ax)], lx, ly,
                                      nudge_thresh, nudge_step)
            ax.text(lx + dxn, ly + dyn,
                    f"{r['net']},{via_label}", ha="center", va="top",
                    fontsize=5, fontweight="bold",
                    bbox=dict(facecolor=fc_lbl, edgecolor=ec_lbl,
                              boxstyle="round,pad=0.2", alpha=0.8),
                    zorder=11)

    # Legend.
    handles = [
        Patch(facecolor=net_colors[n], edgecolor="black", label=n, alpha=0.7)
        for n in all_nets
    ]
    handles += [
        Patch(facecolor=PMOS_COLOR, edgecolor=PMOS_EDGE, label="PMOS"),
        Patch(facecolor=NMOS_COLOR, edgecolor=NMOS_EDGE, label="NMOS"),
        Line2D([0], [0], marker="D", color="gray", linestyle="None",
               markersize=6, markeredgecolor="black",
               label="Cross-tier via (on bridged panels only)"),
        Line2D([0], [0], marker="s", color="gray", linestyle="None",
               markersize=6, markeredgecolor="black",
               label="Virtual edge (on bridged panels only)"),
    ]
    fig.legend(handles=handles, title="Legend", fontsize=6,
               title_fontsize=7, loc="upper right",
               bbox_to_anchor=(0.995, 0.985))

    # Tech info text strip.
    tech_keys = ("TECHNOLOGY", "LIB_NAME", "HEIGHT_CONFIG", "COL", "TRACK",
                 "NUM_FIN", "NUM_SITES", "DIFF_BREAK_TYPE", "PWR_CONFIG",
                 "BPC2_PITCH", "BPC1_PITCH", "PC1_PITCH", "PC2_PITCH",
                 "H0_PITCH", "H1_PITCH",
                 "M0_PITCH", "BM0_PITCH")
    tech_lines = [f"{k}: {tech[k]}" for k in tech_keys if k in tech]
    if tech_lines:
        fig.text(0.01, 0.01, "  |  ".join(tech_lines),
                 fontsize=5, va="bottom", color="gray", fontstyle="italic")

    fig.suptitle(f"QFET Layout — {filename or ''}", fontsize=11, fontweight="bold")
    fig.tight_layout(rect=[0, 0.03, 0.96, 0.95])

    if filename:
        out = filename.replace(".res", ".png") if filename.endswith(".res") else filename
        fig.savefig(out, dpi=300, bbox_inches="tight")
        print(f"Saved to {out}")
    else:
        fig.savefig("qfet_layout.png", dpi=300, bbox_inches="tight")
        print("Saved to qfet_layout.png")
    plt.close(fig)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python visualize_QFET_4T.py <results.res> [output.png]")
        sys.exit(1)
    fname = sys.argv[1]
    out_png = sys.argv[2] if len(sys.argv) > 2 else None
    placement, routing, tech = load_results(fname)
    draw_qfet_layout(placement, routing, tech, filename=out_png)
