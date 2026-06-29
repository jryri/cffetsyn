#!/usr/bin/env bash
# Long-run CFFET_4T_SH synthesis for cells that TIMEOUT or INFEASIBLE in quick batch.
set -uo pipefail
cd "$(dirname "$0")/.."

OUT=./output/PROBE3_CFFET_2F_4T_4530OF0/SH
SUMMARY="$OUT/synth_hard_long_summary.tsv"
PYTHON="${PYTHON:-./smtcell/bin/python3}"

# Per-cell outer timeout (seconds). Solver max_time matches via CONFIG_OVERRIDES.
TIMEOUT_CELLS=(
  MUX2_X1 BUF_X8 INV_X8
  AND3_X2 NAND4_X2 NOR4_X2 OR3_X2
  AOI21_X2 OAI21_X2 AOI22_X2
)
TIMEOUT_SEC=3600

INFEASIBLE_CELLS=(
  AND3_X1 OR3_X1 NAND4_X1 NOR4_X1 AND4_X1
  AOI22_X1 OAI22_X1 XOR2_X1
  DFFHQN_X1 DFFRNQ_X1 LHQ_X1
  XOR2AOI22_X1 AOI22XOR2_X1 NAND2NAND2AND2OR2_X1
)
INFEASIBLE_SEC=600

echo -e "cell\tgroup\tstatus\tseconds\tobj\tnotes" > "$SUMMARY"

run_one() {
  local cell="$1" group="$2" limit="$3"
  echo "========== [$group] $cell (limit ${limit}s) =========="
  make CONFIG=CFFET_4T_SH CELL_NAME="$cell" FORCE=1 \
    CONFIG_OVERRIDES="max_time.time=$limit" config >/dev/null 2>&1 || true
  t0=$(date +%s)
  if timeout "$limit" make CONFIG=CFFET_4T_SH CELL_NAME="$cell" spnr >/dev/null 2>&1; then
    st=$(grep -E 'status: (OPTIMAL|FEASIBLE|INFEASIBLE)' "$OUT/logs/${cell}.log" 2>/dev/null | tail -1 | awk '{print $2}' || echo "UNKNOWN")
  else
    ec=$?
    if [[ $ec -eq 124 ]]; then st="TIMEOUT"; else st="ERROR"; fi
  fi
  t1=$(date +%s)
  elapsed=$((t1 - t0))
  obj=""
  if [[ -f "$OUT/result/${cell}.res" ]]; then
    obj=$(grep '^\*\* Objective value:' "$OUT/result/${cell}.res" | awk '{print $4}')
  fi
  echo -e "${cell}\t${group}\t${st}\t${elapsed}\t${obj}" >> "$SUMMARY"
  echo "$cell -> $st (${elapsed}s)"
}

for cell in "${TIMEOUT_CELLS[@]}"; do
  run_one "$cell" "timeout_retry" "$TIMEOUT_SEC"
done
for cell in "${INFEASIBLE_CELLS[@]}"; do
  run_one "$cell" "infeasible_retry" "$INFEASIBLE_SEC"
done

echo "Done. Summary: $SUMMARY"
