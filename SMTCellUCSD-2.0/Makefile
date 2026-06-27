################################################
# Tool config                                  #
#                                              #
# External-tool locations are machine-specific #
# and are NOT hardcoded here. Set your own in  #
# an untracked config.mk (copy config.mk.example) #
# or via environment variables / the command   #
# line. Unset tools fall back to the bare       #
# command name, resolved on PATH. See the       #
# "Tool Setup" section of the README.          #
################################################

# User overrides (untracked). `-include` does not error when absent.
-include config.mk

# Optional install root: when set, tool defaults resolve to
# $(TOOLS_DIR)/<cmd>; otherwise the bare command name (found on PATH).
TOOLS_DIR ?=
_tool = $(if $(TOOLS_DIR),$(TOOLS_DIR)/$(1),$(1))

# Layout / format conversion
KLAYOUT     ?= $(call _tool,klayout)
GDT2GDS     ?= $(call _tool,gdt2gds.Linux)
GDS2GDT     ?= $(call _tool,gds2gdt.Linux)

# Python interpreter
PYTHON ?= $(shell which python3)
# Always run Python unbuffered so log lines stream through tee / less -R
# the moment they're emitted (no 4 KB stdio buffer parked behind a pipe).
# `override` is essential: when PYTHON is set on the command line
# (`make spnr PYTHON=/path/to/python`), Make's command-line override
# normally beats Makefile assignment. `override` flips that precedence so
# we can still append `-u` to a user-provided interpreter path.
override PYTHON := $(PYTHON) -u

# CAD sign-off tools (Cadence / Synopsys). Bare command names resolve on
# PATH after you source the vendor setup; pin a specific install in config.mk.
PEGASUS     ?= $(call _tool,pegasus)
QUANTUS     ?= $(call _tool,quantus)
QRCTECHGEN  ?= $(call _tool,Techgen)
LIBERATE    ?= $(call _tool,liberate)
ICVALIDATOR ?= $(call _tool,icv)

# Optional job-scheduler prefix (e.g. `bsub -I`, `srun`, `qsub`). Empty by
# default so recipes run locally; set QUEUE in config.mk to dispatch jobs.
QUEUE ?=

# Make `cmd | tee log` return cmd's exit status instead of tee's, so a failed
# solver run piped to tee in the `spnr` recipe surfaces a non-zero exit code
# (the GUI and CI rely on this to detect failures).
SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c

# Load preset
CONFIG ?= FinFET_4T_SH
ifeq ($(wildcard input/presets/$(CONFIG).mk),)
  $(error Unknown CONFIG="$(CONFIG)". Run 'make list-configs' to see available presets)
endif
include input/presets/$(CONFIG).mk

# Derived variables
CELL_PREFIX ?= PROBE3
FLAG_LOG_CONSTR ?= False
LIBNAME=$(CELL_PREFIX)_$(TECH)_$(CHANNEL)_$(TRACK)T_$(CPP)$(M1P)OF$(M1OF)


OUT_DIR=./output/$(LIBNAME)/$(HEIGHT_CONFIG)
# `?=` lets the preset's LAYER_FILE win when set (e.g. QFET uses input/layer/*.json);
# falls back to ./input/config/<lib>.layer for techs that don't override.
LAYER_FILE?=./input/config/$(LIBNAME).layer

# Fall back to bundled repo inputs when a preset points at a file that was not
# migrated into this tree (the CFET/FinFET presets reference an ASAP7 netlist /
# .layer kept elsewhere). Missing netlist -> the bundled PROBE_2F4T CDL; missing
# layer -> the bundled input/layer/<LIBNAME>.json.
ifeq ($(wildcard $(CDL_FILE)),)
  CDL_FILE := input/cdl/PROBE_2F4T.cdl
endif
ifeq ($(wildcard $(LAYER_FILE)),)
  LAYER_FILE := input/layer/$(LIBNAME).json
endif
################################################
# Commands to run core flow                    #
################################################
list-configs:
	@echo "Available presets (use CONFIG=<name>):"
	@for f in input/presets/*.mk; do \
		name=$$(basename $$f .mk); \
		desc=$$(head -1 $$f | sed 's/^# *//'); \
		printf "  %-20s %s\n" "$$name" "$$desc"; \
	done

show-config:
	@echo "TECH          = $(TECH)"
	@echo "HEIGHT_CONFIG = $(HEIGHT_CONFIG)"
	@echo "CHANNEL       = $(CHANNEL)"
	@echo "TRACK         = $(TRACK)"
	@echo "CPP           = $(CPP)"
	@echo "M1P           = $(M1P)"
	@echo "M1OF          = $(M1OF)"
	@echo "CDL_FILE      = $(CDL_FILE)"
	@echo "CELL_NAME     = $(CELL_NAME)"
	@echo "LIBNAME       = $(LIBNAME)"
	@echo "OUT_DIR       = $(OUT_DIR)"

# By default an already-generated config/<cell>.json is KEPT (never
# overwritten), so re-running after toggling more cells preserves any tuned
# per-cell settings. Pass FORCE=1 to regenerate all of them from the preset.
config:
	mkdir -p $(OUT_DIR)/config
	$(QUEUE) $(PYTHON) -m src.cellgen.archit.config \
		--track $(TRACK) \
		--tech $(TECH) \
		--height_config $(HEIGHT_CONFIG) \
		--cell_names $(CELL_NAME) \
		--output_dir $(OUT_DIR) \
		$(addprefix --override ,$(CONFIG_OVERRIDES)) \
		$(if $(filter 1 true True,$(FORCE)),--force)

spnr:
	mkdir -p $(OUT_DIR)
	mkdir -p $(OUT_DIR)/result
	mkdir -p $(OUT_DIR)/logs
	mkdir -p $(OUT_DIR)/constraint
	mkdir -p $(OUT_DIR)/view
	$(foreach CELL,$(CELL_NAME),\
		$(QUEUE) $(PYTHON) -m src.main \
			--mode spnr \
			--tech $(TECH) \
			--layer $(LAYER_FILE) \
			--lib_name $(LIBNAME) \
			--track $(TRACK) \
			--height_config $(HEIGHT_CONFIG) \
			--cell_config $(OUT_DIR)/config/$(CELL).json \
			--netlist $(CDL_FILE) \
			--cell_names $(CELL) \
			--output_dir $(OUT_DIR) \
			--flag_log_constraints $(FLAG_LOG_CONSTR) \
			2>&1 | tee $(OUT_DIR)/logs/$(CELL).log;)

gds:
	@mkdir -p $(OUT_DIR)/gds
ifeq ($(TECH),QFET)
	$(foreach CELL,$(CELL_NAME),\
		$(QUEUE) $(PYTHON) -m src.cellgen.postprocess.gds_QFET_SH \
			--result      $(OUT_DIR)/result/$(CELL).res \
			--layer       $(LAYER_FILE) \
			--subckt      $(CELL) \
			--gds-dir     $(OUT_DIR)/gds \
			--lib-name    $(LIBNAME) \
			--cell-config $(OUT_DIR)/config/$(CELL).json;)
else ifeq ($(TECH),CFET)
	$(foreach CELL,$(CELL_NAME),\
		$(QUEUE) $(PYTHON) -m src.cellgen.postprocess.gds_CFET_SH \
			--result_file $(OUT_DIR)/result/$(CELL).res \
			--subckt_name $(CELL) \
			--layer       $(LAYER_FILE) \
			--gds_file    $(OUT_DIR)/gds/$(LIBNAME).gds;)
else ifeq ($(TECH),CFFET)
	$(foreach CELL,$(CELL_NAME),\
		$(QUEUE) $(PYTHON) -m src.cellgen.postprocess.gds_CFFET_SH \
			--result_file $(OUT_DIR)/result/$(CELL).res \
			--subckt_name $(CELL) \
			--layer       $(LAYER_FILE) \
			--gds_file    $(OUT_DIR)/gds/$(LIBNAME).gds;)
else ifeq ($(TECH),FinFET)
	$(foreach CELL,$(CELL_NAME),\
		$(QUEUE) $(PYTHON) -m src.cellgen.postprocess.gds_FinFET_SH \
			--result_file $(OUT_DIR)/result/$(CELL).res \
			--subckt_name $(CELL) \
			--layer       $(LAYER_FILE) \
			--gds_file    $(OUT_DIR)/gds/$(LIBNAME).gds;)
else
	@echo "ERROR: 'make gds' not wired for TECH=$(TECH). Expected QFET|CFET|CFFET|FinFET."; exit 1
endif

lef:
	make gds_to_gdt
	mkdir -p $(OUT_DIR)/lef
	$(PYTHON) src/utility/genLEF.py \
	$(OUT_DIR)/gdt/$(LIBNAME).gdt \
	$(OUT_DIR)/lef/$(LIBNAME).lef

status:
	@for CELL in $(CELL_NAME); do \
	  printf "%s\t\t%s\t\t%s\t\t%s\n" \
	    $$CELL \
	    "$$(grep -E "Elapsed time" $(OUT_DIR)/logs/$${CELL}.log | awk '{print $$7}')" \
	    "$$(grep -E -m 1 "^status: "   $(OUT_DIR)/logs/$${CELL}.log | awk '{print $$2}')" \
		"$$(grep -E -m 1 "Obj#1 obj_cpp              = " $(OUT_DIR)/logs/$${CELL}.log | awk '{print $$4}')" ; \
	done

################################################
# Commands to debug                            #
################################################

viewcell:
	mkdir -p $(OUT_DIR)/view
	$(foreach CELL,$(CELL_NAME),\
		$(QUEUE) $(PYTHON) -m src.cellgen.postprocess.visualize_$(TECH)_4T \
			$(OUT_DIR)/result/$(CELL).res \
			$(OUT_DIR)/view/$(CELL).png;)

verify_pins:
ifeq ($(TECH),CFFET)
	$(foreach CELL,$(CELL_NAME),\
		$(QUEUE) $(PYTHON) -m src.cellgen.archit.CFFET.pin_audit \
			--var $(OUT_DIR)/result/$(CELL).var \
			--res $(OUT_DIR)/result/$(CELL).res \
			--config $(OUT_DIR)/config/$(CELL).json \
			--layer $(LAYER_FILE);)
else
	@echo "ERROR: 'make verify_pins' wired for TECH=CFFET only (got $(TECH))."; exit 1
endif

gds_to_gdt:
	mkdir -p $(OUT_DIR)/gdt
	$(GDS2GDT) $(OUT_DIR)/gds/$(LIBNAME).gds -o $(OUT_DIR)/gdt/$(LIBNAME).gdt

gdt_to_gds:
	mkdir -p $(OUT_DIR)/gds
	$(GDT2GDS) $(OUT_DIR)/gdt/$(LIBNAME).gdt -o $(OUT_DIR)/gds/$(LIBNAME).gds