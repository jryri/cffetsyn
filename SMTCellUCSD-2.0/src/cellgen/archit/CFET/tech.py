"""
CFET technology configuration.

CFET is a 2-tier vertically-stacked transistor architecture: one device sits
directly above the other in the same column footprint. The top device lives on
the PC (Poly Contact) layer and the bottom device on the BPC (Bottom Poly
Contact) layer. Which device is PMOS vs NMOS is set by `stacking_config`:

    'P_on_N' : PMOS on top (PC),    NMOS on bottom (BPC)
    'N_on_P' : NMOS on top (PC),    PMOS on bottom (BPC)

The CFET-specific 2-tier device model:
  - stacking_config + _validate_stacking_config
  - get_pmos_layer() / get_nmos_layer()  - resolve PC/BPC per stacking_config
  - get_top_placement_layer() / get_bottom_placement_layer() / get_placement_layers()

NOTE: layer-wise design rules live in the layer stack, not in this class.
"""

from src.cellgen.core.entity import LayerStack
from src.cellgen.core.graph import LAYER_KIND_PLACE, LAYER_KIND_ROUTE


class CFET_Tech:
    """
    CFET technology class - holds layer information for GDS/LEF generation.

    CFET is a 2-tier stacked transistor architecture. The two placement tiers
    (PC = top, BPC = bottom) share the same column grid by design, so column /
    parity / pitch queries can use the canonical `default_placement_layer` (PC).

    NOTE: layer-wise design rules live in the layer stack, not in this class.
    """

    def __init__(
        self,
        lib_name,
        num_fin,
        num_rt_track,
        unit_width,
        layer_stack: LayerStack,
        height_config: str = "SH",
        num_sites: int = 1,
        stacking_config: str = "P_on_N",
        diffusion_break_type: str = "SDB",
        allow_diffusion_height_mixing: bool = True,
        allow_lisd_merging: bool = True,
        allowable_min_gate_cut_cpp: int = 2,
        enforce_diffusion_alignment: bool = True,
        allowable_diffusion_break_cols="ALL",
        placement_layer_names: frozenset = frozenset({"PC", "BPC"}),
        pin_access_layer_names: frozenset = frozenset({"BPC", "M0"}),
        default_placement_layer: str = "PC",
    ):
        self.lib_name = lib_name
        self.TECHNOLOGY = "CFET"
        self.num_fin = num_fin
        self.num_rt_track = num_rt_track
        self.layer_stack = layer_stack
        self.unit_width = unit_width

        self.height_config = height_config
        self.diffusion_break_type = diffusion_break_type
        self.allow_diffusion_height_mixing = allow_diffusion_height_mixing
        self.allow_lisd_merging = allow_lisd_merging
        self.allowable_min_gate_cut_cpp = allowable_min_gate_cut_cpp
        self.enforce_diffusion_alignment = enforce_diffusion_alignment
        self.allowable_diffusion_break_cols = allowable_diffusion_break_cols

        # CFET 2-tier stacking config (P_on_N / N_on_P)
        self.stacking_config = self._validate_stacking_config(stacking_config)

        # placement-tier config
        if default_placement_layer not in placement_layer_names:
            raise ValueError(
                f"default_placement_layer {default_placement_layer!r} must be in "
                f"placement_layer_names {sorted(placement_layer_names)}"
            )
        self.placement_layer_names = placement_layer_names
        self.default_placement_layer = default_placement_layer
        self.pin_access_layer_names = pin_access_layer_names

        # validated num_sites (single assignment)
        self.num_sites = self._validate_height_config(height_config, num_rt_track, num_sites)

        # ------------------------------------------------------------------ #
        # TODO: parameterize the magic constants below.                      #
        # Needed by downstream GDS / LEF generation (not by the CP-SAT model).#
        # ------------------------------------------------------------------ #
        if self.num_rt_track == 3:
            self.power_config = "M0ICPD"
        elif self.num_rt_track == 4:
            self.power_config = "M0BPR"
        else:
            raise NotImplementedError(
                f"CFET SH supports 3 or 4 routing tracks (got {self.num_rt_track})"
            )
        self.wall_thickness = 0.019
        self.power_rail_thickness = 0.036
        self.m0_power_rail_thickness = 0.036
        self.m0_pitch = 0.024
        self.gate_width = 0.016
        self.gate_height = 0.154
        self.gate_pitch = 0.045
        self.active_height = 0.046
        self.active_width = 0.059
        self.active_overlap = 0.014
        self.active_gap = 0.014

    # ---------------------------------------------------------------------- #
    # layer queries                                                          #
    # ---------------------------------------------------------------------- #

    def get_pitch(self, layer_name):
        """Return the pitch of the named layer."""
        return self._layer_attr(layer_name, "pitch")

    def get_offset(self, layer_name):
        """Return the offset of the named layer."""
        return self._layer_attr(layer_name, "offset")

    def get_width(self, layer_name):
        """Return the width of the named layer."""
        return self._layer_attr(layer_name, "width")

    def _layer_attr(self, layer_name, attr):
        for layer in self.layer_stack.metal_layers:
            if layer.layer_name == layer_name:
                return getattr(layer, attr)
        raise ValueError(f"Layer {layer_name!r} not found in the technology stack.")

    def is_placement_layer(self, layer) -> bool:
        """
        Return True if `layer` (a layer-stack layer object) is a placement-tier layer.

        Uses a name-set lookup against self.placement_layer_names.
        """
        return layer.layer_name in self.placement_layer_names

    @property
    def layer_to_kind(self) -> dict:
        """
        Map every layer in the stack to LAYER_KIND_PLACE / LAYER_KIND_ROUTE.

        Feeds LayeredGridGraph(layer_to_kind=...) - see src.cellgen.core.graph.
        """
        return {
            layer.layer_name: (
                LAYER_KIND_PLACE if self.is_placement_layer(layer) else LAYER_KIND_ROUTE
            )
            for layer in self.layer_stack.metal_layers
        }

    # ---------------------------------------------------------------------- #
    # CFET 2-tier device-layer resolution                                    #
    # ---------------------------------------------------------------------- #

    def get_top_placement_layer(self) -> str:
        """Return the top-tier placement layer name (PC)."""
        return "PC"

    def get_bottom_placement_layer(self) -> str:
        """Return the bottom-tier placement layer name (BPC)."""
        return "BPC"

    def get_placement_layers(self) -> list:
        """Return the placement-tier layer names, top first (PC, BPC)."""
        return ["PC", "BPC"]

    def get_pmos_layer(self) -> str:
        """
        Return the placement layer carrying the PMOS device, per stacking_config.

        'P_on_N' -> PMOS on top    (PC)
        'N_on_P' -> PMOS on bottom (BPC)
        """
        if self.stacking_config == "P_on_N":
            return self.get_top_placement_layer()
        return self.get_bottom_placement_layer()

    def get_nmos_layer(self) -> str:
        """
        Return the placement layer carrying the NMOS device, per stacking_config.

        'P_on_N' -> NMOS on bottom (BPC)
        'N_on_P' -> NMOS on top    (PC)
        """
        if self.stacking_config == "P_on_N":
            return self.get_bottom_placement_layer()
        return self.get_top_placement_layer()

    # ---------------------------------------------------------------------- #
    # stack / routing queries (shared CFET + CFFET)                          #
    # ---------------------------------------------------------------------- #

    def get_front_route_metal(self) -> str:
        """Primary horizontal routing metal on the front face (legacy JSON key M0)."""
        return "M0"

    def get_back_route_metals(self) -> list[str]:
        """Back-face horizontal routing metals. Empty for single-face CFET."""
        return []

    def get_route_metals_for_power_row_ban(self) -> list[str]:
        """Layers subject to M0ICPD top-view power-row signal ban."""
        layers = [self.get_front_route_metal()]
        layers.extend(self.get_placement_layers())
        return layers

    def get_virtual_connect_pairs(self) -> list[tuple[str, str]]:
        """Virtual overlap/boundary connect pairs (bottom tier → front route metal)."""
        return [
            (self.get_bottom_placement_layer(), self.get_front_route_metal()),
        ]

    def get_intra_block_miv_pair(self) -> tuple[str, str]:
        """Bottom/top placement layers connected by intra-block MIV."""
        return (self.get_bottom_placement_layer(), self.get_top_placement_layer())

    def get_stitch_via_name(self) -> str | None:
        """Inter-block stitch via layer name, or None for single-block CFET."""
        return None

    def get_canvas_height(self) -> float:
        """Cell canvas height derived from track count and front M0 pitch."""
        return self.num_rt_track * self.get_pitch(self.get_front_route_metal()) * 2

    def get_m0icpd_fine_route_metals(self) -> list[str]:
        """Horizontal metals using fine M0 pitch under M0ICPD (not doubled)."""
        return [self.get_front_route_metal()]

    def get_domain_placement_layer(self) -> str:
        """Canonical placement layer for col/row domain (default_placement_layer)."""
        return self.default_placement_layer

    # ---------------------------------------------------------------------- #
    # validation                                                             #
    # ---------------------------------------------------------------------- #

    def _validate_stacking_config(self, stacking_config) -> str:
        """
        Validate the 2-tier stacking configuration.

        CFET stacks one device directly above the other; `stacking_config`
        decides which device is on top:
            'P_on_N' : PMOS on top (PC), NMOS on bottom (BPC)
            'N_on_P' : NMOS on top (PC), PMOS on bottom (BPC)
        """
        if stacking_config not in ("P_on_N", "N_on_P"):
            raise ValueError(
                f"stacking_config {stacking_config!r} not supported for CFET. "
                f"Expected one of 'P_on_N', 'N_on_P'."
            )
        return stacking_config

    def _validate_height_config(self, height_config, num_rt_track, num_sites) -> int:
        """
        Validate the height configuration and return the (possibly adjusted) num_sites.

        CFET supports SH (single-height) only.
        """
        if height_config != "SH":
            raise NotImplementedError(
                f"Height configuration {height_config!r} not implemented for CFET. "
                f"Only 'SH' is currently supported."
            )
        if num_rt_track not in (3, 4):
            raise NotImplementedError(
                f"CFET SH supports 3 or 4 routing tracks (got {num_rt_track})"
            )
        assert num_sites == 1, (
            f"SH must have 1 site per standard cell (got {num_sites})"
        )
        return num_sites
