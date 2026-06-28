"""
CFFET technology configuration — dual-face Flip-FET extending CFET.

Convention A tier names (see docs/skills/cffet-layer-nomenclature/SKILL.md):
  Back block:  BBOTPC, BTOPPC + BM0
  Front block: FBOTPC, FTOPPC + FM0 (legacy JSON key M0)
  Stitch: STV (sole inter-block via)
  Split gate: MDI at center seam (BTOPPC/FBOTPC, co-located with STV)
"""

from src.cellgen.archit.CFET.tech import CFET_Tech

# Bottom → top placement tier order (z-index 0..3).
CFFET_TIER_ORDER = ("BBOTPC", "BTOPPC", "FBOTPC", "FTOPPC")

# Intra-block MIV pairs (bottom tier, top tier) per face.
CFFET_MIV_PAIRS = (
    ("BBOTPC", "BTOPPC"),
    ("FBOTPC", "FTOPPC"),
)

# Legacy JSON layer_name aliases → Convention A (for docs crosswalk).
LEGACY_LAYER_ALIASES = {
    "BPC": "FBOTPC",
    "PC": "FTOPPC",
    "M0": "FM0",
}


class CFFET_Tech(CFET_Tech):
    """CFFET: two stacked CFET blocks with dual M0ICPD rails."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("placement_layer_names", frozenset(CFFET_TIER_ORDER))
        kwargs.setdefault("pin_access_layer_names", frozenset({"BM0", "M0"}))
        kwargs.setdefault("default_placement_layer", "FTOPPC")
        super().__init__(*args, **kwargs)
        self.TECHNOLOGY = "CFFET"
        # CFFET always uses M0ICPD on FM0+BM0 (0.5T VDD + signal + 0.5T VSS),
        # including TRACK=4 — unlike planar CFET which switches to M0BPR at 4T.
        if self.num_rt_track in (3, 4):
            self.power_config = "M0ICPD"

    def get_top_placement_layer(self) -> str:
        return "FTOPPC"

    def get_bottom_placement_layer(self) -> str:
        return "BBOTPC"

    # ------------------------------------------------------------------ #
    # Dual-face device tiers (P3b). PMOS/NMOS each may sit on EITHER     #
    # block's legal tier via ``z_var`` (see CFFET._cffet_model_tier_     #
    # restriction). Canonical domain grid still uses front FTOPPC.       #
    # ------------------------------------------------------------------ #
    def get_pmos_layer(self) -> str:
        if self.stacking_config == "P_on_N":
            return "FTOPPC"
        return "FBOTPC"

    def get_nmos_layer(self) -> str:
        if self.stacking_config == "P_on_N":
            return "FBOTPC"
        return "FTOPPC"

    def get_front_bottom_placement_layer(self) -> str:
        """Front-block bottom tier (the v1 'BPC' analogue for domain math)."""
        return "FBOTPC"

    def get_placement_layers(self) -> list:
        return list(CFFET_TIER_ORDER)

    def get_back_route_metals(self) -> list[str]:
        return ["BM0"]

    def get_route_metals_for_power_row_ban(self) -> list[str]:
        layers = [self.get_front_route_metal(), "BM0"]
        layers.extend(self.get_placement_layers())
        return layers

    def get_virtual_connect_pairs(self) -> list[tuple[str, str]]:
        return [
            ("FBOTPC", self.get_front_route_metal()),
            ("BBOTPC", "BM0"),
        ]

    def get_intra_block_miv_pairs(self) -> list[tuple[str, str]]:
        return list(CFFET_MIV_PAIRS)

    def get_intra_block_miv_pair(self) -> tuple[str, str]:
        """CFET API compatibility — front block pair."""
        return ("FBOTPC", "FTOPPC")

    def get_stitch_via_name(self) -> str:
        return "STV"

    def get_mdi_name(self) -> str:
        """Middle Dielectric Isolation marker at the CFFET center seam."""
        return "MDI"

    def get_m0icpd_fine_route_metals(self) -> list[str]:
        return [self.get_front_route_metal(), "BM0"]

    def get_bpc_tiers(self) -> tuple[str, ...]:
        if self.stacking_config == "P_on_N":
            return ("BBOTPC", "FBOTPC")
        return ("BTOPPC", "FTOPPC")

    def get_pc_tiers(self) -> tuple[str, ...]:
        if self.stacking_config == "P_on_N":
            return ("BTOPPC", "FTOPPC")
        return ("BBOTPC", "FBOTPC")

    def _validate_height_config(self, height_config, num_rt_track, num_sites) -> int:
        if height_config != "SH":
            raise NotImplementedError(f"CFFET supports SH only (got {height_config!r})")
        if num_rt_track not in (3, 4):
            raise NotImplementedError(
                f"CFFET supports TRACK=3 or 4 M0ICPD (got {num_rt_track})"
            )
        assert num_sites == 1
        return num_sites
