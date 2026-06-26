"""
QFET technology configuration.

Holds the layer information used by GDS/LEF generation and the placement-tier
config consumed by the QFET orchestrator:
  - placement_layer_names    : set of placement-tier layer names
  - default_placement_layer  : canonical pick for col/parity/pitch queries
  - is_placement_layer(...)  : single-source-of-truth predicate
  - layer_to_kind            : PLACE/ROUTE classification per layer, feeding
                               LayeredGridGraph(layer_to_kind=...)

QFET is SH (single-height) only.
"""

from src.cellgen.core.entity import LayerStack
from src.cellgen.core.graph import LAYER_KIND_PLACE, LAYER_KIND_ROUTE


class QFET_Tech:
    """
    QFET technology class - holds layer information for GDS/LEF generation.

    QFET is a stacked transistor architecture whose placement tiers share the
    same column grid by design, so column / parity / pitch queries can use any
    one of them via `default_placement_layer`.

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
        placement_layer_names: frozenset = frozenset({"BPC1", "PC1"}),
        pin_access_layer_names: frozenset = frozenset({"BM0", "M0"}),
        default_placement_layer: str = "PC1",
    ):
        self.lib_name = lib_name
        self.TECHNOLOGY = "QFET"
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

        # placement-tier config
        if default_placement_layer not in placement_layer_names:
            raise ValueError(
                f"default_placement_layer {default_placement_layer!r} must be in "
                f"placement_layer_names {sorted(placement_layer_names)}"
            )
        self.placement_layer_names = placement_layer_names
        self.default_placement_layer = default_placement_layer
        self.pin_access_layer_names = pin_access_layer_names

        # validated num_sites
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

        Implemented as a name-set lookup against self.placement_layer_names.
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

        QFET supports SH (single-height) only.
        """
        if height_config != "SH":
            raise NotImplementedError(
                f"Height configuration {height_config!r} not implemented for QFET. "
                f"Only 'SH' is currently supported."
            )
        assert num_rt_track in (2, 3, 4), (
            f"SH supports 2, 3, or 4 routing tracks (got {num_rt_track})"
        )
        assert num_sites == 1, (
            f"SH must have 1 site per standard cell (got {num_sites})"
        )
        return num_sites
