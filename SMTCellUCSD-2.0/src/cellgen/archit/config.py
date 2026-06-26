import json
import os
import copy
import argparse
from enum import Enum

"""
Global variable initialization
Call only once at the beginning of main.py
"""
def init():
    # GLOBAL VARIABLES that defines the pin names
    global PWR_NET_NAMES
    global GND_NET_NAMES
    global INPUT_NET_NAMES
    global OUTPUT_NET_NAMES
    PWR_NET_NAMES= ['VDD']
    GND_NET_NAMES= ['VSS']
    INPUT_NET_NAMES = []
    OUTPUT_NET_NAMES = []
    # read the pin names from the json files
    with open("./input/pin_input_collection.json") as f:
        data = json.load(f)
        INPUT_NET_NAMES = data.keys()
    with open("./input/pin_output_collection.json") as f:
        data = json.load(f)
        OUTPUT_NET_NAMES = data.keys()

def _parse_value(raw):
    """Decode a CLI string into the most-specific JSON-compatible type.
    Order: null / bool / int / float / string. Quoted strings keep quotes
    so a user can force "1" to stay a string via --override key=\"1\"."""
    s = raw.strip()
    if s.lower() in ("null", "none"):
        return None
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    # A bracketed/braced literal is a JSON list/dict (e.g. inject_placement
    # carries a list of [tran_name, x, y, flip] tuples). Decode it so an
    # --override list/dict value becomes a real Python container instead of
    # a string. ADDITIVE: a json.loads failure falls through to the scalar
    # logic below (never raises), so all existing scalar behavior is intact.
    if s[:1] in "[{":
        try:
            return json.loads(s)
        except ValueError:
            pass
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _parse_overrides(items):
    """Turn a list of `key=value` strings into {key: parsed_value}. Empty
    list is fine. Duplicates: later wins (Make ordering)."""
    out = {}
    for it in items:
        if "=" not in it:
            raise SystemExit(f"--override expects KEY=VALUE, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = _parse_value(v)
    return out


def _apply_overrides(template, overrides):
    """Mutate template in place. Two syntaxes:

      key=val          -> template[key]["value"] = val   (common case)
      key.sub=val      -> template[key]["sub"]   = val   (nested field)
      key.sub.deep=val -> template[key]["sub"]["deep"] = val   (deeper)

    The dotted form is essential for entries whose `value` field is a
    toggle and the actual payload lives in a sibling field - e.g.
    max_time has `value: bool` (enable/disable) and `time: int` (seconds);
    `max_time.time=2000` sets the seconds without touching the toggle.

    Unknown TOP-LEVEL keys raise (fail loud - catches typos in .mk).
    Unknown nested subkeys raise too. Missing intermediate dicts raise
    rather than silently auto-create - the template shape is canonical.
    """
    if not overrides:
        return
    for key, v in overrides.items():
        parts = key.split(".")
        top = parts[0]
        if top not in template:
            raise SystemExit(
                f"CONFIG_OVERRIDES key {top!r} is not in CONFIG_TEMPLATE. "
                f"Known keys: {sorted(template)}"
            )
        # Default to .value when no path given (legacy ergonomic shortcut).
        if len(parts) == 1:
            entry = template[top]
            if not isinstance(entry, dict) or "value" not in entry:
                raise SystemExit(
                    f"CONFIG_TEMPLATE[{top!r}] has no 'value' field; "
                    f"either dotted-path the field you want, or add 'value'."
                )
            entry["value"] = v
            continue
        # Dotted path: walk to the last-but-one container, then assign.
        node = template[top]
        for i, p in enumerate(parts[1:-1], start=1):
            if not isinstance(node, dict) or p not in node:
                raise SystemExit(
                    f"CONFIG_OVERRIDES path {key!r}: subkey "
                    f"{'.'.join(parts[:i+1])!r} not found in template."
                )
            node = node[p]
        leaf = parts[-1]
        if not isinstance(node, dict) or leaf not in node:
            raise SystemExit(
                f"CONFIG_OVERRIDES path {key!r}: subkey {leaf!r} not "
                f"found under {'.'.join(parts[:-1])!r}. "
                f"Known sub-keys: {sorted(node) if isinstance(node, dict) else 'N/A'}"
            )
        node[leaf] = v


def generate_config(track, tech, height_config, circuit_names, output_dir,
                    overrides=None, force=False):
    """Generate per-cell config JSONs.

    A cell whose ``<output_dir>/config/<cell>.json`` ALREADY EXISTS is left
    untouched (it may carry user-tuned settings) unless ``force=True``. This
    makes ``make config`` idempotent and safe to re-run after toggling more
    cells - already-generated configs are never overwritten."""
    CONFIG_TEMPLATE = {
        "minimum_gate_cut_length" : {
            "value": 2,
            "info": "[TECH] Minimum gate cut length in CPP"
        },
        "lisd_routing" : {
            "value":False,
            "info": "[TECH] Use LISD as a routing resource"
        },
        "lig_routing" : {
            "value":False,
            "info": "[TECH] Use LIG as a routing resource"
        },
        "metal_cost": {
            "value": 1,
            "info": "[COST] Per-edge cost for same-layer wire edges"
        },
        "via_cost": {
            "value": 3,
            "info": "[COST] Per-edge cost for adjacent-layer via edges"
        },
        "virtual_edge_cost": {
            "value": 5,
            "info": "[COST] Per-edge cost for non-adjacent virtual jump edges (VL)"
        },
        "append_gds": {
            "value": True,
            "info": "[GDS] True: append cell into the library-wide gds/<lib>.gds. "
                    "False: emit standalone gds/<cell>.gds"
        },
        "enable_routing": {
            "value": True,
            "info": "[ROUTING] False: placement-only solve, no routing constraints"
        },
        "routing_stage": {
            "value": "external",
            "info": "[ROUTING] internal | external. external = force IO-pin "
                    "SON tree (required for any cell with IO pins). placement = "
                    "no routing (subset of enable_routing=False)."
        },
        "enable_via_induce": {
            "value": False,
            "info": "[ROUTING] True: enforce via-induced metal extension. "
                    "Default False — interacts with arc<->flow biconditional."
        },
        "deterministic_solve": {
            "value": False,
            "info": "[SOLVER] True: lex-ordered objective hierarchy for unique "
                    "minimizer across num_search_workers > 1"
        },
        "route_determinism": {
            "value": False,
            "info": "[SOLVER] True (with deterministic_solve=True): also pin "
                    "routing topology via arc-level tiebreaker (heavier solve)"
        },
        "supervia" : {
            "value": [],
            "info": "[TECH][CFET] List of layers that allow to use supervia. "
                    "CFET reads this list to build per-layer supervia toggles "
                    "consumed by mar_rules_*/via_induce_* (a layer absent from "
                    "the list defaults to no-supervia, i.e. the current "
                    "behavior)."
        },
        "via_c2c_rule": {
            "value": {
                # "PC, M0" : 42,
                "M0, M1" : 45,
                "M1, M2" : 45
            },
            "info": "[TECH] Center-to-center via distance between layers"
        },
        "mar_c2c_rule": {
            "value": {
                "M0" : 20,
                "M1" : 140,
                "M2" : 90
            },
            "info": "[TECH] Center-to-center minimum area rule for metals" 
        },
        "eol_c2c_rule": {
            "value": {
                "M0" : 60,
                "M1" : 45,
                "M2" : 45
            },
            "info": "[TECH] Center-to-center end of line rule for metals" 
        },
        "passthrough_type": {
            "value": "All",
            "info": "[TECH][DH Only] Passthrough options"
        },
        "insert_num_db": {
            "value": 0,
            "info": "[TECH] Maximum number of diffusion breaks allowed to use"
        },
        "virtual_edge": {
            "value": [],
            "conn_style": "sdcolwise",
            "info": "[TECH][CFET Only] Allow virtual connection between layers (layer_1, layer_2)"
        },
        "MPO": {
            "value": 2,
            "info": "[PIN] Minimum Pin Opening at M1"
        },
        "m0_pin_separation": {
            "value": False,
            "info": "[PIN] Enforce M0 pin separation across different rows"
        },
        "m0_pin_extension": {
            "value": True,
            "vacancy_edges": 2,
            "info": "[PIN] Extend M0 pin to vacancy edges"
        },
        "seed": {
            "value": 32,
            "info": "[SOLVER] Random seed"
        },
        "use_objective_set": {
            "value": [],
            "info": "[SOLVER] Set of objectives to use. If not set, a default set will be used."
        },
        "model_preset": {
            "value": 2,
            "info": "[SOLVER] Model preset ID used for solving"
        },
        "num_search_workers": {
            "value": 8,
            "info": "[SOLVER] Number of cores to use for solving"
        },
        "use_strategy" : {
            "value": "PLACE",
            "info": "[SOLVER] Use decision strategy to speedup feasibility "
                    "discovery. Shared values: PLACE | ROUTE | ALL. "
                    "[CFET] also accepts VIA_FIRST, which triggers the "
                    "CFET-only via-first decision strategy (use_via_first_"
                    "strategy); ignored by non-CFET techs. Default PLACE keeps "
                    "current behavior."
        },
        "use_relative_gap": {
            "value": False,
            "perc": 0.01,
            "info": "[SOLVER] Allow percentage optimality gap"
        },
        "max_time": {
            "value": False,
            "time": 3600,
            "info": "[SOLVER] Maximimum solving time in seconds"
        },
        "routing_tolerance" : {
            "value": True,
            "tol": 0,
            "info": "[SPEEDUP] Allowable routing distance to go beyond window in CPP"
        },
        "use_break_symmetry_for_placement" : {
            "value": True,
            "info": "[SPEEDUP] Break symmetry during placement"
        },
        "close_in_low_degree_net": {
            "value": False,
            "info": "[SPEEDUP] Enforce 2-pin nets to be diffusion shared"
        },
        "use_placement_order_for_identical_transistors": {
            "value": False,
            "info": "[SPEEDUP] Enforce identical transistors to be placed in a certain order"
        },
        "fix_placement_across_pn": {
            "value": False,
            "info": "[SPEEDUP][FinFET] When True, additionally align the placement "
                    "order of matched PMOS/NMOS transistors across the P/N rows "
                    "(x_p == x_n) in _fix_placement_order_identical_transistors_. "
                    "Default False reproduces current behavior; the FinFET "
                    "orchestrator may read this instead of overloading "
                    "use_placement_order_for_identical_transistors."
        },
        "use_same_site_for_identical_transistors": {
            "value": False,
            "info": "[SPEEDUP][DH Only] Enforce identical transistors to be placed within the same site"
        },
        "use_close_in_and_same_site_for_identical_transistors": {
            "value": False,
            "info": "[SPEEDUP][DH Only] Encforce identical transistors to be diffusion shared and placed within the same site"
        },
        "use_balanced_site_assignment": {
            "value": False,
            "info": "[SPEEDUP][DH Only] Force equal distribution of identical transistors across sites"
        },
        "use_contiguous_placement_per_site": {
            "value": False,
            "info": "[SPEEDUP][DH Only] Force identical transistors within same site to be contiguous"
        },
        "inject_edge": {
            "value": {},
            "info": "[INJECT] Inject edge(s) to be used (u_layer, u_row, u_col, v_layer, v_row, v_col) : 0 or 1"
        },
        "inject_arc": {
            "value": {},
            "info": "[INJECT] Inject arc(s) to be used (net_name, u_layer, u_row, u_col, v_layer, v_row, v_col) : 0 or 1"
        },
        "inject_flow": {
            "value": {},
            "info": "[INJECT] Inject flow(s) to be used (net_name, k_idx, u_layer, u_row, u_col, v_layer, v_row, v_col) : 0 or 1"
        },
        "inject_placement": {
            "value": [],
            "info": "[INJECT] Inject placement(s) to be used (tran_name, x, y, flip)"
        },
        "inject_track": {
            "value": {},
            "info": "[INJECT] Inject a track to be used on a layer (layer, row/col_idx)"
        },
        "inject_cluster": {
            "value": False,
            "remove_2d_nets": False,
            "min_cluster_size": 2,
            "max_cluster_size": 2,
            "method": "kkhdb",
            "info": "[INJECT] Inject clusters automatically using the 'method' "
                    "loader (auto-clustering). Tunable via min/max_cluster_size."
        },
        "limit_m2_usage": {
            "value": True,
            "info": "[SPEEDUP] Limit each net to one M2 track and each M2 track to one net"
        }
    }
    # ^ (GLOBAL) CFET uses LIG/LISD for routing
    if tech == "CFET":
        CONFIG_TEMPLATE["lisd_routing"]["value"] = True
        CONFIG_TEMPLATE["lig_routing"]["value"] = True
            
    for cir in circuit_names:
        out_path = os.path.join(output_dir, "config", f"{cir}.json")
        if os.path.exists(out_path) and not force:
            # Only keep an existing file when it actually parses as JSON. A
            # corrupt/truncated file (e.g. from an earlier crash or Ctrl-C)
            # carries no user tuning to protect, so fall through and
            # regenerate it rather than leaving a broken config that makes a
            # later 'make spnr' fail far from the cause with a JSONDecodeError.
            try:
                with open(out_path) as _f:
                    json.load(_f)
            except (ValueError, OSError):
                print(f"config: existing {os.path.normpath(out_path)} is "
                      f"corrupt/unreadable; regenerating")
            else:
                print(f"config: keeping existing {os.path.normpath(out_path)} "
                      f"(already generated; pass --force to regenerate)")
                continue
        config_template = copy.deepcopy(CONFIG_TEMPLATE)
        # Apply preset-level overrides from .mk CONFIG_OVERRIDES before any
        # per-cell heuristics so cell-specific tweaks below can still win
        # for special-case cells (e.g. DFFRPQ insert_num_db bump).
        _apply_overrides(config_template, overrides)
        # ^ (Heuristic) How many diffusion breaks to insert?
        if "DFFRPQ" in cir:
            if track == 4 and tech == "FinFET" and height_config == "SH":
                config_template["insert_num_db"]["value"] = 2
            elif track == 4 and tech == "FinFET" and (height_config == "PNNP" or height_config == "NPPN"):
                config_template["insert_num_db"]["value"] = 1
            elif tech == "CFET":
                config_template["insert_num_db"]["value"] = 2
        elif "SDFFQ" in cir:
            if track == 4 and tech == "FinFET" and height_config == "SH":
                config_template["insert_num_db"]["value"] = 4
            elif track == 4 and tech == "FinFET" and (height_config == "PNNP" or height_config == "NPPN"):
                config_template["insert_num_db"]["value"] = 2
            elif tech == "CFET":
                config_template["insert_num_db"]["value"] = 4
        elif "SDFFSQ" in cir:
            if track == 4 and tech == "FinFET" and height_config == "SH":
                config_template["insert_num_db"]["value"] = 4
            elif track == 4 and tech == "FinFET" and (height_config == "PNNP" or height_config == "NPPN"):
                config_template["insert_num_db"]["value"] = 2
            elif tech == "CFET":
                config_template["insert_num_db"]["value"] = 4
        elif "DFF" in cir:
            config_template["insert_num_db"]["value"] = 1
        elif "LHQ" in cir or "LAT" in cir:
            if track == 4 and tech == "FinFET" and height_config == "SH":
                config_template["insert_num_db"]["value"] = 1
            elif track == 4 and tech == "FinFET" and (height_config == "PNNP" or height_config == "NPPN"):
                config_template["insert_num_db"]["value"] = 1
            elif tech == "CFET":
                config_template["insert_num_db"]["value"] = 1
        elif "PREICG_D1" in cir:
            if track == 4 and tech == "FinFET" and height_config == "SH":
                config_template["insert_num_db"]["value"] = 2 # 4T
            elif track == 4 and tech == "FinFET" and (height_config == "PNNP" or height_config == "NPPN"):
                config_template["insert_num_db"]["value"] = 2
            elif tech == "CFET":
                config_template["insert_num_db"]["value"] = 2
        elif "PREICG_D4" in cir:
            if track == 4 and tech == "FinFET" and height_config == "SH":
                config_template["insert_num_db"]["value"] = 4 # 4T
            elif track == 4 and tech == "FinFET" and (height_config == "PNNP" or height_config == "NPPN"):
                config_template["insert_num_db"]["value"] = 2
            elif tech == "CFET":
                config_template["insert_num_db"]["value"] = 4
        elif "CGEN" in cir:
            config_template["insert_num_db"]["value"] = 1
        elif "MXIT" in cir or "MXT" in cir:
            if track == 4 and tech == "FinFET" and height_config == "SH":
                config_template["insert_num_db"]["value"] = 2 # 4T
            elif track == 4 and tech == "FinFET" and (height_config == "PNNP" or height_config == "NPPN"):
                config_template["insert_num_db"]["value"] = 3
            elif tech == "CFET":
                config_template["insert_num_db"]["value"] = 2
        elif "XOR" in cir or "XNOR" in cir:
            config_template["insert_num_db"]["value"] = 1
        elif "AO" in cir or "OA" in cir:
            if track == 4 and tech == "FinFET" and height_config == "SH":
                config_template["insert_num_db"]["value"] = 1 # 4T
            elif track == 4 and tech == "FinFET" and (height_config == "PNNP" or height_config == "NPPN"):
                config_template["insert_num_db"]["value"] = 2
            elif tech == "CFET":
                config_template["insert_num_db"]["value"] = 1
        elif "DLY4" in cir:
            if track == 4 and tech == "FinFET" and (height_config == "SH" or height_config == "PNNP" or height_config == "NPPN"):
                config_template["insert_num_db"]["value"] = 1
            elif tech == "CFET":
                config_template["insert_num_db"]["value"] = 1
        else:
            config_template["insert_num_db"]["value"] = 1
        # ^ (Heuristic) 2-pin nets always diffusion shared?
        if "AO21A1AI2" in cir or "AO" in cir or "OA" in cir \
            or "DFF" in cir or "MXT" in cir or "MXIT" in cir or "LAT" in cir:
            config_template["close_in_low_degree_net"]["value"] = False
        # ^ (Design Rules) Change based on your pitch and track
        if track == 4:
            config_template["mar_c2c_rule"]["value"]["M1"] = 140
            config_template["eol_c2c_rule"]["value"]["M1"] = 45

        # ^ (CFET Special) Allow Gate and S/D Routing
        if tech == "CFET":
            config_template["lisd_routing"]["value"] = True
            config_template["lig_routing"]["value"] = True
            # config_template["eol_c2c_rule"]["value"]["BM0"] = 20
            # config_template["mar_c2c_rule"]["value"]["BM0"] = 40
            # config_template["mar_c2c_rule"]["value"]["M1"] = 0
        # ^ Large Drive Strength Cell (Relative Gap)
        if "_D8" in cir or "_D10" in cir or "_D12" in cir or "_D16" in cir or "_X4" in cir or "_X8" in cir or "_X12" in cir or "_X16" in cir:
            config_template["close_in_low_degree_net"]["value"] = True
            config_template["use_placement_order_for_identical_transistors"]["value"] = True
            config_template["use_balanced_site_assignment"]["value"] = True
            config_template["use_contiguous_placement_per_site"]["value"] = True
            # config_template["use_relative_gap"]["value"] = True
            # config_template["use_relative_gap"]["perc"] = 0.01
        # ^ (Timeout) 50 hour cap
        # if "DFF" in cir:
        #     config_template["max_time"]["time"] = 180000
            # config_template["use_relative_gap"]["value"] = True
            # config_template["use_relative_gap"]["perc"] = 0.01
        if "DFFRNQ" in cir:
            config_template["insert_num_db"]["value"] = 2
        # ^ Writeout
        with open(out_path, "w") as f:
            json.dump(config_template, f, ensure_ascii=False, indent=4)

def read(config_file):
    """
    Read the config file and return a dict.

    Raises a clear FileNotFoundError pointing the user at `make config` when
    the file isn't there. (A bare construction that never raised left callers
    with `None`, crashing later with a confusing `TypeError: 'NoneType' object
    is not subscriptable`.)
    """
    if not os.path.isfile(config_file):
        raise FileNotFoundError(
            f"Cell config not found: {config_file}\n"
            f"  -> Did you run 'make config' first? It generates the per-cell\n"
            f"     JSONs under [OUT_DIR]/config/. Typical fix:\n"
            f"         make config CONFIG=[your-preset]\n"
            f"     Then re-run 'make spnr' (or whatever invoked this)."
        )
    try:
        with open(config_file, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Invalid JSON in {config_file}: {e.msg}", e.doc, e.pos,
        ) from e

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cell_names",
        default=[],
        nargs="*",
        type=str,
        help="List of cell names. An empty list (e.g. a preset with no cells "
             "selected) is a no-op, matching `make spnr`'s empty-list "
             "semantics rather than crashing.",
    )
    parser.add_argument(
        "--track",
        default=4,
        type=int,
        help="Number of horizontal routing tracks"
    )
    parser.add_argument(
        "--tech",
        default="FinFET",
        type=str,
        help="Technology to use: FinFET, CFET.",
    )
    parser.add_argument(
        "--height_config",
        default="SH",
        type=str,
        help="Height configuration for the technology: SH (Single Height), PNNP/NPPN (Double Height).",
    )
    parser.add_argument(
        "--output_dir",
        default="./output/",
        type=str,
        help="Output directory for the generated files.",
    )
    parser.add_argument(
        "--override",
        action="append",
        default=[],
        metavar="KEY[.SUB...]=VALUE",
        help="Override a CONFIG_TEMPLATE entry. Plain form `KEY=VAL` sets "
             "template[KEY].value; dotted form `KEY.SUB=VAL` targets a "
             "nested field (e.g. `max_time.time=2000` sets the seconds "
             "without flipping the .value toggle). May be passed multiple "
             "times. Value type auto-detected (int/float/bool/null/string). "
             "Sourced from .mk preset's CONFIG_OVERRIDES variable.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate config JSONs even if they already exist (default: "
             "existing per-cell config JSONs are kept, never overwritten).",
    )
    args = parser.parse_args()
    overrides = _parse_overrides(args.override)
    generate_config(
        track=args.track, tech=args.tech, height_config=args.height_config,
        circuit_names=args.cell_names, output_dir=args.output_dir,
        overrides=overrides, force=args.force,
    )

