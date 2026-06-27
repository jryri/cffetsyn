---
name: cffet-synthesizer
description: Use when synthesizing, testing, debugging, or extending CFFET (dual-face Flip-FET) cells in SMTCell — CONFIG=CFFET_3T_SH, pin policy, dual M0ICPD, STV, or verifying SON pin distribution on M0/BM0.
---

# CFFET Synthesizer (SMTCell v1 Scope C)

## Overview

**CFFET** = two back-to-back CFET blocks with Z-axis symmetry, implemented as `class CFFET(CFET)` (not QFET).

| Item | Value |
|------|-------|
| Preset | `CONFIG=CFFET_3T_SH` |
| Layer JSON | `input/layer/PROBE3_CFFET_2F_3T_4530OF0.json` |
| CDL | `input/cdl/PROBE_2F4T.cdl` |
| Working dir | `SMTCellUCSD-2.0/` |

**Companion skill:** `cffet-layer-nomenclature` for naming (FBOTPC, STV, FM0=`M0` in JSON).

## LGG stack (metal indices 0→8)

```
BM1(0) BM0(1) BBOTPC(2) BTOPPC(3) FBOTPC(4) FTOPPC(5) M0(6) M1(7) M2(8)
```

- **STV**: sole inter-block stitch `BTOPPC ↔ FBOTPC`
- **FMIV / BMIV**: intra-block MIV per face
- **M0ICPD**: fine rows on **M0** (front) and **BM0** (back); row 0 = VSS, row 5 = VDD

## Pin policy (P6b) — verify this first

| Net type | SON count | Layers | Assignment |
|----------|-----------|--------|------------|
| **Input** | 1 | M0 **or** BM0 | Round-robin `front, back, front, …` over **CDL pin order** |
| **Output** | 2 | **both** M0 and BM0 | Dual-face (FOUT + BOUT conceptually) |
| Power | — | row 0/5 | Not FIN/BIN |

Config schema in per-cell JSON (`pin_face`):

```json
{
  "face_to_layer": {"front": "M0", "back": "BM0"},
  "input": {"assignment": "round_robin", "order": "cdl", "explicit": {}},
  "output": {"mode": "dual", "faces": ["front", "back"]}
}
```

**Examples (verified):**

| Cell | Inputs | Outputs |
|------|--------|---------|
| INV_X1 | `I` → M0 only | `ZN` → M0 + BM0 |
| NAND2_X1 | `A1`→M0, `A2`→BM0 | `ZN` → M0 + BM0 |

## Standard workflow

```bash
cd SMTCellUCSD-2.0

# 1. Synthesize (timeout 60–120s)
timeout 120 make CONFIG=CFFET_3T_SH CELL_NAME=INV_X1 spnr

# 2. Verify pin distribution (requires prior spnr)
timeout 60 make CONFIG=CFFET_3T_SH CELL_NAME=INV_X1 verify_pins

# 3. View + GDS (optional)
timeout 60 make CONFIG=CFFET_3T_SH CELL_NAME=INV_X1 viewcell gds
```

**Python venv:** use `SMTCellUCSD-2.0/smtcell/bin/python3` (Makefile `PYTHON`) or activate project venv before bare `python3`.

**CFET regression** (must not break):

```bash
timeout 60 make CONFIG=CFET_3T_SH CELL_NAME=INV_X1 spnr
```

## Pin audit tool

Module: `src/cellgen/archit/CFFET/pin_audit.py`

Parses active `net_isSON_*` vars from `.var` and checks policy. Layer index in var name maps via layer JSON (L6=M0, L1=BM0).

```bash
python -m src.cellgen.archit.CFFET.pin_audit \
  --var output/.../INV_X1.var \
  --res output/.../INV_X1.res \
  --config output/.../config/INV_X1.json \
  --layer input/layer/PROBE3_CFFET_2F_3T_4530OF0.json
```

## Key source files

| Path | Role |
|------|------|
| `src/cellgen/archit/CFFET/main.py` | Orchestrator: z_var, STV, merge, SON |
| `src/cellgen/archit/CFFET/tech.py` | 4-tier + dual rail tech queries |
| `src/cellgen/archit/CFFET/util.py` | `.res` writer (Z column + via tags) |
| `src/cellgen/archit/CFFET/pin_audit.py` | Pin policy verification |
| `src/cellgen/archit/config.py` | `pin_face` template for CFFET |
| `src/cellgen/postprocess/visualize_CFFET_4T.py` | 4-tier PNG view |
| `src/cellgen/postprocess/gds_CFFET_SH.py` | Dual M0ICPD GDS |

## Placement (P3b)

- Each transistor has `z_var` ∈ {BBOTPC, BTOPPC, FBOTPC, FTOPPC}
- PMOS → {BTOPPC, FTOPPC}; NMOS → {BBOTPC, FBOTPC} (P_on_N)
- Pin candidates coupled to `z_var` (pins never straddle the seam)

## Known limits (v1)

- **AOI21_X1** @ 3T: INFEASIBLE (capacity, not timeout)
- GDS: routing + M0ICPD rails; full LISD/SDT geometry is simplified vs CFET
- Large cells (DFF, wide AOI): defer until small-cell pin audit passes

## Smoke test matrix (2026-06-27 batch, timeout 1000s)

| Cell | spnr | verify_pins | Input face (round-robin) |
|------|------|-------------|--------------------------|
| INV_X1 / INV_X2 | OPTIMAL | PASS | I→M0 |
| BUF_X1 | OPTIMAL | PASS | I→M0; Z dual M0+BM0 |
| AND2_X1 | OPTIMAL | PASS | A1→M0, A2→BM0 |
| NAND2_X1 / NOR2_X1 | OPTIMAL | PASS | A1→M0, A2→BM0 |
| NAND3_X1 | OPTIMAL | PASS | A1→M0, A2→BM0, A3→M0 |
| AOI21_X1 / OAI21_X1 | INFEASIBLE | — | 6T; 3 inputs, dual output capacity |
| MUX2_X1 / NAND2_X2 | INFEASIBLE (presolve) | — | `all_diff` UNSAT (multi-finger / 6T) |

```bash
timeout 1000 make CONFIG=CFFET_3T_SH CELL_NAME="INV_X1 NAND2_X1" spnr verify_pins
```

Defer: `DFF*`, `*_X4`, `*_X8`, wide AOI/OAI22.

## When NOT to use this skill

- FinFET-only or QFET-only work
- Layer naming questions → use `cffet-layer-nomenclature`
- Upstream SMTCell submodule workflows (this repo uses **inline** `SMTCellUCSD-2.0/`)
