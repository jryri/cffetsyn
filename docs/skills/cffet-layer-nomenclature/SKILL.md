---
name: cffet-layer-nomenclature
description: Use when naming or documenting CFET/CFFET layers, vias, rows, pins, or power models in SMTCell specs, JSON layer files, code, or commits—and layer names feel ambiguous (M0 vs FM0, BPC vs back face, row vs tier).
---

# CFET / CFFET Layer Nomenclature (Convention A)

## Overview

**Convention A = Face-first naming.** Every symbol states **which wafer face** (`F` / `B`) before role. Three orthogonal axes must never be conflated:

| Axis | Symbol | Meaning |
|------|--------|---------|
| **Face** | `F`, `B` | Front / back wafer side |
| **Device tier (Z)** | `BPC_*`, `PC_*` | Bottom / top device contact **layer** in one CFET block |
| **Routing row** | `r = 0…2T−1` | Horizontal **track index** on `FM0` / `BM0` (not a Z layer) |

**Iron rule:** `BPC` means **Bottom** poly contact tier, **not** Back face. Back face uses `_B` suffix or `B` prefix on metal (`BM0`).

## When to Use

- Writing CFFET design specs, layer JSON, SMT constraints, GDS postprocess, or commit messages
- Mapping legacy SMTCell names (`M0`, `PC`, `BPC`) to literature (`FM0`, Flip-FET)
- Reviewing PRs where "PC row" or "BPC = back" appears
- Planning dual-face power (`M0ICPD` on `FM0` **and** `BM0`)

**When NOT to use:** FinFET-only or QFET-only work with no CFET/CFFET context.

## Convention A Grammar

```
{Face?}{Role}{Level?}     Face ∈ {F, B}; omit Face only for single-face CFET legacy
```

### Metal routing layers

| Symbol | Direction | Notes |
|--------|-----------|-------|
| `FM0`, `FM1`, `FM2` | H, V, H | Front-side metals |
| `BM0`, `BM1`, `BM2` | H, V, H | Back-side metals (mirror of front) |

### Device contact layers (one CFET block)

| Symbol | Z in block | Device (P_on_N) |
|--------|------------|-----------------|
| `BPC_F`, `PC_F` | bottom → top | NMOS → PMOS (front block) |
| `BPC_B`, `PC_B` | bottom → top | NMOS → PMOS (back block) |

### Vias (prefix face when face-specific)

| Symbol | Connects |
|--------|----------|
| `CA_F` | `PC_F` → `FM0` |
| `BCA_F` | `BPC_F` → `FM0` |
| `CA_B` | `PC_B` → `BM0` |
| `BCA_B` | `BPC_B` → `BM0` |
| `V0_F`, `V1_F` | `FM0↔FM1`, `FM1↔FM2` |
| `MIV_F` | `BPC_F` ↔ `PC_F` (intra-block cross-tier) |
| `MIV_B` | `BPC_B` ↔ `PC_B` |
| `SV_BF` | `PC_B` ↔ `BPC_F` (inter-block stitch; name = lower→upper tier) |

Merge types (constraints, not layers): `DM`, `GM`, `FDM`.

### I/O pins (block level)

| Pin | Face | Rule |
|-----|------|------|
| `FIN`, `FOUT` | Front (`FM0`) | Input: front **only**; output: front (+ back for dual-sided out) |
| `BIN`, `BOUT` | Back (`BM0`) | Mirror of front pin policy |

## Legacy SMTCell Mapping (main branch)

Single-face **CFET** code/JSON uses short names = **implicit `F`**:

| Legacy (SMTCell) | Convention A | Context |
|------------------|--------------|---------|
| `M0` | `FM0` | Always |
| `M1`, `M2` | `FM1`, `FM2` | Always |
| `PC` | `PC_F` | CFET SH |
| `BPC` | `BPC_F` | CFET SH |
| `CA`, `BCA` | `CA_F`, `BCA_F` | |
| `MIV` (code concept) | `MIV_F` | BPC↔PC cross-tier edge |

**Migration rule:** New CFFET JSON/specs **must** use Convention A explicitly. Do not introduce `BPC2`/`PC1` numeric suffixes alongside `_F`/`_B`.

## Power models (not metal names)

| Symbol | TRACK | Behavior |
|--------|-------|----------|
| `M0ICPD` | 3 | `VSS`/`VDD` on rows `r=0` and `r=2T−1` of **each** active face metal |
| `M0BPR` | 4 | External BPR rails outside signal tracks |

Fine rows: `row_count = TRACK × 2`. Signal rows: `r ∈ {1,…,2T−2}`. Power rows forbid signal nets.

## Writing Checklist

Before merging layer/spec changes:

1. Every new symbol includes face when both faces exist (`FM0`+`BM0`).
2. Docs distinguish **layer** (`PC_F`) vs **row** (`FM0, r=2`).
3. No bare `M0` in CFFET specs (use `FM0` or state "legacy alias").
4. Cross-face nets name merge type (`DM`/`GM`/`FDM`) and stitch (`SV_BF`).
5. Pin side rules reference `FIN`/`BIN`/`FOUT`/`BOUT`, not "M0 pin" alone.

## Common Mistakes

| Wrong | Correct |
|-------|---------|
| "BPC = back side" | `BPC_*` = **Bottom** tier; back face = `_B` or `BM0` |
| "PC row 3" | `PC_F layer, FM0 row r=3` |
| `BPC2`, `PC1` mixed with `BPC_F` | Pick Convention A only |
| Dual-face CFFET with one `M0ICPD` rail | Apply `M0ICPD` separately on `FM0` and `BM0` |

## Reference

Full tables, stack diagram, and CFFET block layout: `docs/superpowers/specs/2026-06-26-layer-nomenclature-design.md`
