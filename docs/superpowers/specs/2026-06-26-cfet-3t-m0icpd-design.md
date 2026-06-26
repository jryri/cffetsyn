# CFET 3T M0ICPD Design Spec

**Date:** 2026-06-26  
**Status:** Approved for implementation  
**Scope:** SMTCellUCSD-2.0 CFET single-height, `TRACK=3`, in-cell M0 power (no BPR)

---

## 1. Goal

Add a new CFET technology preset **`CFET_3T_SH`** that models a **3-track standard cell** with **in-cell M0 power delivery** (`M0ICPD`):

- No BPR layer
- VDD/VSS occupy the top/bottom **0.5T** of the M0 fine grid
- Middle **2T** are signal-only M0 tracks
- PC and BPC are **z-layers**; M0 **rows** are **top-view routing tracks** (not tiers)

Existing **`CFET_4T_SH` + `M0BPR`** remains unchanged.

---

## 2. Fine Grid Model

SMTCell convention: `canvas_height = TRACK × m0_pitch × 2`.

For `TRACK=3` → **6 fine rows** (indices 0–5):

```
fine row:  0      1       2       3       4       5
           ┌──────┬───────┬───────┬───────┬───────┬──────┐
 M0 role:  │ VSS  │      signal (2T)              │ VDD  │
           │ 0.5T │  four fine rows = 2×1T       │ 0.5T │
           └──────┴───────┴───────┴───────┴───────┴──────┘
solver:    POWER  ←──────── SIGNAL ────────→     POWER
```

### Row semantics

| Fine row | Role | Signal nets | Power nets |
|----------|------|-------------|------------|
| 0 | M0 VSS rail (0.5T) | **Forbidden** | VSS only |
| 1–4 | M0 signal tracks (2T total) | Allowed | Forbidden |
| 5 | M0 VDD rail (0.5T) | **Forbidden** | VDD only |

### Pin access (Option B — symmetric)

PC and BPC are layers, not rows. Both device tiers may access **all signal rows**:

```python
SIGNAL_ROW_INDICES = [1, 2, 3, 4]
nmos_pin_access_ri = SIGNAL_ROW_INDICES   # BPC layer
pmos_pin_access_ri = SIGNAL_ROW_INDICES   # PC layer
POWER_ROW_INDICES  = {0: "VSS", 5: "VDD"}
```

Stacking (`P_on_N` / `N_on_P`) affects **which power net ties to which device column**, not row partitioning.

---

## 3. Architecture

### 3.1 `power_config` enum extension

| Value | When | Behavior |
|-------|------|----------|
| `M0BPR` | `TRACK=4` (existing) | VDD/VSS drawn **outside** signal tracks (`m0_pitch × (track+2)`) |
| `M0ICPD` | `TRACK=3` (new) | VDD/VSS on M0 fine rows 0 and 5 **inside** cell height |

`CFET_Tech` selects `M0ICPD` when `num_rt_track == 3` and `height_config == "SH"`.

### 3.2 Layered grid graph (`_init_graph`)

| Layer | Rows (M0ICPD / 3T) |
|-------|---------------------|
| M0 | all 6 fine rows |
| PC | all 6 fine rows |
| BPC | **signal rows only** `m0_rows[1:5]` (indices 1–4), **not** power rows 0/5 |

**Virtual connect** `(BPC, M0)`:

- `M0BPR` / 4T: keep `virtual_connect_method="boundary"` (existing)
- `M0ICPD` / 3T: use `virtual_connect_method="overlap"` so BPC can reach all shared signal rows (not only first/last)

### 3.3 CP-SAT constraints (new / updated)

1. **`_ban_signal_on_power_rows`** (new): For every non-power net, forbid M0 routing nodes on fine rows 0 and 5.
2. **`_ban_other_nets_on_pwr_columns`** (existing): Keep; still restricts VDD/VSS to device power columns on PC/BPC layers.
3. **SON row map** for `num_rt_track==3`: change from `[0, 2]` to **`[1, 4]`** (signal band edges).

Do **not** port FinFET `ban_middle_row_via_for_3T` unless routing tests show false conflicts (CFET has separate PC/BPC tiers).

### 3.4 GDS (`gds_CFET_SH.py`)

New branch `M0ICPD`:

- Draw VSS M0 strip at fine row 0 (0.5T band)
- Draw VDD M0 strip at fine row 5 (0.5T band)
- Draw LIG on those power rows (mirror `__lig_on_m0_bpr__` geometry but aligned to in-cell row coords)
- Cell **boundary height** = `3 × m0_pitch` (no `track+2` extension)
- Do **not** draw BPR shapes

---

## 4. Preset & inputs

### New files

- `input/presets/CFET_3T_SH.mk`
- `input/layer/PROBE3_CFET_2F_3T_4530OF0.json` (copy 4T layer stack; M0 pitch unchanged)

### Preset contents (draft)

```makefile
TECH          = CFET
HEIGHT_CONFIG = SH
CHANNEL       = 2F
TRACK         = 3
CPP           = 45
M1P           = 30
M1OF          = 0
CDL_FILE      = input/cdl/PROBE_2F4T.cdl
CELL_NAME     = INV_X1
```

DRC parameters remain PROBE3 defaults for v1 (not CFET_fp EOL=1/VR=0).

---

## 5. Success criteria (v1)

1. `make CONFIG=CFET_3T_SH spnr` → **OPTIMAL** or **FEASIBLE** for `INV_X1`
2. `make CONFIG=CFET_3T_SH CELL_NAME=AOI21_X1 spnr` → **FEASIBLE** or better
3. `make CONFIG=CFET_3T_SH gds` → GDS renders in-cell VSS/VDD on M0 rows 0/5 (no external M0BPR rails)
4. `make CONFIG=CFET_4T_SH spnr` regression unchanged

---

## 6. Non-goals (v1)

- BPR layer or CFET_fp library (2) physical parity
- M1 lower signal track modeling
- CFFET / DP-merge integration
- New pytest suite (repo has no CFET unit tests; use `make spnr` smoke)

---

## 7. File touch list

| File | Change |
|------|--------|
| `src/cellgen/archit/CFET/tech.py` | `M0ICPD` power_config selection |
| `src/cellgen/archit/CFET/main.py` | pin rows, graph rows, power-row ban, SON map |
| `src/cellgen/postprocess/gds_CFET_SH.py` | `__m0_icpd__`, boundary height |
| `input/presets/CFET_3T_SH.mk` | new preset |
| `input/layer/PROBE3_CFET_2F_3T_4530OF0.json` | new layer file |
| `README.md`, `doc/SMTCell2_GUIDE.md` | document 3T / M0ICPD |

---

## 8. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| BPC only had power rows in old 3T graph | Give BPC `m0_rows[1:5]` + `overlap` virtual connect |
| AOI infeasible on 3T | Accept for v1 if INV passes; tune cell JSON heuristics later |
| GDS height mismatch vs solver | Derive boundary from `3 × m0_pitch`, not `track+2` |
