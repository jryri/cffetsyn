# SMTCell 2.0 Technical Guide

_Author: Yucheng Wang_


## Contents

- [SMTCell Overview](#smtcell-overview)
  - [Toolkit Compatibility](#toolkit-compatibility)
  - [Workflow](#workflow)
  - [File Structure](#file-structure)
  - [Computation Resources & Software Requirements](#computation-resources--software-requirements)
- [Setup & Run SMTCell](#setup--run-smtcell)
  - [Working with the Makefile](#working-with-the-makefile)
  - [SMTCell Commands](#smtcell-commands)
- [Configure, Customize & Accelerate Cell Designs](#configure-customize--accelerate-cell-designs)
  - [Layer Configuration File](#layer-configuration-file)
  - [Cell Configuration File](#cell-configuration-file)

---

## SMTCell Overview

SMTCell 2.0 is a cell layout generation platform developed by the VLSI Lab (Prof. Chung-Kuan Cheng) at the University of California, San Diego, designed for DTCO/STCO exploration. Its primary objective is to facilitate technology-branch exploration for FinFET, CFET, and QFET through intuitive design rule encoding using Constraint Programming. The platform supports gear-ratio and pitch exploration when a matching layer file exists. This document specifically details a specialized version of SMTCell (Ver. 2.0) optimized for PROBE3 technology.

The toolkit ships three technology branches — **FinFET**, **CFET**, and **QFET** — each selected through a Makefile preset (see [Setup & Run SMTCell](#setup--run-smtcell)). **`QFET` is a placeholder name for 3D FinFET**: wherever `QFET` appears (the presets, the `TECH` value, and `src/cellgen/archit/QFET/`), it refers to the 3D FinFET technology — not a separate device family. The compatibility matrix below documents the FinFET, CFET, and QFET branches.

### Toolkit Compatibility

SMTCell 2.0 offers a wide range of customization options across FinFET, CFET, and QFET technology. Below is a quick summary on each option.

| | FinFET | CFET | QFET |
|---|:---:|:---:|:---:|
| Supported RT | 4 | 3, 4 | 4 |
| Power Rail Style | In-bound (M0BPR) | In-bound M0ICPD (3T), M0BPR (4T) | In-bound (M0BPR) |
| Height Options | SH (shipped) | SH (shipped) | SH (shipped) |
| Gear Ratio | ✓ | ✓ | ✓ |
| Offset | ✓ | ✓ | ✓ |
| Min Area Rule | ✓ | ✓ | ✓ |
| End-of-Line Rule | ✓ | ✓ | ✓ |
| Via C2C Separation Rule | ✓ | ✓ | ✓ |
| Min Gate Cut Length | ✓ | ✓ | ✓ |
| Boundary Condition | ✓ | ✓ | ✓ |
| Backside Routing | – | ✓ | – |
| Super Via | ✓ | ✓ | ✓ |
| Gate Routing | ✓ | ✓ | ✓ |
| CA Routing | ✓ | ✓ | ✓ |
| Gate Passthrough | ✓ | – | ✓ |
| Source-Drain Passthrough | ✓ | – | ✓ |

> **QFET** is a placeholder name for 3D FinFET; its support currently mirrors FinFET, and the shipped preset is SH / 4-track. Backside routing for QFET is not verified.

### Workflow

At a high level, SMTCell is constraint-encoding software that allows users to interact with a CP-SAT solver without building the entire constraint stack from scratch. To enable this, several code elements are integrated into SMTCell to ensure intuitiveness and extensibility.

First, SMTCell requires a global layer definition to establish its standard-cell canvas. This is enabled by the layer file (a JSON file), which is expected to be globally uniform and technology-specific — meaning you cannot generate one standard-cell layout with four routing tracks and another with only three.

Second, SMTCell generates a cell-specific `.json` configuration file. This file contains settings for generating each cell. Since each cell can benefit from different SMTCell speedup techniques and designer-specified heuristics, the `.json` file is customized per cell and should be tuned before generation.

Third, once `.json` files are ready, SMTCell can be executed in `spnr` mode to enable simultaneous place-and-route. This step builds constraints based on the layer and `.json` files, while the CP-SAT solver handles the solving process. Once converged, users may proceed to the fourth step.

Fourth, if SMTCell reports `INFEASIBLE` in the third step, the issue could be:

1. An impossible design specification for the given netlist
2. An over-constraining heuristic
3. A software bug (worst-case scenario)

The best practice is to disable speedup parameters in the `.json` file and retry. If SMTCell reports `FEASIBLE` or `OPTIMAL` in the third step, a layout solution is obtained. You can then run the `gds` step to generate a `.gds` file.

Finally, if you encounter any glitches, unwanted metal usage, or you hope to debug or re-generate some layouts with heuristics, you may want to see what SMTCell sees in order to set up additional constraints. The visualization tools under `src/cellgen/postprocess/visualize_*` render a `.png` of the internal canvas with solved placement and routing.

### File Structure

All essential commands of SMTCell can be run using the Makefile. Below is the default file structure (abridged).

```text
SMTCellUCSD-2.0/
├── input/
│   ├── cdl/                        # input netlists (*.cdl)
│   ├── layer/                      # layer configuration files (*.json)
│   ├── mis/                        # KLayout layer-property files (*.lyp)
│   ├── presets/                    # Makefile presets (*.mk), selected via CONFIG=
│   ├── pin_input_collection.json   # recognised input-pin names
│   └── pin_output_collection.json  # recognised output-pin names
├── Makefile
├── output/
│   └── <LIBRARY_NAME>/
│       └── <HEIGHT>/               # SH, or verified preset-specific heights
│           ├── config/             # per-cell *.json
│           ├── constraint/         # *.log (only when FLAG_LOG_CONSTR=True)
│           ├── gds/                # *.gds
│           ├── logs/               # *.log
│           ├── result/             # *.res, *.var
│           └── view/               # *.png
├── doc/
└── src/
    ├── main.py                     # CLI entry point (python -m src.main)
    ├── cellgen/
    │   ├── main.py
    │   ├── archit/                 # per-technology orchestrators
    │   │   ├── config.py           # default cell-config template
    │   │   ├── CFET/               # main.py, tech.py, util.py
    │   │   ├── FinFET/             # main.py, tech.py, util.py
    │   │   └── QFET/               # main.py, tech.py, util.py
    │   ├── core/                   # constraint modeling: graph, placement,
    │   │   │                       #   routing, rule, pin, objective, inject,
    │   │   │                       #   accelerate, variable, entity
    │   ├── postprocess/            # GDS / LEF / visualization: gds_*_SH.py,
    │   │   │                       #   visualize_*
    │   └── solver/                 # CP-SAT wrapper (cpsat_wrapper.py)
    ├── utility/                    # helper scripts (genLEF.py, ...)
    └── gui/                        # optional PySide6 desktop GUI
```

**`./input/*`** contains the input netlists (`cdl/`), the layer configuration files (`layer/*.json`), the KLayout layer-property files (`mis/*.lyp`), and the Makefile presets (`presets/*.mk`). The `pin_input_collection.json` and `pin_output_collection.json` files are hard-coded JSON files that contain all possible input/output pin names. SMTCell needs a pin name collection to recognize IO nets from the given CDL file. Otherwise, it may incorrectly pick an internal net and allocate IO pins.

**`./Makefile`** loads a preset (selected with `CONFIG=`) and exposes all major function calls. This is the go-to file to select the codebase, technology branch, and cells you wish to execute.

**`./input/mis`** contains KLayout-friendly layer-property files. They are annotated with PROBE3 layer names and distinguishable colors. You can load them into KLayout to improve your GDS viewing experience.

**`./output/*`** contains the generated files from SMTCell. They are organized by your defined library name and then by your selected height configuration. The shipped presets use SH; legacy or preset-specific FinFET heights such as PNNP/NPPN should be verified with a matching preset and layer file before use. Inside each output subfolder you may find: `config/` (cell-specific configuration files), `constraint/` (a viewable SMTCell constraint stack for debugging, produced only when `FLAG_LOG_CONSTR=True`), `gds/` (the generated GDS file), `logs/` (the log file generated during execution, used for reviewing the solving process), `result/` (a result `.res` file holding the solved placement and routing, and a variable `.var` file recording all variable results for debugging), and `view/` (debug `.png` renders).

**`./src/*`** contains the SMTCell 2.0 codebase, which follows Object-Oriented Programming (OOP) practices. The CLI entry point is `src/main.py` (`python -m src.main`). The bulk of the code lives under `src/cellgen/`, organized by function: `archit/` (per-technology orchestrators for CFET, FinFET, and QFET, plus the default cell-config template in `config.py`), `core/` (constraint modeling), `postprocess/` (GDS generation, LEF, and canvas visualization), and `solver/` (the CP-SAT wrapper). Helper scripts live under `src/utility/`, and the optional desktop GUI under `src/gui/`.

### Computation Resources & Software Requirements

SMTCell is built on Python 3 and is designed to run on Linux-based operating systems. The platform supports multi-core solving, with a recommendation of allocating at least 4 CPU cores for optimal layout generation speed. Below is a list of key packages utilized within SMTCell:

| Package | Version | Purpose |
|---|---|---|
| `loguru` | ≥ 0.7.2 | print log messages |
| `numpy` | ≥ 2.2.6 | data-related |
| `networkx` | ≥ 3.4.2 | graph-related |
| `scikit-learn` | ≥ 1.7.0 | cluster-related |
| `ortools` | ≥ 9.14.6206 | solver |
| `matplotlib` | ≥ 3.10.3 | plot canvas |
| `klayout` | ≥ 0.30.2 | GDS generation |

Other versions of the package may work but are not rigorously tested.

---

## Setup & Run SMTCell

SMTCell is driven through the Makefile, which loads a **preset** from `input/presets/<name>.mk` selected with `CONFIG=`. A preset fixes the technology, height, track count, pitches, and netlist for one flow. A complete flow is three commands:

```bash
make config CONFIG=FinFET_4T_SH
make spnr   CONFIG=FinFET_4T_SH
make gds    CONFIG=FinFET_4T_SH
```

Before running, inspect the available presets and the resolved settings:

```bash
make list-configs                       # list available presets
make show-config CONFIG=FinFET_4T_SH    # print the resolved configuration
```

Bundled presets include `FinFET_4T_SH`, `CFET_3T_SH`, `CFET_4T_SH`, and `QFET_4T_SH`.

### Working with the Makefile

A preset (`input/presets/<name>.mk`) defines the following variables:

- **TECH** is the technology branch to run — `FinFET`, `CFET`, or `QFET` (a placeholder name for 3D FinFET).
- **HEIGHT_CONFIG** is the architecture to run. The shipped FinFET, CFET, and QFET presets use SH. FinFET PNNP/NPPN and DH-related advanced options are legacy or preset-specific paths; verify the matching preset and layer file before use.
- **TRACK** is the number of horizontal top-view M0 routing tracks (in a single site). CFET supports 3 and 4; FinFET and QFET support 4.
- **CPP, M1P, M1OF** stand for the contacted poly pitch, M1 pitch, and M1 offset in PROBE3 technology. These parameters select the correct `LAYER_FILE` (`input/layer/*.json`). You cannot change them freely unless a corresponding layer file exists.
- **CDL_FILE** is the input netlist. SMTCell searches through this netlist file and grabs the correct cell netlist for input. Be sure that your naming format is consistent.
- **CELL_NAME** is a list of cell names to run. These names are searched automatically in your netlist. If you define a long list, end each line with a `\` for Makefile line-continuation.
- **CELL_PREFIX** (default `PROBE3`) and **FLAG_LOG_CONSTR** (default `False`) are derived defaults. `FLAG_LOG_CONSTR` outputs a constraint file (under `./output/*/constraint/*.log`) for each cell to help check the encoded constraints. **This is only for advanced users and can easily generate >500 MB files that blow up storage.** Be sure to keep it off when not in use.

For `CONFIG=CFET_3T_SH`, `TRACK=3` selects CFET M0ICPD in-cell power routing. Run it with the same Makefile stages:

```bash
make config CONFIG=CFET_3T_SH
make spnr   CONFIG=CFET_3T_SH
make gds    CONFIG=CFET_3T_SH
```

For example, to generate the cell `AND2_X1` whose netlist entry is `.SUBCKT PROBE3_AND2_X1 A B VDD VSS Y`, a preset sets `CELL_PREFIX=PROBE3` and `CELL_NAME=AND2_X1`; the library name is assembled automatically from `CELL_PREFIX`, `TECH`, `CHANNEL`, `TRACK`, and the pitch parameters.

### SMTCell Commands

Pass the same `CONFIG=<preset>` to every stage. Here is what each command does:

- **`make config`**
  - Always the first command for a new generation flow. It generates the per-cell config files under `./output/*/config/*.json`. This command is **idempotent**: a per-cell config that already exists is **kept** (so any tuned settings survive), and you must pass `FORCE=1` to regenerate all of them from the preset. Under the same library and architecture, if you want multiple results with different parameters (e.g., a new T2T rule), back up your generation separately in a different folder to avoid mixing up generations.
- **`make spnr`**
  - Runs the core algorithm for SMTCell, invoking the CP-SAT solver and writing a result file under `./output/*/result/*.res`. **The `.res` is overwritten on a successful generation.** If you plan to save a result, make a backup elsewhere.
- **`make gds`**
  - Reads the result file and generates the GDS file. It is a standalone process that does not involve any solving.
- **`make lef`**
  - Generates a `.lef` abstract from the GDS.
- **`make status`**
  - Prints the per-cell solver status and runtime parsed from the logs.

---

## Configure, Customize & Accelerate Cell Designs

SMTCell offers users a wide range of options to customize and accelerate individual cell designs. Since each netlist has a unique topology, different netlists may require distinct design preferences and acceleration strategies. This section provides an overview of the configuration settings within SMTCell. To run SMTCell, users need to prepare two configuration files: a layer configuration file and a cell configuration file.

The layer configuration file defines the standard-cell canvas for layout generation and is applied universally across the same technology and architecture.

The cell configuration file is specific to each cell. For instance, 100 cell configuration files are required to generate 100 cells. However, SMTCell can automate the generation of these configuration files, allowing users to easily customize them later. `src/cellgen/archit/config.py` governs the default configuration template. If you hope to turn settings on/off by default, this Python file can be edited to fit your need.

### Layer Configuration File

The layer configuration is a JSON file that details the layer information for the standard-cell canvas. The structure of this file is as follows:

```jsonc
"PC" : {
        "layer_type"   : "metal",
        "layer_number" : 7,
        "layer_name"   : "PC",
        "direction"    : "V",
        "offset"       : 0.0,
        "pitch"        : 45.0,
        "width"        : 16.0,
        "gds_layer"    : 7,
        "gds_datatype" : 0,
        "info"         : "PC in layer"
},
"CA" : {
        "layer_type"   : "via",
        "layer_number" : 14,
        "layer_name"   : "CA",
        "upper_layer"  : "M0",
        "lower_layer"  : "PC",
        "gds_layer"    : 14,
        "gds_datatype" : 0,
        "info"         : "CA in layer"
}, ...
```

Each primary key leads to a dictionary of layer information. Each layer can either be a metal or a via (defined by `layer_type`).

- A metal layer defines `layer_type`, `layer_name`, `direction` ("V" for vertical or "H" for horizontal), `offset` (from origin 0,0), `pitch`, and `width`.
- A via layer defines `layer_type`, `layer_name`, `upper_layer`, and `lower_layer` (by name).
- `layer_number`, `gds_layer`, and `gds_datatype` carry the GDS stream numbers used during GDS generation. `info` is optional and serves as a comment field.

SMTCell expects the layers to be defined from the bottom to the top. Between each pair of metal layers, there should be a via layer which connects them. **Please be advised that some critical layers are embedded in the SMTCell functions.** For example, "PC" is a special layer that does not perform any routing rule checking, and "M2" is a special layer that is highly constrained due to pin access rules. It is still possible to change layer names, but the required modification is to update these layer names in SMTCell core functions.

For CFET 3T M0ICPD (`CONFIG=CFET_3T_SH`), CFET SH supports only 3 or 4 routing tracks. The GDS and solver fine-row canvas uses `TRACK * 2 = 6` rows. Row 0 is the VSS M0 in-cell power row, rows 1-4 are signal rows, and row 5 is the VDD M0 in-cell power row. PMOS and NMOS pin access both use signal rows `[1, 2, 3, 4]`. PC/BPC are layers, not rows; rows are top-view M0 routing tracks. The 3T M0ICPD preset has no BPR. CFET 4T remains the M0BPR preset.

### Cell Configuration File

The cell configuration file is a JSON file that details the cell parameters for the standard-cell canvas. Each entry has a `value` (and sometimes auxiliary fields) plus an `info` string tagged with its class. The classes are: Technology (`[TECH]`), cost (`[COST]`), GDS (`[GDS]`), routing (`[ROUTING]`), pin (`[PIN]`), CP-SAT solver (`[SOLVER]`), speedup (`[SPEEDUP]`), and constraint injection (`[INJECT]`). The default template lives in `src/cellgen/archit/config.py`; the tables below list the current parameters and defaults.

> Note: `config.py` also applies per-cell heuristics on top of these defaults — for example, `insert_num_db` is auto-tuned for flip-flop, latch, AOI/OAI, and mux families, and several design-rule values change with `TRACK`. The values below are the template defaults before those heuristics.

#### TECH — technology and design rules

| Parameter | Default | Description |
|---|---|---|
| `minimum_gate_cut_length` | `2` | Minimum length each gate cut must occupy, in #CPP. A diffusion break is a legal cut; cuts are allowed on the cell boundary. |
| `lisd_routing` | `false` (`true` for CFET) | Use LISD (CA) as a routing resource (e.g., CFET frontside/backside CA signal propagation). |
| `lig_routing` | `false` (`true` for CFET) | Use LIG (PC) as a routing resource (e.g., CFET frontside/backside PC signal propagation). |
| `supervia` | `[]` | *(CFET)* List of layers permitted to form a super via (stacked vias, no intermediate metal shape). Not well tested. |
| `via_c2c_rule` | `{M0,M1: 45, M1,M2: 45}` | Center-to-center via distance between adjacent layers. |
| `mar_c2c_rule` | `{M0: 20, M1: 140, M2: 90}` | Center-to-center minimum-area rule per metal layer. |
| `eol_c2c_rule` | `{M0: 60, M1: 45, M2: 45}` | Center-to-center end-of-line rule per metal layer. |
| `passthrough_type` | `"All"` | *(DH only)* Passthrough mode: `All`, `None`, `G`, or `SD`. |
| `insert_num_db` | `0` | Number of diffusion breaks to insert (enlarges the canvas by #CPP). Auto-tuned per cell. |
| `polygon_cell` | `false` | Encourage DH to make polygon cells. |
| `virtual_edge` | `[]` | *(CFET only)* Allow a virtual connection between two layers `(layer_1, layer_2)`. |

#### COST / GDS / ROUTING

| Parameter | Default | Description |
|---|---|---|
| `metal_cost` | `1` | *(COST)* Per-edge cost for same-layer wire edges. |
| `via_cost` | `3` | *(COST)* Per-edge cost for adjacent-layer via edges. |
| `virtual_edge_cost` | `5` | *(COST)* Per-edge cost for non-adjacent virtual jump edges (VL). |
| `append_gds` | `true` | *(GDS)* `true`: append the cell into the library-wide `gds/<lib>.gds`; `false`: emit a standalone `gds/<cell>.gds`. |
| `enable_routing` | `true` | *(ROUTING)* `false`: placement-only solve, no routing constraints. |
| `routing_stage` | `"external"` | *(ROUTING)* `internal` or `external`. `external` forces an IO-pin SON tree (required for any cell with IO pins). |
| `enable_via_induce` | `false` | *(ROUTING)* Enforce via-induced metal extension. |

#### PIN

| Parameter | Default | Description |
|---|---|---|
| `MPO` | `2` | Minimum pin opening at M1; preset-specific, so check the selected preset and generated per-cell config. 3T/4T examples are defined by their generated per-cell configs. Too high a value may yield `INFEASIBLE` or a larger #CPP cell. |
| `m0_pin_separation` | `false` | Enforce M0 pin separation across different rows. |
| `m0_pin_extension` | `true` (`vacancy_edges: 2`) | Extend M0 pins to vacancy edges. |

#### SOLVER — CP-SAT solver settings

| Parameter | Default | Description |
|---|---|---|
| `seed` | `32` | Random seed; with a fixed seed the result should be deterministic. |
| `use_objective_set` | `[]` | Set of objectives to use; a default set is used when empty. |
| `model_preset` | `2` | Predefined solver hyperparameter set. Preset `2` prohibits LP solvers since SMTCell is predominantly Boolean. Should not be touched unless rigorously tested. |
| `num_search_workers` | `8` | Number of cores to use for solving. Values beyond ~10 may slow runtime due to cross-core synchronization. |
| `use_strategy` | `"PLACE"` | Decision-strategy emphasis: `PLACE`, `ROUTE`, or `ALL` (CFET also accepts `VIA_FIRST`). `PLACE` is recommended for faster convergence. |
| `use_relative_gap` | `false` (`perc: 0.01`) | Stop early once the optimality gap reaches `perc` (set `value=true` to enable). ~1% for combinational cells, 4–6% for large DFFs. |
| `max_time` | `false` (`time: 3600`) | Maximum solve time in seconds (set `value=true` to enable). |
| `deterministic_solve` | `false` | Lex-ordered objective hierarchy for a unique minimizer when `num_search_workers > 1`. |
| `route_determinism` | `false` | With `deterministic_solve=true`, also pin the routing topology via an arc-level tiebreaker (heavier solve). |

#### SPEEDUP — acceleration heuristics

| Parameter | Default | Description |
|---|---|---|
| `routing_tolerance` | `true` (`tol: 0`) | Allowable routing distance beyond the window, in #CPP. |
| `use_break_symmetry_for_placement` | `true` | Break symmetry during placement. |
| `close_in_low_degree_net` | `false` | Enforce low-degree (e.g. 2-pin) nets to be diffusion shared. |
| `use_placement_order_for_identical_transistors` | `false` | Pre-determine a placement order for identical transistors (SH, high-drive cells). |
| `fix_placement_across_pn` | `false` | *(FinFET)* Also align the placement order of matched PMOS/NMOS transistors across the P/N rows. |
| `use_same_site_for_identical_transistors` | `false` | *(DH only)* Place identical transistors within the same site (row). |
| `use_close_in_and_same_site_for_identical_transistors` | `false` | *(DH only)* Diffusion-share identical transistors and place them within the same site. |
| `use_balanced_site_assignment` | `false` | *(DH only)* Force equal distribution of identical transistors across sites. |
| `use_contiguous_placement_per_site` | `false` | *(DH only)* Force identical transistors within a site to be contiguous. |
| `limit_m2_usage` | `true` | Limit each net to one M2 track and each M2 track to one net. |

*Two transistors are considered identical when they share the same source/gate/drain net.*

#### INJECT — manual constraint injection

| Parameter | Default | Description |
|---|---|---|
| `inject_edge` | `{}` | Inject edge(s): `(u_layer, u_row, u_col, v_layer, v_row, v_col) : 0 or 1`. |
| `inject_arc` | `{}` | Inject arc(s): `(net_name, u_layer, u_row, u_col, v_layer, v_row, v_col) : 0 or 1`. |
| `inject_flow` | `{}` | Inject flow(s): `(net_name, k_idx, u_layer, u_row, u_col, v_layer, v_row, v_col) : 0 or 1`. |
| `inject_placement` | `[]` | Inject placement(s): `(tran_name, x, y, flip)`. |
| `inject_track` | `{}` | Inject a track on a layer `(layer, row/col_idx)`. *Not implemented.* |
| `inject_cluster` | `false` | Auto-cluster transistors to accelerate large DFFs. Auxiliary fields: `remove_2d_nets`, `multi_level`, `post_merge`, `path_constraints`, `min_cluster_size`, `max_cluster_size`, `method` (`"kkhdb"`), `file`. Larger `max_cluster_size` gives more speedup but may turn the problem `INFEASIBLE`. |
