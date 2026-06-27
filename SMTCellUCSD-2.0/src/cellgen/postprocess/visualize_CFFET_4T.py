"""Visualise CFFET placement + routing (4 placement tiers, dual M0ICPD).

Four side-by-side panels (bottom -> top of LGG stack):

  BBOTPC | BTOPPC | FBOTPC | FTOPPC

Cross-layer vias (STV, FMIV/BMIV, CA*, FV0/FV1, virtual FBOTCA/BBOTCA) are
echoed on every panel so nets can be traced across the dual-face stack.

Usage:
    python -m src.cellgen.postprocess.visualize_CFFET_4T <results.res> [out.png]
"""

from __future__ import annotations

import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle, Patch


LAYER_NAMES = {
    0: "BM1", 1: "BM0",
    2: "BBOTPC", 3: "BTOPPC",
    4: "FBOTPC", 5: "FTOPPC",
    6: "M0", 7: "M1", 8: "M2",
}
NAME_TO_LAYER = {v: k for k, v in LAYER_NAMES.items()}

PLACEMENT_TIERS = ("BBOTPC", "BTOPPC", "FBOTPC", "FTOPPC")

LAYER_TO_TIER = {
    "BM1": "BBOTPC", "BM0": "BBOTPC",
    "BBOTPC": "BBOTPC", "BTOPPC": "BTOPPC",
    "FBOTPC": "FBOTPC", "FTOPPC": "FTOPPC",
    "M0": "FTOPPC", "M1": "FTOPPC", "M2": "FTOPPC",
}

VIA_NAMES = {
    (0, 1): "BV0",
    (1, 2): "BBOTCA",
    (2, 3): "BMIV",
    (3, 4): "STV",
    (4, 5): "FMIV",
    (5, 6): "FTOPCA",
    (6, 7): "FV0",
    (7, 8): "FV1",
    (4, 6): "FBOTCA",
}

BACK_TIERS = frozenset({"BBOTPC", "BTOPPC"})

PMOS_COLOR = "#E8575780"
NMOS_COLOR = "#4A90D980"
PMOS_EDGE = "#C0392B"
NMOS_EDGE = "#2471A3"
TIER_BG = {
    "BBOTPC": "#FFF3E0",
    "BTOPPC": "#FFE0B2",
    "FBOTPC": "#E8F5E9",
    "FTOPPC": "#C8E6C9",
}


def _via_name(l1, l2):
    key = (min(l1, l2), max(l1, l2))
    return VIA_NAMES.get(key, f"V{l1}-{l2}")


def _infer_tier(model: str) -> str:
    """Legacy CFET-style rows without Z: default to front block."""
    if model.lower() == "pmos":
        return "FTOPPC"
    return "FBOTPC"


def load_results(filename):
    """Parse a CFFET .res file."""
    placement, routing, tech = {}, [], {}
    mode = None

    with open(filename) as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue

            if line.startswith("** Placement Result"):
                mode = "placement"
                next(f, None)
                next(f, None)
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
                if len(toks) >= 17:
                    name = toks[0]
                    x, y = float(toks[1]), float(toks[2])
                    z = toks[3]
                    flip = toks[4] == "F"
                    width, height = float(toks[5]), float(toks[6])
                    s_net = toks[9] if float(toks[8]) >= 0 else None
                    d_net = toks[12] if float(toks[11]) >= 0 else None
                    g_net = toks[15] if float(toks[14]) >= 0 else None
                    model = toks[16]
                elif len(toks) >= 16:
                    name = toks[0]
                    x, y = float(toks[1]), float(toks[2])
                    z = _infer_tier(toks[15])
                    flip = toks[3] == "F"
                    width, height = float(toks[4]), float(toks[5])
                    s_net = toks[8] if float(toks[7]) >= 0 else None
                    d_net = toks[11] if float(toks[10]) >= 0 else None
                    g_net = toks[14] if float(toks[13]) >= 0 else None
                    model = toks[15]
                else:
                    continue

                if s_net is None and model.lower() == "nmos":
                    s_net = "VSS"
                elif s_net is None and model.lower() == "pmos":
                    s_net = "VDD"
                if d_net is None and model.lower() == "nmos":
                    d_net = "VSS"
                elif d_net is None and model.lower() == "pmos":
                    d_net = "VDD"

                placement[name] = {
                    "x": x, "y": y, "z": z,
                    "flip": flip, "width": width, "height": height,
                    "s_net": s_net, "d_net": d_net, "g_net": g_net,
                    "model": model,
                }

            elif mode == "routing":
                if "=>" not in raw:
                    continue
                try:
                    left, right = raw.split("=>")
                    lu, ru, cu, net = left.split()[:4]
                    rparts = right.split()
                    lv, rv, cv = rparts[0], rparts[1], rparts[2]
                except (ValueError, IndexError):
                    continue
                routing.append({
                    "layer_u": int(lu), "row_u": float(ru), "col_u": float(cu),
                    "layer_v": int(lv), "row_v": float(rv), "col_v": float(cv),
                    "net": net,
                })

            elif mode == "tech":
                parts = line.split()
                if len(parts) >= 2:
                    tech[parts[0]] = parts[1]

    return placement, routing, tech


def draw_cffet_layout(placement, routing, tech=None, filename=None,
                      pin_radius=2.0, track_width=4.0):
    """Draw four-panel CFFET layout."""
    tech = tech or {}
    tiers = {z: {} for z in PLACEMENT_TIERS}
    for name, info in placement.items():
        z = info["z"]
        if z in tiers:
            tiers[z][name] = info
        else:
            tiers["FBOTPC"][name] = info

    tier_routing = {z: [] for z in PLACEMENT_TIERS}
    cross_tier_routing = []
    for r in routing:
        lu, lv = r["layer_u"], r["layer_v"]
        if lu == lv:
            host = LAYER_TO_TIER.get(LAYER_NAMES.get(lu))
            if host is not None:
                tier_routing[host].append(r)
            else:
                cross_tier_routing.append(r)
        else:
            cross_tier_routing.append(r)

    def _bounds(pl, rt):
        xs, ys = [], []
        for info in pl.values():
            xs += [info["x"], info["x"] + info["width"]]
            ys += [info["y"], info["y"] + info["height"]]
        for seg in rt:
            xs += [seg["col_u"], seg["col_v"]]
            ys += [seg["row_u"], seg["row_v"]]
        if not xs:
            return 0, 100, 0, 100
        return min(xs), max(xs), min(ys), max(ys)

    bounds = {z: _bounds(tiers[z], tier_routing[z]) for z in PLACEMENT_TIERS}
    min_x = min(b[0] for b in bounds.values())
    max_x = max(b[1] for b in bounds.values())
    min_y = min(b[2] for b in bounds.values())
    max_y = max(b[3] for b in bounds.values())
    margin = 30

    n_tiers = len(PLACEMENT_TIERS)
    panel_w = max((max_x - min_x + 2 * margin) / 25, 3)
    panel_h = max((max_y - min_y + 2 * margin) / 25, 4)
    fig, axes_list = plt.subplots(
        1, n_tiers,
        figsize=(panel_w * n_tiers + 2, panel_h),
        dpi=150,
        gridspec_kw={"width_ratios": [1] * n_tiers},
    )
    if n_tiers == 1:
        axes_list = [axes_list]
    axes = dict(zip(PLACEMENT_TIERS, axes_list))

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

    for z, ax in axes.items():
        face = TIER_BG.get(z, "white")
        ax.set_facecolor(face)
        n = len(tiers[z])
        face_label = "back" if z in BACK_TIERS else "front"
        ax.set_title(f"{z} ({face_label}) — {n} dev", fontsize=9, fontweight="bold")
        ax.set_xlabel("X / col")
        ax.set_ylabel("Y / row")
        ax.set_aspect("equal", adjustable="box")
        ax.grid(True, linestyle="--", linewidth=0.3, alpha=0.5)

    for z in PLACEMENT_TIERS:
        ax = axes[z]
        pl = tiers[z]
        rt = tier_routing[z]

        for name, info in pl.items():
            x, y = info["x"], info["y"]
            w, h = info["width"], info["height"]
            flip = info["flip"]
            model = info["model"].lower()
            edge = PMOS_EDGE if model == "pmos" else NMOS_EDGE
            face = PMOS_COLOR if model == "pmos" else NMOS_COLOR
            rect = Rectangle((x, y), w, h, edgecolor=edge, facecolor=face, lw=1.2)
            ax.add_patch(rect)
            ax.text(
                x + w * 0.5, y + h * 0.92, name,
                ha="center", va="top", fontsize=7, color=edge, fontweight="bold",
            )

            if flip:
                pins = [("D", (x, y + h), info["d_net"]),
                        ("G", (x + w / 2, y + h), info["g_net"]),
                        ("S", (x + w, y + h), info["s_net"])]
            else:
                pins = [("S", (x, y + h), info["s_net"]),
                        ("G", (x + w / 2, y + h), info["g_net"]),
                        ("D", (x + w, y + h), info["d_net"])]

            for _, (px, py), net in pins:
                if net is None:
                    continue
                circ = Circle((px, py), radius=pin_radius, edgecolor=edge,
                              facecolor="white", lw=1.0, zorder=10)
                ax.add_patch(circ)
                ax.text(px + 6, py - 6, net, fontsize=5, color=edge,
                        fontstyle="italic")

        for r in rt:
            x0, y0 = r["col_u"], r["row_u"]
            x1, y1 = r["col_v"], r["row_v"]
            col = net_colors[r["net"]]
            layer_name = LAYER_NAMES.get(r["layer_u"], str(r["layer_u"]))
            dx, dy = x1 - x0, y1 - y0
            if abs(dy) < 1e-6:
                patch = Rectangle(
                    (min(x0, x1), y0 - track_width / 2),
                    abs(dx), track_width,
                    facecolor=col, edgecolor="black", lw=0.4, alpha=0.65,
                )
                ax.add_patch(patch)
            elif abs(dx) < 1e-6:
                patch = Rectangle(
                    (x0 - track_width / 2, min(y0, y1)),
                    track_width, abs(dy),
                    facecolor=col, edgecolor="black", lw=0.4, alpha=0.65,
                )
                ax.add_patch(patch)
            xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
            ax.text(xm, ym, f"{r['net']},{layer_name}", ha="center", va="center",
                    fontsize=5, color="white",
                    bbox=dict(facecolor=col, edgecolor="black",
                              boxstyle="round,pad=0.2", alpha=0.6))

        for r in cross_tier_routing:
            x0, y0 = r["col_u"], r["row_u"]
            col = net_colors[r["net"]]
            via_name = _via_name(r["layer_u"], r["layer_v"])
            ax.plot(x0, y0, marker="x", color=col, markersize=track_width * 3,
                    markeredgewidth=1.5, alpha=0.85)
            ax.text(x0, y0 + track_width, f"{r['net']},{via_name}",
                    ha="center", va="bottom", fontsize=5,
                    bbox=dict(facecolor="lightgray", edgecolor="black",
                              boxstyle="round,pad=0.2", alpha=0.55))

        ax.set_xlim(min_x - margin, max_x + margin)
        ax.set_ylim(min_y - margin, max_y + margin)

    handles = [Patch(facecolor=net_colors[n], edgecolor="black", label=n)
               for n in all_nets]
    fig.legend(handles=handles, title="Nets", loc="upper right", fontsize=6)
    title = filename or "CFFET layout"
    fig.suptitle(f"CFFET P&R — {title}", fontsize=11, fontweight="bold")
    plt.tight_layout(rect=[0, 0, 0.92, 0.95])

    if filename:
        plt.savefig(filename, dpi=200)
        print(f"Saved to {filename}")
    else:
        plt.show()
    plt.close(fig)


draw_layout_with_pin_and_routing = draw_cffet_layout


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m src.cellgen.postprocess.visualize_CFFET_4T "
              "<results.res> [output.png]")
        sys.exit(1)
    res_path = sys.argv[1]
    out_png = sys.argv[2] if len(sys.argv) > 2 else None
    placement, routing, tech = load_results(res_path)
    draw_cffet_layout(placement, routing, tech=tech, filename=out_png)
