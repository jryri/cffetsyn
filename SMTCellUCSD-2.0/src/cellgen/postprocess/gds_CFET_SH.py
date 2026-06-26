import klayout.db as pya
import math
from typing import NamedTuple, List, Tuple
import re
import argparse
import os

import logging
from loguru import logger

from src.cellgen.core.entity import Model

SOLVER_RESCALE = 2
PERCISION_DIGITS = 4
SCALE = 10  # 1nm = 0.001um

# Set up logging to print messages
# Custom log format: [LEVEL] TIMESTAMP - MESSAGE
# NOTE: level available: DEBUG, INFO, WARNING, ERROR, CRITICAL
logging.basicConfig(format="[%(levelname)s] %(asctime)s - %(message)s", level=logging.INFO)


class TechData(NamedTuple):
    """a docstring"""

    col: int
    track: int
    cp_pitch: float
    m0_pitch: float
    m1_pitch: float
    m2_pitch: float
    cp_width: float
    m0_width: float
    m1_width: float
    m2_width: float
    active_gap: float
    m0_power_rail_thickness: float
    power_config: str
    io_pins: list


class TransistorData(NamedTuple):
    """a docstring"""

    name: str
    x: float
    y: float
    flip: bool
    width: float
    height: float
    source_row: float
    source_col: float
    source_net: str
    drain_row: float
    drain_col: float
    drain_net: str
    gate_row: float
    gate_col: float
    gate_net: str
    model: str


class MetalData(NamedTuple):
    """a docstring"""

    metal_0: int
    metal_1: int
    row_0: float
    row_1: float
    col_0: float
    col_1: float
    net: str


# custom module
class TextShape:
    """
    Class to create a text shape object.
    Because not fond of klayout's text object
    """

    def __init__(self, textstring, x, y):
        self.textstring = textstring
        self.x = round(x, PERCISION_DIGITS) * SCALE
        self.y = round(y, PERCISION_DIGITS) * SCALE

    def pyatext_obj(self):
        return pya.Text(self.textstring, x=self.x, y=self.y)


class Box:
    """
    Class to create a box shape object.
    Because python cannot handle precision of klayout's box object
    """

    def __init__(self, lx, ly, ux, uy):
        self.lx = round(lx, PERCISION_DIGITS)
        self.ly = round(ly, PERCISION_DIGITS)
        self.ux = round(ux, PERCISION_DIGITS)
        self.uy = round(uy, PERCISION_DIGITS)

    def pyabox_obj(self):
        return pya.Box(
            pya.Point(self.lx * SCALE, self.ly * SCALE),
            pya.Point(self.ux * SCALE, self.uy * SCALE),
        )


class Box2:
    """
    Class to create a box shape object.
    Because python cannot handle precision of klayout's box object
    """

    def __init__(self, lx, ly, w, h):
        self.lx = round(lx, PERCISION_DIGITS)
        self.ly = round(ly, PERCISION_DIGITS)
        self.ux = round(lx + w, PERCISION_DIGITS)
        self.uy = round(ly + h, PERCISION_DIGITS)

    def pyabox_obj(self):
        return pya.Box(
            pya.Point(self.lx * SCALE, self.ly * SCALE),
            pya.Point(self.ux * SCALE, self.uy * SCALE),
        )


class CFETLayout:
    def __init__(self, result_file, subckt_name, gds_file=None, layer_file=None):
        self.result_file = result_file
        self.subckt_name = subckt_name
        # Wire-end via enclosure overhang per metal layer, read from the layer
        # JSON (--layer) so GDS geometry is controlled by the layer stack rather
        # than hardcoded. Falls back to the legacy constants when a layer is
        # absent from the JSON (or when no --layer is supplied).
        self._encl = {}
        if layer_file is not None:
            from src.cellgen.core.entity import LayerStack
            self._encl = {
                m.layer_name: (m.horizontal_enclosure, m.vertical_enclosure)
                for m in LayerStack(layer_file).metal_layers
            }
        self.tech_data, self.pmos_transistor_data, self.nmos_transistor_data, self.metal_data = self._parse_result(self.result_file)
        # sort pmos_transistor_data by x
        self.pmos_transistor_data.sort(key=lambda x: x.x)
        # sort nmos_transistor_data by x
        self.nmos_transistor_data.sort(key=lambda x: x.x)

        self.width = self.tech_data.cp_pitch * (self.tech_data.col)
        if self.tech_data.power_config == "M0ICPD":
            self.height = self.tech_data.m0_pitch * self.tech_data.track * 2
        else:
            self.height = self.tech_data.m0_pitch * (self.tech_data.track + 2)
        # add these to tech_data
        self.fin_y_offset = 21
        self.fin_pitch = 18
        self.fin_width = 6
        self.poly_height = 154
        self.active_x_overlap = 14
        self.active_height = 46
        self.active_x_offset = -7
        self.active_y_offset = (self.height - self.active_height * 2 - self.tech_data.active_gap) / 2
        self.gate_cut_height = 37
        self.lig_width = 14
        self.lig_height = 14
        self.lisd_width = 16
        self.lisd_x_offset = -8
        self.lisd_signal_height = 48
        self.lisd_power_height = 66
        self.lisd_signal_gap = 8
        self.lisd_power_gap = 10
        self.lisd_y_offset = 20
        self.sdt_gap = 12
        self.sdt_y_offset = 24
        self.sdt_height = 42
        self.ca_x_offset = -7
        self.ca_y_offset = 29
        self.ca_width = 14
        self.ca_height = 14
        self.m0_y_offset = 29
        self.m0_x_offset = -7
        self.m0_h_ovl, self.m0_v_ovl = self._encl.get("M0", (3, 0))
        self.v0_y_offset = 29
        self.v0_x_offset = -7
        self.v0_width = 14
        self.v0_height = 14
        self.m1_y_offset = 29
        self.m1_x_offset = -7
        self.m1_h_ovl, self.m1_v_ovl = self._encl.get("M1", (0.5, 5))
        self.v1_y_offset = 29
        self.v1_x_offset = -7
        self.v1_width = 14
        self.v1_height = 14
        self.m2_y_offset = 29
        self.m2_x_offset = -7
        self.m2_h_ovl, self.m2_v_ovl = self._encl.get("M2", (5, 0))

        if self.tech_data.power_config == "M0ICPD":
            self.m0_y_offset = (self.tech_data.m0_pitch - self.tech_data.m0_width) / 2
            self.ca_y_offset = (self.tech_data.m0_pitch - self.ca_height) / 2
            self.v0_y_offset = (self.tech_data.m0_pitch - self.v0_height) / 2
            self.v1_y_offset = (self.tech_data.m0_pitch - self.v1_height) / 2
            self.m1_y_offset = (self.tech_data.m0_pitch - self.v0_height) / 2
            self.m2_y_offset = (self.tech_data.m0_pitch - self.tech_data.m2_width) / 2

        self.gds_file = gds_file

        # Handle existing GDS file (append mode) - same pattern as FinFET
        self.cell = None
        if gds_file and os.path.isfile(gds_file):
            self.layout = pya.Layout()
            self.layout.read(gds_file)
            logging.info(f"[WARNING] GDS PATH already exists: {gds_file}. Writing to an existing file.")
            # check if cell already exists - delete it so we can recreate
            for cell in self.layout.top_cells():
                if self.subckt_name == cell.name:
                    logging.info(f"[WARNING] {cell.name} is deleted.")
                    self.layout.delete_cell(cell.cell_index())
                    break
        else:
            logging.info(f"[INFO] Creating new GDS file: {gds_file}.")
            self.layout = pya.Layout()

        logging.info(f"[INFO] Creating new cell: {self.subckt_name}.")
        self.draw()
        if gds_file:
            self.save(gds_file)

    def _parse_result(self, path: str) -> Tuple[TechData, List[TransistorData], List[TransistorData], List[MetalData]]:
        tech_params = {}
        pmos_transistors: List[TransistorData] = []
        nmos_transistors: List[TransistorData] = []
        metals: List[MetalData] = []

        section = None
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                # detect section headers
                if line.startswith("**") and "Technology Parameters" in line:
                    section = "tech"
                    continue
                elif line.startswith("**") and "Placement Result" in line:
                    section = "place"
                    # skip header line
                    header = next(f)
                    continue
                elif line.startswith("**") and "Routing Result" in line:
                    section = "route"
                    # skip header
                    header = next(f)
                    continue
                elif line.startswith("**") and "Cell Information" in line:
                    section = "cell"
                    # skip header
                    header = next(f)

                if section == "tech":
                    # each line: KEY   VALUE
                    parts = line.split()
                    key = parts[0]
                    val = " ".join(parts[1:])
                    tech_params[key] = val

                elif section == "place":
                    # parse placement rows
                    # Name X Y Flip Width Height SrcRow SrcCol SrcNet DrnRow DrnCol DrnNet GRow GCol GNet Model
                    parts = line.split()
                    if len(parts) < 13:
                        continue
                    name = parts[0]
                    x = float(parts[1])
                    y = float(parts[2])
                    flip_str = parts[3]
                    flip = flip_str == "F"
                    width = float(parts[4])
                    height = float(parts[5])
                    src_row = float(parts[6])
                    src_col = float(parts[7])
                    src_net = parts[8]
                    drn_row = float(parts[9])
                    drn_col = float(parts[10])
                    drn_net = parts[11]
                    gate_row = float(parts[12])
                    gate_col = float(parts[13])
                    gate_net = parts[14]
                    if parts[15].lower() == "pmos":
                        model = Model.PMOS
                        pmos_transistors.append(
                            TransistorData(
                                name,
                                x,
                                y,
                                flip,
                                width,
                                height,
                                src_row,
                                src_col,
                                src_net,
                                drn_row,
                                drn_col,
                                drn_net,
                                gate_row,
                                gate_col,
                                gate_net,
                                model,
                            )
                        )
                    elif parts[15].lower() == "nmos":
                        model = Model.NMOS
                        nmos_transistors.append(
                            TransistorData(
                                name,
                                x,
                                y,
                                flip,
                                width,
                                height,
                                src_row,
                                src_col,
                                src_net,
                                drn_row,
                                drn_col,
                                drn_net,
                                gate_row,
                                gate_col,
                                gate_net,
                                model,
                            )
                        )

                elif section == "route":
                    # each line: M0 ROW0 COL0 NET => M1 ROW1 COL1 NET
                    m = re.match(r"(\d+)\s+([\d\.]+)\s+([\d\.]+)\s+(\S+)\s*=>\s*(\d+)\s+([\d\.]+)\s+([\d\.]+)\s+(\S+)", line)
                    if not m:
                        continue
                    m0, r0, c0, net0, m1, r1, c1, net1 = m.groups()
                    assert net0 == net1, f"Net mismatch in routing data, {net0} != {net1}"
                    # net0 and net1 should be identical
                    metals.append(
                        MetalData(metal_0=int(m0), metal_1=int(m1), row_0=float(r0), row_1=float(r1), col_0=float(c0), col_1=float(c1), net=net0)
                    )
                elif section == "cell":
                    pins = line.split()
                    tech_params["IO_PINS"] = pins

        # build TechData, converting types
        td = TechData(
            col=int(tech_params["COL"]),
            track=int(tech_params["TRACK"]),
            cp_pitch=float(tech_params["CPP"]),
            m0_pitch=float(tech_params["M0P"]),
            m1_pitch=float(tech_params["M1P"]),
            m2_pitch=float(tech_params["M2P"]),
            cp_width=float(tech_params["CP_WIDTH"]),
            m0_width=float(tech_params["M0_WIDTH"]),
            m1_width=float(tech_params["M1_WIDTH"]),
            m2_width=float(tech_params["M2_WIDTH"]),
            active_gap=float(tech_params["ACTIVE_GAP"]),
            m0_power_rail_thickness=float(tech_params["M0_PWR_RAIL_THICKNESS"]),
            power_config=tech_params["PWR_CONFIG"],
            io_pins=tech_params["IO_PINS"]
        )

        return td, pmos_transistors, nmos_transistors, metals

    def draw(self):
        """
        Layer naming convention:
        <layer_number>/<layer_type>
        layer_number: 0-255
        layer_type: 0-255
            - 0         :   DR layer
            - 251       :   Text layer
            - 1-199     :   Auxiliary layer
            - 200-250   :   Auxiliary text layer
        """
        self.layout.dbu = 0.0001  # 1nm
        # create a new cell
        self.cell = self.layout.create_cell(self.subckt_name)
        # Well layer (1/0)
        self.well_layer_idx = self.layout.layer(1, 0)
        self.well_text_layer_idx = self.layout.layer(1, 251)
        # Fin layer (2/0)
        self.fin_layer_idx = self.layout.layer(2, 0)
        # PSUB Text layer (3/251)
        self.psub_text_layer_idx = self.layout.layer(3, 251)
        # Gate layer (7/0)
        self.gate_layer_idx = self.layout.layer(7, 0)
        # Gate cut layer (10/0)
        self.gate_cut_layer_idx = self.layout.layer(10, 0)
        # Active layer (11/0)
        self.active_layer_idx = self.layout.layer(11, 0)
        self.p_active_layer_idx = self.layout.layer(11, 1)
        self.p_active_text_layer_idx = self.layout.layer(11, 201)
        self.n_active_layer_idx = self.layout.layer(11, 2)
        self.n_active_text_layer_idx = self.layout.layer(11, 202)
        # NSELECT layer (12/0)
        self.nselect_layer_idx = self.layout.layer(12, 0)
        # PSELECT layer (13/0)
        self.pselect_layer_idx = self.layout.layer(13, 0)
        # CA DR layer (14/0)
        self.CA_layer_idx = self.layout.layer(14, 0)
        self.CA_SD_layer_idx = self.layout.layer(14, 1)
        self.CA_GB_layer_idx = self.layout.layer(14, 2)
        self.CA_debug_layer_idx = self.layout.layer(14, 50)
        self.CA_upper_debug_layer_idx = self.layout.layer(14, 51)
        self.CA_lower_debug_layer_idx = self.layout.layer(14, 51)
        self.CA_debug_text_layer_idx = self.layout.layer(14, 250)
        # M0 DR layer (15/0)
        self.M0_layer_idx = self.layout.layer(15, 0)
        self.M0_text_layer_idx = self.layout.layer(15, 251)
        self.M0_debug_layer_idx = self.layout.layer(15, 50)
        self.M0_debug_text_layer_idx = self.layout.layer(15, 250)
        # LIG layer (16/0)
        self.lig_layer_idx = self.layout.layer(16, 0)
        self.lig_text_layer_idx = self.layout.layer(16, 251)
        self.lig_debug_layer_idx = self.layout.layer(16, 50)
        self.lig_debug_text_layer_idx = self.layout.layer(16, 250)
        # LISD layer (17/0)
        self.LISD_layer_idx = self.layout.layer(17, 0)
        self.p_LISD_layer_idx = self.layout.layer(17, 1)
        self.n_LISD_layer_idx = self.layout.layer(17, 2)
        self.LISD_text_layer_idx = self.layout.layer(17, 251)
        self.LISD_debug_layer_idx = self.layout.layer(17, 50)
        self.LISD_debug_text_layer_idx = self.layout.layer(17, 250)
        # V0 layer (18/0)
        self.V0_layer_idx = self.layout.layer(18, 0)
        self.V0_text_layer_idx = self.layout.layer(18, 251)
        self.V0_debug_layer_idx = self.layout.layer(18, 50)
        self.V0_debug_text_layer_idx = self.layout.layer(18, 250)
        # M1 DR layer (19/0)
        self.M1_layer_idx = self.layout.layer(19, 0)
        self.M1_text_layer_idx = self.layout.layer(19, 251)
        self.M1_debug_layer_idx = self.layout.layer(19, 50)
        self.M1_debug_text_layer_idx = self.layout.layer(19, 250)
        # V1 layer (21/0)
        self.V1_layer_idx = self.layout.layer(21, 0)
        self.V1_text_layer_idx = self.layout.layer(21, 251)
        self.V1_debug_layer_idx = self.layout.layer(21, 50)
        self.V1_debug_text_layer_idx = self.layout.layer(21, 250)
        # M2 DR layer (20/0)
        self.M2_layer_idx = self.layout.layer(20, 0)
        self.M2_text_layer_idx = self.layout.layer(20, 251)
        self.M2_debug_layer_idx = self.layout.layer(20, 50)
        self.M2_debug_text_layer_idx = self.layout.layer(20, 250)
        # SDT layer (88/0)
        self.SDT_layer_idx = self.layout.layer(88, 0)
        self.p_SDT_layer_idx = self.layout.layer(88, 1)  # CFET FLAG
        self.n_SDT_layer_idx = self.layout.layer(88, 2)  # CFET FLAG
        self.SDT_text_layer_idx = self.layout.layer(88, 251)
        self.SDT_debug_layer_idx = self.layout.layer(88, 50)
        # BPC layer (6/0) - Bottom Placement Contact layer for NMOS in CFET
        self.BPC_layer_idx = self.layout.layer(6, 0)
        self.BPC_text_layer_idx = self.layout.layer(6, 251)
        self.BPC_debug_layer_idx = self.layout.layer(6, 50)
        # PC layer (7/1) - Placement Contact layer for PMOS in CFET (using 7/1 to differentiate from gate)
        self.PC_layer_idx = self.layout.layer(7, 1)
        self.PC_text_layer_idx = self.layout.layer(7, 201)
        self.PC_debug_layer_idx = self.layout.layer(7, 51)
        # Boundary layer (100/0)
        self.boundary_layer_idx = self.layout.layer(100, 0)
        # Draw the power rail
        self.draw_power_rail()

        self.draw_boundary()
        self.draw_auxiliary_layers()
        self.draw_gate()
        self.draw_fin()
        self.draw_actives()
        self.draw_routes()

    def save(self, filename):
        self.layout.write(filename)

    def draw_boundary(self):
        """
        Draw the boundary of the layout
        """
        boundary_box = Box(0, 0, self.width, self.height).pyabox_obj()
        self.cell.shapes(self.boundary_layer_idx).insert(boundary_box)

    def draw_auxiliary_layers(self):
        """
        Draw the auxiliary layers
        """
        self.__well__()
        self.__pselect__()
        self.__nselect__()
        self.__gate_cut_boundary__(self.tech_data.col)

    def draw_gate(self):
        self.__gate__(self.tech_data.col)

    def draw_fin(self):
        self.__fin__()

    def draw_power_rail(self):
        if self.tech_data.power_config == "M0BPR":
            self.__m0_bpr__()
            self.__ca_on_m0_bpr__()
            self.__lig_on_m0_bpr__()
        elif self.tech_data.power_config == "M0ICPD":
            self.__m0_icpd__()
            self.__lig_on_m0_icpd__()
        else:
            raise ValueError(f"Power rail configuration {self.tech_data.power_config} not supported.")

    def _draw_lisd_pmos_at_col(self, ptran, ntran, col):
        """Helper: draw PMOS LISD at the given column with correct cut/power logic.
        Power LISDs must extend to the rail, so cut_above/cut_below are skipped when is_power is True.
        """
        is_power = self._is_lisd_power_at_col(ptran, col=col)
        if is_power:
            # Power LISD extends to rail - no cutting
            self.__lisd_pmos__(transistor=ptran, col=col, is_power=True)
        elif self._is_cut_pmos_above(col=col, nmos_transistor=ntran):
            self.__lisd_pmos__(transistor=ptran, col=col, is_power=False, cut_above=True)
        elif self._is_cut_pmos_below(col=col, nmos_transistor=ntran):
            self.__lisd_pmos__(transistor=ptran, col=col, is_power=False, cut_below=True)
        else:
            self.__lisd_pmos__(transistor=ptran, col=col, is_power=False)

    def draw_actives(self):
        for i, (ptran, ntran) in enumerate(zip(self.pmos_transistor_data, self.nmos_transistor_data)):
            assert ptran.x == ntran.x, f"PMOS and NMOS transistors must have the same x coordinate, but got PMOS: {ptran.x}, NMOS: {ntran.x}"
            # Draw PMOS
            self.__pmos__(ptran)
            # Draw NMOS
            self.__nmos__(ntran)
            # Left LISD
            if self._is_left_lisd_merged(ptran, ntran):
                self._lisd_merged(ptran.x)
            else:
                self._draw_lisd_pmos_at_col(ptran, ntran, col=ptran.x)
            self.__lisd_nmos__(transistor=ntran, col=ntran.x, is_power=self._is_lisd_power_at_col(ntran, col=ntran.x))

            # Right LISD
            if self._is_right_lisd_merged(ptran, ntran):
                self._lisd_merged(col=ptran.x + ptran.width)
            else:
                self._draw_lisd_pmos_at_col(ptran, ntran, col=ptran.x + ptran.width)
            self.__lisd_nmos__(transistor=ntran, col=ntran.x + ntran.width, is_power=self._is_lisd_power_at_col(ntran, col=ntran.x + ntran.width))

    def draw_routes(self):
        """
        Draw the routes.

        Layer indices for dual-layer CFET (BPC and PC):
          0: BPC (bottom placement layer for NMOS)
          1: PC (top placement layer for PMOS)
          2: M0
          3: M1
          4: M2

        Via connections:
          BPC/PC (0/1) -> M0 (2): CA layer
          M0 (2) -> M1 (3): V0 layer
          M1 (3) -> M2 (4): V1 layer
        """
        for metal in self.metal_data:
            assert metal.metal_0 <= metal.metal_1, f"Metal layer {metal.metal_0} cannot be greater than {metal.metal_1}"

            # Placement layers (BPC=0, PC=1)
            if metal.metal_0 == 0 and metal.metal_1 == 0:  # BPC layer (intra-layer, ignore for GDS)
                pass
            elif metal.metal_0 == 1 and metal.metal_1 == 1:  # PC layer (intra-layer, ignore for GDS)
                pass
            elif metal.metal_0 == 0 and metal.metal_1 == 1:  # BPC to PC via (stacked transistor connection)
                pass  # Handled by CFET stacking, no physical via needed

            # CA layer connections (placement to M0)
            elif metal.metal_0 == 0 and metal.metal_1 == 2:  # BPC to M0 (CA layer for NMOS)
                self.__ca__(metal)
                if self._is_on_poly(metal):
                    self.__lig__(metal)
            elif metal.metal_0 == 1 and metal.metal_1 == 2:  # PC to M0 (CA layer for PMOS)
                self.__ca__(metal)
                if self._is_on_poly(metal):
                    self.__lig__(metal)

            # M0 layer
            elif metal.metal_0 == 2 and metal.metal_1 == 2:  # M0 layer
                self.__m0__(metal)

            # V0 layer (M0 to M1)
            elif metal.metal_0 == 2 and metal.metal_1 == 3:  # V0 layer
                self.__v0__(metal)

            # M1 layer
            elif metal.metal_0 == 3 and metal.metal_1 == 3:  # M1 layer
                self.__m1__(metal)

            # V1 layer (M1 to M2)
            elif metal.metal_0 == 3 and metal.metal_1 == 4:  # V1 layer
                self.__v1__(metal)

            # M2 layer
            elif metal.metal_0 == 4 and metal.metal_1 == 4:  # M2 layer
                self.__m2__(metal)

            else:
                raise ValueError(f"Unsupported metal layer combination: {metal.metal_0}, {metal.metal_1}")

    def _route_row_to_gds(self, row: float) -> float:
        if self.tech_data.power_config == "M0ICPD":
            return row
        return row / SOLVER_RESCALE

    def __ca__(self, metal: MetalData, debug: bool = False):
        """
        Draw the CA layer
        """
        lx = metal.col_0 / SOLVER_RESCALE + self.ca_x_offset
        ly = self._route_row_to_gds(metal.row_0) + self.ca_y_offset
        ux = metal.col_1 / SOLVER_RESCALE + self.ca_x_offset + self.ca_width
        uy = self._route_row_to_gds(metal.row_1) + self.ca_y_offset + self.ca_height
        box = Box(lx, ly, ux, uy).pyabox_obj()
        self.cell.shapes(self.CA_layer_idx).insert(box)
        # CA text layer
        ca_text = TextShape(metal.net, (lx + ux) / 2, (ly + uy) / 2).pyatext_obj()
        self.cell.shapes(self.CA_layer_idx).insert(ca_text) if debug else None

    def __lig__(self, metal: MetalData, debug: bool = False):
        """
        Draw the LIG layer
        """
        lx = metal.col_0 / SOLVER_RESCALE + self.ca_x_offset
        ly = self._route_row_to_gds(metal.row_0) + self.ca_y_offset
        ux = metal.col_1 / SOLVER_RESCALE + self.ca_x_offset + self.lig_width
        uy = self._route_row_to_gds(metal.row_1) + self.ca_y_offset + self.lig_height
        box = Box(lx, ly, ux, uy).pyabox_obj()
        # LIG layer
        self.cell.shapes(self.lig_layer_idx).insert(box)

    def _is_on_poly(self, metal: MetalData) -> bool:
        """
        Check if the metal is on the poly layer
        """
        return (metal.col_0 // self.tech_data.cp_pitch) % 2 == 1 and (metal.col_1 // self.tech_data.cp_pitch) % 2 == 1

    def __v0__(self, metal: MetalData, debug: bool = False):
        """
        Draw the V0 layer
        """
        lx = metal.col_0 / SOLVER_RESCALE + self.v0_x_offset
        ly = self._route_row_to_gds(metal.row_0) + self.v0_y_offset
        ux = metal.col_1 / SOLVER_RESCALE + self.v0_x_offset + self.v0_width
        uy = self._route_row_to_gds(metal.row_1) + self.v0_y_offset + self.v0_height
        box = Box(lx, ly, ux, uy).pyabox_obj()
        self.cell.shapes(self.V0_layer_idx).insert(box)
        # V0 text layer
        v0_text = TextShape(metal.net, (lx + ux) / 2, (ly + uy) / 2).pyatext_obj()
        self.cell.shapes(self.V0_text_layer_idx).insert(v0_text) if debug else None

    def __v1__(self, metal: MetalData, debug: bool = False):
        """
        Draw the V1 layer
        """
        lx = metal.col_0 / SOLVER_RESCALE + self.v1_x_offset
        ly = self._route_row_to_gds(metal.row_0) + self.v1_y_offset
        ux = metal.col_1 / SOLVER_RESCALE + self.v1_x_offset + self.v1_width
        uy = self._route_row_to_gds(metal.row_1) + self.v1_y_offset + self.v1_height
        box = Box(lx, ly, ux, uy).pyabox_obj()
        self.cell.shapes(self.V1_layer_idx).insert(box)
        # V1 text layer
        v1_text = TextShape(metal.net, (lx + ux) / 2, (ly + uy) / 2).pyatext_obj()
        self.cell.shapes(self.V1_text_layer_idx).insert(v1_text) if debug else None

    def __m0__(self, metal: MetalData, debug: bool = False):
        """
        Draw the M0 layer
        """
        lx = metal.col_0 / SOLVER_RESCALE + self.m0_x_offset - self.m0_h_ovl
        ly = self._route_row_to_gds(metal.row_0) + self.m0_y_offset - self.m0_v_ovl
        ux = metal.col_1 / SOLVER_RESCALE + self.m0_x_offset + self.ca_width + self.m0_h_ovl  # use lower via to extend
        uy = self._route_row_to_gds(metal.row_1) + self.m0_y_offset + self.tech_data.m0_width + self.m0_v_ovl
        box = Box(lx, ly, ux, uy).pyabox_obj()
        self.cell.shapes(self.M0_layer_idx).insert(box)
        # M0 text layer
        m0_text = TextShape(metal.net, (lx + ux) / 2, (ly + uy) / 2).pyatext_obj()
        self.cell.shapes(self.M0_text_layer_idx).insert(m0_text) if debug else None

    def __m1__(self, metal: MetalData, debug: bool = False):
        """
        Draw the M1 layer
        """
        lx = metal.col_0 / SOLVER_RESCALE + self.m1_x_offset - self.m1_h_ovl
        ly = self._route_row_to_gds(metal.row_0) + self.m1_y_offset - self.m1_v_ovl
        ux = metal.col_1 / SOLVER_RESCALE + self.m1_x_offset + self.tech_data.m1_width + self.m1_h_ovl
        uy = self._route_row_to_gds(metal.row_1) + self.m1_y_offset + self.v0_height + self.m1_v_ovl  # use lower via to extend
        box = Box(lx, ly, ux, uy).pyabox_obj()
        self.cell.shapes(self.M1_layer_idx).insert(box)
        # M1 text layer
        m1_text = TextShape(metal.net, (lx + ux) / 2, (ly + uy) / 2).pyatext_obj()
        if metal.net in self.tech_data.io_pins or debug:
            self.cell.shapes(self.M1_text_layer_idx).insert(m1_text)

    def __m2__(self, metal: MetalData, debug: bool = False):
        """
        Draw the M2 layer
        """
        lx = metal.col_0 / SOLVER_RESCALE + self.m2_x_offset - self.m2_h_ovl
        ly = self._route_row_to_gds(metal.row_0) + self.m2_y_offset - self.m2_v_ovl
        ux = metal.col_1 / SOLVER_RESCALE + self.m2_x_offset + self.v1_width + self.m2_h_ovl  # use lower via to extend
        uy = self._route_row_to_gds(metal.row_1) + self.m2_y_offset + self.tech_data.m2_width + self.m2_v_ovl
        box = Box(lx, ly, ux, uy).pyabox_obj()
        self.cell.shapes(self.M2_layer_idx).insert(box)
        # M2 text layer
        m2_text = TextShape(metal.net, (lx + ux) / 2, (ly + uy) / 2).pyatext_obj()
        if metal.net in self.tech_data.io_pins or debug:
            self.cell.shapes(self.M2_text_layer_idx).insert(m2_text)

    def __well__(self):
        """
        Draw the well
        """
        lx = 0
        ly = self.height / 2
        ux = self.width
        uy = self.height
        well_box = Box(lx, ly, ux, uy).pyabox_obj()
        self.cell.shapes(self.well_layer_idx).insert(well_box)
        well_text = TextShape("VDD", self.width / 2, self.height).pyatext_obj()
        self.cell.shapes(self.well_text_layer_idx).insert(well_text)
        psub_text = TextShape("VSS", self.width / 2, 0).pyatext_obj()
        self.cell.shapes(self.psub_text_layer_idx).insert(psub_text)

    def __pselect__(self):
        """
        Draw the PSELECT layer
        """
        lx = 0
        ly = self.height / 2
        ux = self.width
        uy = self.height
        pselect_box = Box(lx, ly, ux, uy).pyabox_obj()
        self.cell.shapes(self.pselect_layer_idx).insert(pselect_box)

    def __nselect__(self):
        """
        Draw the NSELECT layer
        """
        lx = 0
        ly = 0
        ux = self.width
        uy = self.height / 2
        nselect_box = Box(lx, ly, ux, uy).pyabox_obj()
        self.cell.shapes(self.nselect_layer_idx).insert(nselect_box)

    def __gate_cut_boundary__(self, num_gate):
        """
        Draw the gate cut layer
        """
        # bottom power rail
        lx_1 = 0
        ly_1 = -self.gate_cut_height / 2
        ux_1 = self.tech_data.cp_pitch * self.tech_data.col
        uy_1 = 0.5 * self.gate_cut_height
        box = Box(lx_1, ly_1, ux_1, uy_1).pyabox_obj()
        self.cell.shapes(self.gate_cut_layer_idx).insert(box)
        # upper power rail
        lx_2 = 0
        ly_2 = ly_1 + self.height
        ux_2 = ux_1
        uy_2 = ly_2 + self.gate_cut_height
        box = Box(lx_2, ly_2, ux_2, uy_2).pyabox_obj()
        self.cell.shapes(self.gate_cut_layer_idx).insert(box)
        # leftmost gate cut
        prev_lx = -self.tech_data.cp_width / SOLVER_RESCALE
        prev_ly = self.height / 2 - self.poly_height / 2
        prev_ux = prev_lx + self.tech_data.cp_width
        prev_uy = prev_ly + self.poly_height
        box = Box(prev_lx, prev_ly, prev_ux, prev_uy).pyabox_obj()
        self.cell.shapes(self.gate_cut_layer_idx).insert(box)
        # rightmost gate cut
        last_lx = self.tech_data.cp_pitch * self.tech_data.col - self.tech_data.cp_width / SOLVER_RESCALE
        last_ly = self.height / 2 - self.poly_height / 2
        last_ux = last_lx + self.tech_data.cp_width
        last_uy = last_ly + self.poly_height
        box = Box(last_lx, last_ly, last_ux, last_uy).pyabox_obj()
        self.cell.shapes(self.gate_cut_layer_idx).insert(box)

    def _lisd_merged(self, col):
        lx = self.lisd_x_offset + col / SOLVER_RESCALE
        ly = self.lisd_y_offset
        width = self.lisd_width
        height = self.lisd_signal_height * 2 + self.lisd_signal_gap
        box = Box2(lx, ly, width, height).pyabox_obj()
        self.cell.shapes(self.LISD_layer_idx).insert(box)
        self.cell.shapes(self.p_LISD_layer_idx).insert(box)  # CFET FLAG
        self.cell.shapes(self.n_LISD_layer_idx).insert(box)  # CFET FLAG
        self.cell.shapes(self.SDT_layer_idx).insert(box)
        self.cell.shapes(self.p_SDT_layer_idx).insert(box)  # CFET FLAG
        self.cell.shapes(self.n_SDT_layer_idx).insert(box)  # CFET FLAG

    def __pmos__(self, transistor):
        lx = self.active_x_offset + transistor.x / SOLVER_RESCALE
        ly = transistor.y / ((self.tech_data.m0_pitch * SOLVER_RESCALE) * 2) * self.active_height + self.active_y_offset
        width = transistor.width / SOLVER_RESCALE + self.active_x_overlap
        height = 2 * self.active_height + self.tech_data.active_gap
        box = Box2(lx, ly, width, height).pyabox_obj()
        self.cell.shapes(self.active_layer_idx).insert(box)
        self.cell.shapes(self.p_active_layer_idx).insert(box)  # CFET FLAG

    def __nmos__(self, transistor):
        lx = self.active_x_offset + transistor.x / SOLVER_RESCALE
        ly = transistor.y / (self.tech_data.m0_pitch * SOLVER_RESCALE) * self.active_height + self.active_y_offset
        width = transistor.width / SOLVER_RESCALE + self.active_x_overlap
        height = 2 * self.active_height + self.tech_data.active_gap
        box = Box2(lx, ly, width, height).pyabox_obj()
        self.cell.shapes(self.active_layer_idx).insert(box)
        self.cell.shapes(self.n_active_layer_idx).insert(box)  # CFET FLAG

    def __lisd_pmos__(self, col, transistor, is_power=False, cut_above=False, cut_below=False):
        """
        Draw the LISD (Local Interconnect Source/Drain) layer
        """
        assert not (cut_above and cut_below), f"{transistor} {col} Cannot cut both above and below the transistor"
        assert not (cut_above and is_power), f"{transistor} {col} Cannot cut above the transistor if it is a power transistor"
        lx = self.lisd_x_offset + col / SOLVER_RESCALE

        ly_offset = self.lisd_y_offset + self.tech_data.m0_pitch if cut_below else self.lisd_y_offset
        uy_offset = self.tech_data.m0_pitch if cut_above else 0

        if is_power:
            ly = ly_offset + transistor.y / ((self.tech_data.m0_pitch * SOLVER_RESCALE) * 2) * self.lisd_signal_height
            uy = self.height
        else:
            ly = ly_offset + transistor.y / ((self.tech_data.m0_pitch * SOLVER_RESCALE) * 2) * self.lisd_signal_height
            uy = self.height - self.lisd_y_offset - uy_offset

        ux = lx + self.lisd_width
        box = Box(lx, ly, ux, uy).pyabox_obj()
        self.cell.shapes(self.LISD_layer_idx).insert(box)
        self.cell.shapes(self.p_LISD_layer_idx).insert(box)  # CFET FLAG

        # Draw SDT layer
        ly_offset = self.sdt_y_offset + self.tech_data.m0_pitch if cut_below else self.sdt_y_offset
        uy_offset = self.tech_data.m0_pitch if cut_above else 0

        lx = self.lisd_x_offset + col / SOLVER_RESCALE
        ly = ly_offset + transistor.y / ((self.tech_data.m0_pitch * SOLVER_RESCALE) * 2) * self.sdt_height
        ux = lx + self.lisd_width
        uy = ly + self.sdt_height * 2 + self.sdt_gap - uy_offset
        box = Box(lx, ly, ux, uy).pyabox_obj()
        self.cell.shapes(self.SDT_layer_idx).insert(box)
        self.cell.shapes(self.p_SDT_layer_idx).insert(box)  # CFET FLAG

    def __lisd_nmos__(self, col, transistor, is_power=False):
        """
        Draw the LISD (Local Interconnect Source/Drain) layer
        """
        lx = self.lisd_x_offset + col / SOLVER_RESCALE

        if is_power:
            ly = transistor.y / ((self.tech_data.m0_pitch * SOLVER_RESCALE) * 2) * self.lisd_signal_height
            height = self.lisd_power_height + self.lisd_power_gap + self.lisd_signal_height
        else:
            ly = self.lisd_y_offset + transistor.y / ((self.tech_data.m0_pitch * SOLVER_RESCALE) * 2) * self.lisd_signal_height
            height = self.lisd_signal_height * 2 + self.lisd_signal_gap

        width = self.lisd_width
        box = Box2(lx, ly, width, height).pyabox_obj()
        self.cell.shapes(self.LISD_layer_idx).insert(box)
        self.cell.shapes(self.n_LISD_layer_idx).insert(box)  # CFET FLAG

        # Draw SDT layer
        lx = self.lisd_x_offset + col / SOLVER_RESCALE
        ly = self.sdt_y_offset + transistor.y / ((self.tech_data.m0_pitch * SOLVER_RESCALE) * 2) * self.sdt_height
        ux = lx + self.lisd_width
        uy = ly + self.sdt_height * 2 + self.sdt_gap
        box = Box(lx, ly, ux, uy).pyabox_obj()
        self.cell.shapes(self.SDT_layer_idx).insert(box)
        self.cell.shapes(self.n_SDT_layer_idx).insert(box)  # CFET FLAG

    def __gate__(self, num_gate):
        """
        Draw the gate
        """
        prev_lx = -self.tech_data.cp_width / SOLVER_RESCALE
        prev_ly = self.height / 2 - self.poly_height / 2
        prev_ux = prev_lx + self.tech_data.cp_width
        prev_uy = prev_ly + self.poly_height
        box = Box(prev_lx, prev_ly, prev_ux, prev_uy).pyabox_obj()
        self.cell.shapes(self.gate_layer_idx).insert(box)

        for g_i in range(num_gate):
            lx = prev_lx + self.tech_data.cp_pitch
            ly = prev_ly
            ux = lx + self.tech_data.cp_width
            uy = prev_uy
            box = Box(lx, ly, ux, uy).pyabox_obj()
            self.cell.shapes(self.gate_layer_idx).insert(box)
            prev_lx = lx
            prev_ly = ly
            prev_ux = ux
            prev_uy = uy

    def __fin__(self):
        """
        Draw the fin
        """
        curr_y = self.fin_y_offset
        fin_count = 0
        while True:
            lx = 0
            ly = curr_y
            ux = self.width
            uy = curr_y + self.fin_width
            box = Box(lx, ly, ux, uy).pyabox_obj()
            self.cell.shapes(self.fin_layer_idx).insert(box)
            curr_y += self.fin_pitch + self.fin_width
            fin_count += 1
            if curr_y + self.fin_width > self.height:
                break
        logger.info(f"Total fins drawn: {fin_count}, Fin pitch: {self.fin_pitch}, Fin width: {self.fin_width}")

    def __m0_bpr__(self):
        """
        Draw the M0 backside power supply rail
        """
        # bottom power rail
        lx_1 = 0
        ly_1 = -self.tech_data.m0_power_rail_thickness / 2
        ux_1 = self.tech_data.cp_pitch * self.tech_data.col
        uy_1 = 0.5 * self.tech_data.m0_power_rail_thickness
        box = Box(lx_1, ly_1, ux_1, uy_1).pyabox_obj()
        cx, cy = (lx_1 + ux_1) / 2, (ly_1 + uy_1) / 2
        self.cell.shapes(self.M0_layer_idx).insert(box)
        vdd_text = TextShape("VSS", cx, cy).pyatext_obj()
        self.cell.shapes(self.M0_text_layer_idx).insert(vdd_text)
        # upper power rail
        lx_2 = 0
        ly_2 = ly_1 + self.tech_data.m0_pitch * (self.tech_data.track + 2)
        ux_2 = ux_1
        uy_2 = ly_2 + self.tech_data.m0_power_rail_thickness
        box = Box(lx_2, ly_2, ux_2, uy_2).pyabox_obj()
        self.cell.shapes(self.M0_layer_idx).insert(box)
        cx, cy = (lx_2 + ux_2) / 2, (ly_2 + uy_2) / 2
        vss_text = TextShape("VDD", cx, cy).pyatext_obj()
        self.cell.shapes(self.M0_text_layer_idx).insert(vss_text)

    def __m0_icpd__(self):
        """
        Draw M0ICPD in-cell power rails on fine-pitch rows; no BPR is drawn.
        """
        band_height = self.tech_data.m0_pitch
        rail_thickness = min(self.tech_data.m0_power_rail_thickness, self.tech_data.m0_pitch)
        rail_width = self.tech_data.cp_pitch * self.tech_data.col

        # VSS is centered in the bottom fine row [0, M0P].
        lx_1 = 0
        ly_1 = (band_height - rail_thickness) / 2
        ux_1 = rail_width
        uy_1 = ly_1 + rail_thickness
        box = Box(lx_1, ly_1, ux_1, uy_1).pyabox_obj()
        self.cell.shapes(self.M0_layer_idx).insert(box)
        cx, cy = (lx_1 + ux_1) / 2, (ly_1 + uy_1) / 2
        vss_text = TextShape("VSS", cx, cy).pyatext_obj()
        self.cell.shapes(self.M0_text_layer_idx).insert(vss_text)

        # VDD is centered in the top fine row [height - M0P, height].
        lx_2 = 0
        ly_2 = self.height - band_height + (band_height - rail_thickness) / 2
        ux_2 = rail_width
        uy_2 = ly_2 + rail_thickness
        box = Box(lx_2, ly_2, ux_2, uy_2).pyabox_obj()
        self.cell.shapes(self.M0_layer_idx).insert(box)
        cx, cy = (lx_2 + ux_2) / 2, (ly_2 + uy_2) / 2
        vdd_text = TextShape("VDD", cx, cy).pyatext_obj()
        self.cell.shapes(self.M0_text_layer_idx).insert(vdd_text)

    def __lig_on_m0_icpd__(self):
        """
        Draw LIG strips centered on M0ICPD fine-row power bands; no CA contacts.
        """
        band_height = self.tech_data.m0_pitch
        lig_thickness = min(self.lig_height, self.tech_data.m0_pitch)
        lig_width = self.tech_data.cp_pitch * self.tech_data.col

        # Bottom LIG strip stays within the VSS fine row [0, M0P].
        lx_1 = 0
        ly_1 = (band_height - lig_thickness) / 2
        ux_1 = lig_width
        uy_1 = ly_1 + lig_thickness
        box = Box(lx_1, ly_1, ux_1, uy_1).pyabox_obj()
        self.cell.shapes(self.lig_layer_idx).insert(box)

        # Top LIG strip stays within the VDD fine row [height - M0P, height].
        lx_2 = 0
        ly_2 = self.height - band_height + (band_height - lig_thickness) / 2
        ux_2 = lig_width
        uy_2 = ly_2 + lig_thickness
        box = Box(lx_2, ly_2, ux_2, uy_2).pyabox_obj()
        self.cell.shapes(self.lig_layer_idx).insert(box)

    def __lig_on_m0_bpr__(self):
        # bottom lig on power rail
        lx_1 = 0
        ly_1 = -self.lig_height / 2
        ux_1 = self.tech_data.cp_pitch * self.tech_data.col
        uy_1 = 0.5 * self.lig_height
        box = Box(lx_1, ly_1, ux_1, uy_1).pyabox_obj()
        self.cell.shapes(self.lig_layer_idx).insert(box)
        # upper lig on power rail
        lx_2 = 0
        ly_2 = ly_1 + self.tech_data.m0_pitch * (self.tech_data.track + 2)
        ux_2 = ux_1
        uy_2 = ly_2 + self.lig_height
        box = Box(lx_2, ly_2, ux_2, uy_2).pyabox_obj()
        self.cell.shapes(self.lig_layer_idx).insert(box)

    def __ca_on_m0_bpr__(self):
        """
        Draw CA contacts on M0 backside power rail across the full width
        """
        ca_spacing = self.tech_data.cp_pitch
        num_ca_contacts = int(self.width / ca_spacing) + 1

        for i in range(num_ca_contacts):
            x_position = i * ca_spacing
            if x_position > self.width:
                break
            # Bottom CA on power rail
            lx_1 = x_position - self.ca_width / 2
            ly_1 = -self.ca_height / 2
            ux_1 = lx_1 + self.ca_width
            uy_1 = 0.5 * self.ca_height
            box = Box(lx_1, ly_1, ux_1, uy_1).pyabox_obj()
            self.cell.shapes(self.CA_layer_idx).insert(box)
            # Upper CA on power rail
            lx_2 = x_position - self.ca_width / 2
            ly_2 = ly_1 + self.tech_data.m0_pitch * (self.tech_data.track + 2)
            ux_2 = lx_2 + self.ca_width
            uy_2 = ly_2 + self.ca_height
            box = Box(lx_2, ly_2, ux_2, uy_2).pyabox_obj()
            self.cell.shapes(self.CA_layer_idx).insert(box)

    def _is_cut_pmos_above(self, col, nmos_transistor):
        if nmos_transistor.source_col == col:
            if nmos_transistor.source_net.startswith("VSS"):
                return False
            return nmos_transistor.source_row == self.height
        elif nmos_transistor.drain_col == col:
            if nmos_transistor.drain_net.startswith("VSS"):
                return False
            return nmos_transistor.drain_row == self.height
        return False

    def _is_cut_pmos_below(self, col, nmos_transistor):
        if nmos_transistor.source_col == col:
            if nmos_transistor.source_net.startswith("VSS"):
                return False
            return nmos_transistor.source_row == 0
        elif nmos_transistor.drain_col == col:
            if nmos_transistor.drain_net.startswith("VSS"):
                return False
            return nmos_transistor.drain_row == 0
        return False

    def _is_left_lisd_merged(self, tran_1, tran_2):
        """
        Check if the left LISD of two transistors are merged.
        """
        if tran_1.flip:
            tran_1_left_net = tran_1.drain_net
        else:
            tran_1_left_net = tran_1.source_net
        if tran_2.flip:
            tran_2_left_net = tran_2.drain_net
        else:
            tran_2_left_net = tran_2.source_net
        if tran_1_left_net == tran_2_left_net:
            return True
        return False

    def _is_right_lisd_merged(self, tran_1, tran_2):
        """
        Check if the right LISD of two transistors are merged.
        """
        if tran_1.flip:
            tran_1_right_net = tran_1.source_net
        else:
            tran_1_right_net = tran_1.drain_net
        if tran_2.flip:
            tran_2_right_net = tran_2.source_net
        else:
            tran_2_right_net = tran_2.drain_net
        if tran_1_right_net == tran_2_right_net:
            return True
        return False

    def _is_lisd_power_at_col(self, transistor, col):
        """
        Check if the LISD is a power rail at the given column.
        """
        if transistor.source_col == col:
            if transistor.source_net.startswith("VDD") or transistor.source_net.startswith("VSS"):
                return True
        if transistor.drain_col == col:
            if transistor.drain_net.startswith("VDD") or transistor.drain_net.startswith("VSS"):
                return True
        if transistor.source_col != col:
            if transistor.drain_net == "VDD" and transistor.drain_col == -1:
                return True
            if transistor.drain_net == "VSS" and transistor.drain_col == -1:
                return True
        if transistor.drain_col != col:
            if transistor.source_net == "VDD" and transistor.source_col == -1:
                return True
            if transistor.source_net == "VSS" and transistor.source_col == -1:
                return True
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--result_file",
        type=str,
        help="Path to the result file.",
    )
    parser.add_argument(
        "--subckt_name",
        type=str,
        help="Subcircuit name to extract.",
    )
    parser.add_argument(
        "--gds_file",
        type=str,
        help="Path to save the GDS file.",
    )
    parser.add_argument(
        "--layer",
        type=str,
        default=None,
        help="Path to the layer-stack JSON (enclosure overhangs).",
    )
    args = parser.parse_args()

    gds_path = args.gds_file
    gds_dir = os.path.dirname(gds_path)
    if gds_dir:
        os.makedirs(gds_dir, exist_ok=True)

    # Constructor handles both create and append modes, then saves
    CFETLayout(
        result_file=args.result_file,
        subckt_name=args.subckt_name,
        gds_file=gds_path,
        layer_file=args.layer,
    )
