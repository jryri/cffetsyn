"""CFFET (dual-face Flip-FET) GDS writer.

Extends the QFET JSON-driven writer for CFFET's 4 placement tiers + dual
M0ICPD power rails (BM0 back + M0 front). Layer numbers come from the CFFET
layer JSON (PROBE3_CFFET_2F_3T_4530OF0.json).

Usage:
    python -m src.cellgen.postprocess.gds_CFFET_SH \\
        --result_file output/.../result/INV_X1.res \\
        --subckt_name INV_X1 \\
        --layer       input/layer/PROBE3_CFFET_2F_3T_4530OF0.json \\
        --gds_file    output/.../gds/LIB.gds
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Optional

import klayout.db as pya

try:
    from src.cellgen.core.entity import LayerStack
    from src.cellgen.postprocess.gds_QFET_SH import (
        GDS_KEYS,
        SOLVER_RESCALE,
        _ACTIVE_GAP,
        _ACTIVE_HEIGHT,
        _ACTIVE_X_OFFSET,
        _ACTIVE_X_OVERLAP,
        _FIN_BACK_GDS,
        _FIN_FRONT_GDS,
        _FIN_PITCH,
        _FIN_WIDTH,
        _FIN_Y_OFFSET,
        _GATE_CUT_HEIGHT,
        _LISD_POWER_GAP,
        _LISD_POWER_HEIGHT,
        _LISD_SIGNAL_GAP,
        _LISD_SIGNAL_HEIGHT,
        _LISD_WIDTH,
        _LISD_X_OFFSET,
        _LISD_Y_OFFSET,
        _ROUTE_X_OFFSET,
        _ROUTE_Y_OFFSET,
        _SDT_GAP,
        _SDT_HEIGHT,
        _SDT_Y_OFFSET,
        _VIA_SIZE,
        _box,
        _text,
        _via_box,
        _wire_box,
    )
except ImportError:
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))
    from src.cellgen.core.entity import LayerStack
    from src.cellgen.postprocess.gds_QFET_SH import (
        GDS_KEYS,
        SOLVER_RESCALE,
        _ACTIVE_GAP,
        _ACTIVE_HEIGHT,
        _ACTIVE_X_OFFSET,
        _ACTIVE_X_OVERLAP,
        _FIN_BACK_GDS,
        _FIN_FRONT_GDS,
        _FIN_PITCH,
        _FIN_WIDTH,
        _FIN_Y_OFFSET,
        _GATE_CUT_HEIGHT,
        _LISD_POWER_GAP,
        _LISD_POWER_HEIGHT,
        _LISD_SIGNAL_GAP,
        _LISD_SIGNAL_HEIGHT,
        _LISD_WIDTH,
        _LISD_X_OFFSET,
        _LISD_Y_OFFSET,
        _ROUTE_X_OFFSET,
        _ROUTE_Y_OFFSET,
        _SDT_GAP,
        _SDT_HEIGHT,
        _SDT_Y_OFFSET,
        _VIA_SIZE,
        _box,
        _text,
        _via_box,
        _wire_box,
    )


DBU = 0.00025
PLACEMENT_TIERS = ("BBOTPC", "BTOPPC", "FBOTPC", "FTOPPC")
BACK_TIERS = frozenset({"BBOTPC", "BTOPPC"})
M0ICPD_METALS = ("BM0", "M0")


@dataclass
class Tran:
    name: str
    x: float
    y: float
    z: str
    flip: bool
    width: float
    height: float
    s_col: Optional[float]
    s_net: str
    d_col: Optional[float]
    d_net: str
    g_col: Optional[float]
    g_net: str
    model: str


@dataclass
class Seg:
    layer_u: int
    row_u: float
    col_u: float
    layer_v: int
    row_v: float
    col_v: float
    net: str


def _infer_tier(model: str) -> str:
    return "FTOPPC" if model.upper() == "PMOS" else "FBOTPC"


def parse_res(path: str):
    """Parse CFFET .res (17-token placement with Z, or legacy 16-token)."""
    trans: list[Tran] = []
    segs: list[Seg] = []
    tech: dict[str, str] = {}
    io_pins: list[str] = []
    mode = None

    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("** Placement Result"):
                mode = "place"
                next(f, None)
                next(f, None)
                continue
            if line.startswith("** Cell Information"):
                mode = "cell"
                next(f, None)
                continue
            if line.startswith("** Routing Result"):
                mode = "route"
                next(f, None)
                next(f, None)
                continue
            if line.startswith("** Technology Parameters"):
                mode = "tech"
                next(f, None)
                next(f, None)
                continue

            if mode == "place":
                toks = line.split()
                if len(toks) >= 17:
                    trans.append(Tran(
                        name=toks[0], x=float(toks[1]), y=float(toks[2]),
                        z=toks[3], flip=(toks[4] == "F"),
                        width=float(toks[5]), height=float(toks[6]),
                        s_col=(float(toks[8]) if float(toks[8]) >= 0 else None),
                        s_net=toks[9],
                        d_col=(float(toks[11]) if float(toks[11]) >= 0 else None),
                        d_net=toks[12],
                        g_col=(float(toks[14]) if float(toks[14]) >= 0 else None),
                        g_net=toks[15],
                        model=toks[16],
                    ))
                elif len(toks) >= 16:
                    trans.append(Tran(
                        name=toks[0], x=float(toks[1]), y=float(toks[2]),
                        z=_infer_tier(toks[15]), flip=(toks[3] == "F"),
                        width=float(toks[4]), height=float(toks[5]),
                        s_col=(float(toks[7]) if float(toks[7]) >= 0 else None),
                        s_net=toks[8],
                        d_col=(float(toks[10]) if float(toks[10]) >= 0 else None),
                        d_net=toks[11],
                        g_col=(float(toks[13]) if float(toks[13]) >= 0 else None),
                        g_net=toks[14],
                        model=toks[15],
                    ))
            elif mode == "route":
                if "=>" not in raw:
                    continue
                try:
                    left, right = raw.split("=>")
                    lu, ru, cu, net = left.split()[:4]
                    rparts = right.split()
                    lv, rv, cv = rparts[0], rparts[1], rparts[2]
                except ValueError:
                    continue
                segs.append(Seg(
                    layer_u=int(lu), row_u=float(ru), col_u=float(cu),
                    layer_v=int(lv), row_v=float(rv), col_v=float(cv),
                    net=net,
                ))
            elif mode == "tech":
                parts = line.split()
                if len(parts) >= 2:
                    tech[parts[0]] = " ".join(parts[1:])
            elif mode == "cell":
                io_pins.extend(line.split())

    return trans, segs, tech, io_pins


class CFFETLayout:
    """Emit a CFFET cell into klayout with dual M0ICPD rails."""

    def __init__(
        self,
        result_file: str,
        subckt_name: str,
        gds_file: str,
        layer_file: str,
    ):
        self.result_file = result_file
        self.subckt = subckt_name
        self.gds_file = gds_file

        self.trans, self.segs, self.tech, self.io_pins = parse_res(result_file)
        self.ls = LayerStack(layer_file)

        self._metal_idx_to_name = {
            i: m.layer_name for i, m in enumerate(self.ls.metal_layers)
        }
        self._name_to_metal = {m.layer_name: m for m in self.ls.metal_layers}

        missing = [k for k in GDS_KEYS if k not in self.ls.gds_layers]
        if missing:
            raise ValueError(
                f"Layer JSON missing gds entries: {missing}"
            )

        self.placement_pitch = self._name_to_metal["FTOPPC"].pitch
        self.m0_pitch = self._name_to_metal["M0"].pitch
        self.col = int(self.tech.get("COL", 0))
        self.track = int(self.tech.get("TRACK", 3))
        if self.col == 0:
            raise ValueError(f"Missing COL in {result_file}")
        self.cell_width = self.placement_pitch * self.col
        self.cell_height = self.m0_pitch * self.track * 2
        self.rail_thickness = float(
            self.tech.get(
                "M0_PWR_RAIL_THICKNESS",
                self.m0_pitch,
            )
        )

        if gds_file and os.path.isfile(gds_file):
            self.layout = pya.Layout()
            self.layout.read(gds_file)
            for cell in self.layout.top_cells():
                if cell.name == subckt_name:
                    self.layout.delete_cell(cell.cell_index())
                    break
        else:
            outdir = os.path.dirname(gds_file)
            if outdir:
                os.makedirs(outdir, exist_ok=True)
            self.layout = pya.Layout()

        self.layout.dbu = DBU
        self.cell = self.layout.create_cell(subckt_name)
        self._draw()
        if gds_file:
            self.layout.write(gds_file)
            print(f"[INFO] Wrote {gds_file}")

    def _lyr(self, gds_num: int, dtype: int = 0) -> int:
        return self.layout.layer(gds_num, dtype)

    def _gds(self, key: str) -> int:
        gds_layer, gds_datatype = self.ls.gds_layers[key]
        return self._lyr(gds_layer, gds_datatype)

    def _metal_layer_idx(self, lgg_idx: int) -> Optional[int]:
        name = self._metal_idx_to_name.get(lgg_idx)
        metal = self._name_to_metal.get(name) if name else None
        if metal is None or metal.gds_layer is None:
            return None
        return self._lyr(metal.gds_layer, metal.gds_datatype)

    def _via_for_segment(self, lo_idx: int, hi_idx: int) -> Optional[int]:
        lo_name = self._metal_idx_to_name.get(lo_idx)
        hi_name = self._metal_idx_to_name.get(hi_idx)
        if not lo_name or not hi_name:
            return None
        via = (
            self.ls.via_layers.get((lo_name, hi_name))
            or self.ls.via_layers.get((hi_name, lo_name))
        )
        if via is None or via.gds_layer is None:
            return None
        return self._lyr(via.gds_layer, via.gds_datatype)

    def _route_row(self, row: float) -> float:
        """M0ICPD rows are already physical (no SOLVER_RESCALE on Y)."""
        return row

    def _route_col(self, col: float) -> float:
        return col / SOLVER_RESCALE

    def _wire_box_m0icpd(self, seg: Seg, direction: str, wire_width: float,
                         h_ovl: float, v_ovl: float):
        cu = self._route_col(seg.col_u)
        cv = self._route_col(seg.col_v)
        ru = self._route_row(seg.row_u)
        rv = self._route_row(seg.row_v)
        cu, cv = sorted((cu, cv))
        ru, rv = sorted((ru, rv))
        m0_y_off = (self.m0_pitch - wire_width) / 2
        if direction == "H":
            lx = cu + _ROUTE_X_OFFSET - h_ovl
            ux = cv + _ROUTE_X_OFFSET + _VIA_SIZE + h_ovl
            ly = ru + m0_y_off - v_ovl
            uy = rv + m0_y_off + wire_width + v_ovl
        else:
            via_cx_u = cu + _ROUTE_X_OFFSET + _VIA_SIZE / 2
            via_cx_v = cv + _ROUTE_X_OFFSET + _VIA_SIZE / 2
            lx = via_cx_u - wire_width / 2 - h_ovl
            ux = via_cx_v + wire_width / 2 + h_ovl
            ly = ru + _ROUTE_Y_OFFSET - v_ovl
            uy = rv + _ROUTE_Y_OFFSET + _VIA_SIZE + v_ovl
        return lx, ly, ux, uy

    def _via_box_m0icpd(self, col: float, row: float):
        cx = self._route_col(col)
        ry = self._route_row(row)
        v0_y = (self.m0_pitch - _VIA_SIZE) / 2
        lx = cx + _ROUTE_X_OFFSET
        ly = ry + v0_y
        return lx, ly, lx + _VIA_SIZE, ly + _VIA_SIZE

    def _draw(self):
        self._draw_boundary()
        self._draw_wells_and_selects()
        self._draw_m0icpd_power_rails()
        self._draw_gate_columns()
        self._draw_fins()
        self._draw_actives()
        self._draw_routing()

    def _draw_boundary(self):
        _box(self.cell, self._gds("BOUNDARY"), 0, 0, self.cell_width, self.cell_height)

    def _draw_wells_and_selects(self):
        mid = self.cell_height / 2
        _box(self.cell, self._gds("WELL_FRONT"), 0, mid, self.cell_width, self.cell_height)
        _box(self.cell, self._gds("PSELECT_FRONT"), 0, mid, self.cell_width, self.cell_height)
        _box(self.cell, self._gds("NSELECT_FRONT"), 0, 0, self.cell_width, mid)
        _box(self.cell, self._gds("WELL_BACK"), 0, mid, self.cell_width, self.cell_height)
        _box(self.cell, self._gds("PSELECT_BACK"), 0, mid, self.cell_width, self.cell_height)
        _box(self.cell, self._gds("NSELECT_BACK"), 0, 0, self.cell_width, mid)

    def _draw_m0icpd_power_rails(self):
        """VSS/VDD centered in bottom/top fine rows on BM0 and M0."""
        band = self.m0_pitch
        rail_h = min(self.rail_thickness, band)
        for metal_name in M0ICPD_METALS:
            ml = self._name_to_metal.get(metal_name)
            if ml is None or ml.gds_layer is None:
                continue
            lidx = self._lyr(ml.gds_layer, ml.gds_datatype)
            ly_vss = (band - rail_h) / 2
            _box(self.cell, lidx, 0, ly_vss, self.cell_width, ly_vss + rail_h)
            _text(self.cell, lidx, "VSS", self.cell_width / 2, band / 2)
            ly_vdd = self.cell_height - band + (band - rail_h) / 2
            _box(self.cell, lidx, 0, ly_vdd, self.cell_width, ly_vdd + rail_h)
            _text(self.cell, lidx, "VDD", self.cell_width / 2,
                  self.cell_height - band / 2)

    def _draw_gate_columns(self):
        cp_pitch = self.placement_pitch
        ly = 5.0
        uy = self.cell_height - 5.0
        for tier in PLACEMENT_TIERS:
            ml = self._name_to_metal.get(tier)
            if ml is None or ml.gds_layer is None:
                continue
            lidx = self._lyr(ml.gds_layer, ml.gds_datatype)
            gate_width = ml.width
            i = 0
            while True:
                gx = i * cp_pitch
                if gx > self.cell_width:
                    break
                _box(self.cell, lidx,
                     gx - gate_width / 2, ly,
                     gx + gate_width / 2, uy)
                i += 1

    def _draw_fins(self):
        for gds_layer, gds_dtype in (_FIN_FRONT_GDS, _FIN_BACK_GDS):
            lidx = self._lyr(gds_layer, gds_dtype)
            curr_y = _FIN_Y_OFFSET
            while curr_y + _FIN_WIDTH <= self.cell_height:
                _box(self.cell, lidx, 0, curr_y, self.cell_width, curr_y + _FIN_WIDTH)
                curr_y += _FIN_PITCH + _FIN_WIDTH

    def _draw_actives(self):
        denom = self.m0_pitch * SOLVER_RESCALE
        active_y_offset = (self.cell_height - _ACTIVE_HEIGHT * 2 - _ACTIVE_GAP) / 2
        for t in self.trans:
            is_back = t.z in BACK_TIERS
            act_key = (
                "ACTIVE_BACK_P" if (is_back and t.model == "PMOS")
                else "ACTIVE_BACK_N" if is_back
                else "ACTIVE_FRONT_P" if t.model == "PMOS"
                else "ACTIVE_FRONT_N"
            )
            lx = _ACTIVE_X_OFFSET + t.x / SOLVER_RESCALE
            width = t.width / SOLVER_RESCALE + _ACTIVE_X_OVERLAP
            height = t.height / denom * _ACTIVE_HEIGHT
            if t.model == "PMOS":
                ly = t.y / (denom * 2) * _ACTIVE_HEIGHT + active_y_offset + _ACTIVE_GAP
            else:
                ly = t.y / denom * _ACTIVE_HEIGHT + active_y_offset
            _box(self.cell, self._gds(act_key), lx, ly, lx + width, ly + height)

    def _draw_routing(self):
        gate_layers = set(PLACEMENT_TIERS)
        gate_lgg_idx = {
            i for i, n in self._metal_idx_to_name.items() if n in gate_layers
        }
        m0icpd_idx = {
            i for i, n in self._metal_idx_to_name.items() if n in M0ICPD_METALS
        }

        for s in self.segs:
            if s.layer_u == s.layer_v:
                if s.layer_u in gate_lgg_idx:
                    continue
                lidx = self._metal_layer_idx(s.layer_u)
                if lidx is None:
                    continue
                name = self._metal_idx_to_name[s.layer_u]
                ml = self._name_to_metal[name]
                h_ovl, v_ovl = ml.horizontal_enclosure, ml.vertical_enclosure
                if s.layer_u in m0icpd_idx:
                    lx, ly, ux, uy = self._wire_box_m0icpd(
                        s, ml.direction, ml.width, h_ovl, v_ovl
                    )
                else:
                    lx, ly, ux, uy = _wire_box(
                        s, ml.direction, ml.width, h_ovl, v_ovl
                    )
                _box(self.cell, lidx, lx, ly, ux, uy)
                if name in M0ICPD_METALS and s.net in self.io_pins:
                    _text(self.cell, lidx, s.net, (lx + ux) / 2, (ly + uy) / 2)
            else:
                lo, hi = sorted((s.layer_u, s.layer_v))
                vidx = self._via_for_segment(lo, hi)
                if vidx is None:
                    continue
                if lo in m0icpd_idx or hi in m0icpd_idx:
                    lx, ly, ux, uy = self._via_box_m0icpd(s.col_u, s.row_u)
                else:
                    lx, ly, ux, uy = _via_box(s.col_u, s.row_u)
                _box(self.cell, vidx, lx, ly, ux, uy)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--result_file", required=True)
    parser.add_argument("--subckt_name", required=True)
    parser.add_argument("--gds_file", required=True)
    parser.add_argument("--layer", required=True, help="CFFET layer-stack JSON")
    args = parser.parse_args()
    CFFETLayout(
        result_file=args.result_file,
        subckt_name=args.subckt_name,
        gds_file=args.gds_file,
        layer_file=args.layer,
    )
