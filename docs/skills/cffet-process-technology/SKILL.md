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

## CFET vs CFFET: How Pass Gates (TG) Connect

**Neither flow has a “TG macro”.** Both start from CDL (e.g. `MUX2_X1` transmission-gate netlist).

### CFET (`CFET_3T_SH`) — single front stack

- PMOS @ `PC` (`FTOPPC`), NMOS @ `BPC` (`FBOTPC`); **no `z_var`** — tier = device type.
- N+P sharing a pass **channel net** → **`pairwise_lisd_sharing`** (cross-MOS LISD): same **x**, correct flip, connects PC↔BPC.
- Intra-column vertical tie → **MIV** (BPC↔PC) in CFET vocabulary.
- This is the classic **vertical complementary pass gate** in one column.

### CFFET (`CFFET_3T_SH`) — dual block, four tiers

- Each device has **`z_var` ∈ {BBOTPC, BTOPPC, FBOTPC, FTOPPC}**; PMOS/NMOS each may land on **front or back block**.
- Generic **`pairwise_lisd_sharing` requires `z_eq` (same tier)** → **N@FBOTPC + P@FTOPPC do NOT LISD-share** (different tiers by design).
- Intra-block N+P pass junction → **`FMIV` / `BMIV`** (vertical tie **within** one CFET block) **+** routing on `FM0`/`BM0`, **not** planar LISD.
- If pass devices split across **faces** → **DM/GM/FDM** obligation + **STV** path.
- Multi-row MUX/TG meshes → **IRMD/IRGM** (ASPDAC inter-row MD/gate).

### “Short top S/D so CFFET → FFET and draw TG perfectly” — corrected

| Wrong mental model | Correct model |
|--------------------|---------------|
| CFFET “degenerates to FFET” by shorting | **FFET is the parent process**; **CFFET adds** a second back-to-back CFET stack (4-tier). |
| Arbitrary S/D short | Legal vertical tie is **`FMIV`/`BMIV`** (intra-block) or **DM/IRMD** (merge rules), not a new hack. |
| Same as CFET LISD | Only if you **restrict placement** to **one block** (e.g. all `F*` tiers) **and** route through **FMIV** like CFET MIV — still CFFET rules, simpler sub-case. |
| Disable dual-face = solved | Front-only helps (MUX2 often uses front only) but **routing mesh + merge obligations** still apply. |

**Practical TG-friendly policy (future DTCO, not default today):**

1. Bind pass-pair devices to **same face, same x**, N on `*BOTPC`, P on `*TOPPC`.
2. Treat **FMIV/BMIV** as mandatory channel merge for shared S/D nets (CFET-analog).
3. Keep gates on **IRGM** or aligned **gate sharing**; complementary gates stay **separate nets** (`S` / `!S`).
4. Avoid cross-face on pass nets → no FDM/STV on TG internal nets.

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
| `enable_routing=false` = two-step P&R | **Diagnostic only** — one CP-SAT model with routing constraints stripped |
| Confusing **row** (M0 track) with **tier** (PC layer) | See `cffet-layer-nomenclature` axis table |

## References

- Guo et al., **ASPDAC 2026** — dual-sided 3D-stacked transistor standard-cell synthesis; DM/GM/FDM, inter-row MD/gate, DS-nets.
- Peng et al., **VLSI 2025** — FFET roadmap F3ET/F4ET/**CFFET**; separate FS/BS S/D epi vs CFET.
- Repo: `src/cellgen/archit/CFFET/main.py` (architecture comment block), `cross_face_merge.py`, `inter_row_merge.py`.
- Layer JSON: `input/layer/PROBE3_CFFET_2F_3T_4530OF0.json` vs `PROBE3_CFET_2F_3T_4530OF0.json`.
