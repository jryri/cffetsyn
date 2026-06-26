<p align="center">
    <img src="/doc/figure/SMTCellLogo.png" width="600">
</p>

<h1 align="center">SMTCell 2.0</h1>

<p align="center">
    <em>A constraint-programming platform for standard-cell layout generation across FinFET, CFET, and QFET branches.</em>
</p>

<p align="center">
    <a href="https://github.com/ckchengucsd/SMTCellUCSD-2.0/network/dependencies" alt="Contributors">
        <img src="https://img.shields.io/github/contributors/ckchengucsd/SMTCellUCSD-2.0" /></a>
    <a href="https://github.com/ckchengucsd/SMTCellUCSD-2.0/network/pulse" alt="Activity">
        <img src="https://img.shields.io/github/commit-activity/m/ckchengucsd/SMTCellUCSD-2.0" /></a>
    <img src="https://img.shields.io/badge/python-3.x-blue" alt="Python" />
    <img src="https://img.shields.io/badge/solver-OR--Tools%20CP--SAT-orange" alt="Solver" />
    <img src="https://img.shields.io/badge/platform-Linux-lightgrey" alt="Platform" />
    <img src="https://img.shields.io/badge/license-BSD--3--Clause-green" alt="License" />
    <img src="https://img.shields.io/badge/status-beta-yellow" alt="Status" />
</p>

> [!WARNING]
> **Beta — under active, open development.** SMTCell 2.0 is research software that is still evolving. Interfaces, file formats, default parameters, and generated results may change between commits, and some features are experimental or only partially validated. Expect rough edges, pin releases if you need stability, and please [report any issues](https://github.com/ckchengucsd/SMTCellUCSD-2.0/issues) you run into.

---

## Overview

**SMTCell 2.0** is a cell layout generation platform developed by the VLSI Lab (Prof. Chung-Kuan Cheng's group) at the University of California, San Diego, designed for **DTCO** exploration. Its primary objective is to facilitate technology exploration for **FinFET** and **CFET** architectures through intuitive design-rule encoding using **Constraint Programming**.

At its core, SMTCell is constraint-encoding software that lets users interact with a **CP-SAT** solver without building the entire constraint stack from scratch. The platform offers flexible metal layer customization.

> [!NOTE]
> For a complete technical description, see the [SMTCell Technical Guide](doc/SMTCell2_GUIDE.md).

---

## Table of Contents

- [Toolkit Compatibility](#toolkit-compatibility)
- [Requirements](#requirements)
- [Installation](#installation)
- [Tool Setup](#tool-setup)
- [Workflow](#workflow)
- [Running SMTCell](#running-smtcell)
- [Graphical Interface](#graphical-interface)
- [Reporting Issues](#reporting-issues)
- [How to Cite](#how-to-cite)
- [References](#references)

---

## Toolkit Compatibility

SMTCell 2.0 offers a wide range of customization options across FinFET, CFET, and QFET technology. The table below summarizes support for each option.

| Feature | FinFET | CFET | QFET |
|---|:---:|:---:|:---:|
| Supported RT | 4 | 3, 4 | 4 |
| Power Rail Style | In-bound (M0BPR) | In-bound M0ICPD (3T), M0BPR (4T) | In-bound (M0BPR) |
| Height Options | SH (shipped) | SH (shipped) | SH (shipped) |
| Gear Ratio | ✅ | ✅ | ✅ |
| Offset | ✅ | ✅ | ✅ |
| Min Area Rule | ✅ | ✅ | ✅ |
| End-of-Line Rule | ✅ | ✅ | ✅ |
| Via C2C Separation Rule | ✅ | ✅ | ✅ |
| Min Gate Cut Length | ✅ | ✅ | ✅ |
| Boundary Condition | ✅ | ✅ | ✅ |
| Backside Routing | — | ✅ | — |
| Super Via | ✅ | ✅ | ✅ |
| LIG Routing | ✅ | ✅ | ✅ |
| LISD Routing | ✅ | ✅ | ✅ |
| Gate Passthrough | ✅ | — | ✅ |
| Source-Drain Passthrough | ✅ | — | ✅ |

> **QFET** is a placeholder name for 3D FinFET; its support currently mirrors FinFET, and the shipped preset is SH / 4-track. Backside routing for QFET is not verified.

---

## Requirements

SMTCell is built on **Python 3** and is designed to run on **Linux**-based operating systems. The platform supports multi-core solving; allocating **at least 4 CPU cores** is recommended for optimal layout-generation speed.

| Package | Version | Purpose |
|---|---|---|
| `loguru` | ≥ 0.7.2 | Log messages |
| `numpy` | ≥ 2.2.6 | Data handling |
| `networkx` | ≥ 3.4.2 | Graph operations |
| `scikit-learn` | ≥ 1.7.0 | Clustering |
| `ortools` | ≥ 9.14.6206 | CP-SAT solver |
| `matplotlib` | ≥ 3.10.3 | Canvas plotting |
| `klayout` | ≥ 0.30.2 | GDS generation |

> Other versions of these packages may work but are not rigorously tested.

### Optional Tools (Recommended)

- [KLayout](https://www.klayout.de/) — for viewing `.gds`/`.lef` layouts. KLayout-friendly tech files (`.lyp`) annotated with PROBE3 layer names ship under `input/mis/`.
- [PROBE3.0](https://github.com/ABKGroup/PROBE3.0/) — for custom PDK generation.

---

## Installation

```bash
# (optional) create and activate a Python virtual environment
python3 -m venv smtcell
source smtcell/bin/activate

# install the required packages
pip install "loguru>=0.7.2" "numpy>=2.2.6" "networkx>=3.4.2" \
            "scikit-learn>=1.7.0" "ortools>=9.14.6206" \
            "matplotlib>=3.10.3" "klayout>=0.30.2"
```

You are now ready to generate your first cell.

---

## Tool Setup

SMTCell shells out to external EDA tools — KLayout, the GDS/GDT converters, and the Cadence/Synopsys sign-off suite — whose install paths are machine-specific. The `Makefile` does **not** hardcode them; you supply your own locations without editing any tracked file.

Set them in an untracked `config.mk` (auto-included by the `Makefile`):

```bash
cp config.mk.example config.mk
$EDITOR config.mk          # set the paths you actually have installed
```

…or override per-invocation / via the environment (highest precedence first):

```bash
make spnr CONFIG=FinFET_4T_SH QUANTUS=/opt/quantus/bin/quantus   # command line
export KLAYOUT=/path/to/klayout                                  # environment
```

Any variable left unset falls back to the **bare command name**, resolved on your `PATH` (so `module load` / vendor setup scripts just work). For a single vendor root, set `TOOLS_DIR` and every default resolves under it:

```makefile
# config.mk
TOOLS_DIR = /home/you/eda/bin
```

Configurable variables: `KLAYOUT`, `GDT2GDS`, `GDS2GDT`, `PYTHON`, `PEGASUS`, `QUANTUS`, `QRCTECHGEN`, `LIBERATE`, `ICVALIDATOR`, `QUEUE` (optional cluster job prefix), and `TOOLS_DIR`. `config.mk` is gitignored — commit only `config.mk.example`.

---

## Workflow

SMTCell drives a CP-SAT solver through a four-stage flow. Several code elements are integrated to keep the process intuitive and extensible.

1. **Layer definition.** SMTCell establishes its standard-cell canvas from a global `.layer` file. This definition is expected to be globally uniform and technology-specific — one canvas cannot mix routing-track counts across cells.
2. **Cell configuration.** SMTCell generates a per-cell `.json` template. Because each cell benefits from different speedup techniques and designer-specified heuristics, this file is customized per cell and should be tuned before generation.
3. **Simultaneous place-and-route.** With the `.layer` and `.json` files ready, SMTCell runs in `spnr` mode to build the constraint stack while the CP-SAT solver handles solving. Once converged, the result is written out.
4. **GDS generation.** If the solver reports `FEASIBLE` or `OPTIMAL`, a layout solution exists and you may generate a `.gds` file.

If the solver reports **`INFEASIBLE`**, the cause is typically one of:

- An impossible design specification for the given netlist,
- An over-constraining heuristic, or
- A software bug (worst case).

> [!TIP]
> Best practice on `INFEASIBLE`: disable the speedup parameters in the `.json` file and retry.

For debugging glitches, unwanted metal usage, or heuristic regeneration, run SMTCell in **view** mode to render a `.png` of the internal canvas with solved placement and routing.

---

## Running SMTCell

All major commands run through the **`Makefile`**, which loads a **preset** (`input/presets/<name>.mk`) selected with `CONFIG=`. A preset fixes the technology, height, track count, pitches, and netlist for one flow. List the presets with `make list-configs`; print the resolved settings with `make show-config CONFIG=<preset>`.

Bundled presets: `FinFET_4T_SH`, `CFET_3T_SH`, `CFET_4T_SH`, `QFET_4T_SH`.

> [!NOTE]
> **`QFET` is a placeholder name for 3D FinFET.** Wherever `QFET` appears — the `QFET_*` presets and the `QFET` technology branch — it refers to 3D FinFET, not a separate device technology.

**Preset variables** (set inside `input/presets/<name>.mk`):

| Variable | Description |
|---|---|
| `TECH` | Technology branch — `FinFET`, `CFET`, or `QFET` (placeholder name for 3D FinFET). |
| `HEIGHT_CONFIG` | Cell height — shipped FinFET/CFET/QFET presets use `SH`. FinFET `PNNP`/`NPPN` are legacy or preset-specific paths; verify the matching preset and layer file before use. |
| `TRACK` | Horizontal top-view M0 routing tracks per site — CFET supports `3` and `4`; FinFET/QFET support `4`. |
| `CPP`, `M1P`, `M1OF` | Contacted poly pitch, M1 pitch, and M1 offset in PROBE3. These select the matching `LAYER_FILE` under `input/layer/*.json`. |
| `CDL_FILE` | Input netlist searched for the target cell(s). |
| `CELL_NAME` | One or more cells to generate. List several by ending each line with `\`. |
| `CELL_PREFIX` | Cell/library prefix (default `PROBE3`). |
| `FLAG_LOG_CONSTR` | Dump a human-readable constraint log per cell (default `False`). |

For `CONFIG=CFET_3T_SH`, `TRACK=3` selects the CFET M0ICPD in-cell power-rail model. CFET SH supports only 3 or 4 routing tracks. The solver/GDS canvas expands the 3T preset to `TRACK * 2 = 6` fine rows: row 0 is the VSS M0 in-cell power row, rows 1-4 are signal rows, and row 5 is the VDD M0 in-cell power row. PMOS and NMOS pin access both use signal rows `[1, 2, 3, 4]`. PC/BPC are layers, not rows; rows are top-view M0 routing tracks. The 3T M0ICPD preset has no BPR, while the 4T CFET preset remains M0BPR.

> [!WARNING]
> `FLAG_LOG_CONSTR` is for advanced users only — it can easily generate files larger than 500 MB. Keep it off when not in use.

### Commands

Pass the same `CONFIG=<preset>` to every stage:

| Command | Description |
|---|---|
| `make config CONFIG=<preset>` | Generate the per-cell `.json` configs under `output/<lib>/<height>/config/`. **Idempotent** — existing configs are kept; pass `FORCE=1` to regenerate them from the preset. |
| `make spnr CONFIG=<preset>` | Run the core solve (CP-SAT) and write the result (`.res`) under `.../result/`. A successful run overwrites the existing `.res`. |
| `make gds CONFIG=<preset>` | Read the result and emit the `.gds` (standalone; no solving). |
| `make lef CONFIG=<preset>` | Generate a `.lef` abstract from the GDS. |
| `make status CONFIG=<preset>` | Print per-cell solve status and runtime from the logs. |

```bash
make config CONFIG=FinFET_4T_SH
make spnr   CONFIG=FinFET_4T_SH
make gds    CONFIG=FinFET_4T_SH
```

For the CFET 3-track M0ICPD preset, use the same flow with `CONFIG=CFET_3T_SH`:

```bash
make config CONFIG=CFET_3T_SH
make spnr   CONFIG=CFET_3T_SH
make gds    CONFIG=CFET_3T_SH
```

> [!CAUTION]
> `make spnr` overwrites the existing `.res` on a successful run — back up results you want to keep, or generate into a separate output directory. (`make config` keeps an already-generated per-cell config unless you pass `FORCE=1`.)

---

## Graphical Interface

<p align="center">
    <img src="/doc/figure/SMTCellGUI.png" width="600">
</p>

An optional desktop GUI drives the whole flow from a single window — pick a preset, pick cells, and run `config → spnr → gds → lef` with a live log, per-cell solve status, and a layout preview.

```bash
./src/gui/run.sh            # or:  cd src/gui && python -m smtcell_gui
```

It requires **PySide6** (`pip install PySide6`). From the window you can:

- choose a `CONFIG` preset and the target cells (loaded automatically from the preset's netlist),
- launch each stage individually or **Run all** (which stops on the first failure),
- watch the CP-SAT solver stream live, with `OPTIMAL` / `INFEASIBLE` status and runtime per cell,
- view the generated layout PNG, and open the output directory or the GDS in KLayout.

> [!NOTE]
> The GUI is a thin convenience layer — every button maps to a `make` target you can also run by hand.

## Reporting Issues

If you encounter a problem, please open an issue on the [GitHub repository](https://github.com/ckchengucsd/SMTCellUCSD-2.0/issues).

---

## How to Cite

If you use **SMTCell 2.0** in your research, please cite the paper (and, optionally, this repository).

**Paper** (IEEE style):

> C.-K. Cheng, A. B. Kahng, B. Lin, Y. Wang, and D. Yoon, "An Extended Study of Gear-Ratio-Aware Standard Cell Layout Generation for DTCO Exploration," arXiv preprint arXiv:2603.13665, 2026.

**Software** (IEEE style):

> C.-K. Cheng and Y. Wang, "SMTCell 2.0," GitHub repository, 2026. [Online]. Available: https://github.com/ckchengucsd/SMTCellUCSD-2.0

<details>
<summary>BibTeX</summary>

```bibtex
@article{cheng2026smtcell,
  title   = {An Extended Study of Gear-Ratio-Aware Standard Cell Layout Generation for DTCO Exploration},
  author  = {Cheng, Chung-Kuan and Kahng, Andrew B. and Lin, Bill and Wang, Yucheng and Yoon, Dooseok},
  journal = {arXiv preprint arXiv:2603.13665},
  year    = {2026}
}

@misc{smtcell2,
  title        = {{SMTCell 2.0}},
  author       = {Cheng, Chung-Kuan and Wang, Yucheng},
  year         = {2026},
  howpublished = {\url{https://github.com/ckchengucsd/SMTCellUCSD-2.0}}
}
```

</details>

---

## References

In no particular order:

- Park, Dong Won — Dissertation: [Logical Reasoning Techniques for Physical Layout in Deep Nanometer Technologies](https://escholarship.org/content/qt9mv5653s/qt9mv5653s.pdf)
- Lee, Daeyeal — Dissertation: [Logical Reasoning Techniques for VLSI Applications](https://escholarship.org/content/qt7xp6p3h1/qt7xp6p3h1.pdf)
- Ho, Chia-Tung — Dissertation: [Novel Computer Aided Design (CAD) Methodology for Emerging Technologies to Fight the Stagnation of Moore's Law](https://escholarship.org/content/qt2ts172zd/qt2ts172zd.pdf)
- D. Park, I. Kang, Y. Kim, S. Gao, B. Lin, and C.K. Cheng, "ROAD: Routability Analysis and Diagnosis Framework Based on SAT Techniques," ACM/IEEE Int. Symp. on Physical Design, pp. 65–72, 2019. \[[Paper](https://dl.acm.org/doi/pdf/10.1145/3299902.3309752)\] \[[Slides](https://cseweb.ucsd.edu//~kuan/talk/placeroute18/routability.pdf)\]
- D. Park, D. Lee, I. Kang, S. Gao, B. Lin, C.K. Cheng, "SP&R: Simultaneous Placement and Routing Framework for Standard Cell Synthesis in Sub-7nm," IEEE Asia and South Pacific Design Automation, pp. 345–350, 2020. \[[Paper](https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=9045729)\] \[[Slides](https://www.aspdac.com/aspdac2020/archive/pdf/5C-3.pdf)\]
- C.K. Cheng, C. Ho, D. Lee, and D. Park, "A Routability-Driven Complimentary-FET (CFET) Standard Cell Synthesis Framework using SMT," ACM/IEEE Int. Conf. on Computer-Aided Design, pp. 1–8, 2020. \[[Paper](https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=9256570)\]
- D. Lee, C.T. Ho, I. Kang, S. Gao, B. Lin, and C.K. Cheng, "Many-Tier Vertical Gate-All-Around Nanowire FET Standard Cell Synthesis for Advanced Technology Nodes," IEEE Journal of Exploratory Solid-State Computational Devices and Circuits, 2021, Open Access. \[[Paper](https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=9454552)\]
- C.K. Cheng, C.T. Ho, D. Lee, and B. Lin, "Multi-row Complementary-FET (CFET) Standard Cell Synthesis Framework using Satisfiability Modulo Theories (SMT)," IEEE Journal of Exploratory Solid-State Computational Devices and Circuits, 2021, Open Access. \[[Paper](https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=9390403)\]
- S. Choi, J. Jung, A. B. Kahng, M. Kim, C.-H. Park, B. Pramanik, and D. Yoon, "PROBE3.0: A Systematic Framework for Design-Technology Pathfinding with Improved Design Enablement," IEEE Transactions on Computer-Aided Design of Integrated Circuits and Systems, 2023, Open Access. \[[Paper](https://ieeexplore.ieee.org/document/10322780)\]
- The PROBE3.0 Framework. \[[GitHub](https://github.com/ABKGroup/PROBE3.0)\]
