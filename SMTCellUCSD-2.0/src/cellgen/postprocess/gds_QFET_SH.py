"""QFET (2-tier 3D FinFET) GDS writer.

Adapted from the upstream FinFET GDS writer, with these QFET-specific
differences:

  * Two placement tiers (BPC1, PC1) stacked in Z. BPC1 = backside gate poly
    (GDS 57), PC1 = frontside gate poly (GDS 7). Both share the same X/Y; the
    GDS distinguishes them by layer number.
  * Mid-routing layers H0 / H1 between the placement tiers, plus a MIV chain
    (MIV1: BPC1<->H0, MIV2: H0<->H1, MIV3: H1<->PC1) emitted on debug GDS
    layers 5000-5002 since no upstream PDK has them.
  * Virtual jumps VL1 / VL2 are graph-only shortcuts; drawn only when
    --draw-virtual is passed, on the gds_layer the JSON declares (700/701).

All GDS layer numbers are read from the layer JSON (each entry's
`gds_layer` / `gds_datatype` fields). Active / select / well / boundary
have no JSON counterpart and use upstream-FinFET-compatible constants below.

Usage:
    python -m src.cellgen.postprocess.gds_QFET_SH \\
        --result  output/.../result/BUF_X1.res \\
        --layer   input/layer/PROBE3_QFET_2F_4T_4530OF0.json \\
        --subckt  BUF_X1 \\
        --gds     output/.../gds/BUF_X1.gds
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
except ImportError:
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))
    from src.cellgen.core.entity import LayerStack


SCALE = 4              # 1nm -> 0.001um at dbu=0.00025
DBU = 0.00025          # PROBE3 default
PRECISION = 4
# Both row and col in .res are in canvas (doubled-resolution) space - the
# LayeredGridGraph generates them on a half-pitch grid so gate and S/D cols
# (and pin tracks) all land on integers. Divide by 2 to recover display nm.
# Mirrors upstream gds_FinFET_SH.py:SOLVER_RESCALE.
SOLVER_RESCALE = 2.0

# Named GDS-only layers the writer expects to find in the layer JSON
# (layer_type=="gds" entries -> layer_stack.gds_layers). Active / select /
# well / boundary are solver-irrelevant; they're keyed off transistor
# placement and emitted directly on these GDS layers.
GDS_KEYS = (
    "ACTIVE_FRONT_P", "ACTIVE_FRONT_N",
    "ACTIVE_BACK_P",  "ACTIVE_BACK_N",
    "PSELECT_FRONT",  "NSELECT_FRONT",
    "PSELECT_BACK",   "NSELECT_BACK",
    "WELL_FRONT",     "WELL_BACK",
    "BOUNDARY",
    "GATE_CUT_FRONT", "GATE_CUT_BACK",
    "LISD1",          "BLISD1",
    "SDT1",           "BSDT1",
)

# Direction-based wire-end via enclosure (h_ovl, v_ovl) - the amount a metal
# extends past its routing endpoint to fully enclose the landing via.
# Mirrors upstream FinFET m{0,1,2}_h_ovl / v_ovl: H metals (M0/BM0/H0)
# overhang in X by 3, in Y by 0; V metals (M1/BM1/H1) overhang in X by 0.5,
# in Y by 5.
_METAL_VIA_ENCLOSURE = {
    "H": (3.0, 0.0),
    "V": (0.5, 5.0),
}

# Wire/via anchor offsets - verbatim from upstream gds_FinFET_SH.py.
# Wires and vias are anchored at the LOWER-LEFT of the landing-via box at
# (col/2 + X_OFFSET, row/2 + Y_OFFSET). VIA_SIZE is the 14x14 contact box
# that lands at every (row, col) on cross-layer hops AND defines the
# wire-endpoint enclosure in the perpendicular axis.
_ROUTE_X_OFFSET = -7.0     # ca_x_offset / m0_x_offset / ... (all -7 upstream)
_ROUTE_Y_OFFSET = 29.0     # ca_y_offset / m0_y_offset / ... (all +29 upstream)
_VIA_SIZE       = 14.0     # ca_width / lig_width / v0_width = 14 nm

# All of these are taken verbatim from upstream gds_FinFET_SH.py's __init__
# constants. Don't tweak them; the values are calibrated for cp_pitch=45,
# m0_pitch=24, SOLVER_RESCALE=2.
_GATE_CUT_HEIGHT     = 10.0
_LISD_WIDTH          = 16.0
_LISD_SIGNAL_HEIGHT  = 48.0
_LISD_POWER_HEIGHT   = 66.0
_LISD_SIGNAL_GAP     = 8.0
_LISD_POWER_GAP      = 10.0
_LISD_Y_OFFSET       = 20.0
_LISD_X_OFFSET       = -8.0
_SDT_Y_OFFSET        = 24.0
_SDT_HEIGHT          = 42.0
_SDT_GAP             = 12.0

# Active (OD) styling - from upstream gds_FinFET_SH.py __pmos__ / __nmos__.
# Diffusion is shifted by _ACTIVE_X_OFFSET and widened by _ACTIVE_X_OVERLAP so
# neighbouring devices share continuous OD; the band height is scaled to
# _ACTIVE_HEIGHT and centered with active_y_offset (PMOS upper, NMOS lower).
# _ACTIVE_GAP is the PROBE3 ACTIVE_GAP value: upstream reads it from the .res,
# but the QFET .res omits it, so it is pinned to the PROBE3 value here.
_ACTIVE_X_OVERLAP    = 14.0
_ACTIVE_HEIGHT       = 46.0
_ACTIVE_X_OFFSET     = -7.0
_ACTIVE_GAP          = 14.0

# Fin styling - from upstream gds_FinFET_SH.py __fin__ constants. Fins are a
# substrate grid drawn across the whole cell regardless of device placement.
# The front fin uses upstream FinFET layer 2; the back fin uses the +500
# backside-layer convention shared by the ACTIVE_*/GATE_CUT_* layers.
_FIN_Y_OFFSET        = 21.0
_FIN_PITCH           = 18.0
_FIN_WIDTH           = 6.0
_FIN_FRONT_GDS       = (2, 0)
_FIN_BACK_GDS        = (502, 0)


@dataclass
class Tran:
    name: str
    x: float
    y: float
    z: str          # placement tier name ("BPC1" or "PC1")
    flip: bool
    width: float
    height: float
    s_col: Optional[float]; s_net: str
    d_col: Optional[float]; d_net: str
    g_col: Optional[float]; g_net: str
    model: str      # "PMOS" / "NMOS"


@dataclass
class Seg:
    layer_u: int; row_u: float; col_u: float
    layer_v: int; row_v: float; col_v: float
    net: str


# --- .res parser ---
def parse_res(path: str):
    """Parse a QFET .res file -> (trans, segs, tech, io_pins)."""
    trans: list[Tran] = []
    segs:  list[Seg]  = []
    tech:  dict[str, str] = {}
    io_pins: list[str] = []
    mode = None

    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("** Placement Result"):
                mode = "place"; next(f, None); next(f, None); continue
            if line.startswith("** Cell Information"):
                mode = "cell"; next(f, None); continue
            if line.startswith("** Routing Result"):
                mode = "route"; next(f, None); next(f, None); continue
            if line.startswith("** Technology Parameters"):
                mode = "tech"; next(f, None); next(f, None); continue

            if mode == "place":
                toks = line.split()
                if len(toks) < 14:
                    continue
                trans.append(Tran(
                    name=toks[0], x=float(toks[1]), y=float(toks[2]),
                    z=toks[3], flip=(toks[4] == "F"),
                    width=float(toks[5]), height=float(toks[6]),
                    s_col=(float(toks[7]) if toks[7] != "-1" else None), s_net=toks[8],
                    d_col=(float(toks[9]) if toks[9] != "-1" else None), d_net=toks[10],
                    g_col=(float(toks[11]) if toks[11] != "-1" else None), g_net=toks[12],
                    model=toks[13],
                ))
            elif mode == "route":
                if "=>" not in raw:
                    continue
                try:
                    left, right = raw.split("=>")
                    lu, ru, cu, net = left.split()
                    lv, rv, cv, _ = right.split()
                except ValueError:
                    continue
                segs.append(Seg(
                    layer_u=int(lu), row_u=float(ru), col_u=float(cu),
                    layer_v=int(lv), row_v=float(rv), col_v=float(cv),
                    net=net,
                ))
            elif mode == "tech":
                toks = line.split()
                if len(toks) >= 2:
                    tech[toks[0]] = " ".join(toks[1:])
            elif mode == "cell":
                io_pins.extend(line.split())

    return trans, segs, tech, io_pins


# --- small helpers ---
def _box(cell, layer_idx, lx, ly, ux, uy):
    """Insert axis-aligned box (inputs in nm, SCALE'd to dbu)."""
    lx, ly = round(lx, PRECISION), round(ly, PRECISION)
    ux, uy = round(ux, PRECISION), round(uy, PRECISION)
    cell.shapes(layer_idx).insert(pya.Box(
        pya.Point(int(lx * SCALE), int(ly * SCALE)),
        pya.Point(int(ux * SCALE), int(uy * SCALE)),
    ))


def _text(cell, layer_idx, s, x, y):
    cell.shapes(layer_idx).insert(pya.Text(
        s, int(round(x, PRECISION) * SCALE), int(round(y, PRECISION) * SCALE)
    ))


def _wire_box(seg: Seg, direction: str, wire_width: float,
              h_ovl: float, v_ovl: float):
    """FinFET-upstream wire box: anchored at the lower-left of the landing
    via at each endpoint. Both row and col are divided by SOLVER_RESCALE
    (canvas -> display), then offset by (_ROUTE_X_OFFSET, _ROUTE_Y_OFFSET).
    The opposite axis of the wire direction is sized by the wire's own
    width (e.g. M0.width=14 in Y for a horizontal wire); the wire's own
    axis is extended by VIA_SIZE so the box encloses both endpoint vias,
    plus h_ovl / v_ovl enclosure overhang.

    Matches gds_FinFET_SH.py's __m0__ / __m1__ for the upstream width-14
    metals; the perpendicular (track) axis is centered on the landing-via
    center so off-width wires (e.g. H1, width 16) stay on the track grid.
    Padded by (h_ovl, v_ovl). The landing via is anchored lower-left at
    (col/2 + _ROUTE_X_OFFSET, row/2 + _ROUTE_Y_OFFSET), so its center is at
    +_VIA_SIZE/2; centering the wire there keeps it co-axial with the via
    (identical to the via lower-left anchor when wire_width == _VIA_SIZE)."""
    cu, cv = sorted((seg.col_u / SOLVER_RESCALE, seg.col_v / SOLVER_RESCALE))
    ru, rv = sorted((seg.row_u / SOLVER_RESCALE, seg.row_v / SOLVER_RESCALE))
    if direction == "H":
        # Along-axis (X) spans both endpoint vias; perpendicular (Y) is the
        # wire centered on the landing-via center (row track).
        via_cy_u = ru + _ROUTE_Y_OFFSET + _VIA_SIZE / 2
        via_cy_v = rv + _ROUTE_Y_OFFSET + _VIA_SIZE / 2
        lx = cu + _ROUTE_X_OFFSET - h_ovl
        ux = cv + _ROUTE_X_OFFSET + _VIA_SIZE + h_ovl
        ly = via_cy_u - wire_width / 2 - v_ovl
        uy = via_cy_v + wire_width / 2 + v_ovl
    elif direction == "V":
        # Along-axis (Y) spans both endpoint vias; perpendicular (X) is the
        # wire centered on the landing-via center (col track).
        via_cx_u = cu + _ROUTE_X_OFFSET + _VIA_SIZE / 2
        via_cx_v = cv + _ROUTE_X_OFFSET + _VIA_SIZE / 2
        lx = via_cx_u - wire_width / 2 - h_ovl
        ux = via_cx_v + wire_width / 2 + h_ovl
        ly = ru + _ROUTE_Y_OFFSET - v_ovl
        uy = rv + _ROUTE_Y_OFFSET + _VIA_SIZE + v_ovl
    else:
        raise ValueError(f"Unknown direction {direction!r}")
    return lx, ly, ux, uy


def _via_box(col: float, row: float):
    """FinFET-upstream via box: 14x14, anchored at lower-left
    (col/2 - 7, row/2 + 29). Used for every cross-layer hop
    (CA1/BCA1/V0/BV0/MIV*)."""
    cx = col / SOLVER_RESCALE
    ry = row / SOLVER_RESCALE
    lx = cx + _ROUTE_X_OFFSET
    ly = ry + _ROUTE_Y_OFFSET
    return lx, ly, lx + _VIA_SIZE, ly + _VIA_SIZE


# --- main writer ---
class QFETLayout:
    """Emit a QFET cell into a klayout Layout. Layer numbers come from the
    LayerStack JSON (metal_layers / via_layers / virtual_layers); active &
    well layers use the upstream-FinFET-compatible constants above."""

    def __init__(self, result_file: str, layer_file: str, subckt: str,
                 gds_file: str, draw_virtual: bool = False):
        self.result_file = result_file
        self.subckt      = subckt
        self.gds_file    = gds_file
        self.draw_virtual = draw_virtual

        self.trans, self.segs, self.tech, self.io_pins = parse_res(result_file)
        self.ls = LayerStack(layer_file)

        # idx_to_layer mirrors what QFET._init_graph builds: every metal in
        # JSON-stack order. Virtual layers don't get an LGG idx, so they're
        # looked up by name when needed.
        self._metal_idx_to_name = {i: m.layer_name for i, m in enumerate(self.ls.metal_layers)}
        self._name_to_metal = {m.layer_name: m for m in self.ls.metal_layers}

        # GDS-only layers (active / select / well / boundary). Fail fast if
        # the JSON is missing one of the keys the writer relies on.
        missing = [k for k in GDS_KEYS if k not in self.ls.gds_layers]
        if missing:
            raise ValueError(
                f"Layer JSON {layer_file} is missing 'gds' entries: {missing}. "
                "Add an entry with layer_type='gds', gds_layer=N, "
                "and optionally gds_datatype=D."
            )

        # Cell extent matches upstream FinFET GDS:
        #   width  = cp_pitch * COL,  COL = cpp_cost//2 + 2 (from .res "COL")
        #   height = m0_pitch * (track + 2)   - 4 pin-access + 2 power-rail
        # COL already encodes the boundary padding; nothing else to add.
        self.placement_pitch = self._name_to_metal["PC1"].pitch
        self.m0_pitch        = self._name_to_metal["M0"].pitch
        self.col             = int(self.tech.get("COL", 0))
        self.track           = int(self.tech.get("TRACK", 4))
        if self.col == 0:
            raise ValueError(f"Missing 'COL' in {result_file} tech parameters")
        self.cell_width      = self.placement_pitch * self.col
        self.cell_height     = self.m0_pitch * (self.track + 2)

        # Open or create the layout
        if os.path.isfile(gds_file):
            print(f"[INFO] Reading existing GDS: {gds_file}")
            self.layout = pya.Layout()
            self.layout.read(gds_file)
            for c in self.layout.top_cells():
                if c.name == subckt:
                    print(f"[INFO] Replacing existing cell '{subckt}'")
                    self.layout.delete_cell(c.cell_index())
                    break
        else:
            outdir = os.path.dirname(gds_file)
            if outdir:
                os.makedirs(outdir, exist_ok=True)
            self.layout = pya.Layout()
        self.layout.dbu = DBU
        self.cell = self.layout.create_cell(subckt)

        self._draw()
        self.layout.write(gds_file)
        print(f"[INFO] Wrote {gds_file}")

    # --- per-layer index resolution ---
    def _lyr(self, gds_num: int, dtype: int = 0) -> int:
        return self.layout.layer(gds_num, dtype)

    def _gds(self, key: str) -> int:
        """Resolve a named gds_layer (from layer_stack.gds_layers) to a
        klayout layer index. Key must be one of GDS_KEYS."""
        gds_layer, gds_datatype = self.ls.gds_layers[key]
        return self._lyr(gds_layer, gds_datatype)

    def _metal_layer_idx(self, lgg_idx: int) -> Optional[int]:
        name = self._metal_idx_to_name.get(lgg_idx)
        m = self._name_to_metal.get(name) if name else None
        if m is None or m.gds_layer is None:
            return None
        return self._lyr(m.gds_layer, m.gds_datatype)

    def _via_layer_idx(self, lo_idx: int, hi_idx: int) -> Optional[int]:
        lo_name = self._metal_idx_to_name.get(lo_idx)
        hi_name = self._metal_idx_to_name.get(hi_idx)
        v = self.ls.via_layers.get((lo_name, hi_name)) or self.ls.via_layers.get((hi_name, lo_name))
        if v is None or v.gds_layer is None:
            return None
        return self._lyr(v.gds_layer, v.gds_datatype)

    # --- draw entry points ---
    def _draw(self):
        self._draw_boundary()
        self._draw_wells_and_selects()
        self._draw_power_rails()
        self._draw_gate_columns()           # full PC1 + BPC1 bars at every CPP
        self._draw_fins()                   # substrate fin grid (front + back)
        self._draw_actives_and_gates()      # active OD rects per transistor
        self._draw_lisd_sdt()               # LISD/BLISD + SDT/BSDT at S/D cols
        self._draw_gate_cuts()              # gate-cut boxes where nets differ
        self._draw_routing()                # M/V layer routing + via enclosure
        if self.draw_virtual:
            self._draw_virtual_jumps()

    def _draw_boundary(self):
        _box(self.cell, self._gds("BOUNDARY"), 0, 0, self.cell_width, self.cell_height)

    def _draw_wells_and_selects(self):
        mid = self.cell_height / 2
        # Front: pwell+pselect on top, nselect on bottom
        _box(self.cell, self._gds("WELL_FRONT"),    0, mid, self.cell_width, self.cell_height)
        _box(self.cell, self._gds("PSELECT_FRONT"), 0, mid, self.cell_width, self.cell_height)
        _box(self.cell, self._gds("NSELECT_FRONT"), 0, 0,   self.cell_width, mid)
        # Back: mirror (stacked under front in 3D, here drawn on backside layers)
        _box(self.cell, self._gds("WELL_BACK"),    0, mid, self.cell_width, self.cell_height)
        _box(self.cell, self._gds("PSELECT_BACK"), 0, mid, self.cell_width, self.cell_height)
        _box(self.cell, self._gds("NSELECT_BACK"), 0, 0,   self.cell_width, mid)

    def _draw_power_rails(self):
        """VSS rail straddles y=0 (bottom cell edge), VDD straddles
        y=cell_height (top). Drawn on M0 (front) AND BM0 (back). Mirrors
        upstream FinFET __m0_bpr__:

            ly_1 = -power_rail_thickness / 2
            uy_1 = +power_rail_thickness / 2
            ly_2 = -rail/2 + m0_pitch * (track + 2)   # == cell_h - rail/2
            uy_2 = ly_2 + power_rail_thickness

        Rail thickness defaults to m0_pitch (24 nm) - matches PROBE3 FinFET
        power_rail_thickness; tune via _RAIL_THICKNESS if the tech file
        ever varies it.

        Power-tied routes the solver places at y=0 / y=cell_h will visually
        merge with the rails (electrically identical -> rail IS the wire).
        Signal-net routes at intermediate y stay clear. Routes at rail
        rows that aren't power-tied indicate a solver-policy issue (signal
        on rail row); upstream solver reserves rail rows for power and
        never lands signals there, so this stays a noisy edge case.
        """
        rail_h = self.m0_pitch
        for layer_name in ("M0", "BM0"):
            ml = self._name_to_metal.get(layer_name)
            if ml is None or ml.gds_layer is None:
                continue
            lidx = self._lyr(ml.gds_layer, ml.gds_datatype)
            # Bottom (VSS) rail straddling y=0
            _box(self.cell, lidx,
                 0, -rail_h / 2, self.cell_width, rail_h / 2)
            _text(self.cell, lidx, "VSS", self.cell_width / 2, 0)
            # Top (VDD) rail straddling y=cell_height
            _box(self.cell, lidx,
                 0, self.cell_height - rail_h / 2,
                 self.cell_width, self.cell_height + rail_h / 2)
            _text(self.cell, lidx, "VDD", self.cell_width / 2,
                  self.cell_height)

    # ----- gate columns (full bars at every CPP - upstream finfet style) ---
    def _draw_gate_columns(self):
        """Draw full-height gate poly bars at every CPP column for both PC1
        (front) and BPC1 (back) tiers. Matches upstream FinFET __gate__: the
        gate layer geometry is always intact across the cell; per-column
        cuts (where neighbouring transistors have different gate nets) are
        applied separately on the GATE_CUT_* layer via _draw_gate_cuts.

        Column positions are placed at every cp_pitch step from x=0 to
        cell_width (gate centers land at i*cp_pitch). Bar height spans the
        full pin-access band (y=0..cell_height) minus a small breathing
        room so the gate doesn't smear into the power rails.
        """
        cp_pitch = self.placement_pitch
        for tier in ("BPC1", "PC1"):
            ml = self._name_to_metal.get(tier)
            if ml is None or ml.gds_layer is None:
                continue
            lidx = self._lyr(ml.gds_layer, ml.gds_datatype)
            gate_width = ml.width
            # Bar spans the cell vertically with a 5nm clear from each rail.
            ly = 5.0
            uy = self.cell_height - 5.0
            # cell_width = cp_pitch * COL -> gates at i*cp_pitch for i=0..COL.
            # (Inclusive endpoint puts a bar at the right boundary too.)
            i = 0
            while True:
                gx = i * cp_pitch
                if gx > self.cell_width:
                    break
                _box(self.cell, lidx,
                     gx - gate_width / 2, ly,
                     gx + gate_width / 2, uy)
                i += 1

    def _draw_actives_and_gates(self):
        """Active (OD) rect per transistor, drawn in the upstream FinFET style:
        shifted by _ACTIVE_X_OFFSET and widened by _ACTIVE_X_OVERLAP so adjacent
        diffusions merge into continuous OD, and placed in a scaled active band
        (PMOS upper half, NMOS lower half). The tier (front PC1 / back BPC1)
        selects the active layer; gate poly is drawn by _draw_gate_columns.

        X/Y/height formulas are from gds_FinFET_SH.py __pmos__ / __nmos__
        (t.x / t.y / t.width / t.height are solver units, same as upstream).
        """
        denom = self.m0_pitch * SOLVER_RESCALE          # upstream m0_pitch*RESCALE
        active_y_offset = (self.cell_height
                           - _ACTIVE_HEIGHT * 2 - _ACTIVE_GAP) / 2
        for t in self.trans:
            is_back = (t.z == "BPC1")
            act_key = (
                "ACTIVE_BACK_P"  if (is_back and t.model == "PMOS")
                else "ACTIVE_BACK_N"  if is_back
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
            _box(self.cell, self._gds(act_key),
                 lx, ly, lx + width, ly + height)

    def _draw_fins(self):
        """Horizontal fin stripes across the whole cell, in the upstream FinFET
        style (gds_FinFET_SH.__fin__): a row of fins from _FIN_Y_OFFSET upward
        at (_FIN_PITCH + _FIN_WIDTH) spacing until the top. Fins are a substrate
        grid, so they're drawn on the front (layer 2) AND back (layer 502) fin
        layers independent of device placement - one fin set per tier."""
        for gds_layer, gds_dtype in (_FIN_FRONT_GDS, _FIN_BACK_GDS):
            lidx = self._lyr(gds_layer, gds_dtype)
            curr_y = _FIN_Y_OFFSET
            while True:
                _box(self.cell, lidx,
                     0, curr_y, self.cell_width, curr_y + _FIN_WIDTH)
                curr_y += _FIN_PITCH + _FIN_WIDTH
                if curr_y + _FIN_WIDTH > self.cell_height:
                    break

    # ----- LISD / SDT (upstream-faithful port from gds_FinFET_SH.py) ------
    @staticmethod
    def _is_power_net(net):
        return bool(net) and (net.startswith("VDD") or net.startswith("VSS"))

    @classmethod
    def _is_lisd_power_at_col(cls, trans, col):
        """Mirror upstream finfet's _is_lisd_power_at_col: True iff the pin
        at this transistor's `col` (or the opposite side, when -1) is power.
        Used to switch between signal-LISD and the taller power-LISD."""
        if trans.s_col == col and cls._is_power_net(trans.s_net):
            return True
        if trans.d_col == col and cls._is_power_net(trans.d_net):
            return True
        # Opposite side has -1 column AND power net -> still counts as power
        if trans.s_col != col and trans.d_col in (-1, None) and cls._is_power_net(trans.d_net):
            return True
        if trans.d_col != col and trans.s_col in (-1, None) and cls._is_power_net(trans.s_net):
            return True
        return False

    @staticmethod
    def _left_net(t):
        return t.d_net if t.flip else t.s_net

    @staticmethod
    def _right_net(t):
        return t.s_net if t.flip else t.d_net

    def _pmos_nmos_pairs_per_tier(self):
        """Group transistors into (PMOS, NMOS) pairs at the same X within
        each tier. Mirrors upstream's `zip(pmos_sorted, nmos_sorted)` but
        per-tier and per-column (since QFET allows multiple trans per row)."""
        pairs = []  # list of (tier, ptran, ntran) - both at same x
        for tier in ("BPC1", "PC1"):
            pms = sorted([t for t in self.trans if t.z == tier and t.model == "PMOS"],
                         key=lambda t: t.x)
            nms = sorted([t for t in self.trans if t.z == tier and t.model == "NMOS"],
                         key=lambda t: t.x)
            for p in pms:
                n = next((n for n in nms if n.x == p.x), None)
                if n is not None:
                    pairs.append((tier, p, n))
        return pairs

    def _lisd_idx(self, tier):
        return self._gds("BLISD1" if tier == "BPC1" else "LISD1")

    def _sdt_idx(self, tier):
        return self._gds("BSDT1" if tier == "BPC1" else "SDT1")

    def _draw_lisd_one(self, tier, trans, col, is_power):
        """Emit LISD + SDT at `col` for a single transistor (PMOS or NMOS).
        Formulas ported verbatim from upstream __lisd_pmos__ / __lisd_nmos__
        with QFET's display-Y scale (transistor.y already physical)."""
        m0p = self.m0_pitch
        # Upstream denominator: m0_pitch * SOLVER_RESCALE * 2 = 24*2*2 = 96.
        # QFET emits Y in display units directly (no /SOLVER_RESCALE for Y),
        # so to keep the upstream FORMULA we use the same literal denominator.
        denom = m0p * SOLVER_RESCALE * 2
        lx = _LISD_X_OFFSET + col / SOLVER_RESCALE

        # ---- LISD strip ----
        if trans.model == "PMOS":
            if is_power:
                ly = _LISD_Y_OFFSET + trans.y / denom * _LISD_SIGNAL_HEIGHT + _LISD_POWER_GAP
                hgt = _LISD_POWER_HEIGHT
            else:
                ly = _LISD_Y_OFFSET + trans.y / denom * _LISD_SIGNAL_HEIGHT + _LISD_SIGNAL_GAP
                hgt = _LISD_SIGNAL_HEIGHT
        else:  # NMOS
            if is_power:
                ly = trans.y / denom * _LISD_SIGNAL_HEIGHT
                hgt = _LISD_POWER_HEIGHT
            else:
                ly = _LISD_Y_OFFSET + trans.y / denom * _LISD_SIGNAL_HEIGHT
                hgt = _LISD_SIGNAL_HEIGHT
        _box(self.cell, self._lisd_idx(tier),
             lx, ly, lx + _LISD_WIDTH, ly + hgt)

        # ---- SDT contact (Y formula independent of is_power upstream) ----
        if trans.model == "PMOS":
            sdt_ly = _SDT_Y_OFFSET + trans.y / denom * _SDT_HEIGHT + _SDT_GAP
        else:
            sdt_ly = _SDT_Y_OFFSET + trans.y / denom * _SDT_HEIGHT
        _box(self.cell, self._sdt_idx(tier),
             lx, sdt_ly, lx + _LISD_WIDTH, sdt_ly + _SDT_HEIGHT)

    def _draw_lisd_merged(self, tier, col):
        """One tall LISD covering both PMOS and NMOS regions when they share
        the same S/D net at the same column. Drawn on the LISD layer AND
        the SDT layer (upstream behavior)."""
        lx = _LISD_X_OFFSET + col / SOLVER_RESCALE
        ly = _LISD_Y_OFFSET
        height = _LISD_SIGNAL_HEIGHT * 2 + _LISD_SIGNAL_GAP
        for lidx in (self._lisd_idx(tier), self._sdt_idx(tier)):
            _box(self.cell, lidx,
                 lx, ly, lx + _LISD_WIDTH, ly + height)

    def _draw_lisd_sdt(self):
        """For each (PMOS, NMOS) pair on each tier, emit LISD+SDT at the
        pair's LEFT and RIGHT diffusion columns. If the pair's left (or
        right) net matches across PMOS and NMOS, emit one merged LISD;
        otherwise emit independent PMOS/NMOS LISDs (each marked power when
        its pin is VDD/VSS-tied)."""
        for tier, p, n in self._pmos_nmos_pairs_per_tier():
            # Left and right diffusion columns of the transistor pair.
            # Each transistor occupies [x, x+width] with diffusion at both ends.
            left_col, right_col = p.x, p.x + p.width
            # ---- LEFT side ----
            if self._left_net(p) == self._left_net(n):
                self._draw_lisd_merged(tier, left_col)
            else:
                self._draw_lisd_one(tier, p, left_col,
                                    self._is_lisd_power_at_col(p, left_col))
                self._draw_lisd_one(tier, n, left_col,
                                    self._is_lisd_power_at_col(n, left_col))
            # ---- RIGHT side ----
            if self._right_net(p) == self._right_net(n):
                self._draw_lisd_merged(tier, right_col)
            else:
                self._draw_lisd_one(tier, p, right_col,
                                    self._is_lisd_power_at_col(p, right_col))
                self._draw_lisd_one(tier, n, right_col,
                                    self._is_lisd_power_at_col(n, right_col))

    # ----- gate cuts (front + back) ---------------------------------------
    def _draw_gate_cuts(self):
        """Emit a small cut box at every gate column where the transistor
        on that column has its own gate net (i.e. the column is occupied
        by exactly one trans whose gate net differs from the adjacent tier
        OR where no trans on either side shares the gate).

        Conservative implementation mirroring upstream: for each (column,
        tier) where a transistor lands, compare PMOS gate_net vs NMOS
        gate_net on the SAME tier (rows 0/2). If they differ, the gate
        between them is cut -> emit a cut box at the midline. Tier-aware:
        front-tier cuts on GATE_CUT_FRONT, back-tier on GATE_CUT_BACK.
        """
        # Group transistors by (tier, gate_col, model)
        by_tier_col = {}  # (tier, g_col) -> {"PMOS": net, "NMOS": net}
        for t in self.trans:
            if t.g_col is None or t.g_col < 0:
                continue
            key = (t.z, t.g_col)
            slot = by_tier_col.setdefault(key, {})
            slot[t.model] = t.g_net

        cell_mid_y = self.cell_height / 2
        cp_width = self._name_to_metal["PC1"].width
        for (tier, g_col), nets in by_tier_col.items():
            if nets.get("PMOS") == nets.get("NMOS"):
                continue  # gate shared (or only one model present) - no cut
            cut_idx = self._gds("GATE_CUT_BACK" if tier == "BPC1" else "GATE_CUT_FRONT")
            cx = g_col / SOLVER_RESCALE
            _box(self.cell, cut_idx,
                 cx - cp_width / 2, cell_mid_y - _GATE_CUT_HEIGHT / 2,
                 cx + cp_width / 2, cell_mid_y + _GATE_CUT_HEIGHT / 2)

    def _draw_routing(self):
        """Same-layer routing -> metal box, extended past endpoints by the
        layer's direction-based via enclosure. Adjacent-layer -> via. VL
        non-adjacent jumps go to _draw_virtual_jumps.

        Upstream policy match: same-layer routing on the GATE LAYERS
        (BPC1 / PC1) is NEVER drawn as a wire - the gate poly itself is
        already the full vertical bar at every CPP (_draw_gate_columns).
        Any solver-emitted same-layer arc on a gate layer is a logical
        pickup at the gate node, not a physical wire. Cross-layer vias
        landing ON the gate layer are still drawn (CA / BCA at gate cols).
        """
        gate_layers = {self._name_to_metal[t].layer_name for t in ("BPC1", "PC1")
                       if t in self._name_to_metal}
        gate_lgg_idx = {i for i, n in self._metal_idx_to_name.items() if n in gate_layers}

        for s in self.segs:
            if s.layer_u == s.layer_v:
                # Skip same-layer arcs on gate layers (upstream behavior).
                if s.layer_u in gate_lgg_idx:
                    continue
                lidx = self._metal_layer_idx(s.layer_u)
                if lidx is None:
                    continue
                name = self._metal_idx_to_name[s.layer_u]
                ml = self._name_to_metal[name]
                # Per-metal wire-end via enclosure, read from the layer JSON so
                # GDS geometry is controlled by the layer stack. The JSON values
                # match the legacy direction defaults (_METAL_VIA_ENCLOSURE):
                # H metals -> (3, 0), V metals -> (0.5, 5).
                h_ovl, v_ovl = ml.horizontal_enclosure, ml.vertical_enclosure
                lx, ly, ux, uy = _wire_box(s, ml.direction, ml.width, h_ovl, v_ovl)
                _box(self.cell, lidx, lx, ly, ux, uy)
                if name in ("M0", "BM0") and s.net in self.io_pins:
                    _text(self.cell, lidx, s.net, (lx + ux) / 2, (ly + uy) / 2)
            elif abs(s.layer_v - s.layer_u) == 1:
                lo, hi = sorted((s.layer_u, s.layer_v))
                vidx = self._via_layer_idx(lo, hi)
                if vidx is None:
                    continue
                lx, ly, ux, uy = _via_box(s.col_u, s.row_u)
                _box(self.cell, vidx, lx, ly, ux, uy)

    def _draw_virtual_jumps(self):
        """Mark every (row, col) where a virtual jump endpoint lands, on the
        VL entry's gds_layer. Debug-only - purely informational."""
        for vname, vinfo in self.ls.virtual_layers.items():
            gds = vinfo.get("gds_layer")
            if gds is None:
                continue
            lo = self._name_to_metal.get(vinfo["lower_layer"])
            hi = self._name_to_metal.get(vinfo["upper_layer"])
            if lo is None or hi is None:
                continue
            lidx = self._lyr(gds, vinfo.get("gds_datatype", 0))
            # Solver pairs jumps on the coarser placement pitch (where tier
            # nodes overlap); replicate that here as a sparse marker grid.
            join_pitch = max(lo.pitch, hi.pitch)
            mark = self._name_to_metal["M0"].width
            x = 0.0
            while x <= self.cell_width:
                y = 0.0
                while y <= self.cell_height:
                    _box(self.cell, lidx,
                         x - mark / 2, y - mark / 2,
                         x + mark / 2, y + mark / 2)
                    y += self.m0_pitch
                x += join_pitch


def _resolve_gds_path(args) -> str:
    """Decide standalone vs library-append target.

    Precedence:
      1. --gds <path>   : explicit (legacy + override)
      2. cell config "append_gds" flag (default True) under --gds-dir/--lib-name
      3. fallback to --gds-dir/<subckt>.gds when flag missing
    """
    if args.gds:
        return args.gds
    if not args.gds_dir or not args.lib_name:
        raise SystemExit(
            "Need either --gds or both --gds-dir + --lib-name"
        )
    append = True
    if args.cell_config:
        import json
        with open(args.cell_config) as f:
            cfg = json.load(f)
        entry = cfg.get("append_gds")
        if isinstance(entry, dict) and "value" in entry:
            append = bool(entry["value"])
        elif isinstance(entry, bool):
            append = entry
    fname = f"{args.lib_name}.gds" if append else f"{args.subckt}.gds"
    return os.path.join(args.gds_dir, fname)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--result",  required=True, help="Path to .res file")
    p.add_argument("--layer",   required=True, help="Path to layer JSON")
    p.add_argument("--subckt",  required=True, help="Cell name to write")
    p.add_argument("--gds",     default=None,
                   help="Explicit output GDS path (overrides --gds-dir + --lib-name)")
    p.add_argument("--gds-dir", default=None,
                   help="Output directory; combined with --lib-name and the cell "
                        "config's append_gds flag to pick the target filename")
    p.add_argument("--lib-name", default=None,
                   help="Library name (used as <lib>.gds when append_gds=True)")
    p.add_argument("--cell-config", default=None,
                   help="Path to cell config JSON; reads append_gds flag if set")
    p.add_argument("--draw-virtual", action="store_true",
                   help="Draw VL virtual jumps on debug layers (700/701)")
    args = p.parse_args()
    gds_file = _resolve_gds_path(args)
    QFETLayout(args.result, args.layer, args.subckt, gds_file,
               draw_virtual=args.draw_virtual)


if __name__ == "__main__":
    main()
