# CFET / CFFET Layer Nomenclature (Convention A)

**Date:** 2026-06-27  
**Status:** Approved  
**Scope:** Symbol definitions for SMTCell CFET (main) and future CFFET work  
**Skill:** `docs/skills/cffet-layer-nomenclature/SKILL.md`

---

## 1. Purpose

Unify layer, via, row, pin, and power-model naming across specs, JSON layer files, solver code, and GDS. **Convention A** uses **Face suffix** (`_F` / `_B`) on device tiers and explicit `F`/`B` prefixes on metals.

Do **not** mix with literature numeric tier names (`BPC2`, `PC1`) in the same codebase.

---

## 2. Three Orthogonal Axes

```
         Face (F/B)          Device Z (BPC→PC)        Routing row (on FM0/BM0)
              │                      │                         │
   Front FM0 ─┼─ Back BM0     BPC_* ─┼─ PC_*              r=0 VSS … r=5 VDD
              │                      │                         │
         wafer sides            contact layers            horizontal tracks
```

| Axis | Question it answers | Example |
|------|---------------------|---------|
| Face | Which side of the wafer? | `FM0` vs `BM0` |
| Device Z | Which stacked device tier? | `BPC_F` (bottom) vs `PC_F` (top) |
| Row | Which horizontal track? | `FM0, r=2` (signal) |

---

## 3. Convention A Symbol Table

### 3.1 Front / back metals

| Symbol | Lit. alias | Dir. | SMTCell legacy (CFET main) |
|--------|------------|------|----------------------------|
| `FM0` | Front M0 | H | `M0` |
| `FM1` | Front M1 | V | `M1` |
| `FM2` | Front M2 | H | `M2` |
| `BM0` | Back M0 | H | *(not in main yet)* |
| `BM1` | Back M1 | V | *(not in main yet)* |
| `BM2` | Back M2 | H | *(not in main yet)* |

### 3.2 CFET blocks (CFFET = two blocks)

| Block | Face | Bottom tier | Top tier | Primary routing metal |
|-------|------|-------------|----------|------------------------|
| Back block | `B` | `BPC_B` | `PC_B` | `BM0` |
| Front block | `F` | `BPC_F` | `PC_F` | `FM0` |

Z-order (bottom → top of full CFFET stack):  
`BPC_B → PC_B → [SV_BF stitch region] → BPC_F → PC_F → FM0` (simplified; exact JSON stack order TBD in CFFET layer file).

### 3.3 Vias

| Symbol | Connection | Role |
|--------|------------|------|
| `CA_F` / `BCA_F` | `PC_F`/`BPC_F` → `FM0` | Front contacts |
| `CA_B` / `BCA_B` | `PC_B`/`BPC_B` → `BM0` | Back contacts |
| `V0_F`, `V1_F` | Front inter-metal | Standard vias |
| `V0_B`, `V1_B` | Back inter-metal | Mirror of front |
| `MIV_F` | `BPC_F` ↔ `PC_F` | Intra-block cross-tier (CFET `MIV`) |
| `MIV_B` | `BPC_B` ↔ `PC_B` | Intra-block cross-tier |
| `SV_BF` | `PC_B` ↔ `BPC_F` | Inter-block vertical stitch |

Virtual overlap (solver): `VL_F` (`BPC_F`–`FM0`), `VL_B` (`BPC_B`–`BM0`) when using overlap connect.

### 3.4 Merge types (constraints)

| Symbol | Condition | Penalty / note |
|--------|-----------|----------------|
| `DM` | Drain merge | Same `(col, row)` alignment on both faces |
| `GM` | Gate merge | Same column alignment |
| `FDM` | Field drain merge | Misaligned S/D; fin cut; **+1 CPP** |

Cross-face signal nets: **at least one** of `{DM, GM}` (or legal `FDM` path) required.

### 3.5 I/O pins

| Pin | Metal | Policy |
|-----|-------|--------|
| `FIN` | `FM0` | Input nets: front face only |
| `BIN` | `BM0` | Input nets: back face only (choose one face per input) |
| `FOUT` | `FM0` | Output: required on front |
| `BOUT` | `BM0` | Output: required on back (dual-sided output) |

---

## 4. Power Models

| Config | TRACK | Rails |
|--------|-------|-------|
| `M0ICPD` | 3 | `r=0` → VSS, `r=2T−1` → VDD on each face metal |
| `M0BPR` | 4 | External BPR outside `2T` signal rows |

CFET 3T on main: `M0ICPD` on `FM0` only (`M0` legacy name).  
CFFET target: `M0ICPD` on **both** `FM0` and `BM0` with identical row semantics.

Signal pin rows (symmetric Option B): `r ∈ {1,2,3,4}` for `TRACK=3`; both `BPC_*` and `PC_*` layers access all signal rows.

---

## 5. Literature Crosswalk (read-only)

Use when reading Flip-FET papers; **do not** use numeric names in new SMTCell files.

| Literature | Convention A |
|------------|--------------|
| FM0, BM0 | `FM0`, `BM0` |
| BPC2, PC1 (back pair) | `BPC_B`, `PC_B` |
| BPC1, PC2 (front pair) | `BPC_F`, `PC_F` |
| MIV between pair tiers | `MIV_F` or `MIV_B` |
| Cross-block stitch | `SV_BF` |

Reference: Peng et al., VLSI 2025 — Flip-FET / CFFET architecture (A2 node).

---

## 6. SMTCell Main Branch Today

- Technology: **CFET SH**, single face (`F` implicit)
- Layer JSON: `PROBE3_CFET_2F_3T_4530OF0.json`, `PROBE3_CFET_2F_4T_4530OF0.json`
- Legacy names in JSON/code: `M0`, `PC`, `BPC`, `CA`, `BCA`
- Repo layout: `SMTCellUCSD-2.0/` vendored inline (no submodule)

When editing existing CFET files, keep legacy JSON keys unless doing a deliberate rename migration; **new docs and CFFET files** use Convention A.

---

## 7. Non-Goals

- Renaming all existing CFET JSON keys in one shot
- QFET `H0`/`H1` mid-routing naming (separate technology)
- Numeric tier suffix scheme (Convention B)
