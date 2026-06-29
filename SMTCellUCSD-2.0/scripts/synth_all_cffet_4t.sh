#!/usr/bin/env bash
# Batch CFFET_4T_SH synthesis for all PROBE_2F4T CDL cells.
set -uo pipefail
cd "$(dirname "$0")/.."

OUT=./output/PROBE3_CFFET_2F_4T_4530OF0/SH
SUMMARY="$OUT/synth_all_summary.tsv"
echo -e "cell\tstatus\tseconds\tobj\ttiers\tnotes" > "$SUMMARY"

cell_timeout() {
  case "$1" in
    MUX2_*|DFF*|LHQ_*|XOR2*|AOI22*|OAI22*|NAND2NAND2*|AND4_*|*_X4|*_X8) echo 300 ;;
    *) echo 120 ;;
  esac
}

grep '^\.SUBCKT' input/cdl/PROBE_2F4T.cdl | awk '{print $2}' | while read -r cell; do
  lim=$(cell_timeout "$cell")
  echo "========== $cell (timeout ${lim}s) =========="
  t0=$(date +%s)
  if timeout "$lim" make CONFIG=CFFET_4T_SH CELL_NAME="$cell" spnr > /dev/null 2>&1; then
    st=$(grep -E 'status: (OPTIMAL|FEASIBLE|INFEASIBLE|UNKNOWN)' "$OUT/logs/${cell}.log" 2>/dev/null | tail -1 | awk '{print $2}' || echo "NO_LOG")
  else
    ec=$?
    if [[ $ec -eq 124 ]]; then st="TIMEOUT"; else st="ERROR"; fi
  fi
  t1=$(date +%s)
  elapsed=$((t1 - t0))
    tiers=""
    if [[ -f "$OUT/result/${cell}.res" ]]; then
    obj=$(grep '^\*\* Objective value:' "$OUT/result/${cell}.res" | awk '{print $4}')
    tiers=$(python3 -c "
import sys
res=open(sys.argv[1])
mode=False
s=set()
for line in res:
    if line.startswith('** Placement Result'): mode=True; continue
    if line.startswith('**') and mode: break
    p=line.split()
    if mode and len(p)>=17: s.add(p[3])
print(','.join(sorted(s)))
" "$OUT/result/${cell}.res" 2>/dev/null || true)
  fi
  echo -e "${cell}\t${st}\t${elapsed}\t${obj}\t${tiers}" >> "$SUMMARY"
  echo "$cell -> $st (${elapsed}s)"
done

echo "Done. Summary: $SUMMARY"
