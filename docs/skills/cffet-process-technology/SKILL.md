---
name: cffet-process-technology
description: Use when CFET, FFET, Flip-FET, CFFET, dual-sided, pass gate, TG, DM/GM/FDM, STV, or tier vs face vs row are confused—or when mapping SMTCell presets to Peng VLSI'25 / Guo ASPDAC'26 papers.
---

# CFET / FFET / CFFET Process Technology (SMTCell)

## Overview

**Three names, three scales — do not collapse them.**

| Name | What it is (literature) | SMTCell preset | Transistor tiers per face |
|------|-------------------------|----------------|---------------------------|
| **CFET** | N/P stacked on **frontside only**; shared column; BPR/M0ICPD | `CFET_3T_SH` | **2** (`FBOTPC`≈BPC, `FTOPPC`≈PC) |
| **FFET / Flip-FET** | **Dual-sided** 3D stack: FS **and** BS each have own N/P epi; flip process; `FM0`+`BM0` signals | *(no separate preset — concept)* | **2 per face** (F3ET/F4ET) |
| **CFFET** | **CFET-based FFET**: back-to-back **two CFET blocks** → **4-tier** ultra-stack on FFET process | `CFFET_3T_SH` | **4** (`BBOTPC`,`BTOPPC`,`FBOTPC`,`FTOPPC`) |

**Iron rule:** CFFET is **not** “CFET with optional back face”. It is **FFET process + 4-tier back-to-back CFET** (Peng et al., VLSI 2025). FFET is **not** “degraded CFFET” — CFFET is the **extension**.

**Companion skills:** `cffet-layer-nomenclature` (naming), `cffet-synthesizer` (commands/code).

## Technology Roadmap (A14 → A2, Peng VLSI'25)

```
FinFET / FFETFin
    → FFET (dual-sided FS+BS signal & power)
        → F3ET (self-aligned FS/BS gates, 2-tier per side)
            → F4ET (Forksheet + embedded PR, cell height → 2T)
                → CFFET @ A2 (ultra-stacked 4-tier, back-to-back CFET on FFET)
```

**CFET vs FFET process (VLSI'25):** In CFET, N/P share one frontside stack with high-AR epi challenges. In FFET/F3ET, **N/P S/D epi form separately on FS and BS** — key process difference; dual-sided metal gate, gate-last on BS.

**ASPDAC'26 (Guo et al.)** targets **dual-sided 3D-stacked transistors** (= FFET family): back-to-back N/P, dual interconnects, **2.5T double-row** cells, merge-aware placement + dual-side routing.

## Physical Stack in This Repo (`CFFET_3T_SH`)

Convention A names; JSON: `input/layer/PROBE3_CFFET_2F_3T_4530OF0.json`.

```
        [Front route]  FM0(M0) — FM1 — FM2
              ↑ FTOPCA / FBOTCA (long)     ↑ FMIV
        [Front CFET block]  FTOPPC (PMOS) / FBOTPC (NMOS)
              ↑ STV  ← sole inter-block stitch
        [Back CFET block]   BTOPPC (PMOS) / BBOTPC (NMOS)
              ↑ BTOPCA / BBOTCA (long)     ↑ BMIV
        [Back route]   BM0 — BM1
```

- **Face:** `F*` = front wafer side, `B*` = back (after flip).
- **Tier (Z):** `*BOTPC` = bottom device row in that block, `*TOPPC` = top device row. **`BOT` ≠ back face.**
- **Row (r):** index on `FM0`/`BM0` routing grid (`M0ICPD`: r=0 VSS, r=2T−1 VDD). **Not a device tier.**

LGG routing indices (solver graph, bottom→top):  
`BM1(0) BM0(1) BBOTPC(2) BTOPPC(3) FBOTPC(4) FTOPPC(5) M0(6) M1(7) M2(8)` — STV is a **via** between `BTOPPC`↔`FBOTPC`, not a metal tier index.

## Connectivity Primitives (ASPDAC'26 ↔ SMTCell)

Dual-sided cells connect nets through **merge structures**, not “draw a wire and hope”:

| Symbol | Paper / physical | SMTCell module | When |
|--------|------------------|----------------|------|
| **DM** | Drain merge (aligned S/D) | `cross_face_merge.dm_cf_*` | Cross-face, same x, S/D terminal match |
| **GM** | Gate merge (aligned gates) | `cross_face_merge.gm_cf_*` | Cross-face, same x, shared gate net, N+P |
| **FDM** | Field drain merge (misaligned; fin cut + tall FS↔BS via) | `cross_face_merge.fdm_*` | Cross-face, adjacent columns; **+1 CPP** penalty |
| **IRMD** | Inter-row MD (S/D) | `inter_row_merge.irmd_*` | **Same z, same x, different y** |
| **IRGM** | Inter-row gate tie | `inter_row_merge.irgm_*` | Gate net, same z, different y, N+P |
| **FMIV / BMIV** | Intra-block vertical tie | LGG via edges | `FBOTPC↔FTOPPC`, `BBOTPC↔BTOPPC` |
| **STV** | Inter-block stitch | LGG + `AtMostOne STV/col` | `BTOPPC↔FBOTPC` |

**DS-net:** net whose devices/pins span **both** FS and BS (`FM0` and `BM0`). Requires **≥1 merge** among {GM, DM, FDM} when cross-face pairs exist (`enforce_cross_face_merge`).

## Transmission Gate (TG) — strict definition

**Do not confuse TG with MUX pass pairs or with “both gates tied to S”.**

| | **TG (Transmission Gate)** | **MUX pass leg** (e.g. `MUX2_X1` MM8+MM3) |
|--|---------------------------|---------------------------------------------|
| Topology | NMOS ∥ PMOS, same A↔B channel | Same |
| NMOS gate | **S** | Often **S** (same net as PMOS) |
| PMOS gate | **S̄** (NOT S) | Often **S** (not S̄) |
| Control | Complementary **gate** drives | Complementary **device type** + one wire |
| Textbook | Yes | Implementation shortcut, **not strict TG** |

Strict TG truth table: S=1 → N on P off; S=0 → N off P on. Requires **two gate nets** on the pass pair.

## Gate modes: CG vs SG (same column)

| Mode | Same column N+P | Gate nets | CFET `CFET_3T_SH` | CFFET solver today |
|------|-----------------|-----------|-------------------|---------------------|
| **CG** (common gate) | Yes | N and P share **one** gate net | Default; `gate_share` → same x | Same |
| **SG** (split gate) | Yes | N gate **≠** P gate (S vs S̄) | **Not modeled** — needs F3ET-style independent gate metals | **Not modeled** |

**Iron rule:** `gate_share` in SMTCell only fires when both devices share the **same gate net** → enforces **CG**, forbids strict TG on one CG column.

**Gate cut** = different gate nets on **different gate columns** (S column vs S̄ column). Valid strict TG, but **not single gate column**.

## Single-column CFFET strict TG — design space

Target: one CPP column, one face, A↔B bidirectional pass.

```
        A o──────── channel net ────────o B
              FTOPPC  PMOS   gate = S̄
                 ║ FMIV  (channel vertical tie)
              FBOTPC  NMOS   gate = S
              same device column x
```

### What single column always needs (channel)

| Item | Rule |
|------|------|
| Face | N and P on **same** face (front `F*` or back `B*`) — no STV on pass channel |
| x | Same device column |
| z | N @ `*BOTPC`, P @ `*TOPPC` |
| Channel | **`FMIV`** (front) or **`BMIV`** (back) on shared S/D net — legal vertical tie, not arbitrary metal short |

### Strict TG gates — two implementation paths

**Path A — Two gate columns (no SG)**  
- gate(N)=S on gate col α, gate(P)=S̄ on gate col β, α≠β  
- Channel still one device column + FMIV  
- Matches current solver (no SG); **not single gate column**

**Path B — Single gate column + SG (process)**  
- gate(N)=S, gate(P)=S̄ on **same device column**, independent gate metals (F3ET **SG** class)  
- Requires **SG DRC + solver vars** — **not** in repo today  
- **CG `gate_share` must NOT** bind S and S̄ to same x gate net

### User DTCO stack — single-column strict TG (top → bottom)

One CPP column, **top to bottom** (user-defined). **Do not paraphrase as “FMIV + gate_share”.**

```
  ┌─ N 短路          ← top N: S/D strapped (tie-off)
  ├─ common gate     ← upper gate region
  ├─ P               ← TG PMOS (pass), gate = S̄
  ├─ MDI             ← **split gate boundary** (SG — separates S̄ from S)
  ├─ N               ← TG NMOS (pass), gate = S
  ├─ CG              ← lower common gate region
  └─ P 短路          ← bottom P: S/D strapped (tie-off)
```

| Segment | Role |
|---------|------|
| **N 短路** (top) | Upper strap / tie-off N — not the pass device |
| **common gate** | Upper gate electrode section |
| **P** | Active **TG PMOS**, gate **S̄** |
| **MDI** | **Split gate** — physical separator between P-gate (S̄) and N-gate (S); ASPDAC-class **MD** interconnect, not “FMIV = SG” |
| **N** | Active **TG NMOS**, gate **S** |
| **CG** | Lower common gate region |
| **P 短路** (bottom) | Lower strap / tie-off P — not the pass device |

**Channel (A↔B):** parallel through the **middle P + N**; top N short + bottom P short are **outer diffusion straps**, not arbitrary solver net shorts.

**Strict TG:** middle pair only — gate(N)=S, gate(P)=S̄, split by **MDI**.

**SMTCell gap:** no `MDI` SG primitive; no top-N-short / bottom-P-short tier rules; `gate_share` still assumes CG when gate nets differ incorrectly. Future: SG via MDI + strap tiers + FMIV on pass channel if needed.

## CFET vs CFFET pass connectivity (solver)

### CFET — single front stack, CG pass legs

- PMOS @ `PC`, NMOS @ `BPC`; channel → **LISD** + **MIV**.
- Strict TG on **one CG column** is **impossible** (need S and S̄) unless **two gate columns** or SG process outside model.

### CFFET — four tiers, front/back choice

- Channel @ same x → **FMIV/BMIV**, not LISD (`z_eq` fails across BOT/TOP).
- Strict TG: Path A (two gate cols) or Path B (SG + process short/straps).
- MUX2 CDL pass legs (both gates `S`) are **not** strict TG — do not use them to validate TG flow.

## SMTCell Preset ↔ Paper Mapping

| Paper term | This repo |
|------------|-----------|
| Dual-sided 3D-stacked transistor | `TECH=CFFET` orchestrator |
| CFET (front-only stack) | `TECH=CFET`, layers `PC`/`BPC`/`M0` |
| CFFET (4-tier on FFET) | `TECH=CFFET`, 4 `*BOTPC/*TOPPC` + `STV` |
| Frontside / backside I/O | `pin_face`: `M0` (front) / `BM0` (back) |
| 2.5T double-row DHL | `TRACK=3` → 6 fine rows; multi-row placement v3 |
| Merge-aware placement | `cross_face_merge.py`, `inter_row_merge.py` |
| FDM insertion | `fdm_pair_vars` + `fdm_penalty` objective |

## Common Mistakes (incl. prior agent errors)

| Mistake | Truth |
|---------|-------|
| “CFFET = CFET + BM0 routing option” | CFFET = **two CFET blocks** on **FFET dual-sided process** + **STV** seam |
| “FFET = simplified CFFET” | **Opposite:** CFFET extends FFET to 4-tier |
| “BOT tier = back face” | `FBOTPC` = front **Bottom** tier; back face = **`B*`** prefix (`BBOTPC`, `BM0`) |
| “TG uses same LISD as CFET in CFFET” | N+P on `FBOTPC`+`FTOPPC` → **FMIV vertical path**, not same-tier LISD |
| “MUX pass (both gates S) = TG” | Strict TG needs **gate(N)=S, gate(P)=S̄** |
| “Single-column TG = same gate column with CG” | **Impossible** for strict TG; need **SG** or **two gate columns** |
| “FMIV short = SG” | FMIV = **channel** tie only; SG = **separate gate metals** |
| `enable_routing=false` = two-step P&R | **Diagnostic only** — one CP-SAT model with routing constraints stripped |
| Confusing **row** (M0 track) with **tier** (PC layer) | See `cffet-layer-nomenclature` axis table |

## References

- Guo et al., **ASPDAC 2026** — dual-sided 3D-stacked transistor standard-cell synthesis; DM/GM/FDM, inter-row MD/gate, DS-nets.
- Peng et al., **VLSI 2025** — FFET roadmap F3ET/F4ET/**CFFET**; separate FS/BS S/D epi vs CFET.
- Repo: `src/cellgen/archit/CFFET/main.py` (architecture comment block), `cross_face_merge.py`, `inter_row_merge.py`.
- Layer JSON: `input/layer/PROBE3_CFFET_2F_3T_4530OF0.json` vs `PROBE3_CFET_2F_3T_4530OF0.json`.
