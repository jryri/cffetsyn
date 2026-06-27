# CFFET FDM v2 Design

**Date:** 2026-06-27  
**Status:** Implemented (v2)  
**Follow-up:** v3 multi-row cells for AOI21-class placement capacity

## Goal

Add **Field Drain Merge (FDM)** as a third cross-face connectivity path when aligned **DM** / **GM** cannot satisfy dual-face nets.

## Merge types (Convention A)

| Symbol | Condition | Cost |
|--------|-----------|------|
| GM | Cross-face, same gate column (`x_eq`) | — |
| DM | Cross-face, aligned S/D (`x_eq` + flip match) | — |
| FDM | Cross-face, misaligned S/D (`|x1-x2| = db_dist`) | +1 CPP (objective weight 1000) |

## Implementation

| Component | Path |
|-----------|------|
| Merge vars + obligation | `SMTCellUCSD-2.0/src/cellgen/archit/CFFET/cross_face_merge.py` |
| CFFET wiring | `CFFET/main.py` — placement + routing obligation |
| Routing flow sharing | `routing.py` — `_gather_cross_face_shareable_vars` |
| Objective | `objective.py` — `Objective.fdm_penalty` |
| Config flags | `enable_cross_face_merge`, `enforce_cross_face_merge` |

## Config

```json
"enable_cross_face_merge": { "value": true },
"enforce_cross_face_merge": { "value": true }
```

Set `enforce_cross_face_merge: false` to create FDM vars without hard obligation (debug).

## Smoke results (CFFET_3T_SH, 2026-06-27)

| Cell | v1 | v2 + FDM |
|------|-----|----------|
| INV_X1 / NAND2_X1 / AND2_X1 | OPTIMAL | OPTIMAL |
| AOI21_X1 / OAI21_X1 | INFEASIBLE | INFEASIBLE (→ v3 multi-row) |
| MUX2_X2 / NAND2_X2 | presolve INFEASIBLE | unchanged (→ v3 / 4T) |

FDM unlocks cross-face **misaligned** drain paths but does not add placement row budget. AOI21-class failures remain a **multi-row / 4T** problem (v3).

## References

- ASPDAC'26 FFET paper (uploaded) — dynamic FDM insertion + multi-row
- `docs/superpowers/specs/2026-06-27-cffet-design.md` §4.3
