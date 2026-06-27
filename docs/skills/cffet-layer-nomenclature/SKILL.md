---
name: cffet-layer-nomenclature
description: Use when naming or documenting CFET/CFFET layers, vias, rows, pins, or power models in SMTCell specs, JSON layer files, code, or commits—and layer names feel ambiguous (M0 vs FM0, BPC vs back face, row vs tier).
---

# CFET / CFFET Layer Nomenclature (Convention A)

## Overview

**Convention A = Face-first concatenated naming.** Device tiers use `{Face}{BOT|TOP}{Role}` (no underscores). Three orthogonal axes must never be conflated:

| Axis | Symbol | Meaning |
|------|--------|---------|
| **Face** | `F`, `B` | Front / back wafer side |
| **Device tier (Z)** | `FBOTPC`, `FTOPPC`, … | Bottom / top device contact **layer** in one CFET block |
| **Routing row** | `r = 0…2T−1` | Horizontal **track index** on `FM0` / `BM0` (not a Z layer) |

**Iron rule:** `BOT` means **Bottom** device tier, **not** Back face. Back face starts with **`B`** prefix on the symbol (`BBOTPC`, `BM0`).

## When to Use

- Writing CFFET design specs, layer JSON, SMT constraints, GDS postprocess, or commit messages
- Mapping legacy SMTCell names (`M0`, `PC`, `BPC`) to Convention A
- Reviewing PRs where "PC row" or "BPC = back" appears
- Planning dual-face power (`M0ICPD` on `FM0` **and** `BM0`)

**When NOT to use:** FinFET-only or QFET-only work with no CFET/CFFET context.

## Convention A Grammar

```
Device tier:  {F|B}{BOT|TOP}{PC}
Metal:        {F|B}M{level}          e.g. FM0, BM1
Contact via:  {F|B}{BOT|TOP}CA        e.g. FTOPCA, FBOTCA
Inter-tier:   {F|B}MIV
Stitch via:   STV                     sole inter-block stitch (unique)
```

### Metal routing layers

| Symbol | Direction | Notes |
|--------|-----------|-------|
| `FM0`, `FM1`, `FM2` | H, V, H | Front-side metals |
| `BM0`, `BM1`, `BM2` | H, V, H | Back-side metals (mirror of front) |

### Device contact layers (one CFET block)

| Symbol | Z in block | Device (P_on_N) | Deprecated |
|--------|------------|-----------------|------------|
| `FBOTPC` | bottom | NMOS (front block) | ~~`BPC_F`~~ |
| `FTOPPC` | top | PMOS (front block) | ~~`PC_F`~~ |
| `BBOTPC` | bottom | NMOS (back block) | ~~`BPC_B`~~ |
| `BTOPPC` | top | PMOS (back block) | ~~`PC_B`~~ |

### Vias

| Symbol | Connects | Deprecated |
|--------|----------|------------|
| `FTOPCA` | `FTOPPC` → `FM0` | ~~`CA_F`~~ |
| `FBOTCA` | `FBOTPC` → `FM0` | ~~`BCA_F`~~ |
| `BTOPCA` | `BTOPPC` → `BM0` | ~~`CA_B`~~ |
| `BBOTCA` | `BBOTPC` → `BM0` | ~~`BCA_B`~~ |
| `FV0`, `FV1` | `FM0↔FM1`, `FM1↔FM2` | ~~`V0_F`, `V1_F`~~ |
| `BV0`, `BV1` | `BM0↔BM1`, `BM1↔BM2` | ~~`V0_B`, `V1_B`~~ |
| `FMIV` | `FBOTPC` ↔ `FTOPPC` | ~~`MIV_F`~~ |
| `BMIV` | `BBOTPC` ↔ `BTOPPC` | ~~`MIV_B`~~ |
| `STV` | inter-block stitch (`BTOPPC` ↔ `FBOTPC`) | ~~`SV_BF`, `SVBTF`~~ |

`STV` is the **only** inter-block stitch via in CFFET — no face/tier prefix needed.

Virtual overlap (solver): `FBOTVL` (`FBOTPC`–`FM0`), `BBOTVL` (`BBOTPC`–`BM0`).

Merge types (constraints, not layers): `DM`, `GM`, `FDM`.

### I/O pins (block level)

| Pin | Face metal | Rule |
|-----|------------|------|
| `FIN`, `FOUT` | `FM0` | Input: front **only**; output: front (+ back for dual-sided out) |
| `BIN`, `BOUT` | `BM0` | Mirror of front pin policy |

## Legacy SMTCell Mapping (main branch)

Single-face **CFET** code/JSON uses short names = **implicit front**:

| Legacy (SMTCell) | Convention A | Context |
|------------------|--------------|---------|
| `M0` | `FM0` | Always |
| `M1`, `M2` | `FM1`, `FM2` | Always |
| `PC` | `FTOPPC` | CFET SH |
| `BPC` | `FBOTPC` | CFET SH |
| `CA`, `BCA` | `FTOPCA`, `FBOTCA` | |
| `MIV` (code concept) | `FMIV` | FBOTPC↔FTOPPC cross-tier edge |

**Migration rule:** New CFFET JSON/specs **must** use Convention A symbols above. Do not use `BPC_F`, `PC_F`, or literature `BPC2`/`PC1`.

## Power models (not metal names)

| Symbol | TRACK | Behavior |
|--------|-------|----------|
| `M0ICPD` | 3 | `VSS`/`VDD` on rows `r=0` and `r=2T−1` of **each** active face metal |
| `M0BPR` | 4 | External BPR rails outside signal tracks |

Fine rows: `row_count = TRACK × 2`. Signal rows: `r ∈ {1,…,2T−2}`. Power rows forbid signal nets.

## Writing Checklist

Before merging layer/spec changes:

1. Device tiers use `FBOTPC`/`FTOPPC`/`BBOTPC`/`BTOPPC` — never `BPC_F` or `PC_B`.
2. Docs distinguish **layer** (`FTOPPC`) vs **row** (`FM0, r=2`).
3. No bare `M0` in CFFET specs (use `FM0` or state "legacy alias").
4. Cross-face nets name merge type (`DM`/`GM`/`FDM`) and stitch via (`STV`).
5. Pin side rules reference `FIN`/`BIN`/`FOUT`/`BOUT`, not "M0 pin" alone.

## Common Mistakes

| Wrong | Correct |
|-------|---------|
| "BPC = back side" | `FBOTPC` = front **Bottom** tier; back face = `B`-prefixed (`BBOTPC`, `BM0`) |
| "PC row 3" | `FTOPPC layer, FM0 row r=3` |
| `BPC_F`, `PC_B`, `BPC2` | `FBOTPC`, `BTOPPC`, `BBOTPC` only |
| Dual-face CFFET with one `M0ICPD` rail | Apply `M0ICPD` separately on `FM0` and `BM0` |

## Reference

Full tables, stack diagram, and CFFET block layout: `docs/superpowers/specs/2026-06-26-layer-nomenclature-design.md`
