# CFFET Design Spec (v1)

**Date:** 2026-06-27  
**Status:** Draft — pending user review  
**Scope:** SMTCell `CFFET_3T_SH` — dual-face Flip-FET, extends CFET (Architecture A)  
**Nomenclature:** Convention A — see `docs/skills/cffet-layer-nomenclature/SKILL.md`  
**Baseline:** `main` CFET 3T M0ICPD (`CFET_3T_SH`)

---

## 1. Goal

Implement **CFFET** as a **subclass of CFET** (`class CFFET(CFET)`) modeling two back-to-back CFET blocks with Z-axis symmetry:

- **Back block:** `BBOTPC` / `BTOPPC` + `BM0` (M0ICPD)
- **Front block:** `FBOTPC` / `FTOPPC` + `FM0` (M0ICPD)
- **Sole stitch:** `STV` (`BTOPPC` ↔ `FBOTPC`)
- **Intra-block cross-tier:** `BMIV`, `FMIV`

v1 delivers full **Scope C** (see §8). **FDM is v2.**

---

## 2. Architecture Decision: A — `CFFET extends CFET`

### Why A (user choice)

Reuse CFET routing, M0ICPD power-row bans, cross-device MIV/long-via cost model, LISD/gate sharing framework, and DRC hooks — the same code path that already passes `CFET_3T_SH` regression.

### Critical extension: 4-tier placement

CFET assigns tier from **device model only** (no `z_var`). CFFET adds **`z_var`** (borrow QFET pattern) **only in CFFET** to select among four placement layers, while keeping CFET-style cross-tier routing semantics **per block**.

| Layer | `z` index (bottom→top) | Model (P_on_N) |
|-------|------------------------|----------------|
| `BBOTPC` | 0 | NMOS |
| `BTOPPC` | 1 | PMOS |
| `FBOTPC` | 2 | NMOS |
| `FTOPPC` | 3 | PMOS |

Tier restriction: `_cffet_model_tier_restriction()` (AllowedAssignments on `z_var`).

Diffusion alignment: **two** CFET-pair alignments (back: `BBOTPC`↔`BTOPPC`; front: `FBOTPC`↔`FTOPPC`).

### Refactor prerequisite (Phase 0)

Before overrides proliferate, extract CFET hardcoded `"PC"`/`"BPC"`/`"M0"` into **`c_tech` query methods**:

```python
# CFFET_Tech / refactored CFET_Tech
get_placement_layers() -> list[str]          # CFET: 2; CFFET: 4
get_front_route_metals() -> list[str]          # ["FM0"] legacy key "M0"
get_back_route_metals() -> list[str]          # ["BM0"]
get_intra_block_miv_pairs() -> list[tuple]     # CFET: [(FBOTPC,FTOPPC)]
get_stitch_via() -> str                        # CFFET only: "STV"
get_virtual_connect_pairs() -> list[tuple]    # [(FBOTPC,FM0), (BBOTPC,BM0)]
```

Gate CFET behavior with `TECHNOLOGY == "CFET"` so **CFET_3T/4T regression unchanged**.

---

## 3. Stack & Fine Grid

### 3.1 Layer JSON (`PROBE3_CFFET_2F_3T_*.json`)

Convention A `layer_name` keys (new file):

| Key | Role |
|-----|------|
| `BBOTPC`, `BTOPPC`, `FBOTPC`, `FTOPPC` | Placement tiers |
| `BM0`, `FM0` (`M0` alias tolerated in v1) | Horizontal route + `io_pin: true` both |
| `BM1`, `FM1`, `BM2`, `FM2` | Upper metals (v1 routing optional) |
| `BBOTCA`, `BTOPCA`, `FBOTCA`, `FTOPCA` | Contact vias |
| `BV0`, `FV0`, … | Inter-metal vias |
| `BMIV`, `FMIV` | Intra-block MIV vias |
| `STV` | Inter-block stitch via |
| Virtual | `BBOTVL`, `FBOTVL` overlap pairs |

Z-order (LGG bottom→top):  
`BBOTPC → BTOPPC → STV → FBOTPC → FTOPPC → BM0/FM0 → …`

### 3.2 Dual M0ICPD (`TRACK=3`, 6 fine rows per face)

Same semantics as CFET 3T, applied to **both** `FM0` and `BM0`:

```
row r:   0      1–4 signal      5
         VSS    (2T)            VDD
```

- `signal_row_indices = [1,2,3,4]` on all four PC tiers
- `_ban_signal_on_power_rows` on `{FM0, BM0, FBOTPC, FTOPPC, BBOTPC, BTOPPC}`

Virtual connect: `overlap` on `(FBOTPC, FM0)` and `(BBOTPC, BM0)`.

---

## 4. Merge & Stitch Constraints (v1)

### 4.1 Intra-block (reuse CFET patterns)

| Via | Constraint | CFET source |
|-----|------------|-------------|
| `FMIV` | AtMostOne per col | `_only_one_miv_per_col` scoped to front pair |
| `BMIV` | AtMostOne per col | same, back pair |
| Long via | AtMostOne per col per face | `_only_one_long_via_per_col` |
| LISD / gate share | Optional pairwise (intra-block) | `_pairwise_lisd_sharing`, `_pairwise_gate_sharing` |

### 4.2 STV

| Rule | Implementation |
|------|----------------|
| Graph edge | `STV` via layer between `BTOPPC` and `FBOTPC` |
| Placement | `_only_one_stv_per_col()` — AtMostOne STV edge per column |
| Routing | Cross-face nets without merge must route through STV or legal merge |

### 4.3 Cross-face merge (v1 — strict DM/GM)

Classify **cross-face signal nets** (terminals on back + front blocks).

| Type | Alignment | Constraint method |
|------|-----------|-------------------|
| **GM** | Same gate column | `_cross_face_gate_merge(net)` — mandatory `x_back == x_front` |
| **DM** | Same drain `(col, row)` | `_cross_face_drain_merge(net)` — mandatory alignment on pin-access rows |
| **At-least-one** | Per cross-face net | `_require_cross_face_merge(net)`: `AddBoolOr([has_gm, has_dm])` |

**v2:** `FDM` (+1 CPP fin-cut penalty).

Flow rule: cross-face LISD/gate share **does not** zero routing flow (same as CFET cross-tier rule); merge satisfies connectivity obligation.

### 4.4 Cross-face routing lower bound

Generalize `rt.cfet_cross_device_via_lower_bound` → `rt.cffet_cross_face_via_lower_bound`:

- Terminals on different blocks require ≥1 cross-layer flow through `STV`, `FMIV+BMIV` chain, or legal merge path.

---

## 5. Pin Policy (v1 Scope C)

### 5.1 Architecture rules

| Net class | Policy | Layers |
|-----------|--------|--------|
| **Input** | Single-sided per net | `FIN`→`FM0` only **or** `BIN`→`BM0` only |
| **Output** | Dual-sided per net | **Both** `FOUT` on `FM0` **and** `BOUT` on `BM0` |
| **Power** | M0ICPD rows | Not FIN/BIN |

### 5.2 Classification

Wire `config.INPUT_NET_NAMES` / `OUTPUT_NET_NAMES` into `Circuit` (collections already loaded, currently unused). Add CFFET names: `FIN`, `BIN`, `FOUT`, `BOUT`, plus legacy `A`, `B`, `Y`, …

Fallback when name not in collections: subckt pin order + heuristics (outputs often last pin).

### 5.3 SON model (replace CFET M1-only SON)

Follow QFET multi-layer SON collection on `io_pin` layers (`FM0`, `BM0`), with **CFFET-specific counting**:

| Net type | `num_extra_flow` | SON constraint |
|----------|------------------|----------------|
| Input | 1 | `sum(SON on assigned face layer) == 1`; other face forbidden |
| Output | 2 | `sum(SON on FM0 for k_out_F) == 1` **and** `sum(SON on BM0 for k_out_B) == 1` |
| Internal | 0 | — |

**Opposite of QFET** (`sum all layers == 1` either/or).

### 5.4 Test harness — input face assignment

`cell_config` schema:

```json
"pin_face": {
  "value": {
    "face_to_layer": { "front": "M0", "back": "BM0" },
    "input": {
      "assignment": "round_robin",
      "order": "cdl",
      "default_face": "front",
      "explicit": {}
    },
    "output": { "mode": "dual", "faces": ["front", "back"] }
  },
  "info": "[PIN][CFFET] Input: one face per net. Output: both faces."
}
```

Round-robin: input nets in CDL order → `front, back, front, …`

**INV_X1** (1 input): only `front` (suite-level balance via multi-input cells or `INV_BIN` variant).

Generated in `config.generate_config()` when `tech == "CFFET"`.

---

## 6. Code Layout

### 6.1 New files

| Path | Role |
|------|------|
| `src/cellgen/archit/CFFET/tech.py` | `CFFET_Tech(CFET_Tech)` |
| `src/cellgen/archit/CFFET/main.py` | `CFFET(CFET)` — overrides §4–5 |
| `src/cellgen/archit/CFFET/util.py` | `write_cffet_result`, via classify incl. `STV` |
| `input/layer/PROBE3_CFFET_2F_3T_*.json` | Stack |
| `input/presets/CFFET_3T_SH.mk` | Preset |
| `src/cellgen/postprocess/gds_CFFET_SH.py` | Dual-face GDS |
| `src/cellgen/postprocess/visualize_CFFET_4T.py` | View |

### 6.2 Modified files

| Path | Change |
|------|--------|
| `src/main.py` | Register `CFFET` |
| `src/cellgen/archit/CFET/tech.py` | Query methods (refactor, CFET-compatible) |
| `src/cellgen/archit/CFET/main.py` | Use query methods where safe |
| `src/cellgen/archit/config.py` | `CFFET` template + `pin_face` + round-robin |
| `src/cellgen/core/entity.py` | Input/output net classification |
| `src/cellgen/core/routing.py` | `routing_localization_cffet`, cross-face lower bound |
| `Makefile` | `gds` / `view` for `CFFET` |
| `input/pin_input_collection.json` | Add `FIN`, `BIN` |
| `input/pin_output_collection.json` | Add `FOUT`, `BOUT` |

---

## 7. Implementation Phases (subagent-driven)

Each phase: implementer subagent → spec review → quality review → `timeout 60` smoke.

| Phase | Deliverable | Gate |
|-------|-------------|------|
| **P0** | CFET tech query refactor + `CFFET` registry stub | CFET_3T regression PASS |
| **P1** | Layer JSON + preset + `CFFET_Tech` | LayerStack loads |
| **P2** | `_init_graph` dual M0ICPD + STV + virtual pairs | Graph build OK |
| **P3** | 4-tier `z_var` placement + pair DB alignment ×2 | Placement vars OK |
| **P4** | Routing: FMIV/BMIV/STV AtMostOne + dual power-row ban | `INV_X1` spnr **OPTIMAL** |
| **P5** | Cross-face GM/DM + at-least-one merge | 2-input gate feasible |
| **P6** | Pin policy: input single-face + output dual-face SON | IO nets legal |
| **P7** | GDS/view dual rails + STV | GDS opens, VSS/VDD row 0/5 both faces |
| **P8** | Regression suite | See §8 |

**v2 (out of scope):** FDM, 5nm pitch JSON, BM1/FM1 full routing, GUI preset.

---

## 8. Success Criteria (v1 Scope C)

1. `make CONFIG=CFFET_3T_SH CELL_NAME=INV_X1 spnr` → **OPTIMAL**
2. `make CONFIG=CFFET_3T_SH CELL_NAME=NAND2_X1 spnr` → **OPTIMAL** or **FEASIBLE** (round-robin inputs)
3. `make CONFIG=CFFET_3T_SH CELL_NAME=AOI21_X1 spnr` → **FEASIBLE** or better
4. Output nets: solver activates SON on **both** `FM0` and `BM0`
5. Input nets: each input SON on **one** face only; multi-input cells show FIN/BIN balance
6. Cross-face internal nets: GM or DM merge satisfied
7. `make CONFIG=CFET_3T_SH spnr` regression **unchanged**
8. `make CONFIG=CFFET_3T_SH gds` → dual-face M0ICPD rails visible

---

## 9. Non-Goals (v1)

- FDM insertion (+1 CPP)
- Renaming legacy CFET JSON keys
- `CFFET_4T` / M0BPR dual-face
- Submodule / upstream push
- New pytest suite (smoke via `make spnr`)

---

## 10. Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| CFET 2-tier hardcoding (~30 sites) | P0 query-method refactor + CFET regression |
| `z_var` in CFFET but not CFET | Isolated to `CFFET/main.py` overrides |
| Output dual-SON breaks flow model | Separate flow indices `k_F`, `k_B` per output net |
| INV infeasible with strict merge | Relax cross-face merge for single-input cells in config flag if needed |
| AOI infeasible on 3T dual-face | Accept FEASIBLE for v1; tune heuristics later |

---

## 11. References

- `docs/skills/cffet-layer-nomenclature/SKILL.md`
- `docs/superpowers/specs/2026-06-26-cfet-3t-m0icpd-design.md`
- `docs/superpowers/specs/2026-06-26-layer-nomenclature-design.md`
- Peng et al., VLSI 2025 — Flip-FET / CFFET
