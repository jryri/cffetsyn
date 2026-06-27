import logging
from loguru import logger
import json
import networkx as nx
from typing import Union, List
from enum import Enum
from dataclasses import dataclass, field
import copy
# custom
from src.cellgen.archit import config

class Model(Enum):
    PMOS = "pmos"
    NMOS = "nmos"


class PinType(Enum):
    SOURCE = "source"
    DRAIN = "drain"
    GATE = "gate"
    BULK = "bulk"


# ---------------------------------------------------------------------------
# Placement region model for ?FET (tier/site aware)
# ---------------------------------------------------------------------------
@dataclass
class PlacementRegion:
    """One region where a MOS device can be placed.

    Attributes:
        tier: Physical tier index (0 = front-side, 1 = back-side, ...).
        site: Site index within that tier.
        placeable_rows: M0 row indices where the transistor body can sit.
        pin_access_rows: M0 row indices where pins of this device can be accessed.
    """
    tier: int
    site: int
    placeable_rows: List[int] = field(default_factory=list)
    pin_access_rows: List[int] = field(default_factory=list)

    def __repr__(self) -> str:
        return (f"PlacementRegion(tier={self.tier}, site={self.site}, "
                f"place={self.placeable_rows}, pin={self.pin_access_rows})")


@dataclass
class DevicePlacement:
    """All placement regions for one device type (NMOS or PMOS).

    Provides convenience queries so consumers never need to manually
    iterate the tier/site nesting.
    """
    regions: List[PlacementRegion] = field(default_factory=list)

    # -- convenience queries ------------------------------------------------
    def all_placeable_rows(self) -> List[int]:
        """Sorted, deduplicated list of every placeable row across all regions."""
        return sorted({r for reg in self.regions for r in reg.placeable_rows})

    def all_pin_access_rows(self) -> List[int]:
        """Sorted, deduplicated list of every pin-access row across all regions."""
        return sorted({r for reg in self.regions for r in reg.pin_access_rows})

    def tiers(self) -> List[int]:
        """Sorted list of unique tier indices."""
        return sorted({reg.tier for reg in self.regions})

    def sites_in_tier(self, tier: int) -> List[int]:
        """Sorted list of unique site indices within a tier."""
        return sorted({reg.site for reg in self.regions if reg.tier == tier})

    def rows_in_tier(self, tier: int) -> List[int]:
        """Sorted, deduplicated placeable rows for a given tier."""
        return sorted({r for reg in self.regions if reg.tier == tier
                       for r in reg.placeable_rows})

    def rows_in_site(self, tier: int, site: int) -> List[int]:
        """Placeable rows for a specific (tier, site) pair."""
        for reg in self.regions:
            if reg.tier == tier and reg.site == site:
                return list(reg.placeable_rows)
        return []

    def pin_rows_in_tier(self, tier: int) -> List[int]:
        """Sorted, deduplicated pin-access rows for a given tier."""
        return sorted({r for reg in self.regions if reg.tier == tier
                       for r in reg.pin_access_rows})

    def region(self, tier: int, site: int) -> "PlacementRegion | None":
        """Look up a specific region by (tier, site), or None."""
        for reg in self.regions:
            if reg.tier == tier and reg.site == site:
                return reg
        return None

    def __repr__(self) -> str:
        return f"DevicePlacement({self.regions})"


# ---------------------------------------------------------------------------
# Per-layer CP-SAT domain bundle
# ---------------------------------------------------------------------------
@dataclass
class LayerDomain:
    """Pre-computed indices and CP-SAT domains for one poly-contact layer.

    Built from a LayeredGridGraph so that constraint code can simply do::

        self.pc.domain_col      # CP-SAT domain over all PC column coordinates
        self.bpc.sd_ci          # source/drain column indices on BPC

    Attributes (indices):
        mos_placeable_ci: Odd col indices excluding last (placement anchor columns).
        sd_ci:            Odd col indices (source/drain columns).
        gate_ci:          Even col indices (gate columns).
        all_ci:           All col indices.
        all_cols:         All col coordinates.
        mos_placeable_ri: Even row indices (placement rows).
        all_ri:           All row indices.
        all_rows:         All row coordinates.

    Attributes (CP-SAT domains):
        domain_mos_ci:    Domain over *mos_placeable_ci*.
        domain_mos_ri:    Domain over *mos_placeable_ri*.
        domain_sd_ci:     Domain over *sd_ci*.
        domain_gate_ci:   Domain over *gate_ci*.
        domain_col:       Domain over *all_cols* (coordinates).
        domain_row:       Domain over *all_rows* (coordinates).
    """
    layer: str

    # Column indices / coordinates
    mos_placeable_ci: List[int] = field(default_factory=list)
    sd_ci: List[int]            = field(default_factory=list)
    gate_ci: List[int]          = field(default_factory=list)
    all_ci: List[int]           = field(default_factory=list)
    all_cols: List[int]         = field(default_factory=list)

    # Row indices / coordinates
    mos_placeable_ri: List[int] = field(default_factory=list)
    all_ri: List[int]           = field(default_factory=list)
    all_rows: List[int]         = field(default_factory=list)

    # CP-SAT domains (initialised to None; populated by builder)
    domain_mos_ci: object  = None
    domain_mos_ri: object  = None
    domain_sd_ci: object   = None
    domain_gate_ci: object = None
    domain_col: object     = None
    domain_row: object     = None

    def __repr__(self) -> str:
        return (f"LayerDomain({self.layer!r}, "
                f"cols={len(self.all_cols)}, rows={len(self.all_rows)})")


# ---------------------------------------------------------------------------
# Pairwise same-site BoolVar mapping (frozenset-keyed to avoid duplicates)
# ---------------------------------------------------------------------------
class SameSiteVars:
    """Symmetric mapping of transistor-pair same-site BoolVars.

    Uses ``frozenset`` keys so that ``vars[a, b]`` and ``vars[b, a]``
    resolve to the same entry - no more storing both directions manually.

    Usage::

        ssv = SameSiteVars()
        ssv[t1, t2] = some_bool_var
        ssv[t2, t1]  # returns the same var
        (t1, t2) in ssv  # True
    """

    def __init__(self):
        self._vars: dict = {}  # frozenset({t1, t2}) -> BoolVar

    def __contains__(self, pair) -> bool:
        return frozenset(pair) in self._vars

    def __getitem__(self, pair):
        return self._vars[frozenset(pair)]

    def __setitem__(self, pair, var):
        self._vars[frozenset(pair)] = var

    def __len__(self) -> int:
        return len(self._vars)

    def __iter__(self):
        return iter(self._vars)

    def items(self):
        return self._vars.items()


# Define a Transistor class
class Transistor:
    """Transistor class to represent a transistor in a circuit."""

    # nfet l=0.002u m=1  nfin=1 par=1 p_la=0 nf=1 ngcon=1 plorient=0 cpp=4.2e-08 pre_layout_local=0 ptwell=0 wns=1.6e-08
    def __init__(
        self,
        name,
        source,
        gate,
        drain,
        bulk,
        model,
        w,
        l,
        nfin,
        m=None,
        par=None,
        p_la=None,
        nf=None,
        ngcon=None,
        plorient=None,
        cpp=None,
        pre_layout_local=None,
        ptwell=None,
        wns=None,
    ):
        self.name = name
        # Dictionary mapping terminal names to net names
        self.terminals = {"source": source, "gate": gate, "drain": drain, "bulk": bulk}
        self.source = source
        self.gate = gate
        self.drain = drain
        self.bulk = bulk
        assert "p" in model or "n" in model, logger.error("Model must be contain 'p' or 'n'.")
        self.model = Model.PMOS if "p" in model else Model.NMOS
        self.model_name = model  # original CDL model string
        self.w = w
        self.l = l
        self.nfin = nfin
        self.m = m if m is not None else None
        self.par = par if par is not None else None
        self.p_la = p_la if p_la is not None else None
        self.nf = nf if nf is not None else None
        self.ngcon = ngcon if ngcon is not None else None
        self.plorient = plorient if plorient is not None else None
        self.cpp = cpp if cpp is not None else None
        self.pre_layout_local = pre_layout_local if pre_layout_local is not None else None
        self.ptwell = ptwell if ptwell is not None else None
        self.wns = wns if wns is not None else None

    def get_width(self):
        # return the numerical value of the width
        return float(self.w[:-1])

    def __repr__(self):
        # return f"Transistor({self.name}, {self.terminals}, {self.model}, {self.w}, {self.l}, {self.nfin})"
        return f"Transistor({self.name}, {self.terminals}, {self.model}, w={self.w}, l={self.l}, nfin={self.nfin} m={self.m} par={self.par} p_la={self.p_la} nf={self.nf} ngcon={self.ngcon} plorient={self.plorient} cpp={self.cpp} pre_layout_local={self.pre_layout_local} ptwell={self.ptwell} wns={self.wns}"

    def __eq__(self, other):
        if isinstance(other, Transistor):
            return self.name == other.name
        return False

    # for sorting
    def __lt__(self, other):
        if isinstance(other, Transistor):
            return self.name < other.name
        return NotImplemented


class Net:
    """Net class to represent a net in a circuit."""

    def __init__(self, name, type_=None):
        self.name = name

        if type_ is not None:
            self.type = type_
        else:
            self.type = "internal"
        # List of tuples: (transistor instance, terminal pin_type)
        self.connected_transistors = []

    def is_io_net(self):
        """Check if the net is an input or output net."""
        return self.type == "io"

    def is_power_net(self):
        """Check if the net is a power net."""
        return self.type == "power"

    def is_ground_net(self):
        """Check if the net is a ground net."""
        return self.type == "ground"

    def is_power_or_ground_net(self):
        """Check if the net is a power or ground net."""
        return self.type in ["power", "ground"]
    
    def degree(self):
        """Net Degree"""
        return len(self.connected_transistors)

    def add_connection(self, transistor_name, pin_type):
        if pin_type == "bulk":
            # Bulk is not a terminal pin type
            return
        self.connected_transistors.append((transistor_name, pin_type))

    def get_source_transistor(self):
        """Assuming the first transistor is the source transistor."""
        return self.connected_transistors[0][0]

    def get_source_pin_type(self):
        """Assuming the first transistor is the source transistor."""
        return self.connected_transistors[0][1]

    def source(self):
        """Get the source transistor of the net."""
        return self.connected_transistors[0]

    def get_terminals(self):
        """Get the terminals of the net."""
        return [t for t in self.connected_transistors[1:]]

    def num_terminals(self):
        """Get the number of terminals of the net."""
        return len(self.connected_transistors) - 1

    def terminals(self):
        """Get the terminals of the net."""
        return [t for t in self.connected_transistors[1:]]

    def is_a_terminal_tran(self, transistor):
        """Check if the given transistor is a terminal transistor."""
        for i, (t, __) in enumerate(self.connected_transistors):
            if i == 0:  # Skip the source transistor
                continue
            # if the transistor is given as a string
            if type(transistor) == str:
                if t == transistor:
                    return True
            # if the transistor is given as a Transistor object
            elif type(transistor) == Transistor:
                if t == transistor.name:
                    return True
        return False

    def is_a_source_tran(self, transistor):
        """Check if the given transistor is a source transistor."""
        # if the transistor is given as a string
        if type(transistor) == str:
            if self.connected_transistors[0][0] == transistor:
                return True
        # if the transistor is given as a Transistor object
        elif type(transistor) == Transistor:
            if self.connected_transistors[0][0] == transistor.name:
                return True
        return False

    def __repr__(self):
        return f"Net({self.name})"


# Define a Circuit class to hold nets and transistors
class Circuit:
    """Circuit class to represent a circuit."""

    def __init__(self):
        # Dictionary of net_name -> Net instance
        self.nets = {}
        # Dictionary of transistor name -> Transistor instance
        self.transistors = {}
        # Optionally, store subckt info
        self.subckt_name = None
        # List of pins
        self.pins = []  # All pins
        self.io_pins = []  # Input and output pins
        self.pwr_pins = []  # Power pins
        self.gnd_pins = []  # Ground pins

    def num_transistors(self):
        return len(self.transistors)
    
    def num_pmos_transistors(self):
        pmos_transistors = [t for t in self.transistors.values() if t.model == Model.PMOS]
        return len(pmos_transistors)
    
    def num_nmos_transistors(self):
        nmos_transistors = [t for t in self.transistors.values() if t.model == Model.NMOS]
        return len(nmos_transistors)

    # MH FLAG
    def get_minimum_col(self, num_db=0, num_sites=1, num_placement_layers=1):
        """Get the minimum number of cols based on the number of transistors.

        Two ways to split a transistor row's load:
          - `num_sites`  : multi-height cells (?FET) - one device row, several
            site columns. e.g. num_sites=3 means 3 sites share a row.
          - `num_placement_layers` : 3D stacks (QFET) - multiple gate-poly
            tiers in Z. Each tier hosts its own (P, N) device rows, so the
            per-row trans count drops by the tier count.

        Both divide the bottleneck the same way mathematically, so they're
        composed multiplicatively into a single divisor. Keep them as
        separate kwargs so callers express intent clearly (num_sites is
        about width-axis cell stacking; num_placement_layers is about
        Z-axis tier stacking).

        Returns:
            int: minimum number of SDG columns (always odd: starts/ends
            on a gate column).
        """
        num_pmos = sum(1 for t in self.transistors.values() if t.model == Model.PMOS)
        num_nmos = sum(1 for t in self.transistors.values() if t.model == Model.NMOS)
        bottleneck = max(num_pmos, num_nmos)
        divisor = max(1, num_sites * num_placement_layers)
        # ceil division
        per_row = -(-bottleneck // divisor)
        # NOTE: always multiply by 2 as we start and end on a gate column
        return 1 + per_row * 2 + num_db * 2

    def assign_pins(self, pins):
        self.pins = pins
        self.pwr_pins = [pin for pin in pins if pin in config.PWR_NET_NAMES]
        self.gnd_pins = [pin for pin in pins if pin in config.GND_NET_NAMES]
        self.io_pins = [
            pin for pin in pins if pin not in config.PWR_NET_NAMES + config.GND_NET_NAMES
        ]
        assert len(self.pins) == len(self.io_pins) + len(self.pwr_pins) + len(
            self.gnd_pins
        ), logger.error(
            f"Error in assigning pins. Total pins: {self.pins}, IO pins: {self.io_pins}, Power pins: {self.pwr_pins}, Ground pins: {self.gnd_pins}"
        )
        if len(self.pins) == 0:
            logger.warning(f"No pins found in the circuit {self.subckt_name}.")
        if len(self.pwr_pins) == 0:
            logger.warning(f"No power pins found in the circuit {self.subckt_name}.")
        if len(self.gnd_pins) == 0:
            logger.warning(f"No ground pins found in the circuit {self.subckt_name}.")

    def add_net(self, net_name):
        if net_name not in self.nets:
            if net_name in self.io_pins:
                self.nets[net_name] = Net(net_name, type_="io")
            elif net_name in self.pwr_pins:
                self.nets[net_name] = Net(net_name, type_="power")
            elif net_name in self.gnd_pins:
                self.nets[net_name] = Net(net_name, type_="ground")
            else:
                self.nets[net_name] = Net(net_name, type_="internal")
        return self.nets[net_name]

    def get_net_names(self, with_power_ground=False):
        if with_power_ground:
            return list(self.nets.keys())
        else:
            return [
                net_name
                for net_name in self.nets.keys()
                if not net_name in config.PWR_NET_NAMES + config.GND_NET_NAMES
            ]

    def get_nets(self, with_power_ground=False):
        if with_power_ground:
            return list(self.nets.values())
        else:
            return [
                net
                for net in self.nets.values()
                if not net.name in config.PWR_NET_NAMES + config.GND_NET_NAMES
            ]

    def _is_output_by_pin_order(self, net_name):
        """Heuristic output detection from subckt pin order.

        For single-output cells (the common case) the LAST IO pin in the
        ``.SUBCKT`` declaration order is the output. ``io_pins`` preserves the
        CDL pin order (power/ground stripped), so its tail is the output pin.
        """
        if not self.io_pins:
            return False
        return net_name == self.io_pins[-1]

    def is_input_net(self, net_name):
        """True iff ``net_name`` is a primary input pin.

        Resolution order:
          1. ``config.INPUT_NET_NAMES`` (the curated input-pin collection).
          2. ``config.OUTPUT_NET_NAMES`` membership rules it out.
          3. Fallback to subckt pin order: an IO pin that is not the
             (last-pin) output is treated as an input.
        """
        if net_name not in self.io_pins:
            return False
        if net_name in config.INPUT_NET_NAMES:
            return True
        if net_name in config.OUTPUT_NET_NAMES:
            return False
        return not self._is_output_by_pin_order(net_name)

    def is_output_net(self, net_name):
        """True iff ``net_name`` is a primary output pin.

        Resolution order:
          1. ``config.OUTPUT_NET_NAMES`` (the curated output-pin collection).
          2. ``config.INPUT_NET_NAMES`` membership rules it out.
          3. Fallback to subckt pin order: the last IO pin is the output for
             single-output cells.
        """
        if net_name not in self.io_pins:
            return False
        if net_name in config.OUTPUT_NET_NAMES:
            return True
        if net_name in config.INPUT_NET_NAMES:
            return False
        return self._is_output_by_pin_order(net_name)

    def input_net_names(self):
        """Primary-input net names in subckt (CDL) pin order."""
        return [n for n in self.io_pins if self.is_input_net(n)]

    def output_net_names(self):
        """Primary-output net names in subckt (CDL) pin order."""
        return [n for n in self.io_pins if self.is_output_net(n)]

    def get_power_ground_nets(self):
        return [net for net in self.nets.values() if net.is_power_or_ground_net()]

    def get_power_net_name(self):
        return self.pwr_pins[0]

    def get_ground_net_name(self):
        return self.gnd_pins[0]

    def io_net_names(self):
        # print(f"IO pins: {self.io_pins}")
        return self.io_pins  # If just returning io_pins

    def get_gate_net_names(self):
        return set([t.terminals["gate"] for t in self.transistors.values()])

    def if_net_exists(self, net_name):
        return net_name in self.nets

    def if_transistor_exists(self, transistor_name):
        return transistor_name in self.transistors
    
    def group_transistors_by_nets_and_types(self):
        """
        Transistors belong to the same group if they have identify source/gate/drain nets. 
        By types => Must all be PMOS or all be NMOS
        In that case, there placement can be pre-determined
        """
        tmp_nets_to_transistor_groups = {}
        transistor_groups = []
        for i, tran_i in enumerate(self.transistors.values()):
            for tran_j in list(self.transistors.values())[i + 1 :]:
                FLAG_MATCH = True
                # match gate:
                if tran_i.gate != tran_j.gate:
                    FLAG_MATCH = False
                # match source has to match one of the source/drain
                if tran_i.source != tran_j.source and tran_i.source != tran_j.drain:
                    FLAG_MATCH = False
                # match drain has to match one of the source/drain
                if tran_i.drain != tran_j.source and tran_i.drain != tran_j.drain:
                    FLAG_MATCH = False
                net_keys = "_".join(sorted([tran_i.gate, tran_i.drain, tran_i.source]))
                # if match, try to group them by nets
                if FLAG_MATCH:
                    if net_keys not in tmp_nets_to_transistor_groups:
                        tmp_nets_to_transistor_groups[net_keys] = set()
                    tmp_nets_to_transistor_groups[net_keys].add(tran_i.name)
                    tmp_nets_to_transistor_groups[net_keys].add(tran_j.name)
        # transistor_groups = tmp_nets_to_transistor_groups.values()
        # return transistor_groups
        return tmp_nets_to_transistor_groups
    
    def group_transistors_by_nets(self):
        """
        Transistors belong to the same group if they have identify source/gate/drain nets. 
        In that case, there placement can be pre-determined
        """
        tmp_nmos_nets_to_transistor_groups = {}
        tmp_pmos_nets_to_transistor_groups = {}
        for i, tran_i in enumerate(self.transistors.values()):
            for tran_j in list(self.transistors.values())[i + 1 :]:
                # separate PMOS and NMOS
                if tran_i.model != tran_j.model:
                    continue
                FLAG_MATCH = True
                # match gate:
                if tran_i.gate != tran_j.gate:
                    FLAG_MATCH = False
                # match source has to match one of the source/drain
                if tran_i.source != tran_j.source and tran_i.source != tran_j.drain:
                    FLAG_MATCH = False
                # match drain has to match one of the source/drain
                if tran_i.drain != tran_j.source and tran_i.drain != tran_j.drain:
                    FLAG_MATCH = False
                net_keys = "_".join(sorted([tran_i.gate, tran_i.drain, tran_i.source]))
                # if match, try to group them by nets
                if FLAG_MATCH:
                    if tran_i.model == Model.NMOS:
                        if net_keys not in tmp_nmos_nets_to_transistor_groups:
                            tmp_nmos_nets_to_transistor_groups[net_keys] = set()
                        tmp_nmos_nets_to_transistor_groups[net_keys].add(tran_i.name)
                        tmp_nmos_nets_to_transistor_groups[net_keys].add(tran_j.name)
                    elif tran_i.model == Model.PMOS:
                        if net_keys not in tmp_pmos_nets_to_transistor_groups:
                            tmp_pmos_nets_to_transistor_groups[net_keys] = set()
                        tmp_pmos_nets_to_transistor_groups[net_keys].add(tran_i.name)
                        tmp_pmos_nets_to_transistor_groups[net_keys].add(tran_j.name)
        # nmos_transistor_groups = tmp_nets_to_transistor_groups.values()
        return {"PMOS": tmp_pmos_nets_to_transistor_groups, "NMOS": tmp_nmos_nets_to_transistor_groups}

                
    
    def group_transistors_by_low_degree_nets_and_types(self):
        """
        Transistors (same type) which share the same source/drain net should be placed adjacently
        If the source/drain net has only a degree of 2.
        """
        # tmp_nets_to_transistor_groups = {}
        transistor_groups = []
        for i, tran_i in enumerate(self.transistors.values()):
            for tran_j in list(self.transistors.values())[i + 1 :]:
                # must be same type
                if tran_i.model != tran_j.model:
                    continue
                FLAG_NOT_MATCH = True
                # match_source
                if tran_i.source == tran_j.source and self.nets[tran_i.source].degree() <= 2:
                    FLAG_NOT_MATCH = False
                # match drain
                if tran_i.drain == tran_j.drain and self.nets[tran_i.drain].degree() <= 2:
                    FLAG_NOT_MATCH = False
                # match source to drain
                if tran_i.source == tran_j.drain and self.nets[tran_i.source].degree() <= 2:
                    FLAG_NOT_MATCH = False
                # match drain to source
                if tran_i.drain == tran_j.source and self.nets[tran_i.drain].degree() <= 2:
                    FLAG_NOT_MATCH = False
                # if some net matched
                if not FLAG_NOT_MATCH:
                    transistor_groups.append((tran_i.name, tran_j.name))
        return transistor_groups

    def add_transistor(
        self,
        name,
        source,
        gate,
        drain,
        bulk,
        model,
        w,
        l,
        nfin,
        m=None,
        par=None,
        p_la=None,
        nf=None,
        ngcon=None,
        plorient=None,
        cpp=None,
        pre_layout_local=None,
        ptwell=None,
        wns=None,
    ):
        if not name.startswith("M"):
            raise ValueError(f"A transistor name must start with M. Found transistor name {name} in subcircuit {self.subckt_name}")
        
        # Convert nfin to integer if it's a string
        nfin = int(nfin) if isinstance(nfin, str) else nfin
        
        # Calculate number of transistors to create (base unit is 2 fins)
        num_copies = max(1, nfin // 2)
        if nfin % 2 != 0:
            logger.warning(f"nfin={nfin} is not divisible by 2 for transistor {name}. Rounding down.")
        
        # Find the starting suffix index
        suffix_idx = 0
        while f"{name}S{suffix_idx}" in self.transistors:
            suffix_idx += 1
        
        # Create multiple transistors if nfin > 2
        for copy_idx in range(num_copies):
            # Create a transistor instance with nfin=2 (base unit)
            t = Transistor(
                name,
                source=source,
                gate=gate,
                drain=drain,
                bulk=bulk,
                model=model,
                w=w,
                l=l,
                m=m,
                nfin=2,  # Base unit is 2 fins
                par=par,
                p_la=p_la,
                nf=nf,
                ngcon=ngcon,
                plorient=plorient,
                cpp=cpp,
                pre_layout_local=pre_layout_local,
                ptwell=ptwell,
                wns=wns,
            )
            
            t.name = f"{name}S{suffix_idx + copy_idx}"
            self.transistors[f"{name}S{suffix_idx + copy_idx}"] = t
            logger.debug(f"Adding transistor: {t}")
            
            # Register transistor with each net
            for pin_type, net_name in t.terminals.items():
                # Note: need to check None here, because we set bulk to None by default
                if net_name is None:
                    continue
                net_obj = self.add_net(net_name)
                net_obj.add_connection(t.name, pin_type)

    def rebuild_nets(self):
        """Rebuild the ``nets`` dictionary from current transistor S/D/G assignments.

        After modifying transistor source/drain terminals (e.g. for a
        topology variant), the ``Net.connected_transistors`` lists become
        stale.  This method clears and re-populates them so that
        ``net.source()``, ``net.terminals()``, etc. reflect the updated
        wiring.

        Pins, pin classification (io / power / ground), and net types
        are preserved from the original ``assign_pins`` call.
        """
        # Preserve pin classification
        saved_pins = list(self.pins)
        saved_io = list(self.io_pins)
        saved_pwr = list(self.pwr_pins)
        saved_gnd = list(self.gnd_pins)

        # Clear all nets
        self.nets.clear()

        # Restore pin lists (needed by add_net to classify net types)
        self.pins = saved_pins
        self.io_pins = saved_io
        self.pwr_pins = saved_pwr
        self.gnd_pins = saved_gnd

        # Re-register every transistor's terminals
        for t in self.transistors.values():
            for pin_type, net_name in t.terminals.items():
                if net_name is None:
                    continue
                net_obj = self.add_net(net_name)
                net_obj.add_connection(t.name, pin_type)

    def generate_networkx_graph(self):
        """
        mos -> node
        net -> node
        connect mos <-> net
        """
        G = nx.Graph()
        for tran_name, t in self.transistors.items():
            G.add_node(tran_name)
            G.add_node(t.drain)
            G.add_node(t.gate)
            G.add_node(t.source)
            G.add_edge(tran_name, t.source)
            G.add_edge(tran_name, t.gate)
            G.add_edge(tran_name, t.drain)
        return G

    def __repr__(self):
        return f"Circuit(subckt={self.subckt_name}, io pins={self.io_pins}, power pin={self.pwr_pins}, ground pin={self.gnd_pins}, nets={list(self.nets.keys())}, transistors={list(self.transistors.keys())})"


class MetaCircuit:
    """A collection of ``Circuit`` variants for one cell.

    ``MetaCircuit`` holds a *base* circuit (the original CDL parse) and
    zero or more *topology variants* - copies of the base circuit whose
    transistor source/drain assignments have been permuted.

    Typical usage::

        meta = MetaCircuit("NAND2_X1", base_circuit)
        meta.add_variant(variant_circuit)
        print(meta)                       # summary
        print(meta.num_variants)          # e.g. 1
        variant_0 = meta.get_variant(0)   # Circuit object

    Attributes:
        cell_name: The ``.SUBCKT`` name of the cell.
        base_circuit: The original ``Circuit`` (before topology changes).
        topology_variants: List of ``Circuit`` objects, one per canonical
            topology variant.
    """

    def __init__(self, cell_name: str, base_circuit: Circuit):
        """Create a ``MetaCircuit`` from a base circuit.

        Args:
            cell_name: The ``.SUBCKT`` name.
            base_circuit: The original ``Circuit`` object.
        """
        self.cell_name: str = cell_name
        self.base_circuit: Circuit = base_circuit
        self.topology_variants: list[Circuit] = []

    # -- public API ----------------------------------------------------

    @property
    def num_variants(self) -> int:
        """Number of topology variants stored."""
        return len(self.topology_variants)

    def add_variant(self, circuit: Circuit):
        """Append a topology variant ``Circuit``.

        Args:
            circuit: A ``Circuit`` whose transistor S/D assignments
                represent one topology variant.
        """
        self.topology_variants.append(circuit)

    def get_variant(self, idx: int) -> Circuit:
        """Return the topology-variant ``Circuit`` at *idx*.

        Args:
            idx: Zero-based index into ``topology_variants``.

        Returns:
            The ``Circuit`` object for that variant.

        Raises:
            IndexError: If *idx* is out of range.
        """
        return self.topology_variants[idx]

    def __len__(self):
        return len(self.topology_variants)

    def __iter__(self):
        return iter(self.topology_variants)

    def __getitem__(self, idx):
        return self.topology_variants[idx]

    def __repr__(self):
        return (
            f"MetaCircuit(cell={self.cell_name}, "
            f"base_transistors={self.base_circuit.num_transistors()}, "
            f"variants={self.num_variants})"
        )


class MetalLayer:
    def __init__(
        self,
        layer_name: str,
        layer_type: str,
        direction: str,
        offset: float,
        pitch: float,
        width: float,
        io_pin: bool = False,
        middle_power_rail_spacing: float = 0.0,
        gds_layer: int | None = None,
        gds_datatype: int = 0,
        horizontal_enclosure: float = 0.0,
        vertical_enclosure: float = 0.0,
    ):
        """
        Example signature; your actual implementation may have more fields or methods.
        """
        self.layer_name = layer_name
        self.layer_type = layer_type
        self.direction = direction
        self.offset = offset
        self.pitch = pitch
        self.width = width
        self.io_pin = io_pin
        self.middle_power_rail_spacing = middle_power_rail_spacing
        self.gds_layer = gds_layer
        self.gds_datatype = gds_datatype
        # Wire-end via enclosure overhang (nm) the metal extends past its routing
        # endpoint to enclose the landing via. Used by GDS generation; defaults
        # to 0.0 so layers/JSONs without the field are unaffected.
        self.horizontal_enclosure = horizontal_enclosure
        self.vertical_enclosure = vertical_enclosure

    def __repr__(self):
        return f"MetalLayer({self.layer_name}, {self.direction}, {self.width}, io_pin={self.io_pin})"


class ViaLayer:
    def __init__(
        self,
        layer_name: str,
        layer_type: str,
        gds_layer: int | None = None,
        gds_datatype: int = 0,
    ):
        """
        Example signature; your actual implementation may have more fields or methods.
        """
        self.layer_name = layer_name
        self.layer_type = layer_type
        self.gds_layer = gds_layer
        self.gds_datatype = gds_datatype

    def __repr__(self):
        return f"ViaLayer({self.layer_name})"


class LayerStack:
    def __init__(self, json_input: Union[str, dict]):
        """
        If json_input is a string, treat it as a path to a JSON file.
        If json_input is already a dict, use it directly.

        During initialization:
          1. Read/parse the JSON into a dict.
          2. Build all MetalLayer instances (storing them in a temp dict).
          3. Sort those metals by "layer_number" (ascending) and call add_metal_layer(...)
          4. Build all ViaLayer instances, look up their upper/lower metal objects,
             and call add_via_layer(lower_metal_obj, upper_metal_obj, via_obj).
        """
        # --------------------------------------------------------------------------
        # 1. Load the JSON (if a filename was passed in)
        # --------------------------------------------------------------------------
        if isinstance(json_input, str):
            # Treat json_input as a path to a file
            with open(json_input, "r") as f:
                layer_dict = json.load(f)
        elif isinstance(json_input, dict):
            # Already a Python dict
            layer_dict = json_input
        else:
            raise ValueError("LayerStack __init__ expects a JSON filename or a dict.")

        # --------------------------------------------------------------------------
        # 2. Partition the entries into metal_entries vs. via_entries
        # --------------------------------------------------------------------------
        metal_entries = {}
        via_entries = {}
        virtual_entries = {}
        gds_entries = {}

        for name, props in layer_dict.items():
            ltype = props.get("layer_type", "").lower()
            if ltype == "metal":
                metal_entries[name] = props
            elif ltype == "via":
                via_entries[name] = props
            elif ltype == "virtual":
                # Virtual jump: a direct edge between two metal layers in the
                # LGG, regardless of whether they're adjacent. No physical
                # geometry - just a graph shortcut. Required fields:
                # lower_layer, upper_layer. Optional: method (default "overlap").
                virtual_entries[name] = props
            elif ltype == "gds":
                # GDS-only entry: a layer the solver never sees (active,
                # select, well, boundary, debug overlays). Required:
                # gds_layer. Optional: gds_datatype (default 0).
                gds_entries[name] = props
            else:
                raise ValueError(
                    f"Layer '{name}' has unknown layer_type '{ltype}'. "
                    "Expected 'metal', 'via', 'virtual', or 'gds'."
                )

        # --------------------------------------------------------------------------
        # 3. Instantiate every MetalLayer and keep a name->object mapping
        # --------------------------------------------------------------------------
        #    We also want to remember "layer_number" so we can sort.
        #    But MetalLayer __init__ does not take layer_number, so we just store it
        #    in a temporary dict for sorting. The real MetalLayer only needs the
        #    fields (layer_name, layer_type, direction, offset, pitch, width).
        # --------------------------------------------------------------------------
        temp_metal_list = []
        name_to_metalobj = {}

        for name, props in metal_entries.items():
            ln = props["layer_number"]  # used for ordering only
            direction = props["direction"]
            offset = float(props["offset"])
            pitch = float(props["pitch"])
            width = float(props["width"])
            layer_type = props["layer_type"]  # should be "metal"

            # Create the MetalLayer instance
            io_pin = bool(props.get("io_pin", False))
            mid_prs = float(props.get("middle_power_rail_spacing", 0.0))
            gds_layer = props.get("gds_layer")
            gds_datatype = int(props.get("gds_datatype", 0))
            # Wire-end via enclosure overhang (GDS). Absent -> 0.0 (no overhang).
            horiz_enc = float(props.get("horizontal_enclosure", 0.0))
            verti_enc = float(props.get("vertical_enclosure", 0.0))
            ml = MetalLayer(
                layer_name=props["layer_name"],
                layer_type=layer_type,
                direction=direction,
                offset=offset,
                pitch=pitch,
                width=width,
                io_pin=io_pin,
                middle_power_rail_spacing=mid_prs,
                gds_layer=gds_layer,
                gds_datatype=gds_datatype,
                horizontal_enclosure=horiz_enc,
                vertical_enclosure=verti_enc,
            )
            # 06/18/2025 Note: Implicitly a half metal offset to M1 since we do not encode left boundary M1 
            # if ml.layer_name == "M1" and ml.direction == "V":
            #     ml.offset += ml.pitch / 2
            # Keep it in a list along with its layer_number
            temp_metal_list.append((ln, ml))
            name_to_metalobj[name] = ml

        # --------------------------------------------------------------------------
        # 4. Sort the metals by layer_number ascending (lowest -> highest) and add them
        # --------------------------------------------------------------------------
        temp_metal_list.sort(key=lambda x: x[0])  # sort by layer_number
        self.metal_layers = []
        self.layer_to_index = {}

        for idx, (_, metal_obj) in enumerate(temp_metal_list):
            self.metal_layers.append(metal_obj)
            # layer_to_index maps layer_name -> index in metal_layers list
            self.layer_to_index[metal_obj.layer_name] = idx

        # --------------------------------------------------------------------------
        # 5. Prepare an empty dict for via_layers; we'll add them next
        # --------------------------------------------------------------------------
        #    The key is (lower_metal_name, upper_metal_name) -> ViaLayer
        # --------------------------------------------------------------------------
        self.via_layers = {}

        # --------------------------------------------------------------------------
        # 6. Instantiate every ViaLayer and immediately call add_via_layer(...)
        # --------------------------------------------------------------------------
        for via_name, props in via_entries.items():
            # Example props:
            #   {
            #     "layer_type": "via",
            #     "layer_number": 8,             # may not be strictly necessary
            #     "layer_name": "CA",
            #     "upper_layer": "M0",
            #     "lower_layer": "PC",
            #     "vertical_enclosure": 0.0,
            #     "horizontal_enclosure": 0.0
            #   }

            lower_name = props["lower_layer"]
            upper_name = props["upper_layer"]

            # Look up the already created MetalLayer instances
            lower_metal_obj = name_to_metalobj.get(lower_name)
            upper_metal_obj = name_to_metalobj.get(upper_name)
            if lower_metal_obj is None or upper_metal_obj is None:
                raise ValueError(
                    f"Via '{via_name}' refers to lower='{lower_name}' or upper='{upper_name}', "
                    "but one of those metals was not defined in the JSON."
                )

            layer_type = props["layer_type"]  # should be "via"

            # Create the ViaLayer instance
            via_obj = ViaLayer(
                layer_name=props["layer_name"],
                layer_type=layer_type,
                gds_layer=props.get("gds_layer"),
                gds_datatype=int(props.get("gds_datatype", 0)),
            )

            # Finally, register this via in our internal dict
            self.add_via_layer(lower_metal_obj, upper_metal_obj, via_obj)

        # --------------------------------------------------------------------------
        # 7. Collect virtual jump pairs (graph-only shortcuts between metals)
        # --------------------------------------------------------------------------
        # self.virtual_pairs is a list of (lower_name, upper_name, method) tuples,
        # consumed by archit-level _init_graph and passed to LayeredGridGraph as
        # virtual_connect_pairs + virtual_connect_method. method defaults to
        # "overlap". Pairs may be non-adjacent (that is the whole point).
        #
        # self.virtual_layers carries the richer per-virtual metadata (name +
        # gds_layer + gds_datatype) so a downstream GDS writer can draw the
        # virtual jumps on a debug layer. Keyed by virtual name; the solver
        # only consumes virtual_pairs.
        self.virtual_pairs = []
        self.virtual_layers = {}
        for vname, props in virtual_entries.items():
            lower_name = props["lower_layer"]
            upper_name = props["upper_layer"]
            if lower_name not in name_to_metalobj or upper_name not in name_to_metalobj:
                raise ValueError(
                    f"Virtual '{vname}' refers to lower='{lower_name}' or "
                    f"upper='{upper_name}', but one of those metals was not "
                    "defined in the JSON."
                )
            method = props.get("method", "overlap")
            self.virtual_pairs.append((lower_name, upper_name, method))
            self.virtual_layers[vname] = {
                "layer_name": props.get("layer_name", vname),
                "lower_layer": lower_name,
                "upper_layer": upper_name,
                "method": method,
                "gds_layer": props.get("gds_layer"),
                "gds_datatype": int(props.get("gds_datatype", 0)),
            }

        # --------------------------------------------------------------------------
        # 8. Solver-irrelevant GDS-only layers (active / select / well /
        #    boundary / debug). Stored as {name: (gds_layer, gds_datatype)};
        #    consumed by downstream GDS writers only.
        # --------------------------------------------------------------------------
        self.gds_layers = {}
        for name, props in gds_entries.items():
            if "gds_layer" not in props:
                raise ValueError(
                    f"GDS layer '{name}' is missing the required "
                    "'gds_layer' field."
                )
            self.gds_layers[name] = (
                int(props["gds_layer"]),
                int(props.get("gds_datatype", 0)),
            )

        # --------------------------------------------------------------------------
        # 9. Validate io_pin constraints
        # --------------------------------------------------------------------------
        self._validate_io_pin()
        self._validate_middle_power_rail_spacing()

    # ---------------------------------------------------------------------
    # (The rest of LayerStack is unchanged:)
    # ---------------------------------------------------------------------
    def add_metal_layer(self, metal_layer: MetalLayer):
        """
        Adds a metal layer to the stack in order (lowest to highest).
        """
        self.metal_layers.append(metal_layer)
        self.layer_to_index[metal_layer.layer_name] = len(self.metal_layers) - 1

    def add_via_layer(
        self,
        lower_metal_layer: MetalLayer,
        upper_metal_layer: MetalLayer,
        via_layer: ViaLayer,
    ):
        """
        Defines the via layer that connects two adjacent metal layers.
        """
        self.via_layers[(lower_metal_layer.layer_name, upper_metal_layer.layer_name)] = via_layer

    def get_upper(self, metal_layer: MetalLayer):
        """
        For the given metal layer, returns a tuple (upper_metal_layer, via_layer) where:
          - upper_metal_layer is the next metal layer above.
          - via_layer is the ViaLayer connecting the two.
        Returns (None, None) if there is no upper layer.
        """
        idx = self.layer_to_index.get(metal_layer.layer_name)
        if idx is None or idx == len(self.metal_layers) - 1:
            return None, None
        upper_metal = self.metal_layers[idx + 1]
        via = self.via_layers.get((metal_layer.layer_name, upper_metal.layer_name))
        return upper_metal, via

    def get_lower(self, metal_layer: MetalLayer):
        """
        For the given metal layer, returns a tuple (lower_metal_layer, via_layer) where:
          - lower_metal_layer is the next metal layer below.
          - via_layer is the ViaLayer connecting the two.
        Returns (None, None) if there is no lower layer.
        """
        idx = self.layer_to_index.get(metal_layer.layer_name)
        if idx is None or idx == 0:
            return None, None
        lower_metal = self.metal_layers[idx - 1]
        via = self.via_layers.get((lower_metal.layer_name, metal_layer.layer_name))
        return lower_metal, via

    # -----------------------------------------------------------------
    # io_pin helpers
    # -----------------------------------------------------------------
    def _find_tier_boundary(self) -> int | None:
        """Return the metal_layers index that is the first layer of the
        upper tier (frontside), or *None* if no inter-tier via (MIV*) exists.

        Convention: any via whose layer_name starts with ``"MIV"`` (so
        ``MIV``, ``MIV1``, ``MIV2``, ... all count) marks a tier boundary.
        CFFET stacks use ``STV`` (inter-block stitch) as the face split instead.
        When several MIV-prefixed vias exist (QFET 2-placement + N-mid-routing
        chain), the lowest one is the canonical split point - that's the via
        leaving the last placement-side layer on the backside. All metals
        with index **below** the chosen MIV upper layer belong to tier 0
        (backside); the upper layer and above belong to tier 1 (frontside).
        """
        stv_uppers = [
            self.layer_to_index[upper_name]
            for (_, upper_name), via_obj in self.via_layers.items()
            if via_obj.layer_name == "STV"
        ]
        if stv_uppers:
            return min(stv_uppers)
        miv_uppers = [
            self.layer_to_index[upper_name]
            for (_, upper_name), via_obj in self.via_layers.items()
            if via_obj.layer_name.startswith("MIV")
        ]
        return min(miv_uppers) if miv_uppers else None

    def _validate_io_pin(self):
        """Validate io_pin constraints across the layer stack.

        Rules:
          1. Each tier (split at the MIV via) can have at most one layer
             with ``io_pin == True``.  If there is no MIV, all layers
             belong to a single tier and at most one may be flagged.
        """
        # Rule 1: at most one io_pin per tier
        boundary_idx = self._find_tier_boundary()
        tier_0_io: list[str] = []
        tier_1_io: list[str] = []
        for idx, ml in enumerate(self.metal_layers):
            if not ml.io_pin:
                continue
            if boundary_idx is None or idx < boundary_idx:
                tier_0_io.append(ml.layer_name)
            else:
                tier_1_io.append(ml.layer_name)

        if len(tier_0_io) > 1:
            raise ValueError(
                f"Tier 0 (backside) has multiple io_pin layers: {tier_0_io}. "
                f"At most one per tier is allowed."
            )
        if len(tier_1_io) > 1:
            raise ValueError(
                f"Tier 1 (frontside) has multiple io_pin layers: {tier_1_io}. "
                f"At most one per tier is allowed."
            )

    def io_pin_layers(self) -> list[MetalLayer]:
        """Return the metal layers with ``io_pin == True``, ordered by
        their position in the stack (lowest index first)."""
        return [ml for ml in self.metal_layers if ml.io_pin]

    def _validate_middle_power_rail_spacing(self):
        """Validate middle_power_rail_spacing constraints.

        Rules:
          1. ``middle_power_rail_spacing`` must be non-negative.
          2. It can only be set (> 0) on horizontal layers
             (``direction == "H"``).
        """
        for ml in self.metal_layers:
            if ml.middle_power_rail_spacing < 0:
                raise ValueError(
                    f"Layer '{ml.layer_name}' has negative "
                    f"middle_power_rail_spacing={ml.middle_power_rail_spacing}."
                )
            if ml.middle_power_rail_spacing > 0 and ml.direction != "H":
                raise ValueError(
                    f"Layer '{ml.layer_name}' has middle_power_rail_spacing="
                    f"{ml.middle_power_rail_spacing} but direction "
                    f"'{ml.direction}' — only horizontal (direction='H') "
                    f"layers may define a middle power rail spacing."
                )

    def get_metal_layer(self, layer_name: str) -> MetalLayer:
        """Return the ``MetalLayer`` object for *layer_name*."""
        idx = self.layer_to_index.get(layer_name)
        if idx is None:
            raise ValueError(f"Metal layer '{layer_name}' not found in the stack.")
        return self.metal_layers[idx]

    @classmethod
    def from_json(cls, json_input: Union[str, dict]) -> "LayerStack":
        """Load a layer stack from a JSON file path or dict."""
        return cls(json_input)
