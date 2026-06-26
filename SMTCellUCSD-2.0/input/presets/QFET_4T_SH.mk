# QFET 4-Track preset
TECH       = QFET
HEIGHT_CONFIG = SH
CHANNEL    = 2F
TRACK      = 4
CPP        = 42
M1P        = 42
M1OF       = 21
CDL_FILE   = input/cdl/PROBE_2F4T.cdl
LAYER_FILE = input/layer/PROBE3_QFET_2F_4T_4242OF21.json
# CELL_NAME  = AND2_X1 \
# 			 AOI22XOR2_X1 \
# 			 BUF_X1 \
# 			 DFFHQN_X1 \
# 			 DFFRNQ_X1 \
# 			 LHQ_X1 \
# 			 MUX2_X1 \
# 			 NAND2_X1 \
# 			 NOR2_X1 \
# 			 OR2_X1 \
# 			 XOR2_X1
CELL_NAME = AND2_X1\
             AND3_X2


# Cell-config overrides for this technology preset.
# Each entry is `key=value` (writes template[key].value), or dotted-path
# `key.sub=value` to target a nested field directly (e.g. max_time has
# both a .value boolean toggle AND a .time integer of seconds — the
# dotted form reaches the seconds without touching the toggle).
# Value type auto-detected: int, float, bool, "null", or fallback string.
# Example:  routing_stage=external  metal_cost=2  max_time.time=2000
CONFIG_OVERRIDES := \
  minimum_gate_cut_length=1 \
  lisd_routing=true \
  lig_routing=true \
  insert_num_db=2 \
  max_time.value=true \
  max_time.time=3600
