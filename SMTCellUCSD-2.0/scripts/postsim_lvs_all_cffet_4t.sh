#!/usr/bin/env bash
# Post-sim LVS (+ GDS) for all OPTIMAL CFFET_4T_SH cells in synth_all_summary.tsv
set -uo pipefail
cd "$(dirname "$0")/.."

OUT=./output/PROBE3_CFFET_2F_4T_4530OF0/SH
CDL=input/cdl/PROBE_2F4T.cdl
LAYER=input/layer/PROBE3_CFFET_2F_4T_4530OF0.json
GDS_DIR="$OUT/gds"
SUMMARY="$OUT/postsim_lvs_summary.tsv"
PYTHON="${PYTHON:-./smtcell/bin/python3}"

mkdir -p "$GDS_DIR"
echo -e "cell\tgds\tlvs\tnotes" > "$SUMMARY"

cells=$(awk -F'\t' 'NR>1 && $2=="OPTIMAL"{print $1}' "$OUT/synth_all_summary.tsv")
count=0
pass=0

for cell in $cells; do
  count=$((count + 1))
  res="$OUT/result/${cell}.res"
  var="$OUT/result/${cell}.var"
  cfg="$OUT/config/${cell}.json"
  gds="$GDS_DIR/${cell}.gds"
  gds_st="SKIP"
  lvs_st="FAIL"
  notes=""

  if timeout 120 "$PYTHON" -m src.cellgen.postprocess.gds_CFFET_SH \
      --result_file "$res" \
      --subckt_name "$cell" \
      --layer "$LAYER" \
      --gds_file "$gds" >/dev/null 2>&1; then
    gds_st="OK"
  else
    gds_st="FAIL"
    notes="gds generation failed"
  fi

  if timeout 60 "$PYTHON" -m src.cellgen.postprocess.postsim_lvs \
      --cell "$cell" \
      --res "$res" \
      --cdl "$CDL" \
      --var "$var" \
      --config "$cfg" \
      --layer "$LAYER" > "/tmp/lvs_${cell}.txt" 2>&1; then
    lvs_st="PASS"
    pass=$((pass + 1))
  else
    lvs_st="FAIL"
    notes=$(head -3 "/tmp/lvs_${cell}.txt" | tr '\n' '; ')
  fi

  echo -e "${cell}\t${gds_st}\t${lvs_st}\t${notes}" >> "$SUMMARY"
  echo "$cell gds=$gds_st lvs=$lvs_st"
done

echo "LVS complete: $pass/$count PASS"
echo "Summary: $SUMMARY"
