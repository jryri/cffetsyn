# CFET / CFFET Layer Nomenclature (Convention A)

**Date:** 2026-06-27  
**Status:** Approved  
**Scope:** Symbol definitions for SMTCell CFET (main) and future CFFET work  
**Skill:** `docs/skills/cffet-layer-nomenclature/SKILL.md`

---

## 1. Purpose

Unify layer, via, row, pin, and power-model naming across specs, JSON layer files, solver code, and GDS. **Convention A** uses **face-first concatenated symbols**: `{F|B}{BOT|TOP}{PC}` for device tiers, `{F|B}M{level}` for metals.

Do **not** mix with underscore forms (`BPC_F`), literature numeric names (`BPC2`, `PC1`), or legacy short names in new CFFET files.

---

## 2. Three Orthogonal Axes

```
         Face (F/B)          Device Z (BOT→TOP)       Routing row (on FM0/BM0)
              │                      │                         │
   Front FM0 ─┼─ Back BM0    FBOTPC─┼─ FTOPPC             r=0 VSS … r=5 VDD
              │                      │                         │
         wafer sides            contact layers            horizontal tracks
```

| Axis | Question it answers | Example |
|------|---------------------|---------|
| Face | Which side of the wafer? | `FM0` vs `BM0` |
| Device Z | Which stacked device tier? | `FBOTPC` (bottom) vs `FTOPPC` (top) |
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

Inter-metal vias: `FV0`, `FV1` (front); `BV0`, `BV1` (back).

### 3.2 CFET blocks (CFFET = two blocks)

| Block | Face | Bottom tier | Top tier | Primary routing metal |
|-------|------|-------------|----------|------------------------|
| Back block | `B` | `BBOTPC` | `BTOPPC` | `BM0` |
| Front block | `F` | `FBOTPC` | `FTOPPC` | `FM0` |

Z-order (bottom → top of full CFFET stack):  
`BBOTPC → BTOPPC → [STV] → FBOTPC → FTOPPC → FM0` (simplified; exact JSON stack order TBD in CFFET layer file).

### 3.3 Vias

| Symbol | Connection | Role |
|--------|------------|------|
| `FTOPCA` / `FBOTCA` | `FTOPPC`/`FBOTPC` → `FM0` | Front contacts |
| `BTOPCA` / `BBOTCA` | `BTOPPC`/`BBOTPC` → `BM0` | Back contacts |
| `FV0`, `FV1` | Front inter-metal | Standard vias |
| `BV0`, `BV1` | Back inter-metal | Mirror of front |
| `FMIV` | `FBOTPC` ↔ `FTOPPC` | Intra-block cross-tier (CFET `MIV`) |
| `BMIV` | `BBOTPC` ↔ `BTOPPC` | Intra-block cross-tier |
| `STV` | `BTOPPC` ↔ `FBOTPC` | Sole inter-block stitch via (no F/B/TOP/BOT in name) |

Virtual overlap (solver): `FBOTVL` (`FBOTPC`–`FM0`), `BBOTVL` (`BBOTPC`–`BM0`) when using overlap connect.

### 3.4 Deprecated symbols (do not use in new files)

| Deprecated | Use instead |
|------------|-------------|
| `BPC_F`, `PC_F`, `BPC_B`, `PC_B` | `FBOTPC`, `FTOPPC`, `BBOTPC`, `BTOPPC` |
| `CA_F`, `BCA_F`, `CA_B`, `BCA_B` | `FTOPCA`, `FBOTCA`, `BTOPCA`, `BBOTCA` |
| `V0_F`, `V1_F`, `V0_B`, `V1_B` | `FV0`, `FV1`, `BV0`, `BV1` |
| `MIV_F`, `MIV_B` | `FMIV`, `BMIV` |
| `SV_BF`, `SVBTF`, `BTOPFBOTSV` | `STV` |
| `VL_F`, `VL_B` | `FBOTVL`, `BBOTVL` |

### 3.5 Merge types (constraints)

| Symbol | Condition | Penalty / note |
|--------|-----------|----------------|
| `DM` | Drain merge | Same `(col, row)` alignment on both faces |
| `GM` | Gate merge | Same column alignment |
| `FDM` | Field drain merge | Misaligned S/D; fin cut; **+1 CPP** |

Cross-face signal nets: **at least one** of `{DM, GM}` (or legal `FDM` path) required.

### 3.6 I/O pins

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

Signal pin rows (symmetric Option B): `r ∈ {1,2,3,4}` for `TRACK=3`; both `FBOTPC`/`FTOPPC` (and back equivalents) access all signal rows.

---

## 5. Literature Crosswalk (read-only)

Use when reading Flip-FET papers; **do not** use numeric names in new SMTCell files.

| Literature | Convention A |
|------------|--------------|
| FM0, BM0 | `FM0`, `BM0` |
| BPC2, PC1 (back pair) | `BBOTPC`, `BTOPPC` |
| BPC1, PC2 (front pair) | `FBOTPC`, `FTOPPC` |
| MIV between pair tiers | `FMIV` or `BMIV` |
| Cross-block stitch | `STV` |

Reference: Peng et al., VLSI 2025 — Flip-FET / CFFET architecture (A2 node).

---

## 6. SMTCell Main Branch Today

- Technology: **CFET SH**, single face (`F` implicit)
- Layer JSON: `PROBE3_CFET_2F_3T_4530OF0.json`, `PROBE3_CFET_2F_4T_4530OF0.json`
- Legacy names in JSON/code: `M0`, `PC`, `BPC`, `CA`, `BCA`
- Repo layout: `SMTCellUCSD-2.0/` vendored inline (no submodule)

When editing existing CFET files, keep legacy JSON keys unless doing a deliberate rename migration; **new docs and CFFET files** use Convention A symbols (`FBOTPC`, `FTOPPC`, …).

---

## 7. Non-Goals

- Renaming all existing CFET JSON keys in one shot
- QFET `H0`/`H1` mid-routing naming (separate technology)
- Underscore tier forms (`BPC_F`) or numeric tier suffix scheme (Convention B)
