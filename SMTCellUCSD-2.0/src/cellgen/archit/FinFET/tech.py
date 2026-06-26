from src.cellgen.core.entity import LayerStack
from src.cellgen.core.graph import LAYER_KIND_PLACE, LAYER_KIND_ROUTE


class FinFET_Tech:
    """
    FinFET technology class - holds layer information for GDS/LEF generation.

    FinFET is a single-tier planar architecture: PMOS and NMOS share the single
    PC placement layer (no stacked tiers, no z dimension). Column / parity /
    pitch queries use the single placement layer via `default_placement_layer`.

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
        diffusion_break_type: str = "SDB",
        allow_diffusion_height_mixing: bool = True,
        allow_lisd_merging: bool = True,
        allowable_min_gate_cut_cpp: int = 2,
        enforce_diffusion_alignment: bool = True,
        allowable_diffusion_break_cols="ALL",
        placement_layer_names: frozenset = frozenset({"PC"}),
        pin_access_layer_names: frozenset = frozenset({"M0"}),
        default_placement_layer: str = "PC",
    ):
        self.lib_name = lib_name
        self.TECHNOLOGY = "FinFET"
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

        # placement-tier config - FinFET is single-tier (one placement layer).
        if default_placement_layer not in placement_layer_names:
            raise ValueError(
                f"default_placement_layer {default_placement_layer!r} must be in "
                f"placement_layer_names {sorted(placement_layer_names)}"
            )
        self.placement_layer_names = placement_layer_names
        self.default_placement_layer = default_placement_layer
        self.pin_access_layer_names = pin_access_layer_names

        self.enable_reverse_flow_link = False

        # validated num_sites (single assignment)
        self.num_sites = self._validate_height_config(height_config, num_rt_track, num_sites)

        # ------------------------------------------------------------------ #
        # TODO: parameterize the magic constants below.                      #
        # Needed by downstream GDS / LEF generation (not by the CP-SAT model).#
        # ------------------------------------------------------------------ #
        self.power_config = "M0BPR"
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
    # validation                                                             #
    # ---------------------------------------------------------------------- #

    def _validate_height_config(self, height_config, num_rt_track, num_sites) -> int:
        """
        Validate the height configuration and return the (possibly adjusted) num_sites.

        FinFET accepts SH / PNNP / NPPN as nominal height configurations, but
        only SH (single-height) is currently implemented; PNNP / NPPN raise
        NotImplementedError.
        """
        if height_config not in ("SH", "PNNP", "NPPN"):
            raise ValueError(
                f"Unknown height configuration {height_config!r} for FinFET. "
                f"Expected one of 'SH', 'PNNP', 'NPPN'."
            )
        if height_config != "SH":
            raise NotImplementedError(
                f"Height configuration {height_config!r} not implemented for FinFET. "
                f"Only 'SH' is currently supported."
            )
        assert num_rt_track in (3, 4), (
            f"FinFET SH supports 3 or 4 routing tracks (got {num_rt_track})"
        )
        assert num_sites == 1, (
            f"SH must have 1 site per standard cell (got {num_sites})"
        )
        return num_sites
