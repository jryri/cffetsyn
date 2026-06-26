"""
Shared utilities for the cellgen subpackage.

Currently:
  - print_smtcell_banner: SMTCell2.0 ANSI-Shadow startup banner with cyan->magenta
    gradient, used by every architecture's main entry point (QFET, FinFET, ...).
  - log_variable_info:    dump every solver variable + assigned value (debug aid).
  - read_cdl_file:        parse a SPICE/CDL netlist into Circuit objects.
  - parse_netlist:        parse a single .SUBCKT block into a Circuit.
"""

import itertools
import math

from loguru import logger

from src.cellgen.core.entity import Circuit


# ANSI-Shadow rendering of "SMTCell2.0". Bold + per-row gradient applied at print time.
_SMTCELL_LOGO = (
    r"  ███████╗███╗   ███╗████████╗ ██████╗███████╗██╗     ██╗     ██████╗     ██████╗",
    r"  ██╔════╝████╗ ████║╚══██╔══╝██╔════╝██╔════╝██║     ██║     ╚════██╗   ██╔═████╗",
    r"  ███████╗██╔████╔██║   ██║   ██║     █████╗  ██║     ██║      █████╔╝   ██║██╔██║",
    r"  ╚════██║██║╚██╔╝██║   ██║   ██║     ██╔══╝  ██║     ██║     ██╔═══╝    ████╔╝██║",
    r"  ███████║██║ ╚═╝ ██║   ██║   ╚██████╗███████╗███████╗███████╗███████╗██╗╚██████╔╝",
    r"  ╚══════╝╚═╝     ╚═╝   ╚═╝    ╚═════╝╚══════╝╚══════╝╚══════╝╚══════╝╚═╝ ╚═════╝",
)

# Cyan -> magenta gradient: interp R and G channels with B held at 255.
# One color per logo row, top-to-bottom.
_SMTCELL_GRADIENT = ("#00ffff", "#33ccff", "#6699ff", "#9966ff", "#cc33ff", "#ff00ff")

_SMTCELL_TAGLINE = "Next-Generation Cell-Layout Generator"


def print_smtcell_banner(*, archit: str, tech: str, subckt: str, tagline: str = _SMTCELL_TAGLINE) -> None:
    """
    Log the SMTCell2.0 startup banner via loguru.

    Args:
        archit:  architecture family name (e.g. "QFET", "FinFET", "CFET").
        tech:    technology library name (e.g. "FreePDK15-2F-4T").
        subckt:  subcircuit being generated (e.g. "DFFHQN_X1").
        tagline: optional one-line description; defaults to the project tagline.

    The logo is bold cyan-to-magenta gradient; tech identifier is hot magenta;
    everything else is dim. Colors auto-strip on non-TTY sinks (file output),
    so log files stay clean.
    """
    bar = "━" * 80
    clog = logger.opt(colors=True)
    clog.info(f"<fg #00ffff>{bar}</fg #00ffff>")
    clog.info("")
    for line, color in zip(_SMTCELL_LOGO, _SMTCELL_GRADIENT):
        clog.info(f"<fg {color}><bold>{line}</bold></fg {color}>")
    clog.info("")
    clog.info(
        f"  <dim>{tagline}</dim>"
        f"   <dim>·</dim>   "
        f"<fg #ff66ff>{archit}</fg #ff66ff> <dim>/</dim> "
        f"<fg #ff66ff><bold>{tech}</bold></fg #ff66ff>"
    )
    clog.info(f"  <dim>▸ subckt:</dim>  <bold>{subckt}</bold>")
    clog.info(f"<fg #ff00ff>{bar}</fg #ff00ff>")
    clog.info("")

def log_variable_info(instance, filename=None):
    """
    Log every solver variable + assigned value from a solved instance.

    Walks `instance.opt.Proto().variables` paired with the solver's solution
    vector. When `filename` is provided, writes to that file; otherwise emits
    via loguru at INFO level.

    Args:
        instance: QFET (or any architecture instance) with `.opt` and `.solver`.
        filename: optional path. If None, log to stderr via loguru.
    """
    model_proto = instance.opt.Proto()
    response_proto = instance.solver.ResponseProto()
    if filename:
        with open(filename, "w") as f:
            f.write("Debugging variable information:\n")
            for var_proto, value in zip(model_proto.variables, response_proto.solution):
                f.write(f"{value}    {var_proto.name:<50}\n")
    else:
        logger.info("Debugging variable information:")
        for var_proto, value in zip(model_proto.variables, response_proto.solution):
            logger.info(f"{var_proto.name:<50} {value}")


def read_cdl_file(filename):
    """
    Read a CDL (SPICE-format) netlist file and return one Circuit per .SUBCKT block.

    Comments (lines starting with '*') are skipped. Each subcircuit block runs
    from its `.SUBCKT` header to the next `.ENDS` line; the block is passed to
    `parse_netlist` to populate a fresh Circuit.
    """
    circuits = []
    with open(filename, "r") as f:
        netlist_texts = f.read()
    netlist_text = ""
    flag_subckt = False
    for line in netlist_texts.splitlines():
        if line.startswith("*"):
            continue
        if line.startswith(".SUBCKT"):
            netlist_text = ""
            flag_subckt = True
        if flag_subckt:
            netlist_text += line + "\n"
        if line.startswith(".ENDS"):
            new_circuit = Circuit()
            parse_netlist(netlist_text, new_circuit)
            circuits.append(new_circuit)
            netlist_text = ""
            flag_subckt = False
    return circuits


def parse_netlist(netlist_text, circuit):
    """
    Parse one `.SUBCKT ... .ENDS` block into a Circuit (mutates in place).

    Line layout for each transistor row:
        Mname  source  gate  drain  bulk  model  [w=... l=... nfin=...]
    """
    for raw in netlist_text.strip().splitlines():
        line = raw.strip()
        if not line or line.startswith("*"):
            continue
        if line.upper().startswith(".SUBCKT"):
            tokens = line.split()
            if len(tokens) >= 3:
                circuit.subckt_name = tokens[1]
                circuit.assign_pins(tokens[2:])
                logger.debug(f"Parsing subcircuit {circuit.subckt_name!r}")
            continue
        if line.upper().startswith(".ENDS"):
            break

        tokens = line.split()
        if len(tokens) < 6:
            logger.warning(f"Invalid transistor line (need >=6 tokens): {line}")
            continue
        # Fixed positional tokens: name, source, gate, drain, bulk, model
        t_name, source, gate, drain, bulk, model = tokens[:6]
        # Optional key=value parameters after the fixed 6
        params = {}
        for token in tokens[6:]:
            if "=" in token:
                key, val = token.split("=", 1)
                params[key.lower()] = val
        circuit.add_transistor(
            t_name,
            source=source, gate=gate, drain=drain, bulk=bulk, model=model,
            w=params.get("w"), l=params.get("l"), nfin=params.get("nfin"),
        )


# ----- list helpers --------------

def sliding_windows(lst, X):
    """Return every length-X tuple-window over `lst` in order."""
    start_max = len(lst) - X
    return [tuple(lst[i : i + X]) for i in range(start_max + 1)]


def split_into_parts(lst, n, must_equal_length=False, overlap: int = 0):
    """
    Split `lst` into n parts.

    must_equal_length=False (default): round-robin allocation; sizes differ by 1.
    must_equal_length=True: every part has ceil(len/n) elements; consecutive
        parts can overlap by `overlap` elements (used by pin.py windowing).
    """
    if n <= 0:
        raise ValueError("n must be a positive integer")
    length = len(lst)
    if not must_equal_length:
        base_size, remainder = divmod(length, n)
        parts, start = [], 0
        for i in range(n):
            size = base_size + (1 if i < remainder else 0)
            parts.append(lst[start : start + size])
            start += size
        return parts

    pure_size = math.ceil(length / n)
    if overlap < 0 or overlap >= pure_size:
        raise ValueError(f"overlap must be between 0 and {pure_size - 1}")
    window_size = math.ceil((length + (n - 1) * overlap) / n)
    step = window_size - overlap
    parts = []
    for i in range(n):
        start = i * step
        if start > length - window_size:
            start = length - window_size
        parts.append(lst[start : start + window_size])
    return parts


def spaced_subsequences(lst, k, min_gap=1):
    """All length-k sub-sequences of `lst` with at least `min_gap` between picks."""
    n = len(lst)
    if k <= 0:
        return []
    step = min_gap + 1
    max_start = n - 1 - (k - 1) * step
    if max_start < 0:
        return []
    return [[lst[start + i * step] for i in range(k)] for start in range(max_start + 1)]


def half_permutations(lst):
    """
    Yield each permutation of `lst` exactly once up to head<->tail mirror,
    by fixing (head, tail) to the sorted pair from every 2-combination.
    """
    n = len(lst)
    if n <= 1:
        yield tuple(lst)
        return
    for a, b in itertools.combinations(lst, 2):
        first, last = (a, b) if a < b else (b, a)
        rest = [x for x in lst if x is not first and x is not last]
        for mid in itertools.permutations(rest):
            yield (first,) + mid + (last,)