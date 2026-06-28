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

## Gate modes: CG vs split gate (same column, CFFET)

| Mode | Same column N+P | Gate nets | CFET `CFET_3T_SH` | CFFET solver today |
|------|-----------------|-----------|-------------------|---------------------|
| **CG** (common gate) | Yes | N and P share **one** gate net | Default; `gate_share` → same x | Same |
| **Split gate** | Yes | N gate **≠** P gate (S vs S̄) | **Not modeled** | **Not modeled** — needs **MDI** at CFFET center |

**Iron rule:** `gate_share` only fires when both devices share the **same gate net** → enforces **CG**, forbids strict TG on one CG column.

**Gate cut** = different gate nets on **different gate columns** (S column vs S̄ column). Valid strict TG, but **not single gate column**.

## CFFET 正中間 — STV seam（strict TG 所在）

**Focus here for TG.** Do **not** redirect to FFET / F3ET / generic “SG process” when discussing CFFET pass gates.

CFFET Z-stack (bottom → top, one CPP column):

```
  FTOPPC ─┐
          │  front CFET block
  FBOTPC ─┤
  ═ STV ══╪══ CFFET 正中間（back block ↔ front block 唯一 stitch）
  BTOPPC ─┤
          │  back CFET block
  BBOTPC ─┘
```

- **STV** = sole inter-block via: `BTOPPC ↔ FBOTPC` (`AtMostOne STV/col`).
- **CFFET 正中間** = STV seam 一帶：back block 頂層 `BTOPPC` 與 front block 底層 `FBOTPC` 之間的垂直結構。
- Strict TG 的 **pass P + pass N + MDI** 落在這個 seam 區，不是 front-only CFET 的 `FMIV` 兩 tier，也不是用 FFET 文獻去套。

## Single-column CFFET strict TG — user DTCO stack

One CPP column, **top → bottom** (user-defined). **Do not paraphrase as “FMIV + gate_share”.**

```
  ┌─ N 短路          ← top N: S/D strapped (tie-off)
  ├─ common gate     ← upper gate region
  ├─ P               ← TG PMOS (pass), gate = S̄   ─┐
  ├─ MDI             ← split gate boundary         ├─ CFFET 正中間（STV seam）
  ├─ N               ← TG NMOS (pass), gate = S    ─┘
  ├─ CG              ← lower common gate region
  └─ P 短路          ← bottom P: S/D strapped (tie-off)
```

| Segment | Role | CFFET tier hint |
|---------|------|-----------------|
| **N 短路** (top) | Upper strap / tie-off N — not pass device | e.g. `FTOPPC` 一帶 |
| **common gate** | Upper gate electrode section | above seam |
| **P** | Active **TG PMOS**, gate **S̄** | seam upper (`BTOPPC` side) |
| **MDI** | **Middle Dielectric Isolation** — 上下 gate stack 間厚介電；split gate / multi-Vt 前提 | **CFFET 正中間**，P 與 N 之間 |
| **N** | Active **TG NMOS**, gate **S** | seam lower (`FBOTPC` side) |
| **CG** | Lower common gate region | below seam |
| **P 短路** (bottom) | Lower strap / tie-off P — not pass device | e.g. `BBOTPC` 一帶 |

**Channel (A↔B):** through **middle P + N** in parallel; **N 短路** / **P 短路** are outer diffusion straps, not arbitrary solver net shorts.

**Strict TG:** middle pair only — gate(N)=S, gate(P)=S̄, isolated by **MDI** at **CFFET 正中間**.

## MDI → split gate（物理機制）

**MDI** = **Middle Dielectric Isolation（中間介電層隔離）**。

在上下兩層電晶體 gate stack 之間刻意插入的**厚實絕緣層**，主要目的：

1. **阻斷上下層漏電路徑**（gate-to-gate、body coupling）
2. **隔離上下 gate 電場**，使上層 gate 電位不會驅動下層 channel（反之亦然）

因此 MDI 是 **split gate 的製程前提**，不是 routing merge 的別名：

| 結構 | 作用 | 能否替代 MDI |
|------|------|-------------|
| **MDI** | 上下 gate 電極**物理隔離** → 同欄可 gate(P)=S̄、gate(N)=S | — |
| **STV** | `BTOPPC↔FBOTPC` **channel / diffusion** stitch | 否（走 channel，不管 gate 隔離） |
| **FMIV/BMIV** | 同 block 內 `*BOTPC↔*TOPPC` channel 垂直 tie | 否 |
| **`gate_share`** | 同 gate net 的 N+P 同 gate column（**CG**） | 相反（禁止 split gate） |

### 對準 user stack 的 split gate 剖面

同一 CPP column、同一 gate column `x`，**channel 與 gate 分工**：

```
gate 方向（上→下）          channel 方向（同欄 x）
─────────────────          ─────────────────────
N 短路  (strap)            （非 pass）
common gate
P       gate = S̄  ─── MDI ───  gate = S       N  @ FBOTPC ─┐
════ MDI ════ 厚介電 ════                              ├── STV seam
N       gate = S           P pass @ BTOPPC ─── STV ───┘   （A↔B net）
CG
P 短路  (strap)
```

- **Pass 對**：P（上，S̄）+ N（下，S）= strict TG
- **MDI**：夾在 P gate stack 與 N gate stack 之間 → **split gate**
- **STV**：讓 P、N 的 S/D 接到同一 **channel net**（A↔B），與 MDI 正交
- **N短路 / P短路**：外圈 diffusion strap，不參與 pass

文獻（imec mCFET / VLSI'23）：MDI 使 top/bottom gate **獨立 work-function / Vt**，同一 gate column 可對上下 device 施加不同 gate 電位 — 對 TG 即 S 與 S̄。

### SMTCell 建模草案（討論用）

**辨識 TG split-gate 對**（CDL）：同 channel net、一 P 一 N、`gate(P)` 與 `gate(N)` 互補（S / S̄）。

**Placement 硬約束**（啟用 `mdi_tg` 時）：

| 變數 / 約束 | 內容 |
|-------------|------|
| `same ci` | pass P 與 pass N 同 device column |
| `z(P)=BTOPPC`, `z(N)=FBOTPC` | seam 上下 tier |
| `gate col` | 兩者同一 gate column `ci+1`（**靠 MDI 才合法**） |
| `mdi_at_col[ci]` | BOOL：此欄宣告 MDI split-gate primitive |
| `stv_on_channel` | channel net 在該 column 走 STV |
| `gate_share_at_col[gate_col]=0` | 此 gate column 標記為 **gate cut / split**（非 CG 欄） |

**隔離約束**：

- `S` 與 `S̄` net **禁止** GM/IRGM/任意 merge
- **禁止** 對 pass 對建立 `gate_share_*`（不同 net 已自然不建，但需防 CDL 誤接）

**與現有 `gate_cut` 的關係**：

- 現行：`gate_share_at_col=0` → 連續 gate cut window（給**不同 gate column 的 CG 切斷**）
- MDI split gate：`gate_share_at_col=0` 但 **同一 gate column** 上、下 tier 各接不同 net — 需 **`mdi_at_col` 豁免**「同 column 只能一 net」的隱含假設，改為 **同 column、不同 z 可不同 gate net**

**代價 / DRC**（objective 或 hard）：

- `mdi_at_col` 可能與 inner spacer、multi-Vt 共用製程模組（imec rMDI / eMDI）
- 可設 `AtMostOne mdi_at_col` per seam column（類 STV）

**現況 gap**：無 `mdi_at_col`、無 strap tier（N短路/P短路）、無「同 gate column + 不同 z gate net」的 pin 規則。

## CFET vs CFFET pass connectivity (solver)

### CFET — single front stack, CG pass legs

- PMOS @ `PC`, NMOS @ `BPC`; channel → **LISD** + **MIV**.
- Strict TG on **one CG column** is **impossible** (need S and S̄) unless **two gate columns** or SG process outside model.

### CFFET — four tiers + STV center

- Intra-block channel @ same x → **FMIV/BMIV**, not LISD (`z_eq` fails across BOT/TOP).
- Strict TG @ **CFFET 正中間:** middle **P + MDI + N** at STV seam; outer **N 短路 / P 短路** straps.
- Without MDI: two gate columns only (solver today).
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
| “Single-column TG = same gate column with CG” | **Impossible** for strict TG; need **MDI @ STV seam** or **two gate columns** |
| “FMIV short = MDI / split gate” | FMIV = **intra-block channel** tie; **MDI** = split gate at **CFFET 正中間** |
| “Discuss CFFET TG via FFET/F3ET SG” | Wrong framing — anchor on **STV seam stack** (N短路–P–MDI–N–P短路) |
| `enable_routing=false` = two-step P&R | **Diagnostic only** — one CP-SAT model with routing constraints stripped |
| Confusing **row** (M0 track) with **tier** (PC layer) | See `cffet-layer-nomenclature` axis table |

## References

- Guo et al., **ASPDAC 2026** — dual-sided 3D-stacked transistor standard-cell synthesis; DM/GM/FDM, inter-row MD/gate, DS-nets.
- Peng et al., **VLSI 2025** — FFET roadmap F3ET/F4ET/**CFFET**; separate FS/BS S/D epi vs CFET.
- Repo: `src/cellgen/archit/CFFET/main.py` (architecture comment block), `cross_face_merge.py`, `inter_row_merge.py`.
- Layer JSON: `input/layer/PROBE3_CFFET_2F_3T_4530OF0.json` vs `PROBE3_CFET_2F_3T_4530OF0.json`.
